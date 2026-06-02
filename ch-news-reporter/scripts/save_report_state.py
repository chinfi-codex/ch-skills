#!/usr/bin/env python3
"""Persist and validate a profile watchboard (report_state) for one date.

The watchboard is the living analysis state that carries across days: current
regime, tracking-item ledger, actor weights, signal watchlist, probabilities and
next nodes.  This script only does deterministic I/O and *structural* validation;
every judgement (what to track, how weights move, whether an item resolved) stays
with the model that authored the payload.

Usage:
    # read JSON watchboard from stdin
    cat watchboard.json | python scripts/save_report_state.py \
        --profile iran_dynamic --date today --state-file -

    # validate only, do not write
    python scripts/save_report_state.py --profile ai_daily --date 2026-06-02 \
        --state-file watchboard.json --check-only
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from db_adapter import (
    get_connection,
    get_latest_report_state as db_get_latest_report_state,
    init_news_schema,
    table_exists,
    write_report_state as db_write_report_state,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DB = Path("data/news_research.sqlite")
DEFAULT_CONFIG = Path("config/report_profiles.yaml")
ALLOWED_STATUS = {"open", "confirmed", "dismissed", "expired"}
ENVELOPE_REQUIRED = ["as_of", "regime", "tracking_items", "next_nodes"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Persist/validate a watchboard payload into report_state."
    )
    parser.add_argument("--profile", required=True, help="Profile name, e.g. iran_dynamic.")
    parser.add_argument("--date", default="today", help="today or YYYY-MM-DD.")
    parser.add_argument(
        "--state-file",
        required=True,
        help="Path to watchboard JSON, or '-' to read from stdin.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="Report profiles YAML path."
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate the payload but do not write it.",
    )
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def resolve_date_key(value: str) -> str:
    if value == "today":
        return datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit("--date must be 'today' or YYYY-MM-DD") from exc


def load_profile(config_path: Path, profile_name: str) -> dict[str, Any]:
    if not config_path.exists():
        raise SystemExit(f"Profile config does not exist: {config_path}")
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    profiles = payload.get("profiles") or {}
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        available = ", ".join(sorted(profiles)) or "(none)"
        raise SystemExit(f"Unknown profile '{profile_name}'. Available: {available}")
    return profile


def read_state_payload(state_file: str) -> Any:
    if state_file == "-":
        raw = sys.stdin.read()
    else:
        path = Path(state_file)
        if not path.exists():
            raise SystemExit(f"State file does not exist: {path}")
        raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise SystemExit("State payload is empty.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"State payload is not valid JSON: {exc}") from exc


def load_prior_state(con: Any, profile_name: str, date_key: str) -> dict[str, Any] | None:
    row = db_get_latest_report_state(con, profile_name, before_date_key=date_key)
    if not row:
        return None
    try:
        watchboard = json.loads(str(row.get("payload"))) if row.get("payload") else {}
    except json.JSONDecodeError:
        watchboard = {}
    return {"state_date_key": str(row.get("date_key")), "watchboard": watchboard}


def validate(
    payload: Any, profile: dict[str, Any], prior: dict[str, Any] | None
) -> tuple[list[str], list[str]]:
    """Structural validation only — never judges analytical content."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(payload, dict):
        return ["payload must be a JSON object"], warnings

    for field in ENVELOPE_REQUIRED:
        if field not in payload or payload.get(field) in (None, ""):
            errors.append(f"missing required envelope field: {field}")

    if not payload.get("falsifiers"):
        warnings.append(
            "no 'falsifiers' field — add a falsifiable condition so the next day "
            "can challenge today's call instead of anchoring to it"
        )

    if not profile.get("state_enabled"):
        warnings.append(
            f"profile '{profile.get('title') or '?'}' has state_enabled falsy; "
            "saving anyway"
        )

    raw_items = payload.get("tracking_items")
    if raw_items is not None and not isinstance(raw_items, list):
        errors.append("tracking_items must be a list")
        raw_items = []
    items = raw_items or []
    seen_ids: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"tracking_items[{idx}] is not an object")
            continue
        tid = item.get("id")
        if not tid:
            errors.append(f"tracking_items[{idx}] missing id")
        else:
            if tid in seen_ids:
                errors.append(f"duplicate tracking_item id: {tid}")
            seen_ids.add(str(tid))
        status = item.get("status")
        if status not in ALLOWED_STATUS:
            errors.append(
                f"tracking_item {item.get('id') or idx} has invalid status "
                f"{status!r} (allowed: {sorted(ALLOWED_STATUS)})"
            )

    nodes = payload.get("next_nodes")
    if nodes is not None and not isinstance(nodes, list):
        errors.append("next_nodes must be a list")

    schema = profile.get("state_schema") or {}
    frame = payload.get("frame")
    if schema:
        if frame is None:
            frame = {}
        if not isinstance(frame, dict):
            errors.append("frame must be an object")
            frame = {}
        for field, rule in schema.items():
            rule = rule or {}
            present = field in frame and frame.get(field) not in (None, "")
            if rule.get("required") and not present:
                errors.append(f"frame missing required field: {field}")
            if not present:
                continue
            value = frame.get(field)
            enum = rule.get("enum")
            if enum and value not in enum:
                errors.append(f"frame.{field}={value!r} not in {enum}")
            ftype = rule.get("type")
            if ftype == "list" and not isinstance(value, list):
                errors.append(f"frame.{field} must be a list")
            if ftype == "map" and not isinstance(value, dict):
                errors.append(f"frame.{field} must be a map")
            target_sum = rule.get("sum")
            if target_sum is not None and isinstance(value, dict):
                try:
                    total = sum(float(v) for v in value.values())
                except (TypeError, ValueError):
                    errors.append(f"frame.{field} values must be numeric to sum")
                else:
                    if abs(total - float(target_sum)) > 0.5:
                        errors.append(
                            f"frame.{field} sums to {total:g}, expected {target_sum}"
                        )

    # Silent-drop guard: every prior open item must be reconciled (appear by id).
    if prior:
        prior_wb = prior.get("watchboard") or {}
        prior_open = [
            str(it.get("id"))
            for it in (prior_wb.get("tracking_items") or [])
            if isinstance(it, dict) and it.get("status") == "open" and it.get("id")
        ]
        for pid in prior_open:
            if pid not in seen_ids:
                errors.append(
                    f"prior open tracking_item {pid} is missing from today's "
                    "watchboard — reconcile it (confirm/dismiss/expire) or carry it "
                    "forward as open"
                )

    return errors, warnings


