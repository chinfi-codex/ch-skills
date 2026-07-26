#!/usr/bin/env python3
"""Full-market official quarterly/interim/annual report scan → deterministic evidence.

Pipeline
--------
1. **Discovery** — `disclosure_date(end_date=period)` is the release calendar:
   one call returns every A-share's 预约披露日 and, once filed, `actual_date`.
   Stocks whose `actual_date` has arrived are the released universe.
2. **Statements (API first)** — for each newly-released code, one *range* call per
   endpoint (income / fina_indicator / cashflow / balancesheet) returns the
   current period plus every base period the decomposition needs, so four calls
   cover a stock for good. Rows land in `qreport_fin_cache` keyed by
   (ts_code, cumulative period).
3. **Reference values** — `forecast(ann_date=…)`日扫 + `express(period=…)` give the
   预告/快报 the report is measured against (兑现度).
4. **Prices** — daily bars are fetched **full-market by trade_date** (one call per
   trading day, not per stock) and de-adjusted to qfq, so the 断层 read is
   available for every released stock rather than only the screened ones.
5. **Derivation** — cumulative → single-quarter decomposition for revenue /
   归母 / 扣非 / 经营现金流, margin and expense-ratio deltas, balance-sheet
   forward signals, 兑现度, valuation, price reaction, industry aggregates and a
   mechanical screen.

Everything this script emits is deterministic: decomposition arithmetic, ratio
deltas, threshold hits, counts. Which company is actually good, whether the
margin expansion is durable, what the contract-liability build means — the model
decides that from the evidence, and writes it to the verdict ledger.

Outputs
-------
- ``reports/qreport_scan_<period>.json``     decision pack (meta + industry
  aggregates + the screened shortlist in full detail)
- ``reports/qreport_universe_<period>.json`` one compact row per released stock,
  for lookups and HTML rendering — never meant to be read whole into context.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from store import Store, qfq_adjust_bars  # noqa: E402
from tushare_client import TushareProxy, get_tushare_pro  # noqa: E402

BEIJING_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")

# --------------------------------------------------------------------------- #
# Period arithmetic
# --------------------------------------------------------------------------- #
QUARTER_END = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}


def beijing_now() -> dt.datetime:
    return dt.datetime.now(BEIJING_TZ)


def beijing_today() -> dt.date:
    return beijing_now().date()


def quarter_of(period: str) -> Tuple[int, int]:
    if not re.fullmatch(r"\d{8}", str(period)):
        raise ValueError(f"period must be a quarter end (YYYYMMDD): {period}")
    year = int(period[:4])
    mmdd = period[4:]
    for q, end in QUARTER_END.items():
        if end == mmdd:
            return year, q
    raise ValueError(f"period must be a quarter end (YYYYMMDD): {period}")


def period_end(year: int, quarter: int) -> str:
    return f"{year}{QUARTER_END[quarter]}"


def prev_cumulative_period(period: str) -> Optional[str]:
    """Previous cumulative period inside the same fiscal year (None for Q1)."""
    year, q = quarter_of(period)
    return None if q == 1 else period_end(year, q - 1)


def prev_year_period(period: str) -> str:
    year, q = quarter_of(period)
    return period_end(year - 1, q)


def latest_quarter_end(today: dt.date) -> str:
    q = (today.month - 1) // 3
    if q == 0:
        return period_end(today.year - 1, 4)
    return period_end(today.year, q)


def period_label(period: str) -> str:
    year, q = quarter_of(period)
    return {1: f"{year}Q1", 2: f"{year}H1", 3: f"{year}Q3", 4: f"{year}年报"}[q]


def quarters_elapsed(period: str) -> int:
    return quarter_of(period)[1]


def needed_periods(period: str) -> List[str]:
    """Every cumulative period the derivation reads, current period included."""
    year, q = quarter_of(period)
    out = {period, prev_year_period(period), period_end(year - 1, 4)}
    prev_cum = prev_cumulative_period(period)
    if prev_cum:
        out.add(prev_cum)
        out.add(prev_year_period(prev_cum))
    else:
        # Q1's previous single quarter is last year's Q4 = annual − Q3 cumulative.
        out.add(period_end(year - 1, 3))
    # Last year's Q3 also backs the prior-year annual single-quarter comparison.
    out.add(period_end(year - 1, 3))
    return sorted(out)


def fetch_range(period: str, end_ann: str) -> Tuple[str, str]:
    """Date range covering every needed period in one call per endpoint.

    Careful: `income` / `balancesheet` / `cashflow` filter `start_date`/`end_date`
    against the **announcement** date, while `fina_indicator` filters against the
    report period. Passing `end_date=period` therefore silently drops the current
    report (filed weeks *after* the period end) from three of the four endpoints
    while fina_indicator returns it — which looks like a partial-data bug rather
    than a query bug. One wide announcement window satisfies both semantics: the
    earliest period the derivation needs is last year's Q1, filed no earlier than
    that April, so starting at last-January-1 is safe under either reading.
    """
    year = int(period[:4])
    return f"{year - 1}0101", end_ann


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #
YI = 1e8  # 元 → 亿元


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def to_yi(v: Any) -> Optional[float]:
    x = _f(v)
    return None if x is None else round(x / YI, 4)


def r2(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 2)


def growth_pct(cur: Optional[float], base: Optional[float]) -> Tuple[Optional[float], str]:
    """Percent change with an explicit reason when it is not computable.

    A negative or zero base makes the percentage meaningless (a swing from
    −1亿 to +1亿 is not "−200%"), so the caller is told to talk in absolute
    terms instead. This is the same guard the earnings-forecast skill uses.
    """
    if cur is None and base is None:
        return None, "cur_missing"
    if cur is None:
        return None, "cur_missing"
    if base is None:
        return None, "base_missing"
    if base <= 0:
        return None, "base_nonpositive"
    return round((cur / base - 1.0) * 100.0, 2), "ok"


def diff_pp(cur: Optional[float], base: Optional[float]) -> Optional[float]:
    """Percentage-point delta between two already-percentage figures."""
    if cur is None or base is None:
        return None
    return round(cur - base, 2)


def ratio_pct(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or den is None or den == 0:
        return None
    return round(num / den * 100.0, 2)


def median_of(values: Iterable[Any]) -> Optional[float]:
    vals = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(statistics.median(vals), 2) if vals else None


def shift_ymd(ymd: str, days: int) -> str:
    return (dt.datetime.strptime(ymd, "%Y%m%d") + dt.timedelta(days=days)).strftime("%Y%m%d")


# --------------------------------------------------------------------------- #
# Statement field sets
# --------------------------------------------------------------------------- #
INCOME_FIELDS = (
    "ts_code,ann_date,f_ann_date,end_date,report_type,update_flag,total_revenue,revenue,"
    "oper_cost,sell_exp,admin_exp,rd_exp,fin_exp,operate_profit,total_profit,income_tax,"
    "n_income,n_income_attr_p,invest_income,fv_value_chg_gain,non_oper_income,non_oper_exp"
)
FINA_FIELDS = (
    "ts_code,ann_date,end_date,update_flag,profit_dedt,grossprofit_margin,netprofit_margin,"
    "roe,roe_dt,roe_waa,ar_turn,assets_turn,ocfps,eps,bps,saleexp_to_gr,adminexp_of_gr"
)
CASHFLOW_FIELDS = (
    "ts_code,ann_date,end_date,report_type,update_flag,n_cashflow_act,c_fr_sale_sg,"
    "c_pay_acq_const_fiolta,n_cashflow_inv_act"
)
BALANCE_FIELDS = (
    "ts_code,ann_date,end_date,report_type,update_flag,total_assets,total_liab,"
    "total_hldr_eqy_exc_min_int,money_cap,accounts_receiv_bill,accounts_receiv,notes_receiv,"
    "inventories,contract_liab,adv_receipts,cip_total,cip,fix_assets_total,fix_assets,"
    "goodwill,st_borr,lt_borr,non_cur_liab_due_1y,intan_assets"
)

# data_json keys we keep per period, grouped by the endpoint that supplies them.
_KEEP = {
    "income": ["total_revenue", "revenue", "oper_cost", "sell_exp", "admin_exp", "rd_exp",
               "fin_exp", "operate_profit", "total_profit", "income_tax", "n_income",
               "n_income_attr_p", "invest_income", "fv_value_chg_gain", "non_oper_income",
               "non_oper_exp"],
    "fina": ["profit_dedt", "grossprofit_margin", "netprofit_margin", "roe", "roe_dt",
             "roe_waa", "ar_turn", "assets_turn", "ocfps", "eps", "bps",
             "saleexp_to_gr", "adminexp_of_gr"],
    "cashflow": ["n_cashflow_act", "c_fr_sale_sg", "c_pay_acq_const_fiolta", "n_cashflow_inv_act"],
    "balance": ["total_assets", "total_liab", "total_hldr_eqy_exc_min_int", "money_cap",
                "accounts_receiv_bill", "accounts_receiv", "notes_receiv", "inventories",
                "contract_liab", "adv_receipts", "cip_total", "cip", "fix_assets_total",
                "fix_assets", "goodwill", "st_borr", "lt_borr", "non_cur_liab_due_1y",
                "intan_assets"],
}
_ENDPOINTS = [
    ("income", "income", INCOME_FIELDS),
    ("fina", "fina_indicator", FINA_FIELDS),
    ("cashflow", "cashflow", CASHFLOW_FIELDS),
    ("balance", "balancesheet", BALANCE_FIELDS),
]


def _pick_rows(df: pd.DataFrame, wanted: Set[str], keep: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Latest consolidated row per end_date.

    Tushare returns several rows per period: the original filing, restatements
    (`update_flag=1`) and, for income/cashflow, parent-only or single-quarter
    variants (`report_type != 1`). Keeping the wrong one silently corrupts every
    derived number, so filter to consolidated and let the updated filing win.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if df is None or df.empty:
        return out
    if "report_type" in df.columns:
        df = df[df["report_type"].astype(str).isin(("1", "1.0"))]
    ranked: Dict[str, Tuple[Tuple[int, str], Dict[str, Any]]] = {}
    for row in df.to_dict("records"):
        end = str(row.get("end_date") or "")
        if end not in wanted:
            continue
        rank = (1 if str(row.get("update_flag") or "0") in ("1", "1.0") else 0,
                str(row.get("ann_date") or ""))
        if end in ranked and ranked[end][0] >= rank:
            continue
        rec = {k: _f(row.get(k)) for k in keep}
        rec["ann_date"] = str(row.get("ann_date") or "") or None
        ranked[end] = (rank, rec)
    for end, (_, rec) in ranked.items():
        out[end] = rec
    return out


def fetch_statements_for_code(pro: TushareProxy, ts_code: str, period: str, end_ann: str,
                              wanted: Set[str], sources_needed: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """One range call per endpoint → {period: {field: value, '_sources': {...}}}."""
    start, end = fetch_range(period, end_ann)
    merged: Dict[str, Dict[str, Any]] = {}
    for src, api, fields in _ENDPOINTS:
        if src not in sources_needed:
            continue
        try:
            df = getattr(pro, api)(ts_code=ts_code, start_date=start, end_date=end, fields=fields)
        except Exception:  # noqa: BLE001 - one endpoint failing must not lose the rest
            continue
        for p, rec in _pick_rows(df, wanted, _KEEP[src]).items():
            slot = merged.setdefault(p, {"_sources": set()})
            ann = rec.pop("ann_date", None)
            # The income statement carries the report's disclosure date; the other
            # endpoints are republished on their own cadence and must not overwrite it.
            if src == "income" or not slot.get("ann_date"):
                slot["ann_date"] = ann or slot.get("ann_date")
            slot.update({k: v for k, v in rec.items() if v is not None})
            slot["_sources"].add(src)
    return merged


def merge_statement_periods(existing: Dict[str, Dict[str, Any]],
                            fetched: Dict[str, Dict[str, Any]],
                            replace: bool = False) -> Dict[str, Dict[str, Any]]:
    """Merge a refetch without erasing fields from temporarily lagging endpoints."""
    out = {} if replace else {p: dict(row) for p, row in existing.items()}
    for period, row in fetched.items():
        if replace or period not in out:
            out[period] = dict(row)
            continue
        previous = out[period]
        merged = {**previous, **{k: v for k, v in row.items() if v is not None}}
        merged["_sources"] = set(previous.get("_sources") or set()) | set(
            row.get("_sources") or set())
        out[period] = merged
    return out


# --------------------------------------------------------------------------- #
# Per-stock series view over the cached statement rows
# --------------------------------------------------------------------------- #
class Series:
    """Cumulative statement rows for one stock, with single-quarter derivation."""

    def __init__(self, by_period: Dict[str, Dict[str, Any]]):
        self.by_period = by_period or {}

    def cum(self, period: Optional[str], field: str) -> Optional[float]:
        if not period:
            return None
        row = self.by_period.get(period) or {}
        value = row.get(field)
        if field == "revenue" and value is None:
            value = row.get("total_revenue")
        return _f(value)

    def has(self, period: Optional[str]) -> bool:
        return bool(period) and period in self.by_period

    def single(self, period: Optional[str], field: str) -> Optional[float]:
        """Single-quarter figure: cumulative(P) − cumulative(previous cumulative).

        Q1's cumulative *is* its single quarter. For any other quarter a missing
        previous cumulative makes the single quarter underivable — returning
        cumulative would silently overstate it.
        """
        if not period:
            return None
        cur = self.cum(period, field)
        if cur is None:
            return None
        prev_cum = prev_cumulative_period(period)
        if prev_cum is None:
            return cur
        prev = self.cum(prev_cum, field)
        return None if prev is None else round(cur - prev, 4)

    def prev_single(self, period: str, field: str) -> Optional[float]:
        """The single quarter immediately before `period` (crosses year ends)."""
        prev_cum = prev_cumulative_period(period)
        if prev_cum:
            return self.single(prev_cum, field)
        year, _ = quarter_of(period)
        annual = self.cum(period_end(year - 1, 4), field)
        q3 = self.cum(period_end(year - 1, 3), field)
        if annual is None or q3 is None:
            return None
        return round(annual - q3, 4)


def growth_block(series: Series, period: str, field: str) -> Dict[str, Any]:
    """Cumulative / single-quarter / sequential growth for one line item."""
    yoy_period = prev_year_period(period)
    cum_cur = series.cum(period, field)
    cum_base = series.cum(yoy_period, field)
    cum_yoy, cum_note = growth_pct(cum_cur, cum_base)

    sq_cur = series.single(period, field)
    sq_base = series.single(yoy_period, field)
    sq_yoy, sq_note = growth_pct(sq_cur, sq_base)

    sq_prev = series.prev_single(period, field)
    qoq, qoq_note = growth_pct(sq_cur, sq_prev)

    return {
        "cum_yi": to_yi(cum_cur), "cum_base_yi": to_yi(cum_base),
        "cum_yoy_pct": cum_yoy, "cum_note": cum_note,
        "single_q_yi": to_yi(sq_cur), "single_q_base_yi": to_yi(sq_base),
        "single_q_yoy_pct": sq_yoy, "single_q_note": sq_note,
        "prev_single_q_yi": to_yi(sq_prev), "qoq_pct": qoq, "qoq_note": qoq_note,
    }


def margin_block(series: Series, period: str) -> Dict[str, Any]:
    """Gross / net margin and expense ratios, cumulative and single-quarter.

    Single-quarter margins come from differenced cumulative revenue and cost, not
    from `fina_indicator` — that endpoint only publishes cumulative ratios, and a
    cumulative gross margin hides exactly the quarter-on-quarter turn this skill
    is looking for.
    """
    yoy = prev_year_period(period)

    def gm(p: Optional[str], single: bool) -> Optional[float]:
        if not p:
            return None
        rev = series.single(p, "revenue") if single else series.cum(p, "revenue")
        cost = series.single(p, "oper_cost") if single else series.cum(p, "oper_cost")
        if rev is None or cost is None or rev <= 0:
            return None
        return round((rev - cost) / rev * 100.0, 2)

    def gm_prev_quarter() -> Optional[float]:
        """Gross margin of the quarter immediately before `period` — for Q1 that
        is last year's Q4, so it cannot go through the same-year path."""
        rev = series.prev_single(period, "revenue")
        cost = series.prev_single(period, "oper_cost")
        if rev is None or cost is None or rev <= 0:
            return None
        return round((rev - cost) / rev * 100.0, 2)

    def nm(p: Optional[str], single: bool) -> Optional[float]:
        if not p:
            return None
        rev = series.single(p, "revenue") if single else series.cum(p, "revenue")
        np_ = series.single(p, "n_income_attr_p") if single else series.cum(p, "n_income_attr_p")
        if rev is None or np_ is None or rev <= 0:
            return None
        return round(np_ / rev * 100.0, 2)

    gm_cum, gm_cum_base = gm(period, False), gm(yoy, False)
    gm_sq, gm_sq_base = gm(period, True), gm(yoy, True)
    gm_sq_prev = gm_prev_quarter()

    exp_ratio: Dict[str, Any] = {}
    rev_cum = series.cum(period, "revenue")
    rev_base = series.cum(yoy, "revenue")
    for key, field in (("sell", "sell_exp"), ("admin", "admin_exp"), ("rd", "rd_exp"), ("fin", "fin_exp")):
        cur = ratio_pct(series.cum(period, field), rev_cum)
        base = ratio_pct(series.cum(yoy, field), rev_base)
        exp_ratio[f"{key}_pct"] = cur
        exp_ratio[f"{key}_yoy_pp"] = diff_pp(cur, base)

    return {
        "gross_margin_cum_pct": gm_cum, "gross_margin_cum_yoy_pp": diff_pp(gm_cum, gm_cum_base),
        "gross_margin_single_pct": gm_sq,
        "gross_margin_single_yoy_pp": diff_pp(gm_sq, gm_sq_base),
        "gross_margin_single_qoq_pp": diff_pp(gm_sq, gm_sq_prev),
        "net_margin_cum_pct": nm(period, False),
        "net_margin_cum_yoy_pp": diff_pp(nm(period, False), nm(yoy, False)),
        "net_margin_single_pct": nm(period, True),
        "net_margin_single_yoy_pp": diff_pp(nm(period, True), nm(yoy, True)),
        "expense_ratio": exp_ratio,
        "rd_exp_yi": to_yi(series.cum(period, "rd_exp")),
        "rd_yoy_pct": growth_pct(series.cum(period, "rd_exp"), series.cum(yoy, "rd_exp"))[0],
        "note": "毛利率/净利率单季口径由累计差分自算；金融类公司无 oper_cost 时为 null",
    }


