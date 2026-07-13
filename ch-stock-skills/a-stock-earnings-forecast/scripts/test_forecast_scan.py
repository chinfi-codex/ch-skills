from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import forecast_scan as scanner  # noqa: E402


class AnnouncementCalendarTests(unittest.TestCase):
    def test_calendar_days_include_weekend(self) -> None:
        self.assertEqual(
            scanner.calendar_days("20260710", "20260714"),
            ["20260710", "20260711", "20260712", "20260713", "20260714"],
        )

    def test_calendar_days_reject_reversed_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "窗口倒置"):
            scanner.calendar_days("20260714", "20260713")

    def test_beijing_clock_has_fixed_utc_plus_eight_offset(self) -> None:
        self.assertEqual(scanner.BEIJING_TZ.utcoffset(None), dt.timedelta(hours=8))


if __name__ == "__main__":
    unittest.main()
