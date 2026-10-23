#!/usr/bin/env python3
"""Persistence for the stock pool (pool_member / pool_daily / pool_event).

One row in pool_member is one *episode*: a stock removed and later re-qualified
gets a new row keyed by its new entry_date, so history and rule statistics stay
intact.  The store only validates and persists; it never judges.
Schema mirror: scripts/_shared/init_alpha_data.sql §16.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = SCRIPT_DIR / "_shared"
_DEV_SHARED = SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

from db_core import BACKEND, Backend, get_connection, placeholder, table_exists  # noqa: E402

STATUSES = ("active", "watch", "removed")
SOURCES = ("auto", "manual")
EVENT_KINDS = ("entry", "relimit", "status", "signal", "news", "note")

_TS = "TIMESTAMPTZ NOT NULL DEFAULT NOW()" if BACKEND == Backend.POSTGRESQL else "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
_DATE = "DATE" if BACKEND == Backend.POSTGRESQL else "TEXT"
_NUM = "DOUBLE PRECISION" if BACKEND == Backend.POSTGRESQL else "REAL"
_ID = "BIGSERIAL PRIMARY KEY" if BACKEND == Backend.POSTGRESQL else "INTEGER PRIMARY KEY AUTOINCREMENT"

SCHEMA = [
    f"""
    CREATE TABLE IF NOT EXISTS pool_member (
        ts_code           TEXT NOT NULL,
        entry_date        {_DATE} NOT NULL,
        name              TEXT,
        source            TEXT NOT NULL DEFAULT 'auto',
        status            TEXT NOT NULL DEFAULT 'active',
        limit_price       {_NUM},
        entry_low         {_NUM},
        launch_price      {_NUM},
        entry_amount      {_NUM},
        entry_turnover    {_NUM},
        entry_adj_factor  {_NUM},
        first_limit_time  TEXT,
        reason            TEXT,
        theme_id          TEXT,
        theme_name        TEXT,
        watch_since       {_DATE},
        suggest_remove    INTEGER NOT NULL DEFAULT 0,
        removed_date      {_DATE},
        removed_reason    TEXT,
        created_at        {_TS},
        updated_at        {_TS},
        PRIMARY KEY (ts_code, entry_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pool_member_status ON pool_member(status)",
    f"""
    CREATE TABLE IF NOT EXISTS pool_daily (
        ts_code      TEXT NOT NULL,
        entry_date   {_DATE} NOT NULL,
        trade_date   {_DATE} NOT NULL,
        status       TEXT NOT NULL,
        metrics      TEXT NOT NULL,
        exit_hits    TEXT,
        signals      TEXT,
        verdict      TEXT,
        note         TEXT,
        created_at   {_TS},
        PRIMARY KEY (ts_code, entry_date, trade_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pool_daily_date ON pool_daily(trade_date)",
    f"""
    CREATE TABLE IF NOT EXISTS pool_event (
        id           {_ID},
        ts_code      TEXT NOT NULL,
        entry_date   {_DATE} NOT NULL,
        event_date   {_DATE} NOT NULL,
        kind         TEXT NOT NULL,
        category     TEXT,
        title        TEXT NOT NULL,
        detail       TEXT,
        source       TEXT,
        url          TEXT,
        created_at   {_TS}
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pool_event_stock ON pool_event(ts_code, entry_date, event_date)",
]


class PoolError(ValueError):
    pass


def _iso(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    raw = str(value)
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw[:10]


def _rows(conn: Any, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        item = dict(zip(cols, row))
        for key in ("entry_date", "trade_date", "event_date", "watch_since", "removed_date"):
            if key in item:
                item[key] = _iso(item[key])
        for key in ("metrics", "exit_hits", "signals"):
            if isinstance(item.get(key), str):
                item[key] = json.loads(item[key])
        out.append(item)
    return out


def _exec(conn: Any, sql: str, params: tuple = ()) -> None:
    conn.cursor().execute(sql, params)


def ensure_schema() -> None:
    with get_connection() as conn:
        for stmt in SCHEMA:
            _exec(conn, stmt)


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #
def list_members(statuses: tuple[str, ...] = ("active", "watch"), ts_code: Optional[str] = None) -> list[dict]:
    ph = placeholder()
    where, params = [], []
    if statuses:
        where.append(f"status IN ({', '.join([ph] * len(statuses))})")
        params.extend(statuses)
    if ts_code:
        where.append(f"ts_code = {ph}")
        params.append(ts_code)
    sql = "SELECT * FROM pool_member"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY entry_date, ts_code"
    with get_connection() as conn:
        return _rows(conn, sql, tuple(params))


def open_member(ts_code: str) -> Optional[dict]:
    rows = list_members(("active", "watch"), ts_code)
    return rows[-1] if rows else None


def add_member(rec: dict) -> dict:
    if rec.get("source", "auto") not in SOURCES:
        raise PoolError(f"source 必须是 {SOURCES}")
    if open_member(rec["ts_code"]):
        raise PoolError(f"{rec['ts_code']} 已在池中（active/watch），不重复入池")
    cols = ["ts_code", "entry_date", "name", "source", "status", "limit_price", "entry_low",
            "launch_price", "entry_amount", "entry_turnover", "entry_adj_factor",
            "first_limit_time", "reason", "theme_id", "theme_name"]
    values = [rec.get(c) for c in cols]
    values[cols.index("status")] = "active"
    values[cols.index("source")] = rec.get("source") or "auto"
    ph = placeholder()
    with get_connection() as conn:
        _exec(conn, f"INSERT INTO pool_member ({', '.join(cols)}) VALUES ({', '.join([ph] * len(cols))})",
              tuple(values))
    add_event(rec["ts_code"], rec["entry_date"], rec["entry_date"], "entry",
              rec.get("entry_title") or ("手动入池" if rec.get("source") == "manual" else "规则入池"),
              detail=rec.get("reason"))
    return open_member(rec["ts_code"])


def update_member(ts_code: str, entry_date: str, **fields: Any) -> None:
    allowed = {"name", "status", "reason", "theme_id", "theme_name", "watch_since",
               "suggest_remove", "removed_date", "removed_reason"}
    bad = set(fields) - allowed
    if bad:
        raise PoolError(f"不可更新字段: {sorted(bad)}")
    if "status" in fields and fields["status"] not in STATUSES:
        raise PoolError(f"status 必须是 {STATUSES}")
    if not fields:
        return
    ph = placeholder()
    sets = ", ".join(f"{k} = {ph}" for k in fields) + ", updated_at = CURRENT_TIMESTAMP"
    with get_connection() as conn:
        _exec(conn, f"UPDATE pool_member SET {sets} WHERE ts_code = {ph} AND entry_date = {ph}",
              tuple(fields.values()) + (ts_code, entry_date))


# --------------------------------------------------------------------------- #
# Daily snapshots & events
# --------------------------------------------------------------------------- #
def upsert_daily(ts_code: str, entry_date: str, trade_date: str, status: str,
                 metrics: dict, exit_hits: list, signals: list) -> None:
    ph = placeholder()
    payload = (ts_code, entry_date, trade_date, status,
               json.dumps(metrics, ensure_ascii=False),
               json.dumps(exit_hits, ensure_ascii=False),
               json.dumps(signals, ensure_ascii=False))
    with get_connection() as conn:
        _exec(conn,
              f"INSERT INTO pool_daily (ts_code, entry_date, trade_date, status, metrics, exit_hits, signals) "
              f"VALUES ({', '.join([ph] * 7)}) "
              f"ON CONFLICT (ts_code, entry_date, trade_date) DO UPDATE SET status = EXCLUDED.status, "
              f"metrics = EXCLUDED.metrics, exit_hits = EXCLUDED.exit_hits, signals = EXCLUDED.signals",
              payload)


def set_verdict(ts_code: str, trade_date: str, verdict: str, note: Optional[str] = None) -> int:
    member = open_member(ts_code) or (list_members((), ts_code) or [None])[-1]
    if not member:
        raise PoolError(f"{ts_code} 不在池中")
    ph = placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE pool_daily SET verdict = {ph}, note = {ph} "
                    f"WHERE ts_code = {ph} AND entry_date = {ph} AND trade_date = {ph}",
                    (verdict, note, ts_code, member["entry_date"], trade_date))
        return cur.rowcount


def daily_history(ts_code: str, entry_date: str, limit: int = 30) -> list[dict]:
    ph = placeholder()
    with get_connection() as conn:
        rows = _rows(conn, f"SELECT * FROM pool_daily WHERE ts_code = {ph} AND entry_date = {ph} "
                           f"ORDER BY trade_date DESC LIMIT {int(limit)}", (ts_code, entry_date))
    return list(reversed(rows))


def add_event(ts_code: str, entry_date: str, event_date: str, kind: str, title: str,
              detail: Optional[str] = None, category: Optional[str] = None,
              source: Optional[str] = None, url: Optional[str] = None) -> None:
    if kind not in EVENT_KINDS:
        raise PoolError(f"kind 必须是 {EVENT_KINDS}")
    ph = placeholder()
    with get_connection() as conn:
        # 同一天重跑不重复记事件
        if _rows(conn, f"SELECT 1 AS x FROM pool_event WHERE ts_code = {ph} AND entry_date = {ph} "
                       f"AND event_date = {ph} AND kind = {ph} AND title = {ph}",
                 (ts_code, entry_date, event_date, kind, title)):
            return
        _exec(conn,
              f"INSERT INTO pool_event (ts_code, entry_date, event_date, kind, category, title, detail, source, url) "
              f"VALUES ({', '.join([ph] * 9)})",
              (ts_code, entry_date, event_date, kind, category, title, detail, source, url))


def events(ts_code: str, entry_date: Optional[str] = None, since: Optional[str] = None) -> list[dict]:
    ph = placeholder()
    sql, params = f"SELECT * FROM pool_event WHERE ts_code = {ph}", [ts_code]
    if entry_date:
        sql += f" AND entry_date = {ph}"
        params.append(entry_date)
    if since:
        sql += f" AND event_date >= {ph}"
        params.append(since)
    with get_connection() as conn:
        return _rows(conn, sql + " ORDER BY event_date, id", tuple(params))


# --------------------------------------------------------------------------- #
# Theme context（a-stock-daily-market-sense 主线生命周期，只读）
# --------------------------------------------------------------------------- #
def theme_context(asof: str) -> dict:
    """Latest theme_daily_state rows on or before *asof*, for theme tagging."""
    with get_connection() as conn:
        if not (table_exists(conn, "theme_daily_state") and table_exists(conn, "theme_registry")):
            return {"available": False, "reason": "theme_daily_state 表不存在（daily-market-sense 未跑过主线生命周期）"}
        ph = placeholder()
        latest = _rows(conn, f"SELECT MAX(trade_date) AS d FROM theme_daily_state WHERE trade_date <= {ph}", (asof,))
        day = latest[0]["d"] if latest else None
        if not day:
            return {"available": False, "reason": f"{asof} 及之前没有主线生命周期记录"}
        day = _iso(day)
        rows = _rows(conn,
                     "SELECT s.theme_id, r.name, r.aliases, s.raw_theme_name, s.stars, s.state, s.state_prev, "
                     "s.position, s.crowding, s.members_sample FROM theme_daily_state s "
                     f"JOIN theme_registry r ON r.theme_id = s.theme_id WHERE s.trade_date = {ph} "
                     "ORDER BY s.stars DESC NULLS LAST, s.theme_id" if BACKEND == Backend.POSTGRESQL else
                     "SELECT s.theme_id, r.name, r.aliases, s.raw_theme_name, s.stars, s.state, s.state_prev, "
                     "s.position, s.crowding, s.members_sample FROM theme_daily_state s "
                     f"JOIN theme_registry r ON r.theme_id = s.theme_id WHERE s.trade_date = {ph} "
                     "ORDER BY s.stars DESC, s.theme_id",
                     (day,))
    for row in rows:
        for key in ("aliases", "members_sample"):
            if isinstance(row.get(key), str):
                try:
                    row[key] = json.loads(row[key])
                except ValueError:
                    pass
    return {"available": True, "asof": day, "stale": day != asof, "themes": rows}
