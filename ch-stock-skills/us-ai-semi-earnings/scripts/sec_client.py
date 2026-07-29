#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SEC EDGAR access layer — the numerical authority for this skill.

Three doors, all free and keyless, all rate-limited to SEC's published 10 req/s
fair-use ceiling by one shared limiter in this module:

1. `https://www.sec.gov/files/company_tickers.json` — ticker → CIK. One call,
   ~10k entries, cached on disk because it changes on the order of weeks.
2. `https://data.sec.gov/submissions/CIK##########.json` — the filing stream.
   This is the **discovery layer**: an 8-K carrying item `2.02` is
   "Results of Operations and Financial Condition", i.e. the earnings release,
   and it lands the same afternoon the company reports. The 10-Q/10-K follows
   days later and is what actually carries XBRL.
3. `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` — every
   XBRL fact the company ever tagged, ~2-5 MB. One call per company gets all
   tags, which is why we fetch companyfacts rather than a dozen companyconcept
   calls, and why callers should cache the *extracted rows*, never the blob.

Why the timing split matters, and why it is not a defect to work around: on the
evening a company reports, XBRL does not exist yet. The only facts available are
in the 8-K's EX-99.1 press release (revenue, EPS, and — the part XBRL never
carries — next-quarter guidance). The audited statement view arrives with the
10-Q. A caller that waits for XBRL reports two days late; a caller that only
reads the press release never gets cash flow. This module exposes both and
labels which one answered, so the calling skill can say which it used.

The one structural gift US GAAP gives over the A-share equivalent: filers tag
the **discrete quarter** directly, so single-quarter revenue is a fact to look
up, not `cumulative − prior cumulative` arithmetic with a missing-base failure
mode. The exception is Q4, which almost nobody tags discretely because the 10-K
reports the full year — `quarter_series` rebuilds it as `FY − 9M` and marks the
row `derived_q4`.

Nothing here judges anything. Which tag means "revenue" for a given filer is a
mapping problem and lives in `TAG_ALIASES`; whether that revenue is good news
is the model's business.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from html import unescape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

# SEC asks for a descriptive UA with a contact address and throttles to 10 req/s.
# Unlike Yahoo's edge classifier (see shared/yahoo_http), SEC does not care what
# browser you claim to be — it cares that you identify yourself. A generic
# browser UA gets 403 here, which is the opposite of the Yahoo rule.
DEFAULT_CONTACT = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
USER_AGENT_TEMPLATE = "ch-skills us-ai-semi-earnings ({contact})"
SEC_CALLS_PER_SEC = float(os.environ.get("SEC_CALLS_PER_SEC", "8"))
CALENDAR_QUARTER_RE = re.compile(r"CY\d{4}Q[1-4]")

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
ARCHIVE_DIR_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}"

