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
    ensure_connectable as db_ensure_connectable,
    get_connection,
    get_latest_framework_state as db_get_latest_framework_state,
    get_latest_report_state as db_get_latest_report_state,
    init_news_schema,
    table_exists,
    write_report_state as db_write_report_state,
)
from framework_loader import load_framework


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DB = Path("data/news_research.sqlite")
DEFAULT_CONFIG = Path("config/report_profiles.yaml")
ALLOWED_STATUS = {"open", "confirmed", "dismissed", "expired"}
SETTLED_STATUS = {"confirmed", "dismissed", "expired"}
ENVELOPE_REQUIRED = ["as_of", "regime", "tracking_items", "next_nodes"]
# Fields every tracking item must carry so it can be referenced across days.
ITEM_REQUIRED = ["opened", "statement"]
# Tolerate one-decimal rounding (e.g. 33.3*3=99.9) but reject real drift like
# 99.6 / 100.4.  Override per schema field with `sum_tol`.
DEFAULT_SUM_TOL = 0.2
# A probability bucket moving by at least this much (vs the prior watchboard)
# counts as a framework move that must be explained in `frame_change`.
FRAME_MOVE_EPS = 0.5
# Valid responses to a framework-challenge raised by the slow-thinking layer.
CHALLENGE_RESPONSE = {"accepted", "rejected", "pending"}


