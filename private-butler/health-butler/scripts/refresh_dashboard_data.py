#!/usr/bin/env python3
"""
Refresh only the data-bearing parts of dashboard/index.html from SQLite.

This deliberately does not regenerate the full HTML template. It preserves any
manual HTML/CSS changes and only replaces:
- the embedded allData object
- the Shanghai "today" week anchor
- the footer update timestamp
"""

import argparse
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from export_dashboard_data import all_rows, connect, export_daily
from health_db import ensure_runtime_schema


DEFAULT_DAILY_BASE = 2122
TARGET_INTAKE = 1600
TIME_ZONE = ZoneInfo("Asia/Shanghai")


def default_base_dir():
    return Path(__file__).resolve().parents[1]


def default_dashboard_path(base_dir):
    profile_root = base_dir.parents[1] if len(base_dir.parents) >= 2 else base_dir
    chief_dashboard = profile_root / "skills" / "chief-butler" / "dashboard" / "index.html"
    if chief_dashboard.exists():
        return chief_dashboard
    return base_dir / "dashboard" / "index.html"


def shanghai_now():
    return datetime.now(TIME_ZONE)


def shanghai_today_ymd():
    return shanghai_now().strftime("%Y-%m-%d")


def shanghai_update_time():
    return shanghai_now().strftime("%-m/%-d %H:%M:%S")


def calculate_bmi(weight_kg, height_cm, fallback=None):
    if weight_kg and height_cm:
        height_m = height_cm / 100
        if height_m > 0:
            return round(weight_kg / (height_m * height_m), 1)
    return fallback


def process_data(raw, daily_base=DEFAULT_DAILY_BASE):
    intake = raw.get("nutrition_summary", {}).get("total_calories") or 0
    target = raw.get("nutrition_summary", {}).get("target_calories") or TARGET_INTAKE
    health_markers = raw.get("health_markers", {})
    exercise = raw.get("exercise", {})
    exercise_minutes = health_markers.get("exercise_minutes") or exercise.get("duration_min") or 0
    exercise_type = exercise.get("category") or exercise.get("type") or ""

    exercise_burn = health_markers.get("active_energy_kcal") or exercise.get("active_energy_kcal") or 0
    if not exercise_burn and exercise_minutes > 0:
        kcal_per_min = 5 if any(key in exercise_type for key in ("有氧", "快走", "跑步")) else 4
        exercise_burn = round(exercise_minutes * kcal_per_min)

    water = health_markers.get("water_intake_ml") or raw.get("nutrition_summary", {}).get("water_ml") or 0
    water_target = health_markers.get("water_target_ml") or 2000
    budget = raw.get("energy_budget") or {}
    daily_plan = raw.get("daily_plan") or {}

    weekday = raw.get("weekday") or ""
    profile = raw.get("profile_snapshot", {})
    height_cm = profile.get("height_cm")
    weight_kg = profile.get("weight_kg")
    nutrition = raw.get("nutrition_summary", {})
    exercise_items = exercise.get("items") or []
    exercise_label = " / ".join(item.get("name", "") for item in exercise_items if item.get("name"))

    # Build per-session list for dashboard display
    exercises_list = []
    sessions = exercise.get("sessions") or []
    for session in sessions:
        sess_items = session.get("items") or []
        label = " / ".join(item.get("name", "") for item in sess_items if item.get("name"))
        if not label:
            label = session.get("category") or session.get("type") or "运动"
        exercises_list.append({
            "name": label,
            "type": session.get("type") or "",
            "category": session.get("category") or "",
            "duration_min": session.get("duration_min") or 0,
            "burn": session.get("active_energy_kcal") or 0,
            "done": session.get("status") == "completed",
        })

    return {
        "date": raw.get("date"),
        "weekday": weekday,
        "weekdayShort": weekday.replace("周", ""),
        "heightCm": height_cm,
        "weight": weight_kg,
        "bodyFat": profile.get("body_fat_pct"),
        "bmi": calculate_bmi(weight_kg, height_cm, profile.get("bmi")),
        "intake": intake,
        "targetIntake": target,
        "dailyLimit": budget.get("intake_limit_kcal"),
        "remainingCalories": budget.get("remaining_kcal"),
        "budgetMode": budget.get("mode"),
        "baseBurn": budget.get("base_burn_kcal"),
        "targetDeficit": budget.get("target_deficit_kcal"),
        "exerciseBurn": exercise_burn,
        "exerciseMinutes": exercise_minutes,
        "exerciseType": exercise_type,
        "exerciseLabel": exercise_label,
        "exerciseDone": exercise.get("status") == "completed",
        "exercises": exercises_list,
        "water": water,
        "waterTarget": water_target,
        "waterPct": round((water / water_target) * 100) if water_target else 0,
        "meals": raw.get("meals", {}),
        "sleep": health_markers.get("sleep_hours"),
        "steps": health_markers.get("steps"),
        "protein": nutrition.get("protein_g"),
        "carbs": nutrition.get("carbs_g"),
        "fat": nutrition.get("fat_g"),
        "dailyPlan": {
            "status": daily_plan.get("status"),
            "plannedDurationMin": daily_plan.get("planned_duration_min"),
            "adjustedDurationMin": daily_plan.get("adjusted_duration_min"),
            "adjustmentLevel": daily_plan.get("adjustment_level"),
            "adjustmentReason": daily_plan.get("adjustment_reason"),
            "createdBy": daily_plan.get("created_by"),
            "items": daily_plan.get("items") or [],
        } if daily_plan else None,
    }


