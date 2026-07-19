#!/usr/bin/env python3
"""retrievers 共享的确定性小工具:关键词匹配、URL/标题规范化、行归一化。

只做字符串与 JSON 的机械处理,不含任何领域判断。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

# 允许直接运行单个 retriever(python scripts/retrievers/xxx.py):
# 把 scripts/ 放上 sys.path,以便 import db_adapter。
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR.parent))

from db_adapter import (  # noqa: E402
    BACKEND,
    Backend,
    RealDictCursor,
    adapt_sql,
    placeholder,
    rows_to_dicts,
)


def json_loads(value: Any, fallback: Any) -> Any:
    """容错解析 JSON 文本列(tags_json / metadata_json / raw_json)。"""
    if not value:
        return fallback
    if not isinstance(value, str):
        return value if isinstance(value, (list, dict)) else fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def compact_text(text: str | None, max_len: int = 360) -> str:
    """压空白并截断,与 prepare_report_data 的摘要风格一致。"""
    clean = " ".join((text or "").split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "..."


# ---------------------------------------------------------------------------
# 关键词匹配(支持引号短语:'"Rubin ramp"' 整体匹配;其余为普通子串,大小写不敏感)
# ---------------------------------------------------------------------------
def split_keyword(keyword: str) -> str:
    """剥掉关键词外层双引号;引号只是"整体短语"标记,匹配仍是子串语义。"""
    kw = str(keyword).strip()
    if len(kw) >= 2 and kw.startswith('"') and kw.endswith('"'):
        return kw[1:-1].strip()
    return kw


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    """返回命中的原始关键词列表(未命中为 []);大小写不敏感。"""
    lowered = (text or "").lower()
    hits: list[str] = []
    for keyword in keywords or []:
        kw = split_keyword(keyword)
        if kw and kw.lower() in lowered:
            hits.append(str(keyword))
    return hits


def item_search_text(row: dict[str, Any]) -> str:
    """items 行的可检索文本:标题 + 正文 + 来源名 + tags/metadata 原文。"""
    return " ".join(
        str(row.get(field) or "")
        for field in ("title", "content", "source_name", "tags_json", "metadata_json")
    )


# ---------------------------------------------------------------------------
# URL / 标题规范化(web_search 落库去重与跨通道合并去重共用)
# ---------------------------------------------------------------------------
def normalize_url(url: str | None) -> str:
    """规范化 URL:小写 scheme/host、去 fragment、去末尾斜杠。

    与 prepare_report_data.normalize_url 同族,额外小写 host 以提高判重命中率。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if path == "/":
        path = ""
    normalized = parsed._replace(scheme=scheme, netloc=netloc, path=path, fragment="")
    return urlunparse(normalized)


def normalize_title(title: str | None) -> str:
    """标题规范化:小写 + 压空白,用于标题级判重。"""
    return " ".join((title or "").lower().split())


def dedupe_pair(url: str | None, title: str | None) -> tuple[str, str]:
    """判重键:(规范化 URL, 规范化标题)。两者都相同才算重复。"""
    return (normalize_url(url), normalize_title(title))


# ---------------------------------------------------------------------------
# items 行归一化(与 prepare_report_data.normalize_item_rows 对齐)
# ---------------------------------------------------------------------------
def normalize_item_row(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["tags"] = json_loads(item.pop("tags_json", None), [])
    item["metadata"] = json_loads(item.pop("metadata_json", None), {})
    item["raw"] = json_loads(item.pop("raw_json", None), {})
    return item


# ---------------------------------------------------------------------------
# 双后端查询小帮手(SQLite / PostgreSQL 走同一套 SQL + 占位符)
# ---------------------------------------------------------------------------
def run_query(con: Any, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    """执行只读查询并返回 dict 行;封装 SQLite/PostgreSQL 游标差异。"""
    if BACKEND == Backend.SQLITE:
        cur = con.execute(sql, params)
    else:
        cur = con.cursor(cursor_factory=RealDictCursor)
        cur.execute(adapt_sql(sql), params)
    return rows_to_dicts(cur.fetchall())


def run_delete(con: Any, sql: str, params: list[Any]) -> int:
    """执行 DELETE 并返回受影响行数(供 retention 清理用)。"""
    if BACKEND == Backend.SQLITE:
        cur = con.execute(sql, params)
    else:
        cur = con.cursor()
        cur.execute(adapt_sql(sql), params)
    return int(cur.rowcount or 0)


def ph() -> str:
    """当前后端的参数占位符(透传 db_core.placeholder)。"""
    return placeholder()
