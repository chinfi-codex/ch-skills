#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据中心信用监控的持久化层 —— 建表、幂等 upsert、按窗口读回。

只做存取，不做任何领域判断。指标计算在 metrics.py / ladder.py / attribution.py，
解读全部在模型。

两个 schema 决定写在这里，改之前先想清楚：

1. **观测是长表，不是宽表。** 四个体制（公开债 / 转债 / GPU 抵押 / SPV）的指标集
   是互斥的：有日频价格的没有抵押品字段，有抵押品字段的没有日频价格。压进一张
   宽表会有 60%+ 的列是永久 NULL。所以统一的是「一条观测的形状」而不是列。

2. **幂等键用 asof_date 不用 obs_date。** asof_date 是数据自身的口径日（持仓文件
   的 As of、报告期末），obs_date 是采集日。同一天重跑必须覆盖那一行而不是追加。
   两个日期都要存——staleness_days 是它们的差，也是面板上判断「这个数还新不新」
   的唯一依据。

环境变量（来自 shared/data/db_core.py 的统一契约）：
    ALPHA_DB_BACKEND=postgresql|sqlite
    ALPHA_PG_URL=postgresql://...
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[1] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

from db_core import (  # noqa: E402
    BACKEND,
    Backend,
    adapt_sql,
    get_connection,

)

INSTRUMENT_TABLE = "dc_instruments"
OBSERVATION_TABLE = "dc_observations"
RUN_TABLE = "dc_collect_runs"
EVENT_TABLE = "dc_events"

_IS_PG = BACKEND is Backend.POSTGRESQL
_TS_TYPE = "timestamptz" if _IS_PG else "text"

SCHEMA = [
    f"""
    CREATE TABLE IF NOT EXISTS {INSTRUMENT_TABLE} (
        instrument_key      text    NOT NULL,
        isin                text,
        issuer_key          text    NOT NULL,
        issuer_parent_key   text    NOT NULL,
        rung                integer,
        regime              text    NOT NULL,
        instrument_type     text    NOT NULL,
        coupon              numeric,
        coupon_type         text,
        maturity            date,
        currency            text    NOT NULL DEFAULT 'USD',
        segment             text,
        is_144a             boolean NOT NULL DEFAULT false,
        has_embedded_option boolean NOT NULL DEFAULT false,
        recourse            text,
        collateral_type     text,
        display_name        text,
        first_seen          date    NOT NULL,
        last_seen           date    NOT NULL,
        PRIMARY KEY (instrument_key)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {OBSERVATION_TABLE} (
        asof_date       date    NOT NULL,
        instrument_key  text    NOT NULL,
        metric          text    NOT NULL,
        value           numeric,
        value_text      text,
        unit            text    NOT NULL,
        method          text    NOT NULL,
        source_id       text    NOT NULL,
        obs_date        date    NOT NULL,
        staleness_days  integer NOT NULL DEFAULT 0,
        quality         text    NOT NULL DEFAULT 'ok',
        raw_ref         text,
        PRIMARY KEY (asof_date, instrument_key, metric)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {RUN_TABLE} (
        run_id          text    NOT NULL,
        source_id       text    NOT NULL,
        obs_date        date,
        started_at      {_TS_TYPE},
        ended_at        {_TS_TYPE},
        status          text,
        rows_written    integer,
        basket_fingerprint text,
        note            text,
        PRIMARY KEY (run_id, source_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {EVENT_TABLE} (
        asof_date   date    NOT NULL,
        subject     text    NOT NULL,
        rule_id     text    NOT NULL,
        criterion   text,
        observed    numeric,
        threshold   numeric,
        unit        text,
        detail      text,
        mode        text    NOT NULL DEFAULT 'record_only',
        PRIMARY KEY (asof_date, subject, rule_id)
    )
    """,
]

_IDX = [
    f"CREATE INDEX IF NOT EXISTS idx_dc_obs_metric ON {OBSERVATION_TABLE} (metric, asof_date)",
    f"CREATE INDEX IF NOT EXISTS idx_dc_obs_key ON {OBSERVATION_TABLE} (instrument_key, metric)",
    f"CREATE INDEX IF NOT EXISTS idx_dc_inst_issuer ON {INSTRUMENT_TABLE} (issuer_parent_key)",
]


def init_schema() -> None:
    with get_connection() as conn:
        cur = conn.cursor()
        for stmt in SCHEMA + _IDX:
            cur.execute(adapt_sql(stmt))
        conn.commit()


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
_INSTRUMENT_COLS = [
    "instrument_key", "isin", "issuer_key", "issuer_parent_key", "rung", "regime",
    "instrument_type", "coupon", "coupon_type", "maturity", "currency", "segment",
    "is_144a", "has_embedded_option", "recourse", "collateral_type", "display_name",
    "first_seen", "last_seen",
]

