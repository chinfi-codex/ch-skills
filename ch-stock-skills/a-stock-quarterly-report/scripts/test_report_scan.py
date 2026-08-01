#!/usr/bin/env python3
"""Offline tests for the deterministic core of report_scan.py.

No network, no database. These cover the arithmetic that everything downstream
trusts — period walking, single-quarter decomposition across a year boundary,
the base guards, and the query-semantics trap that silently drops the current
report from three of the four statement endpoints.

    python3 scripts/test_report_scan.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import report_scan as rs  # noqa: E402
import cninfo_client as cn  # noqa: E402
import render_period_html as renderer  # noqa: E402
import verdict  # noqa: E402
from store import qfq_adjust_bars  # noqa: E402


def test_period_arithmetic() -> None:
    assert rs.quarter_of("20260630") == (2026, 2)
    assert rs.prev_cumulative_period("20260630") == "20260331"
    assert rs.prev_cumulative_period("20260331") is None
    assert rs.prev_year_period("20260630") == "20250630"
    assert rs.period_label("20261231") == "2026年报"
    assert rs.quarters_elapsed("20260930") == 3
    assert rs.latest_quarter_end(dt.date(2026, 7, 25)) == "20260630"
    assert rs.latest_quarter_end(dt.date(2026, 1, 5)) == "20251231"
    try:
        rs.quarter_of("20260731")
    except ValueError:
        pass
    else:  # pragma: no cover - guard must reject non-quarter ends
        raise AssertionError("non-quarter period should raise")


def test_needed_periods_cover_the_decomposition() -> None:
    # H1 needs both cumulative legs of this year and last year.
    assert set(rs.needed_periods("20260630")) >= {
        "20260630", "20260331", "20250630", "20250331", "20251231"}
    # Q1's sequential comparison crosses the year end, so last year's Q3 and
    # annual must be present to reconstruct last year's Q4.
    assert {"20251231", "20250930"} <= set(rs.needed_periods("20260331"))


def test_fetch_range_uses_announcement_window() -> None:
    """income/balancesheet/cashflow filter on ann_date, so the window must run
    past the period end — otherwise the current report is invisible."""
    start, end = rs.fetch_range("20260331", "20260726")
    assert start == "20250101"
    assert end == "20260726" > "20260331"


def test_single_quarter_decomposition() -> None:
    series = rs.Series({
        "20250331": {"revenue": 100.0}, "20250630": {"revenue": 250.0},
        "20250930": {"revenue": 400.0}, "20251231": {"revenue": 600.0},
        "20260331": {"revenue": 130.0}, "20260630": {"revenue": 300.0},
    })
    assert series.single("20260630", "revenue") == 170.0        # 300 − 130
    assert series.single("20260331", "revenue") == 130.0        # Q1 cumulative is the quarter
    assert series.prev_single("20260630", "revenue") == 130.0   # previous quarter, same year
    assert series.prev_single("20260331", "revenue") == 200.0   # last year's Q4 = 600 − 400


def test_single_quarter_missing_base_is_none_not_cumulative() -> None:
    """A missing previous cumulative must not silently return the cumulative —
    that would report H1's profit as a single quarter."""
    series = rs.Series({"20260630": {"revenue": 300.0}})
    assert series.single("20260630", "revenue") is None
    assert series.prev_single("20260630", "revenue") is None


def test_financial_firm_revenue_falls_back_to_total_revenue() -> None:
    series = rs.Series({
        "20260331": {"total_revenue": 120.0},
        "20260630": {"total_revenue": 260.0},
    })
    assert series.cum("20260630", "revenue") == 260.0
    assert series.single("20260630", "revenue") == 140.0


def test_refetch_merge_preserves_complete_cached_statement() -> None:
    old = {"20260331": {
        "revenue": 100.0, "n_cashflow_act": 20.0,
        "_sources": {"income", "cashflow"},
    }}
    fresh = {"20260331": {
        "revenue": 110.0, "_sources": {"income"},
    }}
    merged = rs.merge_statement_periods(old, fresh)
    assert merged["20260331"]["revenue"] == 110.0
    assert merged["20260331"]["n_cashflow_act"] == 20.0
    assert merged["20260331"]["_sources"] == {"income", "cashflow"}


