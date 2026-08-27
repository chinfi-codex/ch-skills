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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import db_adapter  # noqa: E402
import validate  # noqa: E402
from collectors import attested, openrouter, ornn, runpod, vast  # noqa: E402
from collectors.base import CollectResult, CollectorError, utc_now_iso  # noqa: E402
from gpu_catalog import load_catalog  # noqa: E402

CONFIG_DIR = SCRIPT_DIR.parent / "config"

API_COLLECTORS = {
    "ornn": ornn.collect,
    "vast": vast.collect,
    "runpod": runpod.collect,
    "openrouter": openrouter.collect,
}


def load_sources() -> Dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))


def load_validation_cfg() -> Dict[str, Any]:
    raw = yaml.safe_load((CONFIG_DIR / "thresholds.yaml").read_text(encoding="utf-8"))
    return (raw or {}).get("validation") or {}


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

    validation_cfg = load_validation_cfg()
    # 比对基准一次性读出来：逐源各查一遍数据库没必要，而且基准应该是
    # 「本次采集开始前」的历史，不该被同一批新数据影响。
    # dry-run 不建表也不应隐式依赖已有数据库；传空基准即可按冷启动规则跳过打标。
    history = {}
    if validation_cfg.get("enabled", True) and not args.dry_run:
        # 多读一段：Ornn 每次带回 90 天历史，基准池要盖得住最早那一行
        # 往前 lookback 天，否则回填的头几十天全都没有基准。
        lookback = int(validation_cfg.get("lookback_days", 30)) + args.history_days
        start = (date.fromisoformat(obs_date) - timedelta(days=lookback)).isoformat()
        history = validate.trailing_history(start, obs_date)

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
            # Validate 在 Persist 之前（PRD §6.1 的步骤顺序）
            suspicious = validate.flag_outliers(
                result.prices, validation_cfg, history=history, obs_date=obs_date)
            for hit in suspicious:
                result.notes.append(
                    f"离群标记 {hit['gpu_model']}/{hit['price_type']}: "
                    f"{hit['value']} vs 基准 {hit['baseline_median']}（{hit['detail']}）")
            latency = int((time.time() - started) * 1000)
            if not args.dry_run:
                db_adapter.save_prices(result.prices)
                db_adapter.save_supply(result.supply)
                db_adapter.save_tokens(result.tokens)
                db_adapter.save_token_apps(result.apps)
            # 采成功但一行都没拿到，和"采到了"不是一回事：健康度面板要能区分。
            # 应用行不进这个判断：它是补强维度，只有它有数不算这次采集成功。
            status = "ok" if (result.prices or result.supply or result.tokens) else "empty"
            record.update({
                "finished_at": utc_now_iso(), "status": status, "latency_ms": latency,
                "price_rows": len(result.prices), "supply_rows": len(result.supply),
                "token_rows": len(result.tokens), "app_rows": len(result.apps),
                "unmapped_ids": sorted(set(result.unmapped)) or None,
                "error": None, "raw_path": result.raw_path,
            })
            summary["sources"][name] = {
                "status": status, "price_rows": len(result.prices),
                "supply_rows": len(result.supply), "token_rows": len(result.tokens),
                "app_rows": len(result.apps),
                "latency_ms": latency,
                "unmapped": sorted(set(result.unmapped)), "notes": result.notes,
                "suspicious": suspicious, "raw_path": result.raw_path,
            }
        except Exception as exc:  # 单源失败不阻断其它源
            latency = int((time.time() - started) * 1000)
            record.update({
                "finished_at": utc_now_iso(), "status": "failed", "latency_ms": latency,
                "price_rows": 0, "supply_rows": 0, "token_rows": 0, "app_rows": 0,
                "unmapped_ids": None,
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
