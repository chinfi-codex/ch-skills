from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from cninfo_announcement_search import _announcement_date, _extend_archive_end  # noqa: E402


class CNInfoDateTests(unittest.TestCase):
    def test_archive_window_extends_only_the_end_date(self) -> None:
        self.assertEqual(
            _extend_archive_end("2026-07-20~2026-07-21"),
            "2026-07-20~2026-07-22",
        )

    def test_epoch_timestamp_is_rendered_in_china_market_timezone(self) -> None:
        timestamp_ms = int(datetime(2026, 7, 21, 16, 30, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(_announcement_date(timestamp_ms), "2026-07-22")


if __name__ == "__main__":
    unittest.main()
