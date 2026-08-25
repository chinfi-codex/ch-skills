"""GPU 算力监控的持久化层 —— 建表、幂等 upsert、按窗口读回。

只做存取，不做任何领域判断。所有指标计算在 metrics.py，所有解读在模型。

幂等键刻意用 obs_date（观测日）而不是 observed_at（观测时刻）：
同一天重跑采集必须覆盖当天那一行，而不是追加第二行。用时刻做键
等于"每次重试都新增一条"，几天下来同一天会堆出好几个互相矛盾的
价格点，7D/30D 变化率就全错了。

环境变量（来自 shared/data/db_core.py 的统一契约）：
    ALPHA_DB_BACKEND=postgresql|sqlite
    ALPHA_PG_URL=postgresql://...
"""

from __future__ import annotations

import json
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

PRICE_TABLE = "gpu_price_observations"
SUPPLY_TABLE = "gpu_supply_observations"
RUN_TABLE = "gpu_collect_runs"

_IS_PG = BACKEND is Backend.POSTGRESQL
_JSON_TYPE = "jsonb" if _IS_PG else "text"
_TS_TYPE = "timestamptz" if _IS_PG else "text"

SCHEMA = [
    f"""
    CREATE TABLE IF NOT EXISTS {PRICE_TABLE} (
        obs_date            date        NOT NULL,
        source              text        NOT NULL,
        gpu_model           text        NOT NULL,
        price_type          text        NOT NULL,
        market_segment      text        NOT NULL DEFAULT 'default',
        region              text        NOT NULL DEFAULT 'global',
        observed_at         {_TS_TYPE},
        price_usd_gpu_hour  numeric,
        sample_count        integer,
        node_gpu_count      integer,
        unit_basis          text,
        price_scope         text,
        query_fingerprint   text,
        raw_ref             text,
        quality_flag        text        NOT NULL DEFAULT 'ok',
        PRIMARY KEY (obs_date, source, gpu_model, price_type, market_segment, region)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SUPPLY_TABLE} (
        obs_date                date    NOT NULL,
        source                  text    NOT NULL,
        gpu_model               text    NOT NULL,
        market_segment          text    NOT NULL DEFAULT 'default',
        observed_at             {_TS_TYPE},
        offer_count             integer,
        available_gpu_count     integer,
        stock_status            text,
        available_region_count  integer,
        source_total_offer_count integer,
        offer_share             numeric,
        capacity_detail         {_JSON_TYPE},
        query_fingerprint       text,
        raw_ref                 text,
        quality_flag            text    NOT NULL DEFAULT 'ok',
        PRIMARY KEY (obs_date, source, gpu_model, market_segment)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {RUN_TABLE} (
        run_id          text    NOT NULL,
        source          text    NOT NULL,
        obs_date        date,
        started_at      {_TS_TYPE},
        finished_at     {_TS_TYPE},
        status          text    NOT NULL,
        attempts        integer,
        latency_ms      integer,
        price_rows      integer,
        supply_rows     integer,
        unmapped_ids    {_JSON_TYPE},
        error           text,
        raw_path        text,
        PRIMARY KEY (run_id, source)
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_gpu_price_model_date ON {PRICE_TABLE} (gpu_model, obs_date)",
    f"CREATE INDEX IF NOT EXISTS idx_gpu_supply_model_date ON {SUPPLY_TABLE} (gpu_model, obs_date)",
    f"CREATE INDEX IF NOT EXISTS idx_gpu_runs_date ON {RUN_TABLE} (obs_date)",
]

PRICE_COLUMNS = [
    "obs_date", "source", "gpu_model", "price_type", "market_segment", "region",
    "observed_at", "price_usd_gpu_hour", "sample_count", "node_gpu_count",
    "unit_basis", "price_scope", "query_fingerprint", "raw_ref", "quality_flag",
]
PRICE_KEY = ["obs_date", "source", "gpu_model", "price_type", "market_segment", "region"]

SUPPLY_COLUMNS = [
    "obs_date", "source", "gpu_model", "market_segment", "observed_at",
    "offer_count", "available_gpu_count", "stock_status", "available_region_count",
    "source_total_offer_count", "offer_share", "capacity_detail",
    "query_fingerprint", "raw_ref", "quality_flag",
]
SUPPLY_KEY = ["obs_date", "source", "gpu_model", "market_segment"]

RUN_COLUMNS = [
    "run_id", "source", "obs_date", "started_at", "finished_at", "status",
    "attempts", "latency_ms", "price_rows", "supply_rows", "unmapped_ids",
    "error", "raw_path",
]
RUN_KEY = ["run_id", "source"]

_JSON_COLUMNS = {"capacity_detail", "unmapped_ids"}


def init_schema() -> None:
    """建表。重复调用安全。"""
    with get_connection() as conn:
        cur = conn.cursor()
        for stmt in SCHEMA:
            cur.execute(adapt_sql(stmt))
        conn.commit()


def _encode(column: str, value: Any) -> Any:
    if column in _JSON_COLUMNS and value is not None and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return value


def _upsert(table: str, columns: List[str], keys: List[str], rows: Iterable[Dict[str, Any]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    updatable = [c for c in columns if c not in keys]
    placeholders = ", ".join(["?"] * len(columns))
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
    sql = adapt_sql(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {assignments}"
    )
    payload = [tuple(_encode(c, r.get(c)) for c in columns) for r in rows]
    with get_connection() as conn:
        cur = conn.cursor()
        cur.executemany(sql, payload)
        conn.commit()
    return len(payload)


def save_prices(rows: Iterable[Dict[str, Any]]) -> int:
    return _upsert(PRICE_TABLE, PRICE_COLUMNS, PRICE_KEY, rows)


def save_supply(rows: Iterable[Dict[str, Any]]) -> int:
    return _upsert(SUPPLY_TABLE, SUPPLY_COLUMNS, SUPPLY_KEY, rows)


def save_run(row: Dict[str, Any]) -> int:
    return _upsert(RUN_TABLE, RUN_COLUMNS, RUN_KEY, [row])


def _fetch(sql: str, params: tuple) -> List[Dict[str, Any]]:
    """按 cursor.description 组装 dict —— psycopg2 默认游标回的是元组，
    sqlite3 回的是 Row，这样两个后端拿到的形状一致。"""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(adapt_sql(sql), params)
        rows = cur.fetchall()
        columns = [c[0] for c in (cur.description or [])]
        out: List[Dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append(dict(row))
            else:
                out.append(dict(zip(columns, row)))
        return out


def read_prices(start_date: str, end_date: str,
                gpu_models: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    sql = (f"SELECT * FROM {PRICE_TABLE} WHERE obs_date >= ? AND obs_date <= ?")
    params: List[Any] = [start_date, end_date]
    if gpu_models:
        sql += " AND gpu_model IN (" + ", ".join(["?"] * len(gpu_models)) + ")"
        params.extend(gpu_models)
    sql += " ORDER BY obs_date, gpu_model, source, price_type"
    return _fetch(sql, tuple(params))


def read_supply(start_date: str, end_date: str,
                gpu_models: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    sql = (f"SELECT * FROM {SUPPLY_TABLE} WHERE obs_date >= ? AND obs_date <= ?")
    params: List[Any] = [start_date, end_date]
    if gpu_models:
        sql += " AND gpu_model IN (" + ", ".join(["?"] * len(gpu_models)) + ")"
        params.extend(gpu_models)
    sql += " ORDER BY obs_date, gpu_model, source"
    return _fetch(sql, tuple(params))


def read_runs(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    sql = (f"SELECT * FROM {RUN_TABLE} WHERE obs_date >= ? AND obs_date <= ? "
           "ORDER BY obs_date DESC, source")
    return _fetch(sql, (start_date, end_date))


def latest_run_per_source() -> List[Dict[str, Any]]:
    """每个源最后一次采集的状态与时间，用于数据源健康度面板。"""
    if _IS_PG:
        sql = (f"SELECT DISTINCT ON (source) * FROM {RUN_TABLE} "
               "ORDER BY source, obs_date DESC, finished_at DESC")
        return _fetch(sql, ())
    rows = _fetch(f"SELECT * FROM {RUN_TABLE} ORDER BY obs_date DESC, finished_at DESC", ())
    seen: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        seen.setdefault(r["source"], r)
    return list(seen.values())


if __name__ == "__main__":
    init_schema()
    print(json.dumps({"ok": True, "backend": BACKEND.value,
                      "tables": [PRICE_TABLE, SUPPLY_TABLE, RUN_TABLE]}, ensure_ascii=False))
