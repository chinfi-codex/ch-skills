#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""价格 → 收益率 → 利差。确定性算术，不做任何判断。

链路（全部免费、无密钥、可复现）：

    clean_price = Market Value / Par × 100          （SPDR 持仓导出）
    ytm         = 二分法解 (price, coupon, maturity)
    g_spread_bp = (ytm − 插值国债收益率) × 100      （FRED 曲线）
    excess_bp   = g_spread_bp − 对应指数 OAS        （ICE BofA）

**G-spread 不是 OAS。** 含赎回条款、浮息、次级永续的券算出来的 G-spread 有偏，
调用方必须据 has_embedded_option 标 option_biased，不与普通高级无担保券同图比较。
这个模块不替调用方做这件事——它只算数。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from collectors.fred import interpolate


def year_fraction(maturity: str, asof: str) -> float:
    m = dt.date.fromisoformat(str(maturity)[:10])
    a = dt.date.fromisoformat(str(asof)[:10])
    return (m - a).days / 365.25


def ytm(price: float, coupon: float, years: float, freq: int = 2,
        tol: float = 1e-10, iters: int = 200) -> float:
    """半年付息的到期收益率，二分法。返回百分数。

    用二分不用牛顿：牛顿在深度折价的长久期零息附近会跑飞，而这里的输入包含
    2.525% 2050 这种价格 56 的券。二分慢但永远收敛。
    """
    periods = max(1, round(years * freq))
    cpn = coupon / freq
    lo, hi = -0.5, 2.0
    for _ in range(iters):
        mid = (lo + hi) / 2
        rate = mid / freq
        if rate <= -1:
            lo = mid
            continue
        pv = sum(cpn / (1 + rate) ** k for k in range(1, periods + 1))
        pv += 100 / (1 + rate) ** periods
        if abs(pv - price) < tol:
            return mid * 100
        if pv > price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 100


def modified_duration(price: float, coupon: float, years: float,
                      yield_pct: float, freq: int = 2) -> Optional[float]:
    """修正久期。发行人层做样本内加权时用它当权重。"""
    periods = max(1, round(years * freq))
    rate = yield_pct / 100 / freq
    if rate <= -1 or price <= 0:
        return None
    cpn = coupon / freq
    weighted = 0.0
    for k in range(1, periods + 1):
        cash = cpn + (100 if k == periods else 0)
        pv = cash / (1 + rate) ** k
        weighted += pv * k / freq
    macaulay = weighted / price
    return macaulay / (1 + rate)


def spread_row(*, instrument: Dict[str, Any], price: float, asof: str,
               curve_day: Dict[int, float],
               index_oas_bp: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """把一只债的价格算成一组指标行。曲线缺当天就返回 None，不用前值冒充。"""
    maturity = instrument.get("maturity")
    coupon = instrument.get("coupon")
    if maturity is None or coupon is None or not curve_day:
        return None
    years = year_fraction(maturity, asof)
    if years <= 0:
        return None

    y = ytm(price, float(coupon), years)
    ust = interpolate(curve_day, years)
    if ust is None:
        return None
    gspread = (y - ust) * 100

    segment = instrument.get("segment") or "ig"
    bench_key = "hy" if segment == "hy" else "ig"
    bench = index_oas_bp.get(bench_key)
    excess = None if bench is None else gspread - bench

    return {
        "years": years,
        "ytm": y,
        "ust": ust,
        "gspread_bp": gspread,
        "excess_bp": excess,
        "bench_key": bench_key,
        "bench_bp": bench,
        "duration": modified_duration(price, float(coupon), years, y),
    }


def nearest_curve_day(curve: Dict[str, Dict[int, float]],
                      asof: str, max_lag_days: int = 5) -> Optional[str]:
    """找不晚于 asof 的最近一个国债曲线日。

    超过 max_lag_days 就返回 None——宁可当天不出利差，也不拿一周前的曲线
    去减今天的收益率。那样算出来的「利差变动」其实是利率变动。
    """
    target = dt.date.fromisoformat(str(asof)[:10])
    best: Optional[str] = None
    for day in curve:
        d = dt.date.fromisoformat(day)
        if d <= target and (target - d).days <= max_lag_days:
            if best is None or d > dt.date.fromisoformat(best):
                best = day
    return best