def quality_block(series: Series, period: str) -> Dict[str, Any]:
    """扣非含金量 + 现金流验证 + 回报率. The two hardest quality tests a real
    report can answer and a forecast cannot: how much of the profit survives the
    非经常性损益 line, and how much of it arrived as cash."""
    np_cum = series.cum(period, "n_income_attr_p")
    dedt_cum = series.cum(period, "profit_dedt")
    ocf_cum = series.cum(period, "n_cashflow_act")
    rev_cum = series.cum(period, "total_revenue") or series.cum(period, "revenue")
    cash_sales = series.cum(period, "c_fr_sale_sg")

    dedt_ratio = ratio_pct(dedt_cum, np_cum) if (np_cum or 0) > 0 else None
    non_recurring_yi = None
    if np_cum is not None and dedt_cum is not None:
        non_recurring_yi = to_yi(np_cum - dedt_cum)

    return {
        "dedt_cum_yi": to_yi(dedt_cum),
        "dedt_ratio_pct": dedt_ratio,
        "non_recurring_yi": non_recurring_yi,
        "dedt_note": ("ok" if dedt_cum is not None else "dedt_missing"),
        "ocf_cum_yi": to_yi(ocf_cum),
        "ocf_single_q_yi": to_yi(series.single(period, "n_cashflow_act")),
        "ocf_to_np_pct": ratio_pct(ocf_cum, np_cum) if (np_cum or 0) > 0 else None,
        "cash_sales_to_revenue_pct": ratio_pct(cash_sales, rev_cum),
        "capex_cum_yi": to_yi(series.cum(period, "c_pay_acq_const_fiolta")),
        "roe_cum_pct": r2(series.cum(period, "roe")),
        "roe_dt_cum_pct": r2(series.cum(period, "roe_dt")),
        "roe_annualized_pct": (r2(series.cum(period, "roe") * 4 / quarters_elapsed(period))
                               if series.cum(period, "roe") is not None else None),
        "assets_turn": r2(series.cum(period, "assets_turn")),
        "ar_turn": r2(series.cum(period, "ar_turn")),
        "note": ("ROE 为累计口径未年化；roe_annualized_pct 是 ÷季数×4 的简单年化，"
                 "未调季节性。经营现金流为累计，单季由差分得出。"),
    }


