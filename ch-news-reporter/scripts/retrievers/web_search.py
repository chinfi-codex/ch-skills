#!/usr/bin/env python3
"""web_search 通道:Tavily 实时检索 + 预算控制 + 结果落库。

职责边界(脚本只做确定性动作):
- 按主题配置的 queries 逐条调 Tavily(复用 _shared/web_search/tavily_search 的核心逻辑);
- 预算控制:每主题 max_queries_per_day + 全局 global_max_queries_per_day;
  当日已执行的 query 通过 items 表里的 query_log 收据行计量,同日重跑不重复扣预算;
- 结果按 O2 落库 items(source_type='web_search',metadata_json 带 query/retrieved_at/topic);
- 去重:先 URL 规范化、再标题规范化,URL 与标题都相同才算重复(与库内任意
  source_type 的既有行比较,避免与 collect_news 抓到的同一篇重复);
- 降级:无 TAVILY_API_KEY / 网络失败 / API 报错时记 warning、通道 degraded,不崩溃。

检索结果值不值得写进报告,是模型的事;本脚本只保证证据可回指(每条结果带
query 与 retrieved_at,每个已执行 query 有一条收据)。
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_SCRIPT_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _SCRIPT_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_TAVILY_DIR = _SCRIPTS_DIR / "_shared" / "web_search"
if _TAVILY_DIR.exists() and str(_TAVILY_DIR) not in sys.path:
    sys.path.insert(0, str(_TAVILY_DIR))

from db_adapter import BACKEND, Backend, write_items as db_write_items  # noqa: E402
from retrievers.common import dedupe_pair, normalize_item_row, ph, run_query  # noqa: E402

try:
    from tavily_search import get_tavily_key, tavily_search
except ImportError:  # pragma: no cover - shared bundle 未同步时优雅降级
    get_tavily_key = None
    tavily_search = None

SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE_TYPE = "web_search"
QUERY_LOG_MARKER = "query_log"
DEFAULT_MAX_QUERIES = 3
DEFAULT_GLOBAL_MAX_QUERIES = 30
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_RESULTS = 5
# 判重窗口:至少覆盖主题时间窗,保底 14 天(跨天重跑与跨源同文都靠它)
MIN_DEDUPE_DAYS = 14


def _now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _source_name(slug: str) -> str:
    return f"tavily:{slug}"


def _settings_map(settings: dict[str, Any] | None) -> dict[str, int]:
    raw = ((settings or {}).get("web_search") or {})
    return {
        "global_max_queries_per_day": int(
            raw.get("global_max_queries_per_day") or DEFAULT_GLOBAL_MAX_QUERIES
        ),
        "retention_days": int(raw.get("retention_days") or DEFAULT_RETENTION_DAYS),
    }


def _usage(con: Any, date_key: str) -> dict[str, Any]:
    """当日已执行 query 计量:读 items 里的 query_log 收据行。

    每条已执行的 query(含 0 结果与报错)都落一条收据,因此预算是精确口径,
    同日重跑 topic_retrieve / prepare 不会重复消耗 Tavily 额度。
    """
    p = ph()
    rows = run_query(
        con,
        f"SELECT metadata_json FROM items WHERE source_type = {p} AND date_key = {p}",
        [SOURCE_TYPE, date_key],
    )
    by_topic: dict[str, set[str]] = {}
    total = 0
    for row in rows:
        try:
            metadata = json.loads(str(row.get("metadata_json") or "{}"))
        except ValueError:
            continue
        if metadata.get("marker") != QUERY_LOG_MARKER:
            continue
        topic = str(metadata.get("topic") or "")
        query = str(metadata.get("query") or "")
        if not topic or not query:
            continue
        by_topic.setdefault(topic, set()).add(query)
        total += 1
    return {"by_topic": by_topic, "total": total}


def _existing_pairs(con: Any, date_key: str, dedupe_days: int) -> set[tuple[str, str]]:
    """判重窗口内全部 source_type 的 (规范化 URL, 规范化标题) 集合。"""
    start = (
        datetime.strptime(date_key, "%Y-%m-%d") - timedelta(days=dedupe_days - 1)
    ).strftime("%Y-%m-%d")
    p = ph()
    rows = run_query(
        con,
        f"SELECT url, title FROM items WHERE date_key BETWEEN {p} AND {p}",
        [start, date_key],
    )
    return {dedupe_pair(row.get("url"), row.get("title")) for row in rows}


def _valid_iso_datetime(value: Any) -> str | None:
    """Tavily published_date 容错:非法格式一律落 None,避免 PG TIMESTAMPTZ 插入失败。

    Tavily 多数返回 ISO 8601,少数源给 RFC 2822(如 "Thu, 16 Jul 2026 08:00:00 GMT");
    两者都规范化为 ISO 字符串,其余格式不落库。
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return text
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(text).isoformat()
    except (TypeError, ValueError):
        return None


