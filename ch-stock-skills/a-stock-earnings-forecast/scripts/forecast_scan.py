#!/usr/bin/env python3
"""Earnings-forecast (业绩预告) full-market scanner + growth decomposition.

Deterministic evidence builder. It does NOT decide which stocks are "excellent"
— that judgment (acceleration quality, base effects, one-off earnings, reason
sustainability) is the model's job. This script only:

  1. Scans forecasts for one reporting period by iterating forecast(ann_date=)
     over a trading-day window (the API rejects range/period-only queries), then
     keeps the latest forecast per stock.
  2. Computes the median net profit and the cumulative YoY from the forecast
     range (万元 basis).
  3. Pulls actual quarterly income to decompose the LATEST single quarter into
     单季度同比 (single-quarter YoY) and 环比 (QoQ), alongside 当年累计同比.
  4. Attaches the latest ACTUAL revenue trajectory (forecasts carry no revenue).

Units: forecast net_profit_* / last_parent_net are 万元; income figures are 元.
Everything is normalized to 元 internally and echoed in 亿元 for readability.

Usage:
    python3 scripts/forecast_scan.py --period 20260630
    python3 scripts/forecast_scan.py                       # latest quarter <= today
    python3 scripts/forecast_scan.py --period 20260630 --start-ann 20260601 --positive-only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from store import FORECAST_COLS, Store
from tushare_client import TushareProxy, get_tushare_pro

YI = 1e8            # 元 -> 亿元
WAN = 1e4          # 万元 -> 元

# 业绩预告类型 (Tushare `type`). Positive = 业绩向好方向; negative = 恶化方向.
POSITIVE_TYPES = {"预增", "略增", "续盈", "扭亏", "减亏"}
NEGATIVE_TYPES = {"预减", "略减", "首亏", "续亏", "增亏"}

INCOME_FIELDS = "ts_code,end_date,ann_date,report_type,n_income_attr_p,revenue"


# --------------------------------------------------------------------------- #
# Period arithmetic
# --------------------------------------------------------------------------- #
_QUARTER_MMDD = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}
_MMDD_QUARTER = {v: k for k, v in _QUARTER_MMDD.items()}


def quarter_of(period: str) -> Tuple[int, int]:
    """'YYYYMMDD' -> (year, quarter). Raises on non quarter-end dates."""
    year, mmdd = int(period[:4]), period[4:]
    if mmdd not in _MMDD_QUARTER:
        raise ValueError(f"{period} is not a quarter-end (must end 0331/0630/0930/1231).")
    return year, _MMDD_QUARTER[mmdd]


def period_end(year: int, quarter: int) -> str:
    return f"{year}{_QUARTER_MMDD[quarter]}"


def prev_cumulative_period(period: str) -> str:
    """Cumulative period immediately before `period` (Q1 -> prior-year annual)."""
    year, q = quarter_of(period)
    return period_end(year - 1, 4) if q == 1 else period_end(year, q - 1)


def prev_year_period(period: str) -> str:
    year, q = quarter_of(period)
    return period_end(year - 1, q)


def latest_quarter_end(today: dt.date) -> str:
    """Most recent quarter-end on or before `today`."""
    y = today.year
    candidates = [f"{y}0331", f"{y}0630", f"{y}0930", f"{y}1231", f"{y-1}1231"]
    ymd = today.strftime("%Y%m%d")
    for c in sorted(candidates, reverse=True):
        if c <= ymd:
            return c
    return f"{y-1}1231"


def period_label(period: str) -> str:
    year, q = quarter_of(period)
    return f"{year}Q{q}" if q != 2 else f"{year}H1"


# --------------------------------------------------------------------------- #
# Cumulative-actuals cache (per stock) with forecast override for the period
# --------------------------------------------------------------------------- #
class CumCache:
    """Cumulative 归母净利/营收 by period-end for ONE stock.

    Actuals come from `by_period` (period-end -> {n_income_attr_p, revenue},
    already filtered to report_type=1 latest restatement; None values allowed and
    ignored). The forecast period's net profit is overridden with the forecast
    median (revenue stays None — not forecast).
    """

    def __init__(self, by_period: Optional[Dict[str, Dict[str, Any]]] = None):
        self._np: Dict[str, float] = {}
        self._rev: Dict[str, float] = {}
        for end, vals in (by_period or {}).items():
            npv = vals.get("n_income_attr_p")
            rev = vals.get("revenue")
            if npv is not None:
                self._np[str(end)] = float(npv)
            if rev is not None:
                self._rev[str(end)] = float(rev)

    def set_forecast(self, period: str, np_yuan: float) -> None:
        self._np[period] = np_yuan

    def np(self, period: str) -> Optional[float]:
        return self._np.get(period)

    def rev(self, period: str) -> Optional[float]:
        return self._rev.get(period)

    def has_actual_np(self, period: str) -> bool:
        return period in self._np

    def single_np(self, period: str) -> Optional[float]:
        """Single-quarter 归母净利 = cum(period) - cum(prev cumulative period)."""
        _, q = quarter_of(period)
        cur = self._np.get(period)
        if cur is None:
            return None
        if q == 1:
            return cur  # Q1 cumulative == Q1 single quarter
        prev = self._np.get(prev_cumulative_period(period))
        return None if prev is None else cur - prev

    def single_rev(self, period: str) -> Optional[float]:
        _, q = quarter_of(period)
        cur = self._rev.get(period)
        if cur is None:
            return None
        if q == 1:
            return cur
        prev = self._rev.get(prev_cumulative_period(period))
        return None if prev is None else cur - prev


def safe_growth(cur: Optional[float], base: Optional[float]) -> Tuple[Optional[float], str]:
    """YoY/QoQ % with honest notes. Never returns a misleading ratio off a
    non-positive base (turnaround/loss cases must be read qualitatively)."""
    if cur is None:
        return None, "cur_missing"
    if base is None:
        return None, "base_missing"
    if base <= 0:
        return None, "base_nonpositive"
    return round((cur / base - 1.0) * 100.0, 2), "ok"


def to_yi(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x / YI, 4)


# --------------------------------------------------------------------------- #
# Forecast scan
# --------------------------------------------------------------------------- #
def trading_days(pro: TushareProxy, start: str, end: str) -> List[str]:
    cal = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
    if cal is None or cal.empty:
        return []
    return sorted(str(d) for d in cal["cal_date"].tolist())


def _clean(v: Any) -> Any:
    return None if (v is None or (isinstance(v, float) and pd.isna(v))) else v


def _row_to_forecast_dict(row: pd.Series) -> Dict[str, Any]:
    return {c: _clean(row.get(c)) for c in FORECAST_COLS}


def _dedupe_latest(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the latest forecast per ts_code (max ann_date, prefer update_flag=1)."""
    best: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        code = r["ts_code"]
        cur = best.get(code)
        key = (str(r.get("ann_date") or ""), 1 if str(r.get("update_flag") or "") == "1" else 0)
        if cur is None or key >= (str(cur.get("ann_date") or ""), 1 if str(cur.get("update_flag") or "") == "1" else 0):
            best[code] = r
    return list(best.values())