def _is_empty(value: Any) -> bool:
    """Treat None, blank strings and empty containers all as 'not provided'.

    A required field set to ``[]`` / ``{}`` / ``""`` is structurally as thin as
    one that is missing, so the validator must reject it the same way.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


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
    payload: Any,
    profile: dict[str, Any],
    prior: dict[str, Any] | None,
    framework_challenges: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Structural validation only — never judges analytical content."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(payload, dict):
        return ["payload must be a JSON object"], warnings

    for field in ENVELOPE_REQUIRED:
        if field not in payload or payload.get(field) in (None, ""):
            errors.append(f"missing required envelope field: {field}")

    falsifiers = payload.get("falsifiers")
    if _is_empty(falsifiers):
        errors.append(
            "missing 'falsifiers' — list at least one falsifiable condition so the "
            "next day can challenge today's call instead of anchoring to it"
        )
    elif not isinstance(falsifiers, list):
        errors.append("falsifiers must be a list")

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
    # Prior watchboard, used by three carry-forward checks below: per-item
    # "update" intent, the silent-drop guard, and the frame_change rationale.
    prior_wb = (prior or {}).get("watchboard") or {}
    prior_items = {
        str(it.get("id")): it
        for it in (prior_wb.get("tracking_items") or [])
        if isinstance(it, dict) and it.get("id")
    }
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
        label = item.get("id") or idx
        status = item.get("status")
        if status not in ALLOWED_STATUS:
            errors.append(
                f"tracking_item {label} has invalid status "
                f"{status!r} (allowed: {sorted(ALLOWED_STATUS)})"
            )
        for field in ITEM_REQUIRED:
            if _is_empty(item.get(field)):
                errors.append(f"tracking_item {label} missing {field}")
        # A settled item must record what happened; an open item leaves it null.
        if status in SETTLED_STATUS and _is_empty(item.get("resolution")):
            errors.append(
                f"tracking_item {label} is {status} but has no resolution — record "
                "what happened and which frame field it moved"
            )
        if _is_empty(item.get("expires_after")):
            warnings.append(
                f"tracking_item {label} has no expires_after — set one to keep the "
                "ledger from growing without bound"
            )
        # Intent on carried-open items: if an item carried from the prior period
        # stays open but its statement changed, that is a modification action and
        # must say what moved and why (settled items use `resolution` instead).
        prior_item = prior_items.get(str(tid)) if tid else None
        if (
            prior_item is not None
            and prior_item.get("status") == "open"
            and status == "open"
        ):
            prev_stmt = str(prior_item.get("statement") or "").strip()
            cur_stmt = str(item.get("statement") or "").strip()
            if prev_stmt and cur_stmt and prev_stmt != cur_stmt and _is_empty(
                item.get("update")
            ):
                errors.append(
                    f"tracking_item {label} 的 statement 较上一期有改动，但缺 'update'"
                    "（写明今日对这条做了什么动作、为何仍 open）"
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
            present = field in frame and not _is_empty(frame.get(field))
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
                tol = float(rule.get("sum_tol", DEFAULT_SUM_TOL))
                try:
                    total = sum(float(v) for v in value.values())
                except (TypeError, ValueError):
                    errors.append(f"frame.{field} values must be numeric to sum")
                else:
                    if abs(total - float(target_sum)) > tol:
                        errors.append(
                            f"frame.{field} sums to {total:g}, expected {target_sum} "
                            f"(tolerance ±{tol:g})"
                        )

    # Silent-drop guard: every prior open item must be reconciled (appear by id),
    # OR be explicitly accounted for by a framework_switch (migrate/retire/promote)
    # on a changeover day — see framework_governance.md §7.
    if prior:
        switch = payload.get("framework_switch") if isinstance(payload, dict) else None
        switched_ids: set[str] = set()
        if isinstance(switch, dict):
            for field in ("migrated", "retired", "promoted"):
                vals = switch.get(field)
                if isinstance(vals, list):
                    switched_ids.update(str(v) for v in vals)
        prior_open = [
            pid for pid, it in prior_items.items() if it.get("status") == "open"
        ]
        for pid in prior_open:
            if pid not in seen_ids and pid not in switched_ids:
                errors.append(
                    f"prior open tracking_item {pid} is missing from today's "
                    "watchboard — reconcile it (confirm/dismiss/expire), carry it "
                    "forward as open, or account for it in framework_switch on a "
                    "changeover day"
                )

        # Framework-move intent: if path or any probability bucket moved vs the
        # prior watchboard, the day must record *why* in `frame_change`.
        cur_frame = payload.get("frame") or {}
        prior_frame = prior_wb.get("frame") or {}
        if isinstance(cur_frame, dict) and isinstance(prior_frame, dict) and prior_frame:
            moved: list[str] = []
            if prior_frame.get("path") != cur_frame.get("path"):
                moved.append("path")
            pp = prior_frame.get("probabilities") or {}
            cp = cur_frame.get("probabilities") or {}
            if isinstance(pp, dict) and isinstance(cp, dict) and (pp or cp):
                try:
                    max_delta = max(
                        abs(float(cp.get(k, 0)) - float(pp.get(k, 0)))
                        for k in set(pp) | set(cp)
                    )
                except (TypeError, ValueError):
                    max_delta = 0.0
                if max_delta > FRAME_MOVE_EPS:
                    moved.append("probabilities")
            if moved and _is_empty(payload.get("frame_change")):
                errors.append(
                    f"框架发生移动（{'/'.join(moved)}）但缺 'frame_change' 说明"
                    "（写明 path/概率/权重为何这么挪）"
                )

    # challenge_responses：framework_state 的未决框架挑战必须被本期 watchboard 逐条
    # 回应（accepted/rejected/pending），与 tracking_item 的 silent-drop guard 同构。
    # 格式校验与"有无未决挑战"无关，始终对存在的 challenge_responses 跑一遍。
    open_challenge_ids = [
        str(ch.get("id"))
        for ch in (framework_challenges or [])
        if isinstance(ch, dict) and ch.get("id")
    ]
    raw_responses = payload.get("challenge_responses")
    responded: set[str] = set()
    if raw_responses is not None:
        if not isinstance(raw_responses, list):
            errors.append("challenge_responses must be a list")
        else:
            for idx, resp in enumerate(raw_responses):
                if not isinstance(resp, dict):
                    errors.append(f"challenge_responses[{idx}] is not an object")
                    continue
                cid = resp.get("challenge_id")
                if _is_empty(cid):
                    errors.append(f"challenge_responses[{idx}] missing challenge_id")
                    continue
                if str(cid) in responded:
                    errors.append(f"challenge_responses 重复 challenge_id: {cid}")
                    continue
                responded.add(str(cid))
                if resp.get("response") not in CHALLENGE_RESPONSE:
                    errors.append(
                        f"challenge_response {cid} 的 response={resp.get('response')!r} "
                        f"不在 {sorted(CHALLENGE_RESPONSE)}"
                    )
                if _is_empty(resp.get("note")):
                    errors.append(
                        f"challenge_response {cid} 缺 note（采纳怎么改 / 驳回凭什么 / pending 为何观察）"
                    )
    # 覆盖校验：每条未决挑战必须被回应（silent-drop 同构）。
    for cid in open_challenge_ids:
        if cid not in responded:
            errors.append(
                f"未决框架挑战 {cid} 未在 challenge_responses 回应 —— 慢思考的挑战"
                "不能被无视，必须 accepted/rejected/pending 逐条回应"
            )

    # framework_switch：换代日的迁移留痕 + 完整覆盖校验（见 framework_governance.md §7）。
    switch = payload.get("framework_switch")
    if not _is_empty(switch):
        if not isinstance(switch, dict):
            errors.append("framework_switch must be an object")
        else:
            for field in ("from", "to", "rationale"):
                if _is_empty(switch.get(field)):
                    errors.append(
                        f"framework_switch 缺 {field}（换代须留痕：从哪版→哪版、为什么）"
                    )
            switch_ids: dict[str, set[str]] = {}
            for field in ("migrated", "retired", "promoted"):
                val = switch.get(field)
                if val is not None and not isinstance(val, list):
                    errors.append(f"framework_switch.{field} must be a list")
                    switch_ids[field] = set()
                else:
                    switch_ids[field] = {str(v) for v in (val or [])}
            # 完整覆盖（须有上一期）：每个提及的 id 必须是上一期 open 项；retire 的
            # 不应保留在新台账，migrate/promote 的须按原 id 在新台账落实。
            if prior:
                prior_open_ids = {
                    pid for pid, it in prior_items.items() if it.get("status") == "open"
                }
                for sid in switch_ids["migrated"] | switch_ids["retired"] | switch_ids["promoted"]:
                    if sid not in prior_open_ids:
                        errors.append(
                            f"framework_switch 提及的 {sid} 不是上一期 open 跟踪项"
                        )
                for rid in switch_ids["retired"]:
                    if rid in seen_ids:
                        errors.append(
                            f"framework_switch.retired 的 {rid} 仍出现在 tracking_items"
                            "（退役项不应保留）"
                        )
                for mid in switch_ids["migrated"] | switch_ids["promoted"]:
                    if mid not in seen_ids:
                        errors.append(
                            f"framework_switch 的 {mid} 标为迁移/升级但未在新 "
                            "tracking_items 出现（须保留原 id 落实迁移）"
                        )

    return errors, warnings


def _load_open_challenges(
    con: Any, profile_name: str, date_key: str
) -> list[dict[str, Any]]:
    """Open framework-challenges from the latest review strictly before date_key.

    Mirrors prepare_report_data's injection so the challenges the model was shown
    in the packet are exactly the ones it must answer here in challenge_responses.
    """
    row = db_get_latest_framework_state(con, profile_name, before_date_key=date_key)
    if not row:
        return []
    try:
        payload = json.loads(str(row.get("payload") or "{}"))
    except json.JSONDecodeError:
        return []
    return [
        ch
        for ch in (payload.get("challenges") or [])
        if isinstance(ch, dict) and ch.get("status") == "open"
    ]


def main() -> int:
    args = parse_args()
    date_key = resolve_date_key(args.date)
    profile = load_profile(Path(args.config), args.profile)
    # Framework single source of truth: a profile's framework.md machine region
    # supersedes the legacy report_profiles.yaml state_schema; fall back to the
    # YAML when no framework.md exists yet so un-migrated profiles keep working.
    framework = load_framework(args.profile)
    schema_source = "report_profiles.yaml:state_schema"
    if framework and framework.get("frame_schema"):
        profile = {**profile, "state_schema": framework["frame_schema"]}
        schema_source = f"framework.md:{framework.get('framework_version')}"
    payload = read_state_payload(args.state_file)

    db_path = str(Path(args.db))
    try:
        db_ensure_connectable(db_path)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    with get_connection(db_path) as con:
        init_news_schema(con)
        if not table_exists(con, "report_state"):
            raise SystemExit(
                "report_state table missing. Run init_alpha_data.sql for PostgreSQL, "
                "or let SQLite init the schema first."
            )
        prior = load_prior_state(con, args.profile, date_key)
        # Open framework-challenges the day must answer — only when this profile
        # has a framework.md (same gate as prepare's injection), so save never
        # demands a response to a challenge the packet never showed the model.
        framework_challenges = (
            _load_open_challenges(con, args.profile, date_key) if framework else []
        )

        # Deterministic fields the script owns (not analytical judgement).
        if isinstance(payload, dict):
            payload.setdefault("as_of", date_key)
            if not payload.get("carried_from") and prior:
                payload["carried_from"] = prior.get("state_date_key")

        errors, warnings = validate(payload, profile, prior, framework_challenges)
        report: dict[str, Any] = {
            "profile": args.profile,
            "schema_source": schema_source,
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
