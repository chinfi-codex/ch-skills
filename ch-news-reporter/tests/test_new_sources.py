from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import collect_news  # noqa: E402
import http_utils  # noqa: E402


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_fake_opener(payload, captured: list[dict]):
    """http_utils opener 假实现:记录请求、返回固定 JSON payload。"""

    def opener(url, *, method, headers, body, timeout):
        captured.append({"url": url, "timeout": timeout})
        return http_utils.RawResponse(
            status=200, headers={}, body=json.dumps(payload)
        )

    return opener

class PolymarketMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.markets = load_fixture("polymarket_markets.json")
        assert len(self.markets) == 5

    def test_build_item_maps_fields(self) -> None:
        item = collect_news.build_polymarket_item(
            self.markets[0], "2026-08-01", collect_news.POLYMARKET_KEYWORDS
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.source_type, "polymarket")
        self.assertEqual(item.title, self.markets[0]["question"])
        # 快照语义:published_at 压成当日 00:00(Asia/Shanghai)
        self.assertEqual(item.published_at, "2026-08-01T00:00:00+08:00")
        self.assertIn("/event/", item.url)
        self.assertIn("赔率:", item.content)
        self.assertIn("Yes", item.content)
        metadata = item.metadata or {}
        for field in (
            "market_id",
            "slug",
            "outcomes",
            "outcomePrices",
            "volume",
            "volume24hr",
            "liquidity",
            "endDate",
        ):
            self.assertIn(field, metadata)
        self.assertEqual(metadata["outcomes"], ["Yes", "No"])
        expected_price = float(
            collect_news.parse_polymarket_list(self.markets[0]["outcomePrices"])[0]
        )
        self.assertAlmostEqual(metadata["outcomePrices"][0], expected_price, places=6)
        # stable_id 带 date_key:同日重跑去重、跨天各自成行
        self.assertEqual(
            metadata["stable_id"],
            f"polymarket:{self.markets[0]['id']}:2026-08-01",
        )
        self.assertEqual(item.raw, self.markets[0])

    def test_stable_id_changes_across_days(self) -> None:
        day1 = collect_news.build_polymarket_item(self.markets[0], "2026-08-01", [])
        day2 = collect_news.build_polymarket_item(self.markets[0], "2026-08-02", [])
        assert day1 is not None and day2 is not None
        self.assertNotEqual(
            (day1.metadata or {})["stable_id"],
            (day2.metadata or {})["stable_id"],
        )
        self.assertEqual(
            collect_news.item_stable_id(day1, "2026-08-01"),
            (day1.metadata or {})["stable_id"],
        )

    def test_json_string_list_fields(self) -> None:
        self.assertEqual(
            collect_news.parse_polymarket_list('["Yes", "No"]'), ["Yes", "No"]
        )
        self.assertEqual(collect_news.parse_polymarket_list(["A"]), ["A"])
        self.assertEqual(collect_news.parse_polymarket_list("not-json"), [])
        self.assertEqual(collect_news.parse_polymarket_list(None), [])


class PolymarketSelectionTests(unittest.TestCase):
    def test_keyword_hits(self) -> None:
        hits = collect_news.polymarket_keyword_hits(
            {"question": "Will the U.S. invade Iran before 2027?"},
            collect_news.POLYMARKET_KEYWORDS,
        )
        self.assertIn("iran", hits)
        self.assertIn("invade", hits)
        self.assertEqual(
            collect_news.polymarket_keyword_hits(
                {"question": "LoL: Karmine Corp vs Natus Vincere - Game 2 Winner"},
                collect_news.POLYMARKET_KEYWORDS,
            ),
            [],
        )

    def test_collect_keeps_free_top_plus_keyword_hits(self) -> None:
        markets = load_fixture("polymarket_markets.json")
        # 构造候选池:头部 3 个高热市场不看关键词,之后只有关键词命中的保留
        filler = [
            {**markets[2], "id": f"filler-{idx}", "volume24hr": 100 - idx}
            for idx in range(20)
        ]
        keyword_market = {
            **markets[4],
            "id": "kw-1",
            "question": "Will the U.S. invade Iran before 2027?",
            "volume24hr": 1,
        }
        payload = markets[:3] + filler + [keyword_market]
        captured: list[dict] = []
        items = collect_news.collect_polymarket(
            20, None, "2026-08-01", opener=make_fake_opener(payload, captured)
        )
        ids = [(item.metadata or {})["market_id"] for item in items]
        # free top 前 10 个不看关键词全保留(3 个高热 + 7 个 filler),
        # 之后的 filler 不命中关键词被过滤,尾部关键词市场保留
        self.assertIn("kw-1", ids)
        self.assertEqual(len(items), 10 + 1)
        self.assertNotIn("filler-10", ids)
        self.assertLessEqual(len(items), collect_news.POLYMARKET_ITEM_LIMIT)
        # 请求带超时与榜单参数
        request = captured[0]
        self.assertEqual(request["timeout"], 20)
        self.assertIn("order=volume24hr", request["url"])
        self.assertIn("active=true", request["url"])
        self.assertIn("closed=false", request["url"])

    def test_collect_respects_limit(self) -> None:
        markets = load_fixture("polymarket_markets.json")
        payload = [
            {**markets[idx % len(markets)], "id": f"m-{idx}", "volume24hr": 1000 - idx}
            for idx in range(40)
        ]
        items = collect_news.collect_polymarket(
            20, 7, "2026-08-01", opener=make_fake_opener(payload, [])
        )
        self.assertLessEqual(len(items), 7)


