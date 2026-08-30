#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日采集：SPDR 持仓 + FRED 基准 → 价格、收益率、利差；可选 SEC 三路。

逐源独立，单源失败不阻断其它源，成败与耗时写进 dc_collect_runs。

一条纪律贯穿全流程：**缺失显式**。国债曲线当天没有就不出利差（而不是拿上周的
曲线去减今天的收益率——那算出来的「利差变动」其实是利率变动）；价格连续多日
不动就标 stale（而不是当成「利差没变」）。

用法：
    python scripts/collect.py                    # 市场层（默认）
    python scripts/collect.py --with-sec         # 加基本面 + GPU 抵押 + SPV
    python scripts/collect.py --dry-run          # 不落库，只打摘要
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import db_adapter as db                                   # noqa: E402
import pricing                                            # noqa: E402
from collectors import fred, sec, spdr                    # noqa: E402
from collectors.base import load_config                   # noqa: E402


# 每次采集顺手写回的基准层天数。幂等 upsert，重叠是有意的：FRED 会回溯修订。
BENCH_PERSIST_DAYS = 30


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _stale_flag(instrument_key: str, price: float, days: int) -> bool:
    """价格连续 N 天不动 → 管理人没重估，不是市场没变。"""
    history = db.price_history(instrument_key, limit=days)
    if len(history) < days - 1:
        return False
    values = [float(r["value"]) for r in history if r.get("value") is not None]
    return bool(values) and all(abs(v - price) < 1e-9 for v in values[:days - 1])


