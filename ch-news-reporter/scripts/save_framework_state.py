#!/usr/bin/env python3
"""Persist and validate a framework-governance review (framework_state).

The slow-thinking layer (see references/reports/framework_governance.md) audits
the analysis framework itself: whether the current regime still holds and, on a
regime shift, proposes a new framework version.  This script does only
deterministic I/O and *structural* validation — every judgement (the regime
verdict, whether to switch, what to migrate) stays with the model + human that
authored the payload.  The per-profile framework.md remains the single source of
truth for the *current* framework; this table is only the governance audit log.

Usage:
    # read review JSON from stdin
    cat review.json | python scripts/save_framework_state.py \
        --profile iran_dynamic --date today --state-file -

    # validate only, do not write
    python scripts/save_framework_state.py --profile iran_dynamic --date today \
        --state-file review.json --check-only
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

from db_adapter import (
    ensure_connectable as db_ensure_connectable,
    get_connection,
    get_latest_framework_state as db_get_latest_framework_state,
    init_news_schema,
    table_exists,
    write_framework_state as db_write_framework_state,
)
from framework_loader import load_framework


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DB = Path("data/news_research.sqlite")

# regime 诊断三档（framework_governance.md §5）。
VERDICT_VALUES = {"稳定", "漂移", "质变"}
# 换代提案的生命周期。
PROPOSAL_STATUS = {"proposed", "approved", "applied", "rejected"}
# 框架级挑战台账状态。
CHALLENGE_STATUS = {"open", "accepted", "rejected", "dropped"}
ENVELOPE_REQUIRED = ["review_date", "framework_version", "regime_verdict"]


def _is_empty(value: Any) -> bool:
    """None / blank string / empty container all count as 'not provided'."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Persist/validate a framework-governance review into framework_state."
    )
    parser.add_argument("--profile", required=True, help="Profile name, e.g. iran_dynamic.")
    parser.add_argument("--date", default="today", help="today or YYYY-MM-DD (review date).")
    parser.add_argument(
        "--state-file",
        required=True,
        help="Path to review JSON, or '-' to read from stdin.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
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


def read_payload(state_file: str) -> Any:
    if state_file == "-":
        raw = sys.stdin.read()
    else:
        path = Path(state_file)
        if not path.exists():
            raise SystemExit(f"State file does not exist: {path}")
        raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise SystemExit("Review payload is empty.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Review payload is not valid JSON: {exc}") from exc


def validate(
    payload: Any, framework: dict[str, Any] | None
) -> tuple[list[str], list[str]]:
    """Structural validation only — never judges whether the verdict is correct."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(payload, dict):
        return ["payload must be a JSON object"], warnings

    for field in ENVELOPE_REQUIRED:
        if _is_empty(payload.get(field)):
            errors.append(f"missing required envelope field: {field}")

    verdict = payload.get("regime_verdict")
    if verdict is not None and verdict not in VERDICT_VALUES:
        errors.append(f"regime_verdict={verdict!r} not in {sorted(VERDICT_VALUES)}")

    status = payload.get("status")
    if status is not None and status not in PROPOSAL_STATUS:
        errors.append(f"status={status!r} not in {sorted(PROPOSAL_STATUS)}")

    # 质变（regime shift）必须带换代提案；稳定/漂移带 proposal 多半是填错。
    proposal = payload.get("proposal")
    if verdict == "质变":
        if _is_empty(proposal):
            errors.append(
                "regime_verdict 为 '质变' 但缺 proposal —— 换代必须给出新框架草案"
                "（至少 new_version + migration_plan）"
            )
        elif not isinstance(proposal, dict):
            errors.append("proposal 必须是对象（含 new_version + migration_plan）")
        else:
            if _is_empty(proposal.get("new_version")):
                errors.append("proposal 缺 new_version（新框架版本号，如 iran-v2-wartime）")
            if _is_empty(proposal.get("migration_plan")):
                errors.append(
                    "proposal 缺 migration_plan（旧 open 跟踪项的 migrate/retire/promote 计划）"
                )
    elif not _is_empty(proposal):
        warnings.append(
            f"regime_verdict 为 '{verdict}' 却带了 proposal —— 非质变通常不提换代，确认是否填错"
        )

    # 质变判据应扎在 framework.md 的 invalidation_triggers 命中上（governance.md §5）。
    if verdict == "质变" and _is_empty(payload.get("triggers_hit")):
        warnings.append(
            "regime_verdict 为 '质变' 但 triggers_hit 为空 —— 换代判据应锚定 "
            "framework.md 的 invalidation_triggers 命中，建议补"
        )

    # challenges 台账：id/statement/status 必填，id 唯一，status 合法。
    challenges = payload.get("challenges")
    if challenges is not None:
        if not isinstance(challenges, list):
            errors.append("challenges must be a list")
        else:
            seen: set[str] = set()
            for idx, ch in enumerate(challenges):
                if not isinstance(ch, dict):
                    errors.append(f"challenges[{idx}] is not an object")
                    continue
                cid = ch.get("id")
                label = cid or idx
                if _is_empty(cid):
                    errors.append(f"challenges[{idx}] missing id")
                else:
                    cid_key = str(cid)
                    if cid_key in seen:
                        errors.append(f"duplicate challenge id: {cid}")
                    else:
                        seen.add(cid_key)
                if _is_empty(ch.get("statement")):
                    errors.append(f"challenge {label} missing statement")
                cst = ch.get("status")
                if cst not in CHALLENGE_STATUS:
                    errors.append(
                        f"challenge {label} has invalid status {cst!r} "
                        f"(allowed: {sorted(CHALLENGE_STATUS)})"
                    )

    # framework_version 应对上当前在役框架（审的是当前框架，不是某个旧版本）。
    if framework and not _is_empty(payload.get("framework_version")):
        current = framework.get("framework_version")
        if current and payload.get("framework_version") != current:
            warnings.append(
                f"review 的 framework_version={payload.get('framework_version')!r} "
                f"与当前在役 framework.md 的 {current!r} 不一致 —— 确认不是在审旧版本"
            )

    if _is_empty(payload.get("next_review_due")):
        warnings.append("缺 next_review_due —— 建议给下次体检日期（默认节奏：距上次 review >3 天触发）")

    return errors, warnings


def main() -> int:
    args = parse_args()
    date_key = resolve_date_key(args.date)
    framework = load_framework(args.profile)
    payload = read_payload(args.state_file)

    db_path = str(Path(args.db))
    try:
        db_ensure_connectable(db_path)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    with get_connection(db_path) as con:
        init_news_schema(con)
        if not table_exists(con, "framework_state"):
            raise SystemExit(
                "framework_state table missing. Run init_alpha_data.sql for "
                "PostgreSQL, or let SQLite init the schema first."
            )
        # Most recent prior review (not used for hard checks yet; exposed so the
        # caller/report can show what the last verdict was).
        prior = db_get_latest_framework_state(con, args.profile, before_date_key=date_key)

        # Deterministic fields the script owns (not analytical judgement).
        if isinstance(payload, dict):
            payload.setdefault("review_date", date_key)
            if framework and _is_empty(payload.get("framework_version")):
                payload["framework_version"] = framework.get("framework_version")

        errors, warnings = validate(payload, framework)
        report: dict[str, Any] = {
            "profile": args.profile,
            "review_date_key": date_key,
            "framework_version": payload.get("framework_version")
            if isinstance(payload, dict)
            else None,
            "regime_verdict": payload.get("regime_verdict")
            if isinstance(payload, dict)
            else None,
            "prior_review": (prior or {}).get("review_date_key"),
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

        db_write_framework_state(
            con,
            args.profile,
            date_key,
            json.dumps(payload, ensure_ascii=False, default=str),
            now_iso(),
        )
        report["status"] = "written"
        report["open_challenges"] = sum(
            1
            for ch in (payload.get("challenges") or [])
            if isinstance(ch, dict) and ch.get("status") == "open"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    raise SystemExit(main())
