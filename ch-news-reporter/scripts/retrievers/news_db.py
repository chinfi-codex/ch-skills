#!/usr/bin/env python3
"""news_db 通道:items 表关键词检索(库内已采集新闻)。

只做确定性取数:按 time_window_days 时间窗拉取 items 行,再用主题关键词
(支持引号短语)与 exclude_keywords 在 Python 侧过滤。判断哪条新闻重要是
模型的事,本脚本只保证"该捞的都捞到"。

说明:
- 库内 web_search 行由 web_search 通道单独呈现,本通道排除,避免双通道重复计数。
- collect_news 写入的 source_type='error' 采集失败行也排除(那是诊断数据,不是新闻)。
- 关键词为空时不返回任何行(防捞爆),并记 warning 提示补配置。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR.parent))

from retrievers.common import (  # noqa: E402
    item_search_text,
    keyword_hits,
    normalize_item_row,
    ph,
    run_query,
)

# 单次候选池上限:窗口内原始行先封顶,再在 Python 侧做关键词过滤
CANDIDATE_LIMIT = 3000
DEFAULT_MAX_ITEMS = 100


def window_start(date_key: str, time_window_days: int) -> str:
    """时间窗起点(含当天共 time_window_days 天)。"""
    day = datetime.strptime(date_key, "%Y-%m-%d")
    return (day - timedelta(days=max(1, time_window_days) - 1)).strftime("%Y-%m-%d")


def _channel_config(topic: dict[str, Any]) -> dict[str, Any]:
    """news_db 配置:True 或 {max_items: N}。"""
    raw = (topic.get("channels") or {}).get("news_db")
    if raw is True:
        return {"enabled": True, "max_items": DEFAULT_MAX_ITEMS}
    if isinstance(raw, dict):
        return {
            "enabled": bool(raw.get("enabled", True)),
            "max_items": int(raw.get("max_items") or DEFAULT_MAX_ITEMS),
        }
    return {"enabled": False, "max_items": DEFAULT_MAX_ITEMS}


def retrieve(con: Any, topic: dict[str, Any], date_key: str) -> dict[str, Any]:
    """按主题配置检索 items 表;返回统一通道结构 + items 列表。"""
    cfg = _channel_config(topic)
    if not cfg["enabled"]:
        return {"status": "skipped", "count": 0, "warnings": ["news_db 通道未启用"], "items": []}

    keywords = [str(k) for k in (topic.get("keywords") or []) if str(k).strip()]
    exclude = [str(k) for k in (topic.get("exclude_keywords") or []) if str(k).strip()]
    warnings: list[str] = []
    if not keywords:
        warnings.append("主题未配置 keywords,news_db 通道返回空(请先在 custom_topics.yaml 补关键词)")
        return {"status": "ok", "count": 0, "warnings": warnings, "items": []}

    days = int(topic.get("time_window_days") or 3)
    start = window_start(date_key, days)
    p = ph()
    sql = (
        f"SELECT * FROM items WHERE date_key BETWEEN {p} AND {p} "
        f"AND source_type NOT IN ({p}, {p}) "
        f"ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT {p}"
    )
    rows = run_query(con, sql, [start, date_key, "web_search", "error", CANDIDATE_LIMIT])

    items: list[dict[str, Any]] = []
    for row in rows:
        text = item_search_text(row)
        hits = keyword_hits(text, keywords)
        if not hits:
            continue
        if exclude and keyword_hits(text, exclude):
            continue
        item = normalize_item_row(row)
        item["matched_keywords"] = hits
        items.append(item)
        if len(items) >= cfg["max_items"]:
            break

    if not items:
        warnings.append(f"时间窗 {start} ~ {date_key} 内无关键词命中(候选池 {len(rows)} 行)")
    return {
        "status": "ok",
        "count": len(items),
        "warnings": warnings,
        "items": items,
        "window": {"start": start, "end": date_key, "days": days},
        "candidates": len(rows),
    }
