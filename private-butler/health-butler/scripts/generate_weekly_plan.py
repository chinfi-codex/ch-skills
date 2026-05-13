#!/usr/bin/env python3
"""Generate the active weekly exercise template from profile and goal facts."""

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


def now_iso():
    return datetime.now(TIME_ZONE).isoformat(timespec="seconds")


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def row_to_dict(row):
    return dict(row) if row else {}


def default_plan():
    # Conservative v1: low-impact cardio + light dumbbell strength. This matches
    # current profile constraints and keeps existing dashboard expectations.
    return [
        (1, "有氧", "快走日", 50, [
            {"name": "慢走热身", "duration": "5min", "note": "活动脚踝、膝盖"},
            {"name": "快走", "duration": "40min", "note": "步频120-130步/分钟，心率110-130"},
            {"name": "慢走放松", "duration": "5min", "note": "小腿拉伸"},
        ], "户外或跑步机均可，微微出汗能说话"),
        (2, "力量+有氧", "推力日", 50, [
            {"name": "肩关节环绕", "sets": "1组", "reps": "20次", "note": "热身"},
            {"name": "俯卧撑", "sets": "3组", "reps": "10-15次", "note": "身体直线"},
            {"name": "哑铃推举", "sets": "3组", "reps": "12次", "note": "3-5kg"},
            {"name": "哑铃飞鸟", "sets": "3组", "reps": "12次", "note": "2-3kg"},
            {"name": "窄距俯卧撑", "sets": "3组", "reps": "8-12次", "note": "侧重三头肌"},
            {"name": "哑铃侧平举", "sets": "3组", "reps": "15次", "note": "1-2kg"},
            {"name": "快走收尾", "duration": "10min", "note": "原地踏步或户外快走"},
        ], "力量35min + 有氧10min，RPE 6-7"),
        (3, "恢复", "轻度有氧日", 40, [
            {"name": "散步", "duration": "15min", "note": "慢速随意"},
            {"name": "全身拉伸", "duration": "10min", "note": "颈、肩、背、腿"},
            {"name": "凯格尔运动", "sets": "3组", "reps": "10次", "note": "提肛"},
            {"name": "快走", "duration": "15min", "note": "轻松 pace"},
        ], "主动恢复，不疲劳"),
        (4, "有氧", "快走加速日", 55, [
            {"name": "慢走热身", "duration": "5min", "note": ""},
            {"name": "快走", "duration": "45min", "note": "步频125-135，微喘但能说话"},
            {"name": "慢走放松", "duration": "5min", "note": "拉伸大腿、小腿"},
        ], "比周一快，心率可稍高"),
        (5, "力量+有氧", "拉力日", 50, [
            {"name": "肩部环绕", "sets": "1组", "reps": "20次", "note": "热身"},
            {"name": "哑铃划船", "sets": "3组", "reps": "12次", "note": "3-5kg"},
            {"name": "哑铃弯举", "sets": "3组", "reps": "12次", "note": "2-3kg"},
            {"name": "哑铃单臂划船", "sets": "3组", "reps": "12次", "note": "3-5kg"},
            {"name": "哑铃锤式弯举", "sets": "3组", "reps": "15次", "note": "2-3kg"},
            {"name": "平板支撑", "sets": "3组", "reps": "30-45秒", "note": "身体直线"},
            {"name": "快走收尾", "duration": "10min", "note": "原地踏步或户外快走"},
        ], "力量35min + 有氧10min，RPE 6-7"),
        (6, "循环", "全身循环日", 40, [
            {"name": "关节活动", "duration": "5min", "note": "热身"},
            {"name": "俯卧撑", "reps": "15次", "note": "第1轮"},
            {"name": "哑铃推举", "reps": "15次", "note": "3kg，第1轮"},
            {"name": "哑铃划船", "reps": "15次", "note": "3kg，第1轮"},
            {"name": "哑铃弯举", "reps": "15次", "note": "2kg，第1轮"},
            {"name": "高抬腿", "duration": "30秒", "note": "原地，第1轮"},
            {"name": "平板支撑", "duration": "30秒", "note": "第1轮"},
            {"name": "以上动作", "note": "重复2轮，共3轮"},
        ], "低强度循环，3轮，每轮休息90秒"),
        (0, "休息", "完全休息日", 0, [], "不安排运动，可散步做家务"),
    ]


def main():
    parser = argparse.ArgumentParser(description="Generate active Health Butler weekly exercise plan")
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--version", default=None)
    args = parser.parse_args()

    base_dir = Path(args.base_dir).expanduser() if args.base_dir else default_base_dir()
    db_path = Path(args.db).expanduser() if args.db else base_dir / "health.db"
    schema_path = base_dir / "db" / "schema.sql"
    version = args.version or datetime.now(TIME_ZONE).strftime("weekly-%Y%m%d")

    conn = connect(db_path)
    try:
        with conn:
            conn.executescript(schema_path.read_text(encoding="utf-8"))
            ensure_runtime_schema(conn)
            profile = row_to_dict(conn.execute("SELECT * FROM profiles WHERE id = 1").fetchone())
            goal = row_to_dict(conn.execute("SELECT * FROM goals WHERE active = 1 ORDER BY id DESC LIMIT 1").fetchone())
            ts = now_iso()
            conn.execute("UPDATE exercise_plans SET active = 0, updated_at = ? WHERE active = 1", (ts,))
            for dow, plan_type, category, duration, exercises, notes in default_plan():
                conn.execute(
                    """
                    INSERT INTO exercise_plans (
                      day_of_week, plan_type, category, duration_min, exercises_json,
                      notes, active, plan_version, profile_snapshot_json,
                      goal_snapshot_json, valid_from, generation_reason,
                      created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dow,
                        plan_type,
                        category,
                        duration,
                        json.dumps(exercises, ensure_ascii=False),
                        notes,
                        version,
                        json.dumps(profile, ensure_ascii=False, default=str),
                        json.dumps(goal, ensure_ascii=False, default=str),
                        datetime.now(TIME_ZONE).strftime("%Y-%m-%d"),
                        "profile + active goal based conservative v1 template",
                        ts,
                        ts,
                    ),
                )
    finally:
        conn.close()

    print(f"Generated weekly exercise plan version {version} in {db_path}")


if __name__ == "__main__":
    main()
