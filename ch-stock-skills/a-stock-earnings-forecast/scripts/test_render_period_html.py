from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_period_html as renderer  # noqa: E402


def _view(parent_annualized: float, parent_pe: float | None, kf_median: float) -> dict:
    evidence = {
        "meta": {
            "ann_window": ["20260516", "20260714"],
            "ann_cutoff": "20260714",
            "ann_cutoff_stock_count": 0,
            "unique_stocks": 1,
            "clock_timezone": "Asia/Shanghai",
            "generated_at": "2026-07-13T20:30:00+08:00",
        },
        "stocks": [
            {
                "ts_code": "000001.SZ",
                "name": "样例公司",
                "ann_date": "20260710",
                "first_ann_date": "20260710",
                "net_profit": {"median_yi": parent_annualized / 2},
                "valuation": {
                    "total_mv_yi": 100.0,
                    "annualized_np_yi": parent_annualized,
                    "pe_annualized": parent_pe,
                    "pe_annualized_note": "ok" if parent_pe is not None else "np_nonpositive",
                },
            }
        ]
    }
    enrich = {
        "stocks": [
            {
                "ts_code": "000001.SZ",
                "found": True,
                "parsed": {
                    "kf_net_profit_yi": {
                        "point": kf_median,
                        "confidence": "high",
                    }
                },
            }
        ]
    }
    return renderer.build_view(
        "20260630",
        evidence,
        enrich,
        verdicts={},
        themes={},
        states={},
        trends={},
        overview_row=None,
        bars_map={},
        today=dt.date(2026, 7, 13),
    )


class KfPeTests(unittest.TestCase):
    def test_positive_parent_and_nonpositive_kf_sets_nonrecurring_warning(self) -> None:
        stock = _view(parent_annualized=4.0, parent_pe=25.0, kf_median=-0.2)["stocks"][0]
        self.assertTrue(stock["kf_nonrecurring"])
        self.assertIsNone(stock["pe_ann_kf"])

    def test_both_parent_and_kf_nonpositive_keep_loss_semantics(self) -> None:
        stock = _view(parent_annualized=-1.0, parent_pe=None, kf_median=-0.2)["stocks"][0]
        self.assertFalse(stock["kf_nonrecurring"])
        self.assertEqual(stock["pe_ann_note"], "np_nonpositive")

    def test_positive_kf_is_annualized_for_headline_pe(self) -> None:
        stock = _view(parent_annualized=4.0, parent_pe=25.0, kf_median=1.5)["stocks"][0]
        self.assertEqual(stock["kf_ann_np_yi"], 3.0)
        self.assertEqual(stock["pe_ann_kf"], 33.3)
        self.assertFalse(stock["kf_nonrecurring"])


class AnnouncementCutoffTests(unittest.TestCase):
    def test_strict_cutoff_accepts_matching_beijing_evidence(self) -> None:
        evidence = {
            "meta": {
                "ann_window": ["20260516", "20260714"],
                "ann_cutoff": "20260714",
                "ann_cutoff_stock_count": 1,
                "unique_stocks": 2,
                "clock_timezone": "Asia/Shanghai",
                "generated_at": "2026-07-13T20:30:00+08:00",
            },
            "stocks": [{"ann_date": "20260713"}, {"ann_date": "20260714"}],
        }
        info = renderer._validate_evidence_cutoff(evidence, "20260714")
        self.assertEqual(info["ann_cutoff_stock_count"], 1)

    def test_strict_cutoff_rejects_stale_evidence(self) -> None:
        evidence = {
            "meta": {
                "ann_window": ["20260516", "20260713"],
                "ann_cutoff": "20260713",
                "ann_cutoff_stock_count": 1,
                "unique_stocks": 1,
                "clock_timezone": "Asia/Shanghai",
            },
            "stocks": [{"ann_date": "20260713"}],
        }
        with self.assertRaisesRegex(ValueError, "门禁失败"):
            renderer._validate_evidence_cutoff(evidence, "20260714")

    def test_html_surfaces_next_day_cutoff_even_when_count_is_zero(self) -> None:
        html = renderer.render_html(_view(parent_annualized=4.0, parent_pe=25.0, kf_median=1.5))
        self.assertIn("公告扫描截至 2026-07-14（北京时间次日口径 · 截止日 0 家）", html)


if __name__ == "__main__":
    unittest.main()
