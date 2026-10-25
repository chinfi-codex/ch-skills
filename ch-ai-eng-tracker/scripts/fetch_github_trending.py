#!/usr/bin/env python3
"""GitHub 本周最火项目抓取：trending 周榜 + 窗口期新建高星仓库 + 元数据 enrich。

三条路都只返回原始数据；ai_eng_hint 用关键词预筛（只标 true/false，不删条目），
工程相关性的最终判断由模型做。

返回 JSON：
{"script","window","generated_at","trending":[...],"new_repos":[...],"degraded":[...]}
enrich 过的条目额外带 topics/license/created_at/archived 等字段。

用法：
    python scripts/fetch_github_trending.py --days 7
    python scripts/fetch_github_trending.py --days 7 --enrich 0   # 跳过 enrich

凭据：GITHUB_TOKEN 可选（无 token 60 req/h；脚本按 ~1.1s/请求节流，
403/429 自动停止 enrich 并记 degraded）。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    HttpError,
    RateLimiter,
    add_common_args,
    emit_error,
    http_get_json,
    http_get_text,
    iso_shanghai,
    keyword_match,
    load_config,
    make_session,
    now_shanghai,
    write_or_emit,
)

TRENDING_URL = "https://github.com/trending"


def gh_headers() -> dict:
    import os

    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_github_count(text: str) -> int | None:
    text = (text or "").strip().lower().replace(",", "")
    import re

    m = re.search(r"([\d.]+)\s*([km]?)", text)
    if not m:
        return None
    value = float(m.group(1)) * {"k": 1000, "m": 1_000_000}.get(m.group(2), 1)
    return int(value)


def parse_trending(html: str, since: str, limit: int, hint_keywords: list[str]) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    for article in soup.select("article.Box-row"):
        link = article.select_one("h2 a")
        if not link:
            continue
        repo_name = " ".join(link.get_text(" ", strip=True).split()).replace(" / ", "/")
        desc_node = article.select_one("p")
        description = desc_node.get_text(" ", strip=True) if desc_node else ""
        lang_node = article.select_one("[itemprop='programmingLanguage']")
        language = lang_node.get_text(" ", strip=True) if lang_node else ""
        counters = article.select("a.Link--muted")
        period_node = article.select_one("span.d-inline-block.float-sm-right")
        items.append(
            {
                "repo": repo_name,
                "url": "https://github.com" + str(link.get("href") or "").strip(),
                "description": description,
                "language": language,
                "stars": parse_github_count(counters[0].get_text(" ", strip=True)) if counters else None,
                "forks": parse_github_count(counters[1].get_text(" ", strip=True)) if len(counters) > 1 else None,
                "stars_in_period": period_node.get_text(" ", strip=True) if period_node else None,
                "trending_since": since,
                "ai_eng_hint": bool(keyword_match(f"{repo_name} {description} {language}", hint_keywords)),
            }
        )
        if len(items) >= limit:
            break
    return items


def search_new_repos(session, queries: list[str], since_date: str, per_query: int, hint_keywords: list[str], limiter: RateLimiter) -> tuple[list[dict], list[dict]]:
    seen: set[str] = set()
    items: list[dict] = []
    degraded: list[dict] = []
    for q in queries:
        url = (
            f"https://api.github.com/search/repositories?q={quote(q)}+created:>={since_date}"
            f"&sort=stars&order=desc&per_page={per_query}"
        )
        try:
            limiter.wait()
            data = http_get_json(session, url, timeout=30, headers=gh_headers())
        except Exception as exc:  # noqa: BLE001
            degraded.append({"source": f"search:{q}", "error": str(exc)})
            continue
        for repo in data.get("items", []):
            full = repo.get("full_name")
            if not full or full in seen:
                continue
            seen.add(full)
            items.append(
                {
                    "repo": full,
                    "url": repo.get("html_url"),
                    "description": repo.get("description") or "",
                    "language": repo.get("language") or "",
                    "stars": repo.get("stargazers_count"),
                    "created_at": repo.get("created_at"),
                    "query": q,
                    "ai_eng_hint": bool(keyword_match(f"{full} {repo.get('description') or ''}", hint_keywords)),
                }
            )
    return items, degraded


def enrich_repos(session, repos: list[str], limiter: RateLimiter) -> tuple[dict[str, dict], list[str]]:
    meta: dict[str, dict] = {}
    errors: list[str] = []
    for repo in repos:
        try:
            limiter.wait()
            data = http_get_json(
                session, f"https://api.github.com/repos/{repo}", timeout=30, headers=gh_headers()
            )
        except HttpError as exc:
            if exc.status in (403, 429):
                errors.append(f"rate_limited at {repo}; enrich 提前停止")
                break
            errors.append(f"{repo}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{repo}: {exc}")
            continue
        license_info = data.get("license") or {}
        meta[repo] = {
            "topics": data.get("topics", []),
            "license": license_info.get("spdx_id") or license_info.get("name"),
            "created_at": data.get("created_at"),
            "pushed_at": data.get("pushed_at"),
            "archived": data.get("archived"),
            "open_issues_count": data.get("open_issues_count"),
        }
    return meta, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub 本周最火项目（trending 周榜 + 新建高星仓库）")
    add_common_args(parser, default_limit=25)
    parser.add_argument("--enrich", type=int, default=None, help="对前 N 个仓库补元数据（覆盖配置 enrich_top）")
    args = parser.parse_args()

    try:
        cfg = load_config().get("github_trending") or {}
    except Exception as exc:  # noqa: BLE001
        emit_error("MISSING_CONFIG", str(exc), exit_code=2)

    since = cfg.get("since") or "weekly"
    hint_keywords = [str(k) for k in (cfg.get("hint_keywords") or [])]
    session = make_session()
    limiter = RateLimiter(1.1)
    degraded: list[dict] = []

    trending: list[dict] = []
    try:
        html = http_get_text(session, f"{TRENDING_URL}?since={since}", timeout=args.timeout)
        trending = parse_trending(html, since, args.limit, hint_keywords)
    except Exception as exc:  # noqa: BLE001
        degraded.append({"source": "trending", "error": str(exc)})

    new_repos: list[dict] = []
    if cfg.get("new_repo_search", True):
        queries = [str(q) for q in (cfg.get("new_repo_queries") or [])]
        since_date = str(date.today() - timedelta(days=args.days))
        new_repos, search_degraded = search_new_repos(
            session, queries, since_date, int(cfg.get("search_per_query") or 20), hint_keywords, limiter
        )
        degraded.extend(search_degraded)
    new_repos.sort(key=lambda x: x.get("stars") or 0, reverse=True)
    new_repos = new_repos[: args.limit]

    enrich_top = args.enrich if args.enrich is not None else int(cfg.get("enrich_top") or 0)
    enrich_errors: list[str] = []
    if enrich_top > 0:
        candidates: list[str] = []
        for item in trending + new_repos:
            repo = item.get("repo")
            if repo and repo not in candidates:
                candidates.append(repo)
        meta, enrich_errors = enrich_repos(session, candidates[:enrich_top], limiter)
        for group in (trending, new_repos):
            for item in group:
                extra = meta.get(item.get("repo"))
                if extra:
                    item.update(extra)
        if enrich_errors:
            degraded.append({"source": "enrich", "error": "; ".join(enrich_errors)})

    if not trending and not new_repos:
        emit_error("GITHUB_FETCH_FAILED", degraded or "trending 与新仓库搜索均无结果")

    payload = {
        "script": "fetch_github_trending",
        "generated_at": iso_shanghai(now_shanghai()),
        "window": {"days": args.days, "trending_since": since},
        "trending": trending,
        "new_repos": new_repos,
        "degraded": degraded,
    }
    write_or_emit(args, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
