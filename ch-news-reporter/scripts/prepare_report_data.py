#!/usr/bin/env python3
"""Prepare profile-specific evidence packets from the unified news database."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

from db_adapter import (
    count_items_by_source as db_count_items_by_source,
    ensure_connectable as db_ensure_connectable,
    get_connection,
    get_enrichments_by_items as db_get_enrichments_by_items,
    get_latest_framework_state as db_get_latest_framework_state,
    get_latest_report_state as db_get_latest_report_state,
    query_items as db_query_items,
    table_exists,
)
from framework_loader import load_framework
from profile_config import (
    DEFAULT_PROFILE_CONFIG,
    load_profile,
    profile_sources,
    reference_dir,
    rss_categories,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DB = Path("data/news_research.sqlite")
DEFAULT_CONFIG = DEFAULT_PROFILE_CONFIG
MACRO_SOURCE_PRIORITY = {"jin10": 0, "rss": 1}
DEFAULT_MACRO_KEYWORDS = [
    "宏观",
    "经济数据",
    "央行",
    "美联储",
    "人民银行",
    "货币政策",
    "财政",
    "利率",
    "收益率",
    "国债",
    "美债",
    "通胀",
    "CPI",
    "PPI",
    "PCE",
    "PMI",
    "社融",
    "社会融资",
    "非农",
    "就业",
    "初请",
    "失业率",
    "零售销售",
    "GDP",
    "汇率",
    "人民币",
    "美元",
    "原油",
    "布伦特",
    "黄金",
    "天然气",
    "大宗商品",
    "库存",
    "OPEC",
    "EIA",
    "API",
    "风险资产",
]
DEFAULT_DATA_EVENT_KEYWORDS = [
    "公布",
    "发布",
    "出炉",
    "录得",
    "实际",
    "预期",
    "前值",
    "初值",
    "终值",
    "修正",
    "announce",
    "released",
    "actual",
    "forecast",
    "previous",
]
INDICATOR_KEYWORDS = {
    "CPI": ["CPI", "消费者物价", "居民消费价格"],
    "PPI": ["PPI", "生产者物价", "工业生产者出厂价格"],
    "PCE": ["PCE", "个人消费支出"],
    "PMI": ["PMI", "采购经理"],
    "SOCI": ["社融", "社会融资", "社会融资规模"],
    "NFP": ["非农", "nonfarm", "non-farm"],
    "JOBLESS": ["初请", "续请", "失业金", "jobless"],
    "UNEMPLOYMENT": ["失业率", "unemployment"],
    "RETAIL_SALES": ["零售销售", "retail sales"],
    "GDP": ["GDP", "国内生产总值"],
    "ISM": ["ISM"],
    "JOLTS": ["JOLTS", "职位空缺"],
    "EIA": ["EIA", "美国能源信息署"],
    "API": ["API", "美国石油协会"],
    "COMMODITY_INVENTORY": ["原油库存", "库存"],
}
CHINA_MONTHLY_INDICATORS = {"CPI", "PPI", "SOCI", "PMI"}
CHINA_REGION_KEYWORDS = ["中国", "我国", "国家统计局", "央行", "人民银行", "财新"]
US_REGION_KEYWORDS = ["美国", "美联储", "美国劳工部", "ISM", "ADP", "非农", "初请"]
WEAK_MACRO_KEYWORDS = {"美元", "黄金", "就业"}
FALSE_POSITIVE_PHRASES = ["黄金创业", "黄金窗口", "黄金时期"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a report-profile evidence packet from SQLite."
    )
    parser.add_argument("--profile", required=True, help="Profile name, e.g. ai_daily.")
    parser.add_argument("--date", default="today", help="today, all, or YYYY-MM-DD.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="Report profiles YAML path."
    )
    parser.add_argument("--limit", type=int, default=200, help="Maximum base items.")
    parser.add_argument(
        "--include-enrichments",
        action="store_true",
        help="Attach rows from the optional enrichments table.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="json",
        help="Evidence packet output format.",
    )
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def resolve_date_key(value: str) -> str | None:
    if value == "all":
        return None
    if value == "today":
        return datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit("--date must be 'today', 'all', or YYYY-MM-DD") from exc


def json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def query_items(
    con: Any,
    profile: dict[str, Any],
    date_key: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch a bounded pool per configured source; selection happens later.

    The old path sorted all sources together by timestamp and then cut the global
    ``limit``.  Snapshot-style sources such as Product Hunt and GitHub Trending
    use midnight timestamps, so a busy Jin10 day could evict them before the
    Agent ever saw them.  Keep up to ``limit`` rows *per source* here, apply the
    profile filters, then let ``select_report_items`` build a source-aware packet.
    """
    sources = profile_sources(profile)
    order_by = "COALESCE(published_at, fetched_at) DESC"
    if sources:
        raw_rows: list[dict[str, Any]] = []
        for source_type in sources:
            raw_rows.extend(
                db_query_items(
                    con,
                    date_key=date_key,
                    source_type=source_type,
                    limit=limit,
                    order_by=order_by,
                )
            )
    else:
        raw_rows = db_query_items(
            con,
            date_key=date_key,
            limit=limit,
            order_by=order_by,
        )
    return normalize_item_rows(raw_rows)


