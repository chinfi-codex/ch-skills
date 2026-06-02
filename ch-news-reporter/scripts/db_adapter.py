"""
News database adapter for ch-news-reporter.

Abstracts SQLite ↔ PostgreSQL differences.  All CRUD goes through here;
scripts never touch sqlite3/psycopg2 directly.

Environment:
    ALPHA_DB_BACKEND=sqlite|postgresql
    ALPHA_PG_URL=postgresql://...
    ALPHA_SQLITE_DIR=~/AlphaData/db

Jieba Chinese word-segmentation is used for PostgreSQL tsvector generation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

try:
    import jieba
except ImportError:
    jieba = None

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[1] / "shared"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))
from db_core import (
    BACKEND,
    Backend,
    adapt_sql,
    get_connection,
    placeholder,
    row_to_dict,
    rows_to_dicts,
    table_exists,
)

try:
    from psycopg2.extras import RealDictCursor
except ImportError:  # pragma: no cover
    RealDictCursor = None

DEFAULT_DB_PATH = _SCRIPT_DIR.parent / "data" / "news_research.sqlite"


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------
def init_news_schema(conn: Any) -> None:
    """Create tables + indexes.  Safe to call repeatedly (IF NOT EXISTS)."""
    if BACKEND == Backend.SQLITE:
        _init_sqlite_schema(conn)
    else:
        _init_postgresql_schema(conn)


def _init_sqlite_schema(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            date_key TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_name TEXT NOT NULL,
            published_at TEXT,
            fetched_at TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT,
            url TEXT,
            author TEXT,
            tags_json TEXT,
            metadata_json TEXT,
            raw_json TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_date ON items(date_key)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_source ON items(source_type, source_name)"
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
            title, content, tags_json,
            content='items', content_rowid='rowid'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS enrichments (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            enrichment_type TEXT NOT NULL,
            source TEXT,
            model TEXT,
            prompt_hash TEXT,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_enrichments_item ON enrichments(item_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_state (
            profile TEXT NOT NULL,
            date_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (profile, date_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_report_state_profile_date "
        "ON report_state(profile, date_key)"
    )


def _init_postgresql_schema(conn: Any) -> None:
    # PostgreSQL schema is managed by init_alpha_data.sql;
    # this function only verifies tables exist.
    if not table_exists(conn, "items"):
        raise RuntimeError(
            "PostgreSQL tables not found. Run init_alpha_data.sql first."
        )


# ---------------------------------------------------------------------------
# Jieba → tsvector helper (PostgreSQL only)
# ---------------------------------------------------------------------------
def _jieba_tokens(text: str) -> str:
    """Segment Chinese text with jieba; return space-joined tokens."""
    if not text:
        return ""
    if jieba is None:
        # Fallback: character-level (same as 'simple' tsvector)
        return " ".join(text.split())
    tokens = jieba.cut(text.strip())
    return " ".join(t for t in tokens if t.strip())


def _build_tsvector(title: str, content: str, tags: str) -> str:
    """Build a PostgreSQL tsvector expression string for INSERT/UPDATE."""
    title_tokens = _jieba_tokens(title or "")
    content_tokens = _jieba_tokens(content or "")
    tags_tokens = _jieba_tokens(tags or "")
    parts = []
    if title_tokens:
        parts.append(f"setweight(to_tsvector('simple', {placeholder()}), 'A')")
    if content_tokens:
        parts.append(f"setweight(to_tsvector('simple', {placeholder()}), 'B')")
    if tags_tokens:
        parts.append(f"setweight(to_tsvector('simple', {placeholder()}), 'C')")
    if not parts:
        return "to_tsvector('')"
    return " || ".join(parts)


# ---------------------------------------------------------------------------
# Items CRUD
# ---------------------------------------------------------------------------
def write_items(
    conn: Any,
    items: list[dict[str, Any]],
    date_key: str,
    fetched_at: str,
) -> int:
    """Upsert news items.  Returns count of inserted/updated rows."""
    inserted = 0
    ph = placeholder()

    for item in items:
        # Compute row_id same as original collect_news.py logic
        row_id = item.get("id") or _stable_id(item, date_key)

        if BACKEND == Backend.SQLITE:
            conn.execute(
                f"""
                INSERT INTO items (
                    id, date_key, source_type, source_name, published_at, fetched_at,
                    title, content, url, author, tags_json, metadata_json, raw_json
                ) VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
                ON CONFLICT(id) DO UPDATE SET
                    fetched_at=excluded.fetched_at,
                    title=excluded.title,
                    content=excluded.content,
                    url=excluded.url,
                    author=excluded.author,
                    tags_json=excluded.tags_json,
                    metadata_json=excluded.metadata_json,
                    raw_json=excluded.raw_json
                """,
                (
                    row_id,
                    date_key,
                    item["source_type"],
                    item["source_name"],
                    item.get("published_at"),
                    fetched_at,
                    (item.get("title") or "").strip() or "(untitled)",
                    (item.get("content") or "").strip(),
                    item.get("url"),
                    item.get("author"),
                    json.dumps(item.get("tags") or [], ensure_ascii=False, default=str),
                    json.dumps(item.get("metadata") or {}, ensure_ascii=False, default=str),
                    json.dumps(item.get("raw") or {}, ensure_ascii=False, default=str),
                ),
            )
        else:
            # PostgreSQL path — include search_vector built with jieba
            title = (item.get("title") or "").strip() or "(untitled)"
            content = (item.get("content") or "").strip()
            tags_json_str = json.dumps(item.get("tags") or [], ensure_ascii=False, default=str)

            tsvector_sql = _build_tsvector(title, content, tags_json_str)
            tsvector_params = []
            if title:
                tsvector_params.append(_jieba_tokens(title))
            if content:
                tsvector_params.append(_jieba_tokens(content))
            if tags_json_str:
                tsvector_params.append(_jieba_tokens(tags_json_str))

            sql = f"""
                INSERT INTO items (
                    id, date_key, source_type, source_name, published_at, fetched_at,
                    title, content, url, author, tags_json, metadata_json, raw_json,
                    search_vector
                ) VALUES (
                    {ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},
                    {tsvector_sql}
                )
                ON CONFLICT(id) DO UPDATE SET
                    fetched_at=excluded.fetched_at,
                    title=excluded.title,
                    content=excluded.content,
                    url=excluded.url,
                    author=excluded.author,
                    tags_json=excluded.tags_json,
                    metadata_json=excluded.metadata_json,
                    raw_json=excluded.raw_json,
                    search_vector=excluded.search_vector
            """
            params = [
                row_id,
                date_key,
                item["source_type"],
                item["source_name"],
                item.get("published_at"),
                fetched_at,
                title,
                content,
                item.get("url"),
                item.get("author"),
                tags_json_str,
                json.dumps(item.get("metadata") or {}, ensure_ascii=False, default=str),
                json.dumps(item.get("raw") or {}, ensure_ascii=False, default=str),
            ] + tsvector_params
            conn.cursor().execute(sql, params)

        inserted += 1

    # SQLite: rebuild FTS index after batch insert
    if BACKEND == Backend.SQLITE:
        conn.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")

    return inserted


def query_items(
    conn: Any,
    keywords: Optional[str] = None,
    date_key: Optional[str] = None,
    source: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: int = 1000,
    order_by: str = "published_at DESC",
) -> list[dict[str, Any]]:
    """Query items with optional keyword search, date filter, source filter."""
    ph = placeholder()
    conditions: list[str] = []
    params: list[Any] = []

    if date_key:
        conditions.append(f"date_key = {ph}")
        params.append(date_key)

    if source_type:
        conditions.append(f"source_type = {ph}")
        params.append(source_type)

    if source:
        conditions.append(f"source_name = {ph}")
        params.append(source)

    if keywords and keywords.strip():
        kw = keywords.strip()
        if BACKEND == Backend.SQLITE:
            # FTS5 match
            conditions.append(
                f"rowid IN (SELECT rowid FROM items_fts WHERE items_fts MATCH {ph})"
            )
            params.append(kw)
        else:
            # PostgreSQL tsvector with jieba-segmented query
            query_tokens = _jieba_tokens(kw)
            conditions.append(f"search_vector @@ plainto_tsquery('simple', {ph})")
            params.append(query_tokens)

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    sql = f"SELECT * FROM items WHERE {where_clause} ORDER BY {order_by} LIMIT {ph}"
    params.append(limit)

    if BACKEND == Backend.SQLITE:
        cur = conn.execute(sql, params)
    else:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(adapt_sql(sql), params)

    return rows_to_dicts(cur.fetchall())


# ---------------------------------------------------------------------------
# Enrichments CRUD
# ---------------------------------------------------------------------------
def ensure_enrichments_table(conn: Any) -> None:
    """Idempotent: create enrichments table (SQLite only; PG managed by schema)."""
    if BACKEND == Backend.SQLITE:
        _init_sqlite_schema(conn)


def write_enrichment(
    conn: Any,
    enrichment_id: str,
    item_id: str,
    enrichment_type: str,
    source: Optional[str],
    model: Optional[str],
    prompt_hash: Optional[str],
    result_json: str,
    created_at: str,
) -> None:
    """Insert a single enrichment record."""
    ph = placeholder()
    sql = f"""
        INSERT INTO enrichments (
            id, item_id, enrichment_type, source, model, prompt_hash, result_json, created_at
        ) VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
        ON CONFLICT(id) DO UPDATE SET
            result_json=excluded.result_json,
            created_at=excluded.created_at
    """
    params = (
        enrichment_id,
        item_id,
        enrichment_type,
        source,
        model,
        prompt_hash,
        result_json,
        created_at,
    )
    if BACKEND == Backend.SQLITE:
        conn.execute(sql, params)
    else:
        conn.cursor().execute(adapt_sql(sql), params)


def get_enrichments_by_items(
    conn: Any, item_ids: list[str]
) -> list[dict[str, Any]]:
    """Fetch enrichments for given item_ids."""
    if not item_ids:
        return []
    ph = placeholder()
    placeholders = ",".join([ph] * len(item_ids))
    sql = f"SELECT * FROM enrichments WHERE item_id IN ({placeholders})"

    if BACKEND == Backend.SQLITE:
        cur = conn.execute(sql, item_ids)
    else:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(adapt_sql(sql), item_ids)

    return rows_to_dicts(cur.fetchall())


# ---------------------------------------------------------------------------
# Coverage (DB-first read policy)
# ---------------------------------------------------------------------------
def count_items_by_source(conn: Any, date_key: str) -> dict[str, int]:
    """Return {source_type: row_count} for a given date_key.

    Used by the DB-first read policy: collect_news skips sources already
    present, prepare_report_data reports per-source coverage to the model.
    """
    ph = placeholder()
    sql = (
        f"SELECT source_type, COUNT(*) AS n FROM items "
        f"WHERE date_key = {ph} GROUP BY source_type"
    )
    if BACKEND == Backend.SQLITE:
        cur = conn.execute(sql, (date_key,))
    else:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(adapt_sql(sql), (date_key,))
    counts: dict[str, int] = {}
    for row in rows_to_dicts(cur.fetchall()):
        counts[str(row.get("source_type"))] = int(row.get("n") or 0)
    return counts


# ---------------------------------------------------------------------------
# Report state / watchboard CRUD
# ---------------------------------------------------------------------------
def write_report_state(
    conn: Any,
    profile: str,
    date_key: str,
    payload_json: str,
    now_iso: str,
) -> None:
    """Upsert one watchboard payload for (profile, date_key).

    payload_json must already be a JSON string.  created_at is preserved on
    update; only payload and updated_at change.
    """
    ph = placeholder()
    sql = f"""
        INSERT INTO report_state (profile, date_key, payload, created_at, updated_at)
        VALUES ({ph},{ph},{ph},{ph},{ph})
        ON CONFLICT(profile, date_key) DO UPDATE SET
            payload=excluded.payload,
            updated_at=excluded.updated_at
    """
    params = (profile, date_key, payload_json, now_iso, now_iso)
    if BACKEND == Backend.SQLITE:
        conn.execute(sql, params)
    else:
        conn.cursor().execute(adapt_sql(sql), params)


def get_latest_report_state(
    conn: Any, profile: str, before_date_key: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Return the most recent report_state row for *profile*.

    When *before_date_key* is given, only rows strictly earlier are considered
    (so regenerating day N loads day N-1, not N's own state).  Returns None when
    the table or any matching row is absent.
    """
    if not table_exists(conn, "report_state"):
        return None
    ph = placeholder()
    conditions = [f"profile = {ph}"]
    params: list[Any] = [profile]
    if before_date_key:
        conditions.append(f"date_key < {ph}")
        params.append(before_date_key)
    where_clause = " AND ".join(conditions)
    sql = (
        f"SELECT * FROM report_state WHERE {where_clause} "
        f"ORDER BY date_key DESC LIMIT {ph}"
    )
    params.append(1)
    if BACKEND == Backend.SQLITE:
        cur = conn.execute(sql, params)
    else:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(adapt_sql(sql), params)
    row = cur.fetchone()
    return row_to_dict(row) if row else None


def get_report_state(
    conn: Any, profile: str, date_key: str
) -> Optional[dict[str, Any]]:
    """Return the exact report_state row for (profile, date_key), or None."""
    if not table_exists(conn, "report_state"):
        return None
    ph = placeholder()
    sql = (
        f"SELECT * FROM report_state WHERE profile = {ph} AND date_key = {ph} "
        f"LIMIT {ph}"
    )
    params: list[Any] = [profile, date_key, 1]
    if BACKEND == Backend.SQLITE:
        cur = conn.execute(sql, params)
    else:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(adapt_sql(sql), params)
    row = cur.fetchone()
    return row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _stable_id(item: dict[str, Any], date_key: str) -> str:
    """Generate deterministic ID from item content (mirrors original logic)."""
    import hashlib

    src = f"{item.get('source_type','')}|{item.get('source_name','')}|{item.get('title','')}|{date_key}"
    return hashlib.md5(src.encode("utf-8")).hexdigest()
