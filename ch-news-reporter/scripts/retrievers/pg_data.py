#!/usr/bin/env python3
"""pg_data 通道:alpha_data 结构化表白名单查询。

只执行白名单内的固定查询:配置键 → 表/键列/日期列的映射写死在本文件,
配置里出现映射以外的键名一律忽略并记 warning;不接受、不拼接任何自定义
SQL 或表名。所有取值都走参数绑定。

仅 PostgreSQL backend 可用(SQLite fallback 没有这些表);backend 不对或
表缺失时优雅降级:记 warning、返回空,不抛异常。
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR.parent))

from db_adapter import BACKEND, Backend, table_exists  # noqa: E402
from retrievers.common import ph, run_query  # noqa: E402

# 白名单:配置键 → (表, 键列, 日期列)。新增挂钩 = 在这里加一行映射。
PG_HOOKS: dict[str, dict[str, str]] = {
    "stock_tickers": {"table": "stock_daily", "key_col": "ts_code", "date_col": "trade_date"},
    "index_codes": {"table": "stock_index_daily", "key_col": "ts_code", "date_col": "trade_date"},
    "theme_ids": {"table": "theme_daily_state", "key_col": "theme_id", "date_col": "trade_date"},
    "tracking_tickers": {"table": "stock_tracking_history", "key_col": "ts_code", "date_col": "asof"},
}

# 标的/键值形态校验(防御纵深;实际取值走参数绑定,不参与 SQL 拼接)
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
PER_KEY_LIMIT = 20


def _window_start(date_key: str, days: int) -> str:
    day = datetime.strptime(date_key, "%Y-%m-%d")
    return (day - timedelta(days=max(1, days) - 1)).strftime("%Y-%m-%d")


def retrieve(con: Any, topic: dict[str, Any], date_key: str) -> dict[str, Any]:
    """按主题 channels.pg_data 配置查询结构化表;返回统一通道结构 + hooks 数据。"""
    raw = (topic.get("channels") or {}).get("pg_data")
    if not raw:
        return {"status": "skipped", "count": 0, "warnings": ["pg_data 通道未配置"], "hooks": {}}
    if not isinstance(raw, dict):
        return {
            "status": "skipped",
            "count": 0,
            "warnings": ["pg_data 配置必须是 map(如 {stock_tickers: [...]}),已跳过"],
            "hooks": {},
        }

    if BACKEND != Backend.POSTGRESQL:
        return {
            "status": "skipped",
            "count": 0,
            "warnings": ["pg_data 仅 PostgreSQL backend 可用(当前为 sqlite),已跳过"],
            "hooks": {},
        }

    days = int(topic.get("time_window_days") or 3)
    start = _window_start(date_key, days)
    warnings: list[str] = []
    hooks: dict[str, Any] = {}
    total = 0

    for hook_name, values in raw.items():
        spec = PG_HOOKS.get(str(hook_name))
        if spec is None:
            warnings.append(f"未识别的 pg_data 挂钩 {hook_name!r},已忽略(白名单: {sorted(PG_HOOKS)})")
            continue
        if not isinstance(values, list) or not values:
            continue
        keys = [str(v).strip() for v in values if str(v).strip()]
        bad = [k for k in keys if not _KEY_RE.match(k)]
        for k in bad:
            warnings.append(f"pg_data.{hook_name} 含非法键值 {k!r},已跳过该值")
        keys = [k for k in keys if _KEY_RE.match(k)]
        if not keys:
            continue

        table = spec["table"]
        if not table_exists(con, table):
            warnings.append(f"alpha_data 表 {table} 不存在,挂钩 {hook_name} 跳过")
            continue

        p = ph()
        sql = (
            f"SELECT * FROM {table} WHERE {spec['key_col']} = {p} "
            f"AND {spec['date_col']} BETWEEN {p} AND {p} "
            f"ORDER BY {spec['date_col']} DESC LIMIT {p}"
        )
        per_key: dict[str, list[dict[str, Any]]] = {}
        for key in keys:
            rows = run_query(con, sql, [key, start, date_key, PER_KEY_LIMIT])
            per_key[key] = rows
            total += len(rows)
        hooks[hook_name] = {"table": table, "keys": per_key}

    empty_hooks = [name for name, payload in hooks.items() if not any(payload["keys"].values())]
    for name in empty_hooks:
        warnings.append(f"pg_data.{name} 在时间窗 {start} ~ {date_key} 内无数据行")

    return {
        "status": "ok",
        "count": total,
        "warnings": warnings,
        "hooks": hooks,
        "window": {"start": start, "end": date_key, "days": days},
    }
