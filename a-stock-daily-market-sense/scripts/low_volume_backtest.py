#!/usr/bin/env python3
"""
Backtest low-position / abnormal-volume trigger models.

Success label:
  In the 10 trading days after the trigger day, the highest close is at least
  10% above the trigger day's close.

This script intentionally evaluates deterministic rule sets only. It does not
write recommendations or change the production report model by itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from market_panel import (
    DEFAULT_DAILY_FIELDS,
    add_numeric_features,
    fetch_by_trade_dates,
    fetch_stock_basic,
    fetch_trade_dates,
    get_pro,
)


def future_max_next_n(series: pd.Series, horizon: int) -> pd.Series:
    reversed_series = series.iloc[::-1]
    return reversed_series.shift(1).rolling(horizon, min_periods=horizon).max().iloc[::-1]


def prepare_features(args: argparse.Namespace) -> pd.DataFrame:
    pro = get_pro()
    asof = datetime.strptime(args.asof, "%Y%m%d").strftime("%Y%m%d")
    target_date, trade_dates = fetch_trade_dates(pro, asof, args.lookback, 0, False)

    daily = fetch_by_trade_dates(
        pro,
        "daily",
        trade_dates,
        DEFAULT_DAILY_FIELDS,
        args.sleep,
        cache_enabled=not args.no_cache,
        refresh_cache=args.refresh_cache,
    )
    if daily.empty:
        raise RuntimeError("daily returned no data for the requested backtest window.")

    features = add_numeric_features(daily)
    features["trade_date"] = features["trade_date"].astype(str)
    features = features.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    grouped = features.groupby("ts_code", group_keys=False)

    features["history_days"] = grouped.cumcount() + 1
    features["prev_high_10d"] = grouped["high"].apply(
        lambda s: s.shift(1).rolling(10, min_periods=5).max()
    )
    features["close_to_high"] = features["close"] / features["high"].replace(0, pd.NA)
    features["future_max_close"] = grouped["close"].apply(
        lambda s: future_max_next_n(s, args.horizon)
    )
    features["future_max_return"] = (features["future_max_close"] / features["close"] - 1.0) * 100.0
    features["success"] = features["future_max_return"] >= args.target_return

    stock_basic = fetch_stock_basic(pro)
    if not stock_basic.empty:
        features = features.merge(stock_basic, on="ts_code", how="left")
    else:
        features["name"] = None
        features["market"] = None

    features["resolved_trade_date"] = target_date
    return features


def low_condition(df: pd.DataFrame, spec: Dict[str, Any]) -> pd.Series:
    mode = spec["low_mode"]
    a = df["close_position_120d"].fillna(1.0) <= spec.get("close_position_max", 0.20)
    b = (
        (df["drawdown_120_high"].fillna(0) <= -spec.get("drawdown_min_abs", 35.0))
        & (df["close_cv_10d"].fillna(1.0) <= spec.get("cv_max", 0.03))
    )

    if mode == "a":
        return a
    if mode == "b":
        return b
    if mode == "a_or_b":
        return a | b
    if mode == "broad":
        return (
            (df["close_position_120d"].fillna(1.0) <= spec.get("close_position_max", 0.35))
            & (df["drawdown_120_high"].fillna(0) <= -spec.get("drawdown_min_abs", 20.0))
        )
    if mode == "drawdown_only":
        return df["drawdown_120_high"].fillna(0) <= -spec.get("drawdown_min_abs", 30.0)
    raise ValueError(f"Unknown low_mode: {mode}")


def apply_spec(df: pd.DataFrame, spec: Dict[str, Any]) -> pd.DataFrame:
    mask = low_condition(df, spec)
    mask &= df["amount_ratio_15d"].fillna(0) >= spec.get("spike_volume_ratio_min", 3.0)
    mask &= df["pct_chg"].fillna(-999) >= spec.get("spike_pct_chg_min", 7.0)

    amount_min = spec.get("amount_min_100m")
    if amount_min is not None:
        mask &= df["amount"].fillna(0) >= amount_min * 100000

    history_min = spec.get("history_days_min")
    if history_min is not None:
        mask &= df["history_days"].fillna(0) >= history_min

    if spec.get("exclude_st"):
        names = df["name"].fillna("")
        mask &= ~names.str.startswith(("ST", "*ST", "退"))

    if spec.get("exclude_recent_listing_names"):
        names = df["name"].fillna("")
        mask &= ~names.str.startswith("C")

    if spec.get("require_prev_high_10d_breakout"):
        mask &= df["close"].fillna(0) >= df["prev_high_10d"].fillna(float("inf"))

    if spec.get("close_to_high_min") is not None:
        mask &= df["close_to_high"].fillna(0) >= spec["close_to_high_min"]

    if spec.get("ret20_max") is not None:
        mask &= df["ret_20d"].fillna(999) <= spec["ret20_max"]

    return df.loc[mask].copy()


def summarize_signals(signals: pd.DataFrame, spec: Dict[str, Any]) -> Dict[str, Any]:
    labeled = signals.dropna(subset=["future_max_return"]).copy()
    if labeled.empty:
        return {
            "model": spec["name"],
            "hypothesis": spec["hypothesis"],
            "signals": 0,
            "hit_rate": None,
        }

    hits = labeled["success"].astype(bool)
    return {
        "model": spec["name"],
        "hypothesis": spec["hypothesis"],
        "signals": int(len(labeled)),
        "hits": int(hits.sum()),
        "hit_rate": round(float(hits.mean() * 100), 2),
        "avg_future_max_return": round(float(labeled["future_max_return"].mean()), 2),
        "median_future_max_return": round(float(labeled["future_max_return"].median()), 2),
        "p25_future_max_return": round(float(labeled["future_max_return"].quantile(0.25)), 2),
        "avg_trigger_pct_chg": round(float(labeled["pct_chg"].mean()), 2),
        "median_amount_ratio_15d": round(float(labeled["amount_ratio_15d"].median()), 2),
        "median_amount_100m": round(float((labeled["amount"] / 100000).median()), 2),
        "first_signal_date": str(labeled["trade_date"].min()),
        "last_signal_date": str(labeled["trade_date"].max()),
        "rules": {
            key: value
            for key, value in spec.items()
            if key not in {"name", "hypothesis"}
        },
    }


def model_specs() -> List[Dict[str, Any]]:
    return [
        {
            "name": "baseline_a_or_b_3x7",
            "hypothesis": "Current skill logic: bottom-zone or deep-flat low position plus 3x volume and +7% spike.",
            "low_mode": "a_or_b",
            "close_position_max": 0.20,
            "drawdown_min_abs": 35.0,
            "cv_max": 0.03,
            "spike_volume_ratio_min": 3.0,
            "spike_pct_chg_min": 7.0,
        },
        {
            "name": "baseline_liquid_no_st",
            "hypothesis": "Baseline signal with liquidity and ST filters to reduce tiny/fragile names.",
            "low_mode": "a_or_b",
            "close_position_max": 0.20,
            "drawdown_min_abs": 35.0,
            "cv_max": 0.03,
            "spike_volume_ratio_min": 3.0,
            "spike_pct_chg_min": 7.0,
            "amount_min_100m": 1.0,
            "history_days_min": 60,
            "exclude_st": True,
            "exclude_recent_listing_names": True,
        },
        {
            "name": "bottom_zone_a_only",
            "hypothesis": "Historical bottom-zone stocks are cleaner than deep-drawdown flat stocks.",
            "low_mode": "a",
            "close_position_max": 0.20,
            "spike_volume_ratio_min": 3.0,
            "spike_pct_chg_min": 7.0,
            "history_days_min": 60,
        },
        {
            "name": "deep_flat_b_only",
            "hypothesis": "Deep drawdown plus 10-day price contraction captures base-building reversals.",
            "low_mode": "b",
            "drawdown_min_abs": 35.0,
            "cv_max": 0.03,
            "spike_volume_ratio_min": 3.0,
            "spike_pct_chg_min": 7.0,
            "history_days_min": 60,
        },
        {
            "name": "broad_anomaly_2_5x5",
            "hypothesis": "A broader abnormal-volume model may capture earlier starts before the +7% hard gate.",
            "low_mode": "broad",
            "close_position_max": 0.35,
            "drawdown_min_abs": 20.0,
            "spike_volume_ratio_min": 2.5,
            "spike_pct_chg_min": 5.0,
            "amount_min_100m": 1.0,
            "history_days_min": 60,
            "exclude_st": True,
        },
        {
            "name": "strong_spike_4x10",
            "hypothesis": "Only very strong first-day thrusts after low position have enough momentum for +10% follow-through.",
            "low_mode": "a_or_b",
            "close_position_max": 0.20,
            "drawdown_min_abs": 35.0,
            "cv_max": 0.03,
            "spike_volume_ratio_min": 4.0,
            "spike_pct_chg_min": 10.0,
            "history_days_min": 60,
        },
        {
            "name": "breakout_10d_confirmed",
            "hypothesis": "Low-position spikes that close above the prior 10-day high have cleaner supply absorption.",
            "low_mode": "a_or_b",
            "close_position_max": 0.20,
            "drawdown_min_abs": 35.0,
            "cv_max": 0.03,
            "spike_volume_ratio_min": 3.0,
            "spike_pct_chg_min": 7.0,
            "require_prev_high_10d_breakout": True,
            "history_days_min": 60,
        },
        {
            "name": "close_near_high",
            "hypothesis": "A low-position spike with strong close location avoids intraday fade signals.",
            "low_mode": "a_or_b",
            "close_position_max": 0.20,
            "drawdown_min_abs": 35.0,
            "cv_max": 0.03,
            "spike_volume_ratio_min": 3.0,
            "spike_pct_chg_min": 7.0,
            "close_to_high_min": 0.95,
            "history_days_min": 60,
        },
        {
            "name": "not_overextended_20d",
            "hypothesis": "Low-position spikes with limited prior 20-day run-up leave more room for follow-through.",
            "low_mode": "a_or_b",
            "close_position_max": 0.20,
            "drawdown_min_abs": 35.0,
            "cv_max": 0.03,
            "spike_volume_ratio_min": 3.0,
            "spike_pct_chg_min": 7.0,
            "ret20_max": 30.0,
            "history_days_min": 60,
        },
        {
            "name": "liquid_breakout_close_high",
            "hypothesis": "Combine liquidity, prior-high breakout, and strong close to prioritize institutional-quality thrusts.",
            "low_mode": "a_or_b",
            "close_position_max": 0.20,
            "drawdown_min_abs": 35.0,
            "cv_max": 0.03,
            "spike_volume_ratio_min": 3.0,
            "spike_pct_chg_min": 7.0,
            "amount_min_100m": 1.0,
            "require_prev_high_10d_breakout": True,
            "close_to_high_min": 0.95,
            "history_days_min": 60,
            "exclude_st": True,
            "exclude_recent_listing_names": True,
        },
        {
            "name": "broad_momentum_quality_15pct",
            "hypothesis": "Broader low zone, but require a very strong thrust day: +15%, 3x volume, 10-day breakout, and close near the day high.",
            "low_mode": "broad",
            "close_position_max": 0.35,
            "drawdown_min_abs": 20.0,
            "spike_volume_ratio_min": 3.0,
            "spike_pct_chg_min": 15.0,
            "amount_min_100m": 1.0,
            "require_prev_high_10d_breakout": True,
            "close_to_high_min": 0.95,
            "history_days_min": 60,
            "exclude_st": True,
            "exclude_recent_listing_names": True,
        },
        {
            "name": "deep_drawdown_thrust_45pct",
            "hypothesis": "Very deep drawdown stocks can produce sharp reflexive follow-through when the first thrust is strong enough.",
            "low_mode": "drawdown_only",
            "drawdown_min_abs": 45.0,
            "spike_volume_ratio_min": 2.5,
            "spike_pct_chg_min": 10.0,
            "amount_min_100m": 1.0,
            "history_days_min": 60,
            "exclude_st": True,
            "exclude_recent_listing_names": True,
        },
    ]


def backtest(args: argparse.Namespace) -> Dict[str, Any]:
    features = prepare_features(args)
    eligible = features.dropna(subset=["future_max_return"]).copy()

    results: List[Dict[str, Any]] = []
    examples: Dict[str, List[Dict[str, Any]]] = {}
    for spec in model_specs():
        signals = apply_spec(eligible, spec)
        summary = summarize_signals(signals, spec)
        results.append(summary)

        preview_cols = [
            "trade_date", "ts_code", "name", "pct_chg", "amount_ratio_15d",
            "amount", "close_position_120d", "drawdown_120_high", "future_max_return", "success",
        ]
        preview = signals.sort_values("trade_date", ascending=False).head(10)
        records = preview[preview_cols].copy() if not preview.empty else pd.DataFrame(columns=preview_cols)
        if not records.empty:
            records["amount_100m"] = (records["amount"] / 100000).round(2)
            records = records.drop(columns=["amount"])
        examples[spec["name"]] = records.where(pd.notnull(records), None).to_dict(orient="records")

    ranked = sorted(
        results,
        key=lambda r: (
            r.get("signals", 0) >= args.min_samples,
            r.get("hit_rate") if r.get("hit_rate") is not None else -1,
            r.get("signals", 0),
        ),
        reverse=True,
    )

    return {
        "metadata": {
            "asof": args.asof,
            "lookback_trade_days": args.lookback,
            "horizon_trade_days": args.horizon,
            "target_return_pct": args.target_return,
            "min_samples_for_ranking": args.min_samples,
            "resolved_trade_date": str(features["resolved_trade_date"].iloc[0]),
            "rows_loaded": int(len(features)),
            "labeled_rows": int(len(eligible)),
        },
        "label_definition": (
            f"success = max(close over next {args.horizon} trading days) "
            f">= trigger close * (1 + {args.target_return / 100:.2f})"
        ),
        "ranked_results": ranked,
        "recent_examples": examples,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backtest low-position volume anomaly rule sets.")
    parser.add_argument("--asof", default=datetime.now().strftime("%Y%m%d"), help="YYYYMMDD analysis end date.")
    parser.add_argument("--lookback", type=int, default=260, help="Trading days to load, including warmup and label horizon.")
    parser.add_argument("--horizon", type=int, default=10, help="Forward trading-day horizon for success label.")
    parser.add_argument("--target-return", type=float, default=10.0, help="Target future max close return in percent.")
    parser.add_argument("--min-samples", type=int, default=30, help="Minimum signals preferred for ranking.")
    parser.add_argument("--sleep", type=float, default=0.12, help="Sleep seconds between uncached Tushare calls.")
    parser.add_argument("--no-cache", action="store_true", help="Disable daily cache.")
    parser.add_argument("--refresh-cache", action="store_true", help="Force refetch daily cache.")
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    result = backtest(args)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
