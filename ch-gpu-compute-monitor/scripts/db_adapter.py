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
ALERT_TABLE = "gpu_alerts"
TOKEN_TABLE = "token_model_observations"
TOKEN_HISTORY_TABLE = "token_volume_history"
TOKEN_APP_TABLE = "token_app_observations"

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
        token_rows      integer,
        unmapped_ids    {_JSON_TYPE},
        error           text,
        raw_path        text,
        PRIMARY KEY (run_id, source)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {ALERT_TABLE} (
        obs_date        date    NOT NULL,
        gpu_model       text    NOT NULL,
        rule_id         text    NOT NULL,
        label           text,
        direction       text,
        metric          text,
        observed        numeric,
        threshold       numeric,
        op              text,
        meaning         text,
        mode            text,
        fired_at        {_TS_TYPE},
        PRIMARY KEY (obs_date, gpu_model, rule_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {TOKEN_TABLE} (
        obs_date                        date    NOT NULL,
        source                          text    NOT NULL,
        model_family                    text    NOT NULL,
        model_slug                      text    NOT NULL,
        variant                         text    NOT NULL,
        coverage_scope                  text    NOT NULL DEFAULT 'gateway',
        price_basis                     text    NOT NULL DEFAULT 'list',
        observed_at                     {_TS_TYPE},
        prompt_tokens                   bigint,
        completion_tokens               bigint,
        requests                        bigint,
        price_prompt_usd_per_mtok       numeric,
        price_completion_usd_per_mtok   numeric,
        price_cache_read_usd_per_mtok   numeric,
        spend_usd                       numeric,
        is_priced                       boolean,
        price_match                     text,
        provider_price_min_usd_per_mtok numeric,
        provider_price_median_usd_per_mtok numeric,
        provider_price_max_usd_per_mtok numeric,
        provider_count                  integer,
        query_fingerprint               text,
        raw_ref                         text,
        quality_flag                    text    NOT NULL DEFAULT 'ok',
        PRIMARY KEY (obs_date, source, model_slug, variant)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {TOKEN_HISTORY_TABLE} (
        week_start      date    NOT NULL,
        source          text    NOT NULL,
        author          text    NOT NULL,
        observed_at     {_TS_TYPE},
        tokens          bigint,
        unit_basis      text,
        coverage_scope  text    NOT NULL DEFAULT 'gateway',
        grain           text    NOT NULL DEFAULT 'author_weekly',
        settled         boolean NOT NULL DEFAULT true,
        query_fingerprint text,
        raw_ref         text,
        quality_flag    text    NOT NULL DEFAULT 'ok',
        PRIMARY KEY (week_start, source, author)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {TOKEN_APP_TABLE} (
        obs_date        date    NOT NULL,
        source          text    NOT NULL,
        app_id          text    NOT NULL,
        observed_at     {_TS_TYPE},
        app_slug        text,
        app_title       text,
        app_url         text,
        categories      {_JSON_TYPE},
        rank            integer,
        total_tokens    bigint,
        total_requests  bigint,
        coverage_scope  text    NOT NULL DEFAULT 'gateway',
        listing_scope   text    NOT NULL DEFAULT 'public_ranked',
        query_fingerprint text,
        raw_ref         text,
        quality_flag    text    NOT NULL DEFAULT 'ok',
        PRIMARY KEY (obs_date, source, app_id)
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_token_app_date ON {TOKEN_APP_TABLE} (obs_date)",
    f"CREATE INDEX IF NOT EXISTS idx_gpu_price_model_date ON {PRICE_TABLE} (gpu_model, obs_date)",
    f"CREATE INDEX IF NOT EXISTS idx_token_hist_week ON {TOKEN_HISTORY_TABLE} (week_start)",
    f"CREATE INDEX IF NOT EXISTS idx_token_obs_date ON {TOKEN_TABLE} (obs_date)",
    f"CREATE INDEX IF NOT EXISTS idx_token_family_date ON {TOKEN_TABLE} (model_family, obs_date)",
    f"CREATE INDEX IF NOT EXISTS idx_gpu_supply_model_date ON {SUPPLY_TABLE} (gpu_model, obs_date)",
    f"CREATE INDEX IF NOT EXISTS idx_gpu_runs_date ON {RUN_TABLE} (obs_date)",
    f"CREATE INDEX IF NOT EXISTS idx_gpu_alerts_model_date ON {ALERT_TABLE} (gpu_model, obs_date)",
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
    "attempts", "latency_ms", "price_rows", "supply_rows", "token_rows",
    "app_rows", "token_history_rows", "unmapped_ids", "error", "raw_path",
]
RUN_KEY = ["run_id", "source"]

ALERT_COLUMNS = [
    "obs_date", "gpu_model", "rule_id", "label", "direction", "metric",
    "observed", "threshold", "op", "meaning", "mode", "fired_at",
]
ALERT_KEY = ["obs_date", "gpu_model", "rule_id"]

TOKEN_COLUMNS = [
    "obs_date", "source", "model_family", "model_slug", "variant",
    "coverage_scope", "price_basis", "observed_at",
    "prompt_tokens", "completion_tokens", "requests",
    "price_prompt_usd_per_mtok", "price_completion_usd_per_mtok",
    "price_cache_read_usd_per_mtok", "spend_usd", "is_priced", "price_match",
    "provider_price_min_usd_per_mtok", "provider_price_median_usd_per_mtok",
    "provider_price_max_usd_per_mtok", "provider_count",
    "query_fingerprint", "raw_ref", "quality_flag",
]
# 主键不含 model_family：它是从 model_slug 剥日期后缀推出来的派生列，
# 放进键里等于允许同一个 slug 在两个家族下各存一行。
TOKEN_KEY = ["obs_date", "source", "model_slug", "variant"]

TOKEN_HISTORY_COLUMNS = [
    "week_start", "source", "author", "observed_at", "tokens", "unit_basis",
    "coverage_scope", "grain", "settled", "query_fingerprint", "raw_ref",
    "quality_flag",
]
TOKEN_HISTORY_KEY = ["week_start", "source", "author"]

TOKEN_APP_COLUMNS = [
    "obs_date", "source", "app_id", "observed_at", "app_slug", "app_title",
    "app_url", "categories", "rank", "total_tokens", "total_requests",
    "coverage_scope", "listing_scope", "query_fingerprint", "raw_ref",
    "quality_flag",
]
# rank 不进主键：它是榜单当天给的名次，同一个应用换名次不该多出一行。
TOKEN_APP_KEY = ["obs_date", "source", "app_id"]

_JSON_COLUMNS = {"capacity_detail", "unmapped_ids", "categories"}


# 后加的列。CREATE TABLE IF NOT EXISTS 对已经建好的表是空操作，所以
# 升级到带 token 维度的版本时，老库里的 gpu_collect_runs 不会自己长出
# token_rows —— 那样 upsert 会直接报 column 不存在。这里按后端各自的方式
# 查一遍现有列，缺哪个补哪个。ADD COLUMN IF NOT EXISTS 只有 PG 支持，
# SQLite 没有，所以不能靠它。
ADDED_COLUMNS = {
    RUN_TABLE: {"token_rows": "integer", "app_rows": "integer",
                "token_history_rows": "integer"},
}


def _existing_columns(cur, table: str) -> set:
    if _IS_PG:
        cur.execute(adapt_sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?"),
            (table,))
        return {r[0] if not isinstance(r, dict) else r["column_name"]
                for r in cur.fetchall()}
    cur.execute(f"PRAGMA table_info({table})")
    return {r[1] if not isinstance(r, dict) else r["name"] for r in cur.fetchall()}


def init_schema() -> None:
    """建表 + 补列。重复调用安全。"""
    with get_connection() as conn:
        cur = conn.cursor()
        for stmt in SCHEMA:
            cur.execute(adapt_sql(stmt))
        for table, columns in ADDED_COLUMNS.items():
            present = _existing_columns(cur, table)
            for name, ddl in columns.items():
                if name not in present:
                    cur.execute(adapt_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
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


def save_tokens(rows: Iterable[Dict[str, Any]]) -> int:
    """推理 token 的量价观测。

    幂等键 (obs_date, source, model_slug, variant)：variant 必须在键里——
    :batch 是折扣档、:free 是零价档，合并进标准档会让同一批 token 按错价计。
    """
    return _upsert(TOKEN_TABLE, TOKEN_COLUMNS, TOKEN_KEY, rows)


def save_token_history(rows: Iterable[Dict[str, Any]]) -> int:
    """周度厂商级历史量。单独一张表是刻意的——它与日度模型级观测口径不同，
    放同一张表迟早会有人把两者 union 起来当一条序列用。"""
    return _upsert(TOKEN_HISTORY_TABLE, TOKEN_HISTORY_COLUMNS, TOKEN_HISTORY_KEY, rows)


def save_token_apps(rows: Iterable[Dict[str, Any]]) -> int:
    """调用方（应用）维度的日度量。

    又是单独一张表，理由和周度历史一样：粒度不同、字段不同、可比范围不同。
    应用榜只有 token 与请求数，没有模型拆分也没有价，跟 token_model_observations
    没有任何可 join 的键——放同一张表迟早会有人把两边的 token 加起来。
    """
    return _upsert(TOKEN_APP_TABLE, TOKEN_APP_COLUMNS, TOKEN_APP_KEY, rows)


def read_token_apps(start_date: str, end_date: str,
                    sources: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    sql = f"SELECT * FROM {TOKEN_APP_TABLE} WHERE obs_date >= ? AND obs_date <= ?"
    params: List[Any] = [start_date, end_date]
    if sources:
        sql += " AND source IN (" + ", ".join(["?"] * len(sources)) + ")"
        params.extend(sources)
    sql += " ORDER BY obs_date, source, rank, app_id"
    return _fetch(sql, tuple(params))


def read_token_history(start_week: str, end_week: str,
                       sources: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    sql = (f"SELECT * FROM {TOKEN_HISTORY_TABLE} "
           "WHERE week_start >= ? AND week_start <= ?")
    params: List[Any] = [start_week, end_week]
    if sources:
        sql += " AND source IN (" + ", ".join(["?"] * len(sources)) + ")"
        params.extend(sources)
    sql += " ORDER BY week_start, source, author"
    return _fetch(sql, tuple(params))


def save_run(row: Dict[str, Any]) -> int:
    return _upsert(RUN_TABLE, RUN_COLUMNS, RUN_KEY, [row])


def save_alerts(rows: Iterable[Dict[str, Any]]) -> int:
    """告警落库（PRD §6.1 步骤 8）。

    幂等键是 (obs_date, gpu_model, rule_id)：同一天重跑覆盖，不追加。
    没有这张表，「确认型拐点」那条要求「连续 ≥10 个采集日」的规则就没有
    跨日依据——告警只活在当天的 evidence 里，重算一次就没了。
    """
    return _upsert(ALERT_TABLE, ALERT_COLUMNS, ALERT_KEY, rows)


def replace_alerts(obs_date: str, gpu_models: List[str],
                   rows: Iterable[Dict[str, Any]]) -> int:
    """在同一事务内替换指定日期/型号的告警，避免清空后写入失败留下空窗。"""
    rows = list(rows)
    if not gpu_models:
        return 0
    delete_sql = (f"DELETE FROM {ALERT_TABLE} WHERE obs_date = ? AND gpu_model IN ("
                  + ", ".join(["?"] * len(gpu_models)) + ")")
    delete_params: List[Any] = [obs_date, *gpu_models]
    updatable = [c for c in ALERT_COLUMNS if c not in ALERT_KEY]
    placeholders = ", ".join(["?"] * len(ALERT_COLUMNS))
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
    insert_sql = (
        f"INSERT INTO {ALERT_TABLE} ({', '.join(ALERT_COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(ALERT_KEY)}) DO UPDATE SET {assignments}"
    )
    payload = [tuple(_encode(c, row.get(c)) for c in ALERT_COLUMNS) for row in rows]
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(adapt_sql(delete_sql), tuple(delete_params))
        if payload:
            cur.executemany(adapt_sql(insert_sql), payload)
    return len(payload)


def clear_alerts(obs_date: str, gpu_models: Optional[List[str]] = None) -> int:
    """清掉某天的旧告警，再写新的——否则规则改阈值后，昨天触发过、
    今天不该触发的那条会永远留在库里。"""
    sql = f"DELETE FROM {ALERT_TABLE} WHERE obs_date = ?"
    params: List[Any] = [obs_date]
    if gpu_models:
        sql += " AND gpu_model IN (" + ", ".join(["?"] * len(gpu_models)) + ")"
        params.extend(gpu_models)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(adapt_sql(sql), tuple(params))
        conn.commit()
        return cur.rowcount


def read_alerts(start_date: str, end_date: str,
                gpu_models: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    sql = f"SELECT * FROM {ALERT_TABLE} WHERE obs_date >= ? AND obs_date <= ?"
    params: List[Any] = [start_date, end_date]
    if gpu_models:
        sql += " AND gpu_model IN (" + ", ".join(["?"] * len(gpu_models)) + ")"
        params.extend(gpu_models)
    sql += " ORDER BY obs_date, gpu_model, rule_id"
    return _fetch(sql, tuple(params))


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


def read_tokens(start_date: str, end_date: str,
                sources: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    sql = f"SELECT * FROM {TOKEN_TABLE} WHERE obs_date >= ? AND obs_date <= ?"
    params: List[Any] = [start_date, end_date]
    if sources:
        sql += " AND source IN (" + ", ".join(["?"] * len(sources)) + ")"
        params.extend(sources)
    sql += " ORDER BY obs_date, source, model_slug, variant"
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
                      "tables": [PRICE_TABLE, SUPPLY_TABLE, RUN_TABLE, ALERT_TABLE,
                                 TOKEN_TABLE, TOKEN_HISTORY_TABLE,
                                 TOKEN_APP_TABLE]},
                     ensure_ascii=False))