_OBSERVATION_COLS = [
    "asof_date", "instrument_key", "metric", "value", "value_text", "unit",
    "method", "source_id", "obs_date", "staleness_days", "quality", "raw_ref",
]

_EVENT_COLS = ["asof_date", "subject", "rule_id", "criterion", "observed",
               "threshold", "unit", "detail", "mode"]


def _upsert(table: str, cols: List[str], keys: List[str],
            rows: Iterable[Dict[str, Any]], *, preserve: Optional[List[str]] = None) -> int:
    rows = list(rows)
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(cols))
    updates = [c for c in cols if c not in keys and c not in (preserve or [])]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in updates)
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {set_clause}")
    if preserve:
        # first_seen 这类字段只在插入时写，冲突时保留库里更早的那个值。
        keep = ", ".join(f"{c} = LEAST({table}.{c}, EXCLUDED.{c})" if _IS_PG
                         else f"{c} = MIN({table}.{c}, EXCLUDED.{c})" for c in preserve)
        sql += f", {keep}"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.executemany(adapt_sql(sql), [[r.get(c) for c in cols] for r in rows])
        conn.commit()
    return len(rows)


def upsert_instruments(rows: Iterable[Dict[str, Any]]) -> int:
    return _upsert(INSTRUMENT_TABLE, _INSTRUMENT_COLS, ["instrument_key"], rows,
                   preserve=["first_seen"])


def upsert_observations(rows: Iterable[Dict[str, Any]]) -> int:
    return _upsert(OBSERVATION_TABLE, _OBSERVATION_COLS,
                   ["asof_date", "instrument_key", "metric"], rows)


def upsert_events(rows: Iterable[Dict[str, Any]]) -> int:
    return _upsert(EVENT_TABLE, _EVENT_COLS, ["asof_date", "subject", "rule_id"], rows)


def record_run(run: Dict[str, Any]) -> None:
    _upsert(RUN_TABLE,
            ["run_id", "source_id", "obs_date", "started_at", "ended_at",
             "status", "rows_written", "basket_fingerprint", "note"],
            ["run_id", "source_id"], [run])


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def _query(sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    """按 cursor.description 组装 dict —— psycopg2 默认游标回的是元组，
    sqlite3 回的是 Row，这样两个后端拿到的形状一致。"""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(adapt_sql(sql), list(params))
        rows = cur.fetchall()
        columns = [c[0] for c in (cur.description or [])]
        out: List[Dict[str, Any]] = []
        for row in rows:
            out.append(dict(row) if isinstance(row, dict) else dict(zip(columns, row)))
        return out


def load_instruments(issuer_parent_keys: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if issuer_parent_keys:
        marks = ", ".join(["?"] * len(issuer_parent_keys))
        return _query(f"SELECT * FROM {INSTRUMENT_TABLE} "
                      f"WHERE issuer_parent_key IN ({marks})", issuer_parent_keys)
    return _query(f"SELECT * FROM {INSTRUMENT_TABLE}")


def load_observations(metric: str, since: str,
                      instrument_keys: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    sql = (f"SELECT * FROM {OBSERVATION_TABLE} WHERE metric = ? AND asof_date >= ?")
    params: List[Any] = [metric, since]
    if instrument_keys:
        marks = ", ".join(["?"] * len(instrument_keys))
        sql += f" AND instrument_key IN ({marks})"
        params.extend(instrument_keys)
    return _query(sql + " ORDER BY asof_date", params)


def load_metric_prefix(prefix: str, since: str) -> List[Dict[str, Any]]:
    return _query(
        f"SELECT * FROM {OBSERVATION_TABLE} WHERE metric LIKE ? AND asof_date >= ? "
        f"ORDER BY asof_date", [prefix + "%", since])


def latest_asof(metric: str = "yld.gspread_bp") -> Optional[str]:
    rows = _query(f"SELECT MAX(asof_date) AS d FROM {OBSERVATION_TABLE} WHERE metric = ?",
                  [metric])
    value = rows[0]["d"] if rows else None
    return str(value) if value else None


def price_history(instrument_key: str, limit: int = 10) -> List[Dict[str, Any]]:
    """给 stale 判定用：某只债最近 N 个价格点，按日期倒序。"""
    return _query(
        f"SELECT asof_date, value FROM {OBSERVATION_TABLE} "
        f"WHERE instrument_key = ? AND metric = 'px.clean' "
        f"ORDER BY asof_date DESC LIMIT {int(limit)}", [instrument_key])


def runs_for(run_id: str) -> List[Dict[str, Any]]:
    return _query(f"SELECT * FROM {RUN_TABLE} WHERE run_id = ?", [run_id])


if __name__ == "__main__":
    init_schema()
    print("schema ready:", INSTRUMENT_TABLE, OBSERVATION_TABLE, RUN_TABLE, EVENT_TABLE)
