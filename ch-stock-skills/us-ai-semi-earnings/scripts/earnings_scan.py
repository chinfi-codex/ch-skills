#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evidence scan for one calendar quarter of US AI/semiconductor earnings.

Produces a decision pack the model reads to judge who delivered, whether the
profit is real, what the guidance implies, and — the part that only exists
because the universe is one supply chain — whether the story each link tells is
consistent with its neighbours.

Everything here is deterministic: fetch, align, difference, divide, count,
threshold. No line of this file decides whether a quarter was good. The
mechanical `screen.hits` and `rank_score` are a funnel for ordering the read,
explicitly not a verdict; `references/methodology.md` carries the judgement
rules and the model applies them.

Source roles, and why the split is structural rather than incidental:

- **SEC XBRL is the numerical authority.** Free, official, complete, and it tags
  discrete quarters, so single-quarter revenue is looked up rather than
  reconstructed. It arrives with the 10-Q, which is 0–10 days *after* the call.
- **The 8-K EX-99.1 press release is what exists on earnings night.** Revenue,
  EPS and — found nowhere in XBRL, ever — next-quarter guidance. A scan run the
  evening of the report has this and not the statements.
- **The transcript is where the forward-looking substance is**: customer names,
  capacity commitments, the margin bridge, and what the sell side would not let
  go of.
- **Nasdaq's public endpoint supplies EPS surprise** against consensus, free,
  which keeps the whole Alpha Vantage budget available for transcripts.

The two-stage arrival is reported, not smoothed over: `data_stage` on every
company says whether the numbers came from XBRL or only from the press release,
so a report can never imply audited-statement rigour for a number that came off
a press release table three hours after the call.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[2] / "shared"
sys.path.insert(0, str(_BUNDLED_SHARED if (_BUNDLED_SHARED / "yahoo_http").exists() else _DEV_SHARED))

import requests  # noqa: E402
import yaml  # noqa: E402

import sec_client as sec  # noqa: E402
import transcript_fetch as tf  # noqa: E402
from store import Store  # noqa: E402

try:
    from yahoo_http import YahooBlocked, yahoo_get  # type: ignore
    _YAHOO_ERROR: Optional[str] = None
except Exception as exc:  # noqa: BLE001
    YahooBlocked = Exception  # type: ignore
    yahoo_get = None  # type: ignore
    _YAHOO_ERROR = str(exc)

NASDAQ_SURPRISE_URL = "https://api.nasdaq.com/api/company/{ticker}/earnings-surprise"
_UA = {"User-Agent": "Mozilla/5.0"}

# Concepts the scan actually reads. A subset of sec_client.TAG_ALIASES — pulling
# all of them multiplies the row count without adding a metric anyone reads.
SCAN_CONCEPTS = (
    "revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income",
    "eps_diluted", "rnd", "sgna", "operating_expenses", "sbc", "ocf", "capex",
    "inventory", "receivables", "deferred_revenue", "cash", "short_term_investments",
    "total_debt_lt", "ppe_net", "total_assets", "equity", "rpo",
)

# Forward-looking language in a press release. Used only to *locate* candidate
# sentences for the model to read — the numbers inside them are never parsed
# into a structured guidance figure, because deciding which of three numbers in
# a sentence is the midpoint is judgement, not extraction.
# Only forward-looking constructions. Bare period phrases ("for the second
# quarter") were tried and removed: every results paragraph opens with one, so
# they pulled the headline revenue sentence in as if it were guidance.
_GUIDANCE_CUES = (
    "guidance", "outlook", "we expect", "we anticipate", "expects", "anticipates",
    "is expected to", "are expected to", "we are raising", "we are lowering",
    "we now expect", "full year", "full-year", "forecast",
)
_NUMBERISH = re.compile(r"\$?\d[\d,.]*\s*(billion|million|%|bn|m\b)|\$\d", re.I)


# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------

def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def r2(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 2)


def pct_change(cur: Optional[float], base: Optional[float]) -> Tuple[Optional[float], str]:
    """YoY/QoQ in percent, with a note when the base makes the ratio meaningless.

    A negative or zero base is the common trap: "net income grew 350%" off a
    loss is arithmetic, not growth, so those return `base_nonpositive` and the
    report is expected to describe the swing in words instead.
    """
    if cur is None:
        return None, "cur_missing"
    if base is None:
        return None, "base_missing"
    if base == 0:
        return None, "base_zero"
    if base < 0:
        return None, "base_nonpositive"
    return round((cur - base) / abs(base) * 100.0, 1), ""


