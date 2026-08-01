#!/usr/bin/env python3
"""Collect market and AI news into a unified SQLite research database."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

from db_adapter import (
    adapt_sql,
    count_items_by_source as db_count_items_by_source,
    get_connection,
    init_news_schema,
    item_exists as db_item_exists,
    make_stable_id,
    placeholder,
    write_items as db_write_items,
)
from jin10_mcp import Jin10McpClient


COLLECTOR_SOURCE_TYPE = {
    "jin10": "jin10",
    "github": "github_trending",
    "rss": "rss",
    "product_hunt": "product_hunt",
    "hacker_news": "hacker_news",
}
ALL_COLLECTORS = set(COLLECTOR_SOURCE_TYPE)


def requested_collectors(source: str) -> set[str]:
    return set(ALL_COLLECTORS) if source == "all" else {source}


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DB = Path("data/news_research.sqlite")
DEFAULT_CONFIG = Path("config/sources.yaml")
GITHUB_TRENDING_URL = "https://github.com/trending"
PRODUCT_HUNT_GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"
HN_API_BASE = "https://hacker-news.firebaseio.com/v0"


@dataclass
class NewsItem:
    source_type: str
    source_name: str
    published_at: str | None
    title: str
    content: str
    url: str
    author: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Jin10, GitHub Trending, Product Hunt, Hacker News, and RSS into SQLite."
    )
    parser.add_argument("--date", default="today", help="today or YYYY-MM-DD.")
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="SQLite database path. Ignored when ALPHA_DB_BACKEND=postgresql.",
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="RSS sources YAML config."
    )
    parser.add_argument(
        "--source",
        choices=[
            "all",
            "jin10",
            "github",
            "rss",
            "product_hunt",
            "hacker_news",
        ],
        default="all",
        help="Source to collect. Default: all.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Optional per-source item limit."
    )
    parser.add_argument("--timeout", type=int, default=20, help="Request timeout.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print summary without writing SQLite.",
    )
    parser.add_argument(
        "--replace-date",
        action="store_true",
        help="Delete selected source rows for the target date before writing.",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help=(
            "DB-first: only collect sources that have fewer than --min-rows rows "
            "for the target date. Sources already present are skipped (no network "
            "call). Ignored when --replace-date is set."
        ),
    )
    parser.add_argument(
        "--min-rows",
        type=int,
        default=None,
        help=(
            "With --only-missing, the row count at or above which a source counts "
            "as already present. Default: per-source thresholds from the config's "
            "collect_min_rows map (falling back to 1). Passing N overrides all "
            "sources uniformly."
        ),
    )
    parser.add_argument(
        "--no-watermark",
        action="store_true",
        help=(
            "Disable the Jin10 pagination watermark (stop when a page is mostly "
            "already in the DB) and always crawl up to the 10-page cap."
        ),
    )
    return parser.parse_args()


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def resolve_date_key(value: str) -> str:
    if value == "today":
        return now_shanghai().strftime("%Y-%m-%d")
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit("--date must be 'today' or YYYY-MM-DD") from exc


def iso_in_shanghai(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt.astimezone(SHANGHAI).isoformat(timespec="seconds")


def parse_any_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%H:%M",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%H:%M":
                today = now_shanghai()
                dt = dt.replace(year=today.year, month=today.month, day=today.day)
            return dt.replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None


def parse_feed_time(entry: Any) -> datetime | None:
    for field in ("published", "updated", "created"):
        dt = parse_any_datetime(entry.get(field))
        if dt:
            return dt
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(field)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def item_date_key(item: NewsItem, fallback_date_key: str) -> str:
    dt = parse_any_datetime(item.published_at)
    if dt:
        return dt.astimezone(SHANGHAI).strftime("%Y-%m-%d")
    return fallback_date_key


# source_type 的 published_at 是当日 00:00 快照（github_trending）或按日诊断行
# （error），跨天相同，id 种子必须带 date_key 才能让每日各行独立；其余源带真实
# 时间戳，不带 date_key，同一文章跨天重采只留一行。
_DATE_KEYED_ID_SOURCE_TYPES = {"github_trending", "error"}


def item_stable_id(item: NewsItem, date_key: str) -> str:
    """DB 行 id：原生 id 优先（Product Hunt / Hacker News），否则走 db_adapter
    的权威 make_stable_id。"""
    metadata = item.metadata or {}
    if metadata.get("stable_id"):
        return str(metadata["stable_id"])
    return make_stable_id(
        item.source_type,
        item.source_name,
        item.url,
        item.published_at,
        item.title,
        date_key=date_key if item.source_type in _DATE_KEYED_ID_SOURCE_TYPES else None,
    )


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146 Safari/537.36"
            )
        }
    )
    return session


@contextmanager
def init_db(db_path: Path) -> Iterator[Any]:
    with get_connection(str(db_path)) as con:
        init_news_schema(con)
        yield con


def write_items(con: Any, items: list[NewsItem], date_key: str) -> int:
    fetched_at = now_shanghai().isoformat(timespec="seconds")
    rows: list[dict[str, Any]] = []
    for item in items:
        row_date = item_date_key(item, date_key)
        if row_date != date_key:
            continue
        rows.append(
            {
                "id": item_stable_id(item, row_date),
                "source_type": item.source_type,
                "source_name": item.source_name,
                "published_at": item.published_at,
                "title": item.title,
                "content": item.content,
                "url": item.url,
                "author": item.author,
                "tags": item.tags or [],
                "metadata": item.metadata or {},
                "raw": item.raw or {},
            }
        )
    return db_write_items(con, rows, date_key, fetched_at)


def selected_source_types(source: str) -> list[str]:
    mapping = {
        "jin10": ["jin10"],
        "github": ["github_trending"],
        "rss": ["rss"],
        "product_hunt": ["product_hunt"],
        "hacker_news": ["hacker_news"],
        "all": [
            "jin10",
            "github_trending",
            "rss",
            "product_hunt",
            "hacker_news",
        ],
    }
    return mapping[source]


def delete_date_rows(con: Any, date_key: str, source_types: list[str]) -> int:
    ph = placeholder()
    placeholders = ",".join(ph for _ in source_types)
    sql = f"DELETE FROM items WHERE date_key = {ph} AND source_type IN ({placeholders})"
    params = [date_key, *source_types]
    if hasattr(con, "execute"):
        cur = con.execute(sql, params)
    else:
        cur = con.cursor()
        cur.execute(adapt_sql(sql), params)
    return cur.rowcount


def jin10_record_to_item(record: Any) -> NewsItem | None:
    """Map one raw Jin10 flash record to a NewsItem (None for non-dict rows)."""
    if not isinstance(record, dict):
        return None
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    title = str(
        record.get("title")
        or data.get("title")
        or record.get("content")
        or data.get("content")
        or data.get("vip_title")
        or ""
    ).strip()
    content = str(
        record.get("content")
        or data.get("content")
        or title
        or data.get("vip_desc")
        or ""
    ).strip()
    return NewsItem(
        source_type="jin10",
        source_name="金十数据 MCP 电报",
        published_at=iso_in_shanghai(
            parse_any_datetime(
                record.get("time")
                or record.get("created_at")
                or data.get("time")
                or data.get("created_at")
            )
        ),
        title=title,
        content=content,
        url=str(
            record.get("url")
            or data.get("source_link")
            or data.get("url")
            or data.get("pic")
            or ""
        ),
        author=str(record.get("source") or data.get("source") or "") or None,
        tags=[str(tag) for tag in record.get("tags", []) if tag],
        metadata={
            "important": record.get("important"),
            "type": record.get("type"),
            "channel": record.get("channel", []),
            "mcp_source": True,
        },
        raw=record,
    )


def collect_jin10(
    session: requests.Session,
    timeout: int,
    limit: int | None,
    db_path: Path | None = None,
    use_watermark: bool = True,
) -> list[NewsItem]:
    del session, timeout

    records: list[dict[str, Any]]
    with ExitStack() as stack:
        con = None
        if use_watermark and db_path is not None:
            try:
                con = stack.enter_context(init_db(db_path))
            except Exception as exc:
                # 水位线 DB 不可用只是少了增量短路，降级为全量翻页（10 页上限）。
                print(
                    f"⚠️  collect_news: 金十水位线 DB 不可用，降级为全量翻页: {exc}",
                    file=sys.stderr,
                )
        record_known = None
        if con is not None:
            def record_known(record: Any) -> bool:  # noqa: F811
                item = jin10_record_to_item(record)
                return item is not None and db_item_exists(
                    con, item_stable_id(item, "")
                )

        records = fetch_jin10_mcp_records(limit=limit, record_known=record_known)

    items: list[NewsItem] = []
    for record in records:
        item = jin10_record_to_item(record)
        if item is None:
            continue
        items.append(item)
        if limit and len(items) >= limit:
            break
    return items


def extract_jin10_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("items", "list", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    for key in ("items", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def extract_jin10_cursor(payload: dict[str, Any]) -> str | None:
    candidates: list[Any] = [
        payload.get("cursor"),
        payload.get("next_cursor"),
        payload.get("nextCursor"),
    ]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("cursor"),
                data.get("next_cursor"),
                data.get("nextCursor"),
            ]
        )
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return None


def fetch_jin10_mcp_records(
    limit: int | None = None,
    record_known: Any = None,
) -> list[dict[str, Any]]:
    """Fetch Jin10 flash pages, newest first.

    Watermark: page 1 is always fetched in full; from page 2 on, when more
    than half of a page's records already exist in the DB (record_known), the
    crawl has caught up with the previous run and pagination stops.  The
    10-page hard cap stays as the final backstop.
    """
    client = Jin10McpClient()
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    max_pages = 10 if limit is None else max(1, min(10, (limit + 19) // 20))
    try:
        for page_index in range(max_pages):
            payload = client.list_flash(cursor=cursor)
            page_items = extract_jin10_items(payload)
            if not page_items:
                break
            records.extend(page_items)
            if limit and len(records) >= limit:
                return records[:limit]
            if page_index >= 1 and record_known is not None:
                known = sum(1 for record in page_items if record_known(record))
                if known > len(page_items) / 2:
                    break
            cursor = extract_jin10_cursor(payload)
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
    finally:
        client.close()
    return records


def parse_int(text: str) -> int | None:
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def fetch_github_repo_metadata(
    session: requests.Session, repo_name: str, timeout: int
) -> dict[str, Any]:
    api_url = f"https://api.github.com/repos/{repo_name}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = session.get(api_url, headers=headers, timeout=timeout)
    if response.status_code == 403:
        return {"api_error": "GitHub API rate limited or forbidden"}
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return {}
    owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
    license_info = (
        payload.get("license") if isinstance(payload.get("license"), dict) else {}
    )
    return {
        "owner": owner.get("login"),
        "owner_type": owner.get("type"),
        "full_name": payload.get("full_name"),
        "description": payload.get("description"),
        "homepage": payload.get("homepage"),
        "topics": payload.get("topics", []),
        "language": payload.get("language"),
        "stargazers_count": payload.get("stargazers_count"),
        "forks_count": payload.get("forks_count"),
        "watchers_count": payload.get("watchers_count"),
        "open_issues_count": payload.get("open_issues_count"),
        "subscribers_count": payload.get("subscribers_count"),
        "network_count": payload.get("network_count"),
        "license": license_info.get("spdx_id") or license_info.get("name"),
        "default_branch": payload.get("default_branch"),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "pushed_at": payload.get("pushed_at"),
        "size": payload.get("size"),
        "archived": payload.get("archived"),
        "disabled": payload.get("disabled"),
        "is_template": payload.get("is_template"),
    }


def parse_github_count(text: str) -> int | None:
    text = text.strip().lower().replace(",", "")
    match = re.search(r"([\d.]+)\s*([km]?)", text)
    if not match:
        return None
    value = float(match.group(1))
    suffix = match.group(2)
    if suffix == "k":
        value *= 1000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def heading_text(row: Any) -> str:
    heading = row.select_one("h2")
    return heading.get_text(" ", strip=True) if heading else ""


def fetch_github_repo_html_metadata(
    session: requests.Session, repo_url: str, repo_name: str, timeout: int
) -> dict[str, Any]:
    response = session.get(repo_url, timeout=timeout)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    metadata: dict[str, Any] = {}

    description_meta = soup.find("meta", attrs={"property": "og:description"})
    if description_meta and description_meta.get("content"):
        description = str(description_meta["content"])
        metadata["description"] = description.removesuffix(f" - {repo_name}").strip()

    issues = soup.select_one(f'a[href="/{repo_name}/issues"] span.Counter')
    pulls = soup.select_one(f'a[href="/{repo_name}/pulls"] span.Counter')
    if issues:
        metadata["open_issues_count"] = parse_github_count(issues.get_text(" ", strip=True))
    if pulls:
        metadata["open_pulls_count"] = parse_github_count(pulls.get_text(" ", strip=True))

    topics = [node.get_text(" ", strip=True) for node in soup.select("a.topic-tag")]
    if topics:
        metadata["topics"] = topics

    for row in soup.select("div.BorderGrid-row"):
        title = heading_text(row)
        row_text = row.get_text(" | ", strip=True)
        parts = [part.strip() for part in row_text.split("|") if part.strip()]
        if title == "About":
            external_links = []
            for link in row.select('a[href^="http"]'):
                href = str(link.get("href") or "")
                if href and "github.com" not in href:
                    external_links.append(href)
            if external_links:
                metadata["homepage"] = external_links[0]
            if "License" in parts:
                idx = parts.index("License")
                if idx + 1 < len(parts):
                    metadata["license"] = parts[idx + 1]
            for idx, part in enumerate(parts):
                lower = part.lower()
                if lower == "stars" and idx + 1 < len(parts):
                    metadata["stargazers_count"] = parse_github_count(parts[idx + 1])
                elif lower == "watchers" and idx + 1 < len(parts):
                    metadata["watchers_count"] = parse_github_count(parts[idx + 1])
                elif lower == "forks" and idx + 1 < len(parts):
                    metadata["forks_count"] = parse_github_count(parts[idx + 1])
        elif title.startswith("Releases"):
            count = parse_github_count(title.replace("Releases", "", 1))
            if count is not None:
                metadata["releases_count"] = count
            relative_time = row.select_one("relative-time")
            if relative_time:
                metadata["latest_release_date"] = (
                    relative_time.get("datetime")
                    or relative_time.get("title")
                    or relative_time.get_text(" ", strip=True)
                )
            if len(parts) >= 3:
                metadata["latest_release"] = parts[2]
        elif title == "Languages":
            languages: dict[str, str] = {}
            for idx, part in enumerate(parts):
                if idx + 1 < len(parts) and parts[idx + 1].endswith("%"):
                    languages[part] = parts[idx + 1]
            if languages:
                metadata["languages"] = languages

    return metadata


def collect_github_trending(
    session: requests.Session, timeout: int, limit: int | None
) -> list[NewsItem]:
    response = session.get(
        GITHUB_TRENDING_URL, params={"since": "daily"}, timeout=timeout
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    items: list[NewsItem] = []
    trending_date = now_shanghai().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat(timespec="seconds")
    for article in soup.select("article.Box-row"):
        repo_link = article.select_one("h2 a")
        if not repo_link:
            continue
        repo_name = " ".join(repo_link.get_text(" ", strip=True).split())
        repo_name = repo_name.replace(" / ", "/")
        repo_url = "https://github.com" + repo_link.get("href", "").strip()
        repo_meta: dict[str, Any] = {}
        html_meta: dict[str, Any] = {}
        try:
            repo_meta = fetch_github_repo_metadata(session, repo_name, timeout)
        except Exception as exc:
            repo_meta = {"api_error": str(exc)}
        try:
            html_meta = fetch_github_repo_html_metadata(
                session, repo_url, repo_name, timeout
            )
        except Exception as exc:
            html_meta = {"html_error": str(exc)}
        description_node = article.select_one("p")
        description = (
            description_node.get_text(" ", strip=True) if description_node else ""
        )
        language_node = article.select_one("[itemprop='programmingLanguage']")
        language = language_node.get_text(" ", strip=True) if language_node else ""
        stars_links = article.select("a.Link--muted")
        stars = parse_int(stars_links[0].get_text(" ", strip=True)) if stars_links else None
        forks = (
            parse_int(stars_links[1].get_text(" ", strip=True))
            if len(stars_links) > 1
            else None
        )
        today_stars_node = article.select_one("span.d-inline-block.float-sm-right")
        today_stars = (
            today_stars_node.get_text(" ", strip=True) if today_stars_node else ""
        )
        items.append(
            NewsItem(
                source_type="github_trending",
                source_name="GitHub Trending Daily",
                published_at=trending_date,
                title=repo_name,
                content=str(
                    repo_meta.get("description")
                    or html_meta.get("description")
                    or description
                ),
                url=repo_url,
                tags=[
                    str(tag)
                    for tag in [
                        repo_meta.get("language") or language,
                        "daily",
                        *(repo_meta.get("topics") or []),
                        *(html_meta.get("topics") or []),
                    ]
                    if tag
                ],
                metadata={
                    "language": repo_meta.get("language")
                    or next(iter((html_meta.get("languages") or {}).keys()), "")
                    or language,
                    "stars": repo_meta.get("stargazers_count")
                    or html_meta.get("stargazers_count")
                    or stars,
                    "forks": repo_meta.get("forks_count")
                    or html_meta.get("forks_count")
                    or forks,
                    "stars_today": today_stars,
                    "github_api": repo_meta,
                    "github_html": html_meta,
                },
                raw={
                    "trending": {
                        "repo": repo_name,
                        "description": description,
                        "url": repo_url,
                        "language": language,
                        "stars": stars,
                        "forks": forks,
                        "stars_today": today_stars,
                    },
                    "github_api": repo_meta,
                    "github_html": html_meta,
                },
            )
        )
        if limit and len(items) >= limit:
            break
    return items


def product_hunt_window(date_key: str) -> tuple[str, str]:
    target_start = datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    # Product Hunt's daily cycle is US-timezone oriented. A 48h window makes the
    # Shanghai-morning cron catch the latest PH daily ranking instead of returning
    # an empty set before the US day has fully rolled over.
    after = target_start.astimezone(timezone.utc) - timedelta(days=1)
    before = target_start.astimezone(timezone.utc) + timedelta(days=1)
    return after.isoformat().replace("+00:00", "Z"), before.isoformat().replace(
        "+00:00", "Z"
    )


def extract_product_hunt_edges(payload: dict[str, Any]) -> list[dict[str, Any]]:
    posts = payload.get("data", {}).get("posts", {})
    edges = posts.get("edges", [])
    if not isinstance(edges, list):
        return []
    return [edge for edge in edges if isinstance(edge, dict)]


def product_hunt_topics(node: dict[str, Any]) -> list[dict[str, str]]:
    topics = node.get("topics", {}).get("edges", [])
    results: list[dict[str, str]] = []
    if not isinstance(topics, list):
        return results
    for edge in topics:
        topic = edge.get("node") if isinstance(edge, dict) else None
        if isinstance(topic, dict):
            results.append(
                {
                    "name": str(topic.get("name") or ""),
                    "slug": str(topic.get("slug") or ""),
                }
            )
    return results


def collect_product_hunt(
    session: requests.Session,
    timeout: int,
    limit: int | None,
    date_key: str,
) -> list[NewsItem]:
    token = os.getenv("PRODUCTHUNT_TOKEN")
    if not token:
        raise RuntimeError("Missing PRODUCTHUNT_TOKEN environment variable")
    first = limit or 20
    posted_after, posted_before = product_hunt_window(date_key)
    query = """
    query($first: Int!, $after: DateTime!, $before: DateTime!) {
      posts(
        first: $first,
        order: RANKING,
        postedAfter: $after,
        postedBefore: $before,
        featured: true
      ) {
        edges {
          node {
            id
            name
            tagline
            description
            url
            website
            votesCount
            commentsCount
            dailyRank
            weeklyRank
            featuredAt
            createdAt
            thumbnail { url }
            topics { edges { node { name slug } } }
            makers { name username }
          }
        }
      }
    }
    """
    response = session.post(
        PRODUCT_HUNT_GRAPHQL_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "variables": {
                "first": first,
                "after": posted_after,
                "before": posted_before,
            },
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"Product Hunt GraphQL error: {payload['errors']}")

    snapshot_time = datetime.strptime(date_key, "%Y-%m-%d").replace(
        tzinfo=SHANGHAI
    )
    items: list[NewsItem] = []
    for edge in extract_product_hunt_edges(payload):
        node = edge.get("node")
        if not isinstance(node, dict):
            continue
        post_id = str(node.get("id") or "")
        name = str(node.get("name") or "").strip()
        tagline = str(node.get("tagline") or "").strip()
        description = str(node.get("description") or "").strip()
        topics = product_hunt_topics(node)
        makers = [
            {
                "name": str(maker.get("name") or ""),
                "username": str(maker.get("username") or ""),
            }
            for maker in node.get("makers", [])
            if isinstance(maker, dict)
        ]
        thumbnail = node.get("thumbnail") if isinstance(node.get("thumbnail"), dict) else {}
        metadata = {
            "stable_id": f"product_hunt:{post_id}" if post_id else "",
            "product_hunt_id": post_id,
            "votes_count": node.get("votesCount"),
            "comments_count": node.get("commentsCount"),
            "daily_rank": node.get("dailyRank"),
            "weekly_rank": node.get("weeklyRank"),
            "featured_at": node.get("featuredAt"),
            "created_at": node.get("createdAt"),
            "website": node.get("website"),
            "topics": topics,
            "makers": makers,
            "thumbnail": thumbnail.get("url"),
            "ranking_window": {"posted_after": posted_after, "posted_before": posted_before},
        }
        if not metadata["stable_id"]:
            # 原生 id 缺失的兜底：published_at 是当日 00:00 快照，与 GitHub
            # Trending 同理必须带 date_key，跨天快照才不会撞成同一行。
            metadata["stable_id"] = make_stable_id(
                "product_hunt",
                "Product Hunt Ranking",
                str(node.get("url") or ""),
                snapshot_time.isoformat(timespec="seconds"),
                name,
                date_key=date_key,
            )
        items.append(
            NewsItem(
                source_type="product_hunt",
                source_name="Product Hunt Ranking",
                published_at=snapshot_time.isoformat(timespec="seconds"),
                title=name,
                content=tagline or description,
                url=str(node.get("url") or node.get("website") or ""),
                author=", ".join(
                    maker["username"] or maker["name"] for maker in makers
                )
                or None,
                tags=["product", "ranking", *[topic["name"] for topic in topics if topic["name"]]],
                metadata=metadata,
                raw={k: v for k, v in node.items() if k != "token"},
            )
        )
    return items


def fetch_hn_json(session: requests.Session, url: str, timeout: int) -> Any:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError(f"Failed to fetch HN URL: {url}")


def collect_hacker_news(
    session: requests.Session,
    timeout: int,
    limit: int | None,
) -> list[NewsItem]:
    per_rank_limit = limit or 50
    ranked: dict[int, dict[str, Any]] = {}
    for rank_source in ("topstories", "beststories"):
        story_ids = fetch_hn_json(session, f"{HN_API_BASE}/{rank_source}.json", timeout)
        if not isinstance(story_ids, list):
            continue
        for rank, story_id in enumerate(story_ids[:per_rank_limit], start=1):
            if not isinstance(story_id, int):
                continue
            item = fetch_hn_json(session, f"{HN_API_BASE}/item/{story_id}.json", timeout)
            if not isinstance(item, dict) or item.get("type") != "story":
                continue
            existing = ranked.setdefault(story_id, item)
            sources = existing.setdefault("_rank_sources", [])
            if rank_source not in sources:
                sources.append(rank_source)
            ranks = existing.setdefault("_ranks", {})
            ranks[rank_source] = rank

    items: list[NewsItem] = []
    for story_id, story in ranked.items():
        hn_url = f"https://news.ycombinator.com/item?id={story_id}"
        story_url = str(story.get("url") or hn_url)
        title = str(story.get("title") or "").strip()
        rank_sources = story.get("_rank_sources", [])
        ranks = story.get("_ranks", {})
        published_at = iso_in_shanghai(parse_any_datetime(story.get("time")))
        metadata = {
            "stable_id": f"hacker_news:{story_id}",
            "hacker_news_id": story_id,
            "score": story.get("score"),
            "descendants": story.get("descendants"),
            "by": story.get("by"),
            "rank_sources": rank_sources,
            "ranks": ranks,
            "hn_url": hn_url,
        }
        raw = dict(story)
        raw.pop("_rank_sources", None)
        raw.pop("_ranks", None)
        raw["rank_sources"] = rank_sources
        raw["ranks"] = ranks
        raw["hn_url"] = hn_url
        items.append(
            NewsItem(
                source_type="hacker_news",
                source_name="Hacker News Hot",
                published_at=published_at,
                title=title,
                content=title,
                url=story_url,
                author=str(story.get("by") or "") or None,
                tags=["hacker_news", *rank_sources],
                metadata=metadata,
                raw=raw,
            )
        )
    return items


def load_rss_sources(config_path: Path) -> list[dict[str, Any]]:
    if not config_path.exists():
        return []
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    sources = payload.get("rss", [])
    return [source for source in sources if source.get("enabled", True)]


def load_collect_min_rows(config_path: Path) -> dict[str, int]:
    """Read the per-source --only-missing thresholds (collect_min_rows map).

    Keys are source_type values (jin10/rss/github_trending/product_hunt/
    hacker_news). Missing config or malformed entries fall back to the
    caller's default of 1.
    """
    if not config_path.exists():
        return {}
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw = payload.get("collect_min_rows") or {}
    thresholds: dict[str, int] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                thresholds[str(key)] = max(1, int(value))
            except (TypeError, ValueError):
                continue
    return thresholds


def collect_rss(
    config_path: Path, timeout: int, limit: int | None, date_key: str
) -> list[NewsItem]:
    session = get_session()
    items: list[NewsItem] = []
    for source in load_rss_sources(config_path):
        feed_url = source.get("url", "")
        try:
            response = session.get(feed_url, timeout=timeout)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
        except Exception as exc:
            items.append(
                NewsItem(
                    source_type="error",
                    source_name=f"rss:{source.get('name') or feed_url}",
                    published_at=None,
                    title="RSS collection failed",
                    content=str(exc),
                    url=str(feed_url),
                    metadata={"error": True, "category": source.get("category")},
                )
            )
            continue
        per_source_count = 0
        for entry in feed.entries:
            published_dt = parse_feed_time(entry)
            if not published_dt:
                continue
            published = iso_in_shanghai(published_dt)
            summary = str(
                entry.get("summary")
                or entry.get("description")
                or entry.get("title")
                or ""
            )
            tags = [source.get("category", "rss")]
            tags.extend(
                str(tag.get("term"))
                for tag in entry.get("tags", [])
                if isinstance(tag, dict) and tag.get("term")
            )
            items.append(
                NewsItem(
                    source_type="rss",
                    source_name=str(source.get("name") or source.get("url")),
                    published_at=published,
                    title=str(entry.get("title") or "").strip(),
                    content=BeautifulSoup(summary, "html.parser").get_text(" ", strip=True),
                    url=str(entry.get("link") or ""),
                    author=str(entry.get("author") or "") or None,
                    tags=tags,
                    metadata={
                        "feed_url": source.get("url"),
                        "category": source.get("category"),
                    },
                    raw=dict(entry),
                )
            )
            per_source_count += 1
            if limit and per_source_count >= limit:
                break
        # 空 feed 可见化：抓取成功但当日 0 条时写诊断行（error_type=feed_empty，
        # 区别于抓取异常），否则薄覆盖日被误判为"源正常只是没新闻"。
        today_count = sum(
            1
            for entry_item in items
            if entry_item.source_type == "rss"
            and entry_item.source_name == str(source.get("name") or source.get("url"))
            and item_date_key(entry_item, date_key) == date_key
        )
        if today_count == 0:
            items.append(
                NewsItem(
                    source_type="error",
                    source_name=f"rss:{source.get('name') or feed_url}",
                    published_at=None,
                    title="RSS feed empty for date",
                    content=f"feed fetched OK but 0 entries for {date_key}",
                    url=str(feed_url),
                    metadata={
                        "error": True,
                        "error_type": "feed_empty",
                        "category": source.get("category"),
                    },
                )
            )
    return items


def collect_sources(
    args: argparse.Namespace,
    date_key: str,
    restrict_to: set[str] | None = None,
) -> tuple[dict[str, list[NewsItem]], dict[str, str]]:
    session = get_session()
    selected = requested_collectors(args.source)
    if restrict_to is not None:
        selected = selected & restrict_to
    results: dict[str, list[NewsItem]] = {}
    errors: dict[str, str] = {}

    collectors = {
        "jin10": lambda: collect_jin10(
            session,
            args.timeout,
            args.limit,
            db_path=Path(args.db),
            use_watermark=not args.no_watermark,
        ),
        "github": lambda: collect_github_trending(session, args.timeout, args.limit),
        "rss": lambda: collect_rss(Path(args.config), args.timeout, args.limit, date_key),
        "product_hunt": lambda: collect_product_hunt(
            session, args.timeout, args.limit, date_key
        ),
        "hacker_news": lambda: collect_hacker_news(
            session, args.timeout, args.limit
        ),
    }
    for name in selected:
        try:
            source_items = collectors[name]()
            results[name] = [
                item for item in source_items if item_date_key(item, date_key) == date_key
            ]
        except Exception as exc:  # Keep other sources usable when one endpoint fails.
            errors[name] = str(exc)
            results[name] = []
    return results, errors


def print_summary(
    results: dict[str, list[NewsItem]],
    written: dict[str, int],
    errors: dict[str, str] | None = None,
    skipped: list[str] | None = None,
) -> None:
    summary: dict[str, Any] = {
        name: {"fetched_for_date": len(items), "written": written.get(name, 0)}
        for name, items in results.items()
    }
    if errors:
        summary["_errors"] = {
            name: {"message": msg, "fetched_for_date": 0, "written": 0}
            for name, msg in errors.items()
        }
        # 显著报警：失败的源不能只藏在 stdout JSON 里，否则会发出"看起来正常其实
        # 缺数"的报告（违背 DB-first 不静默原则）。打到 stderr 让 cron/调用方看见。
        print(
            f"⚠️  collect_news: {len(errors)} 个信源采集失败 —— "
            + "; ".join(f"{name}: {msg}" for name, msg in errors.items()),
            file=sys.stderr,
        )
    if skipped:
        summary["_skipped_present"] = skipped
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> int:
    args = parse_args()
    date_key = resolve_date_key(args.date)

    restrict_to: set[str] | None = None
    skipped: list[str] = []
    if args.only_missing and not args.replace_date:
        with init_db(Path(args.db)) as con:
            counts = db_count_items_by_source(con, date_key)
        requested = requested_collectors(args.source)
        # 每源阈值：--min-rows 显式传入时全源统一覆盖；否则读 config 的
        # collect_min_rows；配置缺失回退默认 1（任意行即视为已采）。
        config_thresholds = load_collect_min_rows(Path(args.config))
        present = set()
        for name in requested:
            source_type = COLLECTOR_SOURCE_TYPE[name]
            threshold = (
                args.min_rows
                if args.min_rows is not None
                else config_thresholds.get(source_type, 1)
            )
            if counts.get(source_type, 0) >= max(1, threshold):
                present.add(name)
        restrict_to = requested - present
        skipped = sorted(present)

    results, errors = collect_sources(args, date_key, restrict_to=restrict_to)
    written: dict[str, int] = {}

    if not args.dry_run:
        with init_db(Path(args.db)) as con:
            if args.replace_date:
                deleted = delete_date_rows(
                    con, date_key, selected_source_types(args.source)
                )
                print(
                    json.dumps(
                        {"replace_date_deleted": deleted},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            for name, items in results.items():
                written[name] = write_items(con, items, date_key)

    print_summary(results, written, errors, skipped=skipped)
    # 非零退出码让 cron/调用方能检测"有信源失败"，而不是看 exit 0 以为一切正常。
    return 1 if errors else 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    raise SystemExit(main())
