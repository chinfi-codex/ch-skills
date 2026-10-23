#!/usr/bin/env python3
"""Daily tracking of every active / watch member.

For each member: pull bars → trend metrics → exit-rule hits → status
transition (hard rules, applied unless --dry-run) → entry signals
（突破 / 缩量回踩，only for members that stay active）.  Also joins the
member's theme_id with today's 主线生命周期 state and lists which members
need deep news work today (deep_news_targets).

Output JSON: asof, members[], transitions[], signals[], deep_news_targets[],
suspended[], errors[], theme_context_asof.  Judgement (四档走势, 消息定性,
提醒措辞) is left to the model — see SKILL.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pool_data  # noqa: E402
import pool_rules  # noqa: E402
import pool_store  # noqa: E402


def _watch_days(rows: list[dict], watch_since: str | None, asof: str) -> int:
    if not watch_since:
        return 0
    return sum(1 for r in rows if watch_since <= r["trade_date"] < asof)


def track_member(member: dict, asof: str, rules: dict, apply: bool, themes: dict) -> dict:
    start = (date.fromisoformat(member["entry_date"])
             - timedelta(days=int(rules["track"]["lookback_calendar_days"]))).isoformat()
    fetched = pool_data.bars(member["ts_code"], start, asof)
    rows = fetched["rows"]
    base = {"ts_code": member["ts_code"], "name": member.get("name"), "source": member["source"],
            "entry_date": member["entry_date"], "status": member["status"],
            "reason": member.get("reason"), "theme_id": member.get("theme_id"),
            "theme_name": member.get("theme_name"), "bar_source": fetched["source"]}
    if not rows or rows[-1]["trade_date"] != asof:
        return dict(base, suspended=True, last_bar=rows[-1]["trade_date"] if rows else None)

    m = pool_rules.compute_metrics(rows, member)
    hits = pool_rules.exit_hits(m, rules)
    decision = pool_rules.decide_status(member, hits, _watch_days(rows, member.get("watch_since"), asof), rules)
    signals = pool_rules.entry_signals(m, rules) if decision["to"] == "active" else []

    theme = themes.get(member.get("theme_id") or "")
    out = dict(base, metrics=m, exit_hits=hits, decision=decision, signals=signals,
               theme_state=({k: theme.get(k) for k in ("name", "state", "state_prev", "stars", "position", "crowding")}
                            if theme else None),
               recent_events=pool_store.events(member["ts_code"], member["entry_date"],
                                               since=(date.fromisoformat(asof) - timedelta(days=7)).isoformat()))
    if not apply:
        return out

    code, entry = member["ts_code"], member["entry_date"]
    pool_store.upsert_daily(code, entry, asof, decision["to"], m, hits, signals)
    if m["relimit_today"]:
        pool_store.add_event(code, entry, asof, "relimit", f"池内再次涨停（入池第 {m['days_in_pool']} 日）")
    fields: dict = {"suggest_remove": 1 if decision["suggest_remove"] else 0}
    if decision["changed"]:
        to = decision["to"]
        fields["status"] = to
        if to == "watch":
            fields["watch_since"] = asof
        elif to == "active":
            fields["watch_since"] = None
        elif to == "removed":
            fields.update(removed_date=asof, removed_reason=decision["reason"])
        pool_store.add_event(code, entry, asof, "status",
                             f"{decision['from']} → {to}", detail=decision["reason"])
    pool_store.update_member(code, entry, **fields)
    for sig in signals:
        pool_store.add_event(code, entry, asof, "signal", sig["label"], detail=sig["fact"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="交易日 YYYY-MM-DD，默认最近一个交易日")
    ap.add_argument("--rules", help="规则文件，默认 assets/pool_rules.yaml")
    ap.add_argument("--codes", help="只跟踪这些代码（逗号分隔），默认全池 active + watch")
    ap.add_argument("--dry-run", action="store_true", help="只计算不落库、不改状态")
    ap.add_argument("--output", help="另存 JSON 路径")
    args = ap.parse_args()

    rules = pool_rules.load_rules(args.rules)
    asof = pool_data.latest_trade_date(args.date)
    pool_store.ensure_schema()
    members = pool_store.list_members(("active", "watch"))
    if args.codes:
        wanted = {c.strip() for c in args.codes.split(",") if c.strip()}
        members = [m for m in members if m["ts_code"] in wanted]
    members = [m for m in members if m["entry_date"] <= asof]

    ctx = pool_store.theme_context(asof)
    themes = {t["theme_id"]: t for t in ctx.get("themes", [])} if ctx.get("available") else {}

    results, errors = [], []
    for member in members:
        try:
            results.append(track_member(member, asof, rules, not args.dry_run, themes))
        except Exception as exc:  # noqa: BLE001 — one bad stock must not sink the run
            errors.append({"ts_code": member["ts_code"], "error": str(exc)})

    tracked = [r for r in results if not r.get("suspended")]
    deep = []
    for r in tracked:
        why = []
        if r["entry_date"] == asof:
            why.append("今日新入池")
        if r["metrics"]["relimit_today"]:
            why.append("池内再涨停")
        if r["signals"]:
            why.append("介入信号")
        if r["decision"]["changed"] or r["decision"]["to"] == "watch":
            why.append("状态变化/预警")
        if abs(r["metrics"]["pct_chg"] or 0) >= 7:
            why.append("大幅异动")
        if why:
            deep.append({"ts_code": r["ts_code"], "name": r["name"], "why": why})

    payload = {
        "asof": asof,
        "dry_run": args.dry_run,
        "member_count": len(members),
        "theme_context_asof": ctx.get("asof") if ctx.get("available") else None,
        "theme_context_note": None if ctx.get("available") else ctx.get("reason"),
        "transitions": [{"ts_code": r["ts_code"], "name": r["name"], **r["decision"]}
                        for r in tracked if r["decision"]["changed"] or r["decision"]["suggest_remove"]],
        "signals": [dict(s, ts_code=r["ts_code"], name=r["name"]) for r in tracked for s in r["signals"]],
        "deep_news_targets": deep,
        "suspended": [{"ts_code": r["ts_code"], "name": r["name"], "last_bar": r["last_bar"]}
                      for r in results if r.get("suspended")],
        "errors": errors,
        "members": results,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
