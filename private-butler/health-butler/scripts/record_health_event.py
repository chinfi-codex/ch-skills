#!/usr/bin/env python3
"""
Write common Health Butler events directly into SQLite, then refresh derived UI data.

Supported event types:
- water
- weight
- medication
- exercise
- meal
"""

import argparse
import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from health_db import ensure_runtime_schema


TIME_ZONE = ZoneInfo("Asia/Shanghai")
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def default_base_dir():
    return Path(__file__).resolve().parents[1]


def now_iso():
    return datetime.now(TIME_ZONE).isoformat(timespec="seconds")


def today_ymd():
    return datetime.now(TIME_ZONE).strftime("%Y-%m-%d")


def weekday_cn(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return WEEKDAYS[dt.weekday()]


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn, schema_path):
    conn.executescript(schema_path.read_text(encoding="utf-8"))


def ensure_daily_log(conn, date_str):
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO daily_logs (date, weekday, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET updated_at=excluded.updated_at
        """,
        (date_str, weekday_cn(date_str), ts, ts),
    )
    row = conn.execute("SELECT id FROM daily_logs WHERE date = ?", (date_str,)).fetchone()
    ensure_targets(conn, row["id"])
    ensure_health_markers(conn, row["id"])
    return row["id"]


def ensure_targets(conn, daily_log_id):
    ts = now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO daily_nutrition_targets (
          daily_log_id, target_calories, water_target_ml, created_at, updated_at
        )
        VALUES (?, 1600, 2000, ?, ?)
        """,
        (daily_log_id, ts, ts),
    )


def ensure_health_markers(conn, daily_log_id):
    ts = now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO health_markers (
          daily_log_id, kegel_sets, kegel_done, created_at, updated_at
        )
        VALUES (?, 0, 0, ?, ?)
        """,
        (daily_log_id, ts, ts),
    )


def add_note(conn, daily_log_id, note):
    if not note:
        return
    ts = now_iso()
    sort_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM daily_notes WHERE daily_log_id = ?",
        (daily_log_id,),
    ).fetchone()["next_order"]
    conn.execute(
        """
        INSERT INTO daily_notes (daily_log_id, note, sort_order, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (daily_log_id, note, sort_order, ts, ts),
    )


def touch_daily(conn, daily_log_id):
    conn.execute("UPDATE daily_logs SET updated_at = ? WHERE id = ?", (now_iso(), daily_log_id))


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def validate_non_negative(value, label):
    if value is not None:
        require(value >= 0, f"{label} must be >= 0")


def estimate_exercise_burn(exercise_type=None, category=None, duration_min=0):
    label = f"{category or ''} {exercise_type or ''}"
    kcal_per_min = 5 if any(key in label for key in ("有氧", "快走", "跑步", "walking", "步行")) else 4
    return round((duration_min or 0) * kcal_per_min)


