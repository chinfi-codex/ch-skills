from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deepseek_search as search


def _mock_response(results: list[dict]) -> Mock:
    response = Mock(status_code=200)
    response.json.return_value = {"results": results}
    return response


class DeepSeekSearchContractTests(unittest.TestCase):
    @patch("requests.post")
    def test_results_are_normalized_to_shared_contract(self, post: Mock) -> None:
        post.return_value = _mock_response(
            [
                {
                    "rerank_score": 2.4,
                    "title": "Example",
                    "url": "https://example.com/item",
                    "snippet": "short",
                    "summary": "long",
                    "content": "full text",
                    "site_name": "Example News",
                    "date": "2026-06-01T00:00:00+00:00",
                    "authority": 3,
                }
            ]
        )

        result = search.deepseek_search("storage IPO", count=8, key="test-key")

        self.assertNotIn("error", result)
        self.assertEqual(set(result), {"query", "results"})
        self.assertEqual(
            set(result["results"][0]),
            {
                "title",
                "url",
                "published_date",
                "snippet",
                "summary",
                "content",
                "site_name",
                "rerank_score",
                "authority",
            },
        )
        self.assertEqual(
            result["results"][0]["published_date"], "2026-06-01T00:00:00+00:00"
        )
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["query"], "storage IPO")
        self.assertEqual(payload["count"], 8)
        self.assertFalse(payload["need_content"])
        self.assertNotIn("ori_question", payload)
        self.assertNotIn("localization", payload)

    @patch("requests.post")
    def test_optional_params_are_forwarded_only_when_given(self, post: Mock) -> None:
        post.return_value = _mock_response([])

        search.deepseek_search(
            "寒武纪 业绩预告",
            need_content=True,
            ori_question="寒武纪最近业绩怎么样",
            localization="203.0.113.7",
            key="test-key",
        )

        payload = post.call_args.kwargs["json"]
        self.assertTrue(payload["need_content"])
        self.assertEqual(payload["ori_question"], "寒武纪最近业绩怎么样")
        self.assertEqual(payload["localization"], "203.0.113.7")
        self.assertTrue(
            post.call_args.kwargs["headers"]["Authorization"].startswith("Bearer ")
        )

    @patch("requests.post")
    def test_query_is_capped_at_128_unicode_chars(self, post: Mock) -> None:
        result = search.deepseek_search("字" * 129, key="test-key")

        self.assertEqual(result["error"]["code"], "invalid_parameters")
        post.assert_not_called()

    @patch("requests.post")
    def test_count_must_be_within_api_bounds(self, post: Mock) -> None:
        for bad_count in (0, 51):
            with self.subTest(count=bad_count):
                result = search.deepseek_search("query", count=bad_count, key="test-key")
                self.assertEqual(result["error"]["code"], "invalid_parameters")
        post.assert_not_called()

    @patch("requests.post")
    def test_blank_query_is_rejected_locally(self, post: Mock) -> None:
        result = search.deepseek_search("   ", key="test-key")

        self.assertEqual(result["error"]["code"], "invalid_parameters")
        post.assert_not_called()

    @patch("deepseek_search.get_deepseek_key", return_value="")
    @patch("requests.post")
    def test_missing_key_returns_structured_error(
        self, post: Mock, _get_key: Mock
    ) -> None:
        result = search.deepseek_search("query")

        self.assertEqual(result["error"]["code"], "missing_api_key")
        self.assertIn("DEEPSEEK_API_KEY", result["error"]["detail"])
        post.assert_not_called()

    @patch("requests.post")
    def test_http_failure_is_structured(self, post: Mock) -> None:
        post.return_value = Mock(status_code=401, text="auth failed")

        result = search.deepseek_search("query", key="bad-key")

        self.assertEqual(result["error"]["code"], "http_401")
        self.assertEqual(result["error"]["detail"], "auth failed")


if __name__ == "__main__":
    unittest.main()