def _receipt_row(slug: str, date_key: str, query: str, status: str, detail: str) -> dict[str, Any]:
    """query_log 收据:每条已执行 query 一行,是预算计量与检索回指的凭据。"""
    receipt_id = hashlib.md5(
        f"web_search_query|{slug}|{date_key}|{query}".encode("utf-8")
    ).hexdigest()
    return {
        "id": receipt_id,
        "source_type": SOURCE_TYPE,
        "source_name": _source_name(slug),
        "published_at": None,
        "title": f"[web_search query] {query}",
        "content": "",
        "url": None,
        "tags": ["web_search", QUERY_LOG_MARKER],
        "metadata": {
            "marker": QUERY_LOG_MARKER,
            "topic": slug,
            "query": query,
            "status": status,
            "detail": detail[:240] if detail else "",
            "retrieved_at": _now_iso(),
        },
        "raw": {},
    }


def _result_row(
    slug: str, query: str, result: dict[str, Any], pair: tuple[str, str]
) -> dict[str, Any]:
    """单条 Tavily 结果 → items 行;id 由 (slug, 规范化 URL, 规范化标题) 派生,重跑幂等。"""
    row_id = hashlib.md5(
        f"web_search|{_source_name(slug)}|{pair[0]}|{pair[1]}".encode("utf-8")
    ).hexdigest()
    return {
        "id": row_id,
        "source_type": SOURCE_TYPE,
        "source_name": _source_name(slug),
        "published_at": _valid_iso_datetime(result.get("published_date")),
        "title": (result.get("title") or "").strip() or "(untitled)",
        "content": (result.get("content") or "").strip(),
        "url": (result.get("url") or "").strip() or None,
        "tags": ["web_search", f"topic:{slug}"],
        "metadata": {
            "topic": slug,
            "query": query,
            "retrieved_at": _now_iso(),
            "score": result.get("score"),
            "published_date": result.get("published_date"),
        },
        "raw": {},
    }


def _channel_config(topic: dict[str, Any]) -> dict[str, Any]:
    raw = (topic.get("channels") or {}).get("web_search")
    if not isinstance(raw, dict):
        return {"enabled": False, "queries": [], "max_queries_per_day": DEFAULT_MAX_QUERIES}
    queries = [str(q).strip() for q in (raw.get("queries") or []) if str(q).strip()]
    # 配置内重复 query 去重(保持顺序)
    queries = list(dict.fromkeys(queries))
    return {
        "enabled": True,
        "queries": queries,
        "max_queries_per_day": int(raw.get("max_queries_per_day") or DEFAULT_MAX_QUERIES),
    }


def _error_code(payload: dict[str, Any]) -> str:
    """兼容两版 tavily_search 的 error 形态(字符串或 {code, detail})。"""
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or "unknown_error")
    return str(err or "unknown_error")


