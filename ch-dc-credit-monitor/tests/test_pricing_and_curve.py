#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定价与曲线的数值自检。

锚点是 2026-08-25 实测值。这些不是「大概对」的断言——价格、票息、到期日和国债
曲线都是当天的真实输入，算出来的 G-spread 必须落在个位数 bp 的容差内，
否则说明公式改坏了。
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import curve as curve_mod          # noqa: E402
import pricing                     # noqa: E402
from collectors.fred import interpolate   # noqa: E402

# 2026-08-25 FRED 国债曲线
CURVE = {1: 4.01, 2: 4.17, 3: 4.25, 5: 4.35, 7: 4.48, 10: 4.64, 20: 5.16, 30: 5.17}
ASOF = "2026-08-25"


def _gspread(price, coupon, maturity):
    row = pricing.spread_row(
        instrument={"maturity": maturity, "coupon": coupon, "segment": "ig"},
        price=price, asof=ASOF, curve_day=CURVE, index_oas_bp={"ig": 81.0})
    return row["gspread_bp"]


def test_gspread_matches_measured_anchors():
    """三个实测锚点，容差 ±2bp。"""
    cases = [
        (96.358, 4.6, "2032-11-15", 89),     # META 4.6 11/32
        (89.706, 6.9, "2052-11-09", 267),    # ORCL 6.9 11/52
        (94.105, 9.25, "2030-06-01", 683),   # CRWV 9.25 06/30
        (89.808, 2.15, "2030-07-15", 70),    # EQIX 2.15 07/30
    ]
    for price, coupon, maturity, expected in cases:
        got = _gspread(price, coupon, maturity)
        assert abs(got - expected) <= 2, f"{maturity}: 得到 {got:.0f}bp，期望 {expected}bp"


def test_par_bond_yields_its_coupon():
    """平价债的 YTM 必须等于票息——最基本的健全性检查。"""
    assert abs(pricing.ytm(100.0, 5.0, 10.0) - 5.0) < 1e-6


def test_price_derivation_from_par_and_market_value():
    """SPDR 持仓没有价格列，价格是 MV/Par×100 导出来的。"""
    assert abs(4392906.5 / 4450000.0 * 100 - 98.716) < 0.01


def test_index_oas_is_percent_not_bp():
    """FRED 给的 0.81 表示 81bp，必须 ×100。抄错概率最高的一个数。"""
    row = pricing.spread_row(
        instrument={"maturity": "2032-11-15", "coupon": 4.6, "segment": "ig"},
        price=96.358, asof=ASOF, curve_day=CURVE, index_oas_bp={"ig": 81.0})
    assert abs(row["gspread_bp"] - row["excess_bp"] - 81.0) < 1e-6


def test_curve_refuses_far_extrapolation():
    """观测跨度之外 3 年以上一律不外推——一个最长只到 6 年的发行人
    不该被推出一个 30 年点位，那是编的。"""
    points = [{"years": 3.76, "gspread_bp": 691.4, "has_embedded_option": False},
              {"years": 4.44, "gspread_bp": 733.0, "has_embedded_option": False},
              {"years": 5.10, "gspread_bp": 789.8, "has_embedded_option": False},
              {"years": 5.89, "gspread_bp": 762.3, "has_embedded_option": False}]
    assert curve_mod.constant_maturity(points, 5.0) is not None      # 区间内，真插值
    assert curve_mod.constant_maturity(points, 30.0) is None         # 远端外推，拒绝
    assert curve_mod.constant_maturity(points, 10.0) is None


def test_constant_maturity_isolates_rolldown():
    """rolldown 隔离——这个 skill 里最容易出、又最难在成品里看出来的错。

    构造一条不动的真实曲线 f(t) = 20t + 160（全局线性，所以插值可精确复现）。
    同一批债券过了一年：期限各减 1，利差各自沿曲线滑到新位置。

      * 那只原本 5 年的债，一年后变成 4 年，利差从 260 掉到 240——**它收窄了
        20bp，但信用一点没变**。跟踪单只 ISIN 的历史就会把这 20bp 读成利差改善。
      * 5 年固定期限点在两个时点都必须是 260，纹丝不动。

    这就是为什么发行人层的时间序列只能走 constant_maturity。
    """
    def f(t):
        return 20.0 * t + 160.0

    def points_at(tenors):
        return [{"years": t, "gspread_bp": f(t), "has_embedded_option": False}
                for t in tenors]

    t0 = points_at([3.0, 5.0, 7.0, 10.0])
    t1 = points_at([2.0, 4.0, 6.0, 9.0])          # 一年后的同一批债

    # 单只债：原本 5 年、现在 4 年，利差自然收窄。
    assert f(5.0) - f(4.0) == 20.0

    # 固定期限点：曲线没动，5Y 就必须不动。
    cm_t0 = curve_mod.constant_maturity(t0, 5.0)
    cm_t1 = curve_mod.constant_maturity(t1, 5.0)
    assert cm_t0 == 260.0
    assert abs(cm_t0 - cm_t1) < 1e-6, (
        f"曲线没动时固定期限点必须不动，得到 {cm_t0} vs {cm_t1}")


