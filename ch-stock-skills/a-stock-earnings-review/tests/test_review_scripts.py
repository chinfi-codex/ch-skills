from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import market_data  # noqa: E402
import price_context as pc  # noqa: E402
import season_merge as sm  # noqa: E402


def _bars(closes, start="2026-01-01", vols=None, adj=None):
    d0 = dt.date.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        out.append({"date": (d0 + dt.timedelta(days=i)).isoformat(), "open": c, "high": c * 1.01,
                    "low": c * 0.99, "close": c, "vol": (vols[i] if vols else 100.0),
                    "adj": (adj[i] if adj else 1.0)})
    return out


class PriceContextTests(unittest.TestCase):
    def test_run_up_and_excess_vs_benchmark(self) -> None:
        stock = _bars([10.0] * 40 + [10.0 + 0.5 * i for i in range(1, 21)] + [20.0] * 5)
        bench = pc.Benchmark(_bars([100.0] * 60 + [110.0] * 5))
        anchor = stock[60]["date"]
        ev = pc.event_block(stock, anchor, bench, gap_min=2.0)
        self.assertEqual(ev["reaction_date"], anchor)
        self.assertEqual(ev["run_up"]["d20"]["ret_pct"], 100.0)   # 10 -> 20 over the 20 bars before
        self.assertEqual(ev["run_up"]["d20"]["excess_pp"], 100.0)  # benchmark flat in that window

    def test_gap_up_filled_when_price_trades_back_below_pre_close(self) -> None:
        stock = _bars([10.0] * 30 + [11.0, 9.5, 9.6])
        stock[30]["open"] = 10.6   # +6% gap
        ev = pc.event_block(stock, stock[30]["date"], None, gap_min=2.0)
        self.assertEqual(ev["reaction"]["gap_dir"], "up")
        self.assertEqual(ev["reaction"]["gap_status"], "filled")
        self.assertIsNone(ev["run_up"]["d20"]["excess_pp"])  # no benchmark

    def test_adjustment_factor_neutralises_split(self) -> None:
        # A 2-for-1 split halves the raw close; the 后复权 factor doubles from then on.
        closes = [20.0] * 30 + [10.0] * 30
        adj = [1.0] * 30 + [2.0] * 30
        stock = _bars(closes, adj=adj)
        ret = pc.window_return(stock, 59, 50, None)
        self.assertEqual(ret["ret_pct"], 0.0)

    def test_hfq_factor_mapping_uses_latest_ex_date(self) -> None:
        bars = _bars([1.0] * 3, start="2026-06-03")
        market_data._apply_factors(bars, [("1900-01-01", 1.0), ("2026-06-04", 1.5)])
        self.assertEqual([b["adj"] for b in bars], [1.0, 1.5, 1.5])

    def test_anchor_after_last_bar_is_pending(self) -> None:
        stock = _bars([10.0] * 5)
        self.assertEqual(pc.event_block(stock, "2027-01-01", None, 2.0)["status"], "pending")

    def test_valuation_bands_ignore_losses_and_decompose_rerating(self) -> None:
        d0 = dt.date(2021, 1, 1)
        rows = []
        for i in range(1500):
            pe = -5.0 if i % 10 == 0 else 20.0 + i * 0.01
            rows.append({"date": (d0 + dt.timedelta(days=i)).isoformat(), "pe_ttm": pe, "pb": 2.0,
                         "ps_ttm": 3.0, "total_mv_yi": 100.0 * 1.002 ** i})
        rows[-1]["pe_ttm"] = 40.0
        bars = [{"date": r["date"], "close": 1.0, "adj": 1.0} for r in rows]
        val = pc.valuation_block(rows, bars)
        y1 = val["bands"]["pe_ttm"]["y1"]
        self.assertGreater(y1["min"], 0)
        self.assertEqual(y1["current_pct"], 100.0)
        dec = val["decomposition"]
        self.assertIsNotNone(dec["rerating_share"])  # market cap and PE both up
        self.assertTrue(0 < dec["rerating_share"] < 1)
        self.assertAlmostEqual((1 + dec["mv_change_pct"] / 100),
                               (1 + dec["pe_ttm_change_pct"] / 100) * (1 + dec["ttm_earnings_change_pct"] / 100),
                               places=2)

    def test_manual_anchor_parsing(self) -> None:
        self.assertEqual(pc.parse_manual_anchors(["300604.sz:forecast:20261022"]),
                         {"300604.SZ": {"forecast": "2026-10-22"}})
        with self.assertRaises(ValueError):
            pc.parse_manual_anchors(["300604.SZ:rumor:20261022"])


