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


class _MemoryStore:
    available = False

    def __init__(self) -> None:
        self.income_upserts: list = []
        self.basic: dict = {}

    def load_income_many(self, codes):
        return {}

    def upsert_income_many(self, records):
        self.income_upserts.extend(records)

    def load_basic(self, codes):
        return dict(self.basic)

    def upsert_basic(self, rows):
        self.basic.update(rows)


class TushareUnavailableTests(unittest.TestCase):
    """No token / no permission must degrade to CNInfo-only, never crash or poison caches."""

    def setUp(self) -> None:
        self.pro = scanner.TushareUnavailable("未配置 TUSHARE_TOKEN")

    def test_failed_income_is_not_cached_as_null(self) -> None:
        store = _MemoryStore()
        result, calls, failed = scanner.gather_income(
            self.pro, store, ["000626.SZ", "301308.SZ"], "20260930", False, 2)
        self.assertEqual((calls, failed), (2, 2))
        self.assertEqual(store.income_upserts, [])
        self.assertEqual(result["000626.SZ"], {})

    def test_basic_and_calendar_degrade_with_note(self) -> None:
        notes: list = []
        self.assertEqual(scanner.resolve_basic(self.pro, _MemoryStore(), ["000626.SZ"], notes), {})
        self.assertIn("stock_basic", notes[0])
        self.assertEqual(scanner.trading_days(self.pro, "20261001", "20261023"), [])
        mv, asof = scanner.fetch_total_mv(self.pro, dt.date(2026, 10, 23), notes)
        self.assertEqual((mv, asof), ({}, None))

    def test_record_name_falls_back_to_announcement_short_name(self) -> None:
        import pandas as pd

        row = pd.Series({"ts_code": "000626.SZ", "type": "预增", "net_profit_min": 31856.0,
                         "net_profit_max": 35977.0, "last_parent_net": 3218.0,
                         "p_change_min": 890.0, "p_change_max": 1018.0, "ann_date": "20261015"})
        rec = scanner.build_stock_record(row, scanner.CumCache({}), "20260930", {}, 0.0,
                                         source_info={"sec_name": "远大控股"})
        self.assertEqual(rec["name"], "远大控股")
        self.assertEqual(rec["net_profit"]["last_parent_yi"], 0.3218)


class VerdictPayloadShapeTests(unittest.TestCase):
    def test_wrong_keys_are_rejected_not_skipped(self) -> None:
        import verdict

        allowed = ("period", "records", "theme_trends", "period_overview")
        errors = verdict.check_payload_shape({"stocks": [], "period_overview": {"t": 1}}, allowed)
        self.assertEqual(len(errors), 3)
        self.assertEqual(verdict.check_payload_shape(
            {"records": [], "period_overview": "综述"}, allowed), [])


if __name__ == "__main__":
    unittest.main()
