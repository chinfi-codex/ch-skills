#!/usr/bin/env python3
"""Pure-function tests for the a-stock-daily-market-sense pipeline.

Runs with plain python (no pytest dependency):

    python3 test_market_panel.py

The file name matches the repo-wide sync/publish exclude pattern test_*.py,
so it never ships to agent installs or ClawHub packages. Tests cover only
deterministic in-memory functions — no Tushare/Baostock/PostgreSQL access.
"""

from __future__ import annotations

import datetime
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

SKILL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
sys.path.insert(0, str(SKILL_ROOT.parents[1] / "shared" / "data"))

import db_adapter
import market_panel as mp
import render_report_html as rrh
import run_daily_panel as rdp


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def test_normalize_date_formats():
    assert mp.normalize_date("20260609") == "20260609"
    assert mp.normalize_date("2026-06-09") == "20260609"
    assert mp.normalize_date("2026/06/09") == "20260609"
    try:
        mp.normalize_date("June 9, 2026")
        raise AssertionError("expected ValueError for unsupported format")
    except ValueError:
        pass


def test_normalize_date_series_semantics():
    series = pd.Series([
        datetime.date(2026, 6, 9),
        "20260609",
        "2026-06-09",
        "2026/6/9",
        None,
        float("nan"),
        "",
        "  20260101 ",
        "garbage",
        pd.Timestamp("2026-05-01"),
    ])
    out = list(db_adapter._normalize_date_series(series))
    assert out[0] == "20260609"
    assert out[1] == "20260609"
    assert out[2] == "20260609"
    assert out[3] == "20260609"
    assert out[4] is None
    assert pd.isna(out[5])
    assert out[6] == ""
    assert out[7] == "20260101"
    assert out[8] == "garbage"
    assert out[9] == "20260501"


def test_missing_edge_ranges():
    df = pd.DataFrame({"trade_date": ["20260603", "20260604", "20260605"]})
    assert mp.missing_edge_ranges(df, "trade_date", "20260603", "20260605") == []
    ranges = mp.missing_edge_ranges(df, "trade_date", "20260601", "20260609")
    assert ranges == [("20260601", "20260602"), ("20260606", "20260609")]
    assert mp.missing_edge_ranges(pd.DataFrame(), "trade_date", "20260601", "20260609") == [
        ("20260601", "20260609")
    ]


def test_market_history_backfill_dates():
    trade_dates = ["20260603", "20260604", "20260605", "20260608", "20260609"]
    existing = {"20260603", "20260604"}
    dates = mp.market_history_backfill_dates("20260609", trade_dates, existing)
    assert dates == ["20260605", "20260608", "20260609"]
    # 已全部存在时仍要刷新目标日
    dates = mp.market_history_backfill_dates("20260609", trade_dates, set(trade_dates))
    assert dates == ["20260609"]


def test_market_data_for_report_rejects_stale_and_filters_future_rows():
    source = Path("market_data.json")
    stale = {
        "metadata": {"window_end": "20260626"},
        "records": [{"trade_date": "20260626", "成交额": 1.0}],
        "quality": {"records_available": 1, "has_120_records": False},
    }
    try:
        rrh.market_data_for_report(stale, "20260717", source)
        raise AssertionError("expected stale market chart data to fail closed")
    except RuntimeError as exc:
        assert "required trade_date=20260717" in str(exc)
        assert "window_end=20260626" in str(exc)

    current = {
        "metadata": {"window_end": "20260718"},
        "records": [
            {"trade_date": "20260716", "成交额": 1.0},
            {"trade_date": "20260717", "成交额": 2.0},
            {"trade_date": "20260718", "成交额": 3.0},
        ],
        "quality": {"records_available": 3, "has_120_records": False},
    }
    scoped = rrh.market_data_for_report(current, "20260717", source)
    assert scoped["metadata"]["window_end"] == "20260717"
    assert scoped["quality"]["records_available"] == 2
    assert [row["trade_date"] for row in scoped["records"]] == ["20260716", "20260717"]


def test_refresh_and_verify_market_chart_data_checks_resolved_date():
    original = rdp.market_panel.write_market_history_json
    try:
        with tempfile.TemporaryDirectory() as tmp:
            chart_path = Path(tmp) / "market_data.json"
            chart_path.write_text(json.dumps({
                "metadata": {"window_end": "20260717"},
                "records": [{"trade_date": "20260717"}],
            }), encoding="utf-8")
            seen = {}

            def _stub(end_date=None, **_kwargs):
                seen["end_date"] = end_date
                return chart_path

            rdp.market_panel.write_market_history_json = _stub
            assert rdp.refresh_and_verify_market_chart_data("20260717") == chart_path
            # 窗口必须锚在报告日：只截最新 N 天的话，回溯渲染时目标日不在记录里，
            # 下面那条 fail-closed 断言就会因为"窗口没覆盖到"而误报
            assert seen["end_date"] == "20260717"
            try:
                rdp.refresh_and_verify_market_chart_data("20260718")
                raise AssertionError("expected missing resolved date to fail closed")
            except RuntimeError as exc:
                assert "required trade_date=20260718" in str(exc)
    finally:
        rdp.market_panel.write_market_history_json = original


