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

    def test_cninfo_days_collapse_into_polite_ranges(self) -> None:
        self.assertEqual(
            scanner._cninfo_date_ranges(["20260710", "20260711", "20260713", "20260714"]),
            [("20260710", "20260711"), ("20260713", "20260714")],
        )

    def test_same_day_pdf_parser_conflict_uses_reconciliation_guard(self) -> None:
        cn_row = {"ann_date": "20260715", "net_profit_min": 1000, "net_profit_max": 2000}
        ts_row = {"ann_date": "20260715", "net_profit_min": -2000, "net_profit_max": -1000}
        self.assertTrue(scanner._same_day_structured_conflict(cn_row, ts_row))
        ts_row["ann_date"] = "20260716"
        self.assertFalse(scanner._same_day_structured_conflict(cn_row, ts_row))


if __name__ == "__main__":
    unittest.main()
