#!/usr/bin/env python3
"""
Atomic market evidence pack builder for the a-stock-daily-market-sense skill.

The script fetches Tushare data and computes deterministic numeric features.
It intentionally does not name themes, write research reports, or produce
investment recommendations. The model using the skill performs interpretation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - dependency check
    raise RuntimeError("Missing dependency: install pandas before using this script.") from exc

try:
    import tushare as ts
except ImportError as exc:  # pragma: no cover - dependency check
    raise RuntimeError("Missing dependency: install tushare before using this script.") from exc


DEFAULT_DAILY_FIELDS = (
    "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
)
DEFAULT_BASIC_FIELDS = (
    "ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio,pe,pb,total_mv,circ_mv"
)
DEFAULT_STOCK_FIELDS = "ts_code,name,market,list_date"
DEFAULT_INDEX_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
SKILL_ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = SKILL_ROOT / "data" / "cache"
REFERENCE_ROOT = SKILL_ROOT / "reference"
DEFAULT_MARKET_HISTORY_CSV = REFERENCE_ROOT / "market_data.csv"

MARKET_TREND_INDEXES = {
    "shanghai": {"name": "上证指数", "ts_code": "000001.SH"},
    "chinext": {"name": "创业板指数", "ts_code": "399006.SZ"},
}


def get_tushare_token() -> str:
    """Read TUSHARE_TOKEN from the environment or cwd/.env."""
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token

    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(env_path):
        return ""

    with open(env_path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "TUSHARE_TOKEN":
                return value.strip().strip('"').strip("'")
    return ""


def get_pro():
    token = get_tushare_token()
    if not token:
        raise RuntimeError("Missing TUSHARE_TOKEN. Set it in the environment or cwd/.env.")
    return ts.pro_api(token)


def normalize_date(value: Optional[str]) -> str:
    """Normalize date input to YYYYMMDD."""
    if not value:
        return datetime.now().strftime("%Y%m%d")
    raw = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    raise ValueError(f"Unsupported date format: {value}. Use YYYYMMDD or YYYY-MM-DD.")


def ymd_to_dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d")


def dataframe_to_records(df: Optional[pd.DataFrame]) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    cleaned = df.copy()
    cleaned = cleaned.where(pd.notnull(cleaned), None)
    return cleaned.to_dict(orient="records")


def split_fields(fields: str) -> List[str]:
    return [field.strip() for field in fields.split(",") if field.strip()]


def cache_file(endpoint: str, trade_date: str) -> Path:
    return CACHE_ROOT / endpoint / f"{trade_date}.parquet"


def read_cached_frame(endpoint: str, trade_date: str, fields: str) -> Optional[pd.DataFrame]:
    path = cache_file(endpoint, trade_date)
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        print(f"[warn] failed to read cache {path}: {exc}", file=sys.stderr)
        return None

    missing = [field for field in split_fields(fields) if field not in df.columns]
    if missing:
        print(f"[warn] ignoring stale cache {path}, missing fields: {','.join(missing)}", file=sys.stderr)
        return None
    return df


def write_cached_frame(endpoint: str, trade_date: str, df: pd.DataFrame) -> None:
    path = cache_file(endpoint, trade_date)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
    except Exception as exc:
        print(f"[warn] failed to write cache {path}: {exc}", file=sys.stderr)


def safe_float(value: Any) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    return float(value)


def nullable_value(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    return value


def fetch_trade_dates(pro, asof: str, lookback: int, offset: int, allow_future: bool) -> Tuple[str, List[str]]:
    """Resolve the analysis trade date and lookback dates using Tushare trade_cal."""
    asof_dt = ymd_to_dt(asof)
    start = (asof_dt - timedelta(days=max(lookback * 3, 260))).strftime("%Y%m%d")
    today = datetime.now().strftime("%Y%m%d")
    end_dt = max(asof_dt, ymd_to_dt(today))
    end = (end_dt + timedelta(days=10)).strftime("%Y%m%d") if allow_future else today

    cal = pro.trade_cal(exchange="", start_date=start, end_date=end, fields="cal_date,is_open")
    if cal is None or cal.empty:
        raise RuntimeError("trade_cal returned no data.")

    open_dates = sorted(cal.loc[cal["is_open"] == 1, "cal_date"].astype(str).tolist())
    if not open_dates:
        raise RuntimeError("No open trading days found in trade_cal result.")

    anchor_candidates = [d for d in open_dates if d <= asof]
    if not anchor_candidates:
        raise RuntimeError(f"No trading day found on or before {asof}.")
    anchor = anchor_candidates[-1]
    anchor_index = open_dates.index(anchor)
    target_index = anchor_index + offset

    if offset > 0 and not allow_future:
        raise RuntimeError("Positive offset reads future data. Re-run with --allow-future only for post-hoc verification.")
    if target_index < 0 or target_index >= len(open_dates):
        raise RuntimeError(f"Offset {offset} from {anchor} is outside available trade calendar.")

    target = open_dates[target_index]
    window_start_index = max(0, target_index - lookback + 1)
    window = open_dates[window_start_index : target_index + 1]
    return target, window


def fetch_by_trade_dates(
    pro,
    endpoint: str,
    trade_dates: Iterable[str],
    fields: str,
    sleep_seconds: float,
    cache_enabled: bool = False,
    refresh_cache: bool = False,
) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    api = getattr(pro, endpoint)
    for trade_date in trade_dates:
        if cache_enabled and not refresh_cache:
            cached = read_cached_frame(endpoint, trade_date, fields)
            if cached is not None:
                frames.append(cached)
                continue

        try:
            df = api(trade_date=trade_date, fields=fields)
        except Exception as exc:
            print(f"[warn] {endpoint} failed for {trade_date}: {exc}", file=sys.stderr)
            continue
        if df is not None and not df.empty:
            if cache_enabled:
                write_cached_frame(endpoint, trade_date, df)
            frames.append(df)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates()


def fetch_stock_basic(pro) -> pd.DataFrame:
    try:
        return pro.stock_basic(exchange="", list_status="L", fields=DEFAULT_STOCK_FIELDS)
    except Exception as exc:
        print(f"[warn] stock_basic failed: {exc}", file=sys.stderr)
        return pd.DataFrame(columns=["ts_code", "name", "market", "list_date"])


def fetch_index_daily(pro, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    try:
        df = pro.index_daily(
            ts_code=index_code,
            start_date=start_date,
            end_date=end_date,
            fields=DEFAULT_INDEX_FIELDS,
        )
    except Exception as exc:
        print(f"[warn] index_daily failed for {index_code}: {exc}", file=sys.stderr)
        return pd.DataFrame()
    return df if df is not None else pd.DataFrame()


def fetch_limit_list(pro, trade_date: str) -> pd.DataFrame:
    try:
        df = pro.limit_list_d(trade_date=trade_date)
    except Exception as exc:
        print(f"[warn] limit_list_d failed for {trade_date}: {exc}", file=sys.stderr)
        return pd.DataFrame()
    return df if df is not None else pd.DataFrame()


def pct_return(series: pd.Series, periods: int) -> pd.Series:
    return (series / series.shift(periods) - 1.0) * 100.0


def add_numeric_features(daily: pd.DataFrame) -> pd.DataFrame:
    df = daily.copy()
    for column in ["open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["ts_code", "trade_date"])
    grouped = df.groupby("ts_code", group_keys=False)
    df["history_days"] = grouped.cumcount() + 1

    for period in (1, 3, 5, 10, 20):
        df[f"ret_{period}d"] = grouped["close"].apply(lambda s, p=period: pct_return(s, p))

    df["close_ma5"] = grouped["close"].apply(lambda s: s.rolling(5, min_periods=5).mean())
    df["prev_high_10d"] = grouped["high"].apply(lambda s: s.shift(1).rolling(10, min_periods=5).max())
    df["close_to_high"] = df["close"] / df["high"].replace(0, pd.NA)
    df["amount_ma20_prev"] = grouped["amount"].apply(lambda s: s.shift(1).rolling(20, min_periods=5).mean())
    df["amount_ratio_20d"] = df["amount"] / df["amount_ma20_prev"]
    # New: 15-day previous-mean ratio for the strict low-position spike rule (3x over the 10-15d baseline).
    df["amount_ma15_prev"] = grouped["amount"].apply(lambda s: s.shift(1).rolling(15, min_periods=10).mean())
    df["amount_ratio_15d"] = df["amount"] / df["amount_ma15_prev"]
    df["high_60d"] = grouped["high"].apply(lambda s: s.rolling(60, min_periods=20).max())
    df["high_120d"] = grouped["high"].apply(lambda s: s.rolling(120, min_periods=30).max())
    df["low_120d"] = grouped["low"].apply(lambda s: s.rolling(120, min_periods=30).min())
    df["drawdown_120_high"] = (df["close"] / df["high_120d"] - 1.0) * 100.0
    range_120 = df["high_120d"] - df["low_120d"]
    df["close_position_120d"] = (df["close"] - df["low_120d"]) / range_120.replace(0, pd.NA)
    df["sustained_volume_days_5"] = grouped["amount_ratio_20d"].apply(
        lambda s: s.gt(1.5).rolling(5, min_periods=1).sum()
    )
    # New: rolling 10-day coefficient of variation of close, used as the "走平/波动收敛" signal in low-position rule B.
    df["close_cv_10d"] = grouped["close"].apply(
        lambda s: s.rolling(10, min_periods=8).std() / s.rolling(10, min_periods=8).mean()
    )
    return df


def add_index_features(panel: pd.DataFrame, index_daily: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Optional[float]]]:
    trade_date = str(panel["trade_date"].iloc[0]) if "trade_date" in panel.columns and not panel.empty else ""
    index_summary = summarize_index(index_daily, trade_date)
    if index_daily is None or index_daily.empty:
        panel["rel_ret_5d"] = None
        panel["rel_ret_10d"] = None
        return panel, index_summary

    index_ret_5d = index_summary["index_ret_5d"]
    index_ret_10d = index_summary["index_ret_10d"]
    panel["rel_ret_5d"] = panel["ret_5d"] - index_ret_5d if index_ret_5d is not None else None
    panel["rel_ret_10d"] = panel["ret_10d"] - index_ret_10d if index_ret_10d is not None else None
    return panel, index_summary


def summarize_index(index_daily: pd.DataFrame, trade_date: str) -> Dict[str, Optional[float]]:
    summary: Dict[str, Optional[float]] = {
        "index_ret_1d": None,
        "index_ret_3d": None,
        "index_ret_5d": None,
        "index_ret_10d": None,
    }
    if index_daily is None or index_daily.empty or not trade_date:
        return summary

    idx = index_daily.copy()
    idx["trade_date"] = idx["trade_date"].astype(str)
    idx["close"] = pd.to_numeric(idx["close"], errors="coerce")
    idx = idx.sort_values("trade_date")
    for period in (1, 3, 5, 10):
        idx[f"index_ret_{period}d"] = pct_return(idx["close"], period)

    row = idx.loc[idx["trade_date"] <= trade_date].tail(1)
    if row.empty:
        return summary
    latest = row.iloc[0]
    for period in (1, 3, 5, 10):
        field = f"index_ret_{period}d"
        if pd.notna(latest[field]):
            summary[field] = round(float(latest[field]), 2)
    return summary


def merge_optional(panel: pd.DataFrame, other: pd.DataFrame, on: List[str]) -> pd.DataFrame:
    if other is None or other.empty:
        return panel
    return panel.merge(other, on=on, how="left")


def build_market_temperature(panel: pd.DataFrame, index_summary: Dict[str, Optional[float]]) -> Dict[str, Any]:
    total = int(len(panel))
    up = int((panel["pct_chg"] > 0).sum())
    down = int((panel["pct_chg"] < 0).sum())
    flat = total - up - down
    total_amount = float(panel["amount"].sum(skipna=True))
    up_amount = float(panel.loc[panel["pct_chg"] > 0, "amount"].sum(skipna=True))
    down_amount = float(panel.loc[panel["pct_chg"] < 0, "amount"].sum(skipna=True))
    top50_amount = float(panel.nlargest(min(50, total), "amount")["amount"].sum(skipna=True)) if total else 0.0

    return {
        "stock_count": total,
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "up_ratio": round(up / total, 4) if total else None,
        "median_pct_chg": round(float(panel["pct_chg"].median(skipna=True)), 2) if total else None,
        "up_gt_3_count": int((panel["pct_chg"] >= 3).sum()),
        "up_gt_5_count": int((panel["pct_chg"] >= 5).sum()),
        "down_lt_minus_3_count": int((panel["pct_chg"] <= -3).sum()),
        "down_lt_minus_5_count": int((panel["pct_chg"] <= -5).sum()),
        "limit_up_approx_count": int((panel["pct_chg"] >= 9.8).sum()),
        "limit_down_approx_count": int((panel["pct_chg"] <= -9.8).sum()),
        "total_amount": round(total_amount, 2),
        "total_amount_100m_yuan": round(total_amount / 100000, 2),
        "amount_unit": "thousand_yuan",
        "up_amount_ratio": round(up_amount / total_amount, 4) if total_amount else None,
        "down_amount_ratio": round(down_amount / total_amount, 4) if total_amount else None,
        "top50_amount_ratio": round(top50_amount / total_amount, 4) if total_amount else None,
        "index": index_summary,
    }


def build_limit_stats(limit_df: pd.DataFrame) -> Dict[str, Any]:
    if limit_df is None or limit_df.empty:
        return {"available": False}
    result: Dict[str, Any] = {"available": True, "row_count": int(len(limit_df))}
    for column in ("limit", "limit_type", "status", "open_times"):
        if column in limit_df.columns:
            counts = limit_df[column].fillna("NA").astype(str).value_counts().head(20)
            result[f"{column}_counts"] = counts.to_dict()
            if column == "limit":
                result["limit_up_count"] = int(counts.get("U", 0))
                result["limit_down_count"] = int(counts.get("D", 0))
                result["limit_open_or_broken_count"] = int(counts.get("Z", 0))
    return result


def compare_scalar(current: Any, previous: Any) -> Dict[str, Any]:
    current_float = safe_float(current)
    previous_float = safe_float(previous)
    change = None
    change_pct = None
    if current_float is not None and previous_float is not None:
        change = round(current_float - previous_float, 4)
        if previous_float != 0:
            change_pct = round(change / previous_float * 100.0, 2)
    return {
        "current": current,
        "previous": previous,
        "change": change,
        "change_pct": change_pct,
    }


def build_temperature_comparison(current: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
    fields = [
        "stock_count",
        "up_count",
        "down_count",
        "up_ratio",
        "median_pct_chg",
        "up_gt_3_count",
        "up_gt_5_count",
        "down_lt_minus_3_count",
        "down_lt_minus_5_count",
        "limit_up_approx_count",
        "limit_down_approx_count",
        "total_amount_100m_yuan",
        "up_amount_ratio",
        "down_amount_ratio",
        "top50_amount_ratio",
    ]
    return {field: compare_scalar(current.get(field), previous.get(field)) for field in fields}


def build_limit_comparison(current: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
    fields = ["row_count", "limit_up_count", "limit_down_count", "limit_open_or_broken_count"]
    return {field: compare_scalar(current.get(field), previous.get(field)) for field in fields}


def build_amount_concentration(features: pd.DataFrame, target_date: str, previous_trade_date: Optional[str]) -> Dict[str, Any]:
    """Summarize market-wide amount concentration without assigning themes."""
    if features is None or features.empty:
        return {}

    df = features.copy()
    df["trade_date"] = df["trade_date"].astype(str)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")

    def ratios_for_date(trade_date: str) -> Dict[str, Any]:
        day = df.loc[df["trade_date"] == trade_date].copy()
        total = float(day["amount"].sum(skipna=True))
        result: Dict[str, Any] = {
            "trade_date": trade_date,
            "total_amount_100m_yuan": round(total / 100000, 2) if total else None,
        }
        for n in (10, 20, 50, 100):
            top_amount = float(day.nlargest(min(n, len(day)), "amount")["amount"].sum(skipna=True)) if total else 0.0
            result[f"top{n}_amount_ratio"] = round(top_amount / total, 4) if total else None
        return result

    current = ratios_for_date(target_date)
    previous = ratios_for_date(previous_trade_date) if previous_trade_date else {}

    series: List[Dict[str, Any]] = []
    for trade_date in sorted(df["trade_date"].unique())[-10:]:
        series.append(ratios_for_date(trade_date))

    top50_values = [item.get("top50_amount_ratio") for item in series if item.get("top50_amount_ratio") is not None]
    top50_change_10d = None
    top50_up_days = None
    if len(top50_values) >= 2:
        top50_change_10d = round(top50_values[-1] - top50_values[0], 4)
        top50_up_days = sum(1 for prev, cur in zip(top50_values, top50_values[1:]) if cur > prev)

    trend = {
        "top50_change_over_series": top50_change_10d,
        "top50_up_days_in_series": top50_up_days,
        "series_length": len(series),
    }

    day = df.loc[df["trade_date"] == target_date].copy()
    top_amount_samples = day.nlargest(min(20, len(day)), "amount")
    top_cols = [
        "ts_code",
        "trade_date",
        "close",
        "pct_chg",
        "ret_3d",
        "ret_5d",
        "amount",
        "amount_ratio_20d",
    ]
    top_cols = [col for col in top_cols if col in top_amount_samples.columns]
    for col in top_cols:
        if col not in {"ts_code", "trade_date"}:
            top_amount_samples[col] = pd.to_numeric(top_amount_samples[col], errors="coerce").round(4)

    return {
        "current": current,
        "previous": previous,
        "change": {
            key: compare_scalar(current.get(key), previous.get(key))
            for key in (
                "top10_amount_ratio",
                "top20_amount_ratio",
                "top50_amount_ratio",
                "top100_amount_ratio",
                "total_amount_100m_yuan",
            )
            if previous
        },
        "recent_series": series,
        "trend": trend,
        "top_amount_samples": top_amount_samples[top_cols].astype(object).where(pd.notnull(top_amount_samples[top_cols]), None).to_dict(orient="records"),
        "interpretation_hints": [
            "Higher top10/top20/top50 ratios indicate more concentrated trading and potentially higher crowding.",
            "Rising top50 concentration over multiple sessions can indicate consensus formation and trend acceleration.",
            "Use top_amount_samples as evidence only; infer themes from company facts and price-volume behavior, not preset labels.",
        ],
    }


def round_optional(value: Any, digits: int = 2) -> Optional[float]:
    numeric = safe_float(value)
    if numeric is None:
        return None
    return round(numeric, digits)


def pct_change_optional(current: Any, base: Any) -> Optional[float]:
    current_float = safe_float(current)
    base_float = safe_float(base)
    if current_float is None or base_float in (None, 0):
        return None
    return round((current_float / base_float - 1.0) * 100.0, 2)


def parse_numeric_text_series(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str)
        .str.replace("%", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.strip(),
        errors="coerce",
    )


def classify_ma_alignment(close: Any, ma5: Any, ma20: Any, ma60: Any) -> Optional[str]:
    values = [safe_float(item) for item in (close, ma5, ma20, ma60)]
    if any(item is None for item in values):
        return None
    close_v, ma5_v, ma20_v, ma60_v = values
    if close_v > ma5_v > ma20_v > ma60_v:
        return "bullish_alignment"
    if close_v < ma5_v < ma20_v < ma60_v:
        return "bearish_alignment"
    if close_v >= ma20_v and ma20_v >= ma60_v:
        return "medium_term_positive"
    if close_v < ma20_v and ma20_v < ma60_v:
        return "medium_term_negative"
    return "mixed_alignment"


def classify_price_volume_state(ret_1d: Any, amount_ratio_20d: Any) -> Optional[str]:
    ret = safe_float(ret_1d)
    amount_ratio = safe_float(amount_ratio_20d)
    if ret is None or amount_ratio is None:
        return None
    if ret > 0 and amount_ratio >= 1.2:
        return "up_volume_expansion"
    if ret > 0 and amount_ratio <= 0.8:
        return "up_volume_contraction"
    if ret < 0 and amount_ratio >= 1.2:
        return "down_volume_expansion"
    if ret < 0 and amount_ratio <= 0.8:
        return "down_volume_contraction"
    return "neutral_volume"


def classify_index_trend_stage(close: Any, ma20: Any, ma60: Any, ret_20d: Any, ret_60d: Any) -> Optional[str]:
    close_v = safe_float(close)
    ma20_v = safe_float(ma20)
    ma60_v = safe_float(ma60)
    ret20_v = safe_float(ret_20d)
    ret60_v = safe_float(ret_60d)
    if any(item is None for item in (close_v, ma20_v, ma60_v, ret20_v, ret60_v)):
        return None
    if close_v > ma20_v > ma60_v and ret20_v > 0 and ret60_v > 0:
        return "uptrend"
    if close_v < ma20_v < ma60_v and ret20_v < 0:
        return "breakdown"
    if close_v >= ma60_v and close_v < ma20_v and ret20_v < 0:
        return "pullback"
    if close_v > ma20_v and ret20_v > 0 and ret60_v <= 0:
        return "breakdown_repair"
    if abs(ret20_v) <= 2.0:
        return "sideways"
    return "mixed"


def build_level(label: str, value: Any) -> Dict[str, Any]:
    return {"label": label, "value": round_optional(value, 2)}


def build_index_trend_summary(index_daily: pd.DataFrame, index_name: str, ts_code: str, target_date: str, trend_days: int) -> Dict[str, Any]:
    if index_daily is None or index_daily.empty:
        return {
            "available": False,
            "name": index_name,
            "ts_code": ts_code,
            "reason": "index_daily returned no data",
        }

    df = index_daily.copy()
    df["trade_date"] = df["trade_date"].astype(str)
    numeric_cols = ["open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"]
    for column in numeric_cols:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.loc[df["trade_date"] <= target_date].sort_values("trade_date")
    df = df.dropna(subset=["close"])
    if df.empty:
        return {
            "available": False,
            "name": index_name,
            "ts_code": ts_code,
            "reason": "no rows on or before target date",
        }

    df = df.tail(max(int(trend_days), 60))
    for period in (1, 5, 20, 60):
        df[f"ret_{period}d"] = pct_return(df["close"], period)
    for period in (5, 20, 60):
        df[f"ma{period}"] = df["close"].rolling(period, min_periods=max(3, period // 2)).mean()

    liquidity_col = "amount" if "amount" in df.columns and df["amount"].notna().any() else "vol"
    if liquidity_col in df.columns:
        df["liquidity_ma5_prev"] = df[liquidity_col].shift(1).rolling(5, min_periods=3).mean()
        df["liquidity_ma20_prev"] = df[liquidity_col].shift(1).rolling(20, min_periods=5).mean()
        df["liquidity_ratio_5d"] = df[liquidity_col] / df["liquidity_ma5_prev"]
        df["liquidity_ratio_20d"] = df[liquidity_col] / df["liquidity_ma20_prev"]
    else:
        df["liquidity_ratio_5d"] = None
        df["liquidity_ratio_20d"] = None

    df["high_20d"] = df["high"].rolling(20, min_periods=5).max() if "high" in df.columns else None
    df["low_20d"] = df["low"].rolling(20, min_periods=5).min() if "low" in df.columns else None
    df["high_60d"] = df["high"].rolling(60, min_periods=20).max() if "high" in df.columns else None
    df["low_60d"] = df["low"].rolling(60, min_periods=20).min() if "low" in df.columns else None

    latest = df.iloc[-1]
    ma5 = latest.get("ma5")
    ma20 = latest.get("ma20")
    ma60 = latest.get("ma60")
    close = latest.get("close")
    ret_1d = latest.get("pct_chg") if pd.notna(latest.get("pct_chg")) else latest.get("ret_1d")

    kline_cols = [
        col
        for col in ["trade_date", "open", "high", "low", "close", "pct_chg", "vol", "amount"]
        if col in df.columns
    ]
    kline_records = df[kline_cols].tail(int(trend_days)).copy()
    for column in kline_cols:
        if column != "trade_date":
            kline_records[column] = pd.to_numeric(kline_records[column], errors="coerce").round(4)

    return {
        "available": True,
        "name": index_name,
        "ts_code": ts_code,
        "trade_date": str(latest.get("trade_date")),
        "window_start": str(df["trade_date"].iloc[0]),
        "window_end": str(df["trade_date"].iloc[-1]),
        "records_loaded": int(len(df)),
        "kline_days": int(min(int(trend_days), len(kline_records))),
        "latest": {
            "close": round_optional(close, 2),
            "pct_chg": round_optional(ret_1d, 2),
            "amount": round_optional(latest.get("amount"), 4) if "amount" in df.columns else None,
            "amount_100m_yuan": round_optional(latest.get("amount") / 100000, 2) if "amount" in df.columns and pd.notna(latest.get("amount")) else None,
            "vol": round_optional(latest.get("vol"), 4) if "vol" in df.columns else None,
        },
        "returns": {
            "ret_1d": round_optional(ret_1d, 2),
            "ret_5d": round_optional(latest.get("ret_5d"), 2),
            "ret_20d": round_optional(latest.get("ret_20d"), 2),
            "ret_60d": round_optional(latest.get("ret_60d"), 2),
        },
        "moving_averages": {
            "ma5": round_optional(ma5, 2),
            "ma20": round_optional(ma20, 2),
            "ma60": round_optional(ma60, 2),
            "close_vs_ma5_pct": pct_change_optional(close, ma5),
            "close_vs_ma20_pct": pct_change_optional(close, ma20),
            "close_vs_ma60_pct": pct_change_optional(close, ma60),
            "ma_alignment_hint": classify_ma_alignment(close, ma5, ma20, ma60),
        },
        "volume_price": {
            "liquidity_field": liquidity_col if liquidity_col in df.columns else None,
            "liquidity_ratio_5d": round_optional(latest.get("liquidity_ratio_5d"), 2),
            "liquidity_ratio_20d": round_optional(latest.get("liquidity_ratio_20d"), 2),
            "price_volume_state_hint": classify_price_volume_state(ret_1d, latest.get("liquidity_ratio_20d")),
        },
        "trend_stage_hint": classify_index_trend_stage(
            close,
            ma20,
            ma60,
            latest.get("ret_20d"),
            latest.get("ret_60d"),
        ),
        "levels": {
            "high_20d": round_optional(latest.get("high_20d"), 2),
            "low_20d": round_optional(latest.get("low_20d"), 2),
            "high_60d": round_optional(latest.get("high_60d"), 2),
            "low_60d": round_optional(latest.get("low_60d"), 2),
            "support_candidates": [
                build_level("low_20d", latest.get("low_20d")),
                build_level("low_60d", latest.get("low_60d")),
                build_level("ma20", ma20),
                build_level("ma60", ma60),
            ],
            "resistance_candidates": [
                build_level("high_20d", latest.get("high_20d")),
                build_level("high_60d", latest.get("high_60d")),
            ],
        },
        "kline_records": kline_records.astype(object).where(pd.notnull(kline_records), None).to_dict(orient="records"),
    }


def count_consecutive_moves(series: pd.Series, direction: str) -> int:
    values = [safe_float(value) for value in series.dropna().tolist()]
    values = [value for value in values if value is not None]
    count = 0
    for index in range(len(values) - 1, 0, -1):
        if direction == "up" and values[index] > values[index - 1]:
            count += 1
        elif direction == "down" and values[index] < values[index - 1]:
            count += 1
        else:
            break
    return count


def classify_volume_temperature(amount_ratio_20d: Any) -> Optional[str]:
    ratio = safe_float(amount_ratio_20d)
    if ratio is None:
        return None
    if ratio >= 1.25:
        return "clear_expansion"
    if ratio >= 1.05:
        return "mild_expansion"
    if ratio <= 0.80:
        return "clear_contraction"
    if ratio <= 0.95:
        return "mild_contraction"
    return "stable_volume"


def classify_sentiment_temperature(activity: Any, limit_up_down_ratio: Any) -> Optional[str]:
    activity_value = safe_float(activity)
    if activity_value is not None:
        if activity_value < 20:
            return "cold"
        if activity_value < 40:
            return "weak"
        if activity_value <= 60:
            return "neutral"
        if activity_value <= 80:
            return "hot"
        return "overheated"

    ratio = safe_float(limit_up_down_ratio)
    if ratio is None:
        return None
    if ratio < 0.5:
        return "cold"
    if ratio < 1.0:
        return "weak"
    if ratio <= 2.0:
        return "neutral"
    if ratio <= 4.0:
        return "hot"
    return "overheated"


def classify_breadth_temperature(up_ratio: Any) -> Optional[str]:
    ratio = safe_float(up_ratio)
    if ratio is None:
        return None
    if ratio >= 0.65:
        return "broad_rise"
    if ratio >= 0.52:
        return "partial_repair"
    if ratio <= 0.35:
        return "broad_decline"
    if ratio <= 0.48:
        return "partial_weakness"
    return "split"


def build_sentiment_trend(target_date: str, trend_days: int) -> Dict[str, Any]:
    csv_path = DEFAULT_MARKET_HISTORY_CSV
    if not csv_path.exists():
        return {
            "available": False,
            "source": str(csv_path),
            "reason": "reference/market_data.csv not found",
        }

    try:
        raw = pd.read_csv(csv_path)
    except Exception as exc:
        return {
            "available": False,
            "source": str(csv_path),
            "reason": f"failed to read market_data.csv: {exc}",
        }

    if raw is None or raw.empty or "日期" not in raw.columns:
        return {
            "available": False,
            "source": str(csv_path),
            "reason": "market_data.csv is empty or missing 日期 column",
        }

    df = raw.copy()
    df["date"] = pd.to_datetime(df["日期"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["trade_date"] = df["date"].dt.strftime("%Y%m%d")
    df = df.loc[df["trade_date"] <= target_date].sort_values("trade_date")
    if df.empty:
        return {
            "available": False,
            "source": str(csv_path),
            "reason": "no sentiment rows on or before target date",
        }

    expected_columns = ["上涨", "下跌", "平盘", "涨停", "跌停", "活跃度", "成交额"]
    for column in expected_columns:
        if column in df.columns:
            df[column] = parse_numeric_text_series(df[column])

    df = df.tail(int(trend_days)).copy()
    flat = df["平盘"] if "平盘" in df.columns else 0
    breadth_total = df["上涨"] + df["下跌"] + flat
    df["up_ratio"] = df["上涨"] / breadth_total.replace(0, pd.NA)
    df["limit_up_down_ratio"] = df["涨停"] / df["跌停"].replace(0, pd.NA)
    if "成交额" in df.columns:
        df["amount_trillion_yuan"] = df["成交额"] / 1e9
        df["amount_ma5"] = df["成交额"].rolling(5, min_periods=3).mean()
        df["amount_ma20"] = df["成交额"].rolling(20, min_periods=5).mean()
        df["amount_ratio_5d"] = df["成交额"] / df["amount_ma5"].replace(0, pd.NA)
        df["amount_ratio_20d"] = df["成交额"] / df["amount_ma20"].replace(0, pd.NA)

    latest = df.iloc[-1]
    previous = df.iloc[-2] if len(df) >= 2 else None
    amount_ma5 = latest.get("amount_ma5") if "amount_ma5" in df.columns else None
    amount_ma20 = latest.get("amount_ma20") if "amount_ma20" in df.columns else None

    recent_cols = [
        col
        for col in [
            "trade_date",
            "上涨",
            "下跌",
            "涨停",
            "跌停",
            "活跃度",
            "成交额",
            "amount_trillion_yuan",
            "up_ratio",
            "limit_up_down_ratio",
        ]
        if col in df.columns
    ]
    recent = df[recent_cols].tail(min(20, len(df))).copy()
    for column in recent_cols:
        if column != "trade_date":
            recent[column] = pd.to_numeric(recent[column], errors="coerce").round(4)

    return {
        "available": True,
        "source": str(csv_path),
        "trade_date": str(latest.get("trade_date")),
        "matches_target_date": str(latest.get("trade_date")) == target_date,
        "window_start": str(df["trade_date"].iloc[0]),
        "window_end": str(df["trade_date"].iloc[-1]),
        "records_loaded": int(len(df)),
        "latest": {
            "up_count": round_optional(latest.get("上涨"), 0),
            "down_count": round_optional(latest.get("下跌"), 0),
            "limit_up_count": round_optional(latest.get("涨停"), 0),
            "limit_down_count": round_optional(latest.get("跌停"), 0),
            "activity": round_optional(latest.get("活跃度"), 2),
            "amount": round_optional(latest.get("成交额"), 4),
            "amount_trillion_yuan": round_optional(latest.get("amount_trillion_yuan"), 2),
            "up_ratio": round_optional(latest.get("up_ratio"), 4),
            "limit_up_down_ratio": round_optional(latest.get("limit_up_down_ratio"), 2),
        },
        "rolling": {
            "amount_ma5": round_optional(amount_ma5, 4),
            "amount_ma20": round_optional(amount_ma20, 4),
            "amount_ma5_trillion_yuan": round_optional(amount_ma5 / 1e9, 2) if amount_ma5 is not None and pd.notna(amount_ma5) else None,
            "amount_ma20_trillion_yuan": round_optional(amount_ma20 / 1e9, 2) if amount_ma20 is not None and pd.notna(amount_ma20) else None,
            "amount_ratio_5d": round_optional(latest.get("amount_ratio_5d"), 2) if "amount_ratio_5d" in df.columns else None,
            "amount_ratio_20d": round_optional(latest.get("amount_ratio_20d"), 2) if "amount_ratio_20d" in df.columns else None,
            "up_ratio_ma5": round_optional(df["up_ratio"].rolling(5, min_periods=3).mean().iloc[-1], 4),
            "activity_ma5": round_optional(df["活跃度"].rolling(5, min_periods=3).mean().iloc[-1], 2) if "活跃度" in df.columns else None,
            "limit_up_down_ratio_ma5": round_optional(df["limit_up_down_ratio"].rolling(5, min_periods=3).mean().iloc[-1], 2),
        },
        "changes": {
            "up_count_vs_previous": compare_scalar(latest.get("上涨"), previous.get("上涨") if previous is not None else None),
            "down_count_vs_previous": compare_scalar(latest.get("下跌"), previous.get("下跌") if previous is not None else None),
            "activity_vs_previous": compare_scalar(latest.get("活跃度"), previous.get("活跃度") if previous is not None else None),
            "amount_vs_previous": compare_scalar(latest.get("成交额"), previous.get("成交额") if previous is not None else None),
            "amount_vs_20d_avg_pct": pct_change_optional(latest.get("成交额"), amount_ma20),
            "up_ratio_improving_days": count_consecutive_moves(df["up_ratio"], "up"),
            "up_ratio_deteriorating_days": count_consecutive_moves(df["up_ratio"], "down"),
        },
        "temperature_hints": {
            "volume": classify_volume_temperature(latest.get("amount_ratio_20d") if "amount_ratio_20d" in df.columns else None),
            "sentiment": classify_sentiment_temperature(latest.get("活跃度"), latest.get("limit_up_down_ratio")),
            "breadth": classify_breadth_temperature(latest.get("up_ratio")),
        },
        "recent_series": recent.astype(object).where(pd.notnull(recent), None).to_dict(orient="records"),
    }


def build_market_trend(pro, target_date: str, trade_dates: List[str], trend_days: int) -> Dict[str, Any]:
    safe_trend_days = max(20, int(trend_days))
    start_date = trade_dates[0] if trade_dates else target_date
    indices: Dict[str, Any] = {}
    for key, config in MARKET_TREND_INDEXES.items():
        index_daily = fetch_index_daily(pro, config["ts_code"], start_date, target_date)
        indices[key] = build_index_trend_summary(
            index_daily=index_daily,
            index_name=config["name"],
            ts_code=config["ts_code"],
            target_date=target_date,
            trend_days=safe_trend_days,
        )

    return {
        "metadata": {
            "trend_days_requested": safe_trend_days,
            "index_start_date": start_date,
            "index_end_date": target_date,
            "sentiment_source": str(DEFAULT_MARKET_HISTORY_CSV),
            "included_indices": list(MARKET_TREND_INDEXES.keys()),
        },
        "indices": indices,
        "sentiment": build_sentiment_trend(target_date, safe_trend_days),
        "interpretation_hints": [
            "Use only shanghai and chinext index trend evidence in module 1.",
            "Index trend evidence is deterministic; the model should write the concise trend judgment.",
            "Sentiment uses reference/market_data.csv when available and only rows on or before the analysis date.",
            "Do not add financing, GEM PE, external assets, style indexes, or STAR Market index to module 1.",
        ],
    }


def candidate_columns() -> List[str]:
    return [
        "ts_code",
        "name",
        "market",
        "trade_date",
        "close",
        "pct_chg",
        "ret_3d",
        "ret_5d",
        "ret_10d",
        "ret_20d",
        "rel_ret_5d",
        "rel_ret_10d",
        "amount",
        "amount_ratio_20d",
        "sustained_volume_days_5",
        "turnover_rate",
        "volume_ratio",
        "total_mv",
        "drawdown_120_high",
        "close_position_120d",
    ]


def clean_candidates(df: pd.DataFrame, limit: int) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    cols = [c for c in candidate_columns() if c in df.columns]
    out = df[cols].head(limit).copy()
    numeric_cols = [c for c in out.columns if c not in {"ts_code", "name", "market", "trade_date"}]
    for col in numeric_cols:
        out[col] = pd.to_numeric(out[col], errors="coerce").round(4)
    out = out.astype(object).where(pd.notnull(out), None)
    return out.to_dict(orient="records")


def build_candidates(panel: pd.DataFrame, sample_limit: int) -> Dict[str, Any]:
    liquid = panel.loc[panel["amount"].fillna(0) > 0].copy()
    if liquid.empty:
        return {
            "strong_samples": [],
            "weak_samples": [],
            "low_position_volume_samples": [],
            "divergence_samples": {"up_against_index": [], "down_against_index": []},
        }

    numeric_inputs = [
        "pct_chg",
        "ret_3d",
        "ret_5d",
        "ret_10d",
        "amount",
        "amount_ratio_20d",
        "sustained_volume_days_5",
        "rel_ret_5d",
        "drawdown_120_high",
        "close_position_120d",
    ]
    for column in numeric_inputs:
        if column in liquid.columns:
            liquid[column] = pd.to_numeric(liquid[column], errors="coerce")

    liquid["strong_score"] = (
        liquid["pct_chg"].fillna(0)
        + liquid["ret_3d"].fillna(0) * 0.4
        + liquid["ret_5d"].fillna(0) * 0.4
        + liquid["amount_ratio_20d"].fillna(1).clip(upper=5) * 1.5
        + liquid["rel_ret_5d"].fillna(0) * 0.4
    )
    liquid["weak_score"] = (
        liquid["pct_chg"].fillna(0)
        + liquid["ret_3d"].fillna(0) * 0.4
        + liquid["ret_5d"].fillna(0) * 0.4
        + liquid["rel_ret_5d"].fillna(0) * 0.4
    )

    strong = liquid.sort_values(["strong_score", "amount"], ascending=[False, False])
    weak = liquid.sort_values(["weak_score", "amount"], ascending=[True, False])

    low_volume = liquid.loc[
        (liquid["drawdown_120_high"].fillna(0) <= -20)
        & (liquid["close_position_120d"].fillna(1) <= 0.35)
        & (liquid["amount_ratio_20d"].fillna(0) >= 1.8)
        & (liquid["ret_3d"].fillna(-999) > 0)
    ].sort_values(["sustained_volume_days_5", "amount_ratio_20d", "ret_5d"], ascending=[False, False, False])

    up_against = liquid.loc[
        (liquid["ret_5d"].fillna(-999) > 0)
        & (liquid["rel_ret_5d"].fillna(-999) >= 5)
        & (liquid["amount_ratio_20d"].fillna(0) >= 1.0)
    ].sort_values(["rel_ret_5d", "ret_5d", "amount"], ascending=[False, False, False])

    down_against = liquid.loc[
        (liquid["ret_5d"].fillna(999) < 0)
        & (liquid["rel_ret_5d"].fillna(999) <= -5)
    ].sort_values(["rel_ret_5d", "ret_5d", "amount"], ascending=[True, True, False])

    return {
        "strong_samples": clean_candidates(strong, sample_limit),
        "weak_samples": clean_candidates(weak, sample_limit),
        "low_position_volume_samples": clean_candidates(low_volume, sample_limit),
        "divergence_samples": {
            "up_against_index": clean_candidates(up_against, sample_limit),
            "down_against_index": clean_candidates(down_against, sample_limit),
        },
    }


def build_money_effect_samples(
    panel: pd.DataFrame,
    pct_chg_threshold: float,
    amount_threshold_100m_yuan: float,
    sample_limit: int,
) -> Dict[str, Any]:
    """
    Build the money-effect candidate pool for daily theme grouping.

    Hard filters (designed to capture the "today truly made money" cohort):
      - pct_chg >= pct_chg_threshold (default 7.0%)
      - amount    >= amount_threshold_100m_yuan in 100m yuan (default 2.0 == 2亿)

    Sort:
      - amount descending. Per the skill, money-effect leadership is judged
        primarily by trading amount, not by single-day pct_chg ranking.

    Output:
      - candidates: per-stock detailed records (same column set as other
                    candidate pools, so downstream rendering is consistent).
      - summary: aggregate statistics about this candidate pool.

    Theme grouping is intentionally NOT performed here. The model groups
    candidates by business facts at report-writing time.
    """
    if panel is None or panel.empty:
        return {
            "available": False,
            "filter_criteria": {
                "pct_chg_threshold": pct_chg_threshold,
                "amount_threshold_100m_yuan": amount_threshold_100m_yuan,
                "sample_limit": sample_limit,
                "sort_by": "amount_desc",
            },
            "candidates": [],
            "summary": {"candidate_count": 0},
        }

    # daily.amount unit is thousand yuan: 1亿元 = 100000 千元
    amount_threshold_thousand_yuan = amount_threshold_100m_yuan * 100000

    df = panel.copy()
    for column in ("pct_chg", "amount", "ret_3d", "ret_5d", "rel_ret_5d", "amount_ratio_20d"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    qualified = df.loc[
        (df["pct_chg"].fillna(-999) >= pct_chg_threshold)
        & (df["amount"].fillna(0) >= amount_threshold_thousand_yuan)
    ].copy()

    if qualified.empty:
        return {
            "available": True,
            "filter_criteria": {
                "pct_chg_threshold": pct_chg_threshold,
                "amount_threshold_100m_yuan": amount_threshold_100m_yuan,
                "sample_limit": sample_limit,
                "sort_by": "amount_desc",
            },
            "candidates": [],
            "summary": {
                "candidate_count": 0,
                "total_amount_100m_yuan": 0.0,
            },
        }

    qualified = qualified.sort_values("amount", ascending=False).head(sample_limit)
    total_amount_100m = float(qualified["amount"].sum() / 100000)

    def _safe_median(col: str) -> Optional[float]:
        if col not in qualified.columns:
            return None
        series = pd.to_numeric(qualified[col], errors="coerce")
        if series.dropna().empty:
            return None
        return round(float(series.median()), 2)

    summary = {
        "candidate_count": int(len(qualified)),
        "total_amount_100m_yuan": round(total_amount_100m, 2),
        "median_pct_chg": round(float(qualified["pct_chg"].median()), 2),
        "max_pct_chg": round(float(qualified["pct_chg"].max()), 2),
        "min_pct_chg": round(float(qualified["pct_chg"].min()), 2),
        "median_ret_3d": _safe_median("ret_3d"),
        "median_ret_5d": _safe_median("ret_5d"),
        "median_rel_ret_5d": _safe_median("rel_ret_5d"),
        "median_amount_ratio_20d": _safe_median("amount_ratio_20d"),
        "limit_up_approx_count": int((qualified["pct_chg"] >= 9.8).sum()),
    }

    return {
        "available": True,
        "filter_criteria": {
            "pct_chg_threshold": pct_chg_threshold,
            "amount_threshold_100m_yuan": amount_threshold_100m_yuan,
            "sample_limit": sample_limit,
            "sort_by": "amount_desc",
            "amount_unit_note": "amount is thousand yuan in daily; threshold converted internally.",
        },
        "candidates": clean_candidates(qualified, sample_limit),
        "summary": summary,
        "interpretation_hints": [
            "Group these candidates by business facts (端侧AI、光芯片、机器人零部件等); do NOT use SW/THS/EM industry labels as preset groups.",
            "A theme qualifies as a leading line when its group_amount_share is high (e.g., >= 30%) AND its 5d-median return / 5d-relative-return are positive AND continuous-volume-days median >= 2.",
            "If candidate_count is small (<10) or the top-amount group_share is low (<20%), today is more likely capital rotation than a confirmed leading line.",
        ],
    }


def build_volume_decline_samples(
    panel: pd.DataFrame,
    pct_chg_max: float,
    amount_ratio_min: float,
    amount_threshold_100m_yuan: float,
    sample_limit: int,
) -> Dict[str, Any]:
    """
    Build the volume-spike-decline (爆量下跌) candidate pool.

    Hard filters (the "money is actively fleeing" cohort):
      - pct_chg          <= pct_chg_max (default -3.0%)
      - amount_ratio_20d >= amount_ratio_min (default 2.0x)
      - amount           >= amount_threshold_100m_yuan (default 1.0 == 1亿)

    Sort:
      - decline_intensity = amount_ratio_20d * abs(pct_chg), descending.
        This surfaces stocks that combine large drops with abnormal volume,
        which is the canonical 爆量下跌 signal the skill cares about.
    """
    if panel is None or panel.empty:
        return {
            "available": False,
            "filter_criteria": {
                "pct_chg_max": pct_chg_max,
                "amount_ratio_min": amount_ratio_min,
                "amount_threshold_100m_yuan": amount_threshold_100m_yuan,
                "sample_limit": sample_limit,
            },
            "candidates": [],
            "summary": {"candidate_count": 0},
        }

    amount_threshold_thousand_yuan = amount_threshold_100m_yuan * 100000

    df = panel.copy()
    for column in ("pct_chg", "amount", "amount_ratio_20d", "ret_3d", "ret_5d", "rel_ret_5d", "drawdown_120_high"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    qualified = df.loc[
        (df["pct_chg"].fillna(999) <= pct_chg_max)
        & (df["amount_ratio_20d"].fillna(0) >= amount_ratio_min)
        & (df["amount"].fillna(0) >= amount_threshold_thousand_yuan)
    ].copy()

    if qualified.empty:
        return {
            "available": True,
            "filter_criteria": {
                "pct_chg_max": pct_chg_max,
                "amount_ratio_min": amount_ratio_min,
                "amount_threshold_100m_yuan": amount_threshold_100m_yuan,
                "sample_limit": sample_limit,
                "sort_by": "amount_ratio_20d * abs(pct_chg) desc",
            },
            "candidates": [],
            "summary": {"candidate_count": 0},
        }

    qualified["decline_intensity"] = (
        qualified["amount_ratio_20d"].fillna(0) * qualified["pct_chg"].fillna(0).abs()
    ).round(4)

    qualified = qualified.sort_values(
        ["decline_intensity", "amount_ratio_20d", "pct_chg"],
        ascending=[False, False, True],
    ).head(sample_limit)

    total_amount_100m = float(qualified["amount"].sum() / 100000)

    def _safe_median(col: str) -> Optional[float]:
        if col not in qualified.columns:
            return None
        series = pd.to_numeric(qualified[col], errors="coerce")
        if series.dropna().empty:
            return None
        return round(float(series.median()), 2)

    summary = {
        "candidate_count": int(len(qualified)),
        "total_amount_100m_yuan": round(total_amount_100m, 2),
        "median_pct_chg": round(float(qualified["pct_chg"].median()), 2),
        "min_pct_chg": round(float(qualified["pct_chg"].min()), 2),
        "median_amount_ratio_20d": round(float(qualified["amount_ratio_20d"].median()), 2),
        "max_amount_ratio_20d": round(float(qualified["amount_ratio_20d"].max()), 2),
        "median_ret_5d": _safe_median("ret_5d"),
        "median_drawdown_120_high": _safe_median("drawdown_120_high"),
        "limit_down_approx_count": int((qualified["pct_chg"] <= -9.8).sum()),
    }

    # Inject decline_intensity into the candidate dict so downstream rendering can use it.
    base_records = clean_candidates(qualified, sample_limit)
    intensity_lookup = qualified.set_index("ts_code")["decline_intensity"].to_dict()
    for record in base_records:
        record["decline_intensity"] = intensity_lookup.get(record.get("ts_code"))

    return {
        "available": True,
        "filter_criteria": {
            "pct_chg_max": pct_chg_max,
            "amount_ratio_min": amount_ratio_min,
            "amount_threshold_100m_yuan": amount_threshold_100m_yuan,
            "sample_limit": sample_limit,
            "sort_by": "amount_ratio_20d * abs(pct_chg) desc",
            "amount_unit_note": "amount is thousand yuan in daily; threshold converted internally.",
        },
        "candidates": base_records,
        "summary": summary,
        "interpretation_hints": [
            "These are 爆量下跌 candidates: large drop + abnormal volume + meaningful turnover.",
            "Group by risk type (前期高位抱团瓦解、ST出清、业绩雷、流动性杀跌、机构调仓), not by industry labels.",
            "Cross-check with drawdown_120_high to tell high-position breakdown from low-position washout.",
        ],
    }


def build_low_position_volume_anomaly_samples(
    features: pd.DataFrame,
    target_date: str,
    drawdown_min_abs: float,
    close_position_max: float,
    cv_max: float,
    spike_volume_ratio_min: float,
    spike_pct_chg_min: float,
    lookback_days: int,
    sustain_volume_ratio_min: float,
    quiet_volume_ratio_max: float,
    sample_limit: int,
) -> Dict[str, Any]:
    """
    Low-position volume anomaly samples — three categorized scenarios.

    "Low position" (either A or B is enough):
      A. Close position in the 120d range <= close_position_max (default 0.20),
         i.e. near the historical bottom of the monthly range.
      B. Drawdown from 120d high <= -drawdown_min_abs (default 35%) AND the most
         recent 10 close prices are flat enough: close_cv_10d <= cv_max (default 3%).

    "Volume spike" trigger day requires:
      - amount_ratio_15d >= spike_volume_ratio_min (default 3.0)
      - pct_chg          >= spike_pct_chg_min       (default 7.0)

    Three scenarios based on where the trigger day falls relative to D and what
    the volume / price did afterwards:

      starter      — trigger day == D (today is the spike day).
      sustain      — trigger day is within [D-lookback_days, D-1] AND every
                     post-trigger day has amount >= trigger amount * sustain_volume_ratio_min
                     AND close at D >= open of trigger day. Interpretation: 换手吃筹.
      quiet        — trigger day is within [D-lookback_days, D-1] AND median amount
                     of the most recent 3 days is <= trigger amount * quiet_volume_ratio_max
                     AND close at D >= 0.95 * close of trigger day. Interpretation: 缩量企稳.

    Stocks may match multiple scenarios; we tag each candidate with its primary
    scenario by priority: starter > sustain > quiet.
    """
    if features is None or features.empty:
        return {
            "available": False,
            "filter_criteria": {},
            "candidates": [],
            "summary": {"candidate_count": 0},
        }

    df = features.copy()
    df["trade_date"] = df["trade_date"].astype(str)
    for column in (
        "open", "high", "low", "close", "pct_chg", "amount",
        "amount_ratio_15d", "amount_ratio_20d",
        "drawdown_120_high", "close_position_120d", "close_cv_10d",
        "close_ma5", "prev_high_10d", "close_to_high", "history_days",
    ):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    # Resolve the trade dates around target.
    all_dates = sorted(df["trade_date"].unique())
    if target_date not in all_dates:
        return {
            "available": False,
            "filter_criteria": {},
            "candidates": [],
            "summary": {"candidate_count": 0},
        }
    target_idx = all_dates.index(target_date)
    lookback_start_idx = max(0, target_idx - lookback_days)
    window_dates = all_dates[lookback_start_idx : target_idx + 1]
    if not window_dates:
        return {
            "available": False,
            "filter_criteria": {},
            "candidates": [],
            "summary": {"candidate_count": 0},
        }

    window_df = df.loc[df["trade_date"].isin(window_dates)].copy()

    # Compute "low position" qualifier on each row inside the window:
    # A: close_position_120d <= close_position_max
    # B: drawdown_120_high <= -drawdown_min_abs AND close_cv_10d <= cv_max
    window_df["is_low_A"] = window_df["close_position_120d"].fillna(1.0) <= close_position_max
    window_df["is_low_B"] = (
        (window_df["drawdown_120_high"].fillna(0) <= -drawdown_min_abs)
        & (window_df["close_cv_10d"].fillna(1.0) <= cv_max)
    )
    window_df["is_low_position"] = window_df["is_low_A"] | window_df["is_low_B"]

    names = window_df["name"].fillna("") if "name" in window_df.columns else pd.Series("", index=window_df.index)
    is_special_or_new = names.str.startswith(("ST", "*ST", "退", "C"))
    is_mature = window_df["history_days"].fillna(0) >= 60
    has_1y_amount = window_df["amount"].fillna(0) >= 100000

    # Observation pool: current broad coverage rule.
    window_df["is_observation_trigger"] = (
        window_df["is_low_position"]
        & (window_df["amount_ratio_15d"].fillna(0) >= spike_volume_ratio_min)
        & (window_df["pct_chg"].fillna(-999) >= spike_pct_chg_min)
    )

    # High-quality pool A: deep drawdown + strong thrust.
    window_df["is_high_quality_A"] = (
        (window_df["drawdown_120_high"].fillna(0) <= -45.0)
        & (window_df["amount_ratio_15d"].fillna(0) >= 2.5)
        & (window_df["pct_chg"].fillna(-999) >= 10.0)
        & has_1y_amount
        & is_mature
        & ~is_special_or_new
    )

    # High-quality pool B: broader low zone + strong momentum quality.
    window_df["is_high_quality_B"] = (
        (window_df["close_position_120d"].fillna(1.0) <= 0.35)
        & (window_df["drawdown_120_high"].fillna(0) <= -20.0)
        & (window_df["amount_ratio_15d"].fillna(0) >= 3.0)
        & (window_df["pct_chg"].fillna(-999) >= 15.0)
        & has_1y_amount
        & is_mature
        & ~is_special_or_new
        & (window_df["close"].fillna(0) >= window_df["prev_high_10d"].fillna(float("inf")))
        & (window_df["close_to_high"].fillna(0) >= 0.95)
    )

    # Trigger day can enter through the observation pool or either high-quality pool.
    window_df["is_spike_trigger"] = (
        window_df["is_observation_trigger"]
        | window_df["is_high_quality_A"]
        | window_df["is_high_quality_B"]
    )

    # Build per-stock series indexed by trade_date.
    candidates: List[Dict[str, Any]] = []
    target_rows = window_df.loc[window_df["trade_date"] == target_date].set_index("ts_code")

    grouped = window_df.groupby("ts_code", group_keys=False)
    for ts_code, sub in grouped:
        sub = sub.sort_values("trade_date").reset_index(drop=True)
        if sub.empty or sub["is_spike_trigger"].sum() == 0:
            continue

        # Find the most recent trigger day inside the window.
        trigger_rows = sub.loc[sub["is_spike_trigger"]]
        if trigger_rows.empty:
            continue
        trigger_row = trigger_rows.iloc[-1]
        trigger_date = str(trigger_row["trade_date"])
        trigger_idx_in_window = sub.index[sub["trade_date"] == trigger_date].tolist()
        if not trigger_idx_in_window:
            continue
        t_idx = trigger_idx_in_window[-1]
        days_since_trigger = (len(sub) - 1) - t_idx  # 0 means trigger == today

        target_row = target_rows.loc[ts_code] if ts_code in target_rows.index else None
        if target_row is None:
            continue

        target_close = float(target_row.get("close") or 0)
        target_close_ma5 = target_row.get("close_ma5")
        if days_since_trigger > 0:
            if pd.isna(target_close_ma5) or target_close < float(target_close_ma5):
                continue

        scenario = None
        sustain_ratio = None
        quiet_ratio = None
        post_trigger_days = sub.iloc[t_idx + 1 :]

        if days_since_trigger == 0:
            scenario = "starter"
        else:
            trigger_amount = float(trigger_row.get("amount") or 0)
            trigger_open = float(trigger_row.get("open") or 0)
            trigger_close = float(trigger_row.get("close") or 0)

            # Sustain check: every post-trigger day's amount >= trigger * sustain_ratio_min,
            #                AND target close >= trigger open (price has not collapsed).
            if not post_trigger_days.empty and trigger_amount > 0:
                post_amounts = pd.to_numeric(post_trigger_days["amount"], errors="coerce").dropna()
                if not post_amounts.empty:
                    min_post_ratio = float(post_amounts.min() / trigger_amount)
                    sustain_ratio = round(min_post_ratio, 4)
                    if (
                        min_post_ratio >= sustain_volume_ratio_min
                        and target_close >= trigger_open
                    ):
                        scenario = "sustain"

            # Quiet check: median of last 3 days' amount <= trigger * quiet_ratio_max,
            #              AND target close >= 0.95 * trigger close (price has not broken down).
            if scenario is None and trigger_amount > 0 and trigger_close > 0:
                tail = pd.to_numeric(post_trigger_days["amount"].tail(3), errors="coerce").dropna()
                if not tail.empty:
                    median_post_ratio = float(tail.median() / trigger_amount)
                    quiet_ratio = round(median_post_ratio, 4)
                    if (
                        median_post_ratio <= quiet_volume_ratio_max
                        and target_close >= 0.95 * trigger_close
                    ):
                        scenario = "quiet"

        if scenario is None:
            # Triggered in window but post-trigger behavior matched neither sustain nor quiet:
            # treat as 分歧型 / undetermined; still surface for completeness.
            scenario = "undetermined"

        matched_models: List[str] = []
        if bool(trigger_row.get("is_high_quality_A")):
            matched_models.append("high_quality_A_deep_drawdown_thrust")
        if bool(trigger_row.get("is_high_quality_B")):
            matched_models.append("high_quality_B_broad_momentum_quality")
        quality_tier = "+".join(["A" if "high_quality_A_deep_drawdown_thrust" in matched_models else "",
                                 "B" if "high_quality_B_broad_momentum_quality" in matched_models else ""]).strip("+")
        if not quality_tier:
            quality_tier = "C"

        candidates.append({
            "ts_code": ts_code,
            "name": nullable_value(target_row.get("name")),
            "market": nullable_value(target_row.get("market")),
            "scenario": scenario,
            "quality_tier": quality_tier,
            "matched_models": matched_models,
            "observation_pool": bool(trigger_row.get("is_observation_trigger")),
            "trigger_date": trigger_date,
            "days_since_trigger": int(days_since_trigger),
            "trigger_pct_chg": round(float(trigger_row.get("pct_chg") or 0), 2),
            "trigger_amount_ratio_15d": round(float(trigger_row.get("amount_ratio_15d") or 0), 2),
            "trigger_amount_100m_yuan": round(float(trigger_row.get("amount") or 0) / 100000, 2),
            "trigger_drawdown_120_high": round(float(trigger_row.get("drawdown_120_high") or 0), 2),
            "trigger_close_position_120d": (
                round(float(trigger_row.get("close_position_120d")), 4)
                if pd.notna(trigger_row.get("close_position_120d"))
                else None
            ),
            "trigger_close_to_high": (
                round(float(trigger_row.get("close_to_high")), 4)
                if pd.notna(trigger_row.get("close_to_high"))
                else None
            ),
            "trigger_break_prev_high_10d": bool(
                pd.notna(trigger_row.get("prev_high_10d"))
                and float(trigger_row.get("close") or 0) >= float(trigger_row.get("prev_high_10d"))
            ),
            "history_days": (
                int(trigger_row.get("history_days"))
                if pd.notna(trigger_row.get("history_days"))
                else None
            ),
            "trigger_low_track": "+".join(
                track for track, matched in (
                    ("A", bool(trigger_row.get("is_low_A"))),
                    ("B", bool(trigger_row.get("is_low_B"))),
                )
                if matched
            ) or "HQ",
            "post_trigger_min_volume_ratio": sustain_ratio,
            "post_trigger_recent3_volume_ratio": quiet_ratio,
            "today_close": round(float(target_row.get("close") or 0), 2),
            "today_close_ma5": (
                round(float(target_close_ma5), 2)
                if pd.notna(target_close_ma5)
                else None
            ),
            "today_above_ma5": (
                bool(target_close >= float(target_close_ma5))
                if pd.notna(target_close_ma5)
                else None
            ),
            "today_pct_chg": round(float(target_row.get("pct_chg") or 0), 2),
            "today_amount_100m_yuan": round(float(target_row.get("amount") or 0) / 100000, 2),
            "today_drawdown_120_high": round(float(target_row.get("drawdown_120_high") or 0), 2),
            "today_close_position_120d": (
                round(float(target_row.get("close_position_120d")), 4)
                if pd.notna(target_row.get("close_position_120d"))
                else None
            ),
        })

    # Sort: high-quality pools first, then starter/sustain/quiet/undetermined.
    scenario_priority = {"starter": 0, "sustain": 1, "quiet": 2, "undetermined": 3}
    quality_priority = {"A+B": 0, "A": 0, "B": 1, "C": 2}
    candidates.sort(key=lambda r: (
        quality_priority.get(r["quality_tier"], 99),
        scenario_priority.get(r["scenario"], 99),
        -float(r.get("trigger_amount_ratio_15d") or 0),
        -float(r.get("trigger_pct_chg") or 0),
    ))
    candidates = candidates[:sample_limit]

    counts: Dict[str, int] = {"starter": 0, "sustain": 0, "quiet": 0, "undetermined": 0}
    quality_counts: Dict[str, int] = {"A": 0, "B": 0, "A+B": 0, "C": 0}
    model_counts: Dict[str, int] = {
        "high_quality_A_deep_drawdown_thrust": 0,
        "high_quality_B_broad_momentum_quality": 0,
    }
    for c in candidates:
        counts[c["scenario"]] = counts.get(c["scenario"], 0) + 1
        quality_counts[c["quality_tier"]] = quality_counts.get(c["quality_tier"], 0) + 1
        for model in c.get("matched_models") or []:
            model_counts[model] = model_counts.get(model, 0) + 1

    return {
        "available": True,
        "filter_criteria": {
            "low_position_rule_A": f"close_position_120d <= {close_position_max}",
            "low_position_rule_B": (
                f"drawdown_120_high <= -{drawdown_min_abs}% AND close_cv_10d <= {cv_max}"
            ),
            "spike_volume_ratio_min": spike_volume_ratio_min,
            "spike_pct_chg_min": spike_pct_chg_min,
            "high_quality_pool_A": (
                "drawdown_120_high <= -45% AND amount_ratio_15d >= 2.5 "
                "AND pct_chg >= 10% AND amount >= 1亿 AND history_days >= 60 "
                "AND not ST/*ST/退/C"
            ),
            "high_quality_pool_B": (
                "close_position_120d <= 0.35 AND drawdown_120_high <= -20% "
                "AND amount_ratio_15d >= 3.0 AND pct_chg >= 15% AND amount >= 1亿 "
                "AND close >= prev_high_10d AND close/high >= 0.95 "
                "AND history_days >= 60 AND not ST/*ST/退/C"
            ),
            "post_trigger_display_rule": "days_since_trigger == 0 OR today_close >= today_close_ma5",
            "lookback_days_for_trigger": lookback_days,
            "sustain_post_volume_ratio_min": sustain_volume_ratio_min,
            "quiet_post_volume_ratio_max": quiet_volume_ratio_max,
            "sample_limit": sample_limit,
            "sort_priority": "quality_tier A/A+B > B > C, then starter > sustain > quiet > undetermined",
        },
        "candidates": candidates,
        "summary": {
            "candidate_count": len(candidates),
            "starter_count": counts.get("starter", 0),
            "sustain_count": counts.get("sustain", 0),
            "quiet_count": counts.get("quiet", 0),
            "undetermined_count": counts.get("undetermined", 0),
            "quality_A_count": quality_counts.get("A", 0),
            "quality_B_count": quality_counts.get("B", 0),
            "quality_A_plus_B_count": quality_counts.get("A+B", 0),
            "quality_C_count": quality_counts.get("C", 0),
            "high_quality_A_count": model_counts.get("high_quality_A_deep_drawdown_thrust", 0),
            "high_quality_B_count": model_counts.get("high_quality_B_broad_momentum_quality", 0),
        },
        "interpretation_hints": [
            "starter (启动型): trigger day = D, no post-trigger validation yet.",
            "sustain (持续换手型): trigger day in past lookback window, every later day stayed at > sustain_ratio_min of the trigger amount, price holding above trigger open. 资金在换手吃筹.",
            "quiet (缩量企稳型): trigger day in past lookback window, recent 3 days median volume <= quiet_ratio_max of the trigger amount, price holding above 0.95x trigger close. 放量拉升后缩量横盘.",
            "undetermined: triggered but post-trigger behavior matches neither sustain nor quiet — surface for the model to inspect (often 分歧型 / 冲高回落).",
        ],
    }


def build_resilient_against_index_samples(
    panel: pd.DataFrame,
    index_summary: Dict[str, Optional[float]],
    index_5d_max: float,
    index_10d_max: float,
    rel_ret_5d_min: float,
    ret_5d_min: float,
    amount_threshold_100m_yuan: float,
    sample_limit: int,
) -> Dict[str, Any]:
    """
    "该弱不弱就是强" — find resilient stocks that hold up while the index is weak.

    Skips entirely when index environment is not weak: produces empty candidates
    with a reason field. The model should not list bearish-divergence stocks.

    Index weakness gate (either is enough):
      - index_ret_5d  <= index_5d_max  (default -2.0)
      - index_ret_10d <= index_10d_max (default -3.0)

    Candidate filters (all must pass):
      - rel_ret_5d >= rel_ret_5d_min  (default 5.0pct relative outperformance)
      - ret_5d     >= ret_5d_min      (default 0.0, absolute return positive)
      - amount     >= amount_threshold (default 1亿, ensures real participation)

    Sort: rel_ret_5d desc, then ret_5d desc.
    """
    index_ret_5d = index_summary.get("index_ret_5d") if index_summary else None
    index_ret_10d = index_summary.get("index_ret_10d") if index_summary else None

    weak_environment = False
    weakness_reasons: List[str] = []
    if index_ret_5d is not None and index_ret_5d <= index_5d_max:
        weak_environment = True
        weakness_reasons.append(f"index_ret_5d={index_ret_5d} <= {index_5d_max}")
    if index_ret_10d is not None and index_ret_10d <= index_10d_max:
        weak_environment = True
        weakness_reasons.append(f"index_ret_10d={index_ret_10d} <= {index_10d_max}")

    base = {
        "filter_criteria": {
            "index_5d_max": index_5d_max,
            "index_10d_max": index_10d_max,
            "rel_ret_5d_min": rel_ret_5d_min,
            "ret_5d_min": ret_5d_min,
            "amount_threshold_100m_yuan": amount_threshold_100m_yuan,
            "sample_limit": sample_limit,
            "philosophy": "该弱不弱就是强 — only list resilient/up against a weak index; do not list 逆势下跌.",
        },
        "index_environment": {
            "index_ret_5d": index_ret_5d,
            "index_ret_10d": index_ret_10d,
            "is_weak": weak_environment,
            "weakness_reasons": weakness_reasons,
        },
    }

    if not weak_environment:
        return {
            **base,
            "available": True,
            "candidates": [],
            "summary": {
                "candidate_count": 0,
                "skipped_reason": "Index is not weak by current thresholds; resilient-stock screen is intentionally not produced.",
            },
        }

    if panel is None or panel.empty:
        return {
            **base,
            "available": False,
            "candidates": [],
            "summary": {"candidate_count": 0},
        }

    amount_threshold_thousand_yuan = amount_threshold_100m_yuan * 100000
    df = panel.copy()
    for column in ("ret_5d", "rel_ret_5d", "amount", "pct_chg", "amount_ratio_20d", "drawdown_120_high"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    qualified = df.loc[
        (df["rel_ret_5d"].fillna(-999) >= rel_ret_5d_min)
        & (df["ret_5d"].fillna(-999) >= ret_5d_min)
        & (df["amount"].fillna(0) >= amount_threshold_thousand_yuan)
    ].copy()

    if qualified.empty:
        return {
            **base,
            "available": True,
            "candidates": [],
            "summary": {"candidate_count": 0},
        }

    qualified = qualified.sort_values(
        ["rel_ret_5d", "ret_5d", "amount"], ascending=[False, False, False]
    ).head(sample_limit)

    summary = {
        "candidate_count": int(len(qualified)),
        "median_rel_ret_5d": round(float(qualified["rel_ret_5d"].median()), 2),
        "max_rel_ret_5d": round(float(qualified["rel_ret_5d"].max()), 2),
        "median_ret_5d": round(float(qualified["ret_5d"].median()), 2),
        "median_amount_ratio_20d": (
            round(float(qualified["amount_ratio_20d"].median()), 2)
            if "amount_ratio_20d" in qualified.columns
            else None
        ),
    }

    return {
        **base,
        "available": True,
        "candidates": clean_candidates(qualified, sample_limit),
        "summary": summary,
        "interpretation_hints": [
            "Only output when index is weak; the philosophy is 该弱不弱就是强.",
            "Do NOT list 逆势下跌 stocks — those are absorbed by the volume-decline module.",
            "Group resilient candidates by business facts to find avoidance/clustering themes.",
        ],
    }


def build_panel(args: argparse.Namespace) -> Dict[str, Any]:
    pro = get_pro()
    asof = normalize_date(args.asof)
    target_date, trade_dates = fetch_trade_dates(pro, asof, args.lookback, args.offset, args.allow_future)
    previous_trade_date = trade_dates[-2] if len(trade_dates) >= 2 else None

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
        raise RuntimeError("daily returned no data for the requested window.")

    features = add_numeric_features(daily)
    panel = features.loc[features["trade_date"] == target_date].copy()
    if panel.empty:
        raise RuntimeError(f"No daily rows for resolved trade date {target_date}.")
    previous_panel = (
        features.loc[features["trade_date"] == previous_trade_date].copy()
        if previous_trade_date
        else pd.DataFrame()
    )

    basic = fetch_by_trade_dates(pro, "daily_basic", [target_date], DEFAULT_BASIC_FIELDS, args.sleep)
    panel = merge_optional(panel, basic, ["ts_code", "trade_date"])

    stock_basic = fetch_stock_basic(pro)
    if not stock_basic.empty:
        panel = panel.merge(stock_basic, on="ts_code", how="left")
        features = features.merge(stock_basic, on="ts_code", how="left")
    else:
        panel["name"] = None
        panel["market"] = None
        features["name"] = None
        features["market"] = None

    index_daily = fetch_index_daily(pro, args.index, trade_dates[0], target_date)
    panel, index_summary = add_index_features(panel, index_daily)
    previous_index_summary = summarize_index(index_daily, previous_trade_date or "")

    limit_df = pd.DataFrame()
    previous_limit_df = pd.DataFrame()
    if args.with_limit and not args.skip_limit:
        limit_df = fetch_limit_list(pro, target_date)
        if previous_trade_date:
            previous_limit_df = fetch_limit_list(pro, previous_trade_date)

    market_temperature = build_market_temperature(panel, index_summary)
    previous_market_temperature = (
        build_market_temperature(previous_panel, previous_index_summary)
        if previous_panel is not None and not previous_panel.empty
        else {}
    )
    market_trend = build_market_trend(pro, target_date, trade_dates, args.market_trend_days)
    limit_stats = build_limit_stats(limit_df)
    previous_limit_stats = build_limit_stats(previous_limit_df)
    candidates = build_candidates(panel, args.sample_limit)

    money_effect = build_money_effect_samples(
        panel,
        pct_chg_threshold=args.money_pct_threshold,
        amount_threshold_100m_yuan=args.money_amount_threshold,
        sample_limit=args.money_sample_limit,
    )
    volume_decline = build_volume_decline_samples(
        panel,
        pct_chg_max=args.decline_pct_max,
        amount_ratio_min=args.decline_volume_ratio,
        amount_threshold_100m_yuan=args.decline_amount_threshold,
        sample_limit=args.decline_sample_limit,
    )
    low_position_anomaly = build_low_position_volume_anomaly_samples(
        features,
        target_date=target_date,
        drawdown_min_abs=args.low_drawdown_min,
        close_position_max=args.low_close_position_max,
        cv_max=args.low_cv_max,
        spike_volume_ratio_min=args.low_spike_volume_ratio,
        spike_pct_chg_min=args.low_spike_pct_chg,
        lookback_days=args.low_lookback_days,
        sustain_volume_ratio_min=args.low_sustain_ratio,
        quiet_volume_ratio_max=args.low_quiet_ratio,
        sample_limit=args.low_sample_limit,
    )
    resilient = build_resilient_against_index_samples(
        panel,
        index_summary=index_summary,
        index_5d_max=args.resilient_index_5d_max,
        index_10d_max=args.resilient_index_10d_max,
        rel_ret_5d_min=args.resilient_rel_ret_min,
        ret_5d_min=args.resilient_abs_ret_min,
        amount_threshold_100m_yuan=args.resilient_amount_threshold,
        sample_limit=args.resilient_sample_limit,
    )

    return {
        "metadata": {
            "asof_input": asof,
            "resolved_trade_date": target_date,
            "previous_trade_date": previous_trade_date,
            "offset": args.offset,
            "lookback_trade_days_requested": args.lookback,
            "lookback_trade_days_loaded": len(trade_dates),
            "window_start": trade_dates[0] if trade_dates else None,
            "window_end": target_date,
            "index": args.index,
            "daily_rows": int(len(daily)),
            "panel_rows": int(len(panel)),
            "daily_cache_enabled": not bool(args.no_cache),
            "daily_cache_root": str(CACHE_ROOT / "daily"),
            "future_data_allowed": bool(args.allow_future),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "market_temperature": market_temperature,
        "market_temperature_previous": previous_market_temperature,
        "market_temperature_change": (
            build_temperature_comparison(market_temperature, previous_market_temperature)
            if previous_market_temperature
            else {}
        ),
        "market_trend": market_trend,
        "amount_concentration": build_amount_concentration(features, target_date, previous_trade_date),
        "limit_stats": limit_stats,
        "limit_stats_previous": previous_limit_stats,
        "limit_stats_change": (
            build_limit_comparison(limit_stats, previous_limit_stats)
            if limit_stats.get("available") and previous_limit_stats.get("available")
            else {}
        ),
        **candidates,
        "money_effect_samples": money_effect,
        "volume_decline_samples": volume_decline,
        "low_position_volume_anomaly_samples": low_position_anomaly,
        "resilient_against_index_samples": resilient,
        "notes": [
            "Theme grouping is intentionally not performed by this script.",
            "Do not use market/industry/concept labels as preset grouping rules; infer themes from evidence and business facts.",
            "Tushare daily amount is returned in thousand yuan; total_amount_100m_yuan is converted to 100 million yuan.",
            "limit_up_approx_count and limit_down_approx_count are daily pct_chg threshold approximations. Official limit_list_d stats are skipped by default to avoid rate limits; use --with-limit only when needed.",
            "market_trend is module 1 evidence only: shanghai index, chinext index, and sentiment trend from reference/market_data.csv.",
            "amount_concentration measures trading amount concentration only; it does not assign themes or sectors.",
            "money_effect_samples filters by pct_chg and amount thresholds and sorts by amount; it is the canonical pool for daily money-effect / leading-line analysis.",
            "volume_decline_samples filters by pct_chg, amount_ratio_20d and amount thresholds, sorted by decline_intensity = amount_ratio_20d * abs(pct_chg).",
            "low_position_volume_anomaly_samples uses the strict rules (3x volume, +7% spike, deep drawdown or bottom range) and classifies into starter/sustain/quiet/undetermined.",
            "resilient_against_index_samples only outputs in a weak-index environment (该弱不弱就是强); it never outputs bearish-divergence stocks.",
        ],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a Tushare A-share daily market evidence pack.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    panel = subparsers.add_parser("panel", help="Fetch daily-based market evidence pack.")
    panel.add_argument("--asof", default=None, help="Analysis date, YYYYMMDD or YYYY-MM-DD. Defaults to today.")
    panel.add_argument("--offset", type=int, default=0, help="Trading-day offset from asof. Positive offsets require --allow-future.")
    panel.add_argument("--allow-future", action="store_true", help="Allow positive trading-day offsets for post-hoc verification.")
    panel.add_argument("--lookback", type=int, default=120, help="Number of trading days to load.")
    panel.add_argument("--index", default="000300.SH", help="Benchmark index ts_code, default CSI 300.")
    panel.add_argument("--sample-limit", type=int, default=40, help="Max rows in each candidate sample.")
    panel.add_argument("--market-trend-days", type=int, default=90,
                       help="Module 1 market-trend window in trading/history rows (default 90).")
    panel.add_argument("--sleep", type=float, default=0.12, help="Sleep seconds between API calls.")
    panel.add_argument("--no-cache", action="store_true", help="Disable local daily parquet cache.")
    panel.add_argument("--refresh-cache", action="store_true", help="Force refetch daily data and overwrite local cache.")
    panel.add_argument("--with-limit", action="store_true", help="Fetch official limit_list_d stats. Disabled by default because the endpoint is rate limited.")
    panel.add_argument("--skip-limit", action="store_true", help=argparse.SUPPRESS)

    # Money-effect candidate pool: pct_chg + amount hard thresholds, sorted by amount.
    panel.add_argument("--money-pct-threshold", type=float, default=7.0,
                       help="Money-effect pool: minimum pct_chg in percent (default 7.0).")
    panel.add_argument("--money-amount-threshold", type=float, default=2.0,
                       help="Money-effect pool: minimum amount in 100m yuan (default 2.0 == 2亿).")
    panel.add_argument("--money-sample-limit", type=int, default=80,
                       help="Money-effect pool: max rows after sorting by amount desc (default 80).")

    # Volume-decline (爆量下跌) candidate pool.
    panel.add_argument("--decline-pct-max", type=float, default=-3.0,
                       help="Volume-decline pool: maximum pct_chg in percent, more negative is stricter (default -3.0).")
    panel.add_argument("--decline-volume-ratio", type=float, default=2.0,
                       help="Volume-decline pool: minimum amount_ratio_20d (default 2.0).")
    panel.add_argument("--decline-amount-threshold", type=float, default=1.0,
                       help="Volume-decline pool: minimum amount in 100m yuan (default 1.0 == 1亿).")
    panel.add_argument("--decline-sample-limit", type=int, default=60,
                       help="Volume-decline pool: max rows after sorting (default 60).")

    # Low-position volume anomaly (低位放量异动) — module 5.
    panel.add_argument("--low-drawdown-min", type=float, default=35.0,
                       help="Low-position pool rule B: minimum |drawdown_120_high| in percent (default 35.0).")
    panel.add_argument("--low-close-position-max", type=float, default=0.20,
                       help="Low-position pool rule A: maximum close_position_120d (default 0.20).")
    panel.add_argument("--low-cv-max", type=float, default=0.03,
                       help="Low-position pool rule B: maximum 10-day close coefficient of variation, the 走平 signal (default 0.03).")
    panel.add_argument("--low-spike-volume-ratio", type=float, default=3.0,
                       help="Low-position pool: minimum amount_ratio_15d for the spike trigger day (default 3.0).")
    panel.add_argument("--low-spike-pct-chg", type=float, default=7.0,
                       help="Low-position pool: minimum pct_chg in percent on the spike trigger day (default 7.0).")
    panel.add_argument("--low-lookback-days", type=int, default=5,
                       help="Low-position pool: how many trading days to look back for the trigger day (default 5).")
    panel.add_argument("--low-sustain-ratio", type=float, default=0.7,
                       help="Sustain scenario: every post-trigger day's amount must be >= sustain_ratio * trigger amount (default 0.7).")
    panel.add_argument("--low-quiet-ratio", type=float, default=0.5,
                       help="Quiet scenario: median of last 3 post-trigger days' amount must be <= quiet_ratio * trigger amount (default 0.5).")
    panel.add_argument("--low-sample-limit", type=int, default=60,
                       help="Low-position pool: max rows after sorting (default 60).")

    # Resilient-against-index (该弱不弱就是强) — module 6.
    panel.add_argument("--resilient-index-5d-max", type=float, default=-2.0,
                       help="Resilient pool: index 5d return must be <= this to count as weak environment (default -2.0).")
    panel.add_argument("--resilient-index-10d-max", type=float, default=-3.0,
                       help="Resilient pool: index 10d return must be <= this to count as weak environment (default -3.0).")
    panel.add_argument("--resilient-rel-ret-min", type=float, default=5.0,
                       help="Resilient pool: minimum 5d relative outperformance vs index in pct (default 5.0).")
    panel.add_argument("--resilient-abs-ret-min", type=float, default=0.0,
                       help="Resilient pool: minimum 5d absolute return in pct (default 0.0).")
    panel.add_argument("--resilient-amount-threshold", type=float, default=1.0,
                       help="Resilient pool: minimum amount in 100m yuan (default 1.0 == 1亿).")
    panel.add_argument("--resilient-sample-limit", type=int, default=40,
                       help="Resilient pool: max rows after sorting (default 40).")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.command == "panel":
        result = build_panel(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
