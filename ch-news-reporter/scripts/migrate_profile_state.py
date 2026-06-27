#!/usr/bin/env python3
"""Rename report_state/framework_state rows from one profile to another."""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any

from db_adapter import (
    adapt_sql,
    ensure_connectable as db_ensure_connectable,
    get_connection,
    init_news_schema,
    placeholder,
    table_exists,
)


DEFAULT_DB = Path("data/news_research.sqlite")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move persisted report/framework state rows between profile names."
    )
    parser.add_argument("--from-profile", required=True, dest="from_profile")
    parser.add_argument("--to-profile", required=True, dest="to_profile")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Report planned changes and conflicts without updating rows.",
    )
    return parser.parse_args()


def fetch_values(con: Any, table: str, profile: str, date_col: str) -> list[str]:
    if not table_exists(con, table):
        return []
    ph = placeholder()
    sql = f"SELECT {date_col} FROM {table} WHERE profile = {ph} ORDER BY {date_col}"
    if hasattr(con, "execute"):
        cur = con.execute(sql, (profile,))
    else:
        cur = con.cursor()
        cur.execute(adapt_sql(sql), (profile,))
    rows = cur.fetchall()
    values: list[str] = []
    for row in rows:
        value = row[0] if not isinstance(row, dict) else row.get(date_col)
        values.append(str(value))
    return values


def update_profile(con: Any, table: str, from_profile: str, to_profile: str) -> int:
    if not table_exists(con, table):
        return 0
    ph = placeholder()
    sql = f"UPDATE {table} SET profile = {ph} WHERE profile = {ph}"
    params = (to_profile, from_profile)
    if hasattr(con, "execute"):
        cur = con.execute(sql, params)
    else:
        cur = con.cursor()
        cur.execute(adapt_sql(sql), params)
    return int(cur.rowcount or 0)


def main() -> int:
    args = parse_args()
    if args.from_profile == args.to_profile:
        raise SystemExit("--from-profile and --to-profile must differ")

    try:
        db_ensure_connectable(str(Path(args.db)))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report: dict[str, Any] = {
        "from_profile": args.from_profile,
        "to_profile": args.to_profile,
        "check_only": args.check_only,
        "tables": {},
        "conflicts": {},
    }

    with get_connection(str(Path(args.db))) as con:
        init_news_schema(con)
        specs = {
            "report_state": "date_key",
            "framework_state": "review_date_key",
        }
        for table, date_col in specs.items():
            source_dates = fetch_values(con, table, args.from_profile, date_col)
            target_dates = fetch_values(con, table, args.to_profile, date_col)
            conflicts = sorted(set(source_dates) & set(target_dates))
            report["tables"][table] = {
                "source_rows": len(source_dates),
                "target_rows": len(target_dates),
                "would_move": 0 if conflicts else len(source_dates),
            }
            if conflicts:
                report["conflicts"][table] = conflicts

        if report["conflicts"]:
            report["status"] = "blocked: target profile has same-date rows"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1

        if args.check_only:
            report["status"] = "valid (check-only, not written)"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        for table in specs:
            report["tables"][table]["moved"] = update_profile(
                con, table, args.from_profile, args.to_profile
            )
        report["status"] = "written"

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    raise SystemExit(main())