def _fetch_forecast_day(pro: TushareProxy, day: str, period: str) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
    try:
        df = pro.forecast(ann_date=day)
    except Exception as exc:  # noqa: BLE001
        return day, [], str(exc)
    rows = df[df["end_date"] == period] if (df is not None and not df.empty) else pd.DataFrame()
    return day, [_row_to_forecast_dict(r) for _, r in rows.iterrows()], None


def scan_forecasts_incremental(pro: TushareProxy, store: Store, period: str, start_ann: str,
                               end_ann: str, refetch_days: int, rebuild: bool, workers: int,
                               notes: List[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch only announcement-days not yet scanned (plus a small re-scan overlap
    for late/revised filings), persist them, then return the full accumulated set
    for the period from cache. Announcement-day calls run in a bounded thread pool."""
    days = trading_days(pro, start_ann, end_ann)
    if not days:
        notes.append(f"交易日历为空({start_ann}~{end_ann})，改用逐日历日回退。")
        d0 = dt.datetime.strptime(start_ann, "%Y%m%d").date()
        d1 = dt.datetime.strptime(end_ann, "%Y%m%d").date()
        days = [(d0 + dt.timedelta(n)).strftime("%Y%m%d") for n in range((d1 - d0).days + 1)]

    logged = set() if rebuild else store.logged_ann_dates(period)
    recent = set(days[-refetch_days:]) if refetch_days > 0 else set()
    to_fetch = [d for d in days if d not in logged or d in recent]

    new_rows: List[Dict[str, Any]] = []
    day_counts: Dict[str, int] = {}
    failed = 0
    if to_fetch:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(to_fetch)))) as ex:
            for day, rows, err in ex.map(lambda d: _fetch_forecast_day(pro, d, period), to_fetch):
                if err is not None:
                    failed += 1
                    if failed <= 3:
                        notes.append(f"forecast(ann_date={day}) 失败: {err[:80]}")
                    continue
                day_counts[day] = len(rows)
                new_rows.extend(rows)
    if failed:
        notes.append(f"共有 {failed} 个公告日取数失败(已跳过)。")

    store.upsert_forecasts(new_rows)
    store.record_fetch_days(period, day_counts)

    if store.available:
        full = store.load_forecasts(period)
    else:
        full = _dedupe_latest(new_rows)

    stats = {
        "ann_days_in_window": len(days),
        "ann_days_fetched": len(to_fetch),
        "ann_days_skipped_cached": len(days) - len(to_fetch),
        "forecast_fetch_failed": failed,
        "new_forecast_rows": len(new_rows),
        "forecasts_total": len(full),
        "cache": "on" if store.available else f"off ({store.reason})",
    }
    return full, stats


def _median(lo: Any, hi: Any) -> Optional[float]:
    vals = [float(v) for v in (lo, hi) if pd.notna(v)]
    return sum(vals) / len(vals) if vals else None


def build_stock_record(row: pd.Series, cache: CumCache, period: str,
                       basic: Dict[str, Dict[str, str]], min_pchange: float) -> Dict[str, Any]:
    ts_code = str(row["ts_code"])
    ftype = str(row["type"]) if pd.notna(row.get("type")) else ""

    # --- forecast net profit (万元 -> 元) ---
    np_min = None if pd.isna(row.get("net_profit_min")) else float(row["net_profit_min"]) * WAN
    np_max = None if pd.isna(row.get("net_profit_max")) else float(row["net_profit_max"]) * WAN
    np_med = _median(np_min, np_max)
    last_parent = None if pd.isna(row.get("last_parent_net")) else float(row["last_parent_net"]) * WAN

    if np_med is not None:
        cache.set_forecast(period, np_med)

    # --- 当年累计同比: prefer disclosed p_change midpoint, else derive ---
    pchg = _median(row.get("p_change_min"), row.get("p_change_max"))
    if pchg is not None:
        pchg = round(pchg, 2)
    cum_source = "p_change"
    if pchg is None and np_med is not None and last_parent is not None and last_parent > 0:
        pchg = round((np_med / last_parent - 1.0) * 100.0, 2)
        cum_source = "derived"
    elif pchg is None:
        cum_source = "na"

    # --- single-quarter decomposition on 归母净利 ---
    py = prev_year_period(period)
    single_cur = cache.single_np(period)
    single_prev = cache.single_np(py)
    # single value of the previous quarter (for QoQ). For Q2 that's Q1 (= its cumulative).
    single_prev_q = cache.single_np(prev_cumulative_period(period))
    sq_yoy, sq_note = safe_growth(single_cur, single_prev)
    qoq, qoq_note = safe_growth(single_cur, single_prev_q)

    # Cross-check the company's own prior-year base (last_parent_net) against the
    # income statement's prior-year cumulative. A divergence flags a restatement,
    # meaning cum-YoY (company base) and single-Q-YoY (income base) sit on
    # slightly different footings.
    base_consistency = "na"
    income_prev = cache.np(py)
    if last_parent is not None and income_prev is not None and abs(income_prev) > 1e4:
        base_consistency = "diverge" if abs(last_parent - income_prev) / abs(income_prev) > 0.05 else "ok"

    # --- trailing ACTUAL revenue (forecasts carry no revenue) ---
    rev_block: Optional[Dict[str, Any]] = None
    rev_period = prev_cumulative_period(period)  # latest fully-reported cumulative period
    rev_cum = cache.rev(rev_period)
    if rev_cum is not None:
        rev_py = prev_year_period(rev_period)
        rev_prevq = prev_cumulative_period(rev_period)
        rc_yoy, _ = safe_growth(rev_cum, cache.rev(rev_py))
        rs_cur = cache.single_rev(rev_period)
        rs_yoy, _ = safe_growth(rs_cur, cache.single_rev(rev_py))
        rs_qoq, _ = safe_growth(
            rs_cur,
            cache.single_rev(rev_prevq) if quarter_of(rev_prevq)[1] != quarter_of(rev_period)[1] else None,
        )
        rev_block = {
            "period": rev_period,
            "period_label": period_label(rev_period),
            "note": "最近实际报告期(actual, 非预告)——业绩预告不含营收字段",
            "cum_yi": to_yi(rev_cum),
            "cum_yoy_pct": rc_yoy,
            "single_q_yi": to_yi(rs_cur),
            "single_q_yoy_pct": rs_yoy,
            "qoq_pct": rs_qoq,
        }

    info = basic.get(ts_code, {})
    # Mechanical threshold hits only — NOT a verdict. "Excellent" is the model's call.
    turnaround = ftype in {"扭亏", "减亏"} or (last_parent is not None and last_parent <= 0)
    positive_type = ftype in POSITIVE_TYPES
    accelerating = sq_yoy is not None and pchg is not None and sq_yoy > pchg
    cum_yoy_ge_min = pchg is not None and pchg >= min_pchange
    prefilter_pass = positive_type and (pchg is None or pchg >= min_pchange)

    return {
        "ts_code": ts_code,
        "name": info.get("name", ""),
        "industry": info.get("industry", ""),
        "area": info.get("area", ""),
        "market": info.get("market", ""),
        "type": ftype,
        "ann_date": str(row.get("ann_date") or ""),
        "first_ann_date": str(row.get("first_ann_date", "")) if pd.notna(row.get("first_ann_date")) else "",
        "update_flag": str(row.get("update_flag", "")) if pd.notna(row.get("update_flag")) else "",
        "summary": str(row.get("summary", "")) if pd.notna(row.get("summary")) else "",
        "change_reason": str(row.get("change_reason", "")) if pd.notna(row.get("change_reason")) else "",
        "net_profit": {
            "min_yi": to_yi(np_min),
            "max_yi": to_yi(np_max),
            "median_yi": to_yi(np_med),
            "last_parent_yi": to_yi(last_parent),
        },
        "profit_growth": {
            "latest_quarter": f"Q{quarter_of(period)[1]}",
            "cum_yoy_pct": pchg,
            "cum_yoy_source": cum_source,
            "single_q_yoy_pct": sq_yoy,
            "single_q_note": sq_note,
            "qoq_pct": qoq,
            "qoq_note": qoq_note,
            "single_q_cur_yi": to_yi(single_cur),
            "single_q_prev_yi": to_yi(single_prev),
            "base_consistency": base_consistency,
        },
        "revenue_trailing": rev_block,
        "flags": {
            "positive_type": positive_type,
            "negative_type": ftype in NEGATIVE_TYPES,
            "turnaround": turnaround,
            "accelerating": bool(accelerating),
            "qoq_positive": qoq is not None and qoq > 0,
            "cum_yoy_ge_min": bool(cum_yoy_ge_min),
            "prefilter_pass": bool(prefilter_pass),
        },
    }


def fetch_all_basic(pro: TushareProxy) -> Dict[str, Dict[str, str]]:
    df = pro.stock_basic(exchange="", list_status="L",
                         fields="ts_code,name,industry,area,market")
    out: Dict[str, Dict[str, str]] = {}
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            out[str(r["ts_code"])] = {
                "name": str(r.get("name", "")),
                "industry": str(r.get("industry", "")),
                "area": str(r.get("area", "")),
                "market": str(r.get("market", "")),
            }
    return out


def resolve_basic(pro: TushareProxy, store: Store, ts_codes: List[str]) -> Dict[str, Dict[str, str]]:
    """Cache-first stock_basic: one Tushare bulk call only when new codes appear."""
    basic = store.load_basic(ts_codes)
    missing = [c for c in ts_codes if c not in basic]
    if missing:
        fetched = fetch_all_basic(pro)
        store.upsert_basic(fetched)
        for c in ts_codes:
            if c in fetched:
                basic[c] = fetched[c]
    return basic


def needed_income_periods(period: str) -> Set[str]:
    """Period-ends whose actual income the decomposition may touch."""
    py = prev_year_period(period)
    rp = prev_cumulative_period(period)  # revenue trailing anchor / current prior cum
    s = {
        prev_cumulative_period(period),
        prev_cumulative_period(prev_cumulative_period(period)),  # Q3/Q4 QoQ base
        py,
        prev_cumulative_period(py),
        rp,
        prev_year_period(rp),
        prev_cumulative_period(rp),
    }
    return {p for p in s if p}


def fetch_income_periods(pro: TushareProxy, ts_code: str, period: str,
                         needed: Set[str]) -> Dict[str, Dict[str, Any]]:
    """ONE income range call → period-end -> {n_income_attr_p, revenue, ann_date}.
    Periods the API doesn't return are NULL-filled so newly-listed gaps aren't re-queried."""
    start = f"{quarter_of(period)[0] - 1}0101"
    end = dt.date.today().strftime("%Y%m%d")
    try:
        df = pro.income(ts_code=ts_code, start_date=start, end_date=end, fields=INCOME_FIELDS)
    except Exception:  # noqa: BLE001
        df = None

    fetched: Dict[str, Dict[str, Any]] = {}
    if df is not None and not df.empty:
        df = df[df["report_type"] == "1"].sort_values("ann_date")  # latest restatement wins
        for _, r in df.iterrows():
            fetched[str(r["end_date"])] = {
                "n_income_attr_p": _clean(r.get("n_income_attr_p")),
                "revenue": _clean(r.get("revenue")),
                "ann_date": str(r.get("ann_date", "")),
            }
    for p in needed:
        fetched.setdefault(p, {"n_income_attr_p": None, "revenue": None, "ann_date": None})
    return fetched


def gather_income(pro: TushareProxy, store: Store, ts_codes: List[str], period: str,
                  refresh: bool, workers: int) -> Tuple[Dict[str, Dict[str, Dict[str, Any]]], int]:
    """Batch-load cached income for all candidates, concurrently fetch only the
    misses, then batch-write them once. Returns {ts_code: {period: {...}}} + #API calls."""
    needed = needed_income_periods(period)
    result: Dict[str, Dict[str, Dict[str, Any]]] = {
        c: dict(v) for c, v in store.load_income_many(ts_codes).items()
    }
    for c in ts_codes:
        result.setdefault(c, {})
    to_fetch = [c for c in ts_codes if refresh or not all(p in result[c] for p in needed)]

    upsert_records: List[Dict[str, Any]] = []
    if to_fetch:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(to_fetch)))) as ex:
            fetched_all = ex.map(lambda c: fetch_income_periods(pro, c, period, needed), to_fetch)
            for code, by_period in zip(to_fetch, fetched_all):
                result[code].update(by_period)
                for p, v in by_period.items():
                    upsert_records.append({"ts_code": code, "period": p, **v})
    store.upsert_income_many(upsert_records)
    return result, len(to_fetch)