def balance_block(series: Series, period: str, rev_cum_yoy: Optional[float]) -> Dict[str, Any]:
    """Balance-sheet forward signals — the part of a real report that talks about
    the *next* quarters rather than the one just closed."""
    yoy = prev_year_period(period)
    # Balance-sheet items are point-in-time, so "sequential" means the previous
    # balance-sheet date — which for Q1 is last year's annual, not a same-year
    # quarter.
    year, _ = quarter_of(period)
    prev_bs = prev_cumulative_period(period) or period_end(year - 1, 4)

    def block(field: str, fallback: Optional[str] = None) -> Dict[str, Any]:
        def get(p: Optional[str]) -> Optional[float]:
            if not p:
                return None
            v = series.cum(p, field)
            return v if v is not None else (series.cum(p, fallback) if fallback else None)
        cur, base, prev = get(period), get(yoy), get(prev_bs)
        return {
            "yi": to_yi(cur),
            "yoy_pct": growth_pct(cur, base)[0],
            "qoq_pct": growth_pct(cur, prev)[0],
        }

    contract = block("contract_liab", "adv_receipts")
    receiv = block("accounts_receiv_bill", "accounts_receiv")
    inventory = block("inventories")
    cip = block("cip_total", "cip")
    fixed = block("fix_assets_total", "fix_assets")

    total_assets = series.cum(period, "total_assets")
    total_liab = series.cum(period, "total_liab")
    equity = series.cum(period, "total_hldr_eqy_exc_min_int")
    goodwill = series.cum(period, "goodwill")
    net_cash = None
    money = series.cum(period, "money_cap")
    debt = sum(v for v in (series.cum(period, "st_borr"), series.cum(period, "lt_borr"),
                           series.cum(period, "non_cur_liab_due_1y")) if v is not None)
    if money is not None:
        net_cash = to_yi(money - debt)

    return {
        "contract_liab": contract,
        "receivables": receiv,
        "inventories": inventory,
        "cip": cip,
        "fixed_assets": fixed,
        "goodwill_yi": to_yi(goodwill),
        "goodwill_to_equity_pct": ratio_pct(goodwill, equity),
        "debt_ratio_pct": ratio_pct(total_liab, total_assets),
        "net_cash_yi": net_cash,
        # A receivable or inventory book growing materially faster than revenue is
        # the classic tell that the revenue was booked but not really sold-through.
        "receivable_vs_revenue_gap_pp": diff_pp(receiv["yoy_pct"], rev_cum_yoy),
        "inventory_vs_revenue_gap_pp": diff_pp(inventory["yoy_pct"], rev_cum_yoy),
        "note": ("合同负债缺失时回落到预收款项；应收含票据口径优先 accounts_receiv_bill。"
                 "gap_pp = 该科目同比 − 营收累计同比，正值越大越要追问收入质量。"),
    }


def valuation_block(series: Series, period: str, total_mv_wan: Optional[float],
                    mv_asof: Optional[str], pe_ttm_market: Optional[float]) -> Dict[str, Any]:
    """PE on reported profit. Two denominators, both stated explicitly.

    Annualised = this period's pace ×4/quarters — no seasonality adjustment, so a
    Q1-heavy or H2-heavy business is systematically mispriced by it. TTM =
    trailing four quarters, which digests seasonality and is the honest headline
    once a real report exists (unlike a forecast, where TTM has to be estimated).
    """
    year, q = quarter_of(period)
    mv_yi = to_yi((total_mv_wan or 0) * 1e4) if total_mv_wan is not None else None
    factor = 4.0 / q

    def ttm(field: str) -> Optional[float]:
        cur = series.cum(period, field)
        annual = series.cum(period_end(year - 1, 4), field)
        base = series.cum(prev_year_period(period), field)
        if q == 4:
            return cur
        if cur is None or annual is None or base is None:
            return None
        return round(annual + cur - base, 4)

    def pe(profit: Optional[float]) -> Tuple[Optional[float], str]:
        if mv_yi is None:
            return None, "mv_missing"
        if profit is None:
            return None, "np_missing"
        if profit <= 0:
            return None, "np_nonpositive"
        return round(mv_yi / profit, 2), "ok"

    np_ann = series.cum(period, "n_income_attr_p")
    dedt_ann = series.cum(period, "profit_dedt")
    np_ann_yi = to_yi(np_ann * factor) if np_ann is not None else None
    dedt_ann_yi = to_yi(dedt_ann * factor) if dedt_ann is not None else None
    np_ttm_yi, dedt_ttm_yi = to_yi(ttm("n_income_attr_p")), to_yi(ttm("profit_dedt"))

    pe_ann_np, note_ann_np = pe(np_ann_yi)
    pe_ann_dedt, note_ann_dedt = pe(dedt_ann_yi)
    pe_ttm_np, note_ttm_np = pe(np_ttm_yi)
    pe_ttm_dedt, note_ttm_dedt = pe(dedt_ttm_yi)

    return {
        "total_mv_yi": mv_yi, "mv_asof": mv_asof,
        "annualize_label": {1: "×4", 2: "×2", 3: "×4/3", 4: "×1"}[q],
        "np_annualized_yi": np_ann_yi, "dedt_annualized_yi": dedt_ann_yi,
        "np_ttm_yi": np_ttm_yi, "dedt_ttm_yi": dedt_ttm_yi,
        "pe_annualized_np": pe_ann_np, "pe_annualized_np_note": note_ann_np,
        "pe_annualized_dedt": pe_ann_dedt, "pe_annualized_dedt_note": note_ann_dedt,
        "pe_ttm_np": pe_ttm_np, "pe_ttm_np_note": note_ttm_np,
        "pe_ttm_dedt": pe_ttm_dedt, "pe_ttm_dedt_note": note_ttm_dedt,
        "pe_ttm_market": r2(pe_ttm_market),
        "note": ("头条口径 = 扣非TTM PE；年化PE 未调季节性，淡旺季分明的行业会被系统性高/低估。"
                 "pe_ttm_market 是 daily_basic 的市场口径 PE-TTM，用于交叉核对。"),
    }


