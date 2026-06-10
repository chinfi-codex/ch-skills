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
    assert summary["limit_up_approx_count"] == 1
    empty = mp.build_money_effect_samples(None, 7.0, 2.0, 10)
    assert empty["available"] is False


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