def record_water(conn, daily_log_id, args):
    require(args.amount_ml > 0, "amount_ml must be > 0")
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO water_events (daily_log_id, event_time, amount_ml, source, notes, created_at, updated_at)
        VALUES (?, ?, ?, 'manual', ?, ?, ?)
        """,
        (daily_log_id, args.time or ts, args.amount_ml, args.note, ts, ts),
    )
    add_note(conn, daily_log_id, args.note)


def latest_target_weight(conn):
    row = conn.execute(
        "SELECT target_weight_kg FROM goals WHERE profile_id = 1 AND active = 1 AND target_weight_kg IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        return row["target_weight_kg"]
    row = conn.execute(
        "SELECT target_weight_kg FROM body_measurements WHERE target_weight_kg IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["target_weight_kg"] if row else None


def profile_height_m(conn):
    row = conn.execute("SELECT height_cm FROM profiles WHERE id = 1").fetchone()
    if row and row["height_cm"]:
        return row["height_cm"] / 100
    return None


def record_weight(conn, daily_log_id, args):
    require(args.weight_kg > 0, "weight_kg must be > 0")
    if args.body_fat_pct is not None:
        require(0 <= args.body_fat_pct <= 100, "body_fat_pct must be between 0 and 100")
    if args.bmi is not None:
        require(args.bmi > 0, "bmi must be > 0")
    if args.target_weight_kg is not None:
        require(args.target_weight_kg > 0, "target_weight_kg must be > 0")
    ts = now_iso()
    height_m = profile_height_m(conn)
    bmi = args.bmi
    if bmi is None and height_m and args.weight_kg:
        bmi = round(args.weight_kg / (height_m * height_m), 1)
    target_weight = args.target_weight_kg if args.target_weight_kg is not None else latest_target_weight(conn)
    conn.execute(
        """
        INSERT INTO body_measurements (
          daily_log_id, measurement_type, measured_at, weight_kg, body_fat_pct,
          bmi, target_weight_kg, notes, created_at, updated_at
        )
        VALUES (?, 'morning', ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(daily_log_id, measurement_type) DO UPDATE SET
          measured_at=excluded.measured_at,
          weight_kg=excluded.weight_kg,
          body_fat_pct=excluded.body_fat_pct,
          bmi=excluded.bmi,
          target_weight_kg=excluded.target_weight_kg,
          notes=excluded.notes,
          updated_at=excluded.updated_at
        """,
        (daily_log_id, args.time or ts, args.weight_kg, args.body_fat_pct, bmi, target_weight, args.note, ts, ts),
    )
    # 同步更新 profiles 表中的体重和 BMI
    conn.execute(
        """
        UPDATE profiles SET weight_kg = ?, bmi = ?, updated_at = ? WHERE id = 1
        """,
        (args.weight_kg, bmi, ts),
    )
    add_note(conn, daily_log_id, args.note)


def ensure_medication(conn, drug_name, scheduled_time=None):
    name = "entecavir" if "恩替卡韦" in drug_name else drug_name
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO medications (name, display_name, default_scheduled_time, active, created_at, updated_at)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
          display_name=excluded.display_name,
          default_scheduled_time=COALESCE(excluded.default_scheduled_time, medications.default_scheduled_time),
          updated_at=excluded.updated_at
        """,
        (name, drug_name, scheduled_time, ts, ts),
    )
    return conn.execute("SELECT id FROM medications WHERE name = ?", (name,)).fetchone()["id"]


def record_medication(conn, daily_log_id, args):
    ts = now_iso()
    med_id = ensure_medication(conn, args.drug_name, args.scheduled_time)
    conn.execute(
        """
        INSERT INTO medication_events (
          daily_log_id, medication_id, scheduled_time, taken_time, taken,
          empty_stomach, notes, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(daily_log_id, medication_id, scheduled_time) DO UPDATE SET
          taken_time=excluded.taken_time,
          taken=excluded.taken,
          empty_stomach=excluded.empty_stomach,
          notes=excluded.notes,
          updated_at=excluded.updated_at
        """,
        (
            daily_log_id,
            med_id,
            args.scheduled_time,
            args.taken_time or (ts if args.taken else None),
            1 if args.taken else 0,
            1 if args.empty_stomach else 0,
            args.note,
            ts,
            ts,
        ),
    )
    add_note(conn, daily_log_id, args.note)


