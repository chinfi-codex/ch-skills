#!/usr/bin/env python3
"""Offline regression tests for the market-state card (market_state).

纯离线：不触 Tushare / Baostock / PG，直接用合成 DataFrame 注入纯计算函数。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

import market_state_card as m  # noqa: E402

ASOF = "20260731"


def make_dates(n: int) -> list[str]:
    end = date(2026, 7, 31)  # 与 ASOF 对齐，窗口最后一天即 asof
    return [(end - timedelta(days=n - 1 - i)).strftime("%Y%m%d") for i in range(n)]


def price_frame(closes: list[float], highs: list[float] | None = None) -> pd.DataFrame:
    dates = make_dates(len(closes))
    return pd.DataFrame({
        "trade_date": dates,
        "close": closes,
        "high": highs if highs is not None else list(closes),
    })


def daily_frame(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["ts_code", "trade_date", "close"])


def stock_rows(ts_code: str, closes: list[float], dates: list[str]) -> list[tuple[str, str, float]]:
    return [(ts_code, d, c) for d, c in zip(dates, closes)]


def test_index_drawdown_from_250d_high() -> None:
    closes = [100.0] * 249 + [110.0]
    highs = list(closes)
    highs[100] = 120.0  # 250 日高点落在第 101 行
    out = m.compute_position_metrics(price_frame(closes, highs), ASOF)
    assert out["insufficient_history"] is False
    assert out["high_250d"] == 120.0
    assert out["high_250d_date"] == make_dates(250)[100]
    assert out["drawdown_from_high_250d_pct"] == round((110.0 / 120.0 - 1.0) * 100.0, 2)
    assert out["ret_20d"] == 10.0
    ma60 = (59 * 100.0 + 110.0) / 60.0
    assert out["close_vs_ma60_pct"] == round((110.0 / ma60 - 1.0) * 100.0, 2)
    assert out["above_ma60"] is True


def test_index_insufficient_history_degrades_to_null() -> None:
    closes = [100.0 + i for i in range(30)]  # 30 个交易日，单调上行
    out = m.compute_position_metrics(price_frame(closes), ASOF)
    assert out["insufficient_history"] is True
    assert out["high_250d"] is None
    assert out["high_250d_date"] is None
    assert out["drawdown_from_high_250d_pct"] is None
    assert out["close_vs_ma60_pct"] is None
    assert out["above_ma60"] is None
    # 21 行即可算的 ret_20d 仍正常输出
    assert out["ret_20d"] == round((129.0 / 109.0 - 1.0) * 100.0, 2)


def test_breadth_percentages_and_denominators() -> None:
    dates = make_dates(61)
    rows: list[tuple[str, str, float]] = []
    # A：60 天横盘 10，最后一天跳到 12 → 站上 20/60 日线、60 日收益为正
    rows += stock_rows("A", [10.0] * 60 + [12.0], dates)
    # B：60 天横盘 10，最后一天跌到 8 → 全部不满足
    rows += stock_rows("B", [10.0] * 60 + [8.0], dates)
    # C：全程横盘 10 → close 等于均线不算站上，ret_60d 为 0 不算正
    rows += stock_rows("C", [10.0] * 61, dates)
    # D：只有 30 天历史，最后一天跳到 12 → 只进 ma20 口径，ma60/ret60 分母剔除
    rows += stock_rows("D", [10.0] * 29 + [12.0], dates[31:])
    out = m.compute_breadth(daily_frame(rows), ASOF)
    assert out["trade_date"] == dates[-1]
    assert out["total"] == 4
    assert out["insufficient_history"] is False
    assert (out["pct_above_ma20"], out["above_ma20_count"], out["above_ma20_total"]) == (50.0, 2, 4)
    assert (out["pct_above_ma60"], out["above_ma60_count"], out["above_ma60_total"]) == (round(100 / 3, 2), 1, 3)
    assert (out["pct_positive_ret_60d"], out["positive_ret_60d_count"], out["positive_ret_60d_total"]) == (round(100 / 3, 2), 1, 3)


def test_breadth_insufficient_window_nulls_60d_metrics() -> None:
    dates = make_dates(30)
    rows = stock_rows("A", [10.0] * 29 + [12.0], dates) + stock_rows("B", [10.0] * 30, dates)
    out = m.compute_breadth(daily_frame(rows), ASOF)
    assert out["insufficient_history"] is True
    assert out["pct_above_ma20"] == 50.0
    assert out["pct_above_ma60"] is None
    assert out["above_ma60_count"] is None
    assert out["pct_positive_ret_60d"] is None


def margin_frame(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"trade_date": make_dates(len(values)), "rzye_yi": values})


def test_margin_flags_off_low() -> None:
    values = [100.0] * 16 + [90.0, 91.0, 92.0, 93.0, 94.0]
    out = m.compute_margin_trend(margin_frame(values), ASOF)
    assert out["latest"] == 94.0
    assert out["chg_20d_pct"] == -6.0
    assert out["chg_5d_pct"] == -6.0  # values[-6] 是低点前一日的 100
    assert out["is_new_low_20d"] is False
    assert out["days_since_20d_low"] == 4
    assert out["vs_250d_ago_pct"] is None
    assert out["insufficient_history"] is True


def test_margin_flags_at_new_low() -> None:
    values = [100.0] * 20 + [80.0]
    out = m.compute_margin_trend(margin_frame(values), ASOF)
    assert out["is_new_low_20d"] is True
    assert out["days_since_20d_low"] == 0
    assert out["chg_20d_pct"] == -20.0


def test_margin_vs_250d_ago_with_full_window() -> None:
    values = [200.0] + [100.0] * 250
    out = m.compute_margin_trend(margin_frame(values), ASOF)
    assert out["insufficient_history"] is False
    assert out["vs_250d_ago_pct"] == -50.0


def test_sw_industries_positive_count_and_benchmark_high() -> None:
    up = [10.0] * 60 + [12.0]      # ret_60d = +20%
    down = [10.0] * 60 + [9.0]     # ret_60d = -10%
    short = [10.0] * 30            # 历史不足 → ret_60d None
    frames = {"X1": price_frame(up), "X2": price_frame(down), "X3": price_frame(short)}
    names = {"X1": "行业一", "X2": "行业二", "X3": "行业三"}
    bench_closes = [100.0] * 250
    bench_highs = list(bench_closes)
    bench_highs[200] = 130.0
    out = m.summarize_sw_industries(frames, names, ASOF, price_frame(bench_closes, bench_highs))
    assert out["count_positive_60d"] == 1
    assert out["count_above_ma60"] == 1
    assert out["benchmark_high_250d_date"] == make_dates(250)[200]
    by_code = {row["ts_code"]: row for row in out["industries"]}
    assert by_code["X1"]["ret_60d"] == 20.0
    assert by_code["X2"]["ret_60d"] == -10.0
    assert by_code["X3"]["ret_60d"] is None
    assert by_code["X3"]["insufficient_history"] is True


def test_liquidity_reuses_evidence_numbers() -> None:
    evidence = {
        "market_temperature": {"total_amount_100m_yuan": 20000.0},
        "market_trend": {"sentiment": {"trade_date": ASOF, "rolling": {"amount_ma20": 1.9e9}}},
    }
    out = m.liquidity_from_evidence(evidence)
    assert out["amount_today_yi"] == 20000.0
    assert out["amount_ma20_yi"] == 19000.0
    assert out["ratio"] == round(20000.0 / 19000.0, 3)
    assert m.liquidity_from_evidence(None) is None
    assert m.liquidity_from_evidence({}) is None


def run() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
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
