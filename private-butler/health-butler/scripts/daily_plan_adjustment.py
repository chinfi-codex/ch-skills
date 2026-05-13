#!/usr/bin/env python3
"""Deterministic 01:00 daily plan adjustment for Health Butler."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from calculate_daily_budget import calculate_for_daily
from health_db import ensure_runtime_schema


TIME_ZONE = ZoneInfo("Asia/Shanghai")
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def default_base_dir():
    return Path(__file__).resolve().parents[1]


def now_iso():
    return datetime.now(TIME_ZONE).isoformat(timespec="seconds")


def today_ymd():
    return datetime.now(TIME_ZONE).strftime("%Y-%m-%d")


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TIME_ZONE)


def weekday_cn(date_str):
    return WEEKDAYS[parse_date(date_str).weekday()]


def sqlite_dow(date_str):
    # SQLite/exercise_plans convention: 0=Sunday, 1=Monday, ..., 6=Saturday.
    py_weekday = parse_date(date_str).weekday()
    return 0 if py_weekday == 6 else py_weekday + 1


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
    row = conn.execute("SELECT * FROM daily_logs WHERE date = ?", (date_str,)).fetchone()
    conn.execute(
        """
        INSERT OR IGNORE INTO daily_nutrition_targets (
          daily_log_id, target_calories, water_target_ml, created_at, updated_at
        )
        VALUES (?, 1600, 2000, ?, ?)
        """,
        (row["id"], ts, ts),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO health_markers (
          daily_log_id, kegel_sets, kegel_done, created_at, updated_at
        )
        VALUES (?, 0, 0, ?, ?)
        """,
        (row["id"], ts, ts),
    )
    return row


def plan_for_date(conn, date_str):
    return conn.execute(
        """
        SELECT * FROM exercise_plans
        WHERE day_of_week = ? AND active = 1
        ORDER BY id DESC
        LIMIT 1
        """,
        (sqlite_dow(date_str),),
    ).fetchone()


def completed_exercise_minutes(conn, daily_log_id):
    row = conn.execute(
        """
        SELECT COALESCE(SUM(duration_min), 0) AS minutes
        FROM exercise_sessions
        WHERE daily_log_id = ? AND status = 'completed'
        """,
        (daily_log_id,),
    ).fetchone()
    return row["minutes"] or 0


def exercise_status(conn, date_str):
    daily = conn.execute("SELECT * FROM daily_logs WHERE date = ?", (date_str,)).fetchone()
    if not daily:
        return "unknown", 0, 0
    plan = plan_for_date(conn, date_str)
    planned = plan["duration_min"] if plan else 0
    done = completed_exercise_minutes(conn, daily["id"])
    if planned == 0:
        return "rest", planned, done
    if done == 0:
        return "missed", planned, done
    if done >= planned * 0.8:
        return "met", planned, done
    return "partial", planned, done


def intake_status(conn, daily_log_id):
    budget = conn.execute("SELECT * FROM daily_energy_budgets WHERE daily_log_id = ?", (daily_log_id,)).fetchone()
    if not budget:
        return "unknown", 0
    remaining = budget["remaining_kcal"]
    if remaining < -150:
        return "over", remaining
    if remaining < 0:
        return "slightly_over", remaining
    return "on_track", remaining


def water_status(conn, daily_log_id):
    water = conn.execute("SELECT * FROM daily_water_summary WHERE daily_log_id = ?", (daily_log_id,)).fetchone()
    if not water:
        return "unknown", 0
    amount = water["water_intake_ml"] or 0
    if amount < 1200:
        return "low", amount
    if amount >= (water["water_target_ml"] or 2000):
        return "met", amount
    return "partial", amount


def fatigue_signal(conn, daily_log_id):
    markers = conn.execute("SELECT * FROM health_markers WHERE daily_log_id = ?", (daily_log_id,)).fetchone()
    if not markers:
        return None
    if markers["sleep_hours"] is not None and markers["sleep_hours"] < 6:
        return "sleep_low"
    if markers["energy_level"] is not None and markers["energy_level"] <= 3:
        return "energy_low"
    if markers["prostate_symptoms"]:
        return "symptom_present"
    return None


def consecutive_under_done(conn, end_date):
    count = 0
    cursor = parse_date(end_date)
    for _ in range(2):
        date_str = cursor.strftime("%Y-%m-%d")
        status, planned, _ = exercise_status(conn, date_str)
        if planned > 0 and status in ("missed", "partial"):
            count += 1
        cursor -= timedelta(days=1)
    return count