class SeasonMergeTests(unittest.TestCase):
    forecast = {"meta": {"unique_stocks": 2}, "stocks": [
        {"ts_code": "300604.SZ", "name": "长川科技", "industry": "", "type": "预增",
         "ann_date": "20261022", "first_ann_date": "20261020",
         "net_profit": {"min_yi": 17.15, "max_yi": 18.15, "median_yi": 17.65, "last_parent_yi": 8.65},
         "profit_growth": {"cum_yoy_pct": 104.0, "single_q_yoy_pct": None, "qoq_pct": None},
         "flags": {"accelerating": False}, "change_reason": "需求增长" * 200,
         "source": {"parent_base": {"source": "table", "confidence": "high"}}},
        {"ts_code": "000626.SZ", "name": "远大控股", "type": "预增", "ann_date": "20261015",
         "net_profit": {"median_yi": 3.39}, "profit_growth": {"cum_yoy_pct": 954.0}, "flags": {}},
    ]}
    enrich = {"stocks": [{"ts_code": "300604.SZ", "parsed": {
        "kf_net_profit_yi": {"low": 16.67, "high": 17.67, "point": None, "confidence": "high"}}}]}
    universe = {"meta": {"released_count": 2}, "universe": [
        {"ts_code": "300604.SZ", "name": "长川科技", "industry": "半导体", "ann_date": "20261023",
         "np_single_yoy_pct": 120.5, "np_cum_yoy_pct": 103.0, "income_loaded": True, "in_range": "within"},
        {"ts_code": "600519.SH", "name": "贵州茅台", "industry": "白酒", "ann_date": "20261022",
         "income_loaded": False},
    ]}

    def test_modes_headline_and_anchors(self) -> None:
        verdicts = {"forecast": {"300604.SZ": {"tier": "强", "reason": "r", "caveat": "c"}}, "formal": {}}
        merged = sm.merge("20260930", self.forecast, self.enrich, self.universe, None, verdicts,
                          {"date": "2026-10-23", "stocks": {"000626.SZ": {"board": "炼化及贸易",
                                                                         "total_mv_yi": 80.0, "pe_ttm": 30.0}}})
        by = {s["ts_code"]: s for s in merged["stocks"]}
        cc = by["300604.SZ"]
        self.assertEqual(cc["mode"], "deep")
        self.assertEqual(cc["headline"]["source"], "formal")
        self.assertEqual(cc["anchors"], {"forecast": "20261020", "formal": "20261023"})
        self.assertEqual(cc["forecast"]["kf_median_yi"], 17.17)
        self.assertTrue(cc["forecast"]["change_reason"].endswith("…"))
        self.assertEqual(cc["verdicts"]["forecast"]["tier"], "强")
        self.assertEqual(by["000626.SZ"]["mode"], "inference")
        self.assertEqual(by["000626.SZ"]["industry"], "炼化及贸易")
        self.assertEqual(by["000626.SZ"]["industry_source"], "eastmoney_board")
        # Formal record without an income statement is not deep yet.
        self.assertEqual(by["600519.SH"]["mode"], "inference")
        self.assertEqual(by["600519.SH"]["headline"]["source"], "formal_incomplete")
        self.assertEqual(merged["stocks"][0]["ts_code"], "300604.SZ")  # deep first

    def test_forecast_only_period(self) -> None:
        merged = sm.merge("20260930", self.forecast, None, None, None, {"forecast": {}, "formal": {}})
        self.assertTrue(all(s["mode"] == "inference" for s in merged["stocks"]))
        rollup = {r["industry"]: r for r in merged["industry_rollup"]}
        self.assertEqual(rollup["未归类"]["forecast_n"], 2)


if __name__ == "__main__":
    unittest.main()