def record_exercise(conn, daily_log_id, args):
    if args.duration_min is not None:
        require(args.duration_min >= 0, "duration_min must be >= 0")
    if args.rpe is not None:
        require(1 <= args.rpe <= 10, "rpe must be between 1 and 10")
    for item in args.items or []:
        require(isinstance(item, (dict, str)), "exercise items must be strings or objects")
        if isinstance(item, dict):
            if item.get("sets") is not None:
                require(item["sets"] >= 0, "exercise item sets must be >= 0")
            validate_non_negative(item.get("unit_value"), "exercise item unit_value")
            validate_non_negative(item.get("duration_min_estimated"), "exercise item duration_min_estimated")
            validate_non_negative(item.get("calories_burned"), "exercise item calories_burned")
    ts = now_iso()
    status = args.status or ("completed" if args.done else "planned")
    item_burn = sum((item.get("calories_burned") or 0) for item in args.items or [] if isinstance(item, dict))
    active_energy = args.active_energy_kcal
    burn_source = args.burn_source
    if active_energy is None and item_burn:
        active_energy = item_burn
        burn_source = burn_source or "manual_estimate"
    if active_energy is None and args.duration_min:
        active_energy = estimate_exercise_burn(args.exercise_type, args.category, args.duration_min)
        burn_source = burn_source or "manual_estimate"
    if active_energy is not None:
        validate_non_negative(active_energy, "active_energy_kcal")
    burn_source = burn_source or ("manual_estimate" if active_energy is not None else None)
    cur = conn.execute(
        """
        INSERT INTO exercise_sessions (
          daily_log_id, planned_instance_id, type, category, start_time, duration_min,
          active_energy_kcal, burn_source, unit_summary, rpe, status, notes,
          created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            daily_log_id,
            args.planned_instance_id,
            args.exercise_type,
            args.category,
            args.time,
            args.duration_min or 0,
            active_energy,
            burn_source,
            args.unit_summary,
            args.rpe,
            status,
            args.note,
            ts,
            ts,
        ),
    )
    session_id = cur.lastrowid
    for idx, item in enumerate(args.items or []):
        if isinstance(item, str):
            item = {"name": item}
        conn.execute(
            """
            INSERT INTO exercise_items (
              exercise_session_id, name, sets, reps, unit_type, unit_value,
              duration_min_estimated, calories_burned, done, sort_order,
              created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                item.get("name", "未命名运动"),
                item.get("sets"),
                item.get("reps"),
                item.get("unit_type"),
                item.get("unit_value"),
                item.get("duration_min_estimated"),
                item.get("calories_burned"),
                1 if item.get("done", args.done) else 0,
                idx,
                ts,
                ts,
            ),
        )
    add_note(conn, daily_log_id, args.note)


def record_meal(conn, daily_log_id, args):
    require(isinstance(args.items, list), "items-json must be a JSON array")
    require(len(args.items) > 0 or args.status in ("pending", "planned", "skipped"), "completed meals must include at least one item")
    for item in args.items:
        require(isinstance(item, dict), "meal items must be JSON objects")
        require(str(item.get("name", "")).strip(), "meal item name is required")
        validate_non_negative(item.get("calories", 0), "meal item calories")
        validate_non_negative(item.get("protein_g"), "meal item protein_g")
        validate_non_negative(item.get("carbs_g"), "meal item carbs_g")
        validate_non_negative(item.get("fat_g"), "meal item fat_g")
        validate_non_negative(item.get("water_ml"), "meal item water_ml")
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO meals (daily_log_id, meal_type, meal_time, status, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(daily_log_id, meal_type) DO UPDATE SET
          meal_time=excluded.meal_time,
          status=excluded.status,
          notes=excluded.notes,
          updated_at=excluded.updated_at
        """,
        (daily_log_id, args.meal_type, args.time, args.status or "completed", args.note, ts, ts),
    )
    meal_id = conn.execute(
        "SELECT id FROM meals WHERE daily_log_id = ? AND meal_type = ?",
        (daily_log_id, args.meal_type),
    ).fetchone()["id"]
    if args.replace:
        conn.execute("DELETE FROM meal_items WHERE meal_id = ?", (meal_id,))
    next_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM meal_items WHERE meal_id = ?",
        (meal_id,),
    ).fetchone()["next_order"]
    for idx, item in enumerate(args.items or []):
        conn.execute(
            """
            INSERT INTO meal_items (
              meal_id, name, amount_text, calories, protein_g, carbs_g, fat_g,
              water_ml, calorie_source, water_source, estimate_confidence,
              sort_order, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meal_id,
                item.get("name", "未命名食物"),
                item.get("amount"),
                item.get("calories", 0),
                item.get("protein_g"),
                item.get("carbs_g"),
                item.get("fat_g"),
                item.get("water_ml"),
                item.get("calorie_source", "manual_estimate"),
                item.get("water_source"),
                item.get("estimate_confidence", "medium"),
                next_order + idx,
                ts,
                ts,
            ),
        )
    add_note(conn, daily_log_id, args.note)


