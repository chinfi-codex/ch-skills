from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from cninfo import cninfo_announcement_date, extend_cninfo_archive_window  # noqa: E402
from fetcher_core import StockDataFetcher  # noqa: E402


class CNInfoDateTests(unittest.TestCase):
    def test_archive_window_extends_only_the_end_date(self) -> None:
        self.assertEqual(
            extend_cninfo_archive_window("2026-07-20~2026-07-21"),
            "2026-07-20~2026-07-22",
        )

    def test_epoch_timestamp_is_rendered_in_china_market_timezone(self) -> None:
        timestamp_ms = int(datetime(2026, 7, 21, 16, 30, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(cninfo_announcement_date(timestamp_ms), "2026-07-22")

    @patch("fetcher_core.request_with_retry")
    def test_query_uses_and_reports_effective_archive_range(self, mocked_request) -> None:
        timestamp_ms = int(datetime(2026, 7, 21, 16, 30, tzinfo=timezone.utc).timestamp() * 1000)
        mocked_request.return_value.json.return_value = {
            "totalpages": 1,
            "totalRecordNum": 1,
            "announcements": [
                {
                    "announcementTime": timestamp_ms,
                    "secName": "测试公司",
                    "secCode": "300001",
                    "announcementTitle": "2026年半年度报告",
                    "adjunctUrl": "finalpage/2026-07-22/example.pdf",
                    "announcementId": "example",
                }
            ],
        }
        fetcher = StockDataFetcher.__new__(StockDataFetcher)
        fetcher._session = SimpleNamespace(post=object())

        result = fetcher.query_cninfo_announcement_page(
            stock="300001,org-id",
            date_range="2026-07-21~2026-07-21",
        )

        payload = mocked_request.call_args.kwargs["data"]
        self.assertEqual(payload["seDate"], "2026-07-21~2026-07-22")
        self.assertEqual(result["date_range"], "2026-07-21~2026-07-21")
        self.assertEqual(result["date_range_effective"], "2026-07-21~2026-07-22")
        self.assertEqual(result["announcements"][0]["announcement_time"], "2026-07-22")

    def test_get_announcements_exposes_effective_range_in_query_audit(self) -> None:
        fetcher = StockDataFetcher.__new__(StockDataFetcher)
        fetcher.resolve_cninfo_stock = lambda *_args, **_kwargs: "300001,org-id"
        fetcher.query_cninfo_announcement_page = lambda **_kwargs: {
            "date_range_effective": "2026-07-21~2026-07-22",
            "total_pages": 0,
            "total_records": 0,
            "announcements": [],
        }

        result = fetcher.get_announcements(
            "300001.SZ",
            date_range="2026-07-21~2026-07-21",
        )

        self.assertEqual(result["query"]["date"], "2026-07-21~2026-07-21")
        self.assertEqual(result["query"]["date_effective"], "2026-07-21~2026-07-22")


if __name__ == "__main__":
    unittest.main()