def build_adjustment(conn, target_date):
    yesterday = (parse_date(target_date) - timedelta(days=1)).strftime("%Y-%m-%d")
    y_daily = conn.execute("SELECT * FROM daily_logs WHERE date = ?", (yesterday,)).fetchone()
    y_exercise_status, _, _ = exercise_status(conn, yesterday)
    y_intake_status, y_remaining = ("unknown", 0)
    y_water_status, y_water = ("unknown", 0)
    y_fatigue = None
    if y_daily:
        calculate_for_daily(conn, y_daily)
        y_intake_status, y_remaining = intake_status(conn, y_daily["id"])
        y_water_status, y_water = water_status(conn, y_daily["id"])
        y_fatigue = fatigue_signal(conn, y_daily["id"])

    plan = plan_for_date(conn, target_date)
    planned_duration = plan["duration_min"] if plan else 0
    adjusted_duration = planned_duration
    level = "none"
    reasons = []

    if consecutive_under_done(conn, yesterday) >= 2 and planned_duration > 0:
        adjusted_duration = min(20, max(10, round(planned_duration * 0.5)))
        level = "recovery"
        reasons.append("连续2天运动未完全达标，今日降级为恢复量")
    elif y_fatigue and planned_duration > 0:
        adjusted_duration = min(20, max(10, round(planned_duration * 0.5)))
        level = "recovery"
        reasons.append(f"昨日疲劳/症状信号：{y_fatigue}，今日降级")
    elif y_exercise_status == "partial" and planned_duration > 0:
        adjusted_duration = round(planned_duration * 0.8)
        level = "reduced"
        reasons.append("昨日运动部分完成，今日训练量下调20%")
    elif y_exercise_status == "missed":
        if planned_duration == 0:
            adjusted_duration = 15
            level = "light_add"
            reasons.append("昨日运动未完成，今日休息日增加15分钟低强度活动")
        else:
            adjusted_duration = planned_duration
            level = "shift_focus"
            reasons.append("昨日运动未完成，今日不叠加完整补课，保留主计划并优先低强度收尾")

    if y_intake_status == "over":
        reasons.append(f"昨日摄入超预算约{abs(round(y_remaining))}kcal，今日优先控制零食")
    if y_water_status == "low":
        reasons.append(f"昨日饮水约{round(y_water)}ml，今日提高饮水优先级")

    if planned_duration == 0 and adjusted_duration == 0:
        status = "rest"
    elif level == "none":
        status = "planned"
    else:
        status = "adjusted"

    return plan, {
        "status": status,
        "planned_duration_min": planned_duration,
        "adjusted_duration_min": adjusted_duration,
        "adjustment_level": level,
        "adjustment_reason": "；".join(reasons) if reasons else "按周计划执行",
        "yesterday_exercise_status": y_exercise_status,
        "yesterday_intake_status": y_intake_status,
        "yesterday_water_status": y_water_status,
        "yesterday_fatigue_signal": y_fatigue,
    }


def parse_plan_items(plan):
    if not plan or not plan["exercises_json"]:
        return []
    try:
        return json.loads(plan["exercises_json"])
    except json.JSONDecodeError:
        return []


def duration_from_text(value):
    if not value:
        return None
    text = str(value)
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits and ("min" in text or "分钟" in text) else None


