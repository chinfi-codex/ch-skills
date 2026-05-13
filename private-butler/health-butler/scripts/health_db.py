#!/usr/bin/env python3
"""Runtime SQLite migrations for Health Butler.

The skill executes ``db/schema.sql`` on every write, but SQLite will not alter
existing tables from updated CREATE TABLE statements. Keep additive migrations
here so old local databases converge to the current shape without data loss.
"""

from __future__ import annotations

import sqlite3


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    if not table_exists(conn, table_name):
        return False
    return any(row[1] == column_name for row in conn.execute(f"PRAGMA table_info({table_name})"))


def add_column_if_missing(conn: sqlite3.Connection, table_name: str, column_sql: str) -> None:
    column_name = column_sql.split()[0]
    if not column_exists(conn, table_name, column_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")


def ensure_runtime_schema(conn: sqlite3.Connection) -> None:
    # Create target tables first so additive foreign-key columns can reference
    # them when SQLite validates ALTER TABLE statements.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS exercise_plans (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          day_of_week INTEGER NOT NULL,
          plan_type TEXT NOT NULL,
          category TEXT,
          duration_min INTEGER,
          exercises_json TEXT,
          notes TEXT,
          active INTEGER DEFAULT 1,
          plan_version TEXT,
          profile_snapshot_json TEXT,
          goal_snapshot_json TEXT,
          valid_from TEXT,
          valid_to TEXT,
          generation_reason TEXT,
          created_at TEXT,
          updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_plan_instances (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          daily_log_id INTEGER NOT NULL REFERENCES daily_logs(id) ON DELETE CASCADE,
          plan_date TEXT NOT NULL UNIQUE,
          source_plan_id INTEGER REFERENCES exercise_plans(id) ON DELETE SET NULL,
          status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'adjusted', 'partial', 'completed', 'skipped', 'rest')),
          planned_duration_min INTEGER NOT NULL DEFAULT 0,
          adjusted_duration_min INTEGER NOT NULL DEFAULT 0,
          adjustment_level TEXT NOT NULL DEFAULT 'none',
          adjustment_reason TEXT,
          created_by TEXT NOT NULL DEFAULT 'weekly_generator',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )

    add_column_if_missing(conn, "meal_items", "water_ml INTEGER")
    add_column_if_missing(conn, "meal_items", "calorie_source TEXT NOT NULL DEFAULT 'manual_estimate'")
    add_column_if_missing(conn, "meal_items", "water_source TEXT")
    add_column_if_missing(conn, "meal_items", "estimate_confidence TEXT NOT NULL DEFAULT 'medium'")

    add_column_if_missing(conn, "exercise_sessions", "planned_instance_id INTEGER REFERENCES daily_plan_instances(id) ON DELETE SET NULL")
    add_column_if_missing(conn, "exercise_sessions", "active_energy_kcal REAL")
    add_column_if_missing(conn, "exercise_sessions", "burn_source TEXT NOT NULL DEFAULT 'manual_estimate'")
    add_column_if_missing(conn, "exercise_sessions", "unit_summary TEXT")

    add_column_if_missing(conn, "exercise_items", "unit_type TEXT")
    add_column_if_missing(conn, "exercise_items", "unit_value REAL")
    add_column_if_missing(conn, "exercise_items", "duration_min_estimated INTEGER")
    add_column_if_missing(conn, "exercise_items", "calories_burned REAL")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS exercise_plans (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          day_of_week INTEGER NOT NULL,
          plan_type TEXT NOT NULL,
          category TEXT,
          duration_min INTEGER,
          exercises_json TEXT,
          notes TEXT,
          active INTEGER DEFAULT 1,
          plan_version TEXT,
          profile_snapshot_json TEXT,
          goal_snapshot_json TEXT,
          valid_from TEXT,
          valid_to TEXT,
          generation_reason TEXT,
          created_at TEXT,
          updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_energy_budgets (
          daily_log_id INTEGER PRIMARY KEY REFERENCES daily_logs(id) ON DELETE CASCADE,
          mode TEXT NOT NULL DEFAULT 'manual',
          bmr REAL NOT NULL,
          activity_multiplier REAL NOT NULL,
          base_burn_kcal REAL NOT NULL,
          exercise_burn_kcal REAL NOT NULL DEFAULT 0,
          target_deficit_kcal REAL NOT NULL DEFAULT 500,
          intake_limit_kcal REAL NOT NULL,
          actual_intake_kcal REAL NOT NULL DEFAULT 0,
          remaining_kcal REAL NOT NULL,
          computed_at TEXT NOT NULL,
          notes TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_plan_instances (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          daily_log_id INTEGER NOT NULL REFERENCES daily_logs(id) ON DELETE CASCADE,
          plan_date TEXT NOT NULL UNIQUE,
          source_plan_id INTEGER REFERENCES exercise_plans(id) ON DELETE SET NULL,
          status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'adjusted', 'partial', 'completed', 'skipped', 'rest')),
          planned_duration_min INTEGER NOT NULL DEFAULT 0,
          adjusted_duration_min INTEGER NOT NULL DEFAULT 0,
          adjustment_level TEXT NOT NULL DEFAULT 'none',
          adjustment_reason TEXT,
          created_by TEXT NOT NULL DEFAULT 'weekly_generator',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_plan_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          daily_plan_instance_id INTEGER NOT NULL REFERENCES daily_plan_instances(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          unit_type TEXT,
          target_value TEXT,
          sets TEXT,
          reps TEXT,
          duration_min INTEGER,
          priority INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'completed', 'skipped')),
          notes TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_reviews (
          daily_log_id INTEGER PRIMARY KEY REFERENCES daily_logs(id) ON DELETE CASCADE,
          review_date TEXT NOT NULL UNIQUE,
          intake_status TEXT NOT NULL DEFAULT 'unknown',
          water_status TEXT NOT NULL DEFAULT 'unknown',
          exercise_status TEXT NOT NULL DEFAULT 'unknown',
          weight_signal TEXT,
          fatigue_signal TEXT,
          summary TEXT,
          next_adjustment TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )

    for column_sql in (
        "plan_version TEXT",
        "profile_snapshot_json TEXT",
        "goal_snapshot_json TEXT",
        "valid_from TEXT",
        "valid_to TEXT",
        "generation_reason TEXT",
    ):
        add_column_if_missing(conn, "exercise_plans", column_sql)

    refresh_summary_views(conn)