def test_market_history_json_preserves_margin_data_date_as_a_date():
    original_backend = mp.BACKEND
    original_reader = mp.read_market_history
    try:
        mp.BACKEND = mp.Backend.POSTGRESQL
        mp.read_market_history = lambda: pd.DataFrame([{
            "date": "20260807",
            "rise": 2855,
            "limit_up": 76,
            "fall": 2536,
            "limit_down": 4,
            "flat": 144,
            "activity": 51.84,
            "sentiment": 51.84,
            "amount": 2683401729.382,
            "margin_net_buy": 11057565565,
            "margin_data_date": "20260806",
            "turnover_rate": 2.6757,
        }])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "market_data.json"
            mp.write_market_history_json(json_path=out)
            payload = json.loads(out.read_text(encoding="utf-8"))
        assert "融资数据日" in payload["columns"]
        assert "margin_data_date" not in payload["columns"]
        assert payload["records"][0]["融资数据日"] == "20260806"
        assert "融资数据日" not in payload["series"]
    finally:
        mp.BACKEND = original_backend
        mp.read_market_history = original_reader


# --------------------------------------------------------------------------- #
# Sentiment
# --------------------------------------------------------------------------- #
def test_calc_market_sentiment_bounds_and_weights():
    assert mp.calc_market_sentiment(0, 0, 0, 0, 0) == 50.0
    all_up = mp.calc_market_sentiment(5000, 0, 0, 100, 0)
    assert all_up > 99.0
    all_down = mp.calc_market_sentiment(0, 5000, 0, 0, 100)
    assert all_down < 1.0
    # 跌停权重高于涨停：同样的涨跌结构里，跌停更多的盘面情绪更低
    more_limit_down = mp.calc_market_sentiment(2000, 2000, 100, 50, 100)
    more_limit_up = mp.calc_market_sentiment(2000, 2000, 100, 100, 50)
    assert more_limit_down < more_limit_up


def test_compute_market_activity_from_daily():
    daily = pd.DataFrame({
        "trade_date": ["20260609"] * 6 + ["20260608"],
        "pct_chg": [10.0, 5.0, 0.0, -3.0, -9.9, 2.0, 8.0],
        "amount": [100.0, 200.0, 50.0, 80.0, 60.0, 10.0, 999.0],
    })
    row, columns, detail = mp.compute_market_activity_from_daily(daily, "20260609")
    assert detail["available"] is True
    assert row["上涨"] == 3 and row["下跌"] == 2 and row["平盘"] == 1
    assert row["涨停"] == 1 and row["跌停"] == 1
    assert row["成交额"] == 500.0
    # 目标日缺失时显式回报不可用
    _, _, missing_detail = mp.compute_market_activity_from_daily(daily, "20260610")
    assert missing_detail["available"] is False


# --------------------------------------------------------------------------- #
# Board-rule limit detection
# --------------------------------------------------------------------------- #
def test_classify_limit_pct():
    assert mp.classify_limit_pct("600519.SH", "贵州茅台", "主板") == 0.10
    assert mp.classify_limit_pct("000001.SZ", "平安银行", "主板") == 0.10
    assert mp.classify_limit_pct("600000.SH", "ST某某", "主板") == 0.05
    assert mp.classify_limit_pct("600000.SH", "*ST某某", "主板") == 0.05
    assert mp.classify_limit_pct("300750.SZ", "宁德时代", "创业板") == 0.20
    # 创业板 ST 仍是 20%
    assert mp.classify_limit_pct("300001.SZ", "ST创业", "创业板") == 0.20
    assert mp.classify_limit_pct("688981.SH", "中芯国际", "科创板") == 0.20
    assert mp.classify_limit_pct("832000.BJ", "某北交", "北交所") == 0.30
    # market 缺失时按代码前缀兜底
    assert mp.classify_limit_pct("688001.SH", "某科创", "") == 0.20
    assert mp.classify_limit_pct("430047.BJ", "某北交", "") == 0.30


def test_exchange_round_to_fen_half_up():
    # 10.05 × 1.1 = 11.055 → 交易所四舍五入到 11.06（二进制浮点下 11.055
    # 实为 11.0549…，朴素 round 会得到 11.05）
    series = pd.Series([10.05 * 1.1, 9.87 * 1.1, 10.0 * 1.1])
    out = mp.exchange_round_to_fen(series).tolist()
    assert out == [11.06, 10.86, 11.00]


