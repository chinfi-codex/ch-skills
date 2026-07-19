#!/usr/bin/env python3
"""Earnings-forecast (业绩预告) full-market scanner + growth decomposition.

Deterministic evidence builder. It does NOT decide which stocks are "excellent"
— that judgment (acceleration quality, base effects, one-off earnings, reason
sustainability) is the model's job. This script only:

  1. Scans forecasts for one reporting period by iterating forecast(ann_date=)
     over every calendar day (the API rejects range/period-only queries and
     announcements can be dated on weekends), then keeps the latest forecast
     per stock. The default cutoff is Beijing run date + 1 day.
  2. Computes the median net profit and the cumulative YoY from the forecast
     range (万元 basis).
  3. Pulls actual quarterly income to decompose the LATEST single quarter into
     单季度同比 (single-quarter YoY) and 环比 (QoQ), alongside 当年累计同比.
  4. Attaches the latest ACTUAL revenue trajectory (forecasts carry no revenue).
  5. Attaches a valuation reference per stock: 预告中值年化 PE (median annualized
     by 4/q) plus a rolling variant (prior annual + median - prior same-period),
     against the latest-trade-day total market cap (one daily_basic bulk call).

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

from cninfo_client import list_forecast_announcements
from cninfo_enrich import enrich_announcement, forecast_row_from_enrich
from store import FORECAST_COLS, Store
from tushare_client import TushareProxy, get_tushare_pro

YI = 1e8            # 元 -> 亿元
WAN = 1e4          # 万元 -> 元

# 业绩预告类型 (Tushare `type`). Positive = 业绩向好方向; negative = 恶化方向.
POSITIVE_TYPES = {"预增", "略增", "续盈", "扭亏", "减亏"}
NEGATIVE_TYPES = {"预减", "略减", "首亏", "续亏", "增亏"}

INCOME_FIELDS = "ts_code,end_date,ann_date,report_type,n_income_attr_p,revenue"
DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount"
ADJ_FACTOR_FIELDS = "ts_code,trade_date,adj_factor"
DAILY_BASIC_FIELDS = "ts_code,trade_date,total_mv"

# Price-reaction engine (净利润断层观察): history window before the announcement
# for the 1y-position percentile, and the minimum bars to trust that percentile.
PRICE_HISTORY_CAL_DAYS = 400
POS_MIN_BARS = 60
BEIJING_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")


# --------------------------------------------------------------------------- #
# Period arithmetic
# --------------------------------------------------------------------------- #
_QUARTER_MMDD = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}
_MMDD_QUARTER = {v: k for k, v in _QUARTER_MMDD.items()}


def beijing_now() -> dt.datetime:
    """Timezone-aware clock used by all run-date and freshness semantics."""
    return dt.datetime.now(BEIJING_TZ)


def beijing_today() -> dt.date:
    return beijing_now().date()


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


def calendar_days(start: str, end: str) -> List[str]:
    """Inclusive YYYYMMDD calendar days; announcement dates are not trade dates."""
    d0 = dt.datetime.strptime(start, "%Y%m%d").date()
    d1 = dt.datetime.strptime(end, "%Y%m%d").date()
    if d1 < d0:
        raise ValueError(f"公告日扫描窗口倒置: {start} > {end}")
    return [(d0 + dt.timedelta(days=n)).strftime("%Y%m%d")
            for n in range((d1 - d0).days + 1)]


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
    days = calendar_days(start_ann, end_ann)

    cached_tushare = store.load_tushare_forecasts(period)
    # First run after the CNInfo-primary migration must seed the dedicated
    # reconciliation table even when the legacy fetch log already has watermarks.
    logged = set() if rebuild or (store.available and not cached_tushare) else store.logged_ann_dates(period)
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

    # Dedupe before upsert: a wide scan collects the same (ts_code, end_date) on
    # multiple announcement days; the batch upsert must not carry duplicate keys.
    # Only record the fetch-log watermark if the rows actually persisted — never
    # mark a day scanned when its forecasts failed to write (would drop them).
    if store.upsert_tushare_forecasts(_dedupe_latest(new_rows)):
        store.record_fetch_days(period, day_counts)
    elif store.available:
        notes.append("预告落库失败，本次不记录公告日水位(下次将重扫)。")

    if store.available:
        full = store.load_tushare_forecasts(period)
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


def _cninfo_date_ranges(days: List[str]) -> List[Tuple[str, str]]:
    """Collapse sorted YYYYMMDD days into contiguous ranges."""
    if not days:
        return []
    ordered = sorted(set(days))
    groups: List[Tuple[str, str]] = []
    start = prev = ordered[0]
    for day in ordered[1:]:
        expected = (dt.datetime.strptime(prev, "%Y%m%d").date() + dt.timedelta(days=1)).strftime("%Y%m%d")
        if day != expected:
            groups.append((start, prev))
            start = day
        prev = day
    groups.append((start, prev))
    return groups


def scan_cninfo_incremental(store: Store, period: str, start_ann: str, end_ann: str,
                            refetch_days: int, rebuild: bool, workers: int,
                            notes: List[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Discover the authoritative announcement universe from CNInfo by day."""
    days = calendar_days(start_ann, end_ann)
    logged = set() if rebuild else store.logged_cninfo_ann_dates(period)
    recent = set(days[-refetch_days:]) if refetch_days > 0 else set()
    to_fetch = [day for day in days if day not in logged or day in recent]
    rows: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    failed = 0
    # CNInfo list API is sensitive to burst concurrency. Query contiguous date
    # ranges sequentially and let its paginator pace requests; one full-window
    # migration is ~59 pages, while a normal daily run is a single short range.
    for range_start, range_end in _cninfo_date_ranges(to_fetch):
        start_dash = f"{range_start[:4]}-{range_start[4:6]}-{range_start[6:]}"
        end_dash = f"{range_end[:4]}-{range_end[4:6]}-{range_end[6:]}"
        try:
            found = list_forecast_announcements(period, f"{start_dash}~{end_dash}", timeout=30)
        except Exception as exc:  # noqa: BLE001
            failed += len(calendar_days(range_start, range_end))
            notes.append(f"CNInfo公告扫描({range_start}~{range_end})失败: {str(exc)[:100]}")
            continue
        range_days = calendar_days(range_start, range_end)
        range_counts = {day: 0 for day in range_days}
        for row in found:
            day = _date_digits(row.get("ann_date"))
            if day in range_counts:
                range_counts[day] += 1
        counts.update(range_counts)
        rows.extend(found)
    if store.upsert_cninfo_announcements(period, rows):
        store.record_cninfo_fetch_days(period, counts)
    elif store.available:
        notes.append("CNInfo公告元数据落库失败，本次不记录CNInfo水位。")
    full = store.load_cninfo_announcements(period) if store.available else rows
    return full, {
        "cninfo_ann_days_in_window": len(days),
        "cninfo_ann_days_fetched": len(to_fetch),
        "cninfo_ann_days_skipped_cached": len(days) - len(to_fetch),
        "cninfo_fetch_failed": failed,
        "cninfo_new_announcement_rows": len(rows),
        "cninfo_announcement_rows_total": len(full),
    }


