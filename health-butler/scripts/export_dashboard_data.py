#!/usr/bin/env python3
"""
Export SQLite Health Butler data back to the dashboard-compatible daily JSON.
"""

import argparse
import json
import sqlite3
from pathlib import Path


def default_base_dir():
    return Path(__file__).resolve().parents[1]


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def one(conn, query, params=()):
    return conn.execute(query, params).fetchone()


def all_rows(conn, query, params=()):
    return conn.execute(query, params).fetchall()


def estimate_exercise_burn(exercise):
    duration_min = exercise["duration_min"] or 0
    label = f"{exercise['category'] or ''} {exercise['type'] or ''}"
    kcal_per_min = 5 if any(key in label for key in ("有氧", "快走", "跑步")) else 4
    return round(duration_min * kcal_per_min)


def calculate_bmi(weight_kg, height_cm, fallback=None):
    if weight_kg and height_cm:
        height_m = height_cm / 100
        if height_m > 0:
            return round(weight_kg / (height_m * height_m), 1)
    return fallback


def export_daily(conn, daily):
    daily_id = daily["id"]
    profile = one(conn, "SELECT height_cm FROM profiles WHERE id = 1")
    body = one(conn, "SELECT * FROM body_measurements WHERE daily_log_id = ? AND measurement_type = 'morning'", (daily_id,))
    nutrition = one(conn, "SELECT * FROM daily_nutrition_summary WHERE daily_log_id = ?", (daily_id,))
    water = one(conn, "SELECT * FROM daily_water_summary WHERE daily_log_id = ?", (daily_id,))
    targets = one(conn, "SELECT * FROM daily_nutrition_targets WHERE daily_log_id = ?", (daily_id,))
    markers = one(conn, "SELECT * FROM health_markers WHERE daily_log_id = ?", (daily_id,))
    exercise_rows = all_rows(conn, "SELECT * FROM exercise_sessions WHERE daily_log_id = ? ORDER BY id", (daily_id,))

    data = {
        "version": 2,
        "schema": "health-butler-daily-log",
        "date": daily["date"],
        "weekday": daily["weekday"],
        "profile_snapshot": {},
        "meals": {},
        "nutrition_summary": {
            "total_calories": nutrition["total_calories"] if nutrition else 0,
            "target_calories": nutrition["target_calories"] if nutrition else 1600,
            "remaining_calories": nutrition["remaining_calories"] if nutrition else 1600,
            "protein_g": nutrition["protein_g"] if nutrition and nutrition["protein_g"] is not None else None,
            "carbs_g": nutrition["carbs_g"] if nutrition and nutrition["carbs_g"] is not None else None,
            "fat_g": nutrition["fat_g"] if nutrition and nutrition["fat_g"] is not None else None,
            "water_ml": water["water_intake_ml"] if water else 0,
        },
        "exercise": {},
        "medication": {},
        "health_markers": {},
        "notes": [],
        "created_at": daily["created_at"],
        "updated_at": daily["updated_at"],
    }

    if body:
        height_cm = profile["height_cm"] if profile else None
        data["profile_snapshot"] = {
            "height_cm": height_cm,
            "weight_kg": body["weight_kg"],
            "body_fat_pct": body["body_fat_pct"],
            "target_weight_kg": body["target_weight_kg"],
            "bmi": calculate_bmi(body["weight_kg"], height_cm, body["bmi"]),
        }
        data["profile_snapshot"] = {k: v for k, v in data["profile_snapshot"].items() if v is not None}

    for meal in all_rows(conn, "SELECT * FROM meals WHERE daily_log_id = ? ORDER BY CASE meal_type WHEN 'breakfast' THEN 1 WHEN 'lunch' THEN 2 WHEN 'snack' THEN 3 WHEN 'dinner' THEN 4 ELSE 5 END", (daily_id,)):
        items = []
        total = 0
        for item in all_rows(conn, "SELECT * FROM meal_items WHERE meal_id = ? ORDER BY sort_order, id", (meal["id"],)):
            calories = item["calories"] or 0
            total += calories
            items.append({
                "name": item["name"],
                "amount": item["amount_text"],
                "calories": calories,
            })
        data["meals"][meal["meal_type"]] = {
            "time": meal["meal_time"],
            "items": items,
            "total_calories": total,
            "status": meal["status"],
            "notes": meal["notes"] or "",
        }

    if exercise_rows:
        items = []
        types = []
        categories = []
        statuses = []
        notes = []
        duration_min = 0
        active_energy_kcal = 0
        rpes = []
        for exercise in exercise_rows:
            if exercise["type"] and exercise["type"] not in types:
                types.append(exercise["type"])
            if exercise["category"] and exercise["category"] not in categories:
                categories.append(exercise["category"])
            statuses.append(exercise["status"])
            if exercise["notes"]:
                notes.append(exercise["notes"])
            duration_min += exercise["duration_min"] or 0
            active_energy_kcal += estimate_exercise_burn(exercise)
            if exercise["rpe"] is not None:
                rpes.append(exercise["rpe"])
            for item in all_rows(conn, "SELECT * FROM exercise_items WHERE exercise_session_id = ? ORDER BY sort_order, id", (exercise["id"],)):
                items.append({
                    "name": item["name"],
                    "sets": item["sets"],
                    "reps": item["reps"],
                    "done": bool(item["done"]),
                })
        if statuses and all(status == "completed" for status in statuses):
            status = "completed"
        elif any(status == "completed" for status in statuses):
            status = "partial"
        elif any(status == "partial" for status in statuses):
            status = "partial"
        elif any(status == "planned" for status in statuses):
            status = "planned"
        elif any(status == "pending" for status in statuses):
            status = "pending"
        else:
            status = statuses[0]
        data["exercise"] = {
            "type": " + ".join(types) if types else None,
            "category": " + ".join(categories) if categories else None,
            "duration_min": duration_min,
            "active_energy_kcal": active_energy_kcal,
            "items": items,
            "rpe": max(rpes) if rpes else None,
            "status": status,
            "notes": "；".join(notes),
        }

    for med in all_rows(
        conn,
        """
        SELECT me.*, m.name, m.display_name
        FROM medication_events me
        JOIN medications m ON m.id = me.medication_id
        WHERE me.daily_log_id = ?
        ORDER BY me.scheduled_time, me.id
        """,
        (daily_id,),
    ):
        data["medication"][med["name"]] = {
            "drug_name": med["display_name"],
            "scheduled_time": med["scheduled_time"],
            "taken_time": med["taken_time"],
            "taken": bool(med["taken"]),
            "empty_stomach": bool(med["empty_stomach"]),
            "notes": med["notes"] or "",
        }

    if markers:
        data["health_markers"] = {
            "water_intake_ml": water["water_intake_ml"] if water else 0,
            "water_target_ml": water["water_target_ml"] if water else (targets["water_target_ml"] if targets else 2000),
            "water_remaining_ml": water["water_remaining_ml"] if water else 2000,
            "sleep_bedtime": markers["sleep_bedtime"],
            "sleep_wake_time": markers["sleep_wake_time"],
            "sleep_hours": markers["sleep_hours"],
            "kegel_sets": markers["kegel_sets"],
            "kegel_reps_per_set": markers["kegel_reps_per_set"],
            "kegel_done": bool(markers["kegel_done"]),
            "kegel_notes": markers["kegel_notes"],
            "prostate_symptoms": markers["prostate_symptoms"],
            "energy_level": markers["energy_level"],
        }
    else:
        data["health_markers"] = {
            "water_intake_ml": water["water_intake_ml"] if water else 0,
            "water_target_ml": water["water_target_ml"] if water else 2000,
            "water_remaining_ml": water["water_remaining_ml"] if water else 2000,
        }

    data["notes"] = [
        row["note"]
        for row in all_rows(conn, "SELECT note FROM daily_notes WHERE daily_log_id = ? ORDER BY sort_order, id", (daily_id,))
    ]

    return data


def main():
    parser = argparse.ArgumentParser(description="Export dashboard-compatible daily JSON from Health Butler SQLite")
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    base_dir = Path(args.base_dir).expanduser() if args.base_dir else default_base_dir()
    db_path = Path(args.db).expanduser() if args.db else base_dir / "health.db"
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else base_dir / "data" / "daily"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        raise SystemExit(f"Missing database: {db_path}")

    conn = connect(db_path)
    exported = 0
    try:
        for daily in all_rows(conn, "SELECT * FROM daily_logs ORDER BY date"):
            data = export_daily(conn, daily)
            path = output_dir / f"{daily['date']}.json"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            exported += 1
    finally:
        conn.close()

    print(f"Exported {exported} daily JSON files to {output_dir}")


if __name__ == "__main__":
    main()