def fulfillment_block(series: Series, period: str,
                      refs: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """How the reported number landed against the company's own forecast/快报.

    This is the one signal that only exists once the real report is out, and it
    doubles as a backtest of the earnings-forecast skill's own tiering.
    """
    if not refs:
        return None
    np_cum = series.cum(period, "n_income_attr_p")
    rev_cum = series.cum(period, "revenue")
    out: Dict[str, Any] = {}

    fc = refs.get("forecast")
    if fc:
        lo, hi = _f(fc.get("np_min")), _f(fc.get("np_max"))
        lo_yuan = lo * 1e4 if lo is not None else None
        hi_yuan = hi * 1e4 if hi is not None else None
        mid = None
        if lo_yuan is not None and hi_yuan is not None:
            mid = (lo_yuan + hi_yuan) / 2.0
        elif lo_yuan is not None or hi_yuan is not None:
            mid = lo_yuan if lo_yuan is not None else hi_yuan
        verdict, position, vs_mid = None, None, None
        if np_cum is not None and lo_yuan is not None and hi_yuan is not None:
            if np_cum > hi_yuan:
                verdict = "above"
            elif np_cum < lo_yuan:
                verdict = "below"
            else:
                verdict = "within"
            if hi_yuan != lo_yuan:
                position = round((np_cum - lo_yuan) / (hi_yuan - lo_yuan), 3)
        if np_cum is not None and mid not in (None, 0):
            vs_mid = round((np_cum / mid - 1.0) * 100.0, 2) if mid > 0 else None
        out["forecast"] = {
            "type": fc.get("type"), "ann_date": fc.get("ann_date"),
            "np_min_yi": to_yi(lo_yuan), "np_max_yi": to_yi(hi_yuan), "np_median_yi": to_yi(mid),
            "actual_np_yi": to_yi(np_cum),
            "in_range": verdict, "range_position": position, "vs_median_pct": vs_mid,
            "change_reason": fc.get("change_reason"),
        }

    ex = refs.get("express")
    if ex:
        ex_np = _f(ex.get("np_min"))  # express stores its single value in np_min
        ex_rev = _f(ex.get("revenue"))
        out["express"] = {
            "ann_date": ex.get("ann_date"),
            "np_yi": to_yi(ex_np), "revenue_yi": to_yi(ex_rev),
            "np_vs_final_pct": (round((ex_np / np_cum - 1.0) * 100.0, 2)
                                if ex_np is not None and (np_cum or 0) > 0 else None),
            "revenue_vs_final_pct": (round((ex_rev / rev_cum - 1.0) * 100.0, 2)
                                     if ex_rev is not None and (rev_cum or 0) > 0 else None),
        }
    return out or None


# --------------------------------------------------------------------------- #
# Price reaction (业绩公布后的股价断层)
# --------------------------------------------------------------------------- #
def compute_reaction(bars: List[Dict[str, Any]], anchor_ann: str, gap_min: float) -> Dict[str, Any]:
    """Gap / reaction-day / follow-through metrics anchored on the filing date.

    Periodic reports are filed after the close, so the market's answer lands on
    the next trading session; if the filing date itself is a trading day with an
    open, that day is the reaction day.
    """
    empty = {
        "anchor_ann_date": anchor_ann, "reaction_date": None, "gap_open_pct": None,
        "r_day_pct": None, "r_vol_ratio": None, "pre_close": None, "pre_pos_pct": None,
        "pre_pos_bars": None, "pre_mom_20d_pct": None, "since_ann_pct": None,
        "gap_dir": None, "gap_status": "none", "trading_days_since_r": None,
    }
    if not bars or not anchor_ann:
        return empty
    dates = [b["trade_date"] for b in bars]
    idx = None
    for i, d in enumerate(dates):
        if d >= anchor_ann:
            idx = i
            break
    if idx is None:
        return {**empty, "gap_status": "pending"}
    if idx == 0:
        return {**empty, "gap_status": "pending"}
    pre = bars[idx - 1]
    r = bars[idx]
    pre_close = _f(pre.get("close"))
    open_ = _f(r.get("open"))
    if not pre_close or not open_:
        return empty
    gap = round((open_ / pre_close - 1.0) * 100.0, 2)
    close_r = _f(r.get("close"))
    r_day = round((close_r / pre_close - 1.0) * 100.0, 2) if close_r else None

    prior_vols = [_f(b.get("vol")) or 0.0 for b in bars[max(0, idx - 20):idx]]
    avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0.0
    vol_ratio = round((_f(r.get("vol")) or 0.0) / avg_vol, 2) if avg_vol > 0 else None

    window = bars[:idx]
    highs = [_f(b.get("high")) for b in window if _f(b.get("high"))]
    lows = [_f(b.get("low")) for b in window if _f(b.get("low"))]
    pre_pos = None
    if highs and lows and max(highs) > min(lows):
        pre_pos = round((pre_close - min(lows)) / (max(highs) - min(lows)) * 100.0, 1)
    mom20 = None
    if idx >= 21:
        base = _f(bars[idx - 21].get("close"))
        if base:
            mom20 = round((pre_close / base - 1.0) * 100.0, 2)

    last_close = _f(bars[-1].get("close"))
    since = round((last_close / pre_close - 1.0) * 100.0, 2) if last_close else None

    gap_dir = "up" if gap >= gap_min else ("down" if gap <= -gap_min else None)
    if gap_dir is None:
        status = "none"
    elif gap_dir == "up":
        status = "filled" if any(
            (_f(b.get("low")) if _f(b.get("low")) is not None else _f(b.get("close")) or 0)
            <= pre_close for b in bars[idx:]) else "intact"
    else:
        status = "filled" if any(
            (_f(b.get("high")) if _f(b.get("high")) is not None else _f(b.get("close")) or 0)
            >= pre_close for b in bars[idx:]) else "intact"

    return {
        "anchor_ann_date": anchor_ann, "reaction_date": r["trade_date"],
        "gap_open_pct": gap, "r_day_pct": r_day, "r_vol_ratio": vol_ratio,
        "pre_close": r2(pre_close), "pre_pos_pct": pre_pos, "pre_pos_bars": len(window),
        "pre_mom_20d_pct": mom20, "since_ann_pct": since,
        "gap_dir": gap_dir, "gap_status": status,
        "trading_days_since_r": len(bars) - idx - 1,
    }


def fetch_market_bars(pro: TushareProxy, store: Store, start: str, end: str,
                      notes: List[str], workers: int = 4) -> Dict[str, List[Dict[str, Any]]]:
    """Full-market qfq daily bars over [start, end], one call per trading day.

    Per-stock `pro_bar` would be thousands of calls; `daily(trade_date=…)` plus
    `adj_factor(trade_date=…)` is two calls per trading day for the whole market,
    which is what makes 断层 affordable for every released stock instead of only
    the screened ones.
    """
    try:
        cal = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
        days = sorted(str(d) for d in cal["cal_date"].tolist())
    except Exception as exc:  # noqa: BLE001
        notes.append(f"trade_cal 获取失败，跳过股价断层：{str(exc)[:80]}")
        return {}

    have_days = store.cached_bar_dates(start, end)
    todo = [d for d in days if d not in have_days]

    def one_day(day: str) -> Tuple[str, List[Dict[str, Any]]]:
        px = pro.daily(trade_date=day, fields="ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount")
        adj = pro.adj_factor(trade_date=day, fields="ts_code,adj_factor")
        if px is None or px.empty:
            return day, []
        merged = px.merge(adj, on="ts_code", how="left") if adj is not None and not adj.empty else px
        recs = []
        for row in merged.to_dict("records"):
            recs.append({
                "ts_code": row["ts_code"], "trade_date": str(row["trade_date"]),
                "open": _f(row.get("open")), "high": _f(row.get("high")), "low": _f(row.get("low")),
                "close": _f(row.get("close")), "pre_close": _f(row.get("pre_close")),
                "pct_chg": _f(row.get("pct_chg")), "vol": _f(row.get("vol")),
                "amount": _f(row.get("amount")), "adj_factor": _f(row.get("adj_factor")),
            })
        return day, recs

    fetched: List[Dict[str, Any]] = []
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for fut in as_completed([pool.submit(one_day, d) for d in todo]):
                try:
                    _, recs = fut.result()
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"日线抓取失败：{str(exc)[:60]}")
                    continue
                fetched.extend(recs)

    # Persist raw prices + factor. qfq is derived over the complete cached series
    # on read so a later dividend cannot leave old/new cache slices on different
    # adjustment bases.
    if fetched:
        store.upsert_bars_many(fetched)
        if not store.available:  # no DB → hand the in-memory rows back
            out: Dict[str, List[Dict[str, Any]]] = {}
            for r in fetched:
                out.setdefault(r["ts_code"], []).append(r)
            return {
                code: qfq_adjust_bars(sorted(rows, key=lambda b: b["trade_date"]))
                for code, rows in out.items()
            }
    return {}  # persisted; the caller reads them back per code from the store