# Item 2.02 = "Results of Operations and Financial Condition" — the earnings 8-K.
EARNINGS_8K_ITEM = "2.02"
STATEMENT_FORMS = ("10-Q", "10-K", "10-K/A", "10-Q/A")
# Foreign private issuers (TSM/ASML/ARM/UMC/STM/ASX) file these instead and tag
# far less; callers must degrade rather than report the gaps as zeros.
FOREIGN_FORMS = ("20-F", "40-F", "6-K", "20-F/A")


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Token-less sliding gate: at most `per_sec` acquisitions per second."""

    def __init__(self, per_sec: float):
        self._min_gap = 1.0 / max(per_sec, 0.5)
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                gap = now - self._last
                if gap >= self._min_gap:
                    self._last = now
                    return
                wait = self._min_gap - gap
            time.sleep(wait)


_LIMITER = _RateLimiter(SEC_CALLS_PER_SEC)
_SESSION_LOCAL = threading.local()


def sec_user_agent() -> str:
    """UA string SEC requires. Contact address comes from SEC_CONTACT_EMAIL."""
    contact = DEFAULT_CONTACT or "research contact not set"
    return USER_AGENT_TEMPLATE.format(contact=contact)


def _session() -> requests.Session:
    sess = getattr(_SESSION_LOCAL, "sess", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": sec_user_agent(),
            "Accept-Encoding": "gzip, deflate",
        })
        _SESSION_LOCAL.sess = sess
    return sess


class SecError(RuntimeError):
    """Raised when EDGAR refuses or a document is unusable."""


def sec_get(url: str, *, attempts: int = 4, timeout: int = 45,
            as_json: bool = True) -> Any:
    """Rate-limited GET with retry. Returns parsed JSON or text.

    404 is returned as None rather than raised: a company that has never filed a
    given document is a normal state (ADRs have no companyfacts), not an error.
    """
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        _LIMITER.acquire()
        try:
            resp = _session().get(url, timeout=timeout)
            if resp.status_code == 404:
                return None
            if resp.status_code == 403:
                raise SecError(
                    f"SEC returned 403 for {url}. Set SEC_CONTACT_EMAIL to a real "
                    f"address — EDGAR rejects requests without an identifying User-Agent."
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                raise SecError(f"SEC {resp.status_code} for {url}")
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        except SecError as exc:
            last_exc = exc
            if "403" in str(exc):
                raise
            if i < attempts - 1:
                time.sleep(1.5 * (2 ** i))
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if i < attempts - 1:
                time.sleep(1.5 * (2 ** i))
    raise SecError(f"SEC GET failed after {attempts} attempts: {url} ({last_exc})")


# ---------------------------------------------------------------------------
# ticker -> CIK
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    root = Path(__file__).resolve().parent.parent / ".cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def load_ticker_cik_map(*, max_age_days: int = 14,
                        cache_path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """`{TICKER: {cik, cik10, title}}`. Disk-cached; refreshed every 2 weeks."""
    path = cache_path or (_cache_dir() / "company_tickers.json")
    fresh = False
    if path.exists():
        age = time.time() - path.stat().st_mtime
        fresh = age < max_age_days * 86400
    raw: Optional[Dict[str, Any]] = None
    if fresh:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
    if raw is None:
        raw = sec_get(TICKER_MAP_URL)
        if raw is None:
            raise SecError("SEC ticker map unavailable")
        try:
            path.write_text(json.dumps(raw), encoding="utf-8")
        except OSError:
            pass

    out: Dict[str, Dict[str, Any]] = {}
    for entry in raw.values():
        ticker = str(entry.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        cik = int(entry.get("cik_str", 0))
        out[ticker] = {"cik": cik, "cik10": f"{cik:010d}", "title": entry.get("title", "")}
    return out


# ---------------------------------------------------------------------------
# filings discovery
# ---------------------------------------------------------------------------

def get_submissions(cik10: str) -> Optional[Dict[str, Any]]:
    return sec_get(SUBMISSIONS_URL.format(cik10=cik10))


def iter_recent_filings(submissions: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Flatten `filings.recent` (the last ~1000 filings) into dicts.

    Older filings live in `filings.files[]` shards; a skill that only ever looks
    at the current and prior fiscal year never needs them, so we do not fetch
    them and callers should not assume full history here.
    """
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    n = len(forms)
    keys = ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime",
            "primaryDocument", "primaryDocDescription", "items", "form")
    cols = {k: (recent.get(k) or [None] * n) for k in keys}
    for i in range(n):
        yield {k: cols[k][i] for k in keys}