def test_statement_fetch_diagnostics_distinguish_lag_from_failure() -> None:
    class Pro:
        def income(self, **_: object) -> pd.DataFrame:
            return pd.DataFrame([{
                "end_date": "20260630", "report_type": "1", "update_flag": "0",
                "ann_date": "20260725", "revenue": 100.0,
            }])

        def fina_indicator(self, **_: object) -> pd.DataFrame:
            return pd.DataFrame()

        def cashflow(self, **_: object) -> pd.DataFrame:
            raise RuntimeError("temporary network failure")

        def balancesheet(self, **_: object) -> pd.DataFrame:
            return pd.DataFrame([{
                "end_date": "20250331", "report_type": "1", "update_flag": "0",
                "ann_date": "20250425", "total_assets": 200.0,
            }])

    merged, diagnostics = rs.fetch_statements_for_code(
        Pro(), "000001.SZ", "20260630", "20260802",
        {"20260630", "20250331"}, ["income", "fina", "cashflow", "balance"])
    assert merged["20260630"]["_sources"] == {"income"}
    assert diagnostics["income"]["status"] == "ok"
    assert diagnostics["fina"]["status"] == "current_period_not_returned"
    assert diagnostics["balance"]["status"] == "current_period_not_returned"
    assert diagnostics["cashflow"]["status"] == "request_failed"
    assert "network failure" in diagnostics["cashflow"]["error"]


def test_html_defaults_to_assigned_themes_only() -> None:
    evidence = {
        "meta": {
            "period": "20260630", "ann_cutoff": "20260802",
            "ann_cutoff_stock_count": 0, "clock_timezone": "Asia/Shanghai",
            "released_count": 2, "with_statements": 2,
        },
        "stocks": [
            {"ts_code": "000001.SZ", "name": "A", "growth": {}, "source": {}},
            {"ts_code": "000002.SZ", "name": "B", "growth": {}, "source": {}},
        ],
    }
    verdicts = {
        "000001.SZ": {"theme_id": "TH-1", "tier": "强"},
        "000002.SZ": {"theme_id": None, "tier": "中"},
    }
    themes = {"TH-1": {"name": "主线一"}}
    default_view = renderer.build_view(
        "20260630", evidence, verdicts, themes, {}, {}, {}, dt.date(2026, 8, 1))
    assert [s["ts_code"] for s in default_view["stocks"]] == ["000001.SZ"]
    assert default_view["excluded_unassigned"] == 1

    full_view = renderer.build_view(
        "20260630", evidence, verdicts, themes, {}, {}, {}, dt.date(2026, 8, 1),
        include_unassigned=True)
    assert {s["ts_code"] for s in full_view["stocks"]} == {"000001.SZ", "000002.SZ"}
    assert full_view["excluded_unassigned"] == 0


def test_qfq_is_rebased_over_the_complete_raw_series() -> None:
    rows = [
        {"trade_date": "20260101", "close": 10.0, "open": 10.0,
         "high": 10.0, "low": 10.0, "pre_close": 10.0, "adj_factor": 1.0},
        {"trade_date": "20260701", "close": 6.0, "open": 6.0,
         "high": 6.0, "low": 6.0, "pre_close": 10.0, "adj_factor": 0.6},
    ]
    adjusted = qfq_adjust_bars(rows)
    assert adjusted[0]["close"] == 16.67
    assert adjusted[1]["close"] == 6.0


def test_security_and_model_contract_guards() -> None:
    assert cn.validate_pdf_url("https://static.cninfo.com.cn/finalpage/a.pdf")
    assert not cn.validate_pdf_url("javascript:alert(1)")
    assert not cn.validate_pdf_url("https://evil.example/finalpage/a.pdf")
    assert "</script>" not in renderer.safe_json_for_script({"x": "</script>"})

    evidence = {"000001.SZ": {
        "ts_code": "000001.SZ", "ann_date": "20260420",
        "growth": {"np": {}, "revenue": {}},
    }}
    valid, errors = verdict.validate_records(
        [{"ts_code": "000001.SZ", "tier": "强"}], evidence, {})
    assert not valid and any("reason" in error for error in errors)


