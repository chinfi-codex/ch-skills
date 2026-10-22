"""双轨（估值锚定 + 流动性挖掘）两个原子脚本的纯计算回归，不碰网络与数据库。

锁定的口径：
- 分位 = 历史里 ≤ 当日值的占比（含当日），PE/PB 只取正值样本，样本不足不出分位；
- 大盘轨只取已锁星（★★ 及以上）主线里 ≥ 400 亿的成员；
- 中小盘门槛 = 市值 < 400 亿 且 (成交额比 ≥ 1.8 或 换手比 ≥ 2.0) 且 近 5 日放量日 ≥ 3；
- 主线成员连接只看 stars ≥ 2，主线外候选单独成组。

跑法：python3 tests/run_tests.py dual
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

SKILL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import liquidity_scan as ls  # noqa: E402
import valuation_anchor as va  # noqa: E402


# --------------------------------------------------------------------------- #
# valuation_anchor
# --------------------------------------------------------------------------- #
def test_percentile_counts_ties_and_needs_samples():
    values = list(range(1, 301))  # 末值 300 是最大值
    out = va.percentile_of_last(values, window=750)
    assert out["percentile"] == 100.0 and out["samples"] == 300
    values = list(range(300, 0, -1))  # 末值 1 是最小值
    assert va.percentile_of_last(values, window=750)["percentile"] == round(1 / 300 * 100, 1)
    short = va.percentile_of_last(list(range(1, 100)), window=750)
    assert short["percentile"] is None and "fewer than" in short["reason"]


def test_percentile_drops_non_positive_and_flags_loss_maker():
    values = [-5.0] * 50 + list(range(1, 301))
    out = va.percentile_of_last(values, window=750)
    assert out["samples"] == 300  # 负 PE 不进母体
    loss = va.percentile_of_last(list(range(1, 301)) + [-3.0], window=750)
    assert loss["percentile"] is None and loss["latest"] == -3.0


def test_percentile_window_truncates_to_recent():
    values = [100.0] * 500 + list(range(1, 251))  # 窗口 250 只看后段
    out = va.percentile_of_last(values, window=250)
    assert out["samples"] == 250 and out["percentile"] == 100.0


def _context():
    return {"money_effect": {"theme_grouping_aid": {"records": [
        {"ts_code": "A.SZ", "name": "大票A", "total_mv_100m_yuan": 2380.0},
        {"ts_code": "B.SH", "name": "大票B", "total_mv_100m_yuan": 400.0},
        {"ts_code": "C.SZ", "name": "小票C", "total_mv_100m_yuan": 128.0},
        {"ts_code": "D.SZ", "name": "大票D", "total_mv_100m_yuan": 900.0},
    ]}}}


def _mapping():
    return {"themes": [
        {"name": "半导体", "stars": 3, "members": ["A.SZ", "B.SH", "C.SZ"],
         "sublines": [{"name": "设备", "members": ["A.SZ"]}]},
        {"name": "轮动", "stars": 1, "members": ["D.SZ"], "sublines": []},
    ]}


def test_large_cap_selection_respects_stars_and_threshold():
    picked = va.select_large_cap_members(_context(), _mapping())
    assert [p["ts_code"] for p in picked] == ["A.SZ", "B.SH"]  # 400 亿含边界；★ 主题不取
    assert picked[0]["subline"] == "设备" and picked[1]["subline"] is None


def test_stock_reading_requires_row_on_asof():
    hist = pd.DataFrame({
        "ts_code": ["A.SZ"] * 300,
        "trade_date": [f"2025{(i // 28) + 1:02d}{(i % 28) + 1:02d}" for i in range(300)],
        "pe_ttm": [float(i + 1) for i in range(300)],
        "pb": [2.0] * 300,
        "dv_ttm": [0.5] * 300,
    })
    asof = hist["trade_date"].iloc[-1]
    out = va.stock_reading(hist, asof)
    assert out["available"] and out["pe_pct_3y"] == 100.0 and out["pe_valid"]
    assert out["dv_ttm_pct"] == 0.5
    missing = va.stock_reading(hist, "20991231")
    assert not missing["available"] and "no daily_basic row" in missing["reason"]


# --------------------------------------------------------------------------- #
# liquidity_scan
# --------------------------------------------------------------------------- #
def _frames(amounts_by_code, mv_by_code, turnover_by_code=None):
    days = [f"202609{d:02d}" for d in range(1, 27)]
    daily_rows, basic_rows = [], []
    for code, amounts in amounts_by_code.items():
        turns = (turnover_by_code or {}).get(code) or [1.0] * len(days)
        for day, amt, turn in zip(days, amounts, turns):
            daily_rows.append({"ts_code": code, "trade_date": day, "pct_chg": 1.0, "amount": amt})
            basic_rows.append({"ts_code": code, "trade_date": day, "turnover_rate": turn,
                               "total_mv": mv_by_code[code] * 1e4})
    return pd.DataFrame(daily_rows), pd.DataFrame(basic_rows), days[-1]


def test_threshold_needs_surge_and_sustained_days():
    base = [100.0] * 21
    sustained = base + [100.0, 200.0, 200.0, 100.0, 250.0]   # 3 个放量日，当日 2.5x
    one_day = base + [100.0] * 4 + [300.0]                   # 只有当日放量
    big = base + [100.0, 200.0, 200.0, 100.0, 250.0]         # 同形态但市值 ≥ 400 亿
    daily, basic, asof = _frames(
        {"S.SZ": sustained, "O.SZ": one_day, "L.SH": big},
        {"S.SZ": 128.0, "O.SZ": 86.0, "L.SH": 2380.0},
    )
    today = ls.compute_liquidity(daily, basic, asof)
    passed = ls.apply_thresholds(today)
    assert list(passed["ts_code"]) == ["S.SZ"]
    row = today.set_index("ts_code").loc["S.SZ"]
    assert row["heavy_days_5"] == 3 and round(row["amount_ratio_20d"], 2) == 2.27


def test_turnover_surge_alone_can_pass():
    base = [100.0] * 21
    amounts = base + [160.0, 160.0, 160.0, 100.0, 150.0]  # 成交额比 < 1.8，但 3 个 >1.5x 放量日
    turns = [1.0] * 25 + [2.5]
    daily, basic, asof = _frames({"T.SZ": amounts}, {"T.SZ": 64.0}, {"T.SZ": turns})
    passed = ls.apply_thresholds(ls.compute_liquidity(daily, basic, asof))
    assert list(passed["ts_code"]) == ["T.SZ"]


def test_scan_splits_mainline_and_outside():
    base = [100.0] * 21
    hot = base + [100.0, 200.0, 200.0, 100.0, 250.0]
    daily, basic, asof = _frames({"C.SZ": hot, "X.SZ": hot}, {"C.SZ": 128.0, "X.SZ": 90.0})
    today = ls.compute_liquidity(daily, basic, asof)
    scan = ls.build_scan(today, {"C.SZ": "小票C", "X.SZ": "*ST某某"}, _mapping(), asof)
    assert [c["ts_code"] for c in scan["mainline_candidates"]] == ["C.SZ"]
    assert scan["mainline_candidates"][0]["theme"] == "半导体"
    assert [c["ts_code"] for c in scan["outside_candidates"]] == ["X.SZ"]
    assert scan["outside_candidates"][0]["is_st"] is True
    assert scan["passed_count"] == 2 and scan["mapping_applied"]