def main() -> int:
    args = parse_args()
    date_key = resolve_date_key(args.date)
    profile = load_profile(Path(args.config), args.profile)
    payload = read_state_payload(args.state_file)

    with get_connection(str(Path(args.db))) as con:
        init_news_schema(con)
        if not table_exists(con, "report_state"):
            raise SystemExit(
                "report_state table missing. Run init_alpha_data.sql for PostgreSQL, "
                "or let SQLite init the schema first."
            )
        prior = load_prior_state(con, args.profile, date_key)

        # Deterministic fields the script owns (not analytical judgement).
        if isinstance(payload, dict):
            payload.setdefault("as_of", date_key)
            if not payload.get("carried_from") and prior:
                payload["carried_from"] = prior.get("state_date_key")

        errors, warnings = validate(payload, profile, prior)
        report: dict[str, Any] = {
            "profile": args.profile,
            "date_key": date_key,
            "carried_from": payload.get("carried_from") if isinstance(payload, dict) else None,
            "warnings": warnings,
            "errors": errors,
        }

        if errors:
            report["status"] = "invalid"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1

        if args.check_only:
            report["status"] = "valid (check-only, not written)"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        db_write_report_state(
            con,
            args.profile,
            date_key,
            json.dumps(payload, ensure_ascii=False, default=str),
            now_iso(),
        )
        report["status"] = "written"
        report["open_items"] = sum(
            1
            for item in (payload.get("tracking_items") or [])
            if isinstance(item, dict) and item.get("status") == "open"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    raise SystemExit(main())
