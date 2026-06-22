#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-day continuity ledger for Nasdaq-tech themes (file-based, no LLM).

Mirrors a-stock-daily-market-sense's theme_lifecycle.py split: the script is
deterministic — it provides context, validates the enum + state-machine
transitions, normalizes theme aliases against a registry, and appends the day's
records. The model does the judgment (which theme is which, what state it's in).

Why this exists: new-direction mining cares whether a theme/ticker *persists*.
A one-night pop and a 3-day emerging direction look identical in a single
report; the ledger is the memory that tells them apart, and it closes the loop
on past "建议纳入" calls.

Storage lives OUTSIDE the skill tree (default ~/.usmarket-ledger, override with
USMARKET_LEDGER_DIR) so it is not version-controlled and not copied around by
skill_sync — every synced copy of the skill reads/writes the one shared ledger,
the same isolation DMS gets from Postgres.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Theme lifecycle states (adapted from DMS's 8-state machine, focused on the
# US new-direction use case). 修复 is folded into 低位启动/在场 re-entry.
STATES = ["低位启动", "在场", "确认主线", "高位分歧", "退潮", "沉寂"]
FIRST_STATES = {"低位启动", "在场", "确认主线", "高位分歧"}
TRANSITIONS: Dict[str, set] = {
    "低位启动": {"低位启动", "在场", "确认主线", "高位分歧", "退潮", "沉寂"},
    "在场": {"在场", "确认主线", "高位分歧", "退潮", "沉寂"},
    "确认主线": {"确认主线", "在场", "高位分歧", "退潮"},
    "高位分歧": {"高位分歧", "确认主线", "在场", "退潮", "沉寂"},
    "退潮": {"退潮", "沉寂", "低位启动", "在场"},
    "沉寂": {"沉寂", "低位启动", "在场"},
}
DORMANT_AFTER_DAYS = 5  # absent this many recorded days running -> implicitly 沉寂


def ledger_dir() -> Path:
    path = Path(os.environ.get("USMARKET_LEDGER_DIR") or (Path.home() / ".usmarket-ledger"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def registry_path() -> Path:
    return ledger_dir() / "theme_registry.json"


def ledger_path() -> Path:
    return ledger_dir() / "theme_ledger.jsonl"


def load_registry() -> Dict[str, Any]:
    path = registry_path()
    if not path.exists():
        return {"themes": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_registry(registry: Dict[str, Any]) -> None:
    registry_path().write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_records() -> List[Dict[str, Any]]:
    path = ledger_path()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _latest_state_by_theme(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Most recent ledger row per theme_id (records are appended in date order)."""
    latest: Dict[str, Dict[str, Any]] = {}
    for row in sorted(records, key=lambda r: r.get("asof", "")):
        tid = row.get("theme_id")
        if tid:
            latest[tid] = row
    return latest


def cmd_context(args: argparse.Namespace) -> Dict[str, Any]:
    registry = load_registry()
    records = read_records()
    asof = args.asof
    recent = [r for r in records if r.get("asof", "") <= asof][-args.days * 12 :]  # generous slice
    latest = _latest_state_by_theme(records)

    # Watchlist: themes active recently but with no record exactly on `asof` —
    # the model must decide 退潮/沉寂/在场 for these when writing today's file.
    watchlist = []
    for tid, row in latest.items():
        if row.get("asof") == asof:
            continue
        last_seen = row.get("asof", "")
        gap = _trading_gap(records, last_seen, asof)
        watchlist.append(
            {
                "theme_id": tid,
                "name": registry.get("themes", {}).get(tid, {}).get("name", tid),
                "last_seen": last_seen,
                "last_state": row.get("state"),
                "recorded_days_since": gap,
                "hint": "超过沉寂阈值，若仍缺席记 沉寂" if gap >= DORMANT_AFTER_DAYS else "近期在场，缺席则判 退潮 或继续在场",
            }
        )

    return {
        "asof": asof,
        "ledger_dir": str(ledger_dir()),
        "registry": registry,
        "recent_records": recent,
        "watchlist": sorted(watchlist, key=lambda w: w["last_seen"], reverse=True),
        "states": STATES,
        "transitions": {k: sorted(v) for k, v in TRANSITIONS.items()},
        "first_states": sorted(FIRST_STATES),
    }


def _trading_gap(records: List[Dict[str, Any]], last_seen: str, asof: str) -> int:
    """How many distinct recorded dates fall in (last_seen, asof]."""
    dates = sorted({r.get("asof", "") for r in records if last_seen < r.get("asof", "") <= asof})
    return len(dates)


def _resolve_theme(record: Dict[str, Any], registry: Dict[str, Any]) -> str:
    """Return a theme_id, registering a new theme block if provided."""
    themes = registry.setdefault("themes", {})
    if "new_theme" in record and record["new_theme"]:
        block = record["new_theme"]
        tid = block.get("theme_id", "")
        if not tid.startswith("TH-"):
            raise ValueError(f"new_theme.theme_id 必须以 TH- 前缀：{tid!r}")
        entry = themes.setdefault(
            tid, {"name": block.get("name", tid), "aliases": [], "first_seen": record.get("asof")}
        )
        for alias in block.get("aliases", []) or []:
            if alias not in entry["aliases"]:
                entry["aliases"].append(alias)
        return tid
    tid = record.get("theme_id", "")
    if tid not in themes:
        raise ValueError(f"theme_id 未注册：{tid!r}（新主线请用 new_theme 块注册）")
    # Record the raw name as an alias if it's new — keeps cross-day identity.
    raw = record.get("raw_theme_name")
    if raw and raw != themes[tid]["name"] and raw not in themes[tid]["aliases"]:
        themes[tid]["aliases"].append(raw)
    return tid


def cmd_record(args: argparse.Namespace) -> Dict[str, Any]:
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    asof = payload.get("asof")
    if not asof:
        raise ValueError("输入缺少 asof")

    registry = load_registry()
    existing = read_records()
    latest = _latest_state_by_theme(existing)
    errors: List[str] = []
    prepared: List[Dict[str, Any]] = []

    for rec in payload.get("records", []):
        try:
            tid = _resolve_theme(rec, registry)
        except ValueError as exc:
            errors.append(str(exc))
            continue

        state = rec.get("state")
        if state not in STATES:
            errors.append(f"{tid}: 非法状态 {state!r}，合法值 {STATES}")
            continue

        prev = latest.get(tid)
        is_first = prev is None or prev.get("state") in (None, "沉寂")
        if is_first and not rec.get("force"):
            if state not in FIRST_STATES:
                errors.append(f"{tid}: 首次/再启动只能是 {sorted(FIRST_STATES)}，给的是 {state}")
                continue
        elif not rec.get("force"):
            allowed = TRANSITIONS.get(prev.get("state"), set())
            if state not in allowed:
                errors.append(
                    f"{tid}: 非法转移 {prev.get('state')} → {state}；合法去向 {sorted(allowed)}"
                )
                continue

        share = rec.get("dollar_vol_share")
        if share is not None and not (0 <= share <= 1):
            errors.append(f"{tid}: dollar_vol_share 须在 [0,1]，给的是 {share}")
            continue

        prepared.append(
            {
                "asof": asof,
                "theme_id": tid,
                "name": registry["themes"][tid]["name"],
                "raw_theme_name": rec.get("raw_theme_name"),
                "stars": rec.get("stars"),
                "dollar_vol_share": share,
                "position": rec.get("position"),
                "state": state,
                "in_pool": bool(rec.get("in_pool", False)),
                "is_new_direction": bool(rec.get("is_new_direction", False)),
                "members": rec.get("members", []),
                "evidence": rec.get("evidence", ""),
            }
        )

    if errors:
        return {"status": "error", "errors": errors, "hint": "修正后重跑；幂等可重复执行。"}

    # Idempotent upsert: drop any existing rows for this asof, then re-append.
    kept = [r for r in existing if r.get("asof") != asof]
    all_rows = kept + prepared
    ledger_path().write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in all_rows) + "\n", encoding="utf-8"
    )
    save_registry(registry)
    return {"status": "success", "asof": asof, "recorded": len(prepared), "ledger": str(ledger_path())}


def main() -> None:
    parser = argparse.ArgumentParser(description="Nasdaq-tech theme cross-day ledger (deterministic).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ctx = sub.add_parser("context", help="取注册表 + 近期状态 + watchlist，供模型判定当日状态")
    ctx.add_argument("--asof", required=True, help="交易日 YYYY-MM-DD")
    ctx.add_argument("--days", type=int, default=10, help="回看交易日数，默认 10")

    rec = sub.add_parser("record", help="校验并落库当日主线判定（模型写的 lifecycle JSON）")
    rec.add_argument("--input", required=True, help="lifecycle_YYYYMMDD.json 路径")

    args = parser.parse_args()
    result = cmd_context(args) if args.cmd == "context" else cmd_record(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