def ratio_pct(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or not den:
        return None
    return round(num / den * 100.0, 1)


def diff_pp(cur: Optional[float], base: Optional[float]) -> Optional[float]:
    if cur is None or base is None:
        return None
    return round(cur - base, 1)


def to_musd(v: Optional[float]) -> Optional[float]:
    """USD -> millions, the unit the whole evidence pack speaks in."""
    return None if v is None else round(v / 1e6, 1)


# ---------------------------------------------------------------------------
# universe
# ---------------------------------------------------------------------------

def load_universe(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    companies: Dict[str, Dict[str, Any]] = {}
    for bucket, spec in (raw.get("buckets") or {}).items():
        for ticker in spec.get("tickers") or []:
            ticker = str(ticker).strip().upper()
            if not ticker:
                continue
            # A ticker may legitimately sit in two buckets (MRVL is both compute
            # silicon and optical interconnect); the first wins for aggregation
            # and the remaining categories stay available for lookup.
            if ticker in companies:
                companies[ticker]["also_in"].append(bucket)
                continue
            companies[ticker] = {
                "ticker": ticker,
                "bucket": bucket,
                "chain_role": spec.get("chain_role"),
                "bucket_desc": spec.get("desc"),
                "also_in": [],
                "fiscal_year_end_month": (raw.get("fiscal_offsets") or {}).get(ticker),
            }
    return {
        "companies": companies,
        "buckets": raw.get("buckets") or {},
        "meta": raw.get("meta") or {},
    }


# ---------------------------------------------------------------------------
# per-company fetch
# ---------------------------------------------------------------------------

def expected_quarter_end(facts_by_frame: Dict[str, Dict[str, Any]], frame: str) -> Optional[str]:
    """When the target fiscal quarter closed, in YYYY-MM-DD.

    Takes the already-assembled per-frame facts rather than a freshly fetched
    fact table, because those two are not the same thing: on any run after the
    first, XBRL is served from cache and no fact table is built. Reading only
    the fresh table made this return None on every cached run, and the caller
    then fell back to the *calendar* quarter end — which is right for TXN and
    wrong for every off-calendar filer. MU's quarter closed 2026-05-28 and it
    reported on 06-24, so a 06-30 anchor rejected its own earnings 8-K as
    "filed before the quarter ended" and the company silently dropped to a
    consensus-date-only provenance.

    Read straight off the facts once the 10-Q has landed. Before that — the case
    that matters, because it is earnings night — projected from the same frame a
    year earlier plus 364 days. 364 rather than 365 because a 52/53-week filer's
    quarter ends on a fixed weekday, and the extra day would drift the estimate
    by one weekday every year.
    """
    probes = ("revenue", "net_income", "gross_profit", "operating_income")
    for concept in probes:
        row = (facts_by_frame.get(frame) or {}).get(concept) or {}
        if row.get("end"):
            return row["end"]
    prior = sec.prior_year_frame(frame)
    for concept in probes:
        row = (facts_by_frame.get(prior) or {}).get(concept) or {}
        if row.get("end"):
            base = sec._parse_ymd(row["end"])
            if base:
                return (base + dt.timedelta(days=364)).isoformat()
    return None


def match_earnings_8k(filings: Sequence[Dict[str, Any]], quarter_end: Optional[str],
                      *, max_lag_days: int = 95) -> Optional[Dict[str, Any]]:
    """The item-2.02 8-K that reports the quarter ending `quarter_end`.

    Matching on filing date against the quarter end, rather than guessing the
    quarter from the filing date, is what keeps off-calendar filers straight:
    the gap between a quarter closing and the company reporting it ranges from
    about two to eight weeks across this universe, so a fixed offset misassigns
    NVDA by a full quarter.
    """
    if not quarter_end:
        return filings[0] if filings else None
    end = sec._parse_ymd(quarter_end)
    if not end:
        return None
    best: Optional[Tuple[int, Dict[str, Any]]] = None
    for f in filings:
        fd = sec._parse_ymd(f.get("filing_date"))
        if not fd:
            continue
        lag = (fd - end).days
        if 0 <= lag <= max_lag_days and (best is None or lag < best[0]):
            best = (lag, f)
    return best[1] if best else None


def match_statement(filings: Sequence[Dict[str, Any]], quarter_end: Optional[str],
                    *, max_lag_days: int = 95) -> Optional[Dict[str, Any]]:
    """The first 10-Q/10-K filed after this fiscal quarter closed."""
    return match_earnings_8k(filings, quarter_end, max_lag_days=max_lag_days)


def match_foreign_release(filings: Sequence[Dict[str, Any]], reported_date: Optional[str],
                          *, tolerance_days: int = 4) -> Optional[Dict[str, Any]]:
    """The 6-K a foreign private issuer used to publish results.

    ADRs (TSM, ASML, ARM, UMC, STM, ASX) never file an 8-K, so the item-2.02
    discovery that works for domestic filers finds nothing and they look like
    they never reported. They also file 6-Ks constantly — TSM files one for
    monthly revenue — so "the most recent 6-K" is the wrong pick too.

    The anchor is instead the announcement date from Nasdaq's consensus
    endpoint, which is already being fetched for every name and costs nothing:
    the 6-K filed within a few days of it is the results filing.
    """
    if not reported_date:
        return None
    target = sec._parse_ymd(reported_date)
    if not target:
        return None
    near = [(abs((sec._parse_ymd(f["filing_date"]) - target).days), f)
            for f in filings if sec._parse_ymd(f.get("filing_date"))]
    near = [(d, f) for d, f in near if d <= tolerance_days]
    if not near:
        return None
    near.sort(key=lambda t: t[0])
    return near[0][1]


def extract_guidance_excerpts(text: str, *, max_excerpts: int = 14) -> List[Dict[str, Any]]:
    """Sentences in the press release that carry forward-looking numbers.

    Locating, not parsing. The model reads the sentence and decides what the
    guidance is; a regex that tried to pull "the midpoint" out of
    "$10.5 billion to $11.1 billion, plus or minus $300 million" would be
    inventing precision.
    """
    if not text:
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if len(line) < 40 or len(line) > 900:
            continue
        for sentence in re.split(r"(?<=[.;])\s+", line):
            s = sentence.strip()
            if len(s) < 40 or s.lower() in seen:
                continue
            low = s.lower()
            if not any(cue in low for cue in _GUIDANCE_CUES):
                continue
            if not _NUMBERISH.search(s):
                continue
            seen.add(low)
            out.append({"text": s[:600],
                        "cues": [c for c in _GUIDANCE_CUES if c in low][:3]})
            if len(out) >= max_excerpts:
                return out
    return out


def _headline_excerpt(text: str, *, max_chars: int = 1400) -> str:
    """The opening prose of a press release, exhibit boilerplate stripped.

    EDGAR exhibits begin with a few lines of filing furniture (`EX-99`, the
    document filename, `Document`) before the actual release; skipping short
    leading lines lands on the dateline paragraph, which is where every issuer
    states revenue and EPS.
    """
    lines = [ln.strip() for ln in text.split("\n")]
    body: List[str] = []
    for ln in lines:
        if not ln:
            continue
        if not body and (len(ln) < 25 or ln.lower() in ("document", "exhibit 99")
                         or ln.upper().startswith("EX-99")):
            continue
        body.append(ln)
        if sum(len(x) for x in body) >= max_chars:
            break
    return " ".join(body)[:max_chars]


def fetch_surprise(ticker: str) -> List[Dict[str, Any]]:
    """EPS actual vs consensus from Nasdaq's public endpoint.

    Free and keyless, which is the whole point: the Alpha Vantage `EARNINGS`
    function returns the same thing but would spend transcript budget to do it.
    `fiscalQtrEnd` arrives as "Jun 2026", so the frame is derived from the last
    day of that month through the same midpoint rule everything else uses.
    """
    try:
        resp = requests.get(NASDAQ_SURPRISE_URL.format(ticker=ticker.upper()),
                            headers=_UA, timeout=25)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return []
    table = ((payload.get("data") or {}).get("earningsSurpriseTable") or {})
    rows = table.get("rows") or []
    out: List[Dict[str, Any]] = []
    for row in rows:
        qend = str(row.get("fiscalQtrEnd") or "").strip()
        try:
            month = dt.datetime.strptime(qend, "%b %Y")
        except ValueError:
            continue
        nxt = dt.date(month.year + (month.month // 12), (month.month % 12) + 1, 1)
        last_day = (nxt - dt.timedelta(days=1)).isoformat()
        frame = sec.calendar_frame(None, last_day)
        if not frame:
            continue
        reported = row.get("dateReported")
        try:
            reported = dt.datetime.strptime(str(reported), "%m/%d/%Y").date().isoformat()
        except (ValueError, TypeError):
            reported = None
        eps = _f(row.get("eps"))
        est = _f(row.get("consensusForecast"))
        # A percentage surprise divides by consensus, so a consensus near zero
        # produces a headline number that says nothing about the beat's size:
        # Intel missing 0.10 by 0.20 prints as +200%. Flagged rather than
        # suppressed — the underlying cents are still the fact.
        unstable = est is not None and abs(est) < 0.25
        out.append({
            "ticker": ticker.upper(), "frame": frame, "fiscal_date_ending": last_day,
            "reported_date": reported, "eps_reported": eps, "eps_estimated": est,
            "surprise": r2(eps - est) if eps is not None and est is not None else None,
            "surprise_pct": _f(row.get("percentageSurprise")),
            "surprise_pct_unstable": unstable or None,
            "surprise_note": ("consensus EPS is near zero — quote the cents, not the percentage"
                              if unstable else None),
            "source": "nasdaq_public",
        })
    return out


def fetch_bars(ticker: str, *, range_: str = "1y") -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Daily bars from Yahoo's chart endpoint through the shared hardened GET."""
    if yahoo_get is None:
        return [], f"yahoo_http unavailable: {_YAHOO_ERROR}"
    try:
        resp = yahoo_get(f"/v8/finance/chart/{ticker}",
                         {"range": range_, "interval": "1d"})
        payload = resp.json()
    except YahooBlocked as exc:
        return [], f"blocked_by_edge: {str(exc)[:120]}"
    except Exception as exc:  # noqa: BLE001
        return [], f"chart_error: {str(exc)[:120]}"
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return [], "chart_empty"
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    bars: List[Dict[str, Any]] = []
    for i, ts in enumerate(stamps):
        close = _f((quote.get("close") or [None] * len(stamps))[i])
        if close is None:
            continue
        bars.append({
            "date": dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat(),
            "open": _f((quote.get("open") or [None] * len(stamps))[i]),
            "high": _f((quote.get("high") or [None] * len(stamps))[i]),
            "low": _f((quote.get("low") or [None] * len(stamps))[i]),
            "close": close,
            "volume": _f((quote.get("volume") or [None] * len(stamps))[i]),
        })
    return bars, None


def price_history_range(frame_end: Optional[str], today: dt.date) -> str:
    """Smallest Yahoo range that still includes the frame's reporting window."""
    end = sec._parse_ymd(frame_end)
    if not end:
        return "1y"
    age_days = max(0, (today - end).days) + 120
    if age_days <= 366:
        return "1y"
    if age_days <= 2 * 366:
        return "2y"
    if age_days <= 5 * 366:
        return "5y"
    if age_days <= 10 * 366:
        return "10y"
    return "max"


def price_history_window(bars: Sequence[Dict[str, Any]], announce_date: Optional[str],
                         *, limit: int = 120, pre_announce: int = 40) -> List[Dict[str, Any]]:
    """Return a compact OHLCV window that keeps the earnings date visible.

    Current-season reports naturally fall inside the latest 120 sessions. For an
    older frame, anchor the window around the first trading session on or after
    the announcement so the chart still shows both the setup and the aftermath.
    """
    usable = [
        {k: b.get(k) for k in ("date", "open", "high", "low", "close", "volume")}
        for b in bars
        if b.get("date") and all(b.get(k) is not None for k in ("open", "high", "low", "close"))
    ]
    usable.sort(key=lambda b: b["date"])
    if limit <= 0 or len(usable) <= limit:
        return usable
    marker = (
        next((i for i, b in enumerate(usable) if b["date"] >= announce_date), None)
        if announce_date else None
    )
    if marker is None:
        return usable[-limit:]
    latest_start = len(usable) - limit
    start = min(max(0, marker - max(0, pre_announce)), latest_start)
    return usable[start:start + limit]


def price_reaction(bars: Sequence[Dict[str, Any]], announce_date: Optional[str],
                   *, gap_min: float = 2.0) -> Dict[str, Any]:
    """How the stock took it.

    Both the announcement day and the following session are reported rather than
    one "reaction day", because US tech reports both before the open and after
    the close and the 8-K does not say which. Guessing picks the wrong session
    for roughly half the universe; showing both lets the reader see which one
    carries the move.
    """
    out: Dict[str, Any] = {
        "announce_date": announce_date, "same_day_pct": None, "next_day_pct": None,
        "gap_open_pct": None, "gap_dir": None, "gap_status": "none",
        "vol_ratio": None, "position_52w": None, "since_announce_pct": None,
        "trading_days_since": None, "bars_available": len(bars), "note": "",
    }
    if not bars:
        out["note"] = "no_bars"
        return out
    closes = [b["close"] for b in bars[-252:] if b.get("close") is not None]
    if closes:
        lo, hi = min(closes), max(closes)
        if hi > lo:
            out["position_52w"] = round((closes[-1] - lo) / (hi - lo), 3)
    if not announce_date:
        out["note"] = "no_announce_date"
        return out

    idx = next((i for i, b in enumerate(bars) if b["date"] >= announce_date), None)
    if idx is None:
        out["note"] = "announcement_after_last_bar"
        return out
    announce_day = sec._parse_ymd(announce_date)
    first_reaction_day = sec._parse_ymd(bars[idx].get("date"))
    if (announce_day and first_reaction_day
            and (first_reaction_day - announce_day).days > 7):
        out["note"] = "no_bar_near_announcement"
        return out

    def _chg(i: int) -> Optional[float]:
        if i <= 0 or i >= len(bars):
            return None
        prev, cur = bars[i - 1]["close"], bars[i]["close"]
        return round((cur - prev) / prev * 100.0, 2) if prev else None

    out["same_day_pct"] = _chg(idx)
    out["next_day_pct"] = _chg(idx + 1)

    # The gap is measured on whichever of the two sessions actually moved — the
    # one that opened away from the prior close by more than the threshold.
    for i in (idx, idx + 1):
        if i <= 0 or i >= len(bars):
            continue
        prev_close, open_ = bars[i - 1]["close"], bars[i].get("open")
        if not prev_close or open_ is None:
            continue
        gap = round((open_ - prev_close) / prev_close * 100.0, 2)
        if abs(gap) >= gap_min:
            out["gap_open_pct"] = gap
            out["gap_dir"] = "up" if gap > 0 else "down"
            after = bars[i:]
            if gap > 0:
                filled = any(b["low"] is not None and b["low"] <= prev_close for b in after)
            else:
                filled = any(b["high"] is not None and b["high"] >= prev_close for b in after)
            out["gap_status"] = "filled" if filled else "intact"
            break
    else:
        out["gap_status"] = "none"

    vols = [b["volume"] for b in bars[max(0, idx - 20):idx] if b.get("volume")]
    if vols and bars[idx].get("volume"):
        avg = sum(vols) / len(vols)
        out["vol_ratio"] = round(bars[idx]["volume"] / avg, 2) if avg else None

    last = bars[-1]["close"]
    base = bars[idx - 1]["close"] if idx > 0 else None
    if base:
        out["since_announce_pct"] = round((last - base) / base * 100.0, 2)
    out["trading_days_since"] = len(bars) - idx - 1
    if out["trading_days_since"] is not None and out["trading_days_since"] < 3 \
            and out["gap_status"] == "intact":
        out["note"] = "gap_untested: only a few sessions since the report"
    return out


# ---------------------------------------------------------------------------
# metric blocks
# ---------------------------------------------------------------------------

def _val(facts: Dict[str, Dict[str, Any]], concept: str) -> Optional[float]:
    row = facts.get(concept) or {}
    return _f(row.get("val"))


def _unit(facts: Dict[str, Dict[str, Any]], concept: str) -> Optional[str]:
    row = facts.get(concept) or {}
    return str(row.get("unit") or "").strip() or None


def _musd(facts: Dict[str, Dict[str, Any]], concept: str) -> Optional[float]:
    """Return a USD fact in millions; never relabel a local-currency fact."""
    return to_musd(_val(facts, concept)) if _unit(facts, concept) == "USD" else None


def growth_block(cur: Dict[str, Any], yoy: Dict[str, Any], qoq: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for concept in ("revenue", "gross_profit", "operating_income", "net_income",
                    "ocf", "eps_diluted", "rnd", "capex"):
        c, y, q = _val(cur, concept), _val(yoy, concept), _val(qoq, concept)
        yv, ynote = pct_change(c, y)
        qv, qnote = pct_change(c, q)
        row = (cur.get(concept) or {})
        unit = _unit(cur, concept)
        out[concept] = {
            "value_musd": (
                _musd(cur, concept) if concept != "eps_diluted" else r2(c)
            ),
            "value_local_millions": (
                to_musd(c)
                if concept != "eps_diluted" and c is not None and unit not in (None, "USD")
                else None
            ),
            "unit": unit,
            "yoy_pct": yv, "yoy_note": ynote,
            "qoq_pct": qv, "qoq_note": qnote,
            "derived": row.get("derived"),
            "span": f"{row.get('start')}~{row.get('end')}" if row.get("end") else None,
        }
    return out


def margin_block(cur: Dict[str, Any], yoy: Dict[str, Any], qoq: Dict[str, Any]) -> Dict[str, Any]:
    def _margins(f: Dict[str, Any]) -> Dict[str, Optional[float]]:
        rev = _val(f, "revenue")
        gp = _val(f, "gross_profit")
        if gp is None and rev is not None and _val(f, "cost_of_revenue") is not None:
            gp = rev - _val(f, "cost_of_revenue")
        return {
            "gross": ratio_pct(gp, rev),
            "operating": ratio_pct(_val(f, "operating_income"), rev),
            "net": ratio_pct(_val(f, "net_income"), rev),
            "rnd": ratio_pct(_val(f, "rnd"), rev),
            "sgna": ratio_pct(_val(f, "sgna"), rev),
            # Stock comp as a share of revenue has no A-share equivalent and is
            # the main bridge between GAAP and the non-GAAP numbers consensus
            # is set against.
            "sbc": ratio_pct(_val(f, "sbc"), rev),
        }

    c, y, q = _margins(cur), _margins(yoy), _margins(qoq)
    return {
        name: {"pct": c[name], "yoy_pp": diff_pp(c[name], y[name]),
               "qoq_pp": diff_pp(c[name], q[name])}
        for name in c
    }


def quality_block(cur: Dict[str, Any], yoy: Dict[str, Any],
                  rev_yoy_pct: Optional[float]) -> Dict[str, Any]:
    """Whether the profit is backed by cash and not parked in working capital."""
    ni, ocf = _val(cur, "net_income"), _val(cur, "ocf")
    inv_yoy, _ = pct_change(_val(cur, "inventory"), _val(yoy, "inventory"))
    ar_yoy, _ = pct_change(_val(cur, "receivables"), _val(yoy, "receivables"))
    capex = _val(cur, "capex")
    fcf_units_are_usd = _unit(cur, "ocf") == _unit(cur, "capex") == "USD"
    return {
        "ocf_to_net_income_pct": ratio_pct(ocf, ni) if (ni or 0) > 0 else None,
        "ocf_musd": _musd(cur, "ocf"),
        "free_cash_flow_musd": (
            to_musd(ocf - capex)
            if fcf_units_are_usd and ocf is not None and capex is not None else None
        ),
        "inventory_yoy_pct": inv_yoy,
        "receivable_yoy_pct": ar_yoy,
        # Positive gap = the balance sheet grew faster than sales. On its own it
        # is a question, not a verdict: a semi company building for a ramp and
        # one that cannot ship look identical here until the call explains which.
        "inventory_vs_revenue_gap_pp": diff_pp(inv_yoy, rev_yoy_pct),
        "receivable_vs_revenue_gap_pp": diff_pp(ar_yoy, rev_yoy_pct),
        "sbc_musd": _musd(cur, "sbc"),
        "capex_musd": _musd(cur, "capex"),
        "capex_to_revenue_pct": ratio_pct(capex, _val(cur, "revenue")),
    }


def balance_block(cur: Dict[str, Any], yoy: Dict[str, Any], qoq: Dict[str, Any]) -> Dict[str, Any]:
    cash = _val(cur, "cash")
    sti = _val(cur, "short_term_investments")
    debt = _val(cur, "total_debt_lt")
    gross_cash = (cash or 0) + (sti or 0) if (cash is not None or sti is not None) else None
    cash_components = [
        (value, _unit(cur, concept))
        for value, concept in ((cash, "cash"), (sti, "short_term_investments"))
        if value is not None
    ]
    cash_is_usd = bool(cash_components) and all(unit == "USD" for _, unit in cash_components)
    rpo_yoy, _ = pct_change(_val(cur, "rpo"), _val(yoy, "rpo"))
    dr_yoy, _ = pct_change(_val(cur, "deferred_revenue"), _val(yoy, "deferred_revenue"))
    return {
        "cash_and_investments_musd": (
            to_musd(gross_cash) if cash_is_usd else None
        ),
        "long_term_debt_musd": _musd(cur, "total_debt_lt"),
        "net_cash_musd": (
            to_musd(gross_cash - debt)
            if gross_cash is not None and debt is not None
            and cash_is_usd
            and _unit(cur, "total_debt_lt") == "USD" else None
        ),
        "inventory_musd": _musd(cur, "inventory"),
        "inventory_qoq_pct": pct_change(_val(cur, "inventory"), _val(qoq, "inventory"))[0],
        "ppe_net_musd": _musd(cur, "ppe_net"),
        "ppe_yoy_pct": pct_change(_val(cur, "ppe_net"), _val(yoy, "ppe_net"))[0],
        # RPO / deferred revenue is the closest thing a US filer has to an order
        # book. Only some tag it, which is itself informative.
        "rpo_musd": _musd(cur, "rpo"),
        "rpo_yoy_pct": rpo_yoy,
        "deferred_revenue_musd": _musd(cur, "deferred_revenue"),
        "deferred_revenue_yoy_pct": dr_yoy,
        "equity_musd": _musd(cur, "equity"),
    }


def screen_block(growth: Dict[str, Any], margins: Dict[str, Any], quality: Dict[str, Any],
                 surprise: Optional[Dict[str, Any]], reaction: Dict[str, Any],
                 th: Dict[str, float]) -> Dict[str, Any]:
    """Mechanical threshold hits and an ordering score.

    A funnel for deciding what to read first, and nothing more. It cannot see
    guidance, it cannot read the call, and it will happily rank a company that
    beat on a one-off tax benefit above one that raised full-year guidance.
    """
    hits: List[str] = []
    rev_yoy = growth["revenue"]["yoy_pct"]
    ni_yoy = growth["net_income"]["yoy_pct"]
    gm_pp = margins["gross"]["yoy_pp"]
    ocf_ratio = quality["ocf_to_net_income_pct"]
    surprise_pct = (
        None if (surprise or {}).get("surprise_pct_unstable")
        else (surprise or {}).get("surprise_pct")
    )

    if rev_yoy is not None and rev_yoy >= th["rev_yoy"]:
        hits.append("revenue_yoy")
    if (growth["revenue"]["qoq_pct"] or -999) >= th["rev_qoq"]:
        hits.append("revenue_qoq")
    if ni_yoy is not None and ni_yoy >= th["ni_yoy"]:
        hits.append("net_income_yoy")
    if gm_pp is not None and gm_pp >= th["gm_pp"]:
        hits.append("gross_margin_up")
    if ocf_ratio is not None and ocf_ratio >= th["ocf_ratio"]:
        hits.append("cash_backed")
    if surprise_pct is not None and surprise_pct >= th["surprise_pct"]:
        hits.append("eps_beat")
    if (quality["rpo_yoy_pct"] if "rpo_yoy_pct" in quality else None) is not None:
        pass  # rpo lives in balance_block; kept out of the funnel deliberately
    if reaction.get("gap_dir") == "up" and reaction.get("gap_status") == "intact":
        hits.append("gap_up_intact")

    penalty: List[str] = []
    if ocf_ratio is not None and ocf_ratio < th["ocf_ratio_low"]:
        penalty.append("cash_lags_profit")
    if (quality["receivable_vs_revenue_gap_pp"] or 0) >= th["gap_pp"]:
        penalty.append("receivables_outrun_revenue")
    if (quality["inventory_vs_revenue_gap_pp"] or 0) >= th["gap_pp"]:
        penalty.append("inventory_outrun_revenue")
    if gm_pp is not None and gm_pp <= -th["gm_pp"]:
        penalty.append("gross_margin_down")
    if surprise_pct is not None and surprise_pct < 0:
        penalty.append("eps_miss")

    score = 0.0
    score += min(max(rev_yoy or 0, -50), 150) * 0.30
    score += min(max(ni_yoy or 0, -50), 300) * 0.10
    score += (gm_pp or 0) * 2.0
    score += min(max((ocf_ratio or 0) - 100, -100), 100) * 0.10
    score += min(max(surprise_pct or 0, -30), 60) * 0.50
    score -= 8.0 * len(penalty)
    return {"hits": hits, "penalty": penalty, "rank_score": round(score, 1),
            "note": "mechanical funnel only — not an assessment of the quarter"}


# ---------------------------------------------------------------------------
# per-company pipeline
# ---------------------------------------------------------------------------

def scan_company(company: Dict[str, Any], frame: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Everything deterministic that can be said about one company this quarter."""
    ticker = company["ticker"]
    store: Store = ctx["store"]
    out: Dict[str, Any] = {
        "ticker": ticker, "name": company.get("name"), "cik": company.get("cik"),
        "bucket": company["bucket"], "chain_role": company.get("chain_role"),
        "also_in": company.get("also_in") or [],
        "frame": frame, "reported": False, "data_stage": "not_reported",
        "sources": [], "errors": [],
    }
    cik10 = company.get("cik10")
    if not cik10:
        out["errors"].append("no_cik: not in SEC ticker map (ADR or delisted)")
        return out

    # --- filings -----------------------------------------------------------
    try:
        subs = sec.get_submissions(cik10)
    except sec.SecError as exc:
        out["errors"].append(f"submissions_failed: {str(exc)[:140]}")
        return out
    if not subs:
        out["errors"].append("submissions_missing")
        return out
    out["name"] = out["name"] or subs.get("name")
    # SEC's fiscalYearEnd ("0630") is the authoritative fiscal calendar; the
    # YAML fiscal_offsets only need to cover what SEC lacks (ADRs). The
    # transcript path derives Alpha Vantage's fiscal-quarter label from this.
    fye_raw = (subs.get("fiscalYearEnd") or "")
    sec_fye_month = int(fye_raw[:2]) if len(fye_raw) >= 2 and fye_raw[:2].isdigit() else None
    out["fiscal_year_end_month"] = company.get("fiscal_year_end_month") or sec_fye_month
    found = sec.find_earnings_filings(subs, since=ctx["since"], until=ctx["end_date"])

    # --- XBRL --------------------------------------------------------------
    frames_needed = [frame, sec.prior_year_frame(frame), sec.prior_quarter_frame(frame)]
    fact_table: Optional[Dict[str, Any]] = None
    facts_by_frame: Dict[str, Dict[str, Any]] = {}
    cached = store.load_facts([ticker], frames_needed)
    for fr in frames_needed:
        got = cached.get((ticker, fr))
        if got:
            facts_by_frame[fr] = got
            if "sec_xbrl_cache" not in out["sources"]:
                out["sources"].append("sec_xbrl_cache")

    log = ctx["xbrl_log"].get(ticker) or {}
    newest_statement = found["statements"][0]["filing_date"] if found["statements"] else None
    cached_quarter_end = (
        expected_quarter_end(facts_by_frame, frame) or ctx["frame_end_fallback"]
    )
    target_statement = match_statement(found["statements"], cached_quarter_end)
    missing_cached_frame = any((ticker, fr) not in cached for fr in frames_needed)
    stale = (ctx["refresh_xbrl"] or not log
             or (newest_statement and (log.get("latest_filed") or "") < newest_statement)
             or (target_statement is not None and missing_cached_frame))
    if stale:
        try:
            raw_facts = sec.get_companyfacts(cik10)
        except sec.SecError as exc:
            raw_facts = None
            out["errors"].append(f"companyfacts_failed: {str(exc)[:140]}")
        if raw_facts:
            fact_table = sec.build_fact_table(raw_facts, SCAN_CONCEPTS)
            store.save_facts(ticker, fact_table, keep_frames=frames_needed +
                             [sec.prior_year_frame(sec.prior_quarter_frame(frame))])
            out["sources"].append("sec_xbrl")
        elif found["foreign"]:
            out["statement_source"] = "adr_limited"
            out["errors"].append(
                "no_companyfacts: foreign private issuer (20-F/6-K) — XBRL coverage is partial")
    if fact_table:
        for fr in frames_needed:
            fresh = {
                c: s["quarters"].get(fr) or {}
                for c, s in (fact_table.get("series") or {}).items()
            }
            if any((fresh.get(c) or {}).get("val") is not None for c in SCAN_CONCEPTS):
                facts_by_frame[fr] = fresh

    cur = facts_by_frame.get(frame) or {}
    yoy = facts_by_frame.get(sec.prior_year_frame(frame)) or {}
    qoq = facts_by_frame.get(sec.prior_quarter_frame(frame)) or {}
    has_xbrl = any((cur.get(c) or {}).get("val") is not None for c in SCAN_CONCEPTS)

    # --- the earnings release and its press release ------------------------
    quarter_end = expected_quarter_end(facts_by_frame, frame) or ctx["frame_end_fallback"]
    out["quarter_end_expected"] = quarter_end
    surprise_row = ctx["surprises"].get((ticker, frame))
    er = match_earnings_8k(found["earnings_8k"], quarter_end)
    announce_source = "sec_8k_item_2_02"
    if er is None:
        er = match_foreign_release(found["foreign"],
                                   (surprise_row or {}).get("reported_date"))
        announce_source = "sec_6k_matched_by_consensus_date" if er else announce_source
    if er is None and (surprise_row or {}).get("reported_date"):
        # Reported according to consensus data, but no matching SEC filing was
        # found in the discovery window. Recorded rather than dropped, with the
        # weaker provenance stated.
        er = {"filing_date": surprise_row["reported_date"], "accession": None,
              "form": None, "items": None, "dir_url": None, "primary_doc": None}
        announce_source = "nasdaq_consensus_date_only"
    if er:
        out["reported"] = True
        out["announcement"] = {
            "date": er["filing_date"], "accession": er.get("accession"),
            "form": er.get("form"), "items": er.get("items"),
            "provenance": announce_source,
            "url": (f"{er['dir_url']}/{er['primary_doc']}"
                    if er.get("dir_url") and er.get("primary_doc") else er.get("dir_url")),
        }
        if er.get("accession"):
            store.save_filings(ticker, [dict(er, frame=frame, kind="earnings_release")])
        cached_pr = store.load_press_release(ticker, er["accession"]) if er.get("accession") else None
        pr_text = (cached_pr or {}).get("text") or ""
        if not pr_text and not ctx["no_press_release"] and er.get("accession"):
            try:
                docs = sec.list_filing_documents(er["dir_url"], er["accession"])
                doc = sec.find_press_release(docs)
                if doc:
                    pr_text = sec.fetch_document_text(doc["url"])
                    store.save_press_release(
                        ticker, er["accession"], frame=frame,
                        filing_date=er["filing_date"], exhibit_type=doc.get("type"),
                        url=doc["url"], text=pr_text)
                    out["announcement"]["press_release_url"] = doc["url"]
                    out["announcement"]["exhibit_type"] = doc.get("type")
            except sec.SecError as exc:
                out["errors"].append(f"press_release_failed: {str(exc)[:120]}")
        elif cached_pr:
            out["announcement"]["press_release_url"] = cached_pr.get("url")
            out["announcement"]["exhibit_type"] = cached_pr.get("exhibit_type")
        if pr_text:
            out["sources"].append("sec_press_release")
            out["guidance_excerpts"] = extract_guidance_excerpts(pr_text)
            out["press_release_chars"] = len(pr_text)
            # The opening paragraphs always carry the headline revenue / EPS.
            # For an ADR with no quarterly XBRL this is the *only* place the
            # quarter's numbers exist, so the excerpt travels in the evidence
            # rather than forcing a second fetch to see them.
            out["press_release_head"] = _headline_excerpt(pr_text)

    statement_filed = match_statement(found["statements"], quarter_end)
    if statement_filed:
        store.save_filings(ticker, [dict(statement_filed, frame=frame, kind="statement")])
        out["statement_filing"] = {"form": statement_filed["form"],
                                   "date": statement_filed["filing_date"],
                                   "accession": statement_filed["accession"]}

    # A foreign private issuer files 20-F/6-K, not 10-Q, and tags far less. Say
    # so explicitly rather than letting empty blocks read as a fetch failure.
    if found["foreign"] and not found["statements"]:
        out["statement_source"] = "adr_limited"
        out["statement_source_note"] = (
            "foreign private issuer: no 10-Q, XBRL quarterly coverage is partial or absent — "
            "read the press release and the call, not the statement blocks")
    else:
        out.setdefault("statement_source", "sec_xbrl")

    out["data_stage"] = (
        "xbrl" if has_xbrl else
        "press_release_only" if out.get("press_release_chars") else
        "reported_no_numbers" if out["reported"] else "not_reported")

    if not out["reported"]:
        out["note"] = "no item-2.02 8-K matched this frame — company has not reported yet"
        return out

    # --- derived metrics ---------------------------------------------------
    growth = growth_block(cur, yoy, qoq)
    margins = margin_block(cur, yoy, qoq)
    quality = quality_block(cur, yoy, growth["revenue"]["yoy_pct"])
    balance = balance_block(cur, yoy, qoq)
    out.update({"growth": growth, "margins": margins, "quality": quality,
                "balance": balance})

    surprise = surprise_row
    out["surprise"] = surprise
    if surprise:
        out["sources"].append(surprise.get("source", "surprise"))

    ticker_bars = ctx["bars"].get(ticker) or []
    announce_date = (out.get("announcement") or {}).get("date")
    out["price_history"] = price_history_window(ticker_bars, announce_date)
    reaction = price_reaction(ticker_bars, announce_date,
                              gap_min=ctx["gap_min"])
    out["price_reaction"] = reaction

    ts = ctx["transcripts"].get((ticker, frame))
    out["transcript"] = ts or {"status": "not_fetched"}
    if ts and ts.get("status") == "ok":
        out["sources"].append(f"transcript:{ts.get('source')}")

    out["screen"] = screen_block(growth, margins, quality, surprise, reaction, ctx["thresholds"])
    return out


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def _median(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 1)


def bucket_summary(rows: Sequence[Dict[str, Any]], universe: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per-bucket medians over the companies that have actually reported."""
    out: List[Dict[str, Any]] = []
    for bucket, spec in (universe.get("buckets") or {}).items():
        members = [r for r in rows if r["bucket"] == bucket]
        reported = [r for r in members if r.get("reported") and r.get("growth")]
        out.append({
            "bucket": bucket,
            "chain_role": spec.get("chain_role"),
            "desc": spec.get("desc"),
            "members": len(members),
            "reported": len(reported),
            "reported_pct": round(len(reported) / len(members) * 100, 1) if members else None,
            "median_revenue_yoy_pct": _median(r["growth"]["revenue"]["yoy_pct"] for r in reported),
            "median_revenue_qoq_pct": _median(r["growth"]["revenue"]["qoq_pct"] for r in reported),
            "median_gross_margin_pct": _median(r["margins"]["gross"]["pct"] for r in reported),
            "median_gross_margin_yoy_pp": _median(r["margins"]["gross"]["yoy_pp"] for r in reported),
            "median_eps_surprise_pct": _median(
                None if (r.get("surprise") or {}).get("surprise_pct_unstable")
                else (r.get("surprise") or {}).get("surprise_pct")
                for r in reported),
            "median_capex_to_revenue_pct": _median(
                r["quality"]["capex_to_revenue_pct"] for r in reported),
            "reported_tickers": [r["ticker"] for r in reported],
            "pending_tickers": [r["ticker"] for r in members if not r.get("reported")],
        })
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS = {
    "rev_yoy": 15.0, "rev_qoq": 5.0, "ni_yoy": 25.0, "gm_pp": 1.0,
    "ocf_ratio": 80.0, "ocf_ratio_low": 50.0, "surprise_pct": 3.0, "gap_pp": 20.0,
}


def quarter_end_of_frame(frame: str) -> Optional[str]:
    m = re.fullmatch(r"CY(\d{4})Q([1-4])", frame)
    if not m:
        return None
    year, q = int(m.group(1)), int(m.group(2))
    month = q * 3
    nxt = dt.date(year + (month // 12), (month % 12) + 1, 1)
    return (nxt - dt.timedelta(days=1)).isoformat()


def main(argv: Optional[List[str]] = None) -> int:
    today = dt.date.today()
    ap = argparse.ArgumentParser(description="US AI/semiconductor earnings evidence scan")
    ap.add_argument("--frame", default=sec.latest_closed_frame(today),
                    help="calendar quarter, e.g. CY2026Q2 (default: last closed quarter)")
    ap.add_argument("--tickers", default=None, help="restrict to these tickers (comma separated)")
    ap.add_argument("--buckets", default=None, help="restrict to these universe buckets")
    ap.add_argument("--since", default=None, help="filing discovery lower bound (default: frame end - 30d)")
    ap.add_argument("--end-date", default=None, help="as-of date, YYYY-MM-DD (default: today)")
    ap.add_argument("--universe", default=str(_SCRIPT_DIR.parent / "assets" / "universe.yaml"))
    ap.add_argument("--workers", type=int, default=6, help="parallel companies (SEC caps at 10 req/s)")
    ap.add_argument("--top", type=int, default=60,
                    help="highest-priority tickers listed in priority_tickers; companies remains complete")
    ap.add_argument("--refresh-xbrl", action="store_true", help="ignore the XBRL cache water-mark")
    ap.add_argument("--no-cache", action="store_true", help="run without the database")
    ap.add_argument("--no-price", action="store_true", help="skip Yahoo bars and the reaction block")
    ap.add_argument("--no-press-release", action="store_true", help="skip 8-K exhibit fetch")
    ap.add_argument("--no-transcript", action="store_true", help="skip transcript retrieval entirely")
    ap.add_argument("--no-av", action="store_true", help="transcripts from Motley Fool only, never spend Alpha Vantage quota")
    ap.add_argument("--transcript-limit", type=int, default=40,
                    help="max NEW transcripts to fetch this run (cached ones are free)")
    ap.add_argument("--gap-min", type=float, default=2.0, help="gap threshold in percent")
    ap.add_argument("--out", default=None)
    ap.add_argument("--universe-out", default=None)
    for key, val in DEFAULT_THRESHOLDS.items():
        ap.add_argument(f"--th-{key.replace('_', '-')}", type=float, default=val)
    args = ap.parse_args(argv)

    frame = args.frame.upper()
    if not sec.CALENDAR_QUARTER_RE.fullmatch(frame):
        print(f"[error] --frame must look like CY2026Q2, got {frame!r}", file=sys.stderr)
        return 2
    if not sec.DEFAULT_CONTACT:
        print("[warn] SEC_CONTACT_EMAIL is unset — EDGAR may answer 403. "
              "export SEC_CONTACT_EMAIL=you@example.com", file=sys.stderr)

    frame_end = quarter_end_of_frame(frame)
    end_date = args.end_date or today.isoformat()
    since = args.since or (sec._parse_ymd(frame_end) - dt.timedelta(days=30)).isoformat()

    universe = load_universe(Path(args.universe))
    companies = universe["companies"]
    if args.buckets:
        keep = {b.strip() for b in args.buckets.split(",")}
        companies = {t: c for t, c in companies.items() if c["bucket"] in keep}
    if args.tickers:
        keep_t = {t.strip().upper() for t in args.tickers.split(",")}
        companies = {t: c for t, c in companies.items() if t in keep_t}
    if not companies:
        print("[error] no companies selected", file=sys.stderr)
        return 2

    store = Store(enabled=not args.no_cache)
    tmap = sec.load_ticker_cik_map()
    for ticker, c in companies.items():
        rec = tmap.get(ticker)
        if rec:
            c["cik"], c["cik10"], c["name"] = str(rec["cik"]), rec["cik10"], rec["title"]
    store.save_companies([
        {"ticker": t, "cik": c.get("cik"), "name": c.get("name"), "bucket": c["bucket"],
         "chain_role": c.get("chain_role"),
         "fiscal_year_end_month": c.get("fiscal_year_end_month"),
         "statement_source": "sec_xbrl" if c.get("cik10") else "unresolved"}
        for t, c in companies.items()])

    tickers = sorted(companies)
    print(f"[scan] frame={frame} companies={len(tickers)} since={since} as_of={end_date} "
          f"cache={'on' if store.available else 'off'}", file=sys.stderr)
    if not store.available:
        print(f"[warn] running without cache: {store.error}", file=sys.stderr)

    # --- shared pre-fetch (surprise, prices) -------------------------------
    surprises: Dict[Tuple[str, str], Dict[str, Any]] = dict(store.load_surprises([frame]))
    fresh_surprise: List[Dict[str, Any]] = []
    bars: Dict[str, List[Dict[str, Any]]] = {}
    price_errors: Dict[str, str] = {}
    price_range = price_history_range(frame_end, today)

    def _prefetch(ticker: str) -> None:
        for row in fetch_surprise(ticker):
            fresh_surprise.append(row)
        if not args.no_price:
            got, err = fetch_bars(ticker, range_=price_range)
            if got:
                bars[ticker] = got
                store.save_bars(ticker, got)
            elif err:
                cached_bars = store.load_bars(ticker)
                if cached_bars:
                    bars[ticker] = cached_bars
                price_errors[ticker] = err

    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(_prefetch, tickers))
    if fresh_surprise:
        store.save_surprises(fresh_surprise)
        for row in fresh_surprise:
            surprises[(row["ticker"], row["frame"])] = row

    ctx: Dict[str, Any] = {
        "store": store, "since": since, "end_date": end_date,
        "refresh_xbrl": args.refresh_xbrl, "no_press_release": args.no_press_release,
        "xbrl_log": store.xbrl_fetch_log(tickers), "surprises": surprises,
        "bars": bars,
        "gap_min": args.gap_min, "frame_end_fallback": frame_end,
        "transcripts": {}, "thresholds": {
            k: getattr(args, f"th_{k}") for k in DEFAULT_THRESHOLDS},
    }

    # --- per-company scan --------------------------------------------------
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda t: scan_company(companies[t], frame, ctx), tickers))

    # Persist SEC-derived fiscal year ends backfilled during the scan, so the
    # fiscal-quarter mapping does not depend on the YAML offsets being complete.
    store.save_companies([
        {"ticker": r["ticker"], "cik": r.get("cik"), "name": r.get("name"),
         "bucket": r["bucket"], "chain_role": r.get("chain_role"),
         "fiscal_year_end_month": r.get("fiscal_year_end_month"),
         "statement_source": "sec_xbrl" if r.get("cik") else "unresolved"}
        for r in rows if r.get("fiscal_year_end_month")])

    # --- transcripts, after we know who reported and how big the beat was ---
    transcript_stats = {"cached": 0, "fetched": 0, "pending": 0, "skipped_budget": 0,
                        "skipped_limit": 0}
    if not args.no_transcript:
        budget = tf.AVBudget()
        cached_status = store.transcript_status([frame], tickers)
        reported = [r for r in rows if r.get("reported") and r.get("announcement")]
        # Priority: biggest surprise first, then largest revenue. When quota is
        # the binding constraint, the calls worth buying are the ones whose
        # numbers already look like news.
        reported.sort(key=lambda r: (
            -abs(
                0 if (r.get("surprise") or {}).get("surprise_pct_unstable")
                else (r.get("surprise") or {}).get("surprise_pct") or 0
            ),
            -(r.get("growth", {}).get("revenue", {}).get("value_musd") or 0)))
        budget_left = args.transcript_limit
        for row in reported:
            ticker = row["ticker"]
            have = cached_status.get((ticker, frame))
            if have and have.get("status") == "ok":
                payload = store.load_transcript(ticker, frame) or {}
                ctx["transcripts"][(ticker, frame)] = {
                    "status": "ok", "source": have.get("source"), "url": have.get("url"),
                    "published_date": have.get("published_date"),
                    "stats": {k: have.get(k) for k in
                              ("segment_count", "char_count", "prepared_segments",
                               "qa_segments")},
                    "cached": True,
                    "participants": payload.get("participants", []),
                }
                transcript_stats["cached"] += 1
                continue
            if budget_left <= 0:
                transcript_stats["skipped_limit"] += 1
                continue
            quarter_end = sec._parse_ymd(row.get("quarter_end_expected"))
            fye_month = (row.get("fiscal_year_end_month")
                         or companies[ticker].get("fiscal_year_end_month") or 12)
            av_quarter = (tf.fiscal_quarter_label(quarter_end, fye_month)
                          if quarter_end else None)
            payload = tf.get_transcript(ticker, call_date=row["announcement"]["date"],
                                        av_quarter=av_quarter,
                                        budget=budget, allow_av=not args.no_av)
            store.save_transcript(ticker, frame, payload)
            budget_left -= 1
            if payload["status"] == "ok":
                transcript_stats["fetched"] += 1
                ctx["transcripts"][(ticker, frame)] = {
                    "status": "ok", "source": payload.get("source"),
                    "url": payload.get("url"), "published_date": payload.get("published_date"),
                    "stats": payload["stats"], "cached": False,
                    "participants": payload.get("participants", []),
                }
            else:
                transcript_stats["pending"] += 1
                if any(a.get("status") in ("budget_exhausted", "rate_limited")
                       for a in payload.get("attempts", [])):
                    transcript_stats["skipped_budget"] += 1
                ctx["transcripts"][(ticker, frame)] = {
                    "status": payload["status"], "attempts": payload.get("attempts"),
                    "note": payload.get("note")}
        # Re-attach now that transcripts exist.
        for row in rows:
            ts = ctx["transcripts"].get((row["ticker"], frame))
            if ts:
                row["transcript"] = ts
                if ts.get("status") == "ok":
                    row.setdefault("sources", []).append(f"transcript:{ts.get('source')}")

    # --- assemble ----------------------------------------------------------
    buckets = bucket_summary(rows, universe)
    reported_rows = [r for r in rows if r.get("reported")]
    reported_rows.sort(key=lambda r: -(r.get("screen", {}).get("rank_score") or -999))

    stage_counts: Dict[str, int] = {}
    for r in rows:
        stage_counts[r["data_stage"]] = stage_counts.get(r["data_stage"], 0) + 1

    meta = {
        "type": "us_ai_semi_earnings_evidence",
        "frame": frame,
        "frame_quarter_end": frame_end,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "as_of": end_date,
        "discovery_since": since,
        "universe_size": len(tickers),
        "reported_count": len(reported_rows),
        "reported_pct": round(len(reported_rows) / len(tickers) * 100, 1) if tickers else None,
        "data_stage_counts": stage_counts,
        "transcript_stats": transcript_stats,
        "av_budget": tf.AVBudget().status(),
        "thresholds": ctx["thresholds"],
        "cache": store.stats() if store.available else {"cache": "off", "error": store.error},
        "price_errors": price_errors or None,
        "source_roles": {
            "numbers": "sec_xbrl (authority) — arrives with the 10-Q, 0-10 days after the call",
            "earnings_night": "sec_8k_ex99 press release — revenue/EPS/guidance, same day",
            "call": "motley_fool (free, irregular coverage) then alpha_vantage (25/day free tier)",
            "consensus": "api.nasdaq.com earnings-surprise (free, keyless)",
            "prices": "yahoo chart via shared/yahoo_http",
        },
        "data_notes": [
            "Fiscal quarters are aligned to calendar quarters by the midpoint of the reporting "
            "span, matching SEC's own CY frame assignment. A company's own 'Q1' label is not used.",
            "GAAP only. Consensus EPS is a non-GAAP number, so surprise_pct compares against a "
            "different basis than the XBRL figures — read them as two separate facts.",
            "Cash-flow quarters are differenced from year-to-date spans (derived=cumulative_diff); "
            "US filers do not tag discrete-quarter cash flow.",
            "data_stage=press_release_only means the 10-Q has not landed: no cash flow, no "
            "balance sheet, and the numbers are unaudited press-release figures.",
        ],
    }

    decision_pack = {
        "meta": meta,
        "buckets": buckets,
        "priority_tickers": [r["ticker"] for r in reported_rows[:max(0, args.top)]],
        "companies": reported_rows,
        "not_reported": [
            {"ticker": r["ticker"], "bucket": r["bucket"],
             "errors": r.get("errors") or None}
            for r in rows if not r.get("reported")],
    }

    out_path = Path(args.out or (_SCRIPT_DIR.parent / "reports" / f"usearn_scan_{frame}.json"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(decision_pack, ensure_ascii=False, indent=2), encoding="utf-8")

    uni_path = Path(args.universe_out or
                    (_SCRIPT_DIR.parent / "reports" / f"usearn_universe_{frame}.json"))
    uni_path.write_text(json.dumps({"meta": meta, "companies": rows},
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[scan] reported {len(reported_rows)}/{len(tickers)} | stages {stage_counts} | "
          f"transcripts {transcript_stats}", file=sys.stderr)
    print(f"[scan] decision pack -> {out_path}", file=sys.stderr)
    print(f"[scan] full universe -> {uni_path}", file=sys.stderr)
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
