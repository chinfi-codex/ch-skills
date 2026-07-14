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


class PeBucketTests(unittest.TestCase):
    def test_upper_bound_is_inclusive(self) -> None:
        self.assertEqual(renderer._pe_bucket(15.0), "pe_le15")
        self.assertEqual(renderer._pe_bucket(15.01), "pe_15_30")
        self.assertEqual(renderer._pe_bucket(50.0), "pe_30_50")
        self.assertEqual(renderer._pe_bucket(100.0), "pe_50_100")
        self.assertEqual(renderer._pe_bucket(100.01), "pe_gt100")

    def test_missing_pe_is_na_bucket(self) -> None:
        self.assertEqual(renderer._pe_bucket(None), "pe_na")

    def test_bucket_follows_kf_headline_pe(self) -> None:
        # 扣非年化 PE = 33.3 → 30–50 档（头条口径），而非归母 25
        stock = _view(parent_annualized=4.0, parent_pe=25.0, kf_median=1.5)["stocks"][0]
        self.assertEqual(stock["pe_headline"], 33.3)
        self.assertEqual(stock["pe_bucket"], "pe_30_50")

    def test_bucket_falls_back_to_parent_when_kf_nonpositive(self) -> None:
        # 扣非≤0 → 头条退回归母 25 → 15–30 档
        stock = _view(parent_annualized=4.0, parent_pe=25.0, kf_median=-0.2)["stocks"][0]
        self.assertEqual(stock["pe_headline"], 25.0)
        self.assertEqual(stock["pe_bucket"], "pe_15_30")

    def test_loss_stock_is_na_bucket(self) -> None:
        stock = _view(parent_annualized=-1.0, parent_pe=None, kf_median=-0.2)["stocks"][0]
        self.assertEqual(stock["pe_bucket"], "pe_na")


class FilterBarTests(unittest.TestCase):
    def test_multiselect_bar_present_and_search_removed(self) -> None:
        html = renderer.render_html(_view(parent_annualized=4.0, parent_pe=25.0, kf_median=1.5))
        self.assertIn('id="msbar"', html)          # multi-select container built by JS
        self.assertIn('id="fclear"', html)          # 清空筛选 reset
        self.assertIn('"pe_buckets"', html)         # PE 分段项进了 DATA
        self.assertNotIn('id="q"', html)            # 旧的自由搜索框已移除

    def test_pe_bucket_options_cover_all_segments(self) -> None:
        opts = renderer.pe_bucket_options()
        self.assertEqual([o["v"] for o in opts],
                         ["pe_le15", "pe_15_30", "pe_30_50", "pe_50_100", "pe_gt100", "pe_na"])


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
