#!/usr/bin/env python3
"""Migrate ch-news-reporter SQLite data into PostgreSQL alpha_data tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# The adapter resolves its backend at import time, so set safe migration defaults
# before importing it. Explicit caller-provided environment variables still win.
os.environ.setdefault("ALPHA_DB_BACKEND", "postgresql")
os.environ.setdefault(
    "ALPHA_PG_URL",
    "postgresql://alpha_user:alpha_pass@localhost:5432/alpha_data",
)

from db_adapter import get_connection, init_news_schema, write_items


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SQLITE = SCRIPT_DIR.parent / "data" / "news_research.sqlite"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate ch-news-reporter SQLite items/enrichments to PostgreSQL."
    )
    parser.add_argument(
        "--sqlite",
        default=str(DEFAULT_SQLITE),
        help="Source SQLite database path. Default: data/news_research.sqlite",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Maximum item rows per adapter write batch. Default: 500.",
    )
    return parser.parse_args()


def json_value(text: Any, default: Any) -> Any:
    if text is None or text == "":
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(str(text))
    except json.JSONDecodeError:
        return default


def source_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def source_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")]


def iter_chunks(rows: list[dict[str, Any]], size: int) -> Any:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def read_source_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not source_table_exists(conn, "items"):
        raise RuntimeError("SQLite source does not contain an items table")
    return list(conn.execute("SELECT * FROM items ORDER BY date_key, fetched_at, id"))


def migrate_items(
    pg_conn: Any,
    source_rows: list[sqlite3.Row],
    batch_size: int,
) -> tuple[int, int]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for row in source_rows:
        tags = json_value(row["tags_json"], [])
        metadata = json_value(row["metadata_json"], {})
        raw = json_value(row["raw_json"], {})
        if not isinstance(tags, list):
            tags = [str(tags)]
        if not isinstance(metadata, dict):
            metadata = {"value": metadata}
        if not isinstance(raw, dict):
            raw = {"value": raw}

        date_key = str(row["date_key"])
        fetched_at = row["fetched_at"] or datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        grouped[date_key][str(fetched_at)].append(
            {
                "id": row["id"],
                "source_type": row["source_type"],
                "source_name": row["source_name"],
                "published_at": row["published_at"],
                "title": row["title"],
                "content": row["content"],
                "url": row["url"],
                "author": row["author"],
                "tags": tags,
                "metadata": metadata,
                "raw": raw,
            }
        )

    migrated = 0
    errors = 0
    for date_key in sorted(grouped):
        date_total = 0
        for fetched_at, rows in sorted(grouped[date_key].items()):
            for chunk in iter_chunks(rows, batch_size):
                try:
                    migrated_now = write_items(pg_conn, chunk, date_key, fetched_at)
                    pg_conn.commit()
                    migrated += migrated_now
                    date_total += migrated_now
                except Exception as exc:  # keep other batches moving
                    errors += len(chunk)
                    pg_conn.rollback()
                    print(
                        f"[error] items {date_key} fetched_at={fetched_at}: {exc}",
                        file=sys.stderr,
                    )
        print(f"[items] {date_key}: migrated {date_total} rows")

    return migrated, errors


def read_source_enrichments(
    conn: sqlite3.Connection,
) -> tuple[list[sqlite3.Row], list[str]]:
    if not source_table_exists(conn, "enrichments"):
        print("[enrichments] source table not found; skipped")
        return [], []

    columns = source_table_columns(conn, "enrichments")
    order_columns = [name for name in ("created_at", "fetched_at", "id") if name in columns]
    order_clause = f" ORDER BY {', '.join(order_columns)}" if order_columns else ""
    return list(conn.execute(f"SELECT * FROM enrichments{order_clause}")), columns


def row_value(
    row: sqlite3.Row,
    columns: set[str],
    name: str,
    default: Any = None,
) -> Any:
    return row[name] if name in columns else default


def stable_legacy_enrichment_id(row: sqlite3.Row, columns: set[str]) -> str:
    existing = row_value(row, columns, "id")
    if existing:
        return str(existing)
    seed = "|".join(
        str(row_value(row, columns, name, "") or "")
        for name in ("item_id", "target_type", "target_url", "source", "created_at")
    )
    return "legacy-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def normalize_enrichment_row(
    row: sqlite3.Row,
    columns: set[str],
    new_schema: bool,
) -> tuple[Any, ...]:
    if new_schema:
        return (
            row_value(row, columns, "id"),
            row_value(row, columns, "item_id"),
            row_value(row, columns, "enrichment_type"),
            row_value(row, columns, "source"),
            row_value(row, columns, "model"),
            row_value(row, columns, "prompt_hash"),
            row_value(row, columns, "result_json"),
            row_value(row, columns, "created_at")
            or datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    payload = {column: row[column] for column in columns}
    return (
        stable_legacy_enrichment_id(row, columns),
        row_value(row, columns, "item_id"),
        "legacy",
        row_value(row, columns, "target_url")
        or row_value(row, columns, "source"),
        None,
        None,
        json.dumps(payload, ensure_ascii=False, default=str),
        row_value(row, columns, "created_at")
        or row_value(row, columns, "fetched_at")
        or datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def migrate_enrichments(
    pg_conn: Any,
    rows: list[sqlite3.Row],
    source_columns: list[str],
) -> tuple[int, int]:
    if not rows:
        return 0, 0

    columns = set(source_columns)
    new_schema = {"enrichment_type", "result_json"}.issubset(columns)
    if new_schema:
        print("[enrichments] source schema: current")
    else:
        print("[enrichments] source schema: legacy; packing source columns into result_json")

    sql = """
        INSERT INTO enrichments (
            id, item_id, enrichment_type, source, model, prompt_hash,
            result_json, created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(id) DO UPDATE SET
            item_id=excluded.item_id,
            enrichment_type=excluded.enrichment_type,
            source=excluded.source,
            model=excluded.model,
            prompt_hash=excluded.prompt_hash,
            result_json=excluded.result_json,
            created_at=excluded.created_at
    """
    migrated = 0
    errors = 0
    cur = pg_conn.cursor()
    for row in rows:
        row_id = stable_legacy_enrichment_id(row, columns)
        try:
            params = normalize_enrichment_row(row, columns, new_schema)
            row_id = params[0]
            cur.execute("SAVEPOINT migrate_enrichment")
            cur.execute(sql, params)
            cur.execute("RELEASE SAVEPOINT migrate_enrichment")
            migrated += 1
        except Exception as exc:
            errors += 1
            cur.execute("ROLLBACK TO SAVEPOINT migrate_enrichment")
            cur.execute("RELEASE SAVEPOINT migrate_enrichment")
            print(f"[error] enrichment {row_id}: {exc}", file=sys.stderr)
    pg_conn.commit()
    print(f"[enrichments] migrated {migrated} rows; errors {errors}")
    return migrated, errors


def main() -> int:
    args = parse_args()
    sqlite_path = Path(args.sqlite).expanduser().resolve()
    if not sqlite_path.exists():
        print(f"[error] SQLite source not found: {sqlite_path}", file=sys.stderr)
        return 2

    print(f"[source] {sqlite_path}")
    print(f"[target] {os.environ['ALPHA_PG_URL']}")

    source = sqlite3.connect(sqlite_path)
    source.row_factory = sqlite3.Row
    try:
        source_items = read_source_items(source)
        source_enrichments, enrichment_columns = read_source_enrichments(source)
        print(f"[items] source rows: {len(source_items)}")
        if source_enrichments:
            print(f"[enrichments] source rows: {len(source_enrichments)}")

        with get_connection(None) as pg_conn:
            init_news_schema(pg_conn)
            item_count, item_errors = migrate_items(
                pg_conn,
                source_items,
                max(args.batch_size, 1),
            )
            enrichment_count, enrichment_errors = migrate_enrichments(
                pg_conn, source_enrichments, enrichment_columns
            )
    finally:
        source.close()

    print(
        "[done] "
        f"items migrated={item_count}, item_errors={item_errors}, "
        f"enrichments migrated={enrichment_count}, "
        f"enrichment_errors={enrichment_errors}"
    )
    return 0 if item_errors == 0 and enrichment_errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