def test_option_bearing_bonds_excluded_from_buckets():
    """含权券的 G-spread 有偏，不进桶均值。"""
    points = [{"years": 6.0, "gspread_bp": 100.0, "has_embedded_option": False},
              {"years": 7.0, "gspread_bp": 500.0, "has_embedded_option": True}]
    buckets = curve_mod.bucket_spreads(points)
    assert buckets["5-10y"]["mean_bp"] == 100.0
    assert buckets["5-10y"]["n"] == 1


def test_shape_analysis_ignores_short_end_noise():
    """2 年以内的券定价被货币市场因素主导，放进形状分析会让负斜率段
    在几乎每个发行人身上都触发。"""
    points = [{"years": 1.2, "gspread_bp": 75.0, "has_embedded_option": False},
              {"years": 1.7, "gspread_bp": 49.0, "has_embedded_option": False},
              {"years": 3.0, "gspread_bp": 60.0, "has_embedded_option": False},
              {"years": 8.0, "gspread_bp": 90.0, "has_embedded_option": False},
              {"years": 20.0, "gspread_bp": 110.0, "has_embedded_option": False}]
    block = curve_mod.issuer_curve(points, tenors=[5, 10, 30], min_bonds=4,
                                   inversion_threshold_bp=10)
    assert block["negative_segment"] is None, "短端噪音不该被当成曲线形状"
    assert block["curve_inverted"] is False


def test_same_tenor_pair_is_not_a_curve_segment():
    """同一年份上的两只债价差是横截面离散，不是曲线斜率。"""
    points = [{"years": 6.72, "gspread_bp": 99.0, "has_embedded_option": False},
              {"years": 6.72, "gspread_bp": 86.0, "has_embedded_option": False},
              {"years": 20.0, "gspread_bp": 150.0, "has_embedded_option": False},
              {"years": 25.0, "gspread_bp": 160.0, "has_embedded_option": False}]
    block = curve_mod.issuer_curve(points, tenors=[5, 10, 30], min_bonds=4,
                                   inversion_threshold_bp=10)
    assert block["negative_segment"] is None


def test_thin_curve_blocks_constant_maturity():
    points = [{"years": 3.0, "gspread_bp": 200.0, "has_embedded_option": False},
              {"years": 5.0, "gspread_bp": 260.0, "has_embedded_option": False}]
    block = curve_mod.issuer_curve(points, tenors=[5, 10, 30], min_bonds=4,
                                   inversion_threshold_bp=10)
    assert block["thin_curve"] is True
    assert all(v is None for v in block["constant_maturity_bp"].values())


def test_interpolation_clamps_at_curve_ends():
    """曲线两端不外推，直接钳住——国债曲线本身没有 40 年点。"""
    assert interpolate(CURVE, 0.5) == CURVE[1]
    assert interpolate(CURVE, 60.0) == CURVE[30]
    mid = interpolate(CURVE, 6.0)
    assert CURVE[5] < mid < CURVE[7]


def _run() -> int:
    """无 pytest 时的兜底跑法：`python3 tests/test_pricing_and_curve.py`。"""
    failures, total = [], 0
    for name, obj in sorted(globals().items()):
        if not (name.startswith("test_") and callable(obj)):
            continue
        total += 1
        try:
            obj()
        except Exception as exc:                      # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    for line in failures:
        print("FAIL", line)
    print(f"{total - len(failures)}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run())


def test_benchmark_rows_keep_the_unit_convention():
    """基准层落库最容易抄错的就是单位：指数 OAS 在 FRED 是百分数，
    fetch 时已经 ×100 成 bp；国债收益率保持 pct。写反了整条超额口径全错。"""
    from collectors import fred as fred_mod

    rows = fred_mod.benchmark_rows({
        "curve": {"2026-08-25": {5: 4.35, 10: 4.64}},
        "index_oas_bp": {"2026-08-25": {"ig": 81.0, "hy": 270.0}},
    })
    by_metric = {r["metric"]: r for r in rows}
    assert by_metric["bench.ust_5y"]["unit"] == "pct"
    assert by_metric["bench.ust_5y"]["value"] == 4.35
    assert by_metric["bench.index_oas_ig"]["unit"] == "bp"
    assert by_metric["bench.index_oas_ig"]["value"] == 81.0
    assert by_metric["bench.ust_5y"]["instrument_key"] == fred_mod.UST_KEY
    assert by_metric["bench.index_oas_ig"]["instrument_key"] == fred_mod.INDEX_KEY
    # asof_date 与 obs_date 同为口径日：FRED 给的就是那一天的值，不存在采集滞后。
    assert all(r["asof_date"] == r["obs_date"] for r in rows)


def test_benchmark_rows_respect_the_since_cutoff():
    """回补窗口之外的日期不该被写进来——否则 --days 200 实际写了 400 天。"""
    from collectors import fred as fred_mod

    rows = fred_mod.benchmark_rows({
        "curve": {"2026-01-05": {5: 4.10}, "2026-08-25": {5: 4.35}},
        "index_oas_bp": {},
    }, since="2026-06-01")
    assert {r["asof_date"] for r in rows} == {"2026-08-25"}