def upsert_daily_plan(conn, daily, plan, adjustment):
    ts = now_iso()
    existing = conn.execute("SELECT * FROM daily_plan_instances WHERE plan_date = ?", (daily["date"],)).fetchone()
    if existing and existing["created_by"] == "manual":
        return existing["id"], False

    conn.execute(
        """
        INSERT INTO daily_plan_instances (
          daily_log_id, plan_date, source_plan_id, status, planned_duration_min,
          adjusted_duration_min, adjustment_level, adjustment_reason, created_by,
          created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'cron_adjustment', ?, ?)
        ON CONFLICT(plan_date) DO UPDATE SET
          daily_log_id=excluded.daily_log_id,
          source_plan_id=excluded.source_plan_id,
          status=excluded.status,
          planned_duration_min=excluded.planned_duration_min,
          adjusted_duration_min=excluded.adjusted_duration_min,
          adjustment_level=excluded.adjustment_level,
          adjustment_reason=excluded.adjustment_reason,
          created_by=excluded.created_by,
          updated_at=excluded.updated_at
        """,
        (
            daily["id"],
            daily["date"],
            plan["id"] if plan else None,
            adjustment["status"],
            adjustment["planned_duration_min"],
            adjustment["adjusted_duration_min"],
            adjustment["adjustment_level"],
            adjustment["adjustment_reason"],
            ts,
            ts,
        ),
    )
    row = conn.execute("SELECT * FROM daily_plan_instances WHERE plan_date = ?", (daily["date"],)).fetchone()
    instance_id = row["id"]
    conn.execute("DELETE FROM daily_plan_items WHERE daily_plan_instance_id = ?", (instance_id,))
    for idx, item in enumerate(parse_plan_items(plan)):
        duration = duration_from_text(item.get("duration"))
        unit_type = "duration" if duration else ("sets_reps" if item.get("sets") or item.get("reps") else None)
        target_value = item.get("duration") or item.get("reps") or item.get("sets")
        conn.execute(
            """
            INSERT INTO daily_plan_items (
              daily_plan_instance_id, name, unit_type, target_value, sets, reps,
              duration_min, priority, status, notes, sort_order, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?)
            """,
            (
                instance_id,
                item.get("name", "未命名运动"),
                unit_type,
                target_value,
                item.get("sets"),
                item.get("reps"),
                duration,
                idx,
                item.get("note"),
                idx,
                ts,
                ts,
            ),
        )
    return instance_id, True


def write_review(conn, date_str, adjustment):
    daily = conn.execute("SELECT * FROM daily_logs WHERE date = ?", (date_str,)).fetchone()
    if not daily:
        return
    status, _, _ = exercise_status(conn, date_str)
    intake, _ = intake_status(conn, daily["id"])
    water, _ = water_status(conn, daily["id"])
    fatigue = fatigue_signal(conn, daily["id"])
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO daily_reviews (
          daily_log_id, review_date, intake_status, water_status, exercise_status,
          fatigue_signal, summary, next_adjustment, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(daily_log_id) DO UPDATE SET
          intake_status=excluded.intake_status,
          water_status=excluded.water_status,
          exercise_status=excluded.exercise_status,
          fatigue_signal=excluded.fatigue_signal,
          summary=excluded.summary,
          next_adjustment=excluded.next_adjustment,
          updated_at=excluded.updated_at
        """,
        (
            daily["id"],
            date_str,
            intake,
            water,
            status,
            fatigue,
            f"{date_str} review: intake={intake}, water={water}, exercise={status}",
            adjustment.get("adjustment_reason"),
            ts,
            ts,
        ),
    )


def refresh_outputs(base_dir):
    subprocess.run(
        [str(base_dir / "scripts" / "calculate_daily_budget.py"), "--base-dir", str(base_dir), "--all", "--quiet"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        [str(base_dir / "scripts" / "export_dashboard_data.py"), "--base-dir", str(base_dir)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        [str(base_dir / "scripts" / "refresh_dashboard_data.py"), "--base-dir", str(base_dir)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    chief_dir = base_dir.parents[1] / "skills" / "chief-butler"
    collect_script = chief_dir / "scripts" / "collect_status.py"
    if collect_script.exists():
        subprocess.run([str(collect_script), "--base-dir", str(chief_dir)], check=True, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description="Adjust Health Butler daily plan deterministically")
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--date", default=None)
    parser.add_argument("--skip-refresh", action="store_true")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).expanduser() if args.base_dir else default_base_dir()
    db_path = Path(args.db).expanduser() if args.db else base_dir / "health.db"
    schema_path = base_dir / "db" / "schema.sql"
    target_date = args.date or today_ymd()
    yesterday = (parse_date(target_date) - timedelta(days=1)).strftime("%Y-%m-%d")

    conn = connect(db_path)
    try:
        with conn:
            conn.executescript(schema_path.read_text(encoding="utf-8"))
            ensure_runtime_schema(conn)
            daily = ensure_daily_log(conn, target_date)
            plan, adjustment = build_adjustment(conn, target_date)
            instance_id, wrote_items = upsert_daily_plan(conn, daily, plan, adjustment)
            write_review(conn, yesterday, adjustment)
            calculate_for_daily(conn, daily)
    finally:
        conn.close()

    if not args.skip_refresh:
        refresh_outputs(base_dir)

    print(
        f"Adjusted plan for {target_date}: instance={instance_id}, "
        f"items={'updated' if wrote_items else 'kept'}, level={adjustment['adjustment_level']}, "
        f"reason={adjustment['adjustment_reason']}"
    )


if __name__ == "__main__":
    main()