def retrieve(
    con: Any,
    slug: str,
    topic: dict[str, Any],
    date_key: str,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行主题 web_search 通道;返回统一通道结构 + 窗口内证据行。"""
    cfg = _channel_config(topic)
    if not cfg["enabled"]:
        return {
            "status": "skipped",
            "count": 0,
            "warnings": ["web_search 通道未配置"],
            "queries": [],
            "items": [],
            "budget": {},
        }
    if not cfg["queries"]:
        return {
            "status": "skipped",
            "count": 0,
            "warnings": ["web_search 未配置 queries,已跳过"],
            "queries": [],
            "items": [],
            "budget": {},
        }

    limits = _settings_map(settings)
    days = int(topic.get("time_window_days") or 3)
    usage = _usage(con, date_key)
    already = usage["by_topic"].get(slug, set())
    topic_remaining = max(0, cfg["max_queries_per_day"] - len(already))
    global_remaining = max(0, limits["global_max_queries_per_day"] - usage["total"])
    budget_n = min(topic_remaining, global_remaining)
    pending = [q for q in cfg["queries"] if q not in already]
    to_run = pending[:budget_n]
    skipped_budget = len(pending) - len(to_run)

    budget = {
        "topic_max": cfg["max_queries_per_day"],
        "topic_used": len(already),
        "global_max": limits["global_max_queries_per_day"],
        "global_used": usage["total"],
        "ran_now": 0,
        "skipped_budget": skipped_budget,
    }

    warnings: list[str] = []
    query_reports: list[dict[str, Any]] = []
    inserted = 0
    duplicated = 0
    status = "ok"

    if skipped_budget:
        warnings.append(
            f"{skipped_budget} 条 query 因预算上限未执行(主题余量 {topic_remaining},全局余量 {global_remaining})"
        )

    if to_run and (tavily_search is None or get_tavily_key is None):
        warnings.append("tavily_search 模块不可用(shared bundle 未同步?),web_search 通道降级")
        status = "degraded"
        to_run = []
    elif to_run and not get_tavily_key():
        warnings.append("缺少 TAVILY_API_KEY,web_search 通道降级(未执行任何 query)")
        status = "degraded"
        to_run = []

    if to_run:
        dedupe_days = max(days, MIN_DEDUPE_DAYS)
        seen_pairs = _existing_pairs(con, date_key, dedupe_days)
        rows_to_write: list[dict[str, Any]] = []
        for query in to_run:
            payload = tavily_search(
                query, max_results=DEFAULT_MAX_RESULTS, topic="news", days=days
            )
            error = payload.get("error")
            if error:
                code = _error_code(payload)
                detail = str(payload.get("detail") or "")
                query_reports.append({"query": query, "status": "error", "error": code})
                warnings.append(f"query 失败({code}): {query}")
                status = "degraded"
                rows_to_write.append(_receipt_row(slug, date_key, query, "error", detail or code))
                budget["ran_now"] += 1
                # 无 key / 依赖缺失属于全局故障,后续 query 不必再试
                if code in {"missing_api_key", "missing_dependency"}:
                    warnings.append("其余 query 因全局故障中止")
                    break
                continue
            results = payload.get("results") or []
            kept = 0
            for result in results:
                pair = dedupe_pair(result.get("url"), result.get("title"))
                if pair in seen_pairs:
                    duplicated += 1
                    continue
                seen_pairs.add(pair)
                rows_to_write.append(_result_row(slug, query, result, pair))
                kept += 1
            inserted += kept
            query_reports.append(
                {
                    "query": query,
                    "status": "ok" if results else "empty",
                    "results": len(results),
                    "kept": kept,
                    "duplicated": len(results) - kept,
                }
            )
            rows_to_write.append(
                _receipt_row(slug, date_key, query, "ok" if results else "empty", "")
            )
            budget["ran_now"] += 1
        if rows_to_write:
            db_write_items(con, rows_to_write, date_key, _now_iso())

    # 读回时间窗内本主题的全量 web_search 证据(不含收据行),供证据包使用
    start = (datetime.strptime(date_key, "%Y-%m-%d") - timedelta(days=days - 1)).strftime(
        "%Y-%m-%d"
    )
    p = ph()
    rows = run_query(
        con,
        f"SELECT * FROM items WHERE source_type = {p} AND source_name = {p} "
        f"AND date_key BETWEEN {p} AND {p} "
        f"ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT {p}",
        [SOURCE_TYPE, _source_name(slug), start, date_key, 500],
    )
    items = []
    for row in rows:
        item = normalize_item_row(row)
        if (item.get("metadata") or {}).get("marker") == QUERY_LOG_MARKER:
            continue
        items.append(item)

    return {
        "status": status,
        "count": len(items),
        "warnings": warnings,
        "queries": query_reports,
        "items": items,
        "inserted_now": inserted,
        "duplicated_now": duplicated,
        "budget": budget,
        "window": {"start": start, "end": date_key, "days": days},
    }


def groom(con: Any, retention_days: int, date_key: str) -> int:
    """retention 清理:删除早于 (date_key - retention_days) 的 web_search 行。

    由 topic_retrieve 在每次检索后自动调用,也可经 --groom 单独触发。
    """
    if retention_days <= 0:
        return 0
    cutoff = (
        datetime.strptime(date_key, "%Y-%m-%d") - timedelta(days=retention_days)
    ).strftime("%Y-%m-%d")
    from retrievers.common import run_delete

    deleted = run_delete(
        con,
        f"DELETE FROM items WHERE source_type = {ph()} AND date_key < {ph()}",
        [SOURCE_TYPE, cutoff],
    )
    if deleted and BACKEND == Backend.SQLITE:
        # SQLite FTS 是外部内容表,删行后重建一次索引
        con.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
    return deleted
