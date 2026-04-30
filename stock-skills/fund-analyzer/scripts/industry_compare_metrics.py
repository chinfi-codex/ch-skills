#!/usr/bin/env python3
"""
Pure calculation helpers for industry fund comparison.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

import numpy as np
import pandas as pd


def annual_return(nav_series: pd.Series, trading_days: int) -> float:
    cleaned = pd.to_numeric(nav_series, errors="coerce").dropna()
    if len(cleaned) < 2:
        return np.nan
    periods = len(cleaned) - 1
    return float((cleaned.iloc[-1] / cleaned.iloc[0]) ** (trading_days / periods) - 1)


def max_drawdown(
    nav_series: pd.Series,
) -> Tuple[float, Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    cleaned = pd.to_numeric(nav_series, errors="coerce").dropna()
    if cleaned.empty:
        return np.nan, None, None
    if cleaned.index.has_duplicates:
        cleaned = cleaned[~cleaned.index.duplicated(keep="last")]
    rolling_max = cleaned.cummax()
    drawdown = (rolling_max - cleaned) / rolling_max
    trough_pos = int(np.argmax(drawdown.to_numpy()))
    max_dd = float(drawdown.iloc[trough_pos])
    trough_idx = cleaned.index[trough_pos]
    if pd.isna(trough_idx):
        return max_dd, None, None
    peak_slice = cleaned.iloc[: trough_pos + 1]
    peak_pos = int(np.argmax(peak_slice.to_numpy()))
    peak_idx = cleaned.index[peak_pos] if len(peak_slice) else None
    return max_dd, peak_idx, trough_idx


def drawdown_repair_days(
    nav_series: pd.Series,
    peak_idx: Optional[pd.Timestamp],
    trough_idx: Optional[pd.Timestamp],
) -> Optional[int]:
    if peak_idx is None or trough_idx is None:
        return None
    cleaned = pd.to_numeric(nav_series, errors="coerce").dropna()
    if cleaned.index.has_duplicates:
        cleaned = cleaned[~cleaned.index.duplicated(keep="last")]
    if cleaned.empty or peak_idx not in cleaned.index or trough_idx not in cleaned.index:
        return None
    peak_value = float(cleaned.loc[peak_idx])
    post_trough = cleaned.loc[trough_idx:]
    repaired = post_trough[post_trough >= peak_value]
    if repaired.empty:
        return None
    repair_idx = repaired.index[0]
    trough_pos = cleaned.index.get_loc(trough_idx)
    repair_pos = cleaned.index.get_loc(repair_idx)
    if isinstance(trough_pos, slice) or isinstance(repair_pos, slice):
        return None
    return int(repair_pos - trough_pos)


def downside_protection(aligned: pd.DataFrame) -> float:
    industry_curve = (1 + aligned["industry_ret"]).cumprod()
    rolling_peak = industry_curve.cummax()
    industry_drawdown = (rolling_peak - industry_curve) / rolling_peak
    trigger_days = industry_drawdown[industry_drawdown >= 0.10]
    if trigger_days.empty:
        return np.nan

    start = trigger_days.index[0]
    peak_loc = industry_curve.loc[:start].idxmax()
    window = aligned.loc[peak_loc:start]
    if len(window) < 2:
        return np.nan

    fund_drop = window["adjusted_nav"].iloc[-1] / window["adjusted_nav"].iloc[0] - 1
    industry_drop = window["industry_close"].iloc[-1] / window["industry_close"].iloc[0] - 1
    return float(industry_drop - fund_drop)


def fmt_pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "样本不足"
    return f"{value * 100:+.1f}%"


def fmt_num(value: Any) -> str:
    if value is None or pd.isna(value):
        return "样本不足"
    return f"{value:.2f}"


def fmt_days(value: Optional[int]) -> str:
    if value is None or pd.isna(value):
        return "未修复"
    return f"{int(value)}天"


def fmt_focus(value: Optional[float]) -> str:
    if value is None or pd.isna(value):
        return "样本不足"
    label = "高" if value > 0.7 else "中" if value > 0.5 else "低"
    return f"{value * 100:.0f}%({label})"


def fmt_percentile(value: Optional[float]) -> str:
    if value is None or pd.isna(value):
        return "样本不足"
    rank_pct = max(1, int(round((1 - value) * 100)))
    return f"前{rank_pct}%"


def fmt_aum_total(value: Optional[float]) -> str:
    if value is None or pd.isna(value):
        return "样本不足"
    return f"{value:.1f}亿"


def format_date(value: Any) -> str:
    if value is None or pd.isna(value):
        return "样本不足"
    return pd.to_datetime(value).strftime("%Y-%m-%d")
