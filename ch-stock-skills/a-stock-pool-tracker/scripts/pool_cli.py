#!/usr/bin/env python3
"""Pool maintenance CLI — every write the model or the user makes by hand.

Subcommands:
  list      [--status active,watch,removed]
  show      TS_CODE                          member + last 30 snapshots + events
  add       TS_CODE --reason R [--date D] [--anchor-low P] [--name N]   手动入池
  annotate  TS_CODE [--reason R] [--theme-id TH-x] [--theme-name N]     写入池理由 / 主线归属
  remove    TS_CODE --reason R [--date D]    人工或模型认定的移除（如实质利空）
  event     TS_CODE --title T [--kind news] [--category C] [--detail] [--source] [--url] [--date]
  verdict   TS_CODE --verdict 健康|分歧|预警|走坏 [--note] [--date]
  themes    [--date D]                       当日主线生命周期上下文

All output is JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pool_data  # noqa: E402
import pool_store  # noqa: E402

VERDICTS = ("健康", "分歧", "预警", "走坏")
NEWS_CATEGORIES = ("催化兑现", "新催化", "题材证伪", "实质利空", "中性")


def _out(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _norm_code(code: str) -> str:
    code = code.strip().upper()
    if "." in code:
        return code
    suffix = ".SH" if code.startswith(("6", "9")) else ".BJ" if code.startswith(("4", "8")) else ".SZ"
    return code + suffix


def _require_open(ts_code: str) -> dict:
    member = pool_store.open_member(ts_code)
    if not member:
        raise SystemExit(f"{ts_code} 不在池中（active/watch）")
    return member


def cmd_list(args) -> None:
    statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
    _out(pool_store.list_members(statuses))


def cmd_show(args) -> None:
    rows = pool_store.list_members((), args.ts_code)
    if not rows:
        raise SystemExit(f"{args.ts_code} 没有任何入池记录")
    member = rows[-1]
    _out({"member": member, "episodes": len(rows),
          "daily": pool_store.daily_history(args.ts_code, member["entry_date"]),
          "events": pool_store.events(args.ts_code, member["entry_date"])})


def cmd_add(args) -> None:
    day = pool_data.latest_trade_date(args.date)
    start = (date.fromisoformat(day) - timedelta(days=15)).isoformat()
    rows = pool_data.bars(args.ts_code, start, day)["rows"]
    if not rows or rows[-1]["trade_date"] != day:
        raise SystemExit(f"{args.ts_code} 在 {day} 没有日线（停牌或代码错误）")
    bar = rows[-1]
    name = args.name
    if not name:
        info = pool_data.call("stock_basic", ts_code=args.ts_code, fields="ts_code,name")
        name = info[0]["name"] if info else None
    member = pool_store.add_member({
        "ts_code": args.ts_code, "entry_date": day, "name": name, "source": "manual",
        "limit_price": bar["close"], "entry_low": args.anchor_low or bar["low"],
        "launch_price": bar["pre_close"], "entry_amount": bar["amount"],
        "entry_adj_factor": bar.get("adj_factor"), "reason": args.reason,
    })
    _out({"ok": True, "member": member,
          "note": "手动股命中移除规则时只进预警并标记 suggest_remove，不会被自动移出"})


def cmd_annotate(args) -> None:
    member = _require_open(args.ts_code)
    fields = {k: v for k, v in (("reason", args.reason), ("theme_id", args.theme_id),
                                ("theme_name", args.theme_name)) if v is not None}
    if not fields:
        raise SystemExit("至少给一个 --reason / --theme-id / --theme-name")
    pool_store.update_member(args.ts_code, member["entry_date"], **fields)
    _out({"ok": True, "member": pool_store.open_member(args.ts_code)})


def cmd_remove(args) -> None:
    member = _require_open(args.ts_code)
    day = args.date or pool_data.latest_trade_date()
    pool_store.update_member(args.ts_code, member["entry_date"], status="removed",
                             removed_date=day, removed_reason=args.reason)
    pool_store.add_event(args.ts_code, member["entry_date"], day, "status",
                         f"{member['status']} → removed（人工）", detail=args.reason)
    _out({"ok": True, "ts_code": args.ts_code, "removed_date": day})


def cmd_event(args) -> None:
    member = _require_open(args.ts_code)
    if args.category and args.category not in NEWS_CATEGORIES:
        raise SystemExit(f"--category 必须是 {NEWS_CATEGORIES}")
    day = args.date or pool_data.latest_trade_date()
    pool_store.add_event(args.ts_code, member["entry_date"], day, args.kind, args.title,
                         detail=args.detail, category=args.category, source=args.source, url=args.url)
    _out({"ok": True})


def cmd_verdict(args) -> None:
    if args.verdict not in VERDICTS:
        raise SystemExit(f"--verdict 必须是 {VERDICTS}")
    day = args.date or pool_data.latest_trade_date()
    n = pool_store.set_verdict(args.ts_code, day, args.verdict, args.note)
    if not n:
        raise SystemExit(f"{args.ts_code} 在 {day} 没有跟踪快照，先跑 pool_track.py")
    _out({"ok": True})


def cmd_themes(args) -> None:
    _out(pool_store.theme_context(args.date or pool_data.latest_trade_date()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list")
    p.add_argument("--status", default="active,watch")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show")
    p.add_argument("ts_code", type=_norm_code)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("add")
    p.add_argument("ts_code", type=_norm_code)
    p.add_argument("--reason", required=True)
    p.add_argument("--date")
    p.add_argument("--anchor-low", type=float, help="防守价，默认入池日最低价")
    p.add_argument("--name")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("annotate")
    p.add_argument("ts_code", type=_norm_code)
    p.add_argument("--reason")
    p.add_argument("--theme-id")
    p.add_argument("--theme-name")
    p.set_defaults(fn=cmd_annotate)

    p = sub.add_parser("remove")
    p.add_argument("ts_code", type=_norm_code)
    p.add_argument("--reason", required=True)
    p.add_argument("--date")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("event")
    p.add_argument("ts_code", type=_norm_code)
    p.add_argument("--title", required=True)
    p.add_argument("--kind", default="news", choices=pool_store.EVENT_KINDS)
    p.add_argument("--category")
    p.add_argument("--detail")
    p.add_argument("--source")
    p.add_argument("--url")
    p.add_argument("--date")
    p.set_defaults(fn=cmd_event)

    p = sub.add_parser("verdict")
    p.add_argument("ts_code", type=_norm_code)
    p.add_argument("--verdict", required=True)
    p.add_argument("--note")
    p.add_argument("--date")
    p.set_defaults(fn=cmd_verdict)

    p = sub.add_parser("themes")
    p.add_argument("--date")
    p.set_defaults(fn=cmd_themes)

    args = ap.parse_args()
    pool_store.ensure_schema()
    try:
        args.fn(args)
    except pool_store.PoolError as exc:
        raise SystemExit(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