def run(with_sec: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    run_id = uuid.uuid4().hex[:12]
    sources_cfg = load_config("sources.yaml")
    universe = load_config("universe.yaml")
    thresholds = load_config("thresholds.yaml")
    summary: Dict[str, Any] = {"run_id": run_id, "sources": {}, "asof": None}

    if not dry_run:
        db.init_schema()

    # --- 基准层：没有它，利差是绝对数，读不出走宽是谁的事 ---------------------
    started = _now()
    t0 = time.monotonic()
    try:
        bench = fred.fetch_benchmarks(sources_cfg)
        bench_status, bench_note = "ok", (
            f"国债曲线 {len(bench['curve'])} 天，指数 OAS {len(bench['index_oas_bp'])} 天，"
            f"最新 {bench['latest']}")
    except Exception as exc:                              # noqa: BLE001
        bench = {"curve": {}, "index_oas_bp": {}, "latest": None}
        bench_status, bench_note = "failed", f"FRED 取不到：{exc}"
    # 基准层要落库，不能只活在内存里。以前算完利差就扔，代价是判据 1 的市场 beta
    # 每次都得重打一次 FRED，锚点日的指数 OAS 也无处可查。写最近 30 天而不是全量：
    # 幂等 upsert 顺手吃掉 FRED 的回溯修订，又不用每天重写几百天。历史一次性回补
    # 走 scripts/backfill.py。
    bench_since = (dt.date.today() - dt.timedelta(days=BENCH_PERSIST_DAYS)).isoformat()
    bench_rows = fred.benchmark_rows(bench, since=bench_since) if bench["curve"] else []
    if bench_rows and not dry_run:
        db.upsert_observations(bench_rows)
    summary["sources"]["fred"] = {"status": bench_status, "note": bench_note,
                                  "persisted_rows": len(bench_rows),
                                  "seconds": round(time.monotonic() - t0, 1)}
    if not dry_run:
        db.record_run({"run_id": run_id, "source_id": "fred", "obs_date": None,
                       "started_at": started, "ended_at": _now(),
                       "status": bench_status, "rows_written": len(bench_rows),
                       "basket_fingerprint": None, "note": bench_note})

    # --- 市场层：SPDR 持仓 → 价格 → 收益率 → 利差 ---------------------------
    started = _now()
    t0 = time.monotonic()
    try:
        holdings = spdr.collect(sources_cfg, universe)
    except Exception as exc:                              # noqa: BLE001
        holdings = spdr.CollectResult(source_id="spdr", status="failed",
                                      note=f"SPDR 取不到：{exc}")
    summary["sources"]["spdr"] = {"status": holdings.status, "note": holdings.note,
                                  "seconds": round(time.monotonic() - t0, 1)}

    observations: List[Dict[str, Any]] = list(holdings.observations)
    instruments = {i["instrument_key"]: i for i in holdings.instruments}
    asof = None
    for obs in holdings.observations:
        asof = obs["asof_date"] if asof is None else max(asof, obs["asof_date"])
    summary["asof"] = asof

    derived = 0
    no_curve = 0
    stale_hits = 0
    curve_day_key = pricing.nearest_curve_day(bench["curve"], asof) if asof else None
    curve_day = bench["curve"].get(curve_day_key or "", {})
    index_day_key = pricing.nearest_curve_day(bench["index_oas_bp"], asof) if asof else None
    index_oas = bench["index_oas_bp"].get(index_day_key or "", {})
    summary["curve_anchor"] = curve_day_key
    summary["index_anchor"] = index_day_key

    stale_days = int(thresholds["quality"]["stale_price_days"])
    for obs in holdings.observations:
        inst = instruments.get(obs["instrument_key"])
        if inst is None:
            continue
        if not dry_run and _stale_flag(obs["instrument_key"], float(obs["value"]), stale_days):
            obs["quality"] = "stale"
            stale_hits += 1
        if not curve_day:
            no_curve += 1
            continue
        row = pricing.spread_row(instrument=inst, price=float(obs["value"]),
                                 asof=obs["asof_date"], curve_day=curve_day,
                                 index_oas_bp=index_oas)
        if row is None:
            no_curve += 1
            continue
        quality = "option_biased" if inst.get("has_embedded_option") else obs["quality"]
        base = {
            "asof_date": obs["asof_date"], "instrument_key": obs["instrument_key"],
            "value_text": None, "method": "derived", "source_id": obs["source_id"],
            "obs_date": obs["obs_date"], "staleness_days": obs["staleness_days"],
            "quality": quality,
            "raw_ref": f"curve={curve_day_key} idx={index_day_key}",
        }
        for metric, value, unit in (
                ("yld.ytm", row["ytm"], "pct"),
                ("yld.gspread_bp", row["gspread_bp"], "bp"),
                ("yld.excess_vs_index_bp", row["excess_bp"], "bp"),
                ("ref.years_to_maturity", row["years"], "years"),
                ("ref.duration", row["duration"], "years")):
            if value is None:
                continue
            observations.append({**base, "metric": metric,
                                 "value": round(float(value), 4), "unit": unit})
            derived += 1

    summary["sources"]["spdr"].update({
        "instruments": len(instruments), "derived_rows": derived,
        "no_curve_rows": no_curve, "stale_flagged": stale_hits})

    if not dry_run:
        db.upsert_instruments(holdings.instruments)
        db.upsert_observations(observations)
        db.record_run({"run_id": run_id, "source_id": "spdr", "obs_date": asof,
                       "started_at": started, "ended_at": _now(),
                       "status": holdings.status,
                       "rows_written": len(observations),
                       "basket_fingerprint": holdings.basket_fingerprint,
                       "note": holdings.note})

    # --- 可选：基本面 / GPU 抵押载体 / SPV 台账 ------------------------------
    if with_sec:
        for label, fn in (("sec_facts", sec.collect_fundamentals),
                          ("sec_rfile", sec.collect_gpu_secured),
                          ("spv_ledger", sec.collect_spv)):
            started = _now()
            t0 = time.monotonic()
            try:
                res = (fn(universe=universe) if label == "spv_ledger"
                       else fn(sources_cfg, universe))
            except Exception as exc:                      # noqa: BLE001
                res = sec.CollectResult(source_id=label, status="failed",
                                        note=f"{label} 失败：{exc}")
            summary["sources"][label] = {"status": res.status, "note": res.note,
                                         "rows": len(res.observations),
                                         "seconds": round(time.monotonic() - t0, 1)}
            if not dry_run:
                db.upsert_observations(res.observations)
                db.record_run({"run_id": run_id, "source_id": label, "obs_date": asof,
                               "started_at": started, "ended_at": _now(),
                               "status": res.status, "rows_written": len(res.observations),
                               "basket_fingerprint": None, "note": res.note})
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="采集 DC 信用监控数据。")
    parser.add_argument("--with-sec", action="store_true",
                        help="同时跑基本面、GPU 抵押载体、SPV 台账")
    parser.add_argument("--dry-run", action="store_true", help="不落库")
    args = parser.parse_args()
    print(json.dumps(run(args.with_sec, args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
