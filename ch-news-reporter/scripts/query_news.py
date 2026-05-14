#!/usr/bin/env python3
"""Query the ch-news-reporter SQLite database."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DB = Path("data/news_research.sqlite")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query unified news SQLite data.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
    parser.add_argument("--date", default="today", help="today, all, or YYYY-MM-DD.")
    parser.add_argument("--q", default="", help="FTS query. Empty means latest rows.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum rows.")
    parser.add_argument(
        "--source-type",
        choices=[
            "cls",
            "jin10",
            "github_trending",
            "rss",
            "product_hunt",
            "hacker_news",
            "error",
        ],
        help="Optional source type filter.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "csv"],
        default="markdown",
        help="Output format.",
    )
    return parser.parse_args()


def resolve_date_key(value: str) -> str | None:
    if value == "all":
        return None
    if value == "today":
        return datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit("--date must be 'today', 'all', or YYYY-MM-DD") from exc


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"Database does not exist: {db_path}")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def escape_fts_query(query: str) -> str:
    terms = [term.strip() for term in query.replace('"', " ").split() if term.strip()]
    return " OR ".join(f'"{term}"' for term in terms)


def query_rows(args: argparse.Namespace) -> list[dict[str, str]]:
    date_key = resolve_date_key(args.date)
    params: list[object] = []
    where: list[str] = []

    if date_key:
        where.append("items.date_key = ?")
        params.append(date_key)
    if args.source_type:
        where.append("items.source_type = ?")
        params.append(args.source_type)

    if args.q.strip():
        fts_query = escape_fts_query(args.q)
        terms = [term.strip() for term in args.q.split() if term.strip()]
        sql = "SELECT DISTINCT items.* FROM items"
        like_clauses: list[str] = []
        fts_clause = "items.rowid IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)"
        params.append(fts_query)
        for term in terms:
            like_clauses.append(
                "(items.title LIKE ? OR items.content LIKE ? OR items.tags_json LIKE ?)"
            )
            like_term = f"%{term}%"
            params.extend([like_term, like_term, like_term])
        where.append("(" + " OR ".join([fts_clause, *like_clauses]) + ")")
    else:
        sql = "SELECT items.* FROM items"

    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(items.published_at, items.fetched_at) DESC LIMIT ?"
    params.append(args.limit)

    con = connect(Path(args.db))
    try:
        return [dict(row) for row in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def emit_json(rows: list[dict[str, str]]) -> None:
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def emit_csv(rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)


def compact_text(text: str | None, max_len: int = 240) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "..."


def emit_markdown(rows: list[dict[str, str]]) -> None:
    print(f"## 查询结果 ({len(rows)} 条)\n")
    for row in rows:
        time_label = row.get("published_at") or row.get("fetched_at") or "未知时间"
        source = row.get("source_name") or row.get("source_type") or "unknown"
        title = row.get("title") or "(untitled)"
        url = row.get("url") or ""
        content = compact_text(row.get("content"))
        link = f" ([link]({url}))" if url else ""
        print(f"- **{time_label}** - {title} [{source}]{link}")
        if content and content != title:
            print(f"  {content}")


def main() -> int:
    args = parse_args()
    rows = query_rows(args)
    if args.format == "json":
        emit_json(rows)
    elif args.format == "csv":
        emit_csv(rows)
    else:
        emit_markdown(rows)
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    raise SystemExit(main())