def test_compute_limit_flags_board_rules():
    daily = pd.DataFrame({
        # 主板涨停 / 创业板 +15% 非涨停 / 创业板 20% 涨停 / 主板 ST +5% 涨停
        # / 北交所 +15% 非涨停 / 北交所 30% 涨停 / 主板 ST -5% 跌停 / 主板四舍五入边界
        "ts_code": ["600000.SH", "300100.SZ", "300200.SZ", "600100.SH",
                     "830000.BJ", "830001.BJ", "600200.SH", "600300.SH"],
        "trade_date": ["20260609"] * 8,
        "pre_close": [10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.05],
        "close": [11.00, 11.50, 12.00, 10.50, 11.50, 13.00, 9.50, 11.06],
    })
    basic = pd.DataFrame({
        "ts_code": ["600000.SH", "300100.SZ", "300200.SZ", "600100.SH",
                     "830000.BJ", "830001.BJ", "600200.SH", "600300.SH"],
        "name": ["甲", "乙", "丙", "ST丁", "戊", "己", "ST庚", "辛"],
        "market": ["主板", "创业板", "创业板", "主板", "北交所", "北交所", "主板", "主板"],
    })
    flags = mp.compute_limit_flags(daily, basic).set_index("ts_code")
    assert flags.loc["600000.SH", "is_limit_up"] == True  # noqa: E712
    assert flags.loc["300100.SZ", "is_limit_up"] == False  # 创业板 +15% 不是涨停  # noqa: E712
    assert flags.loc["300200.SZ", "is_limit_up"] == True   # 创业板 20cm 涨停  # noqa: E712
    assert flags.loc["600100.SH", "is_limit_up"] == True   # ST +5% 涨停（旧近似漏掉）  # noqa: E712
    assert flags.loc["830000.BJ", "is_limit_up"] == False  # 北交所 +15% 不是涨停  # noqa: E712
    assert flags.loc["830001.BJ", "is_limit_up"] == True   # 北交所 30cm 涨停  # noqa: E712
    assert flags.loc["600200.SH", "is_limit_down"] == True  # ST -5% 跌停  # noqa: E712
    assert flags.loc["600300.SH", "is_limit_up"] == True   # 四舍五入边界 11.06  # noqa: E712


def test_compute_limit_flags_st_dual_band():
    # 退市整理期 ST 执行 10% 档：*ST阳光 20260609 实测 5.82 → 6.40 封板
    daily = pd.DataFrame({
        "ts_code": ["000608.SZ", "605081.SH", "600400.SH"],
        "trade_date": ["20260609"] * 3,
        "pre_close": [5.82, 0.40, 10.00],
        "close": [6.40, 0.44, 10.50],
    })
    basic = pd.DataFrame({
        "ts_code": ["000608.SZ", "605081.SH", "600400.SH"],
        "name": ["*ST阳光", "*ST太和", "ST正常"],
        "market": ["主板", "主板", "主板"],
    })
    flags = mp.compute_limit_flags(daily, basic).set_index("ts_code")
    assert flags.loc["000608.SZ", "is_limit_up"] == True  # 10% 备档命中  # noqa: E712
    assert flags.loc["605081.SH", "is_limit_up"] == True  # noqa: E712
    assert flags.loc["600400.SH", "is_limit_up"] == True  # 5% 主档命中  # noqa: E712


def test_count_limit_hits_prefers_flags():
    frame = pd.DataFrame({
        "pct_chg": [10.0, 15.0, 5.0],
        "is_limit_up": [True, False, True],
        "is_limit_down": [False, False, False],
    })
    up, down, detection = mp.count_limit_hits(frame)
    assert (up, down, detection) == (2, 0, "board_rule_price_match")
    up, down, detection = mp.count_limit_hits(frame[["pct_chg"]])
    assert (up, down, detection) == (2, 0, "pct_chg_approx")


# --------------------------------------------------------------------------- #
# Classifiers
# --------------------------------------------------------------------------- #
def test_classifiers():
    assert mp.classify_ma_alignment(11, 10, 9, 8) == "bullish_alignment"
    assert mp.classify_ma_alignment(8, 9, 10, 11) == "bearish_alignment"
    assert mp.classify_ma_alignment(None, 9, 10, 11) is None
    assert mp.classify_volume_temperature(2.5) == "clear_expansion"
    assert mp.classify_volume_temperature(None) is None
    assert mp.classify_breadth_temperature(None) is None
    up3 = mp.count_consecutive_moves(pd.Series([1, 2, 3, 4]), "up")
    assert up3 == 3
    assert mp.count_consecutive_moves(pd.Series([4, 3, 2, 1]), "up") == 0


# --------------------------------------------------------------------------- #
# Index/style summary builders
# --------------------------------------------------------------------------- #
def _synthetic_index_daily(days: int = 100) -> pd.DataFrame:
    start = datetime.date(2026, 1, 1)
    rows = []
    for idx in range(days):
        close = 100.0 + idx
        rows.append({
            "trade_date": (start + datetime.timedelta(days=idx)).strftime("%Y%m%d"),
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "pre_close": close - 1.0,
            "pct_chg": round(100.0 / (close - 1.0), 4),
            "vol": 1000.0 + idx,
            "amount": 100000000.0 + idx * 1000000.0,
        })
    rows.append({
        "trade_date": "20260420",
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 999.0,
        "pre_close": 998.0,
        "pct_chg": 1.0,
        "vol": 1.0,
        "amount": 1.0,
    })
    rows.append({
        "trade_date": "20260315",
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": None,
        "pre_close": 1.0,
        "pct_chg": None,
        "vol": 1.0,
        "amount": 1.0,
    })
    return pd.DataFrame(rows)


