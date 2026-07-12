from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tavily_search as search


class TavilyDateWindowTests(unittest.TestCase):
    @patch("requests.post")
    def test_absolute_date_window_is_forwarded_to_api(self, post: Mock) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "results": [
                {
                    "title": "Example",
                    "url": "https://example.com/item",
                    "published_date": "2026-07-09",
                    "score": 0.9,
                    "content": "Source text",
                }
            ]
        }
        post.return_value = response

        result = search.tavily_search(
            "storage IPO",
            start_date="2026-07-01",
            end_date="2026-07-10",
            key="test-key",
        )

        self.assertNotIn("error", result)
        self.assertEqual(set(result), {"query", "results"})
        self.assertEqual(
            set(result["results"][0]),
            {"title", "url", "published_date", "content"},
        )
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["start_date"], "2026-07-01")
        self.assertEqual(payload["end_date"], "2026-07-10")
        self.assertNotIn("days", payload)

    @patch("requests.post")
    def test_days_and_absolute_window_are_mutually_exclusive(self, post: Mock) -> None:
        result = search.tavily_search(
            "spaceflight",
            days=7,
            start_date="2026-07-01",
            key="test-key",
        )

        self.assertEqual(result["error"]["code"], "invalid_parameters")
        self.assertIn("cannot be combined", result["error"]["detail"])
        post.assert_not_called()

    @patch("requests.post")
    def test_dates_require_strict_yyyy_mm_dd_format(self, post: Mock) -> None:
        result = search.tavily_search(
            "spaceflight",
            start_date="2026-7-1",
            key="test-key",
        )

        self.assertEqual(result["error"]["code"], "invalid_parameters")
        self.assertIn("YYYY-MM-DD", result["error"]["detail"])
        post.assert_not_called()

    @patch("requests.post")
    def test_dates_must_form_forward_window(self, post: Mock) -> None:
        result = search.tavily_search(
            "spaceflight",
            start_date="2026-07-11",
            end_date="2026-07-10",
            key="test-key",
        )

        self.assertEqual(result["error"]["code"], "invalid_parameters")
        self.assertIn("on or before", result["error"]["detail"])
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
