"""SQLite FTS trigger sync tests (db_adapter).

Covers the migration from per-write `rebuild` to AFTER INSERT/UPDATE/DELETE
triggers on items_fts: fresh schema, live sync on write/update/delete, and
opening a legacy database (no triggers, stale index).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("ALPHA_DB_BACKEND", "sqlite")

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from db_adapter import (  # noqa: E402
    BACKEND,
    Backend,
    get_connection,
    init_news_schema,
    item_exists,
    query_items,
    write_items,
)


def make_row(row_id: str, title: str, content: str = "") -> dict:
    return {
        "id": row_id,
        "source_type": "rss",
        "source_name": "Test Feed",
        "published_at": "2026-08-01T10:00:00+08:00",
        "title": title,
        "content": content,
        "url": f"https://example.test/{row_id}",
        "tags": [],
        "metadata": {},
        "raw": {},
    }


@unittest.skipUnless(BACKEND == Backend.SQLITE, "FTS trigger tests need SQLite backend")
class FtsTriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.sqlite")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _triggers(self, con) -> set[str]:
        return {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }

    def test_insert_update_delete_stay_in_sync(self) -> None:
        with get_connection(self.db_path) as con:
            init_news_schema(con)
            self.assertEqual(
                self._triggers(con),
                {"items_fts_ai", "items_fts_ad", "items_fts_au"},
            )
            write_items(
                con,
                [make_row("r1", "alpha liquidity update"),
                 make_row("r2", "gamma chip supply")],
                "2026-08-01",
                "2026-08-01T12:00:00+08:00",
            )
            hits = query_items(con, keywords="liquidity")
            self.assertEqual([row["id"] for row in hits], ["r1"])

            # upsert 改标题 → 索引同步：新词命中，旧词不再命中
            write_items(
                con,
                [make_row("r1", "delta sanctions roundup")],
                "2026-08-01",
                "2026-08-01T13:00:00+08:00",
            )
            self.assertEqual([row["id"] for row in query_items(con, keywords="sanctions")], ["r1"])
            self.assertEqual(query_items(con, keywords="liquidity"), [])

            # 删除 → 索引同步
            con.execute("DELETE FROM items WHERE id = ?", ("r1",))
            self.assertEqual(query_items(con, keywords="sanctions"), [])
            # 未删的行仍可命中
            self.assertEqual([row["id"] for row in query_items(con, keywords="chip")], ["r2"])

    def test_item_exists(self) -> None:
        with get_connection(self.db_path) as con:
            init_news_schema(con)
            self.assertFalse(item_exists(con, "r1"))
            write_items(con, [make_row("r1", "alpha")], "2026-08-01", "2026-08-01T12:00:00+08:00")
            self.assertTrue(item_exists(con, "r1"))

    def test_legacy_db_gets_triggers_and_rebuild(self) -> None:
        # 模拟老库：建表建索引（无触发器），手工重建一次 FTS 后插入一行，
        # 再删触发器场景下第二次写入不进索引。
        import sqlite3

        con = sqlite3.connect(self.db_path)
        con.execute(
            """
            CREATE TABLE items (
                id TEXT PRIMARY KEY, date_key TEXT NOT NULL, source_type TEXT NOT NULL,
                source_name TEXT NOT NULL, published_at TEXT, fetched_at TEXT NOT NULL,
                title TEXT NOT NULL, content TEXT, url TEXT, author TEXT,
                tags_json TEXT, metadata_json TEXT, raw_json TEXT
            )
            """
        )
        con.execute(
            "CREATE VIRTUAL TABLE items_fts USING fts5("
            "title, content, tags_json, content='items', content_rowid='rowid')"
        )
        con.execute(
            "INSERT INTO items (id, date_key, source_type, source_name, published_at,"
            " fetched_at, title, content, url, tags_json, metadata_json, raw_json)"
            " VALUES ('old1', '2026-07-31', 'rss', 'Test Feed', NULL, 't',"
            " 'legacy omega story', '', 'https://example.test/old1', '[]', '{}', '{}')"
        )
        # 老库状态：FTS 从未 rebuild 过 → 历史行对 MATCH 不可见
        self.assertEqual(
            con.execute(
                "SELECT count(*) FROM items_fts WHERE items_fts MATCH 'omega'"
            ).fetchone()[0],
            0,
        )
        con.commit()
        con.close()

        # 升级路径打开：触发器建立 + 一次性 rebuild → 历史行立即可检索
        with get_connection(self.db_path) as con2:
            init_news_schema(con2)
            self.assertEqual(
                self._triggers(con2),
                {"items_fts_ai", "items_fts_ad", "items_fts_au"},
            )
            self.assertEqual(
                [row["id"] for row in query_items(con2, keywords="omega")], ["old1"]
            )


if __name__ == "__main__":
    unittest.main()