def refresh_summary_views(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP VIEW IF EXISTS daily_nutrition_summary;
        DROP VIEW IF EXISTS daily_water_summary;

        CREATE VIEW daily_nutrition_summary AS
        SELECT
          dl.id AS daily_log_id,
          dl.date,
          COALESCE(dnt.target_calories, 1600) AS target_calories,
          COALESCE(SUM(mi.calories), 0) AS total_calories,
          COALESCE(dnt.target_calories, 1600) - COALESCE(SUM(mi.calories), 0) AS remaining_calories,
          COALESCE(SUM(mi.protein_g), dne.protein_g) AS protein_g,
          COALESCE(SUM(mi.carbs_g), dne.carbs_g) AS carbs_g,
          COALESCE(SUM(mi.fat_g), dne.fat_g) AS fat_g
        FROM daily_logs dl
        LEFT JOIN daily_nutrition_targets dnt ON dnt.daily_log_id = dl.id
        LEFT JOIN daily_nutrition_estimates dne ON dne.daily_log_id = dl.id
        LEFT JOIN meals m ON m.daily_log_id = dl.id AND m.status = 'completed'
        LEFT JOIN meal_items mi ON mi.meal_id = m.id
        GROUP BY dl.id;

        CREATE VIEW daily_water_summary AS
        SELECT
          dl.id AS daily_log_id,
          dl.date,
          COALESCE(dnt.water_target_ml, 2000) AS water_target_ml,
          COALESCE(we_sum.water_events_ml, 0) + COALESCE(mi_sum.meal_water_ml, 0) AS water_intake_ml,
          MAX(COALESCE(dnt.water_target_ml, 2000) - (COALESCE(we_sum.water_events_ml, 0) + COALESCE(mi_sum.meal_water_ml, 0)), 0) AS water_remaining_ml
        FROM daily_logs dl
        LEFT JOIN daily_nutrition_targets dnt ON dnt.daily_log_id = dl.id
        LEFT JOIN (
          SELECT daily_log_id, SUM(amount_ml) AS water_events_ml
          FROM water_events
          GROUP BY daily_log_id
        ) we_sum ON we_sum.daily_log_id = dl.id
        LEFT JOIN (
          SELECT m.daily_log_id, SUM(COALESCE(mi.water_ml, 0)) AS meal_water_ml
          FROM meals m
          JOIN meal_items mi ON mi.meal_id = m.id
          WHERE m.status = 'completed'
          GROUP BY m.daily_log_id
        ) mi_sum ON mi_sum.daily_log_id = dl.id
        GROUP BY dl.id;

        CREATE INDEX IF NOT EXISTS idx_daily_plan_instances_daily_log_id ON daily_plan_instances(daily_log_id);
        CREATE INDEX IF NOT EXISTS idx_daily_plan_items_instance_id ON daily_plan_items(daily_plan_instance_id);
        CREATE INDEX IF NOT EXISTS idx_daily_reviews_review_date ON daily_reviews(review_date);
        """
    )