def refresh_outputs(base_dir, skip_refresh=False):
    if skip_refresh:
        return
    subprocess.run([str(base_dir / "scripts" / "calculate_daily_budget.py"), "--base-dir", str(base_dir), "--all", "--quiet"], check=True)
    subprocess.run([str(base_dir / "scripts" / "export_dashboard_data.py"), "--base-dir", str(base_dir)], check=True)
    subprocess.run([str(base_dir / "scripts" / "refresh_dashboard_data.py"), "--base-dir", str(base_dir)], check=True)
    chief_dir = base_dir.parents[1] / "skills" / "chief-butler"
    collect_script = chief_dir / "scripts" / "collect_status.py"
    if collect_script.exists():
        subprocess.run([str(collect_script), "--base-dir", str(chief_dir)], check=True, stdout=subprocess.DEVNULL)


def parse_items(value):
    if not value:
        return []
    return json.loads(value)


def main():
    parser = argparse.ArgumentParser(description="Record a Health Butler event into SQLite")
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--date", default=None)
    parser.add_argument("--time", default=None)
    parser.add_argument("--note", default=None)
    parser.add_argument("--skip-refresh", action="store_true")

    subparsers = parser.add_subparsers(dest="event_type", required=True)

    water = subparsers.add_parser("water")
    water.add_argument("--amount-ml", type=int, required=True)

    weight = subparsers.add_parser("weight")
    weight.add_argument("--weight-kg", type=float, required=True)
    weight.add_argument("--body-fat-pct", type=float)
    weight.add_argument("--bmi", type=float)
    weight.add_argument("--target-weight-kg", type=float)

    medication = subparsers.add_parser("medication")
    medication.add_argument("--drug-name", default="恩替卡韦（润众）")
    medication.add_argument("--scheduled-time", default="22:00")
    medication.add_argument("--taken-time")
    medication.add_argument("--taken", action="store_true")
    medication.add_argument("--empty-stomach", action="store_true")

    exercise = subparsers.add_parser("exercise")
    exercise.add_argument("--exercise-type")
    exercise.add_argument("--category")
    exercise.add_argument("--duration-min", type=int)
    exercise.add_argument("--active-energy-kcal", type=float)
    exercise.add_argument("--burn-source", choices=["manual_estimate", "user_reported", "future_apple", "apple", "unknown"])
    exercise.add_argument("--unit-summary")
    exercise.add_argument("--planned-instance-id", type=int)
    exercise.add_argument("--rpe", type=int)
    exercise.add_argument("--status", choices=["pending", "planned", "partial", "completed", "skipped"])
    exercise.add_argument("--done", action="store_true")
    exercise.add_argument("--items-json", dest="items", type=parse_items)

    meal = subparsers.add_parser("meal")
    meal.add_argument("--meal-type", choices=["breakfast", "lunch", "snack", "dinner"], required=True)
    meal.add_argument("--status", choices=["pending", "planned", "completed", "skipped"], default="completed")
    meal.add_argument("--items-json", dest="items", type=parse_items, required=True)
    meal.add_argument("--replace", action="store_true")

    args = parser.parse_args()
    base_dir = Path(args.base_dir).expanduser() if args.base_dir else default_base_dir()
    db_path = base_dir / "health.db"
    schema_path = base_dir / "db" / "schema.sql"

    conn = connect(db_path)
    date_str = args.date or today_ymd()
    try:
        with conn:
            init_schema(conn, schema_path)
            ensure_runtime_schema(conn)
            daily_log_id = ensure_daily_log(conn, date_str)
            if args.event_type == "water":
                record_water(conn, daily_log_id, args)
            elif args.event_type == "weight":
                record_weight(conn, daily_log_id, args)
            elif args.event_type == "medication":
                record_medication(conn, daily_log_id, args)
            elif args.event_type == "exercise":
                record_exercise(conn, daily_log_id, args)
            elif args.event_type == "meal":
                record_meal(conn, daily_log_id, args)
            touch_daily(conn, daily_log_id)
    finally:
        conn.close()

    refresh_outputs(base_dir, args.skip_refresh)
    print(f"Recorded {args.event_type} for {date_str} into {db_path}")


if __name__ == "__main__":
    main()
