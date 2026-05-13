#!/usr/bin/env python3
"""Compute authoritative daily calorie budgets into SQLite."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from health_db import ensure_runtime_schema


TIME_ZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_TARGET_DEFICIT = 500


def default_base_dir():
    return Path(__file__).resolve().parents[1]


def now_iso():
    return datetime.now(TIME_ZONE).isoformat(timespec="seconds")


def today_ymd():
    return datetime.now(TIME_ZONE).strftime("%Y-%m-%d")


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def activity_multiplier(profile):
    level = (profile["exercise_level"] or "").lower()
    if any(key in level for key in ("久坐", "sedentary")):
        return 1.2
    if any(key in level for key in ("轻度", "恢复", "light")):
        return 1.2
    if any(key in level for key in ("中等", "moderate")):
        return 1.375
    if any(key in level for key in ("高", "active")):
        return 1.55
    return 1.2


def estimate_bmr(profile):
    if profile and profile["bmr"]:
        return float(profile["bmr"])
    if profile and profile["weight_kg"] and profile["height_cm"] and profile["age"]:
        gender_offset = 5 if (profile["gender"] or "").lower() in ("male", "m", "男") else -161
        return round(10 * profile["weight_kg"] + 6.25 * profile["height_cm"] - 5 * profile["age"] + gender_offset)
    return 1544.0


def estimate_session_burn(row):
    if row["active_energy_kcal"] is not None:
        return float(row["active_energy_kcal"])
    label = f"{row['category'] or ''} {row['type'] or ''}"
    kcal_per_min = 5 if any(key in label for key in ("有氧", "快走", "跑步", "walking", "步行")) else 4
    return round((row["duration_min"] or 0) * kcal_per_min)


def daily_exercise_burn(conn, daily_log_id):
    rows = conn.execute(
        """
        SELECT * FROM exercise_sessions
        WHERE daily_log_id = ? AND status = 'completed'
        """,
        (daily_log_id,),
    ).fetchall()
    return sum(estimate_session_burn(row) for row in rows)


def active_target_deficit(conn):
    # Keep v1 deterministic: target_deficit is explicit and stable. A future
    # goal field can override this without changing downstream consumers.
    return DEFAULT_TARGET_DEFICIT


def calculate_for_daily(conn, daily, *, mode="manual", deficit=None):
    profile = conn.execute("SELECT * FROM profiles WHERE id = 1").fetchone()
    nutrition = conn.execute(
        "SELECT * FROM daily_nutrition_summary WHERE daily_log_id = ?",
        (daily["id"],),
    ).fetchone()
    bmr = estimate_bmr(profile)
    multiplier = activity_multiplier(profile) if profile else 1.2
    base_burn = round(bmr * multiplier)
    exercise_burn = daily_exercise_burn(conn, daily["id"])
    target_deficit = float(deficit if deficit is not None else active_target_deficit(conn))
    intake_limit = round(base_burn + exercise_burn - target_deficit)
    actual_intake = float(nutrition["total_calories"] if nutrition else 0)
    remaining = round(intake_limit - actual_intake)
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO daily_energy_budgets (
          daily_log_id, mode, bmr, activity_multiplier, base_burn_kcal,
          exercise_burn_kcal, target_deficit_kcal, intake_limit_kcal,
          actual_intake_kcal, remaining_kcal, computed_at, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(daily_log_id) DO UPDATE SET
          mode=excluded.mode,
          bmr=excluded.bmr,
          activity_multiplier=excluded.activity_multiplier,
          base_burn_kcal=excluded.base_burn_kcal,
          exercise_burn_kcal=excluded.exercise_burn_kcal,
          target_deficit_kcal=excluded.target_deficit_kcal,
          intake_limit_kcal=excluded.intake_limit_kcal,
          actual_intake_kcal=excluded.actual_intake_kcal,
          remaining_kcal=excluded.remaining_kcal,
          computed_at=excluded.computed_at,
          notes=excluded.notes
        """,
        (
            daily["id"],
            mode,
            bmr,
            multiplier,
            base_burn,
            exercise_burn,
            target_deficit,
            intake_limit,
            actual_intake,
            remaining,
            ts,
            "manual formula: BMR * activity_multiplier + exercise_burn - target_deficit",
        ),
    )
    return {
        "date": daily["date"],
        "intake_limit_kcal": intake_limit,
        "remaining_kcal": remaining,
        "exercise_burn_kcal": exercise_burn,
    }


def main():
    parser = argparse.ArgumentParser(description="Compute Health Butler daily calorie budgets")
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--date", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--mode", default="manual", choices=["manual"])
    parser.add_argument("--target-deficit-kcal", type=float, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).expanduser() if args.base_dir else default_base_dir()
    db_path = Path(args.db).expanduser() if args.db else base_dir / "health.db"
    schema_path = base_dir / "db" / "schema.sql"

    conn = connect(db_path)
    results = []
    try:
        with conn:
            conn.executescript(schema_path.read_text(encoding="utf-8"))
            ensure_runtime_schema(conn)
            if args.all:
                rows = conn.execute("SELECT * FROM daily_logs ORDER BY date").fetchall()
            else:
                date = args.date or today_ymd()
                rows = conn.execute("SELECT * FROM daily_logs WHERE date = ?", (date,)).fetchall()
                if not rows:
                    raise SystemExit(f"No daily log for {date}")
            for daily in rows:
                results.append(calculate_for_daily(conn, daily, mode=args.mode, deficit=args.target_deficit_kcal))
    finally:
        conn.close()

    if not args.quiet:
        for item in results:
            print(
                f"{item['date']}: limit={item['intake_limit_kcal']} kcal, "
                f"remaining={item['remaining_kcal']} kcal, exercise={item['exercise_burn_kcal']} kcal"
            )


if __name__ == "__main__":
    main()