class HnCommentTests(unittest.TestCase):
    def test_parse_comment_strips_html(self) -> None:
        node = load_fixture("hn_comment.json")
        comment = collect_news.parse_hn_comment(node)
        self.assertIsNotNone(comment)
        assert comment is not None
        self.assertNotIn("<a href", comment["text"])
        self.assertNotIn("&#x2F;", comment["text"])
        self.assertIn("https://www.ynab.com/", comment["text"])
        self.assertEqual(comment["author"], node.get("by"))
        self.assertEqual(comment["kids_count"], len(node.get("kids") or []))

    def test_parse_comment_truncates(self) -> None:
        node = {
            "type": "comment",
            "by": "someone",
            "text": "<p>" + "x" * 1000,
            "kids": [1, 2],
        }
        comment = collect_news.parse_hn_comment(node)
        assert comment is not None
        self.assertEqual(len(comment["text"]), collect_news.HN_COMMENT_TEXT_LIMIT)

    def test_parse_comment_rejects_dead_deleted(self) -> None:
        self.assertIsNone(
            collect_news.parse_hn_comment({"type": "comment", "text": "hi", "dead": True})
        )
        self.assertIsNone(
            collect_news.parse_hn_comment(
                {"type": "comment", "text": "hi", "deleted": True}
            )
        )
        self.assertIsNone(collect_news.parse_hn_comment({"type": "story", "text": "hi"}))


class HnCommentBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_fetch = collect_news.fetch_hn_json
        self.calls: list[str] = []

    def tearDown(self) -> None:
        collect_news.fetch_hn_json = self.original_fetch

    def install_fake_fetch(self, fail_comments: bool = False) -> None:
        def fake_fetch(session, url, timeout):
            self.calls.append(url)
            if url.endswith("topstories.json"):
                return list(range(1, 21))
            if url.endswith("beststories.json"):
                return []
            item_id = int(url.rsplit("/", 1)[1].split(".")[0])
            if item_id >= 1000:  # comment
                if fail_comments:
                    raise RuntimeError("boom")
                return {
                    "id": item_id,
                    "type": "comment",
                    "by": "commenter",
                    "text": f"<p>comment {item_id}",
                    "kids": [],
                }
            return {
                "id": item_id,
                "type": "story",
                "title": f"story {item_id}",
                "score": item_id * 10,
                "time": 1_785_000_000,
                "kids": [1000 + item_id * 10 + offset for offset in range(8)],
            }

        collect_news.fetch_hn_json = fake_fetch

    def comment_calls(self) -> list[str]:
        return [
            url
            for url in self.calls
            if "/item/" in url and int(url.rsplit("/", 1)[1].split(".")[0]) >= 1000
        ]

    def test_budget_capped_and_top_score_selected(self) -> None:
        self.install_fake_fetch()
        items = collect_news.collect_hacker_news(
            session=None, timeout=5, limit=None, comment_story_limit=10, comment_limit=5
        )
        # 额外评论请求硬封顶 N*M = 50
        self.assertLessEqual(len(self.comment_calls()), 10 * 5)
        with_comments = [
            (item.metadata or {})["hacker_news_id"]
            for item in items
            if (item.metadata or {}).get("comments")
        ]
        # 分数最高的 10 个 story(id 11..20, score=id*10)有评论
        self.assertEqual(sorted(with_comments), list(range(11, 21)))
        for item in items:
            comments = (item.metadata or {}).get("comments") or []
            self.assertLessEqual(len(comments), 5)

    def test_comment_failure_degrades_silently(self) -> None:
        self.install_fake_fetch(fail_comments=True)
        items = collect_news.collect_hacker_news(
            session=None, timeout=5, limit=None, comment_story_limit=10, comment_limit=5
        )
        self.assertEqual(len(items), 20)
        for item in items:
            self.assertNotIn("comments", item.metadata or {})


if __name__ == "__main__":
    unittest.main()
