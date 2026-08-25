#!/usr/bin/env python3
"""历史回填：把每个源接口能给到的历史全部拉进库，并把拿不到的部分说清楚。

现实是不对称的，这个脚本的一半价值就是把这个不对称显式化：

  * Ornn 有 index-history 接口，无 key 时给滚动 3 个月日度结算（实测 92 个点），
    正好盖住首页的 90D 窗口 —— 这一路上线当天就是完整曲线。
  * Vast 的 bundles 只回"此刻"的挂牌，Runpod 的 gpuTypes 也只回当下，
    两家都没有公开的历史接口（Runpod 还关了 GraphQL introspection）。
    它们的序列只能从首次采集当天开始往后长，补不回去。
  * CoreWeave / Nebius 是人工核对，历史等于你核对过几次。

所以「补 90 天」对不同源意味着不同的事。脚本跑完会逐源报告实际覆盖到哪天、
有多少个点、缺口在哪，而不是给一个笼统的"已回填"。

用法：
    python scripts/backfill.py                    # 回填至今 90 天
    python scripts/backfill.py --days 120         # 要更长（无 key 时 Ornn 会被钳到 3 个月）
    python scripts/backfill.py --report-only      # 不取数，只看当前库里的覆盖情况
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import db_adapter  # noqa: E402
from collectors import openrouter, ornn  # noqa: E402
from collectors.base import CollectorError, utc_now_iso  # noqa: E402
from gpu_catalog import load_catalog  # noqa: E402

CONFIG_DIR = SCRIPT_DIR.parent / "config"

# 有历史接口的源在这里注册。没有的源不是"忘了实现"，是接口不存在。
# openrouter 是个混合体：日度模型级序列补不回去，但厂商级周度的 market-share
# 有 52 周真历史，落进另一张表（口径不同，见 collectors/openrouter.collect_history）。
HISTORY_COLLECTORS = {"ornn": ornn.collect, "openrouter": openrouter.collect_history}


def _d(value: Any) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def coverage(start: str, end: str) -> Dict[str, Any]:
    """按 (source, gpu_model, price_type) 报告库里实际覆盖了哪些天。"""
    rows = db_adapter.read_prices(start, end)
    buckets: Dict[tuple, List[str]] = defaultdict(list)
    for row in rows:
        if row.get("price_usd_gpu_hour") is None:
            continue
        buckets[(row["source"], row["gpu_model"], row["price_type"])].append(
            _d(row["obs_date"]))

    out = []
    for (source, model, ptype), days in sorted(buckets.items()):
        uniq = sorted(set(days))
        first, last = uniq[0], uniq[-1]
        span = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
        missing = span - len(uniq)
        out.append({
            "source": source, "gpu_model": model, "price_type": ptype,
            "first_date": first, "last_date": last,
            "points": len(uniq), "span_days": span,
            # 缺口是真实存在的采集空洞，不做插值，只如实报出来
            "gap_days": missing,
            "contiguous": missing == 0,
        })
    return {"window": {"start": start, "end": end}, "series": out}


def token_history_coverage() -> Dict[str, Any]:
    """厂商级周度历史的覆盖情况。单独报，不和日度价格序列混在一张表里。"""
    rows = db_adapter.read_token_history("2000-01-01", date.today().isoformat())
    if not rows:
        return {"weeks": 0, "reason": "库里还没有周度历史（跑一次 backfill 再看）"}
    weeks = sorted({_d(r["week_start"]) for r in rows})
    expected = ((date.fromisoformat(weeks[-1])
                 - date.fromisoformat(weeks[0])).days // 7) + 1
    authors = sorted({r["author"] for r in rows})
    return {
        "first_week": weeks[0], "last_week": weeks[-1],
        "weeks": len(weeks), "expected_weeks": expected,
        "gap_weeks": expected - len(weeks),
        "contiguous": expected == len(weeks),
        "authors": authors,
        "rows": len(rows),
        "unit_basis": sorted({r.get("unit_basis") for r in rows}),
        "note": ("厂商级周度，口径与日度模型级序列不同：只能看份额与增速，"
                 "不能读绝对水平，也不能与日度序列相减"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="把接口能给到的历史全部回填进库")
    parser.add_argument("--days", type=int, default=90,
                        help="回填窗口天数，默认 90（对齐首页固定窗口）")
    parser.add_argument("--date", default=date.today().isoformat(), help="窗口右端")
    parser.add_argument("--report-only", action="store_true", help="不取数，只报覆盖情况")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    end = args.date
    start = (date.fromisoformat(end) - timedelta(days=args.days)).isoformat()
    cfg_all = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))
    defaults = cfg_all.get("defaults") or {}
    sources = cfg_all.get("sources") or {}
    catalog = load_catalog()

    summary: Dict[str, Any] = {
        "window": {"start": start, "end": end, "days": args.days},
        "started_at": utc_now_iso(),
        "backfilled": {},
        "no_history_api": [],
    }

    if not args.report_only:
        db_adapter.init_schema()
        run_id = f"backfill-{end}-{uuid.uuid4().hex[:8]}"
        for name, cfg in sources.items():
            if not cfg.get("enabled"):
                continue
            if not cfg.get("has_history_api"):
                summary["no_history_api"].append({
                    "source": name,
                    "reason": ("接口只回当下快照，没有公开的历史入口；"
                               "该源序列只能从首次采集当天开始"),
                })
                continue
            fn = HISTORY_COLLECTORS.get(name)
            if fn is None:
                summary["backfilled"][name] = {
                    "status": "failed",
                    "error": f"{name} 标了 has_history_api 但没注册历史采集器"}
                continue
            started = time.time()
            try:
                if name == "openrouter":
                    # 它不吃 GPU 目录，也不按天回填：一次拿全 52 周厂商级历史
                    result = fn(cfg, defaults=defaults)
                else:
                    # 多要 5 天，抵消 T-1 结算和边界日的对齐损耗
                    result = fn(cfg, catalog, end, history_days=args.days + 5,
                                defaults=defaults)
                db_adapter.save_prices(result.prices)
                db_adapter.save_supply(result.supply)
                db_adapter.save_token_history(result.history)
                latency = int((time.time() - started) * 1000)
                db_adapter.save_run({
                    "run_id": run_id, "source": name, "obs_date": end,
                    "started_at": utc_now_iso(), "finished_at": utc_now_iso(),
                    "status": "ok", "attempts": defaults.get("retries", 3),
                    "latency_ms": latency, "price_rows": len(result.prices),
                    "supply_rows": len(result.supply),
                    "unmapped_ids": sorted(set(result.unmapped)) or None,
                    "error": None, "raw_path": result.raw_path,
                })
                summary["backfilled"][name] = {
                    "status": "ok", "price_rows": len(result.prices),
                    "supply_rows": len(result.supply),
                    "token_history_rows": len(result.history), "latency_ms": latency,
                    "notes": result.notes,
                }
            except (CollectorError, Exception) as exc:  # noqa: BLE001
                summary["backfilled"][name] = {
                    "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500]}

    summary["coverage"] = coverage(start, end)
    summary["token_history_coverage"] = token_history_coverage()
    summary["finished_at"] = utc_now_iso()

    text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    failed = [s for s, v in summary["backfilled"].items() if v.get("status") != "ok"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
