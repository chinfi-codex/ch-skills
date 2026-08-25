#!/usr/bin/env python3
"""每日采集编排：逐源 fetch → raw 落盘 → 标准化 → 幂等入库 → 记健康日志。

一个源挂掉不阻断其它源（PRD §6.3）。每个源的成败、耗时、行数、未映射标识
都写进 gpu_collect_runs，指标层和数据源健康度面板都读这张表。

脚本不下任何结论。它只回答"今天各源采到了什么、缺了什么"，
"这些数字意味着什么"是模型的事。

用法：
    python scripts/collect.py                          # 采全部启用的源
    python scripts/collect.py --sources ornn,runpod    # 只采指定源
    python scripts/collect.py --date 2026-08-25        # 指定观测日
    python scripts/collect.py --history-days 120       # Ornn 回填窗口
    python scripts/collect.py --dry-run                # 采但不写库
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import db_adapter  # noqa: E402
from collectors import attested, ornn, runpod, vast  # noqa: E402
from collectors.base import CollectResult, CollectorError, utc_now_iso  # noqa: E402
from gpu_catalog import load_catalog  # noqa: E402

CONFIG_DIR = SCRIPT_DIR.parent / "config"

API_COLLECTORS = {
    "ornn": ornn.collect,
    "vast": vast.collect,
    "runpod": runpod.collect,
}


def load_sources() -> Dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))


def run_one(name: str, cfg: Dict[str, Any], catalog, obs_date: str,
            defaults: Dict[str, Any], history_days: int) -> CollectResult:
    if cfg.get("mode") == "attested":
        return attested.collect(name, cfg, catalog, obs_date)
    fn = API_COLLECTORS.get(name)
    if fn is None:
        raise CollectorError(f"未知数据源 {name}")
    if name == "ornn":
        return fn(cfg, catalog, obs_date, history_days=history_days, defaults=defaults)
    return fn(cfg, catalog, obs_date, defaults=defaults)


def main() -> int:
    parser = argparse.ArgumentParser(description="GPU 算力价格与供给日采集")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="观测日 YYYY-MM-DD，默认今天")
    parser.add_argument("--sources", default=None,
                        help="逗号分隔的源名；默认采 config 里所有 enabled 的源")
    parser.add_argument("--history-days", type=int, default=120,
                        help="Ornn 历史回填窗口天数（免费层会被钳到滚动 3 个月）")
    parser.add_argument("--dry-run", action="store_true", help="采集但不写库")
    parser.add_argument("--output", default=None, help="把本次采集摘要另存为 JSON")
    args = parser.parse_args()

    obs_date = args.date
    cfg_all = load_sources()
    defaults = cfg_all.get("defaults") or {}
    sources = cfg_all.get("sources") or {}
    catalog = load_catalog()

    if args.sources:
        wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
    else:
        wanted = [k for k, v in sources.items() if v.get("enabled")]

    if not args.dry_run:
        db_adapter.init_schema()

    run_id = f"{obs_date}-{uuid.uuid4().hex[:8]}"
    summary: Dict[str, Any] = {"run_id": run_id, "obs_date": obs_date,
                               "started_at": utc_now_iso(), "sources": {}}

    for name in wanted:
        cfg = sources.get(name)
        if cfg is None:
            summary["sources"][name] = {"status": "unknown_source"}
            continue
        started = time.time()
        record: Dict[str, Any] = {
            "run_id": run_id, "source": name, "obs_date": obs_date,
            "started_at": utc_now_iso(), "attempts": defaults.get("retries", 3),
        }
        try:
            result = run_one(name, cfg, catalog, obs_date, defaults, args.history_days)
            latency = int((time.time() - started) * 1000)
            if not args.dry_run:
                db_adapter.save_prices(result.prices)
                db_adapter.save_supply(result.supply)
            # 采成功但一行都没拿到，和"采到了"不是一回事：健康度面板要能区分
            status = "ok" if (result.prices or result.supply) else "empty"
            record.update({
                "finished_at": utc_now_iso(), "status": status, "latency_ms": latency,
                "price_rows": len(result.prices), "supply_rows": len(result.supply),
                "unmapped_ids": sorted(set(result.unmapped)) or None,
                "error": None, "raw_path": result.raw_path,
            })
            summary["sources"][name] = {
                "status": status, "price_rows": len(result.prices),
                "supply_rows": len(result.supply), "latency_ms": latency,
                "unmapped": sorted(set(result.unmapped)), "notes": result.notes,
                "raw_path": result.raw_path,
            }
        except Exception as exc:  # 单源失败不阻断其它源
            latency = int((time.time() - started) * 1000)
            record.update({
                "finished_at": utc_now_iso(), "status": "failed", "latency_ms": latency,
                "price_rows": 0, "supply_rows": 0, "unmapped_ids": None,
                "error": f"{type(exc).__name__}: {exc}"[:1000], "raw_path": None,
            })
            summary["sources"][name] = {"status": "failed",
                                        "error": f"{type(exc).__name__}: {exc}"[:500]}
        if not args.dry_run:
            db_adapter.save_run(record)

    summary["finished_at"] = utc_now_iso()
    ok = [s for s, v in summary["sources"].items() if v.get("status") in ("ok", "empty")]
    summary["ok_sources"] = sorted(ok)
    summary["empty_sources"] = sorted(s for s, v in summary["sources"].items()
                                      if v.get("status") == "empty")
    summary["failed_sources"] = sorted(s for s, v in summary["sources"].items()
                                       if v.get("status") not in ("ok", "empty"))
    summary["dry_run"] = args.dry_run

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # 全部源都失败才算这次运行失败；部分失败是可接受的降级
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