def test_build_market_style_index_summary_series_and_metrics():
    df = _synthetic_index_daily(100)
    summary = mp.build_market_style_index_summary(
        df,
        {"name": "测试风格", "bs_code": "sh.test", "style_role": "测试"},
        "20260410",
        20,
    )
    assert summary["available"] is True
    assert summary["latest"]["close"] == 199.0
    assert summary["returns"]["ret_5d"] == round((199.0 / 194.0 - 1.0) * 100.0, 2)
    assert summary["returns"]["ret_20d"] == round((199.0 / 179.0 - 1.0) * 100.0, 2)
    assert summary["moving_averages"]["ma20"] == round(sum(range(180, 200)) / 20, 2)
    assert summary["moving_averages"]["ma60"] == round(sum(range(140, 200)) / 60, 2)
    assert summary["series"]["days"] == 90
    records = summary["series"]["records"]
    assert len(records) == 90
    assert records[0]["trade_date"] == "20260111"
    assert records[-1] == {"trade_date": "20260410", "close": 199.0}
    assert all(row["trade_date"] <= "20260410" for row in records)
    assert all(isinstance(row["close"], float) and round(row["close"], 2) == row["close"] for row in records)
    compact = mp.compact_market_style_index(summary)
    assert "series" not in compact


def test_style_summary_zero_amount_denominator_guard():
    df = _synthetic_index_daily(80)
    df["amount"] = 0.0
    summary = mp.build_market_style_index_summary(
        df,
        {"name": "零额", "bs_code": "sh.zero", "style_role": "测试"},
        "20260410",
        20,
    )
    assert summary["available"] is True
    assert summary["volume_price"]["amount_ratio_20d"] is None
    json.dumps(summary, allow_nan=False)


def test_unavailable_market_style_summary_has_no_series():
    summary = mp.build_market_style_index_summary(
        pd.DataFrame(),
        {"name": "空风格", "bs_code": "sh.empty", "style_role": "测试"},
        "20260410",
        20,
    )
    assert summary["available"] is False
    assert "series" not in summary


def test_index_registry_covers_trend_and_style_sources():
    registry = list(mp.INDEX_REGISTRY)
    trend_keys = {item["key"] for item in registry if item["source"] == "tushare"}
    style_keys = {item["key"] for item in registry if item["source"] == "baostock"}
    assert trend_keys == set(mp.MARKET_TREND_INDEXES)
    assert style_keys == set(mp.MARKET_STYLE_INDEXES)
    assert all(item["roles"] == ["trend"] for item in registry if item["source"] == "tushare")
    assert all(item["roles"] == ["style"] for item in registry if item["source"] == "baostock")
    assert any(item["key"] == "csi300" and item["source"] == "baostock" and item["bs_code"] == "sh.000300" for item in registry)
    if "csi300" in mp.MARKET_TREND_INDEXES:
        assert any(item["key"] == "csi300" and item["source"] == "tushare" for item in registry)


def test_extract_style_series_payload():
    records = [{"trade_date": f"202601{day:02d}", "close": 100 + day} for day in range(1, 11)]
    evidence = {
        "market_trend": {
            "market_style": {
                "available": True,
                "source": "baostock.query_history_k_data_plus + stock_index_daily cache",
                "trade_date": "20260110",
                "indices": {
                    "mega_cap": {
                        "available": True,
                        "name": "超大盘",
                        "style_role": "容量大盘",
                        "series": {"days": 90, "records": records},
                    },
                    "csi300": {
                        "available": True,
                        "name": "沪深300",
                        "style_role": "容量中枢",
                        "series": {"days": 90, "records": records[-5:]},
                    },
                    "missing": {"available": True, "name": "缺序列"},
                },
            }
        }
    }
    payload = rrh.extract_style_series_payload(evidence, display_days=6)
    assert payload is not None
    assert payload["display_days"] == 6
    assert payload["trade_date"] == "20260110"
    assert "window_start" not in payload and "window_end" not in payload and "source" not in payload
    assert [item["key"] for item in payload["indices"]] == ["mega_cap", "csi300"]
    assert len(payload["indices"][0]["records"]) == 6
    assert rrh.extract_style_series_payload({}) is None
    assert rrh.extract_style_series_payload({"market_trend": {"market_style": {"available": False}}}) is None


# --------------------------------------------------------------------------- #
# Candidate pools (synthetic frames)
# --------------------------------------------------------------------------- #
def _synthetic_panel() -> pd.DataFrame:
    # amount 单位为千元：3 亿 = 300000 千元
    return pd.DataFrame({
        "ts_code": ["A.SZ", "B.SZ", "C.SH", "D.SH", "E.SZ"],
        "trade_date": ["20260609"] * 5,
        "name": ["甲", "乙", "丙", "丁", "戊"],
        "pct_chg": [9.9, 7.5, 6.9, -4.0, -5.0],
        "amount": [300000.0, 500000.0, 900000.0, 250000.0, 80000.0],
        "amount_ratio_20d": [3.0, 1.2, 0.9, 2.5, 4.0],
        "ret_3d": [12.0, 8.0, 5.0, -6.0, -9.0],
        "ret_5d": [15.0, 9.0, 6.0, -8.0, -12.0],
        "rel_ret_5d": [10.0, 4.0, 1.0, -9.0, -13.0],
        "drawdown_120_high": [-2.0, -5.0, -8.0, -30.0, -45.0],
    })