def _date_digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _latest_cninfo_by_code(rows: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    latest: Dict[str, Dict[str, Any]] = {}
    first: Dict[str, str] = {}
    for row in rows:
        code = str(row.get("ts_code") or "")
        ann_date = _date_digits(row.get("ann_date"))
        if not code or not ann_date:
            continue
        first[code] = min(first.get(code, ann_date), ann_date)
        cur = latest.get(code)
        key = (ann_date, str(row.get("announcement_id") or ""))
        if cur is None or key >= (_date_digits(cur.get("ann_date")), str(cur.get("announcement_id") or "")):
            normalized = dict(row)
            normalized["ann_date"] = ann_date
            latest[code] = normalized
    return latest, first


def _cached_enrich_record(ts_code: str, cached: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ts_code": ts_code, "code": ts_code[:6], "found": True,
        "announcement": cached.get("announcement") or {},
        "parsed": cached.get("parsed") or {}, "text": cached.get("text") or "",
        "notes": [], "from_cache": True,
    }


def _amount_mid_raw(row: Dict[str, Any]) -> Optional[float]:
    vals = [row.get("net_profit_min"), row.get("net_profit_max")]
    vals = [float(v) for v in vals if v is not None and pd.notna(v)]
    return sum(vals) / len(vals) if vals else None


def _same_day_structured_conflict(cn_row: Dict[str, Any], ts_row: Dict[str, Any]) -> bool:
    """Detect an unstable PDF table parse against the same official disclosure.

    CNInfo remains the discovery/authority source.  Tushare is used here only as
    an independent structured representation of the *same announcement*.  A
    sign disagreement is always unsafe; a midpoint difference above 25% is
    also treated as a parser-row mismatch. This catches PDF cell concatenation
    such as a current upper bound ``600`` followed immediately by prior-period
    ``176.80``, which text extraction exposes as the false number ``600176.80``.
    """
    if _date_digits(cn_row.get("ann_date")) != _date_digits(ts_row.get("ann_date")):
        return False
    cn_mid, ts_mid = _amount_mid_raw(cn_row), _amount_mid_raw(ts_row)
    if cn_mid is None or ts_mid is None:
        return False
    if abs(cn_mid) > 100 and abs(ts_mid) > 100 and (cn_mid > 0) != (ts_mid > 0):
        return True
    relative = abs(cn_mid - ts_mid) / max(abs(ts_mid), 100.0)
    return relative > 0.25


def _force_negative_amount(amount: Any) -> None:
    """Normalize unsigned loss cells in an in-memory CNInfo parsed record."""
    if not isinstance(amount, dict):
        return
    vals = [amount.get("low"), amount.get("high")]
    if all(v is not None for v in vals):
        negative = sorted((-abs(float(vals[0])), -abs(float(vals[1]))))
        amount["low"], amount["high"] = negative
    if amount.get("point") is not None:
        amount["point"] = -abs(float(amount["point"]))


def build_cninfo_primary_forecasts(store: Store, period: str, announcements: List[Dict[str, Any]],
                                   tushare_rows: List[Dict[str, Any]], workers: int,
                                   notes: List[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any],
                                                              Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Build the final forecast universe: CNInfo authority + explicit TS exceptions.

    All discovered CNInfo PDFs are cached once.  Parsed CNInfo fields win; a
    Tushare value is used only to fill a field that the PDF parser could not
    stabilize.  Tushare-only rows remain visible as labeled exception records
    (mainly prospectus-embedded forecasts) and are counted in reconciliation.
    """
    latest, first_dates = _latest_cninfo_by_code(announcements)
    cn_codes = sorted(latest)
    ts_map = {str(row["ts_code"]): dict(row) for row in tushare_rows}
    cached = store.get_enrich_many(cn_codes, period)
    to_fetch = []
    for code in cn_codes:
        current = cached.get(code)
        cached_date = _date_digits((current or {}).get("announcement", {}).get("ann_date"))
        if not current or current.get("parsed") is None or cached_date < latest[code]["ann_date"]:
            to_fetch.append(code)

    fetched: Dict[str, Dict[str, Any]] = {}
    if to_fetch:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(to_fetch)))) as ex:
            for code, rec in zip(to_fetch, ex.map(lambda c: enrich_announcement(latest[c], period), to_fetch)):
                rec["from_cache"] = False
                fetched[code] = rec

    upsert_enrich: List[Dict[str, Any]] = []
    enrich_records: Dict[str, Dict[str, Any]] = {}
    for code in cn_codes:
        rec = fetched.get(code)
        if rec is None:
            rec = _cached_enrich_record(code, cached[code])
        enrich_records[code] = rec
        if rec.get("found") and not rec.get("from_cache"):
            ann = rec.get("announcement") or {}
            upsert_enrich.append({
                "ts_code": code, "period": period, "ann_date": ann.get("ann_date", ""),
                "title": ann.get("title", ""), "url": ann.get("url", ""),
                "parsed": rec.get("parsed"), "text": rec.get("text", ""),
            })
    store.upsert_enrich_many(upsert_enrich)

    cn_rows: Dict[str, Dict[str, Any]] = {}
    source_index: Dict[str, Dict[str, Any]] = {}
    parsed_rows = 0
    parse_fallback = 0
    conflict_fallback = 0
    missing_no_fallback: List[str] = []
    rejected_unparsed: List[str] = []
    for code in cn_codes:
        ann = latest[code]
        rec = enrich_records[code]
        ts_row = ts_map.get(code)
        parsed = rec.get("parsed") or {}
        effective_type = str(parsed.get("forecast_type") or (ts_row or {}).get("type") or "")
        if effective_type in ("首亏", "续亏", "增亏", "减亏"):
            _force_negative_amount(parsed.get("parent_net_yi"))
            _force_negative_amount(parsed.get("kf_net_profit_yi"))
        row = forecast_row_from_enrich(rec, period) if rec.get("found") else None
        structured_source = "cninfo_pdf"
        if row is not None:
            parsed_rows += 1
            if ts_row:
                # CNInfo facts have priority; Tushare only fills parser gaps and
                # preserves the earliest known disclosure date.
                for field in ("type", "p_change_min", "p_change_max", "last_parent_net", "change_reason"):
                    if row.get(field) in (None, ""):
                        row[field] = ts_row.get(field)
                prior_first = _date_digits(ts_row.get("first_ann_date") or ts_row.get("ann_date"))
                if prior_first:
                    row["first_ann_date"] = min(first_dates[code], prior_first)
                row["update_flag"] = row.get("update_flag") or ts_row.get("update_flag")
                if _same_day_structured_conflict(row, ts_row):
                    # The two rows describe the same official announcement; a
                    # material mismatch indicates that the PDF table parser
                    # selected a prior-period/adjacent row. Keep CNInfo reason
                    # and provenance, but use the reconciled structured amounts.
                    cn_reason = row.get("change_reason")
                    first_ann_date = row.get("first_ann_date")
                    row = dict(ts_row)
                    if cn_reason:
                        row["change_reason"] = cn_reason
                    if first_ann_date:
                        row["first_ann_date"] = first_ann_date
                    structured_source = "tushare_reconciliation_fallback"
                    conflict_fallback += 1
            else:
                row["first_ann_date"] = first_dates[code]
        elif ts_row is not None:
            row = dict(ts_row)
            structured_source = "tushare_parse_fallback"
            parse_fallback += 1
        else:
            missing_no_fallback.append(code)
            title = str(ann.get("title") or "")
            explicit_forecast = any(token in title for token in (
                "业绩预告", "业绩预增", "业绩预减", "业绩预盈", "业绩预亏", "业绩公告",
            ))
            if not explicit_forecast:
                # Supplemental search intentionally has high recall. A generic
                # 经营情况公告 with neither parsed profit nor Tushare corroboration
                # is not stable evidence of an earnings forecast (e.g. contract
                # operating statistics), so reject it from the report universe.
                rejected_unparsed.append(code)
                continue
            ftype = next((token for token in (*POSITIVE_TYPES, *NEGATIVE_TYPES) if token in title), "")
            row = {
                "ts_code": code, "end_date": period, "ann_date": ann["ann_date"],
                "type": ftype, "p_change_min": None, "p_change_max": None,
                "net_profit_min": None, "net_profit_max": None, "last_parent_net": None,
                "first_ann_date": first_dates[code], "summary": f"CNInfo公告待稳定解析：{title}",
                "change_reason": "", "update_flag": "1" if any(t in title for t in ("修正", "更正")) else "0",
            }
            structured_source = "cninfo_unparsed"
        cn_rows[code] = row
        source_index[code] = {
            "authority": "cninfo", "discovery_source": "cninfo",
            "structured_source": structured_source,
            "announcement_id": ann.get("announcement_id"),
            "title": ann.get("title"), "url": ann.get("url"),
        }

    accepted_cn_codes = set(cn_rows)
    ts_only = sorted(set(ts_map) - accepted_cn_codes)
    final_map = dict(cn_rows)
    for code in ts_only:
        final_map[code] = ts_map[code]
        source_index[code] = {
            "authority": "tushare_exception", "discovery_source": "tushare_reconciliation",
            "structured_source": "tushare", "reason": "CNInfo独立预告公告未命中（常见于IPO文件内嵌预测）",
        }
    final_rows = _dedupe_latest(list(final_map.values()))
    if store.available and not store.upsert_forecasts(final_rows):
        notes.append("CNInfo主源合并结果写入forecast_cache失败；本次仍使用内存结果。")

    enrich_payload = {
        "meta": {
            "period": period, "period_label": period_label(period),
            "generated_at": beijing_now().isoformat(timespec="seconds"),
            "requested": len(cn_codes),
            "found": sum(1 for rec in enrich_records.values() if rec.get("found")),
            "from_cache": len(cn_codes) - len(to_fetch),
            "downloaded": len(to_fetch),
            "missing": [code for code, rec in enrich_records.items() if not rec.get("found")],
            "source_role": "CNInfo official announcement facts; generated by forecast_scan",
        },
        "stocks": [enrich_records[code] for code in cn_codes if enrich_records[code].get("found")],
    }
    stats = {
        "primary_source": "cninfo",
        "cninfo_discovery_candidates": len(cn_codes),
        "cninfo_unique_stocks": len(accepted_cn_codes),
        "tushare_reconciliation_stocks": len(ts_map),
        "source_intersection": len(accepted_cn_codes & set(ts_map)),
        "cninfo_only_stocks": len(accepted_cn_codes - set(ts_map)),
        "tushare_only_exceptions": len(ts_only),
        "cninfo_pdf_structured": parsed_rows,
        "cninfo_pdf_parse_fallback": parse_fallback,
        "cninfo_pdf_conflict_fallback": conflict_fallback,
        "cninfo_unparsed_without_fallback": [code for code in missing_no_fallback if code not in rejected_unparsed],
        "cninfo_rejected_unparsed": rejected_unparsed,
        "cninfo_pdf_downloaded": len(to_fetch),
        "cninfo_pdf_from_cache": len(cn_codes) - len(to_fetch),
    }
    return final_rows, stats, enrich_payload, source_index


def _median(lo: Any, hi: Any) -> Optional[float]:
    vals = [float(v) for v in (lo, hi) if pd.notna(v)]
    return sum(vals) / len(vals) if vals else None


# 年化口径标签（q -> 展示用换算说明）
_ANNUALIZE_LABEL = {1: "中值×4", 2: "中值×2", 3: "中值×4/3", 4: "年报中值即年化"}


def fetch_total_mv(pro: TushareProxy, today: dt.date, notes: List[str]) -> Tuple[Dict[str, float], Optional[str]]:
    """Latest-trade-day total market cap (元) for the whole market — ONE
    daily_basic bulk call per run. Today's snapshot only publishes after the
    close, so step back through the last few trade days until rows appear."""
    days = trading_days(pro, (today - dt.timedelta(days=15)).strftime("%Y%m%d"),
                        today.strftime("%Y%m%d"))
    for day in reversed(days[-3:]):
        try:
            df = pro.daily_basic(trade_date=day, fields=DAILY_BASIC_FIELDS)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"daily_basic({day}) 取数失败: {str(exc)[:80]}——本次无总市值，年化PE缺失。")
            return {}, None
        if df is not None and not df.empty:
            out: Dict[str, float] = {}
            for _, r in df.iterrows():
                mv = _clean(r.get("total_mv"))
                if mv is not None:
                    out[str(r["ts_code"])] = float(mv) * WAN  # 万元 -> 元
            return out, day
    notes.append("最近交易日 daily_basic 均无数据，年化PE缺失。")
    return {}, None


def build_valuation(np_med: Optional[float], cache: CumCache, period: str,
                    total_mv: Optional[float], mv_asof: Optional[str]) -> Dict[str, Any]:
    """预告中值年化 PE（估值参照证据，不是买卖依据；读法与坑见 methodology §十一）。

    - 年化净利 = 预告中值 ÷ 报告期季数 × 4（简单年化，未调季节性；年报即中值）。
    - 滚动净利 = 上年年报实际 + 本期预告中值 − 上年同期实际（季节性对照口径；
      年报预告时上年同期=上年年报，两口径自然相等）。
    - 年化净利 ≤ 0 时 PE 无意义，只给 note，不硬算。
    """
    year, q = quarter_of(period)
    annualized = None if np_med is None else np_med * 4.0 / q
    prior_annual = cache.np(period_end(year - 1, 4))
    prior_same = cache.np(prev_year_period(period))
    rolling = None
    if np_med is not None and prior_annual is not None and prior_same is not None:
        rolling = prior_annual + np_med - prior_same

    def pe_of(profit: Optional[float]) -> Tuple[Optional[float], str]:
        if profit is None:
            return None, "np_missing"
        if profit <= 0:
            return None, "np_nonpositive"
        if total_mv is None or total_mv <= 0:
            return None, "mv_missing"
        return round(total_mv / profit, 1), "ok"

    pe_ann, ann_note = pe_of(annualized)
    pe_roll, roll_note = pe_of(rolling)
    if rolling is None and np_med is not None:
        roll_note = "base_missing"  # 上年年报/上年同期实际缺失（次新股等）
    return {
        "mv_asof": mv_asof,
        "total_mv_yi": to_yi(total_mv),
        "annualized_np_yi": to_yi(annualized),
        "annualize_label": _ANNUALIZE_LABEL[q],
        "pe_annualized": pe_ann,
        "pe_annualized_note": ann_note,
        "rolling_np_yi": to_yi(rolling),
        "pe_rolling": pe_roll,
        "pe_rolling_note": roll_note,
    }


def build_stock_record(row: pd.Series, cache: CumCache, period: str,
                       basic: Dict[str, Dict[str, str]], min_pchange: float,
                       price_reaction: Optional[Dict[str, Any]] = None,
                       total_mv: Optional[float] = None,
                       mv_asof: Optional[str] = None,
                       source_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ts_code = str(row["ts_code"])
    ftype = str(row["type"]) if pd.notna(row.get("type")) else ""

    # --- forecast net profit (万元 -> 元) ---
    np_min = None if pd.isna(row.get("net_profit_min")) else float(row["net_profit_min"]) * WAN
    np_max = None if pd.isna(row.get("net_profit_max")) else float(row["net_profit_max"]) * WAN
    np_med = _median(np_min, np_max)
    last_parent = None if pd.isna(row.get("last_parent_net")) else float(row["last_parent_net"]) * WAN
    py = prev_year_period(period)
    if last_parent is None:
        last_parent = cache.np(py)

    # CNInfo title templates do not always carry the forecast-type word. Infer
    # only the mechanical direction from the disclosed amount versus the actual
    # prior-year base; this remains evidence classification, not a quality verdict.
    if not ftype and np_med is not None and last_parent is not None:
        if last_parent <= 0 < np_med:
            ftype = "扭亏"
        elif last_parent < 0 and np_med <= 0:
            ftype = "减亏" if np_med > last_parent else "增亏"
        elif last_parent > 0 and np_med < 0:
            ftype = "首亏"
        elif last_parent > 0:
            ratio = np_med / last_parent
            ftype = "预增" if ratio >= 1.3 else "略增" if ratio > 1 else "预减" if ratio <= 0.7 else "略减"

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
        "source": source_info or {},
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
        "valuation": build_valuation(np_med, cache, period, total_mv, mv_asof),
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
        "price_reaction": price_reaction,
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


def build_industry_summary(stocks: List[Dict[str, Any]], top_members: int = 5) -> List[Dict[str, Any]]:
    """Per stock_basic-industry tallies for the 产业结构综述: direction counts,
    growth medians, gap tallies and the largest members. Counting only — how the
    industries cluster into macro groups (高端制造/消费/地产链…) and what the
    structure means is the model's judgment (references/methodology.md §十)."""
    def med(vals: List[Any]) -> Optional[float]:
        nums = sorted(float(v) for v in vals if v is not None)
        if not nums:
            return None
        n = len(nums)
        m = nums[n // 2] if n % 2 else (nums[n // 2 - 1] + nums[n // 2]) / 2.0
        return round(m, 2)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for s in stocks:
        groups.setdefault(s.get("industry") or "未知", []).append(s)
    out: List[Dict[str, Any]] = []
    for ind, grp in groups.items():
        members = sorted(grp, key=lambda s: -abs(s["net_profit"].get("median_yi") or 0.0))[:top_members]
        out.append({
            "industry": ind,
            "n": len(grp),
            "positive_n": sum(1 for s in grp if s["flags"]["positive_type"]),
            "negative_n": sum(1 for s in grp if s["flags"]["negative_type"]),
            "turnaround_n": sum(1 for s in grp if s["flags"]["turnaround"]),
            "accelerating_n": sum(1 for s in grp if s["flags"]["accelerating"]),
            "gap_up_n": sum(1 for s in grp if (s.get("price_reaction") or {}).get("gap_dir") == "up"),
            "gap_down_n": sum(1 for s in grp if (s.get("price_reaction") or {}).get("gap_dir") == "down"),
            "cum_yoy_median": med([s["profit_growth"].get("cum_yoy_pct") for s in grp]),
            "single_q_yoy_median": med([s["profit_growth"].get("single_q_yoy_pct") for s in grp]),
            "qoq_median": med([s["profit_growth"].get("qoq_pct") for s in grp]),
            "members": [{
                "name": m.get("name", ""),
                "type": m.get("type", ""),
                "cum_yoy_pct": m["profit_growth"].get("cum_yoy_pct"),
                "single_q_yoy_pct": m["profit_growth"].get("single_q_yoy_pct"),
                "np_median_yi": m["net_profit"].get("median_yi"),
                "gap_dir": (m.get("price_reaction") or {}).get("gap_dir"),
            } for m in members],
        })
    out.sort(key=lambda g: (-g["n"], g["industry"]))
    return out


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
        period_end(quarter_of(period)[0] - 1, 4),  # 上年年报 — 滚动PE 基数(仅 Q3 时是新增)
    }
    return {p for p in s if p}


def fetch_income_periods(pro: TushareProxy, ts_code: str, period: str,
                         needed: Set[str]) -> Dict[str, Dict[str, Any]]:
    """ONE income range call → period-end -> {n_income_attr_p, revenue, ann_date}.
    Periods the API doesn't return are NULL-filled so newly-listed gaps aren't re-queried."""
    start = f"{quarter_of(period)[0] - 1}0101"
    end = beijing_today().strftime("%Y%m%d")
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


# --------------------------------------------------------------------------- #
# Price reaction (净利润断层观察): deterministic gap/position/holding metrics
# around the FIRST forecast announcement. No opportunity judgment here — the
# model reads these plus the theme attribution to judge 断层机会.
# --------------------------------------------------------------------------- #
def _shift_ymd(ymd: str, days: int) -> str:
    return (dt.datetime.strptime(ymd, "%Y%m%d").date() + dt.timedelta(days=days)).strftime("%Y%m%d")


def _r2(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 2)


def compute_reaction(bars: List[Dict[str, Any]], anchor_ann: str, gap_min: float) -> Dict[str, Any]:
    """Reaction metrics anchored on the first announcement date.

    业绩预告一般在披露日(ann_date)的前一交易日盘后发出，ann_date 标的是正式披露日，
    所以市场反应落在 ann_date 当天：pre-ann close = 最后一个 < anchor_ann 的交易日收盘
    (即公告日前一交易日)，reaction day R = 第一个 >= anchor_ann 的交易日(即公告日当天，
    若为交易日；否则顺延到之后第一个交易日)。
    """
    block: Dict[str, Any] = {"anchor_ann_date": anchor_ann}
    usable = [b for b in bars if b.get("close") is not None and b.get("trade_date")]
    if not usable:
        block["note"] = "no_bars"
        return block
    pre_idx = None
    for i, b in enumerate(usable):
        if str(b["trade_date"]) < anchor_ann:   # 严格小于：公告日前一交易日为基准
            pre_idx = i
        else:
            break
    if pre_idx is None:
        block["note"] = "no_pre_ann_bar"
        return block

    pre = usable[pre_idx]
    pre_close = float(pre["close"])
    block["pre_ann_date"] = str(pre["trade_date"])
    block["pre_ann_close"] = _r2(pre_close)

    # --- pre-announcement position / momentum (the two-case classifier inputs) ---
    hist = usable[max(0, pre_idx + 1 - 250): pre_idx + 1]
    block["history_bars"] = len(hist)
    if len(hist) >= POS_MIN_BARS:
        lows = [float(b["low"]) for b in hist if b.get("low") is not None]
        highs = [float(b["high"]) for b in hist if b.get("high") is not None]
        lo, hi = min(lows), max(highs)
        block["pre_pos_1y_pct"] = _r2((pre_close - lo) / (hi - lo) * 100.0) if hi > lo else None
    else:
        block["pre_pos_1y_pct"] = None
        block["note"] = "history_short"
    if pre_idx >= 20 and usable[pre_idx - 20].get("close"):
        block["pre_mom_20d_pct"] = _r2((pre_close / float(usable[pre_idx - 20]["close"]) - 1) * 100.0)
    else:
        block["pre_mom_20d_pct"] = None

    # --- reaction day and after ---
    post = usable[pre_idx + 1:]
    if not post:
        block["gap_status"] = "pending"
        block["note"] = block.get("note") or "pending_reaction"
        return block
    r = post[0]
    gap_open = (float(r["open"]) / pre_close - 1) * 100.0 if r.get("open") else None
    block["reaction_date"] = str(r["trade_date"])
    block["gap_open_pct"] = _r2(gap_open)
    block["r_day_pct"] = _r2((float(r["close"]) / pre_close - 1) * 100.0)
    if r.get("open") and float(r["open"]) > 0:
        block["r_close_vs_open_pct"] = _r2((float(r["close"]) / float(r["open"]) - 1) * 100.0)
    vol20 = [float(b["vol"]) for b in usable[max(0, pre_idx - 19): pre_idx + 1] if b.get("vol")]
    if vol20 and r.get("vol"):
        avg = sum(vol20) / len(vol20)
        block["r_vol_ratio"] = _r2(float(r["vol"]) / avg) if avg > 0 else None

    last = post[-1]
    block["latest_date"] = str(last["trade_date"])
    block["latest_close"] = _r2(float(last["close"]))
    block["since_ann_pct"] = _r2((float(last["close"]) / pre_close - 1) * 100.0)
    block["trading_days_since_r"] = len(post)
    lows_since = [float(b["low"]) for b in post if b.get("low") is not None]
    highs_since = [float(b["high"]) for b in post if b.get("high") is not None]
    min_low = min(lows_since) if lows_since else None
    max_high = max(highs_since) if highs_since else None
    block["min_low_since_r"] = _r2(min_low)
    block["max_high_since_r"] = _r2(max_high)

    # Direction-aware 净利润断层：向上=业绩超预期跳空(强表现)，向下=不及预期跳空(弱表现)。
    # 回补口径随方向翻转：向上断层被跌回 pre_close 下方 = 回补；向下断层被涨回 pre_close 上方 = 回补。
    if gap_open is None:
        gap_dir = None
    elif gap_open >= gap_min:
        gap_dir = "up"
    elif gap_open <= -gap_min:
        gap_dir = "down"
    else:
        gap_dir = None
    block["gap_dir"] = gap_dir
    if gap_dir is None:
        block["gap_status"] = "none"
    elif gap_dir == "up":
        block["gap_status"] = "filled" if (min_low is not None and min_low <= pre_close) else "intact"
    else:  # down
        block["gap_status"] = "filled" if (max_high is not None and max_high >= pre_close) else "intact"
    # Mechanical threshold hits only — NOT an opportunity verdict.
    block["flags"] = {
        "gap_up": gap_dir == "up",
        "gap_down": gap_dir == "down",
        "gap_intact": block["gap_status"] == "intact",
        "low_base": block.get("pre_pos_1y_pct") is not None and block["pre_pos_1y_pct"] < 40.0,
        "high_pos": block.get("pre_pos_1y_pct") is not None and block["pre_pos_1y_pct"] >= 60.0,
    }
    return block


def _price_fetch_ranges(bars: List[Dict[str, Any]], anchor_ann: str, today_str: str) -> List[Tuple[str, str]]:
    """Head/tail incremental ranges for one stock's bar cache."""
    need_start = _shift_ymd(anchor_ann, -PRICE_HISTORY_CAL_DAYS)
    if not bars:
        return [(need_start, today_str)]
    cached_min = str(bars[0]["trade_date"])
    cached_max = str(bars[-1]["trade_date"])
    ranges: List[Tuple[str, str]] = []
    # 45d buffer absorbs holidays; younger stocks simply have no earlier bars.
    if cached_min > _shift_ymd(need_start, 45):
        ranges.append((need_start, _shift_ymd(cached_min, -1)))
    if cached_max < today_str:
        ranges.append((_shift_ymd(cached_max, 1), today_str))
    return ranges


def _apply_qfq(df: pd.DataFrame, adj: pd.DataFrame) -> pd.DataFrame:
    """Return OHLC prices on latest-available-date qfq basis for this range."""
    if df is None or df.empty or adj is None or adj.empty:
        return df
    daily = df.copy()
    factors = adj.copy()
    daily["ts_code"] = daily["ts_code"].astype(str)
    daily["trade_date"] = daily["trade_date"].astype(str)
    factors["ts_code"] = factors["ts_code"].astype(str)
    factors["trade_date"] = factors["trade_date"].astype(str)
    factors["adj_factor"] = pd.to_numeric(factors["adj_factor"], errors="coerce")
    factors = factors.dropna(subset=["ts_code", "trade_date", "adj_factor"])
    if factors.empty:
        return daily
    end_date = str(daily["trade_date"].max())
    base = (
        factors.loc[factors["trade_date"] <= end_date]
        .sort_values(["ts_code", "trade_date"])
        .groupby("ts_code", as_index=False)
        .tail(1)[["ts_code", "adj_factor"]]
        .rename(columns={"adj_factor": "base_adj_factor"})
    )
    merged = daily.merge(
        factors[["ts_code", "trade_date", "adj_factor"]],
        on=["ts_code", "trade_date"], how="left",
    ).merge(base, on="ts_code", how="left")
    merged["qfq_factor"] = merged["adj_factor"] / merged["base_adj_factor"]
    valid = merged["qfq_factor"].notna() & (merged["qfq_factor"] > 0)
    for col in ("open", "high", "low", "close", "pre_close"):
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce")
            merged.loc[valid, col] = merged.loc[valid, col] * merged.loc[valid, "qfq_factor"]
    if "pct_chg" in merged.columns:
        merged["pct_chg"] = pd.to_numeric(merged["pct_chg"], errors="coerce")
    return merged.drop(columns=["adj_factor", "base_adj_factor", "qfq_factor"], errors="ignore")


def _fetch_bars(pro: TushareProxy, ts_code: str, ranges: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for start, end in ranges:
        if start > end:
            continue
        try:
            df = pro.daily(ts_code=ts_code, start_date=start, end_date=end, fields=DAILY_FIELDS)
        except Exception:  # noqa: BLE001
            continue
        if df is None or df.empty:
            continue
        try:
            adj = pro.adj_factor(ts_code=ts_code, start_date=start, end_date=end, fields=ADJ_FACTOR_FIELDS)
            df = _apply_qfq(df, adj)
        except Exception:  # noqa: BLE001
            pass
        for _, r in df.iterrows():
            rows.append({
                "ts_code": ts_code, "trade_date": str(r["trade_date"]),
                "open": _clean(r.get("open")), "high": _clean(r.get("high")),
                "low": _clean(r.get("low")), "close": _clean(r.get("close")),
                "pre_close": _clean(r.get("pre_close")), "pct_chg": _clean(r.get("pct_chg")),
                "vol": _clean(r.get("vol")), "amount": _clean(r.get("amount")),
            })
    return rows


def gather_price_reactions(pro: TushareProxy, store: Store, anchors: Dict[str, str],
                           gap_min: float, workers: int, refresh_price: bool = False
                           ) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """Cache-first incremental bars for the forecast stocks, then compute the
    reaction block per stock. Network fetch is concurrent; the batch write and
    all computation stay on the main thread."""
    codes = list(anchors.keys())
    if refresh_price:
        store.delete_bars_many(codes)
    cached = store.load_bars_many(codes)
    today = beijing_today()
    market_days = trading_days(
        pro, (today - dt.timedelta(days=15)).strftime("%Y%m%d"), today.strftime("%Y%m%d"),
    )
    # Use the latest actual trading day, not a weekend/holiday calendar date.
    # Otherwise every rerun plans an empty tail request for every stock and the
    # cache can never prove that it is current.
    today_str = market_days[-1] if market_days else today.strftime("%Y%m%d")

    plans = {c: _price_fetch_ranges(cached.get(c, []), anchors[c], today_str) for c in codes}
    to_fetch = [c for c in codes if plans[c]]
    fetched_rows: List[Dict[str, Any]] = []
    if to_fetch:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(to_fetch)))) as ex:
            for rows in ex.map(lambda c: _fetch_bars(pro, c, plans[c]), to_fetch):
                fetched_rows.extend(rows)
    store.upsert_bars_many(fetched_rows)

    new_by_code: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in fetched_rows:
        new_by_code.setdefault(row["ts_code"], {})[row["trade_date"]] = row

    reactions: Dict[str, Dict[str, Any]] = {}
    for c in codes:
        merged = {str(b["trade_date"]): b for b in cached.get(c, [])}
        merged.update(new_by_code.get(c, {}))
        bars = [merged[k] for k in sorted(merged)]
        reactions[c] = compute_reaction(bars, anchors[c], gap_min)
    return reactions, len(to_fetch)


