"""
Unified database connection layer for all CH Skills.

Switch backend via ALPHA_DB_BACKEND env var:
    export ALPHA_DB_BACKEND=postgresql
    export ALPHA_PG_URL="postgresql://alpha_user:password@localhost:5432/alpha_data"

Or fallback to SQLite:
    export ALPHA_DB_BACKEND=sqlite
    export ALPHA_SQLITE_DIR="~/AlphaData/db"
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from enum import Enum
from typing import Any, Generator


class Backend(Enum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
BACKEND = Backend(os.getenv("ALPHA_DB_BACKEND", "postgresql"))
PG_URL = os.getenv("ALPHA_PG_URL", "postgresql://alpha_user:alpha_pass@/alpha_data?host=/tmp")
SQLITE_DIR = os.path.expanduser(os.getenv("ALPHA_SQLITE_DIR", "."))
PG_CONNECT_TIMEOUT = int(os.getenv("ALPHA_PG_CONNECT_TIMEOUT", "5"))


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------
@contextmanager
def get_connection(db_path: str | None = None) -> Generator[Any, None, None]:
    """
    Yield a DB connection (sqlite3.Connection or psycopg2 connection).
    Caller receives the connection; commit/rollback is handled on exit.
    """
    if BACKEND == Backend.SQLITE:
        import sqlite3

        path = os.path.expanduser(db_path or os.path.join(SQLITE_DIR, "alpha.db"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    else:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(PG_URL, cursor_factory=RealDictCursor, connect_timeout=PG_CONNECT_TIMEOUT)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# SQL dialect helpers
# ---------------------------------------------------------------------------
def placeholder() -> str:
    """Return the parameter placeholder for the current backend."""
    return "?" if BACKEND == Backend.SQLITE else "%s"


def adapt_sql(sql: str) -> str:
    """Convert SQLite-flavoured SQL to PostgreSQL dialect.

    Rules applied (idempotent for SQLite):
      1. '?' placeholders -> '%s'
      2. sqlite_master    -> information_schema.tables
      3. ON CONFLICT(...) -> PostgreSQL compatible (same syntax, no change needed)
      4. PRAGMA           -> no-op (returned as empty SELECT)
    """
    if BACKEND == Backend.SQLITE:
        return sql

    # 1. positional placeholders
    sql = sql.replace("?", "%s")

    # 2. sqlite_master → information_schema.tables
    sql = sql.replace(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name =",
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name =",
    )

    # 3. PRAGMA → no-op
    if sql.strip().upper().startswith("PRAGMA"):
        return "SELECT 1 WHERE false"

    return sql


def table_exists(conn: Any, table_name: str) -> bool:
    """Return True if *table_name* exists in the current DB."""
    if BACKEND == Backend.SQLITE:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        return cur.fetchone() is not None
    else:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = %s",
            (table_name,),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Row helpers (produce plain dict from any row type)
# ---------------------------------------------------------------------------
def row_to_dict(row: Any) -> dict[str, Any]:
    """Normalise sqlite3.Row / RealDictRow / dict → plain dict."""
    return dict(row)


def rows_to_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    """Bulk version of row_to_dict."""
    return [dict(r) for r in rows]