def test_reference_scan_returns_latest_rows_without_database() -> None:
    class Pro:
        def forecast(self, ann_date: str, fields: str) -> pd.DataFrame:
            if ann_date not in {"20260401", "20260402"}:
                return pd.DataFrame()
            return pd.DataFrame([{
                "ts_code": "000001.SZ", "ann_date": ann_date,
                "end_date": "20260331", "type": "预增",
                "p_change_min": 10, "p_change_max": 20,
                "net_profit_min": 1, "net_profit_max": 2,
                "summary": "", "change_reason": "",
            }])

        def express(self, **_: object) -> pd.DataFrame:
            return pd.DataFrame()

    class NoStore:
        def logged_forecast_ann_dates(self, _: str) -> set[str]:
            return set()

        def upsert_forecast_ref(self, _: object) -> bool:
            return False

        def record_forecast_fetch_days(self, *_: object) -> None:
            raise AssertionError("no-DB scan must not claim a persisted fetch")

    rows = rs.scan_reference_values(
        Pro(), NoStore(), "20260331", "20260402", 0, [], workers=2)
    assert len(rows) == 1 and rows[0]["ann_date"] == "20260402"


def test_growth_guards() -> None:
    assert rs.growth_pct(120.0, 100.0) == (20.0, "ok")
    assert rs.growth_pct(100.0, 0.0) == (None, "base_nonpositive")
    assert rs.growth_pct(100.0, -50.0) == (None, "base_nonpositive")
    assert rs.growth_pct(None, 100.0) == (None, "cur_missing")
    assert rs.growth_pct(100.0, None) == (None, "base_missing")


def test_growth_block_and_margins() -> None:
    series = rs.Series({
        "20250331": {"revenue": 100.0, "oper_cost": 70.0, "n_income_attr_p": 10.0},
        "20250630": {"revenue": 220.0, "oper_cost": 155.0, "n_income_attr_p": 24.0},
        "20250930": {"revenue": 330.0, "oper_cost": 235.0, "n_income_attr_p": 33.0},
        "20251231": {"revenue": 450.0, "oper_cost": 320.0, "n_income_attr_p": 45.0},
        "20260331": {"revenue": 150.0, "oper_cost": 100.0, "n_income_attr_p": 20.0},
        "20260630": {"revenue": 330.0, "oper_cost": 225.0, "n_income_attr_p": 45.0},
    })
    g = rs.growth_block(series, "20260630", "n_income_attr_p")
    assert g["cum_yoy_pct"] == 87.5                            # 45 vs 24
    assert series.single("20260630", "n_income_attr_p") == 25.0  # 45 − 20
    assert g["single_q_yoy_pct"] == 78.57                      # 25 vs 14
    assert g["qoq_pct"] == 25.0                                # 25 vs 20

    m = rs.margin_block(series, "20260630")
    # Q2 2026 gross margin: (180 − 125)/180; Q2 2025: (120 − 85)/120.
    assert m["gross_margin_single_pct"] == 30.56
    assert m["gross_margin_single_yoy_pp"] == 1.39
    assert m["gross_margin_single_qoq_pp"] == -2.77            # vs Q1 2026 33.33%


def test_margin_sequential_crosses_year_end_for_q1() -> None:
    series = rs.Series({
        "20250930": {"revenue": 300.0, "oper_cost": 200.0},
        "20251231": {"revenue": 400.0, "oper_cost": 280.0},   # Q4: 100 rev / 80 cost → 20%
        "20260331": {"revenue": 120.0, "oper_cost": 84.0},    # Q1: 30%
    })
    m = rs.margin_block(series, "20260331")
    assert m["gross_margin_single_pct"] == 30.0
    assert m["gross_margin_single_qoq_pp"] == 10.0


