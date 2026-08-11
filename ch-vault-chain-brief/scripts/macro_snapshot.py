#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
宏观快照提取器（PostgreSQL 侧的"手"）。

只做取数与确定性计算：核心指标的区间序列、首尾变动、区间高低点，
以及机械可判的数据健康标记（卡值 / 空值 / 缺日 / 中国宏观滞后）。
**不做任何解读、归因或文字结论**——那些交给模型。

读取的表（alpha_data）：
  macro_daily_snapshots  金十等来源采集入库的每日宏观快照（jsonb）

用法：
  python scripts/macro_snapshot.py --start 2026-07-31 --end 2026-08-07
  python scripts/macro_snapshot.py --end 2026-08-07 --days 5
  python scripts/macro_snapshot.py --end 2026-08-07 --days 2 --indent 2

连接串走仓库共享 db_core：环境变量 ALPHA_PG_URL（或 DATABASE_URL）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"


def _dev_shared() -> Path:
    """开发态：向上找到仓库根（含 skill-sync.yaml），用 shared/data。"""
    for parent in _SCRIPT_DIR.parents:
        if (parent / "skill-sync.yaml").exists():
            return parent / "shared" / "data"
    return _SCRIPT_DIR.parents[1] / "shared" / "data"


sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _dev_shared()))
from db_core import get_connection  # noqa: E402

# 报告只用得上这几个核心指标；顺序即报告里建议的呈现顺序
MACRO_SCALARS = ("BRENT", "GOLD", "US_TREASURY_10Y", "USD_CNY", "WTI", "NATURAL_GAS", "BTC")


def _rows_to_dicts(cur) -> List[Dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _stale_runs(series: List[tuple[str, Any]], min_run: int) -> List[Dict[str, Any]]:
    """找出连续重复同一数值的区段，用于识别源端卡值。"""
    runs: List[Dict[str, Any]] = []
    run_start = 0
    for i in range(1, len(series) + 1):
        same = i < len(series) and series[i][1] == series[run_start][1] and series[i][1] is not None
        if same:
            continue
        length = i - run_start
        if length >= min_run and series[run_start][1] is not None:
            runs.append(
                {
                    "value": series[run_start][1],
                    "from": series[run_start][0],
                    "to": series[i - 1][0],
                    "days": length,
                }
            )
        run_start = i
    return runs


def extract_macro(conn, start: str, end: str, stale_days: int) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot_date, fetched_at, sources, data FROM macro_daily_snapshots "
            "WHERE snapshot_date BETWEEN %s AND %s ORDER BY snapshot_date",
            (start, end),
        )
        rows = _rows_to_dicts(cur)

    if not rows:
        return {"available": False, "reason": "no macro_daily_snapshots in window"}

    series: List[Dict[str, Any]] = []
    for row in rows:
        data = row["data"] or {}
        entry: Dict[str, Any] = {"date": str(row["snapshot_date"]), "sources": row["sources"]}
        for key in MACRO_SCALARS:
            entry[key] = data.get(key)
        entry["CHINA_MACRO"] = data.get("CHINA_MACRO")
        series.append(entry)

    changes: Dict[str, Any] = {}
    for key in MACRO_SCALARS:
        points = [(e["date"], e[key]) for e in series if e[key] is not None]
        if len(points) < 2:
            changes[key] = {"available": False}
            continue
        f, l = float(points[0][1]), float(points[-1][1])
        changes[key] = {
            "first_date": points[0][0],
            "last_date": points[-1][0],
            "first": f,
            "last": l,
            "abs_change": round(l - f, 4),
            "pct_change": round((l / f - 1) * 100, 2) if f else None,
            "min": min(float(p[1]) for p in points),
            "max": max(float(p[1]) for p in points),
        }

    # ---- 数据健康：卡值 / 空值 / 缺日 / 中国宏观滞后 ----
    health: Dict[str, Any] = {"stale_fields": {}, "null_in_latest": [], "missing_dates": []}
    for key in MACRO_SCALARS:
        runs = _stale_runs([(e["date"], e[key]) for e in series], stale_days)
        if runs:
            health["stale_fields"][key] = runs

    latest = series[-1]
    for key in MACRO_SCALARS:
        if latest.get(key) is None:
            health["null_in_latest"].append(key)

    have = {e["date"] for e in series}
    cursor = datetime.strptime(start, "%Y-%m-%d").date()
    stop = datetime.strptime(end, "%Y-%m-%d").date()
    while cursor <= stop:
        if cursor.isoformat() not in have:
            health["missing_dates"].append(cursor.isoformat())
        cursor += timedelta(days=1)

    china = latest.get("CHINA_MACRO") or {}
    health["china_macro_latest"] = {
        name: {
            "date": (payload or {}).get("date"),
            "yoy": (payload or {}).get("yoy"),
            "value": (payload or {}).get("value"),
        }
        for name, payload in china.items()
    }

    return {
        "available": True,
        "count": len(series),
        "latest_date": latest["date"],
        "latest_sources": latest["sources"],
        "series": series,
        "changes": changes,
        "data_health": health,
    }


def resolve_window(args: argparse.Namespace) -> tuple[str, str]:
    end = args.end or date.today().isoformat()
    if args.start:
        return args.start, end
    stop = datetime.strptime(end, "%Y-%m-%d").date()
    if args.days:
        return (stop - timedelta(days=args.days - 1)).isoformat(), end
    return (stop - timedelta(days=stop.weekday() + 1)).isoformat(), end


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="提取宏观核心指标快照（PG）")
    parser.add_argument("--start", help="窗口起始日 YYYY-MM-DD；变动幅度以该日为基准")
    parser.add_argument("--end", help="窗口结束日 YYYY-MM-DD，默认今天")
    parser.add_argument("--days", type=int, help="窗口天数（含结束日），与 --start 互斥")
    parser.add_argument("--stale-days", type=int, default=3, help="连续几日同值判为源端卡值")
    parser.add_argument("--indent", type=int, default=None, help="JSON 缩进")
    args = parser.parse_args(list(argv) if argv is not None else None)

    start, end = resolve_window(args)
    out: Dict[str, Any] = {
        "window": {"start": start, "end": end},
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    try:
        with get_connection() as conn:
            out["macro"] = extract_macro(conn, start, end, args.stale_days)
    except Exception as exc:  # noqa: BLE001 —— 连接/查询失败要让模型看到降级原因
        json.dump(
            {"error": "DB_UNAVAILABLE", "detail": str(exc)[:400], **out},
            sys.stdout,
            ensure_ascii=False,
        )
        print()
        return 3

    json.dump(out, sys.stdout, ensure_ascii=False, indent=args.indent, default=str)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
