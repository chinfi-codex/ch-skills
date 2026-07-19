from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from profile_config import _validate_topic  # noqa: E402
from retrievers import vault_fs, web_search  # noqa: E402


class HistoricalWebSearchTests(unittest.TestCase):
    def test_historical_date_never_calls_live_tavily(self) -> None:
        topic = {
            "time_window_days": 3,
            "channels": {
                "web_search": {
                    "queries": ["future leakage guard"],
                    "max_queries_per_day": 1,
                }
            },
        }
        with (
            patch.object(
                web_search,
                "_usage",
                return_value={"by_topic": {}, "total": 0},
            ),
            patch.object(web_search, "run_query", return_value=[]),
            patch.object(
                web_search,
                "tavily_search",
                side_effect=AssertionError("historical replay called Tavily"),
            ),
            patch.object(
                web_search,
                "get_tavily_key",
                side_effect=AssertionError("historical replay requested API key"),
            ),
        ):
            result = web_search.retrieve(
                object(), "test_topic", topic, "2000-01-01", settings={}
            )

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["budget"]["ran_now"], 0)
        self.assertEqual(result["budget"]["skipped_historical"], 1)
        self.assertTrue(any("不执行实时 Tavily" in item for item in result["warnings"]))


class VaultPathBoundaryTests(unittest.TestCase):
    def test_parent_path_cannot_escape_vault_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            vault = base / "vault"
            outside = base / "outside"
            vault.mkdir()
            outside.mkdir()
            (outside / "secret.md").write_text("# Secret\n\nRubin", encoding="utf-8")
            topic = {
                "keywords": ["Rubin"],
                "channels": {
                    "alpha_vault": {"enabled": True, "paths": ["../outside"]}
                },
            }

            result = vault_fs.retrieve(topic, {"vault_root": str(vault)})

        self.assertEqual(result["count"], 0)
        self.assertEqual(result["notes"], [])
        self.assertTrue(any("路径越界" in item for item in result["warnings"]))


class CustomTopicConfigValidationTests(unittest.TestCase):
    def test_zero_time_window_is_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "time_window_days 必须 >= 1"):
            _validate_topic("test_topic", {"time_window_days": 0})

    def test_negative_open_budget_is_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "open_budget 必须 >= 0"):
            _validate_topic("test_topic", {"open_budget": -1})


if __name__ == "__main__":
    unittest.main()