def test_pick_rows_prefers_consolidated_and_restated() -> None:
    df = pd.DataFrame([
        {"end_date": "20260331", "report_type": "1", "update_flag": "0",
         "ann_date": "20260429", "revenue": 100.0},
        {"end_date": "20260331", "report_type": "1", "update_flag": "1",
         "ann_date": "20260520", "revenue": 111.0},   # restatement wins
        {"end_date": "20260331", "report_type": "2", "update_flag": "1",
         "ann_date": "20260521", "revenue": 999.0},   # parent-only, must be dropped
        {"end_date": "20250331", "report_type": "1", "update_flag": "0",
         "ann_date": "20250428", "revenue": 90.0},
        {"end_date": "20241231", "report_type": "1", "update_flag": "0",
         "ann_date": "20250428", "revenue": 80.0},   # outside `wanted`
    ])
    picked = rs._pick_rows(df, {"20260331", "20250331"}, ["revenue"])
    assert set(picked) == {"20260331", "20250331"}
    assert picked["20260331"]["revenue"] == 111.0


def test_valuation_ttm_and_nonpositive_guard() -> None:
    series = rs.Series({
        "20250331": {"n_income_attr_p": 1e8, "profit_dedt": 0.9e8},
        "20251231": {"n_income_attr_p": 5e8, "profit_dedt": 4.5e8},
        "20260331": {"n_income_attr_p": 2e8, "profit_dedt": 1.8e8},
    })
    v = rs.valuation_block(series, "20260331", total_mv_wan=1_200_000.0,
                           mv_asof="20260724", pe_ttm_market=20.0)
    assert v["total_mv_yi"] == 120.0
    assert v["np_annualized_yi"] == 8.0            # 2亿 × 4
    assert v["np_ttm_yi"] == 6.0                   # 5 + 2 − 1
    assert v["pe_ttm_np"] == 20.0                  # matches the market PE-TTM
    assert v["pe_annualized_np"] == 15.0

    loss = rs.Series({"20260331": {"n_income_attr_p": -1e8}})
    lv = rs.valuation_block(loss, "20260331", 1_200_000.0, "20260724", None)
    assert lv["pe_annualized_np"] is None
    assert lv["pe_annualized_np_note"] == "np_nonpositive"


def test_fulfillment_against_forecast_range() -> None:
    series = rs.Series({"20260331": {"n_income_attr_p": 1.35e8, "revenue": 10e8}})
    refs = {"forecast": {"type": "预增", "ann_date": "20260415",
                         "np_min": 12000.0, "np_max": 14000.0,  # 万元 → 1.2亿 ~ 1.4亿
                         "change_reason": "土地收储"}}
    f = rs.fulfillment_block(series, "20260331", refs)["forecast"]
    assert f["in_range"] == "within"
    assert f["np_median_yi"] == 1.3
    assert f["range_position"] == 0.75
    assert f["vs_median_pct"] == 3.85

    beat = rs.fulfillment_block(rs.Series({"20260331": {"n_income_attr_p": 1.6e8}}),
                                "20260331", refs)["forecast"]
    assert beat["in_range"] == "above"


def test_compute_reaction_gap_and_fill() -> None:
    def bar(d: str, o: float, c: float) -> dict:
        return {"trade_date": d, "open": o, "high": max(o, c), "low": min(o, c),
                "close": c, "vol": 1000.0}
    bars = [bar("20260420", 10.0, 10.0), bar("20260421", 10.0, 10.0),
            bar("20260422", 11.0, 11.5),                      # +10% gap up
            bar("20260423", 11.5, 11.8)]
    r = rs.compute_reaction(bars, "20260422", gap_min=2.0)
    assert r["gap_open_pct"] == 10.0 and r["gap_dir"] == "up"
    assert r["gap_status"] == "intact" and r["trading_days_since_r"] == 1

    filled = rs.compute_reaction(bars + [bar("20260424", 11.0, 9.5)], "20260422", 2.0)
    assert filled["gap_status"] == "filled"
    intraday = bars + [{
        "trade_date": "20260424", "open": 11.6, "high": 11.8,
        "low": 9.9, "close": 11.2, "vol": 1000.0,
    }]
    assert rs.compute_reaction(intraday, "20260422", 2.0)["gap_status"] == "filled"

    # Filing with no session yet is "pending", not "no gap".
    assert rs.compute_reaction(bars, "20260501", 2.0)["gap_status"] == "pending"