# --------------------------------------------------------------------------- #
# Mechanical screen (funnel only — not a quality verdict)
# --------------------------------------------------------------------------- #
def screen_block(growth: Dict[str, Dict[str, Any]], quality: Dict[str, Any],
                 margins: Dict[str, Any], balance: Dict[str, Any],
                 reaction: Dict[str, Any], fulfillment: Optional[Dict[str, Any]],
                 thresholds: Dict[str, float]) -> Dict[str, Any]:
    """Threshold hits + a transparent rank score.

    The whole market files a report every quarter, so something has to narrow
    5,000+ rows to a set a model can actually read. That is *all* this does: each
    hit is one documented threshold on one deterministic number, and the score is
    the count of hits in four buckets. It deliberately does not weigh 'is this a
    good company' — that judgement is the model's, written to the verdict ledger.
    """
    hits: List[str] = []
    rev, npf, dedt = growth["revenue"], growth["np"], growth["dedt"]

    if (rev.get("single_q_yoy_pct") or -999) >= thresholds["rev_yoy"]:
        hits.append("rev_single_yoy_ge")
    if (npf.get("single_q_yoy_pct") or -999) >= thresholds["np_yoy"]:
        hits.append("np_single_yoy_ge")
    if (dedt.get("single_q_yoy_pct") or -999) >= thresholds["np_yoy"]:
        hits.append("dedt_single_yoy_ge")
    if (npf.get("single_q_yoy_pct") is not None and npf.get("cum_yoy_pct") is not None
            and npf["single_q_yoy_pct"] > npf["cum_yoy_pct"]):
        hits.append("np_accelerating")
    if (rev.get("single_q_yoy_pct") is not None and rev.get("cum_yoy_pct") is not None
            and rev["single_q_yoy_pct"] > rev["cum_yoy_pct"]):
        hits.append("rev_accelerating")
    if (npf.get("qoq_pct") or -999) > 0:
        hits.append("np_qoq_positive")

    if (quality.get("dedt_ratio_pct") or -999) >= thresholds["dedt_ratio"]:
        hits.append("dedt_clean")
    if (quality.get("ocf_to_np_pct") or -999) >= thresholds["ocf_ratio"]:
        hits.append("ocf_backed")
    if quality.get("ocf_cum_yi") is not None and quality["ocf_cum_yi"] < 0 and (npf.get("cum_yi") or 0) > 0:
        hits.append("ocf_negative_while_profitable")
    if (quality.get("roe_annualized_pct") or -999) >= thresholds["roe"]:
        hits.append("roe_ge")

    if (margins.get("gross_margin_single_yoy_pp") or -999) >= thresholds["margin_pp"]:
        hits.append("margin_expanding")
    if (margins.get("gross_margin_single_yoy_pp") or 999) <= -thresholds["margin_pp"]:
        hits.append("margin_compressing")

    if (balance["contract_liab"].get("yoy_pct") or -999) >= thresholds["orderbook"]:
        hits.append("orderbook_building")
    if (balance["cip"].get("yoy_pct") or -999) >= thresholds["capex"]:
        hits.append("capex_cycle")
    if (balance.get("receivable_vs_revenue_gap_pp") or -999) >= thresholds["gap_pp"]:
        hits.append("receivable_outpacing_revenue")
    if (balance.get("inventory_vs_revenue_gap_pp") or -999) >= thresholds["gap_pp"]:
        hits.append("inventory_outpacing_revenue")

    if (npf.get("cum_yi") or 0) < 0:
        hits.append("loss_making")
    if (rev.get("single_q_yoy_pct") or 0) < 0:
        hits.append("revenue_declining")

    if reaction.get("gap_dir") == "up":
        hits.append("gap_up")
    if reaction.get("gap_dir") == "down":
        hits.append("gap_down")
    if fulfillment and (fulfillment.get("forecast") or {}).get("in_range") == "above":
        hits.append("beat_forecast")
    if fulfillment and (fulfillment.get("forecast") or {}).get("in_range") == "below":
        hits.append("miss_forecast")

    hit_set = set(hits)
    growth_score = sum(k in hit_set for k in
                       ("rev_single_yoy_ge", "np_single_yoy_ge", "dedt_single_yoy_ge",
                        "np_accelerating", "rev_accelerating", "np_qoq_positive"))
    quality_score = sum(k in hit_set for k in ("dedt_clean", "ocf_backed", "roe_ge"))
    edge_score = sum(k in hit_set for k in ("margin_expanding", "orderbook_building", "capex_cycle"))
    market_score = sum(k in hit_set for k in ("gap_up", "beat_forecast"))
    penalty = sum(k in hit_set for k in
                  ("ocf_negative_while_profitable", "margin_compressing",
                   "receivable_outpacing_revenue", "inventory_outpacing_revenue",
                   "loss_making", "revenue_declining", "gap_down", "miss_forecast"))

    return {
        "hits": hits,
        "growth_score": growth_score, "quality_score": quality_score,
        "edge_score": edge_score, "market_score": market_score, "penalty": penalty,
        "rank_score": growth_score * 2 + quality_score * 2 + edge_score + market_score - penalty,
        "note": "机械阈值命中与计数，仅用于把全市场收敛成模型可读的候选池，不是优秀度结论。",
    }


# --------------------------------------------------------------------------- #
# Reference values (forecast / express) for 兑现度
# --------------------------------------------------------------------------- #
def scan_reference_values(pro: TushareProxy, store: Store, period: str, end_ann: str,
                          refetch_days: int, notes: List[str],
                          workers: int = 4) -> List[Dict[str, Any]]:
    """Populate `qreport_forecast_ref` with 业绩预告 (day-scanned) and 业绩快报.

    `forecast` rejects a period-only query, so it is scanned by announcement day
    the way the earnings-forecast skill does; `express` accepts `period=` and is
    a single call.
    """
    start_ann = shift_ymd(period, -75)
    days = []
    d0 = dt.datetime.strptime(start_ann, "%Y%m%d").date()
    d1 = dt.datetime.strptime(end_ann, "%Y%m%d").date()
    while d0 <= d1:
        days.append(d0.strftime("%Y%m%d"))
        d0 += dt.timedelta(days=1)
    done = store.logged_forecast_ann_dates(period)
    cutoff = shift_ymd(end_ann, -max(0, refetch_days))
    todo = [d for d in days if d not in done or d >= cutoff]

    def one(day: str) -> Tuple[str, List[Dict[str, Any]]]:
        df = pro.forecast(ann_date=day, fields=(
            "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,"
            "net_profit_max,summary,change_reason"))
        rows = []
        if df is not None and not df.empty:
            for row in df.to_dict("records"):
                if str(row.get("end_date")) != period:
                    continue
                rows.append({
                    "ts_code": row["ts_code"], "period": period, "kind": "forecast",
                    "ann_date": str(row.get("ann_date") or ""), "type": row.get("type"),
                    "np_min": _f(row.get("net_profit_min")), "np_max": _f(row.get("net_profit_max")),
                    "p_change_min": _f(row.get("p_change_min")), "p_change_max": _f(row.get("p_change_max")),
                    "revenue": None, "summary": row.get("summary"),
                    "change_reason": row.get("change_reason"),
                })
        return day, rows

    day_counts: Dict[str, int] = {}
    batch: List[Dict[str, Any]] = []
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for fut in as_completed([pool.submit(one, d) for d in todo]):
                try:
                    day, rows = fut.result()
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"预告参照抓取失败：{str(exc)[:60]}")
                    continue
                day_counts[day] = len(rows)
                batch.extend(rows)
    latest: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in batch:
        key = (str(row["ts_code"]), str(row["period"]), str(row["kind"]))
        prev = latest.get(key)
        if prev is None or str(row.get("ann_date") or "") > str(prev.get("ann_date") or ""):
            latest[key] = row
    batch = list(latest.values())
    if store.upsert_forecast_ref(batch):
        store.record_forecast_fetch_days(period, day_counts)

    try:
        ex = pro.express(period=period, fields="ts_code,ann_date,end_date,revenue,n_income,yoy_net_profit")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"业绩快报抓取失败：{str(exc)[:60]}")
        return batch
    if ex is None or ex.empty:
        return batch
    rows = []
    for row in ex.to_dict("records"):
        if str(row.get("end_date")) != period:
            continue
        rows.append({
            "ts_code": row["ts_code"], "period": period, "kind": "express",
            "ann_date": str(row.get("ann_date") or ""), "type": "业绩快报",
            # express reports a single value; it rides in np_min so the ref table
            # stays one shape for both kinds.
            "np_min": _f(row.get("n_income")), "np_max": _f(row.get("n_income")),
            "p_change_min": _f(row.get("yoy_net_profit")), "p_change_max": _f(row.get("yoy_net_profit")),
            "revenue": _f(row.get("revenue")), "summary": None, "change_reason": None,
        })
    store.upsert_forecast_ref(rows)
    return batch + rows