def find_earnings_filings(submissions: Dict[str, Any], *, since: str,
                          until: Optional[str] = None) -> Dict[str, Any]:
    """Split a company's recent filings into the two things this skill needs.

    `since` / `until` are `YYYY-MM-DD` filing-date bounds (inclusive).

    Returns `earnings_8k[]` (item 2.02 present — newest first), `statements[]`
    (10-Q/10-K), `foreign[]` (20-F/6-K, for ADRs), each entry carrying the
    accession number and a directory URL so exhibits can be resolved later.
    """
    cik = int(submissions.get("cik", 0))
    out: Dict[str, List[Dict[str, Any]]] = {"earnings_8k": [], "statements": [], "foreign": []}
    for f in iter_recent_filings(submissions):
        fdate = f.get("filingDate") or ""
        if not fdate or fdate < since or (until and fdate > until):
            continue
        acc = (f.get("accessionNumber") or "").strip()
        if not acc:
            continue
        entry = {
            "accession": acc,
            "filing_date": fdate,
            "report_date": f.get("reportDate"),
            "accepted_at": f.get("acceptanceDateTime"),
            "form": f.get("form"),
            "items": f.get("items") or "",
            "primary_doc": f.get("primaryDocument"),
            "dir_url": ARCHIVE_DIR_URL.format(cik=cik, acc_nodash=acc.replace("-", "")),
        }
        form = (f.get("form") or "").upper()
        if form.startswith("8-K") and EARNINGS_8K_ITEM in (f.get("items") or ""):
            out["earnings_8k"].append(entry)
        elif form in STATEMENT_FORMS:
            out["statements"].append(entry)
        elif form in FOREIGN_FORMS:
            out["foreign"].append(entry)
    for key in out:
        out[key].sort(key=lambda e: (e["filing_date"], e["accession"]), reverse=True)
    return out


# ---------------------------------------------------------------------------
# 8-K exhibits (the earnings press release)
# ---------------------------------------------------------------------------

_EXHIBIT_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_HREF = re.compile(r'href="([^"]+)"', re.I)
_TAG = re.compile(r"<[^>]+>")


def _strip_tags(html: str) -> str:
    return unescape(_TAG.sub(" ", html)).strip()


def list_filing_documents(dir_url: str, accession: str) -> List[Dict[str, Any]]:
    """Parse a filing's index page into `{seq, type, description, url, size}`.

    The index page is the only place the *exhibit type* (`EX-99.1`) is stated;
    `index.json` gives filenames but not types, and filenames are free-form
    (`q22026txnex99-eredgar.htm`), so type must come from here.
    """
    idx_url = f"{dir_url}/{accession}-index.htm"
    html = sec_get(idx_url, as_json=False)
    if not html:
        return []
    docs: List[Dict[str, Any]] = []
    for row_html in _EXHIBIT_ROW.findall(html):
        cells = [_strip_tags(c) for c in _CELL.findall(row_html)]
        if len(cells) < 4:
            continue
        href = _HREF.search(row_html)
        if not href:
            continue
        link = href.group(1)
        # iXBRL viewer links wrap the real document: /ix?doc=/Archives/...
        if link.startswith("/ix?doc="):
            link = link[len("/ix?doc="):]
        if "/Archives/" not in link:
            continue
        seq, desc, doc_name, doc_type = cells[0], cells[1], cells[2], cells[3]
        docs.append({
            "seq": seq,
            "description": desc,
            "document": doc_name.replace("\xa0", " ").split("  ")[0].strip(),
            "type": doc_type,
            "size": cells[4] if len(cells) > 4 else None,
            "url": "https://www.sec.gov" + link if link.startswith("/") else link,
        })
    return docs