def test_build_money_effect_samples():
    result = mp.build_money_effect_samples(
        _synthetic_panel(), pct_chg_threshold=7.0, amount_threshold_100m_yuan=2.0, sample_limit=10
    )
    summary = result["summary"]
    # C 涨幅不够，D/E 是下跌：只剩 A、B
    assert summary["candidate_count"] == 2
    # 按成交额降序：B(5亿) 在 A(3亿) 前
    codes = [item["ts_code"] for item in result["candidates"]]
    assert codes == ["B.SZ", "A.SZ"]
    assert summary["total_amount_100m_yuan"] == 8.0
    assert summary["qualified_before_limit_count"] == 2
    assert summary["market_total_amount_100m_yuan"] == 20.3
    assert summary["candidate_amount_share_of_market_pct"] == 39.41
    # 合成帧没有 is_limit_up 列 → 退回 ±9.8% 近似口径
    assert summary["limit_up_count"] == 1
    assert summary["limit_detection"] == "pct_chg_approx"
    capped = mp.build_money_effect_samples(
        _synthetic_panel(), pct_chg_threshold=7.0, amount_threshold_100m_yuan=2.0, sample_limit=1
    )
    assert capped["summary"]["candidate_count"] == 1
    assert capped["summary"]["qualified_before_limit_count"] == 2
    assert capped["summary"]["candidate_amount_share_of_market_pct"] == 24.63
    empty = mp.build_money_effect_samples(None, 7.0, 2.0, 10)
    assert empty["available"] is False
    assert empty["summary"]["candidate_amount_share_of_market_pct"] is None


def test_build_money_effect_samples_uses_full_market_turnover_denominator():
    full_market = _synthetic_panel()
    candidate_panel = full_market.loc[full_market["ts_code"].isin(["A.SZ", "B.SZ"])].copy()
    result = mp.build_money_effect_samples(
        candidate_panel,
        pct_chg_threshold=7.0,
        amount_threshold_100m_yuan=2.0,
        sample_limit=10,
        market_panel=full_market,
    )
    summary = result["summary"]
    assert summary["total_amount_100m_yuan"] == 8.0
    assert summary["market_total_amount_100m_yuan"] == 20.3
    assert summary["candidate_amount_share_of_market_pct"] == 39.41


def test_build_volume_decline_samples():
    result = mp.build_volume_decline_samples(
        _synthetic_panel(),
        pct_chg_max=-3.0,
        amount_ratio_min=2.0,
        amount_threshold_100m_yuan=0.5,
        sample_limit=10,
    )
    summary = result["summary"]
    assert summary["candidate_count"] == 2
    # decline_intensity = 放量倍数 * 跌幅绝对值：E(4*5=20) > D(2.5*4=10)
    codes = [item["ts_code"] for item in result["candidates"]]
    assert codes == ["E.SZ", "D.SH"]
    assert result["candidates"][0]["decline_intensity"] == 20.0


