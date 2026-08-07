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


def test_sw_frame_uses_sw_daily_route() -> None:
    expected = pd.DataFrame({"trade_date": [ASOF], "close": [100.0]})

    class FakeMarketPanel:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def fetch_sw_daily(self, _pro, ts_code: str, start: str, end: str) -> pd.DataFrame:
            self.calls.append((ts_code, start, end))
            return expected

        def fetch_index_daily(self, *_args, **_kwargs) -> pd.DataFrame:
            raise AssertionError("SW codes must not use index_daily")

    fake = FakeMarketPanel()
    original = m._mp
    m._mp = lambda: fake
    try:
        actual = m.fetch_sw_frame(object(), "801010.SI", ASOF)
    finally:
        m._mp = original
    assert actual is expected
    assert fake.calls[0][0] == "801010.SI"
    assert fake.calls[0][2] == ASOF


def test_sw_block_is_unavailable_when_endpoint_returns_no_history() -> None:
    frames = {
        "801010.SI": pd.DataFrame(),
        "801030.SI": pd.DataFrame(),
    }
    out = m.build_sw_industries_block(
        frames,
        {"801010.SI": "农林牧渔", "801030.SI": "基础化工"},
        ASOF,
        None,
        "index_classify",
    )
    assert out["available"] is False
    assert out["reason"] == "sw_daily returned no industry history"


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


def test_breadth_qfq_undoes_ex_dividend_drop() -> None:
    """除权跳空不该被读成「跌破 60 日线」。

    这是本卡最容易悄悄失真的一条：stock_daily 存未复权价，6-8 月除权密集，
    未复权口径会把「站上 60 日线」系统性压低，方向恰好压在手册 50% 确认线上。
    """
    dates = make_dates(61)
    # 一只票原本一路走平在 100，最后一天除权 5%（因子从 1.0 变 0.95）：
    # 未复权看是 95 < MA60，前复权看是持平在均线上方。
    rows = [("000001.SZ", d, 100.0) for d in dates[:-1]] + [("000001.SZ", dates[-1], 95.0)]
    frame = pd.DataFrame(rows, columns=["ts_code", "trade_date", "close"])
    factors = [1.0 / 0.95] * 60 + [1.0]  # qfq 基准取窗口末日
    frame["adj_factor"] = factors

    raw = m.compute_breadth(frame.drop(columns=["adj_factor"]), ASOF)
    adj = m.compute_breadth(frame, ASOF)
    assert raw["price_adjustment"] == "none"
    assert raw["pct_above_ma60"] == 0.0, raw["pct_above_ma60"]
    assert adj["price_adjustment"] == "qfq"
    assert adj["adj_coverage"] == 1.0
    assert adj["pct_above_ma60"] == 0.0  # 复权后持平，不高于均线
    assert adj["pct_above_ma20"] == 0.0
    # 关键差别在读数本身：未复权把最后一天读成 95，复权读回 100。
    assert raw["pct_positive_ret_60d"] == 0.0
    assert adj["pct_positive_ret_60d"] == 0.0
    assert m.apply_breadth_qfq(frame)[1]["adj_missing"] == 0


def test_breadth_missing_adj_factor_falls_back_per_stock() -> None:
    dates = make_dates(61)
    rows = [("000001.SZ", d, 100.0 + i) for i, d in enumerate(dates)]
    rows += [("000002.SZ", d, 100.0 + i) for i, d in enumerate(dates)]
    frame = pd.DataFrame(rows, columns=["ts_code", "trade_date", "close"])
    frame["adj_factor"] = [1.0] * 61 + [None] * 61
    out = m.compute_breadth(frame, ASOF)
    # 缺因子的票退回未复权价参与，不被踢出分母
    assert out["above_ma60_total"] == 2
    assert out["adj_missing"] == 61
    assert out["price_adjustment"] == "qfq"


def test_breadth_reports_one_day_delta() -> None:
    dates = make_dates(62)
    # 一只票在倒数第二天还在均线下，最后一天拉回均线上 → 宽度环比 +100pct
    closes = [100.0] * 60 + [90.0, 130.0]
    frame = pd.DataFrame(
        [("000001.SZ", d, c) for d, c in zip(dates, closes)],
        columns=["ts_code", "trade_date", "close"],
    )
    out = m.compute_breadth(frame, dates[-1])
    assert out["prev_trade_date"] == dates[-2]
    assert out["pct_above_ma20"] == 100.0
    assert out["pct_above_ma20_delta_1d"] == 100.0