def _sort_key(rec: Dict[str, Any]) -> Tuple[int, float]:
    v = rec["profit_growth"]["cum_yoy_pct"]
    return (0, -v) if v is not None else (1, 0.0)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Scan A-share earnings forecasts for one period and decompose growth.")
    ap.add_argument("--period", help="Report period end YYYYMMDD (quarter-end). Default: latest quarter <= today.")
    ap.add_argument("--start-ann", help="Announcement-scan window start YYYYMMDD. Default: period_end - 45d.")
    ap.add_argument("--end-ann", help="Announcement-scan window end YYYYMMDD. Default: min(today, period_end + 75d).")
    ap.add_argument("--min-pchange", type=float, default=0.0, help="prefilter_pass threshold on 当年累计同比 %% (default 0).")
    ap.add_argument("--positive-only", action="store_true", help="Drop negative-type forecasts (预减/首亏/续亏/增亏/略减).")
    ap.add_argument("--fetch-workers", type=int, default=4,
                    help="Parallel workers for forecast/income calls (set 1 to serialize under rate limits).")
    ap.add_argument("--refetch-days", type=int, default=3,
                    help="Always re-scan the most recent N trading days for late/revised filings (default 3).")
    ap.add_argument("--rebuild", action="store_true", help="Ignore the fetch log and re-scan the whole window + refresh income.")
    ap.add_argument("--refresh-income", action="store_true", help="Force re-fetch of cached income actuals.")
    ap.add_argument("--no-cache", action="store_true", help="Disable the DB cache (full fetch, non-incremental).")
    ap.add_argument("--out", help="Output JSON path. Default: reports/forecast_scan_<period>.json")
    ap.add_argument("--stdout", action="store_true", help="Also print the full JSON to stdout.")
    args = ap.parse_args(argv)

    today = dt.date.today()
    period = args.period or latest_quarter_end(today)
    quarter_of(period)  # validate

    p_end = dt.datetime.strptime(period, "%Y%m%d").date()
    start_ann = args.start_ann or (p_end - dt.timedelta(days=45)).strftime("%Y%m%d")
    default_end = min(today, p_end + dt.timedelta(days=75))
    end_ann = args.end_ann or default_end.strftime("%Y%m%d")

    notes: List[str] = [
        "业绩预告(forecast)接口只有净利润(min/max)与同比幅度，没有营收字段；"
        "收入取自最近实际报告期(income)，为 trailing 实际值，不是预告值。",
        "单季度同比/环比用实际季报拆解：单季净利 = 累计(预告中值) − 上一累计期(实际)。",
        "已抓的预告/季报存入 DB(forecast_* 表)，每次只增量抓新公告日与新个股，不重复全量抓取。",
    ]

    store = Store(enabled=not args.no_cache)
    refresh_income = args.refresh_income or args.rebuild
    workers = max(1, args.fetch_workers)
    pro = TushareProxy(get_tushare_pro())
    forecasts, fetch_stats = scan_forecasts_incremental(
        pro, store, period, start_ann, end_ann, args.refetch_days, args.rebuild, workers, notes,
    )

    stocks: List[Dict[str, Any]] = []
    income_missing: List[str] = []
    income_calls = 0
    if forecasts:
        all_codes = [str(f["ts_code"]) for f in forecasts]
        basic = resolve_basic(pro, store, all_codes)
        income_by_code, income_calls = gather_income(pro, store, all_codes, period, refresh_income, workers)
        for row in forecasts:
            ts_code = str(row["ts_code"])
            cache = CumCache(income_by_code.get(ts_code, {}))
            if not cache.has_actual_np(prev_cumulative_period(period)):
                income_missing.append(ts_code)
            rec = build_stock_record(row, cache, period, basic, args.min_pchange)
            if args.positive_only and rec["flags"]["negative_type"]:
                continue
            stocks.append(rec)

    stocks.sort(key=_sort_key)
    fetch_stats["income_calls"] = income_calls
    fetch_stats["income_from_cache"] = max(len(forecasts) - income_calls, 0)

    payload = {
        "meta": {
            "period": period,
            "period_label": period_label(period),
            "latest_quarter": f"Q{quarter_of(period)[1]}",
            "ann_window": [start_ann, end_ann],
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "unique_stocks": len(stocks),
            "positive_types": sorted(POSITIVE_TYPES),
            "negative_types": sorted(NEGATIVE_TYPES),
            "screen": {"min_pchange": args.min_pchange, "positive_only": args.positive_only},
            "fetch_stats": fetch_stats,
            "income_missing": income_missing,
            "data_notes": notes,
        },
        "stocks": stocks,
    }

    out_path = args.out or os.path.join("reports", f"forecast_scan_{period}.json")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    summary = {
        "period": period, "unique_stocks": len(stocks),
        "positive": sum(1 for s in stocks if s["flags"]["positive_type"]),
        "accelerating": sum(1 for s in stocks if s["flags"]["accelerating"]),
        "income_missing": len(income_missing),
        "fetch_stats": fetch_stats,
        "out": out_path,
    }
    if args.stdout:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