# --------------------------------------------------------------------------- #
# CNInfo provenance (title + original PDF link; PDFs themselves stay on demand)
# --------------------------------------------------------------------------- #
def scan_cninfo_provenance(store: Store, period: str, end_ann: str, refetch_days: int,
                           notes: List[str]) -> List[Dict[str, Any]]:
    """Bulk-scan the official report category so every covered stock has a
    traceable filing link. Numbers still come from Tushare — this is provenance,
    plus the entry point `report_pdf.py` uses when a PDF is actually needed."""
    import cninfo_client as cn

    start = period
    days: List[str] = []
    d0 = dt.datetime.strptime(start, "%Y%m%d").date()
    d1 = dt.datetime.strptime(end_ann, "%Y%m%d").date()
    while d0 <= d1:
        days.append(d0.strftime("%Y%m%d"))
        d0 += dt.timedelta(days=1)
    done = store.logged_cninfo_ann_dates(period)
    cutoff = shift_ymd(end_ann, -max(0, refetch_days))
    todo = [d for d in days if d not in done or d >= cutoff]
    if not todo:
        return []

    total = 0
    day_counts: Dict[str, int] = {}
    batch: List[Dict[str, Any]] = []
    for day in todo:
        se = f"{day[:4]}-{day[4:6]}-{day[6:]}~{day[:4]}-{day[4:6]}-{day[6:]}"
        try:
            rows = cn.list_report_announcements(period, se)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"CNInfo {day} 公告枚举失败：{str(exc)[:60]}")
            continue
        day_counts[day] = len(rows)
        batch.extend(rows)
        total += len(rows)
    if store.upsert_cninfo_announcements(period, batch):
        store.record_cninfo_fetch_days(period, day_counts)
    return batch


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #
def build_industry_summary(rows: List[Dict[str, Any]], top_members: int = 5) -> List[Dict[str, Any]]:
    """Per-industry counts and medians over the **whole released universe**.

    Industry here is only a grouping of raw material; the macro grouping and the
    structural read are the model's job (see references/methodology.md §七).
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(r.get("industry") or "未分类", []).append(r)
    out = []
    for industry, members in sorted(buckets.items()):
        hits = [set(m.get("hits") or []) for m in members]
        sample = sorted(members, key=lambda m: (m.get("np_cum_yi") or -1e9), reverse=True)[:top_members]
        out.append({
            "industry": industry,
            "n": len(members),
            "growth_n": sum(1 for m in members if (m.get("np_single_yoy_pct") or -999) > 0),
            "decline_n": sum(1 for m in members if (m.get("np_single_yoy_pct") or 999) < 0),
            "loss_n": sum(1 for h in hits if "loss_making" in h),
            "accelerating_n": sum(1 for h in hits if "np_accelerating" in h),
            "margin_up_n": sum(1 for h in hits if "margin_expanding" in h),
            "margin_down_n": sum(1 for h in hits if "margin_compressing" in h),
            "ocf_backed_n": sum(1 for h in hits if "ocf_backed" in h),
            "orderbook_n": sum(1 for h in hits if "orderbook_building" in h),
            "gap_up_n": sum(1 for h in hits if "gap_up" in h),
            "gap_down_n": sum(1 for h in hits if "gap_down" in h),
            "rev_cum_yoy_median": median_of(m.get("rev_cum_yoy_pct") for m in members),
            "np_cum_yoy_median": median_of(m.get("np_cum_yoy_pct") for m in members),
            "np_single_yoy_median": median_of(m.get("np_single_yoy_pct") for m in members),
            "gross_margin_yoy_pp_median": median_of(m.get("gross_margin_single_yoy_pp") for m in members),
            "members_sample": [{
                "ts_code": m["ts_code"], "name": m.get("name"),
                "np_cum_yi": m.get("np_cum_yi"),
                "np_single_yoy_pct": m.get("np_single_yoy_pct"),
                "rev_single_yoy_pct": m.get("rev_single_yoy_pct"),
            } for m in sample],
        })
    return sorted(out, key=lambda x: x["n"], reverse=True)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    today = beijing_today()
    ap = argparse.ArgumentParser(description="A股正式季报/半年报/年报全市场扫描（API 优先）")
    ap.add_argument("--period", default=latest_quarter_end(today), help="报告期末 YYYYMMDD（季度末）")
    ap.add_argument("--end-ann", default=None, help="披露截止日（北京时间），默认今天+1")
    ap.add_argument("--codes", default=None, help="只扫这些股票（逗号分隔），用于定向核验")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只已披露股票（调试用）")
    ap.add_argument("--top", type=int, default=200, help="决策包 stocks[] 保留的候选数")
    ap.add_argument("--min-rank", type=int, default=None, help="按 rank_score 下限截断候选（优先于 --top）")
    ap.add_argument("--fetch-workers", type=int, default=8, help="报表取数并发（限流时调低）")
    ap.add_argument("--refetch-days", type=int, default=3, help="最近 N 个日历日内披露的股票始终重取")
    ap.add_argument("--refresh-fin", action="store_true", help="忽略缓存重取全部财报（追溯调整时用）")
    ap.add_argument("--no-cache", action="store_true", help="本次不落库、全量抓取")
    ap.add_argument("--no-price", action="store_true", help="跳过股价断层与 K 线")
    # ~380 calendar days ≈ a full year of sessions before the earliest filing, so
    # `pre_pos_pct` really is a one-year position percentile. Shorter windows
    # still work — `pre_pos_bars` reports what the percentile was measured over.
    ap.add_argument("--price-lookback", type=int, default=380, help="股价窗口起点＝最早披露日往前 N 个日历日")
    ap.add_argument("--gap-min", type=float, default=2.0, help="跳空幅度 ≥ N%% 记为断层")
    ap.add_argument("--no-cninfo", action="store_true", help="跳过 CNInfo 公告溯源（不影响数值）")
    ap.add_argument("--require-ann-cutoff", default=None, help="断言披露截止日与该值一致，否则非零退出")
    ap.add_argument("--out", default=None, help="决策包输出路径")
    ap.add_argument("--universe-out", default=None, help="全样本紧凑表输出路径")
    ap.add_argument("--stdout", action="store_true", help="同时打印决策包 JSON")
    # Screen thresholds are flags on purpose: the funnel's cut lines are an
    # operator decision, not something the script should hard-code as truth.
    ap.add_argument("--th-rev-yoy", type=float, default=15.0)
    ap.add_argument("--th-np-yoy", type=float, default=30.0)
    ap.add_argument("--th-dedt-ratio", type=float, default=80.0)
    ap.add_argument("--th-ocf-ratio", type=float, default=60.0)
    ap.add_argument("--th-roe", type=float, default=10.0)
    ap.add_argument("--th-margin-pp", type=float, default=1.0)
    ap.add_argument("--th-orderbook", type=float, default=20.0)
    ap.add_argument("--th-capex", type=float, default=30.0)
    ap.add_argument("--th-gap-pp", type=float, default=20.0)
    args = ap.parse_args(argv)

    period = args.period
    quarter_of(period)  # validates
    end_ann = args.end_ann or (today + dt.timedelta(days=1)).strftime("%Y%m%d")
    if not re.fullmatch(r"\d{8}", end_ann):
        raise ValueError(f"end-ann must be YYYYMMDD: {end_ann}")
    dt.datetime.strptime(end_ann, "%Y%m%d")
    if end_ann < period:
        raise ValueError("end-ann cannot be earlier than the report period")
    if args.require_ann_cutoff and args.require_ann_cutoff != end_ann:
        print(f"[gate] ann cutoff mismatch: got {end_ann}, required {args.require_ann_cutoff}", file=sys.stderr)
        return 2

    notes: List[str] = []
    thresholds = {
        "rev_yoy": args.th_rev_yoy, "np_yoy": args.th_np_yoy, "dedt_ratio": args.th_dedt_ratio,
        "ocf_ratio": args.th_ocf_ratio, "roe": args.th_roe, "margin_pp": args.th_margin_pp,
        "orderbook": args.th_orderbook, "capex": args.th_capex, "gap_pp": args.th_gap_pp,
    }

    pro = TushareProxy(get_tushare_pro())
    store = Store(enabled=not args.no_cache)
    if not store.available:
        notes.append(f"缓存不可用，本次全量抓取：{store.reason}")

    # -- 1. discovery ------------------------------------------------------
    try:
        disc = pro.disclosure_date(end_date=period)
    except Exception as exc:  # noqa: BLE001
        print(f"disclosure_date 获取失败：{exc}", file=sys.stderr)
        return 1
    released: Dict[str, Dict[str, Any]] = {}
    disc_rows = []
    for row in (disc.to_dict("records") if disc is not None and not disc.empty else []):
        code = str(row.get("ts_code") or "")
        actual = str(row.get("actual_date") or "").strip()
        pre = str(row.get("pre_date") or "").strip()
        disc_rows.append({"ts_code": code, "pre_date": pre or None,
                          "actual_date": actual or None, "ann_date": str(row.get("ann_date") or "") or None})
        if len(actual) == 8 and actual <= end_ann:
            # A code can appear twice (rescheduled); keep the earliest filing.
            prev = released.get(code)
            if prev is None or actual < prev["actual_date"]:
                released[code] = {"ts_code": code, "actual_date": actual, "pre_date": pre or None}
    store.upsert_disclosure(period, disc_rows)
    released_total = len(released)

    if args.codes:
        wanted_codes = {c.strip().upper() for c in args.codes.split(",") if c.strip()}
        released = {k: v for k, v in released.items() if k in wanted_codes}
        for c in wanted_codes - set(released):
            notes.append(f"{c} 在 {period} 尚无实际披露日，未纳入")
    codes = sorted(released)
    if args.limit:
        codes = codes[:args.limit]
        released = {c: released[c] for c in codes}
    if not codes:
        notes.append("该报告期尚无公司正式披露")

    # -- 2. statements -----------------------------------------------------
    wanted = set(needed_periods(period))
    all_sources = [s for s, _, _ in _ENDPOINTS]
    if args.refresh_fin:
        store.delete_fin_periods(codes, sorted(wanted))
    cached_fin = {} if args.refresh_fin else store.load_fin_many(codes)
    refetch_cutoff = shift_ymd(end_ann, -max(0, args.refetch_days))

    def needs_fetch(code: str) -> List[str]:
        if args.refresh_fin:
            return all_sources
        have = cached_fin.get(code) or {}
        cur = have.get(period) or {}
        missing = [s for s in all_sources if s not in (cur.get("_sources") or set())]
        base_missing = [p for p in wanted if p not in have]
        if released[code]["actual_date"] >= refetch_cutoff:
            # Freshly filed: fina_indicator and the cash-flow statement routinely
            # trail the income statement by a day or two, and restatements land
            # in the same window.
            return all_sources
        if missing or base_missing:
            return all_sources
        return []

    todo = {c: needs_fetch(c) for c in codes}
    todo = {c: s for c, s in todo.items() if s}
    fetch_stats = {"released": len(codes), "fetched": len(todo), "from_cache": len(codes) - len(todo)}

    if todo:
        def work(code: str) -> Tuple[str, Dict[str, Dict[str, Any]]]:
            return code, fetch_statements_for_code(pro, code, period, end_ann, wanted, todo[code])
        write_batch: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max(1, args.fetch_workers)) as pool:
            futures = [pool.submit(work, c) for c in todo]
            for i, fut in enumerate(as_completed(futures), 1):
                try:
                    code, merged = fut.result()
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"财报抓取失败：{str(exc)[:80]}")
                    continue
                cached_fin[code] = merge_statement_periods(
                    cached_fin.get(code) or {}, merged, replace=args.refresh_fin)
                for p in merged:
                    rec = cached_fin[code][p]
                    write_batch.append({"ts_code": code, "period": p, "ann_date": rec.get("ann_date"),
                                        "sources": sorted(rec.get("_sources") or []), "data": rec})
                if len(write_batch) >= 4000:
                    store.upsert_fin_many(write_batch)
                    write_batch = []
                if i % 500 == 0:
                    print(f"  … 财报取数 {i}/{len(todo)}", file=sys.stderr)
        store.upsert_fin_many(write_batch)

    # -- 3. reference values ------------------------------------------------
    fresh_refs = scan_reference_values(
        pro, store, period, end_ann, args.refetch_days, notes,
        workers=min(4, args.fetch_workers))
    refs = store.load_forecast_ref(period)
    for row in fresh_refs:
        slot = refs.setdefault(str(row["ts_code"]), {})
        kind = str(row["kind"])
        previous = slot.get(kind)
        if previous is None or str(row.get("ann_date") or "") >= str(
                previous.get("ann_date") or ""):
            slot[kind] = row

    # -- 4. basics + market cap --------------------------------------------
    basic = store.load_basic(codes)
    if len(basic) < len(codes):
        try:
            df = pro.stock_basic(exchange="", list_status="L",
                                 fields="ts_code,name,industry,area,market")
            fresh = {r["ts_code"]: {"name": r.get("name") or "", "industry": r.get("industry") or "",
                                    "area": r.get("area") or "", "market": r.get("market") or ""}
                     for r in df.to_dict("records")}
            store.upsert_basic(fresh)
            basic = {**fresh, **basic}  # cached rows win: they may cover delisted codes
        except Exception as exc:  # noqa: BLE001
            notes.append(f"stock_basic 获取失败：{str(exc)[:60]}")

    total_mv: Dict[str, float] = {}
    pe_market: Dict[str, float] = {}
    mv_asof: Optional[str] = None
    for back in range(0, 10):
        day = (today - dt.timedelta(days=back)).strftime("%Y%m%d")
        try:
            df = pro.daily_basic(trade_date=day, fields="ts_code,total_mv,pe_ttm")
        except Exception:  # noqa: BLE001
            continue
        if df is not None and not df.empty:
            total_mv = {r["ts_code"]: _f(r.get("total_mv")) for r in df.to_dict("records")}
            pe_market = {r["ts_code"]: _f(r.get("pe_ttm")) for r in df.to_dict("records")}
            mv_asof = day
            break
    if not total_mv:
        notes.append("未取到最新总市值，PE 一律留空")

    # -- 5. prices ----------------------------------------------------------
    bars_by_code: Dict[str, List[Dict[str, Any]]] = {}
    price_note = None
    if not args.no_price and codes:
        ann_min = min(released[c]["actual_date"] for c in codes)
        p_start = shift_ymd(ann_min, -abs(args.price_lookback))
        p_end = today.strftime("%Y%m%d")
        in_mem = fetch_market_bars(pro, store, p_start, p_end, notes,
                                   workers=min(4, args.fetch_workers))
        bars_by_code = in_mem or store.load_bars_many(codes)
        price_note = f"日线窗口 {p_start}~{p_end}（全市场按交易日抓取，前复权）"
    elif args.no_price:
        price_note = "已跳过股价断层（--no-price）"

    # -- 6. provenance ------------------------------------------------------
    ann_index: Dict[str, Dict[str, Any]] = {}
    if not args.no_cninfo:
        fresh_ann = scan_cninfo_provenance(
            store, period, end_ann, args.refetch_days, notes)
        persisted_ann = store.load_cninfo_announcements(period)
        for r in persisted_ann + fresh_ann:
            prev = ann_index.get(str(r["ts_code"]))
            # Corrected filings supersede the original; otherwise the latest wins.
            rank = (1 if r.get("is_corrected") else 0, str(r.get("ann_date") or ""))
            prev_rank = ((1 if prev and prev.get("is_corrected") else 0),
                         str((prev or {}).get("ann_date") or ""))
            if prev is None or rank > prev_rank:
                ann_index[str(r["ts_code"])] = r

    # -- 7. derive ----------------------------------------------------------
    stocks: List[Dict[str, Any]] = []
    universe: List[Dict[str, Any]] = []
    missing_current = 0
    incomplete = 0

    for code in codes:
        by_period = cached_fin.get(code) or {}
        if period not in by_period:
            missing_current += 1
            continue
        series = Series(by_period)
        info = basic.get(code, {})
        ann_date = (by_period[period].get("ann_date") or released[code]["actual_date"])

        growth = {
            "revenue": growth_block(series, period, "revenue"),
            "np": growth_block(series, period, "n_income_attr_p"),
            "dedt": growth_block(series, period, "profit_dedt"),
            "ocf": growth_block(series, period, "n_cashflow_act"),
        }
        margins = margin_block(series, period)
        quality = quality_block(series, period)
        balance = balance_block(series, period, growth["revenue"].get("cum_yoy_pct"))
        valuation = valuation_block(series, period, total_mv.get(code), mv_asof, pe_market.get(code))
        fulfillment = fulfillment_block(series, period, refs.get(code) or {})
        reaction = (compute_reaction(bars_by_code.get(code) or [], ann_date, args.gap_min)
                    if bars_by_code else {"gap_dir": None, "gap_status": "none",
                                          "anchor_ann_date": ann_date, "note": "未计算股价反应"})
        screen = screen_block(growth, quality, margins, balance, reaction, fulfillment, thresholds)

        ann = ann_index.get(code)
        have_sources = sorted(by_period[period].get("_sources") or [])
        missing_sources = [s for s in all_sources if s not in have_sources]
        # The income statement is the spine: without it there is no revenue, no
        # 归母, no single-quarter anything. fina_indicator often lands first, so a
        # record can exist and still be hollow — say so instead of shipping nulls.
        if "income" in missing_sources:
            incomplete += 1
        if not info.get("name") and ann:
            # stock_basic misses very recent listings (BSE especially); the
            # filing itself carries the company name.
            info = {**info, "name": (ann.get("name") or "").strip() or None}
        record = {
            "ts_code": code, "name": info.get("name"), "industry": info.get("industry"),
            "area": info.get("area"), "market": info.get("market"),
            "period": period, "period_label": period_label(period),
            "ann_date": ann_date, "pre_date": released[code].get("pre_date"),
            "source": {
                "authority": "tushare_statements",
                "sources": have_sources,
                "missing_sources": missing_sources,
                "statements_complete": not missing_sources,
                "income_loaded": "income" in have_sources,
                "cninfo_title": (ann or {}).get("title"),
                "cninfo_url": (ann or {}).get("url"),
                "cninfo_is_corrected": bool((ann or {}).get("is_corrected")),
            },
            "growth": growth, "margins": margins, "quality": quality,
            "balance_signals": balance, "valuation": valuation,
            "fulfillment": fulfillment, "price_reaction": reaction, "screen": screen,
        }
        stocks.append(record)
        universe.append({
            "ts_code": code, "name": info.get("name"), "industry": info.get("industry"),
            "ann_date": ann_date,
            "rev_cum_yi": growth["revenue"].get("cum_yi"),
            "rev_cum_yoy_pct": growth["revenue"].get("cum_yoy_pct"),
            "rev_single_yoy_pct": growth["revenue"].get("single_q_yoy_pct"),
            "np_cum_yi": growth["np"].get("cum_yi"),
            "np_cum_yoy_pct": growth["np"].get("cum_yoy_pct"),
            "np_single_yoy_pct": growth["np"].get("single_q_yoy_pct"),
            "np_qoq_pct": growth["np"].get("qoq_pct"),
            "dedt_cum_yi": quality.get("dedt_cum_yi"),
            "dedt_ratio_pct": quality.get("dedt_ratio_pct"),
            "ocf_to_np_pct": quality.get("ocf_to_np_pct"),
            "gross_margin_single_pct": margins.get("gross_margin_single_pct"),
            "gross_margin_single_yoy_pp": margins.get("gross_margin_single_yoy_pp"),
            "roe_annualized_pct": quality.get("roe_annualized_pct"),
            "contract_liab_yoy_pct": balance["contract_liab"].get("yoy_pct"),
            "pe_ttm_dedt": valuation.get("pe_ttm_dedt"),
            "total_mv_yi": valuation.get("total_mv_yi"),
            "gap_dir": reaction.get("gap_dir"), "gap_open_pct": reaction.get("gap_open_pct"),
            "gap_status": reaction.get("gap_status"),
            "in_range": ((fulfillment or {}).get("forecast") or {}).get("in_range"),
            "hits": screen["hits"], "rank_score": screen["rank_score"],
            "income_loaded": "income" in have_sources,
        })

    if missing_current:
        notes.append(f"{missing_current} 家已披露但 Tushare 尚未收录本期报表（上游滞后，下次重跑自动补）")
    if incomplete:
        notes.append(
            f"{incomplete} 家本期利润表尚未上线（只到 fina_indicator 等），增长与质量字段为空——"
            "看 source.missing_sources，别把空值当成零增长；下次重跑自动补")
    unnamed = [s["ts_code"] for s in stocks if not s.get("name")]
    if unnamed:
        # Typically a brand-new listing: Tushare stock_basic has not picked it up
        # and there is no periodic-report filing to borrow the name from (its
        # figures come from the prospectus).
        notes.append(
            f"{len(unnamed)} 只基础信息上游未收录（名称/行业为空，多为刚上市新股）："
            f"{', '.join(unnamed[:8])}——报告里按代码称呼并说明，不要显示 null")

    industry_summary = build_industry_summary(universe)

    stocks.sort(key=lambda s: (-(s["screen"]["rank_score"]),
                               -((s["growth"]["np"].get("single_q_yoy_pct")) or -1e9)))
    if args.min_rank is not None:
        shortlist = [s for s in stocks if s["screen"]["rank_score"] >= args.min_rank]
    else:
        shortlist = stocks[:max(1, args.top)] if stocks else []
    shortlist_codes = {s["ts_code"] for s in shortlist}
    for u in universe:
        u["shortlisted"] = u["ts_code"] in shortlist_codes

    cutoff_count = sum(1 for c in codes if released[c]["actual_date"] == end_ann)
    scheduled_codes = {str(row.get("ts_code") or "") for row in disc_rows
                       if row.get("ts_code")}
    run_scope = "codes" if args.codes else ("limit" if args.limit else "full")
    meta = {
        "period": period, "period_label": period_label(period),
        "quarters_elapsed": quarters_elapsed(period),
        "generated_at": beijing_now().isoformat(timespec="seconds"),
        "clock_timezone": "Asia/Shanghai",
        "ann_cutoff": end_ann, "ann_cutoff_stock_count": cutoff_count,
        "released_count": len(codes), "with_statements": len(stocks),
        "statements_incomplete": incomplete,
        "scheduled_total": len(scheduled_codes),
        "disclosure_progress_pct": (
            round(released_total / len(scheduled_codes) * 100, 1)
            if scheduled_codes else None),
        "run_scope": run_scope,
        "primary_source": "tushare_statements",
        "pdf_role": "on_demand_only",
        "cninfo_role": "provenance_and_on_demand_pdf",
        "shortlist_count": len(shortlist),
        "shortlist_rule": (f"rank_score >= {args.min_rank}" if args.min_rank is not None
                           else f"rank_score 前 {args.top}"),
        "thresholds": thresholds,
        "fetch_stats": {**fetch_stats, "cache": "on" if store.available else "off"},
        "price_note": price_note,
        "data_notes": notes,
        "boundary": ("脚本只出确定性证据：拆解、比率、阈值命中、计数。"
                     "优秀与否、质量成色、行业方向由模型判断并写入 qreport_verdict 台账。"),
    }

    scope_suffix = "" if run_scope == "full" else (
        "_subset" if run_scope == "codes" else f"_limit{args.limit}")
    out_path = args.out or os.path.join(
        "reports", f"qreport_scan_{period}{scope_suffix}.json")
    uni_path = args.universe_out or os.path.join(
        "reports", f"qreport_universe_{period}{scope_suffix}.json")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(uni_path) or ".", exist_ok=True)

    pack = {"meta": meta, "industry_summary": industry_summary, "stocks": shortlist}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(pack, fh, ensure_ascii=False, indent=2)
    with open(uni_path, "w", encoding="utf-8") as fh:
        json.dump({"meta": {k: meta[k] for k in
                            ("period", "ann_cutoff", "released_count", "with_statements", "generated_at")},
                   "universe": universe}, fh, ensure_ascii=False, indent=2)

    if args.stdout:
        print(json.dumps(pack, ensure_ascii=False, indent=2))
    print(f"[ok] {period_label(period)} 已披露 {len(codes)} 家 / 出证据 {len(stocks)} 家 / "
          f"候选 {len(shortlist)} 家 → {out_path}", file=sys.stderr)
    print(f"[ok] 全样本紧凑表 → {uni_path}", file=sys.stderr)
    for n in notes:
        print(f"  · {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