def query_coverage_items(
    con: Any,
    profile: dict[str, Any],
    date_key: str | None,
) -> list[dict[str, Any]]:
    sources = profile_sources(profile)
    order_by = "COALESCE(published_at, fetched_at) DESC"
    if sources:
        raw_rows: list[dict[str, Any]] = []
        for source_type in sources:
            raw_rows.extend(
                db_query_items(
                    con,
                    date_key=date_key,
                    source_type=source_type,
                    limit=None,
                    order_by=order_by,
                )
            )
    else:
        raw_rows = db_query_items(
            con,
            date_key=date_key,
            limit=None,
            order_by=order_by,
        )
    return normalize_item_rows(raw_rows)


def normalize_item_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in raw_rows:
        item["tags"] = json_loads(item.pop("tags_json", None), [])
        item["metadata"] = json_loads(item.pop("metadata_json", None), {})
        item["raw"] = json_loads(item.pop("raw_json", None), {})
        rows.append(item)
    return rows


def normalize_enrichment_row(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("result_json"):
        payload = json_loads(str(row.get("result_json")), {})
        if isinstance(payload, dict):
            enrichment = dict(payload)
            enrichment.setdefault("id", row.get("id"))
            enrichment.setdefault("item_id", row.get("item_id"))
            enrichment.setdefault("target_type", row.get("enrichment_type"))
            enrichment.setdefault("target_url", row.get("source"))
            enrichment.setdefault("fetched_at", row.get("created_at"))
            enrichment.setdefault("status", "ok")
            enrichment.setdefault("metadata", {})
            enrichment.setdefault("raw", {})
            return enrichment
    enrichment = dict(row)
    enrichment["metadata"] = json_loads(enrichment.pop("metadata_json", None), {})
    enrichment["raw"] = json_loads(enrichment.pop("raw_json", None), {})
    return enrichment

def query_enrichments(
    con: Any, item_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    if not item_ids or not table_exists(con, "enrichments"):
        return {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    rows = db_get_enrichments_by_items(con, item_ids)
    for row in rows:
        enrichment = normalize_enrichment_row(row)
        grouped.setdefault(str(enrichment["item_id"]), []).append(enrichment)
    for values in grouped.values():
        values.sort(
            key=lambda enrichment: str(
                enrichment.get("fetched_at") or enrichment.get("created_at") or ""
            ),
            reverse=True,
        )
        values.sort(key=lambda enrichment: str(enrichment.get("target_type") or ""))
    return grouped


def normalize_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    normalized = parsed._replace(fragment="")
    if normalized.path == "/":
        normalized = normalized._replace(path="")
    return urlunparse(normalized)


def github_repo_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return ""
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if owner in {"features", "topics", "marketplace", "explore"}:
        return ""
    return f"https://github.com/{owner}/{repo}"


def source_rank_key(item: dict[str, Any]) -> tuple[Any, ...]:
    metadata = item.get("metadata") or {}
    source_type = item.get("source_type")
    if source_type == "product_hunt":
        rank = metadata.get("daily_rank") or metadata.get("weekly_rank") or 999999
        votes = metadata.get("votes_count") or 0
        comments = metadata.get("comments_count") or 0
        return (rank, -votes, -comments)
    if source_type == "github_trending":
        stars = metadata.get("stars") or (metadata.get("github_api") or {}).get(
            "stargazers_count"
        ) or 0
        forks = metadata.get("forks") or (metadata.get("github_api") or {}).get(
            "forks_count"
        ) or 0
        return (-stars, -forks)
    if source_type == "hacker_news":
        ranks = metadata.get("ranks") or {}
        best_rank = min([rank for rank in ranks.values() if rank] or [999999])
        score = metadata.get("score") or 0
        comments = metadata.get("descendants") or 0
        return (best_rank, -score, -comments)
    return (item.get("published_at") or item.get("fetched_at") or "",)


def select_source_items(
    items: list[dict[str, Any]], source_type: str, max_items: int
) -> list[dict[str, Any]]:
    source_items = [item for item in items if item.get("source_type") == source_type]
    if source_type in {"product_hunt", "github_trending", "hacker_news"}:
        return sorted(source_items, key=source_rank_key)[:max_items]
    return sorted(
        source_items,
        key=lambda item: str(item.get("published_at") or item.get("fetched_at") or ""),
        reverse=True,
    )[:max_items]


def source_selection_config(profile: dict[str, Any]) -> dict[str, Any]:
    configured = profile.get("source_selection") or {}
    if not isinstance(configured, dict):
        raise SystemExit(
            f"profile {profile.get('_name') or '?'} source_selection must be a map"
        )
    preferred = configured.get("preferred_limits") or {}
    if not isinstance(preferred, dict):
        raise SystemExit(
            f"profile {profile.get('_name') or '?'} "
            "source_selection.preferred_limits must be a map"
        )
    normalized: dict[str, int] = {}
    for source, value in preferred.items():
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise SystemExit(
                f"profile {profile.get('_name') or '?'} source_selection "
                f"limit for {source} must be an integer"
            ) from None
        if parsed < 0:
            raise SystemExit(
                f"profile {profile.get('_name') or '?'} source_selection "
                f"limit for {source} must be >= 0"
            )
        normalized[str(source)] = parsed
    try:
        hard_max_share = float(configured.get("hard_max_share", 1.0))
    except (TypeError, ValueError):
        raise SystemExit(
            f"profile {profile.get('_name') or '?'} "
            "source_selection.hard_max_share must be numeric"
        ) from None
    if not 0 < hard_max_share <= 1:
        raise SystemExit(
            f"profile {profile.get('_name') or '?'} "
            "source_selection.hard_max_share must be in (0, 1]"
        )
    return {
        "preferred_limits": normalized,
        "hard_max_share": hard_max_share,
    }


def select_report_items(
    items: list[dict[str, Any]], profile: dict[str, Any], limit: int
) -> list[dict[str, Any]]:
    """Build the final packet with preferred per-source budgets and refill.

    Preferred limits reserve room for discovery sources; they are deliberately
    soft.  Unused room is refilled by global recency while the configured hard
    share prevents one high-volume source from swallowing the whole packet.
    """
    if limit <= 0:
        return []
    selection = source_selection_config(profile)
    preferred = selection["preferred_limits"]
    if not preferred:
        return sorted(
            items,
            key=lambda item: str(
                item.get("published_at") or item.get("fetched_at") or ""
            ),
            reverse=True,
        )[:limit]

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    by_source: dict[str, int] = {}

    def add(item: dict[str, Any]) -> None:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in selected_ids or len(selected) >= limit:
            return
        selected.append(item)
        selected_ids.add(item_id)
        source = str(item.get("source_type") or "")
        by_source[source] = by_source.get(source, 0) + 1

    hard_cap = max(1, int(limit * selection["hard_max_share"]))
    source_order = profile_sources(profile)
    for source in source_order:
        budget = min(
            preferred.get(source, 0), hard_cap, limit - len(selected)
        )
        for item in select_source_items(items, source, budget):
            add(item)

    remaining = sorted(
        (item for item in items if str(item.get("id") or "") not in selected_ids),
        key=lambda item: str(
            item.get("published_at") or item.get("fetched_at") or ""
        ),
        reverse=True,
    )
    for item in remaining:
        if len(selected) >= limit:
            break
        source = str(item.get("source_type") or "")
        if by_source.get(source, 0) >= hard_cap:
            continue
        add(item)
    return selected


def packet_selection_summary(
    items: list[dict[str, Any]], profile: dict[str, Any], limit: int
) -> dict[str, Any]:
    selection = source_selection_config(profile)
    counts: dict[str, int] = {}
    for item in items:
        source = str(item.get("source_type") or "")
        counts[source] = counts.get(source, 0) + 1
    return {
        "limit": limit,
        "selected": len(items),
        "by_source": counts,
        "preferred_limits": selection["preferred_limits"],
        "hard_max_share": selection["hard_max_share"],
    }


def item_search_text(item: dict[str, Any]) -> str:
    parts: list[str] = [
        str(item.get("title") or ""),
        str(item.get("content") or ""),
        str(item.get("source_name") or ""),
    ]
    tags = item.get("tags") or []
    if isinstance(tags, list):
        parts.extend(str(tag) for tag in tags)
    metadata = item.get("metadata") or {}
    if isinstance(metadata, dict):
        parts.extend(str(value) for value in metadata.values() if isinstance(value, str))
    return " ".join(parts)


def contains_any(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords if keyword)


def profile_item_keywords(profile: dict[str, Any]) -> list[str]:
    configured = profile.get("item_keywords") or []
    if not isinstance(configured, list):
        raise SystemExit(f"profile {profile.get('_name') or '?'} item_keywords must be a list")
    return [str(keyword) for keyword in configured if str(keyword).strip()]


def profile_keyword_filter_sources(profile: dict[str, Any]) -> set[str]:
    configured = profile.get("keyword_filter_sources") or []
    if not isinstance(configured, list):
        raise SystemExit(
            f"profile {profile.get('_name') or '?'} keyword_filter_sources must be a list"
        )
    return {str(source) for source in configured if str(source).strip()}


def macro_keyword_hits(text: str, keywords: list[str]) -> list[str]:
    lowered = text.lower()
    return [keyword for keyword in keywords if keyword and keyword.lower() in lowered]


def profile_macro_keywords(profile: dict[str, Any]) -> list[str]:
    configured = profile.get("macro_keywords")
    if isinstance(configured, list) and configured:
        return [str(keyword) for keyword in configured if keyword]
    return DEFAULT_MACRO_KEYWORDS


def profile_data_event_keywords(profile: dict[str, Any]) -> list[str]:
    configured = profile.get("data_event_keywords")
    if isinstance(configured, list) and configured:
        return [str(keyword) for keyword in configured if keyword]
    return DEFAULT_DATA_EVENT_KEYWORDS


def macro_rank_key(item: dict[str, Any]) -> tuple[Any, ...]:
    source = str(item.get("source_type") or "")
    time_label = item.get("published_at") or item.get("fetched_at") or ""
    try:
        dt = datetime.fromisoformat(str(time_label).replace("Z", "+00:00"))
        time_rank = -dt.timestamp()
    except ValueError:
        time_rank = 0
    return (MACRO_SOURCE_PRIORITY.get(source, 99), time_rank)


def apply_profile_filters(
    items: list[dict[str, Any]], profile: dict[str, Any]
) -> list[dict[str, Any]]:
    categories = rss_categories(profile)
    if categories:
        items = [
            item
            for item in items
            if item.get("source_type") != "rss"
            or str((item.get("metadata") or {}).get("category") or "") in categories
        ]
    item_keywords = profile_item_keywords(profile)
    keyword_sources = profile_keyword_filter_sources(profile)
    if item_keywords and keyword_sources:
        items = [
            item
            for item in items
            if str(item.get("source_type") or "") not in keyword_sources
            or contains_any(item_search_text(item), item_keywords)
        ]
    if not profile.get("macro_data"):
        return items
    keywords = profile_macro_keywords(profile)
    filtered = []
    for item in items:
        text = item_search_text(item)
        if any(phrase in text for phrase in FALSE_POSITIVE_PHRASES):
            continue
        hits = macro_keyword_hits(text, keywords)
        if not hits:
            continue
        strong_hits = [hit for hit in hits if hit not in WEAK_MACRO_KEYWORDS]
        if strong_hits or len(hits) >= 2:
            filtered.append(item)
    return sorted(filtered, key=macro_rank_key)


def detected_indicators(text: str) -> list[str]:
    return [
        indicator
        for indicator, keywords in INDICATOR_KEYWORDS.items()
        if contains_any(text, keywords)
    ]


def detect_region(text: str, indicators: list[str]) -> str:
    if contains_any(text, CHINA_REGION_KEYWORDS) or "SOCI" in indicators:
        return "china"
    if contains_any(text, US_REGION_KEYWORDS):
        return "us"
    return "global_or_unknown"


def event_excerpt(text: str, max_len: int = 260) -> str:
    clean = " ".join(text.split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "..."


def detect_macro_data_events(
    items: list[dict[str, Any]], profile: dict[str, Any]
) -> list[dict[str, Any]]:
    release_keywords = profile_data_event_keywords(profile)
    events: list[dict[str, Any]] = []
    for item in items:
        text = item_search_text(item)
        indicators = detected_indicators(text)
        if not indicators:
            continue
        if not contains_any(text, release_keywords):
            continue
        events.append(
            {
                "item_id": item.get("id"),
                "source_type": item.get("source_type"),
                "source_name": item.get("source_name"),
                "published_at": item.get("published_at") or item.get("fetched_at"),
                "title": item.get("title"),
                "detected_indicators": indicators,
                "region": detect_region(text, indicators),
                "excerpt": event_excerpt(text),
            }
        )
    return events


def china_monthly_triggers(events: list[dict[str, Any]]) -> list[str]:
    triggers: set[str] = set()
    for event in events:
        if event.get("region") != "china":
            continue
        indicators = set(event.get("detected_indicators") or [])
        triggers.update(indicators & CHINA_MONTHLY_INDICATORS)
    return sorted(triggers)


def load_macro_monitor_module() -> Any:
    try:
        import macro_monitor

        return macro_monitor
    except ModuleNotFoundError:
        module_path = Path(__file__).with_name("macro_monitor.py")
        spec = importlib.util.spec_from_file_location("macro_monitor", module_path)
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def fetch_macro_market_signals() -> dict[str, Any]:
    try:
        macro_monitor = load_macro_monitor_module()
        return macro_monitor.fetch_dataset("market")
    except Exception as exc:
        return {
            "sources": {"macro_monitor": "ERROR"},
            "data": {},
            "error": str(exc),
        }


def build_conditional_data_fetches(events: list[dict[str, Any]]) -> dict[str, Any]:
    indicators = china_monthly_triggers(events)
    if not indicators:
        return {
            "china_monthly": {
                "triggered": False,
                "indicators": [],
                "reason": "No China CPI/PPI/SOCI/PMI release event detected in today's macro news.",
                "sources": {},
                "data": {},
            }
        }
    try:
        macro_monitor = load_macro_monitor_module()
        payload = macro_monitor.fetch_dataset(
            "china-monthly", china_indicators=indicators
        )
    except Exception as exc:
        payload = {"sources": {"tushare": "ERROR"}, "data": {}, "error": str(exc)}
    return {
        "china_monthly": {
            "triggered": True,
            "indicators": indicators,
            "reason": "China monthly macro release event detected in Jin10/RSS evidence.",
            **payload,
        }
    }


def build_enrichment_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_target(item: dict[str, Any], target_type: str, url: str, reason: str) -> None:
        target_url = normalize_url(github_repo_url(url) or url)
        if not target_url:
            return
        key = (str(item["id"]), target_type, target_url)
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            {
                "item_id": item["id"],
                "source_type": item.get("source_type"),
                "title": item.get("title"),
                "target_type": target_type,
                "target_url": target_url,
                "reason": reason,
            }
        )

    for item in items:
        source_type = item.get("source_type")
        if source_type == "github_trending":
            add_target(item, "github_repo", str(item.get("url") or ""), "GitHub repo")
        elif source_type == "product_hunt":
            metadata = item.get("metadata") or {}
            url = str(metadata.get("website") or item.get("url") or "")
            add_target(item, "product_website", url, "Product Hunt website")
        elif source_type in {"hacker_news", "rss"}:
            url = str(item.get("url") or "")
            if "news.ycombinator.com/item" in url:
                continue
            repo_url = github_repo_url(url)
            if repo_url:
                add_target(item, "github_repo", repo_url, f"{source_type} GitHub link")
            else:
                add_target(item, "article_url", url, f"{source_type} external URL")

    return candidates


def build_coverage(
    con: Any, profile: dict[str, Any], date_key: str | None
) -> dict[str, Any]:
    """Per-source row coverage for the date (DB-first read policy).

    Lets the Agent see which expected sources are present/missing so thin days
    can be flagged in the report; the deterministic count stays in the script.
    """
    expected = profile_sources(profile)
    if date_key is None:
        return {
            "date_key": "all",
            "expected_sources": expected,
            "by_source": {},
            "missing": [],
            "all_present": True,
        }
    counts = db_count_items_by_source(con, date_key)
    by_source = {source: int(counts.get(source, 0)) for source in expected}
    missing = [source for source, count in by_source.items() if count == 0]
    return {
        "date_key": date_key,
        "expected_sources": expected,
        "by_source": by_source,
        "missing": missing,
        "all_present": not missing,
    }


def query_collection_failures(
    con: Any, date_key: str | None, profile: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return profile-relevant per-feed collection failures for diagnostics."""
    if date_key is None:
        return []
    rows = normalize_item_rows(
        db_query_items(
            con,
            date_key=date_key,
            source_type="error",
            limit=None,
            order_by="COALESCE(published_at, fetched_at) DESC",
        )
    )
    categories = rss_categories(profile)
    expected_sources = set(profile_sources(profile))
    failures: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in rows:
        metadata = item.get("metadata") or {}
        category = str(metadata.get("category") or "")
        source_name = str(item.get("source_name") or "")
        if source_name.startswith("rss:") and categories and category not in categories:
            continue
        source = "rss" if source_name.startswith("rss:") else source_name.split(":", 1)[0]
        if source not in expected_sources:
            continue
        feed_name = source_name.split(":", 1)[1] if ":" in source_name else source_name
        key = (source_name, str(item.get("url") or ""))
        if key in seen:
            continue
        seen.add(key)
        failures.append(
            {
                "source": source,
                "feed_name": feed_name,
                "category": category or None,
                "url": item.get("url"),
                "error": compact_text(str(item.get("content") or ""), max_len=240),
            }
        )
    return failures


def apply_profile_coverage_filters(
    coverage: dict[str, Any],
    base_items: list[dict[str, Any]],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Adjust source coverage for deterministic profile-side filters."""
    categories = rss_categories(profile)
    by_source = coverage.get("by_source")
    if not isinstance(by_source, dict):
        return coverage
    if not categories or "rss" not in by_source:
        categories = set()
    if categories and "rss" in by_source:
        rss_count = sum(
            1
            for item in base_items
            if item.get("source_type") == "rss"
            and str((item.get("metadata") or {}).get("category") or "") in categories
        )
        by_source["rss"] = rss_count
        coverage.setdefault("profile_filters", {})["rss_categories"] = sorted(categories)
    item_keywords = profile_item_keywords(profile)
    keyword_sources = profile_keyword_filter_sources(profile)
    if item_keywords and keyword_sources:
        for source in keyword_sources:
            if source in by_source:
                by_source[source] = sum(
                    1
                    for item in base_items
                    if item.get("source_type") == source
                    and contains_any(item_search_text(item), item_keywords)
                )
        coverage.setdefault("profile_filters", {})["keyword_filter_sources"] = sorted(keyword_sources)
    missing = [source for source, count in by_source.items() if int(count or 0) == 0]
    coverage["missing"] = missing
    coverage["all_present"] = not missing
    return coverage


def load_prior_state(
    con: Any, profile_name: str, date_key: str | None
) -> dict[str, Any] | None:
    """Most recent watchboard strictly before date_key (carry-forward input)."""
    row = db_get_latest_report_state(con, profile_name, before_date_key=date_key)
    if not row:
        return None
    payload = json_loads(str(row.get("payload")), {})
    return {
        "state_date_key": str(row.get("date_key")),
        "updated_at": str(row.get("updated_at")) if row.get("updated_at") else None,
        "watchboard": payload,
    }


def load_framework_review(
    con: Any, profile_name: str, date_key: str | None, framework: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Slow-thinking trigger lamp + open challenges for the packet (deterministic).

    Reports how many days since the last framework review (>3 → due), the
    framework's invalidation_triggers (for the model to check against today's
    facts), the last verdict, and any still-open challenges the day must answer.
    None when the profile has no framework.md yet (un-migrated).
    """
    if not framework:
        return None
    prior = db_get_latest_framework_state(con, profile_name, before_date_key=date_key)
    days_since: int | None = None
    last_review: str | None = None
    last_verdict = None
    open_challenges: list[dict[str, Any]] = []
    if prior:
        last_review = str(prior.get("review_date_key"))
        payload = json_loads(str(prior.get("payload")), {})
        last_verdict = payload.get("regime_verdict")
        open_challenges = [
            ch
            for ch in (payload.get("challenges") or [])
            if isinstance(ch, dict) and ch.get("status") == "open"
        ]
        if date_key and last_review:
            try:
                days_since = (
                    datetime.strptime(date_key, "%Y-%m-%d")
                    - datetime.strptime(last_review, "%Y-%m-%d")
                ).days
            except ValueError:
                days_since = None
    due = (prior is None) or (days_since is not None and days_since > 3)
    reason = "首次 review(无上一期)" if prior is None else f"距上次 review {days_since} 天"
    return {
        "due": due,
        "reason": reason,
        "last_review_date": last_review,
        "last_verdict": last_verdict,
        "invalidation_triggers": framework.get("invalidation_triggers") or [],
        "open_challenges": open_challenges,
    }


def build_packet(args: argparse.Namespace) -> dict[str, Any]:
    profile = load_profile(args.profile, Path(args.config))
    date_key = resolve_date_key(args.date)
    state_enabled = bool(profile.get("state_enabled"))
    db_path = str(Path(args.db))
    db_ensure_connectable(db_path)
    with get_connection(db_path) as con:
        base_items = query_items(con, profile, date_key, args.limit)
        filtered_items = apply_profile_filters(base_items, profile)
        items = select_report_items(filtered_items, profile, args.limit)
        coverage_items = query_coverage_items(con, profile, date_key)
        enrichments = (
            query_enrichments(con, [str(item["id"]) for item in items])
            if args.include_enrichments
            else {}
        )
        coverage = apply_profile_coverage_filters(
            build_coverage(con, profile, date_key),
            coverage_items,
            profile,
        )
        feed_failures = query_collection_failures(con, date_key, profile)
        coverage["feed_failures"] = feed_failures
        coverage["degraded"] = bool(feed_failures)
        coverage["complete"] = bool(coverage.get("all_present")) and not feed_failures
        coverage["packet_selection"] = packet_selection_summary(
            items, profile, args.limit
        )
        prior_state = (
            load_prior_state(con, args.profile, date_key) if state_enabled else None
        )
        framework = (
            load_framework(args.profile, config_path=Path(args.config))
            if state_enabled
            else None
        )
        framework_review = (
            load_framework_review(con, args.profile, date_key, framework)
            if state_enabled
            else None
        )

    for item in items:
        item["enrichments"] = enrichments.get(str(item["id"]), [])

    packet = {
        "profile": args.profile,
        "profile_title": profile.get("title") or args.profile,
        "reference_dir": reference_dir(args.profile, profile),
        "date_key": date_key or "all",
        "generated_at": now_iso(),
        "sources": profile_sources(profile),
        "coverage": coverage,
        "state_enabled": state_enabled,
        "prior_state": prior_state,
        "framework_review": framework_review,
        "base_items_count": len(base_items),
        "filtered_items_count": len(filtered_items),
        "items_count": len(items),
        "items": items,
        "enrichment_candidates": build_enrichment_candidates(items),
        "include_enrichments": bool(args.include_enrichments),
    }
    if profile.get("macro_data"):
        macro_events = detect_macro_data_events(items, profile)
        packet["macro_news_items"] = items
        packet["macro_data_events"] = macro_events
        packet["macro_market_signals"] = fetch_macro_market_signals()
        packet["conditional_data_fetches"] = build_conditional_data_fetches(
            macro_events
        )
    return packet


def compact_text(text: str | None, max_len: int = 360) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "..."


def metadata_highlights(item: dict[str, Any]) -> list[str]:
    metadata = item.get("metadata") or {}
    source_type = item.get("source_type")
    if source_type == "github_trending":
        repo = metadata.get("github_api") or {}
        topics = metadata.get("topics") or repo.get("topics") or []
        return [
            f"stars={metadata.get('stars') or repo.get('stargazers_count')}",
            f"forks={metadata.get('forks') or repo.get('forks_count')}",
            f"language={metadata.get('language') or repo.get('language')}",
            f"stars_today={metadata.get('stars_today')}",
            f"license={repo.get('license')}",
            f"pushed_at={repo.get('pushed_at')}",
            f"topics={', '.join(topics[:8]) if isinstance(topics, list) else topics}",
        ]
    if source_type == "product_hunt":
        topics = metadata.get("topics") or []
        topic_names = [
            topic.get("name")
            for topic in topics
            if isinstance(topic, dict) and topic.get("name")
        ]
        makers = [
            maker.get("username") or maker.get("name")
            for maker in metadata.get("makers") or []
            if isinstance(maker, dict)
        ]
        return [
            f"daily_rank={metadata.get('daily_rank')}",
            f"votes={metadata.get('votes_count')}",
            f"comments={metadata.get('comments_count')}",
            f"website={metadata.get('website')}",
            f"topics={', '.join(topic_names[:8])}",
            f"makers={', '.join(makers[:6])}",
        ]
    if source_type == "hacker_news":
        return [
            f"score={metadata.get('score')}",
            f"comments={metadata.get('descendants')}",
            f"rank_sources={', '.join(metadata.get('rank_sources') or [])}",
            f"ranks={json.dumps(metadata.get('ranks') or {}, ensure_ascii=False)}",
        ]
    if source_type == "rss":
        return [
            f"feed_url={metadata.get('feed_url')}",
            f"category={metadata.get('category')}",
        ]
    return []


SIGNAL_GROUP_LABELS = {
    "liquidity_rates_fx": "Liquidity / Rates / FX",
    "equity_position": "Equity Market Position",
    "risk_appetite": "Risk Appetite",
    "commodities": "Commodities",
}
SIGNAL_FIELD_ORDER = (
    ("close", "Close"),
    ("change_pct", "ChgPct"),
    ("ups_percent", "JinChg%"),
    ("ytd_pct", "YTD%"),
    ("pct_off_52w_high", "Off52H%"),
    ("pct_above_52w_low", "Abv52L%"),
    ("ma20", "MA20"),
    ("pct_vs_ma20", "vsMA20%"),
    ("time", "Time"),
    ("source", "Src"),
)


def _format_signal_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def emit_macro_signal_groups(signals: dict[str, Any]) -> None:
    groups = signals.get("groups") or {}
    if not groups:
        return
    print("## Liquidity & Position Snapshot\n")
    for group_key, label in SIGNAL_GROUP_LABELS.items():
        rows = groups.get(group_key) or {}
        if not rows:
            continue
        print(f"### {label}\n")
        headers = ["Key"] + [label for _, label in SIGNAL_FIELD_ORDER]
        print("| " + " | ".join(headers) + " |")
        print("|" + "|".join(["---"] * len(headers)) + "|")
        for key, row in rows.items():
            if not isinstance(row, dict):
                continue
            cells = [key]
            for field, _ in SIGNAL_FIELD_ORDER:
                cells.append(_format_signal_value(row.get(field)))
            print("| " + " | ".join(cells) + " |")
        print()


def emit_markdown(packet: dict[str, Any]) -> None:
    print(f"# Evidence Packet: {packet['profile']} / {packet['date_key']}\n")
    print(f"- Generated: {packet['generated_at']}")
    print(f"- Sources: {', '.join(packet['sources'])}")
    if "base_items_count" in packet:
        print(f"- Base items before profile filters: {packet['base_items_count']}")
    if "filtered_items_count" in packet:
        print(f"- Items after profile filters: {packet['filtered_items_count']}")
    print(f"- Items: {packet['items_count']}")
    print(f"- Enrichment candidates: {len(packet['enrichment_candidates'])}\n")

    coverage = packet.get("coverage") or {}
    if coverage.get("by_source"):
        present = "; ".join(
            f"{source}={count}" for source, count in coverage["by_source"].items()
        )
        print("## Coverage (DB-first)\n")
        print(f"- By source: {present}")
        missing = coverage.get("missing") or []
        print(f"- Missing sources: {', '.join(missing) if missing else '(none)'}\n")
        selection = coverage.get("packet_selection") or {}
        if selection.get("by_source"):
            selected = "; ".join(
                f"{source}={count}"
                for source, count in selection["by_source"].items()
            )
            print(f"- Packet selection: {selected}")
            print(
                "- Selection policy: "
                f"preferred={selection.get('preferred_limits')}; "
                f"hard_max_share={selection.get('hard_max_share')}\n"
            )
        failures = coverage.get("feed_failures") or []
        if failures:
            print("### Feed failures (profile-relevant)\n")
            for failure in failures:
                print(
                    f"- {failure.get('source')} / {failure.get('feed_name')}: "
                    f"{failure.get('error')}"
                )
            print()

    if packet.get("state_enabled"):
        prior = packet.get("prior_state")
        print("## Prior Watchboard (carry-forward)\n")
        if not prior:
            print(
                "- None found — cold start. Build the initial watchboard from "
                "methodology defaults / seed, then save with save_report_state.py.\n"
            )
        else:
            watchboard = prior.get("watchboard") or {}
            open_items = [
                item
                for item in (watchboard.get("tracking_items") or [])
                if isinstance(item, dict) and item.get("status") == "open"
            ]
            print(f"- State date: {prior.get('state_date_key')}")
            try:
                gap = (
                    datetime.strptime(str(packet.get("date_key")), "%Y-%m-%d")
                    - datetime.strptime(str(prior.get("state_date_key")), "%Y-%m-%d")
                ).days
            except (ValueError, TypeError):
                gap = None
            if gap and gap > 1:
                print(
                    f"  ⚠️ 距上一期 watchboard {gap} 天，中间 {gap - 1} 天无状态 —— "
                    "勿把数据空洞误读为局势平静"
                )
            print(f"- Regime: {watchboard.get('regime')}")
            print(f"- Open tracking items to reconcile today: {len(open_items)}")
            for item in open_items:
                print(
                    f"  - {item.get('id')} (opened {item.get('opened')}): "
                    f"{item.get('statement')}"
                )
            print(
                "- Full prior watchboard JSON:\n"
                + json.dumps(watchboard, ensure_ascii=False, indent=2, default=str)
                + "\n"
            )

        fr = packet.get("framework_review")
        if fr:
            print("## Framework Review (slow-thinking)\n")
            lamp = "🔴 DUE — 该跑一次框架体检" if fr.get("due") else "🟢 not due — 框架体检未到点"
            print(f"- Trigger: {lamp}（{fr.get('reason')}；上次结论: {fr.get('last_verdict') or 'N/A'}）")
            triggers = fr.get("invalidation_triggers") or []
            if triggers:
                print("- 对照下列 invalidation_triggers 与当日事实判 regime 是否失效(命中即评估框架换代,见 framework_governance.md):")
                for trig in triggers:
                    print(f"  - {trig}")
            oc = fr.get("open_challenges") or []
            if oc:
                print(
                    f"- 未决框架挑战 {len(oc)} 条 — 本期 watchboard 必须在 challenge_responses 逐条回应(accepted/rejected + 理由):"
                )
                for ch in oc:
                    print(f"  - {ch.get('id')}: {ch.get('statement')}")
            print()

    if packet.get("macro_market_signals"):
        signals = packet["macro_market_signals"]
        emit_macro_signal_groups(signals)
        print("## Macro Market Signals (raw)\n")
        print(
            json.dumps(
                signals,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        print()

    if packet.get("macro_data_events"):
        print("## Macro Data Events\n")
        for event in packet["macro_data_events"]:
            print(
                "- "
                f"{event.get('published_at') or 'unknown'} | "
                f"{event.get('region')} | "
                f"{', '.join(event.get('detected_indicators') or [])} | "
                f"{event.get('title')} [{event.get('source_type')}]"
            )
            if event.get("excerpt"):
                print(f"  Excerpt: {event['excerpt']}")
        print()

    if packet.get("conditional_data_fetches"):
        print("## Conditional Data Fetches\n")
        print(
            json.dumps(
                packet["conditional_data_fetches"],
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        print()

    if packet["enrichment_candidates"]:
        print("## Enrichment Candidates\n")
        for target in packet["enrichment_candidates"]:
            print(
                "- "
                f"{target['target_type']} | {target['title']} | "
                f"{target['target_url']} | {target['reason']}"
            )
        print()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in packet["items"]:
        grouped.setdefault(str(item.get("source_type")), []).append(item)

    for source in packet["sources"]:
        source_items = grouped.get(source) or []
        if not source_items:
            continue
        print(f"## {source} ({len(source_items)})\n")
        for item in source_items:
            time_label = item.get("published_at") or item.get("fetched_at") or "unknown"
            url = item.get("url") or ""
            link = f" ([link]({url}))" if url else ""
            print(f"### {item.get('title') or '(untitled)'}{link}")
            print(f"- Time: {time_label}")
            print(f"- Item ID: {item.get('id')}")
            if item.get("content"):
                print(f"- Summary: {compact_text(item.get('content'), 520)}")
            highlights = [value for value in metadata_highlights(item) if value]
            if highlights:
                print(f"- Metadata: {'; '.join(highlights)}")
            enrichments = item.get("enrichments") or []
            if enrichments:
                print("- Enrichments:")
                for enrichment in enrichments:
                    title = enrichment.get("title") or "(no title)"
                    print(
                        "  - "
                        f"{enrichment.get('target_type')} "
                        f"[{enrichment.get('status')}] {title} "
                        f"{enrichment.get('target_url')}"
                    )
                    excerpt = compact_text(enrichment.get("text_excerpt"), 1200)
                    if excerpt:
                        print(f"    Excerpt: {excerpt}")
                    metadata = enrichment.get("metadata") or {}
                    if metadata:
                        selected = {
                            key: metadata.get(key)
                            for key in (
                                "description",
                                "homepage",
                                "language",
                                "topics",
                                "license",
                                "pushed_at",
                                "latest_release",
                                "h1",
                                "h2",
                                "key_links",
                                "cta_texts",
                                "final_url",
                            )
                            if metadata.get(key)
                        }
                        if selected:
                            print(
                                "    Metadata: "
                                + json.dumps(selected, ensure_ascii=False, default=str)
                            )
            print()


def main() -> int:
    args = parse_args()
    try:
        packet = build_packet(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(packet, ensure_ascii=False, indent=2, default=str))
    else:
        emit_markdown(packet)
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    raise SystemExit(main())