def _synthetic_discount_panel() -> pd.DataFrame:
    # amount 单位千元（5亿=500000）；total_mv 单位万元（80亿=800000）。
    # 直接提供 discount_after_high（前高之后最低价/前高收盘）列，函数据此筛选。
    rows = [
        # 通过：折扣0.80、缩量0.8、放量2.0、8亿、120亿、+9%
        dict(ts_code="H.SZ", name="华", market="主板", pct_chg=9.0, amount=800000.0,
             total_mv=1200000.0, circ_mv=1000000.0, close=80.0, prev_high_120d=100.0,
             discount_after_high=0.80, amount_ratio_20d=2.0,
             pre_volume_contraction_ratio=0.80, amount_vs_prev5_ratio=2.5, pre_ret_5d=-3.0),
        # 通过：折扣0.75、缩量0.7、放量2.5、6亿、100亿、+8%
        dict(ts_code="G.SZ", name="国", market="主板", pct_chg=8.0, amount=600000.0,
             total_mv=1000000.0, circ_mv=800000.0, close=75.0, prev_high_120d=100.0,
             discount_after_high=0.75, amount_ratio_20d=2.5,
             pre_volume_contraction_ratio=0.70, amount_vs_prev5_ratio=3.0, pre_ret_5d=1.0),
        # 折扣过高（0.95>0.85）→ 排除
        dict(ts_code="I.SZ", name="壹", market="主板", pct_chg=8.0, amount=1000000.0,
             total_mv=1500000.0, circ_mv=1200000.0, close=95.0, prev_high_120d=100.0,
             discount_after_high=0.95, amount_ratio_20d=2.5,
             pre_volume_contraction_ratio=0.70, amount_vs_prev5_ratio=3.0, pre_ret_5d=1.0),
        # 折扣过低（0.50<0.6）→ 排除
        dict(ts_code="J.SZ", name="积", market="主板", pct_chg=8.0, amount=1000000.0,
             total_mv=1500000.0, circ_mv=1200000.0, close=50.0, prev_high_120d=100.0,
             discount_after_high=0.50, amount_ratio_20d=2.5,
             pre_volume_contraction_ratio=0.70, amount_vs_prev5_ratio=3.0, pre_ret_5d=1.0),
        # 前面没缩量（1.2>0.9）→ 排除
        dict(ts_code="K.SZ", name="科", market="主板", pct_chg=8.0, amount=700000.0,
             total_mv=1100000.0, circ_mv=900000.0, close=70.0, prev_high_120d=100.0,
             discount_after_high=0.70, amount_ratio_20d=2.5,
             pre_volume_contraction_ratio=1.20, amount_vs_prev5_ratio=2.5, pre_ret_5d=1.0),
        # 当日没放量（1.2<1.8）→ 排除
        dict(ts_code="L.SZ", name="量", market="主板", pct_chg=8.0, amount=700000.0,
             total_mv=1100000.0, circ_mv=900000.0, close=70.0, prev_high_120d=100.0,
             discount_after_high=0.70, amount_ratio_20d=1.2,
             pre_volume_contraction_ratio=0.70, amount_vs_prev5_ratio=1.3, pre_ret_5d=1.0),
        # 市值不足（70亿<80亿）→ 排除
        dict(ts_code="M.SZ", name="迷", market="主板", pct_chg=8.0, amount=700000.0,
             total_mv=700000.0, circ_mv=600000.0, close=70.0, prev_high_120d=100.0,
             discount_after_high=0.70, amount_ratio_20d=2.5,
             pre_volume_contraction_ratio=0.70, amount_vs_prev5_ratio=2.5, pre_ret_5d=1.0),
        # 成交额不足（4亿<5亿）→ 排除
        dict(ts_code="O.SZ", name="欧", market="主板", pct_chg=8.0, amount=400000.0,
             total_mv=1100000.0, circ_mv=900000.0, close=70.0, prev_high_120d=100.0,
             discount_after_high=0.70, amount_ratio_20d=2.5,
             pre_volume_contraction_ratio=0.70, amount_vs_prev5_ratio=2.5, pre_ret_5d=1.0),
        # 涨幅不足（6%<7%）→ 排除
        dict(ts_code="P.SZ", name="平", market="主板", pct_chg=6.0, amount=700000.0,
             total_mv=1100000.0, circ_mv=900000.0, close=70.0, prev_high_120d=100.0,
             discount_after_high=0.70, amount_ratio_20d=2.5,
             pre_volume_contraction_ratio=0.70, amount_vs_prev5_ratio=2.5, pre_ret_5d=1.0),
        # ST → 排除
        dict(ts_code="N.SZ", name="*ST恩", market="主板", pct_chg=8.0, amount=700000.0,
             total_mv=1100000.0, circ_mv=900000.0, close=70.0, prev_high_120d=100.0,
             discount_after_high=0.70, amount_ratio_20d=2.5,
             pre_volume_contraction_ratio=0.70, amount_vs_prev5_ratio=2.5, pre_ret_5d=1.0),
    ]
    frame = pd.DataFrame(rows)
    frame["low_to_target_days"] = 2
    frame["monthly_above_ma10"] = True
    frame["monthly_ma10"] = 1.0
    frame["trade_date"] = "20260609"
    return frame