def test_stale_block_is_flagged_when_cache_is_frozen() -> None:
    """sw_daily 掉权限时 compute 会继续吃旧缓存，必须由 data_through 抓住。"""
    calendar = ["20260728", "20260729", "20260730", "20260731", "20260803"]
    fresh = m.stamp_freshness({}, "20260803", "20260803", calendar)
    assert fresh["stale"] is False and fresh["stale_trading_days"] == 0

    frozen = m.stamp_freshness({}, "20260731", "20260803", calendar)
    assert frozen["stale"] is True
    assert frozen["stale_trading_days"] == 1
    assert "20260731" in frozen["stale_reason"]

    # 融资是 T-1 口径，落后 1 个交易日属正常
    lagged = m.stamp_freshness({}, "20260731", "20260803", calendar, tolerance=1)
    assert lagged["stale"] is False

    # 说不出数据日的读数不该被当成当日读数
    unknown = m.stamp_freshness({}, None, "20260803", calendar)
    assert unknown["stale"] is True and unknown["stale_trading_days"] is None


def test_sw_counts_expose_valid_denominators() -> None:
    """above_ma60=None（历史不足）不能和 False（在均线下）混成同一个数。"""
    dates_long = make_dates(61)
    frames = {
        "801080.SI": pd.DataFrame({"trade_date": dates_long, "close": [100.0] * 60 + [130.0],
                                   "high": [100.0] * 60 + [130.0]}),
        "801150.SI": pd.DataFrame({"trade_date": dates_long, "close": [100.0] * 60 + [70.0],
                                   "high": [100.0] * 61}),
        "801750.SI": pd.DataFrame({"trade_date": make_dates(10), "close": [100.0] * 10,
                                   "high": [100.0] * 10}),  # 历史不足 → above_ma60 为 None
    }
    names = {"801080.SI": "电子", "801150.SI": "医药生物", "801750.SI": "计算机"}
    out = m.summarize_sw_industries(frames, names, dates_long[-1])
    assert out["count_above_ma60"] == 1
    assert out["count_above_ma60_total"] == 2, out["count_above_ma60_total"]
    assert out["count_positive_60d"] == 1
    assert out["count_positive_60d_total"] == 2
    assert out["latest_trade_date"] == dates_long[-1]
    assert all(row["trade_date"] for row in out["industries"])


def test_confirmation_checks_mirror_the_framework_thresholds() -> None:
    breadth = {"available": True, "pct_above_ma60": 52.0, "pct_above_ma60_delta_1d": 1.2,
               "trade_date": "20260731"}
    sw = {"available": True, "count_positive_60d": 12, "count_positive_60d_total": 31,
          "count_positive_60d_prev": 9, "data_through": "20260731"}
    margin = {"available": True, "is_new_low_20d": False, "days_since_20d_low": 3,
              "latest": 26000.0, "chg_5d_pct": 0.4, "trade_date": "20260730"}
    out = m.build_confirmation(breadth, sw, margin)
    by_key = {c["key"]: c for c in out["checks"]}
    assert by_key["margin_stop_new_low"]["hit"] is True
    assert by_key["breadth_recovery"]["hit"] is True
    assert by_key["industry_diffusion"]["hit"] is True
    # 盈利上修脚本没有数据源，永远是 null + external，不能被算成命中
    assert by_key["earnings_revision"]["hit"] is None
    assert by_key["earnings_revision"]["source"] == "external"
    assert out["hits"] == 3 and out["scriptable"] == 3 and out["undetermined"] == 1
    # 融资读数带自己的数据日（T-1），不能说成当日
    assert by_key["margin_stop_new_low"]["as_of"] == "20260730"

    # 阈值边界：刚好等于阈值算命中，差一点就不算
    breadth_low = {**breadth, "pct_above_ma60": 49.99}
    sw_low = {**sw, "count_positive_60d": 9}
    margin_low = {**margin, "is_new_low_20d": True, "days_since_20d_low": 0}
    out2 = m.build_confirmation(breadth_low, sw_low, margin_low)
    assert [c["hit"] for c in out2["checks"] if c["source"] == "script"] == [False, False, False]
    assert out2["hits"] == 0


def test_confirmation_degrades_when_subblock_unavailable() -> None:
    out = m.build_confirmation(
        {"available": False, "reason": "boom"},
        {"available": False, "reason": "sw_daily returned no industry history"},
        {"available": False, "reason": "boom"},
    )
    assert [c["hit"] for c in out["checks"]] == [None, None, None, None]
    assert out["hits"] == 0 and out["undetermined"] == 4


def test_drawdown_tiers_follow_the_manual() -> None:
    assert m.drawdown_tier(-9.99) == "调整"
    assert m.drawdown_tier(-10.0) == "深度调整"
    assert m.drawdown_tier(-19.99) == "深度调整"
    assert m.drawdown_tier(-20.0) == "接近技术性熊市"
    assert m.drawdown_tier(-31.14) == "接近技术性熊市"
    assert m.drawdown_tier(None) is None


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