def find_press_release(docs: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick the earnings press release exhibit out of a filing's documents.

    Preference order is EX-99.1, then any EX-99*, then the largest EX-* — the
    press release is normally by far the biggest exhibit in an earnings 8-K.
    """
    ex99: List[Dict[str, Any]] = []
    for d in docs:
        t = (d.get("type") or "").upper().replace(" ", "")
        if t in ("EX-99.1", "EX-99"):
            return d
        if t.startswith("EX-99"):
            ex99.append(d)
    if ex99:
        return ex99[0]

    def _size(d: Dict[str, Any]) -> int:
        try:
            return int(str(d.get("size") or "0").replace(",", ""))
        except ValueError:
            return 0

    others = [d for d in docs if (d.get("type") or "").upper().startswith("EX-")]
    return max(others, key=_size) if others else None


_BLOCK_END = re.compile(r"</(p|div|tr|h[1-6]|li|table)>", re.I)


def fetch_document_text(url: str, *, max_chars: int = 400_000) -> str:
    """Fetch an EDGAR HTML document and flatten it to newline-separated text.

    Press releases are mostly HTML tables (the income statement), so block-level
    tags become newlines to keep row structure legible; without that the whole
    statement collapses into one unreadable line.
    """
    raw = sec_get(url, as_json=False, timeout=60)
    if not raw:
        return ""
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    body = _BLOCK_END.sub("\n", body)
    body = re.sub(r"<br[^>]*>", "\n", body, flags=re.I)
    text = unescape(_TAG.sub(" ", body))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return text.strip()[:max_chars]


# ---------------------------------------------------------------------------
# XBRL facts
# ---------------------------------------------------------------------------

# Filers pick different tags for the same line, and rank order is preference:
# `_dedupe_by_frame` keeps the earliest-ranked alias that covers a frame. IFRS
# names are appended after the us-gaap ones because the foreign private issuers
# in this universe (TSM files `ifrs-full`) report the same lines under entirely
# different tags; without them an ADR comes back as "no revenue" rather than
# "revenue is tagged elsewhere". `duration` facts cover a span (revenue),
# `instant` facts are a point in time (inventory).
TAG_ALIASES: Dict[str, Dict[str, Any]] = {
    "revenue": {"kind": "duration", "tags": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "RevenueFromContractsWithCustomers",  # IFRS
        "Revenue",                            # IFRS
    ]},
    "cost_of_revenue": {"kind": "duration", "tags": [
        "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfServices",
        "CostOfSales",  # IFRS
    ]},
    "gross_profit": {"kind": "duration", "tags": ["GrossProfit"]},
    "operating_income": {"kind": "duration", "tags": [
        "OperatingIncomeLoss",
        "ProfitLossFromOperatingActivities",  # IFRS
    ]},
    "net_income": {"kind": "duration", "tags": [
        "NetIncomeLoss", "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "ProfitLossAttributableToOwnersOfParent",  # IFRS
    ]},
    "eps_diluted": {"kind": "duration", "tags": [
        "EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted",
        "DilutedEarningsLossPerShare",  # IFRS
    ]},
    "rnd": {"kind": "duration", "tags": ["ResearchAndDevelopmentExpense"]},
    "sgna": {"kind": "duration", "tags": [
        "SellingGeneralAndAdministrativeExpense",
        "GeneralAndAdministrativeExpense",
    ]},
    "operating_expenses": {"kind": "duration", "tags": ["OperatingExpenses", "CostsAndExpenses"]},
    "sbc": {"kind": "duration", "tags": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]},
    "ocf": {"kind": "duration", "tags": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "CashFlowsFromUsedInOperatingActivities",  # IFRS
    ]},
    "capex": {"kind": "duration", "tags": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",  # IFRS
    ]},
    "buyback": {"kind": "duration", "tags": ["PaymentsForRepurchaseOfCommonStock"]},
    "shares_diluted": {"kind": "duration", "tags": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    ]},
    # Balance-sheet instants — the forward-looking half of the read.
    "inventory": {"kind": "instant", "tags": [
        "InventoryNet", "InventoryGross",
        "Inventories",  # IFRS
    ]},
    "receivables": {"kind": "instant", "tags": [
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
        "AccountsReceivableGrossCurrent",
        "TradeAndOtherCurrentReceivables",  # IFRS
    ]},
    "deferred_revenue": {"kind": "instant", "tags": [
        "ContractWithCustomerLiabilityCurrent",
        "DeferredRevenueCurrent",
        "CurrentContractLiabilities",  # IFRS
    ]},
    "cash": {"kind": "instant", "tags": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalents",  # IFRS
    ]},
    "short_term_investments": {"kind": "instant", "tags": [
        "ShortTermInvestments", "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    ]},
    "total_debt_lt": {"kind": "instant", "tags": [
        "LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations",
    ]},
    "ppe_net": {"kind": "instant", "tags": [
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipment",  # IFRS
    ]},
    "goodwill": {"kind": "instant", "tags": ["Goodwill"]},
    "total_assets": {"kind": "instant", "tags": ["Assets"]},
    "equity": {"kind": "instant", "tags": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "Equity",  # IFRS
    ]},
    # Remaining performance obligation: the closest thing US filers have to
    # 合同负债 as an order-book signal, and only some of them tag it.
    "rpo": {"kind": "instant", "tags": [
        "RevenueRemainingPerformanceObligation",
    ]},
}

_DURATION_WINDOWS = {
    "Q": (80, 100),
    "H": (170, 195),
    "9M": (260, 285),
    "FY": (350, 380),
}


def get_companyfacts(cik10: str) -> Optional[Dict[str, Any]]:
    return sec_get(COMPANYFACTS_URL.format(cik10=cik10), timeout=120)


def _parse_ymd(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def calendar_frame(start: Optional[str], end: Optional[str]) -> Optional[str]:
    """Reproduce SEC's `CY####Q#` alignment for a fact's span.

    This is the single most important function in the module, because every
    cross-company comparison this skill makes depends on off-calendar filers
    landing in the right calendar quarter. NVDA's fiscal Q1 runs Feb–Apr, AVGO's
    runs Feb–May, MU's runs Mar–May; naming them all "Q1" and comparing is
    comparing three different economic periods.

    SEC publishes its own `frame` but only after a batch job runs, so the newest
    filing — exactly the one being scanned on earnings night — usually has
    `frame: null`. The rule below reproduces SEC's assignment for the cases where
    it *has* published one: **the calendar quarter containing the span's
    midpoint**. Verified against SEC's own frames — NVDA Jan-28~Apr-28 → CY Q1,
    AVGO May-05~Aug-03 → CY Q2, MU Feb-27~May-28 → CY Q2, TXN Apr-01~Jun-30 →
    CY Q2. Aligning on the *end* date instead pushes AVGO and NVDA a quarter
    late, which silently compares them against the wrong year-ago base.

    Instants use `end − 45d`, putting a balance in the same frame as the quarter
    that closes on that date.

    Returns `CY2026Q2` for a discrete quarter, `CY2025` for a full year, and None
    for 6-month/9-month interim spans, which have no calendar equivalent.
    """
    s, e = _parse_ymd(start), _parse_ymd(end)
    if not e:
        return None
    if not s:
        anchor = e - dt.timedelta(days=45)
        return f"CY{anchor.year}Q{(anchor.month - 1) // 3 + 1}"
    days = (e - s).days
    anchor = s + dt.timedelta(days=days // 2)
    if _DURATION_WINDOWS["Q"][0] <= days <= _DURATION_WINDOWS["Q"][1]:
        return f"CY{anchor.year}Q{(anchor.month - 1) // 3 + 1}"
    if _DURATION_WINDOWS["FY"][0] <= days <= _DURATION_WINDOWS["FY"][1]:
        # A fiscal year is named for the calendar year holding most of it; the
        # midpoint delivers that directly.
        return f"CY{anchor.year}"
    return None


def span_kind(start: Optional[str], end: Optional[str]) -> str:
    """Classify a duration span as Q / H / 9M / FY / other; instants are `I`."""
    s, e = _parse_ymd(start), _parse_ymd(end)
    if not e:
        return "unknown"
    if not s:
        return "I"
    days = (e - s).days
    for label, (lo, hi) in _DURATION_WINDOWS.items():
        if lo <= days <= hi:
            return label
    return "other"


def _fact_rows(facts: Dict[str, Any], tag: str) -> List[Dict[str, Any]]:
    """All rows for one tag, preserving the source unit."""
    for taxonomy in ("us-gaap", "ifrs-full", "dei"):
        node = (facts.get("facts") or {}).get(taxonomy, {}).get(tag)
        if not node:
            continue
        units = node.get("units") or {}
        unit_key = next((u for u in ("USD", "USD/shares", "shares", "pure") if u in units), None)
        if unit_key is None:
            unit_key = next(iter(units), None)
        if unit_key is None:
            continue
        rows = []
        for r in units[unit_key]:
            rows.append({
                "start": r.get("start"),
                "end": r.get("end"),
                "val": r.get("val"),
                "fy": r.get("fy"),
                "fp": r.get("fp"),
                "form": r.get("form"),
                "filed": r.get("filed"),
                "frame": r.get("frame"),
                "accn": r.get("accn"),
                "unit": unit_key,
                "tag": tag,
                "taxonomy": taxonomy,
            })
        return rows
    return []


def resolve_concept(facts: Dict[str, Any], concept: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Return `(primary_tag, rows)` for a logical concept across **all** aliases.

    Aliases must be merged rather than first-hit-wins: filers change tags
    mid-history. NVDA's older quarters sit under
    `RevenueFromContractWithCustomerExcludingAssessedTax` while recent ones moved
    to another alias, so picking one tag and stopping returns a series that
    simply ends a few years ago — which looks like "no recent revenue" instead of
    "wrong tag". Rows carry `alias_rank`; `_dedupe_by_frame` uses it to prefer the
    more specific tag when two aliases both cover a frame.
    """
    spec = TAG_ALIASES.get(concept)
    if not spec:
        raise KeyError(f"unknown concept {concept!r}")
    merged: List[Dict[str, Any]] = []
    primary = ""
    for rank, tag in enumerate(spec["tags"]):
        rows = _fact_rows(facts, tag)
        if not rows:
            continue
        if not primary:
            primary = tag
        for r in rows:
            r["alias_rank"] = rank
        merged.extend(rows)
    return primary, merged


def _dedupe_by_frame(rows: Sequence[Dict[str, Any]], want_kind: str) -> Dict[str, Dict[str, Any]]:
    """Collapse rows to one per calendar frame.

    A quarter is re-reported in later filings as the comparative column, and a
    restatement lands as another row for the same span, so a frame routinely has
    several candidates. Ranking is: preferred alias first, then newest filing —
    the newest filing of the preferred tag is the currently-effective value,
    which is what a restatement is supposed to mean.

    The frame is always recomputed with `calendar_frame` rather than read from
    SEC's `frame` field: SEC leaves it null on fresh filings, so trusting it
    where present and deriving elsewhere would key the same quarter two
    different ways across concepts of the same company.
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        if span_kind(r.get("start"), r.get("end")) != want_kind:
            continue
        frame = calendar_frame(r.get("start"), r.get("end"))
        if frame:
            buckets.setdefault(frame, []).append(r)

    best: Dict[str, Dict[str, Any]] = {}
    for frame, cands in buckets.items():
        # Two stable passes: newest filing first, then preferred alias first.
        cands.sort(key=lambda r: r.get("filed") or "", reverse=True)
        cands.sort(key=lambda r: r.get("alias_rank", 99))
        best[frame] = dict(cands[0], derived_frame=frame)
    return best


def _derive_from_cumulative(rows: Sequence[Dict[str, Any]], tag: str) -> Dict[str, Dict[str, Any]]:
    """Rebuild discrete quarters by differencing year-to-date spans.

    Income-statement items are tagged both ways, but **cash-flow items are only
    ever cumulative** — a filer reports "cash from operations, six months ended
    June 30", never "for the three months ended June 30". Without this, OCF and
    capex come back empty for every quarter except Q1, which reads as missing
    data when it is really a different reporting convention.

    Spans sharing a `start` form one fiscal year's chain (3M, 6M, 9M, 12M);
    consecutive differences give the discrete quarters, and the first link is
    already discrete. Rows are deduped on `end` keeping the newest filing so a
    restated year differences against itself rather than across vintages.
    """
    # Chains are keyed by (alias, fiscal-year start) so a difference is never
    # taken between two different tags — subtracting a `Revenues` YTD from a
    # `RevenueFromContractWithCustomer...` YTD would silently invent a quarter.
    chains: Dict[Tuple[int, str], Dict[str, Dict[str, Any]]] = {}
    for r in rows:
        s, e = r.get("start"), r.get("end")
        if not s or not e or span_kind(s, e) not in ("Q", "H", "9M", "FY"):
            continue
        by_end = chains.setdefault((r.get("alias_rank", 0), s), {})
        prev = by_end.get(e)
        if prev is None or (r.get("filed") or "") >= (prev.get("filed") or ""):
            by_end[e] = r

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for (alias_rank, start), by_end in sorted(chains.items()):
        ordered = [by_end[e] for e in sorted(by_end)]
        prev_row: Optional[Dict[str, Any]] = None
        for row in ordered:
            try:
                val = float(row["val"])
            except (TypeError, ValueError, KeyError):
                prev_row = row
                continue
            if prev_row is None:
                span_start, derived = start, None
            else:
                try:
                    val -= float(prev_row["val"])
                except (TypeError, ValueError, KeyError):
                    prev_row = row
                    continue
                span_start, derived = prev_row.get("end"), "cumulative_diff"
            prev_row = row
            frame = calendar_frame(span_start, row.get("end"))
            if not frame or not CALENDAR_QUARTER_RE.fullmatch(frame):
                continue
            cand = {
                "start": span_start, "end": row.get("end"), "val": val,
                "fy": row.get("fy"), "fp": row.get("fp"), "form": row.get("form"),
                "filed": row.get("filed"), "frame": None, "accn": row.get("accn"),
                "unit": row.get("unit"), "tag": row.get("tag") or tag,
                "taxonomy": row.get("taxonomy"), "derived_frame": frame,
                "alias_rank": alias_rank,
            }
            if derived:
                cand["derived"] = derived
            buckets.setdefault(frame, []).append(cand)

    # Same ranking as _dedupe_by_frame: preferred alias wins, newest filing wins
    # within an alias.
    out: Dict[str, Dict[str, Any]] = {}
    for frame, cands in buckets.items():
        cands.sort(key=lambda r: r.get("filed") or "", reverse=True)
        cands.sort(key=lambda r: r.get("alias_rank", 99))
        out[frame] = cands[0]
    return out


def quarter_series(facts: Dict[str, Any], concept: str) -> Dict[str, Any]:
    """Discrete-quarter series for one concept, keyed `CY####Q#`.

    Directly tagged discrete quarters win. Gaps are then filled from the
    cumulative chain (`derived: "cumulative_diff"`) — which is how every
    cash-flow quarter and almost every Q4 gets built, since the 10-K reports the
    full year and no discrete Q4. `derived` is carried on the row so a caller can
    always tell a reported number from a reconstructed one.

    Instant concepts are returned as-is: a balance is a balance.
    """
    tag, rows = resolve_concept(facts, concept)
    kind = TAG_ALIASES[concept]["kind"]
    if not rows:
        return {"concept": concept, "tag": "", "kind": kind, "quarters": {}, "annual": {},
                "note": "tag_missing"}

    if kind == "instant":
        instants = _dedupe_by_frame(rows, "I")
        return {"concept": concept, "tag": tag, "kind": kind,
                "quarters": instants, "annual": {}, "note": ""}

    quarters = _dedupe_by_frame(rows, "Q")
    annual = _dedupe_by_frame(rows, "FY")
    derived_note = ""
    for frame, row in _derive_from_cumulative(rows, tag).items():
        if frame not in quarters:
            quarters[frame] = row
            if row.get("derived"):
                derived_note = "has_derived_quarters"

    return {"concept": concept, "tag": tag, "kind": kind,
            "quarters": quarters, "annual": annual, "note": derived_note}


def build_fact_table(facts: Dict[str, Any],
                     concepts: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Extract every configured concept into `{concept: quarter_series(...)}`.

    This is the only thing worth caching from a companyfacts blob — the blob
    itself is megabytes of history that will never be read again.
    """
    wanted = list(concepts or TAG_ALIASES.keys())
    entity = {
        "cik": facts.get("cik"),
        "entity_name": facts.get("entityName"),
    }
    series = {c: quarter_series(facts, c) for c in wanted}
    covered = sorted({f for s in series.values() for f in s["quarters"]}, reverse=True)
    return {
        "entity": entity,
        "series": series,
        "frames_covered": covered[:24],
        "missing_concepts": [c for c, s in series.items() if s["note"] == "tag_missing"],
    }


def prior_year_frame(frame: str) -> str:
    """`CY2026Q2` -> `CY2025Q2`; `CY2025` -> `CY2024`."""
    m = re.fullmatch(r"CY(\d{4})(Q[1-4])?", frame)
    if not m:
        return frame
    year = int(m.group(1)) - 1
    return f"CY{year}{m.group(2) or ''}"


def prior_quarter_frame(frame: str) -> str:
    """`CY2026Q1` -> `CY2025Q4` (walks back across the year boundary)."""
    m = re.fullmatch(r"CY(\d{4})Q([1-4])", frame)
    if not m:
        return frame
    year, q = int(m.group(1)), int(m.group(2))
    return f"CY{year - 1}Q4" if q == 1 else f"CY{year}Q{q - 1}"


def frame_for_date(day: dt.date) -> str:
    return f"CY{day.year}Q{(day.month - 1) // 3 + 1}"


def latest_closed_frame(today: Optional[dt.date] = None) -> str:
    """The most recent calendar quarter that has actually ended."""
    today = today or dt.date.today()
    q = (today.month - 1) // 3 + 1
    return f"CY{today.year - 1}Q4" if q == 1 else f"CY{today.year}Q{q - 1}"


# ---------------------------------------------------------------------------
# CLI — probing and self-check, not part of the scan pipeline
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="SEC EDGAR probe (discovery + XBRL extraction)")
    ap.add_argument("ticker", help="e.g. NVDA")
    ap.add_argument("--since", default=None, help="filing-date lower bound YYYY-MM-DD (default 180d ago)")
    ap.add_argument("--frame", default=None, help="calendar frame to print, e.g. CY2026Q2")
    ap.add_argument("--press-release", action="store_true", help="also fetch the latest 8-K EX-99 text head")
    ap.add_argument("--concepts", default="revenue,gross_profit,net_income,eps_diluted,ocf,inventory")
    args = ap.parse_args(argv)

    if not DEFAULT_CONTACT:
        print("[warn] SEC_CONTACT_EMAIL not set — EDGAR may return 403. "
              "export SEC_CONTACT_EMAIL=you@example.com", file=sys.stderr)

    tmap = load_ticker_cik_map()
    rec = tmap.get(args.ticker.upper())
    if not rec:
        print(f"[error] ticker {args.ticker} not in SEC map", file=sys.stderr)
        return 1
    print(f"{args.ticker.upper()} -> CIK {rec['cik10']} ({rec['title']})")

    since = args.since or (dt.date.today() - dt.timedelta(days=180)).isoformat()
    subs = get_submissions(rec["cik10"])
    if not subs:
        print("[error] submissions unavailable", file=sys.stderr)
        return 1
    found = find_earnings_filings(subs, since=since)
    for kind in ("earnings_8k", "statements", "foreign"):
        print(f"\n{kind}:")
        for e in found[kind][:5]:
            print(f"  {e['filing_date']} {e['form']:6} items={e['items'] or '-':10} {e['accession']}")

    if args.press_release and found["earnings_8k"]:
        latest = found["earnings_8k"][0]
        docs = list_filing_documents(latest["dir_url"], latest["accession"])
        pr = find_press_release(docs)
        print(f"\npress release: {pr['type'] if pr else None} {pr['url'] if pr else '-'}")
        if pr:
            text = fetch_document_text(pr["url"])
            print(f"  chars={len(text)}")
            print("  " + "\n  ".join(text.split("\n")[:12]))

    facts = get_companyfacts(rec["cik10"])
    if not facts:
        print("\n[note] no companyfacts — likely a foreign private issuer (20-F/6-K filer)")
        return 0
    table = build_fact_table(facts, args.concepts.split(","))
    frame = args.frame or latest_closed_frame()
    print(f"\nXBRL frame {frame} (missing tags: {table['missing_concepts'] or 'none'})")
    print(f"frames covered: {', '.join(table['frames_covered'][:8])}")
    for concept, s in table["series"].items():
        row = s["quarters"].get(frame)
        pri = s["quarters"].get(prior_year_frame(frame))
        val = row.get("val") if row else None
        base = pri.get("val") if pri else None
        yoy = None
        if isinstance(val, (int, float)) and isinstance(base, (int, float)) and base:
            yoy = round((val - base) / abs(base) * 100, 1)
        flag = f" [{row.get('derived')}]" if row and row.get("derived") else ""
        print(f"  {concept:22} tag={s['tag'] or '-':46} val={val} yoy={yoy}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