def test_build_discount_relaunch_samples():
    result = mp.build_discount_relaunch_samples(
        _synthetic_discount_panel(),
        market_cap_threshold_100m_yuan=80.0,
        amount_threshold_100m_yuan=5.0,
        pct_chg_threshold=7.0,
        discount_min=0.6,
        discount_max=0.85,
        pre_contraction_max=0.9,
        volume_expansion_min=1.8,
        high_lookback=200,
        low_recency_days=5,
        sample_limit=10,
    )
    assert result["available"] is True
    codes = [item["ts_code"] for item in result["candidates"]]
    # 只剩 H、G；按成交额降序 H(8亿) 在 G(6亿) 前
    assert codes == ["H.SZ", "G.SZ"]
    summary = result["summary"]
    assert summary["candidate_count"] == 2
    assert summary["total_amount_100m_yuan"] == 14.0
    assert summary["median_discount_after_high"] == 0.775
    head = result["candidates"][0]
    assert head["total_mv_100m_yuan"] == 120.0
    assert head["amount_100m_yuan"] == 8.0
    assert head["discount_after_high"] == 0.8
    # 最低点太旧（距大涨日 > 5 个交易日）应被剔除
    stale = _synthetic_discount_panel()
    stale.loc[stale["ts_code"] == "H.SZ", "low_to_target_days"] = 9
    res_stale = mp.build_discount_relaunch_samples(
        stale, market_cap_threshold_100m_yuan=80.0, amount_threshold_100m_yuan=5.0,
        pct_chg_threshold=7.0, discount_min=0.6, discount_max=0.85, pre_contraction_max=0.9,
        volume_expansion_min=1.8, high_lookback=200, low_recency_days=5, sample_limit=10,
    )
    assert [c["ts_code"] for c in res_stale["candidates"]] == ["G.SZ"]  # H 因最低点过旧被剔除
    # 月线跌破10月线（monthly_above_ma10=False/None）应被剔除
    below = _synthetic_discount_panel()
    below.loc[below["ts_code"] == "H.SZ", "monthly_above_ma10"] = False
    below.loc[below["ts_code"] == "G.SZ", "monthly_above_ma10"] = None
    res_below = mp.build_discount_relaunch_samples(
        below, market_cap_threshold_100m_yuan=80.0, amount_threshold_100m_yuan=5.0,
        pct_chg_threshold=7.0, discount_min=0.6, discount_max=0.85, pre_contraction_max=0.9,
        volume_expansion_min=1.8, high_lookback=200, low_recency_days=5, sample_limit=10,
    )
    assert res_below["summary"]["candidate_count"] == 0  # 月线在10月线下/不可评估全部剔除
    # 折扣带边界与缩量/放量阈值同时收紧后命中清空
    none_hit = mp.build_discount_relaunch_samples(
        _synthetic_discount_panel(),
        market_cap_threshold_100m_yuan=80.0,
        amount_threshold_100m_yuan=5.0,
        pct_chg_threshold=7.0,
        discount_min=0.6,
        discount_max=0.72,
        pre_contraction_max=0.9,
        volume_expansion_min=1.8,
        high_lookback=200,
        low_recency_days=5,
        sample_limit=10,
    )
    assert none_hit["available"] is True
    assert none_hit["summary"]["candidate_count"] == 0
    empty = mp.build_discount_relaunch_samples(
        None, 80.0, 5.0, 7.0, 0.6, 0.85, 0.9, 1.8, 200, 5, 10
    )
    assert empty["available"] is False


def test_attach_discount_after_high():
    dates = [f"202606{day:02d}" for day in range(1, 9)]
    rows = []
    # Z：前高=20260602 收盘120，之后最低 low=80（20260605）→ 折扣 80/120=0.6667
    z_close = [100.0, 120.0, 110.0, 90.0, 85.0, 95.0, 100.0, 108.0]
    z_low = [98.0, 118.0, 105.0, 86.0, 80.0, 92.0, 98.0, 104.0]
    # Y：收盘单调上行，最高即今日 → 无"前高之后"，应被跳过
    y_close = [50.0, 60.0, 70.0, 80.0, 90.0, 100.0, 110.0, 120.0]
    y_low = [c - 2 for c in y_close]
    for i, d in enumerate(dates):
        rows.append(dict(ts_code="Z.SZ", trade_date=d, close=z_close[i], low=z_low[i]))
        rows.append(dict(ts_code="Y.SZ", trade_date=d, close=y_close[i], low=y_low[i]))
    features = pd.DataFrame(rows)
    out = mp.attach_discount_after_high(features, "20260608", lookback=8)
    assert "Y.SZ" not in out  # 今日即收盘新高，无回撤
    z = out["Z.SZ"]
    assert z["prev_high_close"] == 120.0
    assert z["prev_high_date"] == "20260602"
    assert z["post_low"] == 80.0
    assert z["post_low_date"] == "20260605"
    assert z["low_to_target_days"] == 3  # 20260605 → 20260608 相隔 3 个交易日
    assert abs(z["discount_after_high"] - 0.6667) < 1e-4
    assert abs(z["close_vs_prev_high"] - 0.9) < 1e-4
    # 仅 1 个自然月，月线 10 月均线无法评估 → None
    assert z["monthly_above_ma10"] is None
    assert z["monthly_ma10"] is None


def test_attach_monthly_ma10():
    # 每月一根（11 个自然月），构造一个有效的折扣形态以便进入结果
    dates = ["20250815", "20250915", "20251015", "20251115", "20251215",
             "20260115", "20260215", "20260315", "20260415", "20260515", "20260615"]
    closes = [10.0, 12.0, 14.0, 16.0, 20.0, 13.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    lows = [c - 1.0 for c in closes]
    feats = pd.DataFrame({"ts_code": ["MM.SZ"] * 11, "trade_date": dates, "close": closes, "low": lows})
    out = mp.attach_discount_after_high(feats, "20260615", lookback=11)
    m = out["MM.SZ"]
    # 10 月均线 = 后 10 个月收盘均值 = mean(12,14,16,20,13,11,12,13,14,15) = 14.0；当前月收盘 15 ≥ 14 → True
    assert m["monthly_ma10"] == 14.0
    assert m["monthly_above_ma10"] is True
    # 前高 20(20251215) 之后最低 low=10(20260215)
    assert m["prev_high_close"] == 20.0 and m["post_low"] == 10.0


def test_add_numeric_features_discount_volume_signals():
    closes = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 11.0, 12.0]
    amounts = [2000.0, 2000.0, 800.0, 800.0, 800.0, 800.0, 800.0, 3000.0]
    dates = [f"202606{day:02d}" for day in range(1, 9)]
    daily = pd.DataFrame({
        "ts_code": ["Z.SZ"] * 8,
        "trade_date": dates,
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "pre_close": [closes[0]] + closes[:-1],
        "change": [0.0] * 8,
        "pct_chg": [0.0] * 8,
        "vol": [a / 10 for a in amounts],
        "amount": amounts,
    })
    features = mp.add_numeric_features(daily)
    last = features.sort_values("trade_date").iloc[-1]
    # 前20日均额=8000/7；前5日均额=800 → 缩量比=0.7
    assert abs(float(last["pre_volume_contraction_ratio"]) - 0.7) < 1e-6
    # 当日3000 / 前5日均额800 = 3.75
    assert abs(float(last["amount_vs_prev5_ratio"]) - 3.75) < 1e-6
    # 当日3000 / 前20日均额(8000/7) = 2.625
    assert abs(float(last["amount_ratio_20d"]) - 2.625) < 1e-6
    # 今日前5日涨幅 = close[t-1]/close[t-6]-1 = 11/10-1 = 10%
    assert abs(float(last["pre_ret_5d"]) - 10.0) < 1e-6


