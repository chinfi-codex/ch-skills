#!/usr/bin/env python3
"""Export a compact Health Butler summary for Chief Butler."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from health_db import ensure_runtime_schema


TIME_ZONE = ZoneInfo("Asia/Shanghai")


def default_base_dir():
    return Path(__file__).resolve().parents[1]


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def one(conn, query, params=()):
    return conn.execute(query, params).fetchone()


def latest_daily(conn):
    return one(conn, "SELECT * FROM daily_logs ORDER BY date DESC LIMIT 1")


def latest_body(conn):
    return one(
        conn,
        """
        SELECT dl.date, bm.*
        FROM body_measurements bm
        JOIN daily_logs dl ON dl.id = bm.daily_log_id
        WHERE bm.weight_kg IS NOT NULL
        ORDER BY dl.date DESC, bm.id DESC
        LIMIT 1
        """,
    )


def export_summary(base_dir, output_path):
    db_path = base_dir / "health.db"
    conn = connect(db_path)
    try:
        ensure_runtime_schema(conn)
        daily = latest_daily(conn)
        body = latest_body(conn)
        if not daily:
            summary = {
                "skill": "health-butler",
                "status": "empty",
                "updated_at": datetime.now(TIME_ZONE).isoformat(timespec="seconds"),
                "cards": [],
                "alerts": [],
                "links": {"detail_dashboard": "../chief-butler/dashboard/index.html"},
            }
        else:
            nutrition = one(conn, "SELECT * FROM daily_nutrition_summary WHERE daily_log_id = ?", (daily["id"],))
            water = one(conn, "SELECT * FROM daily_water_summary WHERE daily_log_id = ?", (daily["id"],))
            budget = one(conn, "SELECT * FROM daily_energy_budgets WHERE daily_log_id = ?", (daily["id"],))
            plan = one(conn, "SELECT * FROM daily_plan_instances WHERE daily_log_id = ?", (daily["id"],))
            cards = [
                {
                    "type": "metric",
                    "title": "今日摄入",
                    "value": round(nutrition["total_calories"]) if nutrition else 0,
                    "unit": "kcal",
                },
                {
                    "type": "metric",
                    "title": "还可摄入",
                    "value": round(budget["remaining_kcal"]) if budget else None,
                    "unit": "kcal",
                },
                {
                    "type": "metric",
                    "title": "饮水",
                    "value": water["water_intake_ml"] if water else 0,
                    "target": water["water_target_ml"] if water else 2000,
                    "unit": "ml",
                },
            ]
            if body:
                cards.append({
                    "type": "metric",
                    "title": "最新体重",
                    "value": body["weight_kg"],
                    "unit": "kg",
                    "date": body["date"],
                })
            alerts = []
            if water and (water["water_intake_ml"] or 0) < 1200:
                alerts.append({"level": "info", "text": "今日饮水偏少"})
            if budget and budget["remaining_kcal"] < 0:
                alerts.append({"level": "warn", "text": "今日摄入已超过预算"})
            if plan and plan["adjustment_level"] not in (None, "", "none"):
                alerts.append({"level": "info", "text": plan["adjustment_reason"] or "今日运动计划已调整"})
            summary = {
                "skill": "health-butler",
                "status": "ok",
                "updated_at": datetime.now(TIME_ZONE).isoformat(timespec="seconds"),
                "date": daily["date"],
                "cards": cards,
                "alerts": alerts,
                "links": {"detail_dashboard": "../chief-butler/dashboard/index.html"},
            }
    finally:
        conn.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Export Health Butler chief summary")
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    base_dir = Path(args.base_dir).expanduser() if args.base_dir else default_base_dir()
    output = Path(args.output).expanduser() if args.output else base_dir / "data" / "summary.json"
    summary = export_summary(base_dir, output)
    print(f"Exported {summary['skill']} summary to {output}")


if __name__ == "__main__":
    main()
