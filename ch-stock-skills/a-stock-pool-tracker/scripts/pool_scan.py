#!/usr/bin/env python3
"""Daily entry scan: limit-up stocks that pass the hard rules join the pool.

Rules (assets/pool_rules.yaml → entry): 收盘涨停、首板、非一字板、非 ST、成交额 ≥ 3 亿、
换手率 < 15%、首次封板不晚于 10:30、含当天的 5 日累计涨幅 < 30%。Passing stocks not already in the pool
are inserted with their entry anchors (涨停价 / 涨停日最低价 / 启动价 / 成交额)
and an empty reason — the model fills reason + theme via pool_cli.py annotate.
A stock already in the pool that qualifies again is only reported here; the
relimit event is recorded by pool_track.py.

Output JSON (stdout, optionally --output):
  trade_date, first_time_source, degraded, added[], already_in_pool[],
  near_miss[] (limit-ups failing exactly one of amount/turnover/time),
  reject_counts{}, theme_context{}
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pool_data  # noqa: E402
import pool_rules  # noqa: E402
import pool_store  # noqa: E402

NEAR_MISS_REASONS = {"amount_below_min", "turnover_too_high", "first_time_after_cutoff", "gain_5d_too_high"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="交易日 YYYY-MM-DD，默认最近一个交易日")
    ap.add_argument("--rules", help="规则文件，默认 assets/pool_rules.yaml")
    ap.add_argument("--dry-run", action="store_true", help="只筛选不入池")
    ap.add_argument("--output", help="另存 JSON 路径")
    args = ap.parse_args()

    rules = pool_rules.load_rules(args.rules)
    td = pool_data.latest_trade_date(args.date)
    pool_store.ensure_schema()

    market = pool_data.market_day(td)
    ft = pool_data.first_limit_times(td)
    prior = pool_data.prior_days(td)
    degraded = ft["source"] is None

    passed, near_miss, counts = [], [], Counter()
    for row in market["rows"]:
        row = dict(row, first_time=ft["times"].get(row["ts_code"]), prior=prior.get(row["ts_code"]))
        ok, reasons, facts = pool_rules.screen_limit_up(row, rules, td)
        if "not_limit_close" in reasons:
            continue
        counts["limit_close"] += 1
        if ok:
            passed.append((row, facts))
            continue
        for reason in reasons:
            counts[reason] += 1
        if len(reasons) == 1 and reasons[0] in NEAR_MISS_REASONS and not pool_store.open_member(row["ts_code"]):
            near_miss.append(dict(facts, reject=reasons[0]))

    added, already = [], []
    adj = {}
    if passed and not args.dry_run:
        adj = {r["ts_code"]: r["adj_factor"] for r in pool_data.call("adj_factor", trade_date=pool_data.ts_date(td))}
    for row, facts in sorted(passed, key=lambda x: x[1]["first_limit_time"] or ""):
        facts["open_times"] = (ft["extra"].get(row["ts_code"]) or {}).get("open_times")
        facts["industry"] = row.get("industry")
        existing = pool_store.open_member(row["ts_code"])
        if existing:
            already.append(dict(facts, entry_date=existing["entry_date"], status=existing["status"]))
            continue
        if not args.dry_run:
            pool_store.add_member({
                "ts_code": row["ts_code"], "entry_date": td, "name": row.get("name"), "source": "auto",
                "limit_price": row["close"], "entry_low": row["low"], "launch_price": row["pre_close"],
                "entry_amount": row["amount"], "entry_turnover": facts["turnover_pct"],
                "entry_adj_factor": adj.get(row["ts_code"]), "first_limit_time": facts["first_limit_time"],
                "entry_title": f"规则入池：{facts['first_limit_time'][:2]}:{facts['first_limit_time'][2:4]} 封板，"
                               f"成交 {facts['amount_yi']} 亿，换手 {facts['turnover_pct']}%",
            })
        added.append(facts)

    payload = {
        "trade_date": td,
        "dry_run": args.dry_run,
        "rules": rules["entry"],
        "first_time_source": ft["source"],
        "degraded": degraded,
        "degraded_note": ("首次封板时间两个来源都失败，无法执行 10:30 规则，今日不入池：" + "；".join(ft["errors"]))
        if degraded else None,
        "data_notes": market["notes"] + ft["errors"],
        "added": added,
        "already_in_pool": already,
        "near_miss": sorted(near_miss, key=lambda x: -(x["amount_yi"] or 0))[:30],
        "reject_counts": dict(counts),
        "theme_context": pool_store.theme_context(td),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