def test_amount_concentration_groupby_slicing():
    rows = []
    for date in ("20260608", "20260609"):
        for idx in range(30):
            rows.append({
                "ts_code": f"S{idx:03d}.SZ",
                "trade_date": date,
                "name": f"股{idx}",
                "amount": float((idx + 1) * 1000),
            })
    features = pd.DataFrame(rows)
    result = mp.build_amount_concentration(features, "20260609", "20260608")
    assert result["current"]["trade_date"] == "20260609"
    assert result["previous"]["trade_date"] == "20260608"
    # Top10 占比 = sum(21..30)/sum(1..30)
    expected = sum(range(21, 31)) / sum(range(1, 31))
    assert abs(result["current"]["top10_amount_ratio"] - round(expected, 4)) < 1e-9
    assert len(result["recent_series"]) == 2


# --------------------------------------------------------------------------- #
# qfq adjustment
# --------------------------------------------------------------------------- #
def test_apply_qfq_adjustment():
    daily = pd.DataFrame({
        "ts_code": ["X.SZ"] * 3,
        "trade_date": ["20260605", "20260608", "20260609"],
        "open": [10.0, 10.5, 5.4],
        "high": [10.6, 10.8, 5.6],
        "low": [9.9, 10.2, 5.2],
        "close": [10.5, 10.6, 5.5],
        "pre_close": [10.0, 10.5, 5.3],
        "pct_chg": [5.0, 0.95, 3.77],
        "vol": [100.0, 110.0, 220.0],
        "amount": [1000.0, 1100.0, 1150.0],
    })
    # 0609 除权：复权因子翻倍
    adj = pd.DataFrame({
        "ts_code": ["X.SZ"] * 3,
        "trade_date": ["20260605", "20260608", "20260609"],
        "adj_factor": [1.0, 1.0, 2.0],
    })
    adjusted, meta = mp.apply_qfq_adjustment(daily, adj, "20260609")
    assert meta["adjusted"] is True
    closes = adjusted.sort_values("trade_date")["close"].tolist()
    # 前两日 close 乘 1/2，目标日不变
    assert abs(closes[0] - 5.25) < 1e-9
    assert abs(closes[1] - 5.30) < 1e-9
    assert abs(closes[2] - 5.50) < 1e-9
    # 成交额不复权
    assert adjusted["amount"].tolist() == [1000.0, 1100.0, 1150.0]


# --------------------------------------------------------------------------- #
# Cleanup command
# --------------------------------------------------------------------------- #
def test_cleanup_intermediates():
    with tempfile.TemporaryDirectory() as tmp:
        reports = Path(tmp)
        (reports / "evidence_20260609_utf8.json").write_text("{}")
        (reports / "evidence_20260609_utf8.stderr.log").write_text("")
        (reports / "kline_20260609.json").write_text("{}")
        (reports / "report_context_20260609.json").write_text("{}")
        (reports / "lifecycle_20260609.json").write_text("{}")
        (reports / "strategy_picks_20260609.json").write_text("{}")
        (reports / "calibration_20260609.json").write_text("{}")
        (reports / "profile_refresh_20260609.json").write_text("{}")
        (reports / "weekly_factor_pack_20260609.json").write_text("{}")
        (reports / "factor_mining_discount_relaunch_20260609.json").write_text("{}")
        (reports / "factor_mining_discount_relaunch_20260609_detail.json").write_text("{}")
        module_dir = reports / "module_context_20260609"
        module_dir.mkdir()
        (module_dir / "meta.json").write_text("{}")
        (reports / "report_20260609.md").write_text("# 报告")
        (reports / "report_20260609.html").write_text("<html></html>")

        result = rdp.cleanup_intermediates(reports, "2026-06-09")
        assert result["date"] == "20260609"
        remaining = sorted(p.name for p in reports.iterdir())
        assert remaining == ["report_20260609.html", "report_20260609.md"]
        # 幂等：再跑一次不报错
        again = rdp.cleanup_intermediates(reports, "20260609")
        assert again["removed"] == []


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def main() -> int:
    tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = []
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - 测试运行器需要捕获一切
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
