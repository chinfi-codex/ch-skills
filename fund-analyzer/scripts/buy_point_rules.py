#!/usr/bin/env python3
"""
Core calculations and rule evaluation for fund buy-point analysis.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

LEFT_DISCOUNT_MIN = 0.65
LEFT_DISCOUNT_MAX = 0.75
LEFT_STOP_RATIO = 0.63
LEFT_MIN_PULLBACK_DAYS = 15
RIGHT_BREAKOUT_LOOKBACK = 20
RIGHT_BREAKOUT_RECENT_DAYS = 3
RIGHT_PULLBACK_MIN = 0.02
RIGHT_PULLBACK_MAX = 0.06
RIGHT_PULLBACK_PREFERRED_MIN = 0.03
RIGHT_PULLBACK_PREFERRED_MAX = 0.06
RIGHT_SUPPORT_BUFFER = 0.98
RIGHT_BREAKDOWN_BUFFER = 0.97


def process_nav_data(nav_df: pd.DataFrame) -> Dict[str, Any]:
    nav_series = pd.to_numeric(nav_df["nav"], errors="coerce").dropna()
    if nav_series.empty:
        return {}

    nav_series = nav_series[~nav_series.index.duplicated(keep="last")]
    current_nav = float(nav_series.iloc[-1])
    current_date = nav_series.index[-1]

    monthly_nav = nav_series.resample("ME").last().dropna()
    recent_window = nav_series.tail(120)
    recent_high = float(recent_window.max())
    recent_high_date = recent_window.idxmax()
    decline_pct = ((current_nav / recent_high) - 1) * 100 if recent_high > 0 else 0.0
    decline_ratio = current_nav / recent_high if recent_high > 0 else 0.0

    recent_breakout = detect_recent_breakout(nav_series.tail(RIGHT_BREAKOUT_LOOKBACK + 15))
    pullback_ratio = 0.0
    if recent_breakout["breakout_high"] > 0:
        pullback_ratio = (
            recent_breakout["breakout_high"] - current_nav
        ) / recent_breakout["breakout_high"]

    return {
        "current_nav": current_nav,
        "current_date": current_date,
        "ma5": latest_ma(nav_series, 5),
        "ma10": latest_ma(nav_series, 10),
        "ma20": latest_ma(nav_series, 20),
        "ma60": latest_ma(nav_series, 60),
        "ma200": latest_ma(nav_series, 200),
        "ma20_monthly": latest_ma(monthly_nav, 20),
        "recent_high": recent_high,
        "recent_high_date": recent_high_date,
        "decline_pct": decline_pct,
        "decline_ratio": decline_ratio,
        "decline_days": trading_days_between(nav_series.index, recent_high_date, current_date),
        "slope_10": annualized_slope(nav_series.tail(10)),
        "slope_20": annualized_slope(nav_series.tail(20)),
        "recent_breakout": recent_breakout,
        "pullback_ratio": pullback_ratio,
        "nav_history": nav_series.tail(60).tolist(),
        "nav_dates": [idx.strftime("%Y-%m-%d") for idx in nav_series.tail(60).index],
    }


def process_market_data(market_df: pd.DataFrame) -> Dict[str, Any]:
    frame = market_df.copy().sort_index()
    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    if close.empty:
        return {}

    vol_series = pd.to_numeric(frame["vol"], errors="coerce").dropna()
    return {
        "current_close": float(close.iloc[-1]),
        "ma20": latest_ma(close, 20),
        "ma60": latest_ma(close, 60),
        "ma200": latest_ma(close, 200),
        "current_vol": float(vol_series.iloc[-1]) if not vol_series.empty else 0.0,
        "vol_ratio_20": volume_ratio(vol_series, 20),
        "vol_ratio_5": volume_ratio(vol_series, 5),
        "contraction_ratio": market_contraction_ratio(vol_series),
        "stabilization_k": detect_candlestick(frame.tail(3)),
        "rsi14": calculate_rsi(close, 14),
        **calculate_macd(close),
        "support_midpoint": support_midpoint(frame.tail(8)),
    }


def analyze_buy_signals(nav_data: Dict[str, Any], market_data: Dict[str, Any]) -> Dict[str, Any]:
    left_mode = evaluate_left_mode(nav_data, market_data)
    right_mode = evaluate_right_mode(nav_data, market_data)
    return {
        "left_mode": left_mode,
        "right_mode": right_mode,
        "overall": evaluate_overall(left_mode, right_mode),
    }


def evaluate_left_mode(nav_data: Dict[str, Any], market_data: Dict[str, Any]) -> Dict[str, Any]:
    current_ratio = nav_data.get("decline_ratio", 0.0)
    slope_20 = nav_data.get("slope_20", 0.0)
    contraction_ratio = market_data.get("contraction_ratio", 1.0)
    rsi14 = market_data.get("rsi14")
    market_k = market_data.get("stabilization_k", "其他")

    checks = {
        "space_discount": {
            "passed": LEFT_DISCOUNT_MIN <= current_ratio <= LEFT_DISCOUNT_MAX,
            "detail": f"当前折扣率 {current_ratio:.2f}，目标区间 {LEFT_DISCOUNT_MIN:.2f}-{LEFT_DISCOUNT_MAX:.2f}",
        },
        "time_adjustment": {
            "passed": nav_data.get("decline_days", 0) >= LEFT_MIN_PULLBACK_DAYS,
            "detail": f"距近期高点 {nav_data.get('decline_days', 0)} 个交易日，要求 >= {LEFT_MIN_PULLBACK_DAYS}",
        },
        "slope_repair": {
            "passed": slope_20 > -0.001,
            "detail": f"20日斜率 {slope_20:+.4f}，要求 > -0.001",
        },
        "market_below_ma200": {
            "passed": market_data.get("ma200") is not None
            and market_data.get("current_close", 0) < market_data.get("ma200", 0),
            "detail": f"上证 {market_data.get('current_close', 0):.2f} / 200日线 {market_data.get('ma200', 0) or 0:.2f}",
        },
        "market_volume_contraction": {
            "passed": contraction_ratio < 0.80,
            "detail": f"成交量收缩比 {contraction_ratio:.2f}，要求 < 0.80",
        },
        "market_stabilization": {
            "passed": market_k in {"长下影线", "十字星", "小阳线", "锤子线"},
            "detail": f"市场形态 {market_k}",
        },
        "market_rsi": {
            "passed": rsi14 is not None and rsi14 < 35,
            "detail": f"RSI(14) {rsi14:.1f}" if rsi14 is not None else "RSI 数据不足",
        },
    }
    required_keys = [
        "space_discount",
        "time_adjustment",
        "market_volume_contraction",
        "market_stabilization",
    ]
    required_passed = all(checks[key]["passed"] for key in required_keys)
    passed_count = sum(1 for value in checks.values() if value["passed"])

    stage = "条件不足"
    position_hint = "0%"
    if required_passed and current_ratio <= 0.70:
        stage = "左侧试仓"
        position_hint = "25%" if current_ratio > 0.65 else "40%"
    elif required_passed:
        stage = "左侧观察"
        position_hint = "10%"
    elif passed_count >= 4:
        stage = "左侧关注"

    return {
        "checks": checks,
        "required_passed": required_passed,
        "passed_count": passed_count,
        "stage": stage,
        "position_hint": position_hint,
        "summary": left_summary(stage),
    }


def evaluate_right_mode(nav_data: Dict[str, Any], market_data: Dict[str, Any]) -> Dict[str, Any]:
    breakout = nav_data.get("recent_breakout", {})
    pullback_ratio = nav_data.get("pullback_ratio", 0.0)
    current_nav = nav_data.get("current_nav", 0.0)
    breakout_floor = breakout.get("breakout_floor", 0.0)
    breakout_days = breakout.get("days_since_breakout")
    market_hist = market_data.get("macd_hist")
    market_hist_prev = market_data.get("macd_hist_prev")
    market_k = market_data.get("stabilization_k", "其他")

    checks = {
        "trend_breakout": {
            "passed": breakout.get("passed", False),
            "detail": breakout.get("detail", "未出现有效突破"),
        },
        "multi_ma_alignment": {
            "passed": bullish_alignment(
                nav_data.get("current_nav"),
                nav_data.get("ma5"),
                nav_data.get("ma20"),
                nav_data.get("ma60"),
            ),
            "detail": f"NAV/MA5/MA20/MA60 = {fmt_float(nav_data.get('current_nav'))}/{fmt_float(nav_data.get('ma5'))}/{fmt_float(nav_data.get('ma20'))}/{fmt_float(nav_data.get('ma60'))}",
        },
        "pullback_range": {
            "passed": RIGHT_PULLBACK_MIN <= pullback_ratio <= RIGHT_PULLBACK_MAX,
            "detail": f"回踩幅度 {pullback_ratio * 100:.1f}%，要求 {RIGHT_PULLBACK_MIN * 100:.0f}%-{RIGHT_PULLBACK_MAX * 100:.0f}%",
        },
        "pullback_support": {
            "passed": breakout_floor > 0 and current_nav >= breakout_floor * RIGHT_SUPPORT_BUFFER,
            "detail": f"当前净值 {current_nav:.4f} / 突破阳线支撑 {breakout_floor:.4f}",
        },
        "slope_maintained": {
            "passed": nav_data.get("slope_10", 0.0) > 0,
            "detail": f"10日斜率 {nav_data.get('slope_10', 0.0):+.4f}",
        },
        "market_trend_background": {
            "passed": market_data.get("ma20") is not None
            and market_data.get("current_close", 0) > market_data.get("ma20", 0)
            and market_hist is not None
            and market_hist_prev is not None
            and market_hist > market_hist_prev,
            "detail": f"上证 {market_data.get('current_close', 0):.2f} / 20日线 {market_data.get('ma20', 0) or 0:.2f} / MACD柱 {fmt_float(market_hist_prev)}->{fmt_float(market_hist)}",
        },
        "pullback_window": {
            "passed": breakout_days is not None and 3 <= breakout_days <= 8,
            "detail": f"突破后第 {breakout_days if breakout_days is not None else 'N/A'} 个交易日",
        },
        "market_stabilization": {
            "passed": market_k in {"锤子线", "十字星", "小阳线", "长下影线"},
            "detail": f"市场形态 {market_k}",
        },
        "market_support_valid": {
            "passed": market_data.get("support_midpoint") is not None
            and market_data.get("current_close", 0) > market_data.get("support_midpoint", 0),
            "detail": f"收盘 {market_data.get('current_close', 0):.2f} / 支撑中位 {market_data.get('support_midpoint', 0) or 0:.2f}",
        },
        "washout_volume": {
            "passed": market_data.get("vol_ratio_5", 1.0) < 0.90,
            "detail": f"回踩量能比 {market_data.get('vol_ratio_5', 1.0):.2f}，要求 < 0.90",
        },
        "market_rsi": {
            "passed": market_data.get("rsi14") is not None and 50 < market_data.get("rsi14", 0) < 70,
            "detail": f"RSI(14) {market_data.get('rsi14', 0):.1f}" if market_data.get("rsi14") is not None else "RSI 数据不足",
        },
    }
    required_keys = [
        "trend_breakout",
        "multi_ma_alignment",
        "pullback_range",
        "pullback_support",
        "market_trend_background",
        "pullback_window",
        "market_support_valid",
        "market_stabilization",
    ]
    required_passed = all(checks[key]["passed"] for key in required_keys)
    passed_count = sum(1 for value in checks.values() if value["passed"])

    stage = "条件不足"
    position_hint = "0%"
    if required_passed and RIGHT_PULLBACK_PREFERRED_MIN <= pullback_ratio <= RIGHT_PULLBACK_PREFERRED_MAX:
        stage = "右侧确认"
        position_hint = "20%-30%"
    elif checks["trend_breakout"]["passed"] and checks["multi_ma_alignment"]["passed"]:
        stage = "右侧跟踪"

    return {
        "checks": checks,
        "required_passed": required_passed,
        "passed_count": passed_count,
        "stage": stage,
        "position_hint": position_hint,
        "summary": right_summary(stage),
    }


def evaluate_overall(left_mode: Dict[str, Any], right_mode: Dict[str, Any]) -> Dict[str, Any]:
    if left_mode["required_passed"] and right_mode["required_passed"]:
        return {
            "dominant_mode": "左侧优先，右侧视为加仓确认",
            "action": "分批建仓",
            "position_hint": "左侧 10%-25%，右侧加仓至总仓 30%-40%",
            "rationale": "双模式同时满足时，优先保留左侧更低成本，右侧只作为趋势确认后的加仓触发。",
        }
    if right_mode["stage"] == "右侧确认":
        return {
            "dominant_mode": "右侧",
            "action": "分批建仓",
            "position_hint": right_mode["position_hint"],
            "rationale": "净值完成突破并处于健康回踩窗口，市场趋势背景同步配合。",
        }
    if left_mode["stage"] == "左侧试仓":
        return {
            "dominant_mode": "左侧",
            "action": "试仓",
            "position_hint": left_mode["position_hint"],
            "rationale": "折扣、时间和市场衰竭条件已经基本成立，但仍需继续观察趋势修复。",
        }
    if right_mode["stage"] == "右侧跟踪":
        return {
            "dominant_mode": "右侧",
            "action": "跟踪",
            "position_hint": "0%-10%",
            "rationale": "趋势突破已经出现，但回踩和市场确认还不够完整。",
        }
    if left_mode["stage"] in {"左侧观察", "左侧关注"}:
        return {
            "dominant_mode": "左侧",
            "action": "等待",
            "position_hint": left_mode["position_hint"],
            "rationale": "估值折扣逐步接近，但关键企稳信号还未完全落地。",
        }
    return {
        "dominant_mode": "双模式均不成立",
        "action": "放弃",
        "position_hint": "0%",
        "rationale": "当前既没有足够的左侧折扣，也没有有效的右侧趋势确认。",
    }


def calculate_risk_levels(nav_data: Dict[str, Any]) -> Dict[str, Any]:
    current_nav = nav_data.get("current_nav", 0.0)
    recent_high = nav_data.get("recent_high", 0.0)
    ma20 = nav_data.get("ma20")
    breakout_floor = nav_data.get("recent_breakout", {}).get("breakout_floor", 0.0)
    left_stop = recent_high * LEFT_STOP_RATIO if recent_high > 0 else current_nav * LEFT_STOP_RATIO
    right_stop = breakout_floor * RIGHT_BREAKDOWN_BUFFER if breakout_floor > 0 else ma20
    return {
        "left_stop": left_stop,
        "right_stop": right_stop,
        "take_profit": recent_high * 1.05 if recent_high > 0 else None,
        "trailing_take_profit": ma20,
    }


def build_suggestion(signals: Dict[str, Any]) -> Dict[str, Any]:
    overall = signals["overall"]
    left_checks = signals["left_mode"]["checks"]
    right_checks = signals["right_mode"]["checks"]
    waiting_for: List[str] = []
    if not left_checks["market_volume_contraction"]["passed"]:
        waiting_for.append("上证继续缩量到更明显的衰竭区间")
    if not left_checks["market_stabilization"]["passed"]:
        waiting_for.append("市场出现更明确的止跌K线")
    if not right_checks["pullback_range"]["passed"] and right_checks["trend_breakout"]["passed"]:
        waiting_for.append("突破后回踩幅度进入 2%-6% 区间")
    if not right_checks["pullback_window"]["passed"] and right_checks["trend_breakout"]["passed"]:
        waiting_for.append("回踩进入突破后第 3-8 个交易日窗口")
    if not right_checks["market_trend_background"]["passed"]:
        waiting_for.append("上证重新站稳 20 日线并扩大 MACD 红柱")
    return {
        "action": overall["action"],
        "position_hint": overall["position_hint"],
        "waiting_for": waiting_for,
    }


def detect_recent_breakout(nav_series: pd.Series) -> Dict[str, Any]:
    if len(nav_series) < RIGHT_BREAKOUT_LOOKBACK + 5:
        return {"passed": False, "detail": "净值样本不足", "days_since_breakout": None, "breakout_high": 0.0, "breakout_floor": 0.0}
    series = nav_series.dropna()
    breakout_idx = None
    breakout_high = 0.0
    breakout_floor = 0.0
    start = max(RIGHT_BREAKOUT_LOOKBACK, len(series) - RIGHT_BREAKOUT_RECENT_DAYS - 1)
    for idx in range(start, len(series)):
        prev_window = series.iloc[idx - RIGHT_BREAKOUT_LOOKBACK:idx]
        price = float(series.iloc[idx])
        threshold = float(prev_window.max()) * 0.995
        if price > threshold:
            breakout_idx = series.index[idx]
            breakout_high = price
            breakout_floor = float(prev_window.tail(3).min())
            break
    if breakout_idx is None:
        return {"passed": False, "detail": "最近 3 个交易日未出现 20 日新高突破", "days_since_breakout": None, "breakout_high": 0.0, "breakout_floor": 0.0}
    return {
        "passed": True,
        "detail": f"最近 {trading_days_between(series.index, breakout_idx, series.index[-1])} 个交易日前出现 20 日新高突破",
        "days_since_breakout": trading_days_between(series.index, breakout_idx, series.index[-1]),
        "breakout_high": breakout_high,
        "breakout_floor": breakout_floor,
    }


def detect_candlestick(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty:
        return "无法判断"
    latest = frame.iloc[-1]
    open_price = coalesce_price(latest.get("open"), latest.get("close"))
    close_price = coalesce_price(latest.get("close"), latest.get("open"))
    high_price = coalesce_price(latest.get("high"), max(open_price, close_price))
    low_price = coalesce_price(latest.get("low"), min(open_price, close_price))
    body = abs(close_price - open_price)
    total_range = max(high_price - low_price, 1e-8)
    upper_shadow = high_price - max(open_price, close_price)
    lower_shadow = min(open_price, close_price) - low_price
    change_pct = ((close_price / open_price) - 1) * 100 if open_price else 0.0
    if lower_shadow > body * 2 and lower_shadow / total_range > 0.4:
        return "锤子线"
    if body / total_range < 0.12:
        return "十字星"
    if 0 < change_pct < 1.5:
        return "小阳线"
    if lower_shadow > upper_shadow and lower_shadow / total_range > 0.35:
        return "长下影线"
    return "其他"


def calculate_rsi(series: pd.Series, period: int) -> Optional[float]:
    if len(series) < period + 1:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean().iloc[-1]
    avg_loss = loss.rolling(period).mean().iloc[-1]
    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def calculate_macd(series: pd.Series) -> Dict[str, Optional[float]]:
    if len(series) < 35:
        return {"macd_hist": None, "macd_hist_prev": None}
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2
    return {"macd_hist": float(hist.iloc[-1]), "macd_hist_prev": float(hist.iloc[-2])}


def annualized_slope(series: pd.Series) -> float:
    cleaned = pd.to_numeric(series, errors="coerce").dropna()
    if len(cleaned) < 2:
        return 0.0
    x = np.arange(len(cleaned))
    slope, _ = np.polyfit(x, cleaned.values, 1)
    base = float(cleaned.iloc[0]) if cleaned.iloc[0] else 1.0
    return float(slope / base)


def latest_ma(series: pd.Series, window: int) -> Optional[float]:
    cleaned = pd.to_numeric(series, errors="coerce").dropna()
    if len(cleaned) < window:
        return None
    return float(cleaned.rolling(window=window).mean().iloc[-1])


def trading_days_between(index: pd.Index, start: Any, end: Any) -> int:
    values = list(index)
    try:
        start_pos = values.index(start)
        end_pos = values.index(end)
        return max(end_pos - start_pos, 0)
    except ValueError:
        return 0


def volume_ratio(series: pd.Series, window: int) -> float:
    if series.empty or len(series) < window:
        return 1.0
    current = float(series.iloc[-1])
    base = float(series.tail(window).mean())
    return current / base if base else 1.0


def market_contraction_ratio(series: pd.Series) -> float:
    if series.empty or len(series) < 20:
        return 1.0
    current = float(series.iloc[-1])
    peak = float(series.rolling(window=20).mean().max())
    return current / peak if peak else 1.0


def support_midpoint(frame: pd.DataFrame) -> Optional[float]:
    if frame is None or frame.empty or "low" not in frame or "high" not in frame:
        return None
    low = pd.to_numeric(frame["low"], errors="coerce").dropna()
    high = pd.to_numeric(frame["high"], errors="coerce").dropna()
    if low.empty or high.empty:
        return None
    return float((low.min() + high.max()) / 2)


def bullish_alignment(
    current_nav: Optional[float],
    ma5: Optional[float],
    ma20: Optional[float],
    ma60: Optional[float],
) -> bool:
    values = [current_nav, ma5, ma20, ma60]
    if any(value is None for value in values):
        return False
    return current_nav > ma5 > ma20 > ma60


def left_summary(stage: str) -> str:
    if stage == "左侧试仓":
        return "折扣、时间和市场衰竭条件基本满足，已进入左侧试仓区。"
    if stage == "左侧观察":
        return "已接近左侧关注区，但仍需等待更明确的企稳确认。"
    if stage == "左侧关注":
        return "部分左侧条件成立，位置开始值得关注，但性价比还不够完整。"
    return "左侧条件不足，折扣或市场衰竭信号未形成闭环。"


def right_summary(stage: str) -> str:
    if stage == "右侧确认":
        return "突破、回踩和市场趋势背景已形成较完整的右侧确认结构。"
    if stage == "右侧跟踪":
        return "趋势突破存在，但回踩幅度、时间窗或市场确认尚未完成。"
    return "右侧条件不足，当前仍不是高质量的突破回踩结构。"


def coalesce_price(value: Any, fallback: Any) -> float:
    if value is None or pd.isna(value):
        return float(fallback)
    return float(value)


def fmt_float(value: Optional[float]) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{value:.4f}" if abs(value) < 100 else f"{value:.2f}"
