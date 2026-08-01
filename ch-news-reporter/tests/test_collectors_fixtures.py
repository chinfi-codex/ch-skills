"""Golden-fixture tests for the five collectors' parsing paths.

Fixtures under tests/fixtures/ are real captured samples truncated to 2-3
entries (jin10 MCP structuredContent, GitHub trending HTML, Product Hunt
GraphQL, HN item JSON, a real RSS feed).  Everything runs offline: network
is replaced by fake sessions or patched fetch helpers.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
FIXTURES = Path(__file__).resolve().parent / "fixtures"

import collect_news  # noqa: E402
from collect_news import (  # noqa: E402
    NewsItem,
    collect_github_trending,
    collect_hacker_news,
    collect_product_hunt,
    collect_rss,
    extract_jin10_cursor,
    extract_jin10_items,
    item_stable_id,
    jin10_record_to_item,
)
from db_adapter import make_stable_id  # noqa: E402


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(
        self,
        text: str | None = None,
        json_payload: dict | None = None,
        content: bytes | None = None,
    ) -> None:
        self.text = text or ""
        self._json_payload = json_payload or {}
        self.content = content if content is not None else self.text.encode("utf-8")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._json_payload


class FakeSession:
    """Minimal requests.Session stand-in; maps URL → FakeResponse."""

    def __init__(self, responses: dict[str, FakeResponse], default: FakeResponse | None = None) -> None:
        self._responses = responses
        self._default = default or FakeResponse()

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        return self._responses.get(url, self._default)

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        return self._responses.get(url, self._default)


def assert_required_fields(test: unittest.TestCase, item: NewsItem, source_type: str) -> None:
    test.assertEqual(item.source_type, source_type)
    test.assertTrue(item.title.strip(), "title must be non-empty")
    test.assertTrue(item.url.strip(), "url must be non-empty")
    test.assertTrue(item.published_at, "published_at must be set")


class Jin10FixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(load_fixture("jin10_list_flash.json"))

    def test_extract_items_and_cursor(self) -> None:
        items = extract_jin10_items(self.payload)
        self.assertGreater(len(items), 0)
        self.assertEqual(len(items), 3)
        self.assertTrue(extract_jin10_cursor(self.payload))

    def test_record_to_item_required_fields(self) -> None:
        for record in extract_jin10_items(self.payload):
            item = jin10_record_to_item(record)
            self.assertIsNotNone(item)
            assert_required_fields(self, item, "jin10")
            self.assertEqual(item.source_name, "金十数据 MCP 电报")


class GithubTrendingFixtureTests(unittest.TestCase):
    def test_parse_trending_html(self) -> None:
        html = load_fixture("github_trending.html")
        session = FakeSession({}, default=FakeResponse(text=html))
        with (
            patch.object(collect_news, "fetch_github_repo_metadata", return_value={}),
            patch.object(collect_news, "fetch_github_repo_html_metadata", return_value={}),
        ):
            items = collect_github_trending(session, timeout=5, limit=None)
        self.assertEqual(len(items), 3)
        for item in items:
            assert_required_fields(self, item, "github_trending")
            self.assertIn("/", item.title)
            self.assertTrue(item.url.startswith("https://github.com/"))

    def test_stable_id_keyed_by_date(self) -> None:
        # published_at 被压成当日 00:00 快照，id 必须随 date_key 变化，
        # 否则跨天重采会撞成同一行。
        html = load_fixture("github_trending.html")
        session = FakeSession({}, default=FakeResponse(text=html))
        with (
            patch.object(collect_news, "fetch_github_repo_metadata", return_value={}),
            patch.object(collect_news, "fetch_github_repo_html_metadata", return_value={}),
        ):
            item = collect_github_trending(session, timeout=5, limit=1)[0]
        id_day1 = item_stable_id(item, "2026-08-01")
        id_day2 = item_stable_id(item, "2026-08-02")
        self.assertNotEqual(id_day1, id_day2)


class ProductHuntFixtureTests(unittest.TestCase):
    def test_graphql_response_mapping(self) -> None:
        payload = json.loads(load_fixture("product_hunt_posts.json"))
        session = FakeSession({}, default=FakeResponse(json_payload=payload))
        with patch.dict("os.environ", {"PRODUCTHUNT_TOKEN": "test-token"}):
            items = collect_product_hunt(session, timeout=5, limit=None, date_key="2026-08-01")
        self.assertEqual(len(items), 3)
        for item in items:
            assert_required_fields(self, item, "product_hunt")
            # 原生 id 路径：stable_id 来自 product_hunt:<id>，与哈希算法无关。
            self.assertTrue((item.metadata or {}).get("stable_id", "").startswith("product_hunt:"))


class HackerNewsFixtureTests(unittest.TestCase):
    def test_item_mapping(self) -> None:
        story1 = json.loads(load_fixture("hn_item_1.json"))
        story2 = json.loads(load_fixture("hn_item_2.json"))

        def fake_fetch(session: object, url: str, timeout: int) -> object:
            if url.endswith("topstories.json"):
                return [story1["id"]]
            if url.endswith("beststories.json"):
                return [story2["id"]]
            if url.endswith(f"item/{story1['id']}.json"):
                return story1
            if url.endswith(f"item/{story2['id']}.json"):
                return story2
            raise AssertionError(f"unexpected URL: {url}")

        with patch.object(collect_news, "fetch_hn_json", side_effect=fake_fetch):
            items = collect_hacker_news(None, timeout=5, limit=None)
        self.assertEqual(len(items), 2)
        for item in items:
            assert_required_fields(self, item, "hacker_news")
            self.assertTrue((item.metadata or {}).get("stable_id", "").startswith("hacker_news:"))


class RssFixtureTests(unittest.TestCase):
    EMPTY_FEED_XML = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0"><channel><title>Empty Feed</title>'
        "<link>https://example.com/</link></channel></rss>"
    )

    def _write_config(self, path: Path) -> None:
        path.write_text(
            "rss:\n"
            "  - name: Fed Press\n"
            "    url: https://example.test/fed.xml\n"
            "    category: finance\n"
            "  - name: Empty Feed\n"
            "    url: https://example.test/empty.xml\n"
            "    category: finance\n",
            encoding="utf-8",
        )

    def _run_collect(self, date_key: str) -> list[NewsItem]:
        rss_xml = load_fixture("rss_feed.xml")
        session = FakeSession(
            {
                "https://example.test/fed.xml": FakeResponse(text=rss_xml),
                "https://example.test/empty.xml": FakeResponse(text=self.EMPTY_FEED_XML),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "sources.yaml"
            self._write_config(config)
            with patch.object(collect_news, "get_session", return_value=session):
                return collect_rss(config, timeout=5, limit=None, date_key=date_key)

    def test_rss_entries_parsed(self) -> None:
        # fixture 条目发布于 2026-07-31（UTC 14:00 → 上海 22:00）
        items = self._run_collect("2026-07-31")
        rss_items = [item for item in items if item.source_type == "rss"]
        self.assertEqual(len(rss_items), 2)
        for item in rss_items:
            assert_required_fields(self, item, "rss")

    def test_empty_feed_writes_diagnostic_row(self) -> None:
        items = self._run_collect("2026-07-31")
        diagnostics = [item for item in items if item.source_type == "error"]
        self.assertEqual(len(diagnostics), 1)
        diagnostic = diagnostics[0]
        self.assertEqual((diagnostic.metadata or {}).get("error_type"), "feed_empty")
        self.assertEqual(diagnostic.source_name, "rss:Empty Feed")
        self.assertEqual(diagnostic.url, "https://example.test/empty.xml")

    def test_feed_with_only_old_entries_is_empty_for_date(self) -> None:
        # 换一个日期：两个 feed 当日都为 0 条 → 两条 feed_empty 诊断行
        items = self._run_collect("2026-08-01")
        diagnostics = [item for item in items if item.source_type == "error"]
        self.assertEqual(len(diagnostics), 2)
        self.assertTrue(
            all((d.metadata or {}).get("error_type") == "feed_empty" for d in diagnostics)
        )


class Jin10WatermarkTests(unittest.TestCase):
    """翻页水位线：第 1 页永远抓全；第 2 页起过半已知即停止。"""

    class FakeClient:
        def __init__(self, pages: dict[str | None, dict]) -> None:
            self.pages = pages
            self.calls: list[str | None] = []

        def list_flash(self, cursor: str | None = None) -> dict:
            self.calls.append(cursor)
            return self.pages[cursor]

        def close(self) -> None:
            return None

    @staticmethod
    def _page(records: list[dict], next_cursor: str | None) -> dict:
        return {"data": {"items": records, "next_cursor": next_cursor}}

    def test_stops_when_page_mostly_known(self) -> None:
        pages = {
            None: self._page([{"id": 1}, {"id": 2}], "c2"),
            "c2": self._page([{"id": 3}, {"id": 4}], "c3"),
            "c3": self._page([{"id": 5}, {"id": 6}], None),
        }
        client = self.FakeClient(pages)
        with patch.object(collect_news, "Jin10McpClient", return_value=client):
            records = collect_news.fetch_jin10_mcp_records(
                record_known=lambda record: record["id"] >= 3
            )
        # 第 2 页 2/2 已知 → 停止，不请求第 3 页
        self.assertEqual(client.calls, [None, "c2"])
        self.assertEqual([r["id"] for r in records], [1, 2, 3, 4])

    def test_first_page_always_full_and_no_watermark_keeps_paging(self) -> None:
        pages = {
            None: self._page([{"id": 1}, {"id": 2}], "c2"),
            "c2": self._page([{"id": 3}, {"id": 4}], None),
        }
        client = self.FakeClient(pages)
        with patch.object(collect_news, "Jin10McpClient", return_value=client):
            # 即使第 1 页全部已知，也不触发水位线（第 1 页永远抓全）
            records = collect_news.fetch_jin10_mcp_records(
                record_known=lambda record: True
            )
        self.assertEqual(client.calls, [None, "c2"])
        self.assertEqual(len(records), 4)


class MakeStableIdTests(unittest.TestCase):
    def test_deterministic_and_field_sensitive(self) -> None:
        base = make_stable_id("rss", "Feed", "https://a", "2026-08-01T00:00:00+08:00", "T")
        self.assertEqual(
            base, make_stable_id("rss", "Feed", "https://a", "2026-08-01T00:00:00+08:00", "T")
        )
        self.assertNotEqual(
            base, make_stable_id("rss", "Feed", "https://b", "2026-08-01T00:00:00+08:00", "T")
        )

    def test_date_key_only_when_passed(self) -> None:
        without = make_stable_id("rss", "Feed", "https://a", None, "T")
        with_day1 = make_stable_id("rss", "Feed", "https://a", None, "T", date_key="2026-08-01")
        with_day2 = make_stable_id("rss", "Feed", "https://a", None, "T", date_key="2026-08-02")
        self.assertNotEqual(without, with_day1)
        self.assertNotEqual(with_day1, with_day2)

    def test_native_id_override_passthrough(self) -> None:
        item = NewsItem(
            source_type="hacker_news",
            source_name="Hacker News Hot",
            published_at=None,
            title="t",
            content="c",
            url="u",
            metadata={"stable_id": "hacker_news:123"},
        )
        self.assertEqual(item_stable_id(item, "2026-08-01"), "hacker_news:123")


if __name__ == "__main__":
    unittest.main()
