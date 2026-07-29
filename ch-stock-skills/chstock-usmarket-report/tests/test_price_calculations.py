from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_report.py"
SPEC = importlib.util.spec_from_file_location("usmarket_generate_report", SCRIPT_PATH)
assert SPEC and SPEC.loader
REPORT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = REPORT
SPEC.loader.exec_module(REPORT)


def history(
    closes: list[float],
    *,
    start_day: int = 1,
    volumes: list[int] | None = None,
) -> list[dict]:
    return [
        {
            "date": date(2026, 7, start_day + index),
            "close": close,
            "volume": (volumes or [1_000_000] * len(closes))[index],
        }
        for index, close in enumerate(closes)
    ]


class PriceCalculationTests(unittest.TestCase):
    def test_five_and_twenty_day_returns_use_full_return_intervals(self) -> None:
        rows = history([float(value) for value in range(100, 125)])

        snapshot = REPORT.build_stock_snapshot("TEST", rows, rows[-1]["date"])

        self.assertIsNotNone(snapshot)
        assert snapshot
        self.assertAlmostEqual(snapshot["five_day_trend_pct"], (124 / 119 - 1) * 100, places=4)
        self.assertAlmostEqual(snapshot["trend_20d_pct"], (124 / 104 - 1) * 100, places=4)
        self.assertEqual(snapshot["five_day_start_date"], rows[-6]["date"].isoformat())
        self.assertEqual(snapshot["trend_20d_start_date"], rows[-21]["date"].isoformat())

    def test_volume_ratio_requires_twenty_complete_prior_sessions(self) -> None:
        full_rows = history(
            [100.0] * 21,
            volumes=[1_000_000] * 20 + [2_000_000],
        )
        short_rows = full_rows[-20:]

        self.assertEqual(REPORT._volume_ratio_20d(full_rows, 20), 2.0)
        self.assertIsNone(REPORT._volume_ratio_20d(short_rows, 19))

    def test_relative_returns_require_matching_start_and_end_dates(self) -> None:
        benchmark_rows = history([100.0 + value for value in range(7)], start_day=1)
        stock_rows = history([50.0 + value for value in range(6)], start_day=1)
        report_date = benchmark_rows[-1]["date"]
        benchmark = REPORT.build_stock_snapshot("QQQ", benchmark_rows, report_date)
        stock = REPORT.build_stock_snapshot("TEST", stock_rows, report_date)

        REPORT.inject_relative_fields(stock, benchmark)

        assert stock
        self.assertFalse(stock["vs_qqq_1d_date_aligned"])
        self.assertFalse(stock["vs_qqq_5d_date_aligned"])
        self.assertNotIn("vs_qqq_1d", stock)
        self.assertNotIn("vs_qqq_5d", stock)

    def test_relative_returns_are_calculated_when_windows_match(self) -> None:
        benchmark_rows = history([100.0 + value for value in range(7)])
        stock_rows = history([50.0 + 2 * value for value in range(7)])
        report_date = benchmark_rows[-1]["date"]
        benchmark = REPORT.build_stock_snapshot("QQQ", benchmark_rows, report_date)
        stock = REPORT.build_stock_snapshot("TEST", stock_rows, report_date)

        REPORT.inject_relative_fields(stock, benchmark)

        assert stock and benchmark
        self.assertTrue(stock["vs_qqq_1d_date_aligned"])
        self.assertTrue(stock["vs_qqq_5d_date_aligned"])
        self.assertAlmostEqual(
            stock["vs_qqq_1d"],
            round(stock["change_pct"] - benchmark["change_pct"], 4),
        )
        self.assertAlmostEqual(
            stock["vs_qqq_5d"],
            round(stock["five_day_trend_pct"] - benchmark["five_day_trend_pct"], 4),
        )

    def test_five_day_excess_requires_matching_window_start(self) -> None:
        benchmark_rows = history([100.0 + value for value in range(8)])
        stock_rows = [
            row
            for index, row in enumerate(history([50.0 + value for value in range(8)]))
            if index != 2
        ]
        report_date = benchmark_rows[-1]["date"]
        benchmark = REPORT.build_stock_snapshot("QQQ", benchmark_rows, report_date)
        stock = REPORT.build_stock_snapshot("TEST", stock_rows, report_date)

        REPORT.inject_relative_fields(stock, benchmark)

        assert stock
        self.assertTrue(stock["vs_qqq_1d_date_aligned"])
        self.assertIn("vs_qqq_1d", stock)
        self.assertFalse(stock["vs_qqq_5d_date_aligned"])
        self.assertNotIn("vs_qqq_5d", stock)

    def test_incomplete_current_daily_bar_is_excluded_until_regular_close(self) -> None:
        eastern = ZoneInfo("America/New_York")
        regular_end = datetime(2026, 7, 29, 16, 0, tzinfo=eastern).timestamp()
        rows = [
            {"date": date(2026, 7, 28), "close": 100.0},
            {"date": date(2026, 7, 29), "close": 101.0},
        ]
        meta = {
            "exchangeTimezoneName": "America/New_York",
            "currentTradingPeriod": {"regular": {"end": regular_end}},
        }

        during_session = datetime(2026, 7, 29, 15, 30, tzinfo=eastern).timestamp()
        after_close = datetime(2026, 7, 29, 16, 1, tzinfo=eastern).timestamp()

        self.assertEqual(
            REPORT._exclude_incomplete_daily_bar(rows, meta, during_session),
            rows[:1],
        )
        self.assertEqual(
            REPORT._exclude_incomplete_daily_bar(rows, meta, after_close),
            rows,
        )


if __name__ == "__main__":
    unittest.main()