def test_screen_is_a_funnel_not_a_verdict() -> None:
    growth = {
        "revenue": {"single_q_yoy_pct": 40.0, "cum_yoy_pct": 20.0, "cum_yi": 10.0},
        "np": {"single_q_yoy_pct": 60.0, "cum_yoy_pct": 30.0, "qoq_pct": 5.0, "cum_yi": 2.0},
        "dedt": {"single_q_yoy_pct": 55.0},
        "ocf": {},
    }
    quality = {"dedt_ratio_pct": 95.0, "ocf_to_np_pct": 110.0, "roe_annualized_pct": 18.0,
               "ocf_cum_yi": 2.2}
    margins = {"gross_margin_single_yoy_pp": 3.0}
    balance = {"contract_liab": {"yoy_pct": 50.0}, "cip": {"yoy_pct": 5.0},
               "receivable_vs_revenue_gap_pp": -10.0, "inventory_vs_revenue_gap_pp": -5.0}
    reaction = {"gap_dir": "up"}
    thresholds = {"rev_yoy": 15.0, "np_yoy": 30.0, "dedt_ratio": 80.0, "ocf_ratio": 60.0,
                  "roe": 10.0, "margin_pp": 1.0, "orderbook": 20.0, "capex": 30.0, "gap_pp": 20.0}
    s = rs.screen_block(growth, quality, margins, balance, reaction, None, thresholds)
    assert "np_accelerating" in s["hits"] and "gap_up" in s["hits"]
    assert s["penalty"] == 0 and s["rank_score"] > 0
    assert "不是优秀度结论" in s["note"]

    weak = rs.screen_block(
        {"revenue": {"single_q_yoy_pct": -5.0, "cum_yoy_pct": -2.0},
         "np": {"single_q_yoy_pct": -30.0, "cum_yoy_pct": -20.0, "cum_yi": -1.0, "qoq_pct": -10.0},
         "dedt": {}, "ocf": {}},
        {"ocf_cum_yi": None}, {"gross_margin_single_yoy_pp": -4.0},
        balance, {"gap_dir": "down"}, {"forecast": {"in_range": "below"}}, thresholds)
    assert {"loss_making", "revenue_declining", "gap_down", "miss_forecast"} <= set(weak["hits"])
    assert weak["rank_score"] < 0


def test_industry_summary_counts() -> None:
    rows = [
        {"ts_code": "1.SZ", "name": "A", "industry": "半导体", "np_cum_yi": 5.0,
         "np_single_yoy_pct": 50.0, "rev_cum_yoy_pct": 30.0, "np_cum_yoy_pct": 40.0,
         "gross_margin_single_yoy_pp": 2.0, "hits": ["np_accelerating", "gap_up", "ocf_backed"]},
        {"ts_code": "2.SZ", "name": "B", "industry": "半导体", "np_cum_yi": 1.0,
         "np_single_yoy_pct": -10.0, "rev_cum_yoy_pct": -5.0, "np_cum_yoy_pct": -8.0,
         "gross_margin_single_yoy_pp": -1.0, "hits": ["loss_making", "gap_down"]},
    ]
    summary = rs.build_industry_summary(rows)
    assert len(summary) == 1
    row = summary[0]
    assert row["n"] == 2 and row["growth_n"] == 1 and row["decline_n"] == 1
    assert row["loss_n"] == 1 and row["gap_up_n"] == 1 and row["gap_down_n"] == 1
    assert row["np_single_yoy_median"] == 20.0
    assert row["members_sample"][0]["ts_code"] == "1.SZ"  # sorted by profit size


def run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