def load_dashboard_data(db_path, daily_base=DEFAULT_DAILY_BASE):
    conn = connect(db_path)
    try:
        ensure_runtime_schema(conn)
        data = {}
        for daily in all_rows(conn, "SELECT * FROM daily_logs ORDER BY date"):
            raw = export_daily(conn, daily)
            data[raw["date"]] = process_data(raw, daily_base)
        return data
    finally:
        conn.close()


def replace_one(pattern, replacement, text, label):
    next_text, count = re.subn(pattern, replacement, text, count=1, flags=re.DOTALL)
    if count != 1:
        raise RuntimeError(f"Could not update dashboard {label}")
    return next_text


def extract_number_const(html, name, default):
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*;", html)
    return float(match.group(1)) if match else default


def refresh_dashboard(db_path, dashboard_path):
    html = dashboard_path.read_text(encoding="utf-8")
    daily_base = extract_number_const(html, "DAILY_BASE", extract_number_const(html, "TDEE", DEFAULT_DAILY_BASE))
    data = load_dashboard_data(db_path, daily_base)
    if not data:
        raise RuntimeError(f"No daily rows found in {db_path}")

    all_data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    today = shanghai_today_ymd()
    updated = shanghai_update_time()

    html = replace_one(
        r"const allData = .*?;\n(?=\s*const\s)",
        f"const allData = {all_data_json};\n",
        html,
        "allData",
    )
    # Update currentCenterDate - simple string replacement (handles line numbers)
    center_date_replacement = f"let currentCenterDate = '{today}';"
    lines = html.split('\n')
    found = False
    for i, line in enumerate(lines):
        if 'let currentCenterDate = ' in line:
            # Preserve line number prefix if present (e.g., "  968|")
            prefix = ''
            if '|' in line:
                prefix = line.split('|')[0] + '|'
            lines[i] = prefix + center_date_replacement
            found = True
            break
    if not found:
        raise RuntimeError("Could not update dashboard currentCenterDate")
    html = '\n'.join(lines)
    html = replace_one(
        r"<span>数据更新于 .*? 上海时间 · 主人在，管家就在</span>",
        f"<span>数据更新于 {updated} 上海时间 · 主人在，管家就在</span>",
        html,
        "footer timestamp",
    )

    dashboard_path.write_text(html, encoding="utf-8")
    return len(data), updated


def main():
    parser = argparse.ArgumentParser(description="Refresh dashboard data from SQLite without rewriting HTML/CSS")
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--dashboard", default=None)
    args = parser.parse_args()

    base_dir = Path(args.base_dir).expanduser() if args.base_dir else default_base_dir()
    db_path = Path(args.db).expanduser() if args.db else base_dir / "health.db"
    dashboard_path = Path(args.dashboard).expanduser() if args.dashboard else default_dashboard_path(base_dir)

    if not db_path.exists():
        raise SystemExit(f"Missing database: {db_path}")
    if not dashboard_path.exists():
        raise SystemExit(f"Missing dashboard: {dashboard_path}")

    count, updated = refresh_dashboard(db_path, dashboard_path)
    print(f"Refreshed dashboard data from DB: {count} days, updated {updated} Asia/Shanghai")


if __name__ == "__main__":
    main()