def _sort_key(rec: Dict[str, Any]) -> Tuple[int, float]:
    v = rec["profit_growth"]["cum_yoy_pct"]
    return (0, -v) if v is not None else (1, 0.0)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Scan A-share earnings forecasts for one period and decompose growth.")
    ap.add_argument("--period", help="Report period end YYYYMMDD (quarter-end). Default: latest quarter <= today.")
    ap.add_argument("--start-ann", help="Announcement-scan window start YYYYMMDD. Default: period_end - 45d.")
    ap.add_argument("--end-ann", help="Announcement-scan window end YYYYMMDD. Default: min(Beijing today + 1d, period_end + 75d).")
    ap.add_argument("--min-pchange", type=float, default=0.0, help="prefilter_pass threshold on 当年累计同比 %% (default 0).")
    ap.add_argument("--positive-only", action="store_true", help="Drop negative-type forecasts (预减/首亏/续亏/增亏/略减).")
    ap.add_argument("--fetch-workers", type=int, default=4,
                    help="Parallel workers for forecast/income calls (set 1 to serialize under rate limits).")
    ap.add_argument("--refetch-days", type=int, default=3,
                    help="Always re-scan the most recent N calendar days for late/revised filings (default 3).")
    ap.add_argument("--rebuild", action="store_true", help="Ignore the fetch log and re-scan the whole window + refresh income.")
    ap.add_argument("--refresh-income", action="store_true", help="Force re-fetch of cached income actuals.")
    ap.add_argument("--refresh-price", action="store_true", help="Drop and refetch cached daily bars for the period stocks (qfq basis).")
    ap.add_argument("--no-cache", action="store_true", help="Disable the DB cache (full fetch, non-incremental).")
    ap.add_argument("--gap-min", type=float, default=2.0,
                    help="跳空开盘幅度 >= N%% 记为断层(gap flag)，默认 2.0。")
    ap.add_argument("--no-price", action="store_true", help="跳过股价反应(净利润断层)计算。")
    ap.add_argument("--out", help="Output JSON path. Default: reports/forecast_scan_<period>.json")
    ap.add_argument("--stdout", action="store_true", help="Also print the full JSON to stdout.")
    args = ap.parse_args(argv)

    run_now = beijing_now()
    today = run_now.date()
    period = args.period or latest_quarter_end(today)
    quarter_of(period)  # validate

    p_end = dt.datetime.strptime(period, "%Y%m%d").date()
    start_ann = args.start_ann or (p_end - dt.timedelta(days=45)).strftime("%Y%m%d")
    ann_today = today + dt.timedelta(days=1)
    default_end = min(ann_today, p_end + dt.timedelta(days=75))
    end_ann = args.end_ann or default_end.strftime("%Y%m%d")

    notes: List[str] = [
        "业绩预告以CNInfo官方公告为主发现源和事实权威；公告PDF解析失败，或同公告日解析值出现"
        "正负号/重大数值冲突时才使用Tushare forecast结构化值显式回退，Tushare独有记录仅作为"
        "显式例外（常见于IPO文件内嵌预测）。",
        "业绩预告通常没有营收字段；收入取自最近实际报告期(income)，为 trailing 实际值，不是预告值。",
        "单季度同比/环比用实际季报拆解：单季净利 = 累计(预告中值) − 上一累计期(实际)。",
        f"公告日扫描使用北京时间(Asia/Shanghai)，窗口默认截止到运行日+1({ann_today.strftime('%Y%m%d')})，"
        "逐日历日查询以覆盖盘后按次日入库及周末公告。",
        "CNInfo公告水位、公告元数据、PDF解析、Tushare差异对照和季报均存入PostgreSQL forecast_*表；"
        "日更只重扫最近公告日与新个股。",
    ]

    store = Store(enabled=not args.no_cache)
    refresh_income = args.refresh_income or args.rebuild
    workers = max(1, args.fetch_workers)
    pro = TushareProxy(get_tushare_pro())
    cninfo_announcements, cninfo_stats = scan_cninfo_incremental(
        store, period, start_ann, end_ann, args.refetch_days, args.rebuild, workers, notes,
    )
    if cninfo_stats.get("cninfo_fetch_failed"):
        print(json.dumps({
            "error": "cninfo_primary_discovery_failed",
            "period": period,
            "failed_days": cninfo_stats["cninfo_fetch_failed"],
            "message": "CNInfo是主发现源；公告扫描未完整成功，已停止且不会用Tushare替代生成新报告。",
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3
    tushare_rows, tushare_stats = scan_forecasts_incremental(
        pro, store, period, start_ann, end_ann, args.refetch_days, args.rebuild, workers, notes,
    )
    forecasts, source_stats, enrich_payload, source_index = build_cninfo_primary_forecasts(
        store, period, cninfo_announcements, tushare_rows, workers, notes,
    )
    fetch_stats = {**tushare_stats, **cninfo_stats, **source_stats}
    fetch_stats["tushare_reconciliation_total"] = tushare_stats.get("forecasts_total", 0)
    fetch_stats["forecasts_total"] = len(forecasts)

    enrich_path = os.path.join("reports", f"cninfo_enrich_{period}.json")
    os.makedirs(os.path.dirname(enrich_path), exist_ok=True)
    with open(enrich_path, "w", encoding="utf-8") as fh:
        json.dump(enrich_payload, fh, ensure_ascii=False, indent=2)

    stocks: List[Dict[str, Any]] = []
    income_missing: List[str] = []
    income_calls = 0
    price_calls = 0
    mv_asof: Optional[str] = None
    if forecasts:
        all_codes = [str(f["ts_code"]) for f in forecasts]
        basic = resolve_basic(pro, store, all_codes)
        income_by_code, income_calls = gather_income(pro, store, all_codes, period, refresh_income, workers)
        mv_map, mv_asof = fetch_total_mv(pro, today, notes)
        reactions: Dict[str, Dict[str, Any]] = {}
        if not args.no_price:
            anchors = {
                str(f["ts_code"]): str(f.get("first_ann_date") or f.get("ann_date") or "")
                for f in forecasts
            }
            anchors = {c: a for c, a in anchors.items() if a}
            reactions, price_calls = gather_price_reactions(
                pro, store, anchors, args.gap_min, workers, args.refresh_price,
            )
            notes.append(
                "price_reaction 以首次披露日为锚：预告多在披露日前一交易日盘后发出，跳空/反应取"
                "公告日当天(vs 公告日前一交易日收盘)；公告前位置=一年区间分位；未回补=向上断层其后"
                "最低价未跌破、向下断层其后最高价未涨回公告前收盘；个股日线 OHLC 使用前复权(qfq)"
                "口径，成交量/成交额保留原始口径。"
            )
        for row in forecasts:
            ts_code = str(row["ts_code"])
            cache = CumCache(income_by_code.get(ts_code, {}))
            if not cache.has_actual_np(prev_cumulative_period(period)):
                income_missing.append(ts_code)
            rec = build_stock_record(row, cache, period, basic, args.min_pchange,
                                     price_reaction=reactions.get(ts_code),
                                     total_mv=mv_map.get(ts_code), mv_asof=mv_asof,
                                     source_info=source_index.get(ts_code))
            if args.positive_only and rec["flags"]["negative_type"]:
                continue
            stocks.append(rec)

    stocks.sort(key=_sort_key)
    ann_cutoff_stock_count = sum(1 for s in stocks if str(s.get("ann_date") or "") == end_ann)
    fetch_stats["income_calls"] = income_calls
    fetch_stats["income_from_cache"] = max(len(forecasts) - income_calls, 0)
    fetch_stats["price_fetch_stocks"] = price_calls
    fetch_stats["mv_asof"] = mv_asof
    notes.append(
        "valuation 为估值参照：年化净利=预告中值÷报告期季数×4(简单年化，未调季节性)；"
        "滚动净利=上年年报实际+本期中值−上年同期实际(季节性对照，次新股基数缺失时为空)；"
        "PE=最新交易日总市值(daily_basic)÷对应净利，年化净利≤0 不给 PE。口径与坑见 methodology §十一。"
    )
    notes.append(
        "industry_summary 按 stock_basic 行业口径做确定性计数(方向家数/增速中位/断层计数/大票样本)，"
        "只是产业结构综述的原料；宏观分组与结构判断由模型完成，行业口径本身不当结论用。"
    )

    payload = {
        "meta": {
            "period": period,
            "period_label": period_label(period),
            "latest_quarter": f"Q{quarter_of(period)[1]}",
            "ann_window": [start_ann, end_ann],
            "ann_cutoff": end_ann,
            "ann_cutoff_stock_count": ann_cutoff_stock_count,
            "clock_timezone": "Asia/Shanghai",
            "generated_at": run_now.isoformat(timespec="seconds"),
            "unique_stocks": len(stocks),
            "positive_types": sorted(POSITIVE_TYPES),
            "negative_types": sorted(NEGATIVE_TYPES),
            "screen": {"min_pchange": args.min_pchange, "positive_only": args.positive_only},
            "fetch_stats": fetch_stats,
            "income_missing": income_missing,
            "data_notes": notes,
            "primary_source": "cninfo",
            "tushare_role": "reconciliation_and_exception_fallback",
        },
        "industry_summary": build_industry_summary(stocks),
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
        "ann_cutoff": end_ann,
        "ann_cutoff_stock_count": ann_cutoff_stock_count,
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
