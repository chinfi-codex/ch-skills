#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline self-check. No network, no database — pure logic on fixtures.

Everything asserted here is something that was actually wrong at some point
during the build and would be silent if it regressed: calendar alignment for
off-calendar filers, cash-flow quarters differenced out of year-to-date spans,
the prepared/Q&A boundary landing on the operator's agenda announcement instead
of the real handoff, and the guidance extractor swallowing the headline results
sentence.

    python3 scripts/test_earnings_scan.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import earnings_scan as scan  # noqa: E402
import render_period_html as period_html  # noqa: E402
import sec_client as sec  # noqa: E402
import transcript_fetch as tf  # noqa: E402
import verdict  # noqa: E402
from store import Store  # noqa: E402

FAILURES: list = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        FAILURES.append(name)


def check_true(name: str, cond: bool, detail: str = "") -> None:
    check(name, bool(cond) or detail or False, True)


# ---------------------------------------------------------------------------
print("calendar frame alignment (midpoint rule, verified against SEC's own frames)")
# NVDA fiscal Q1 runs Feb-Apr and must land in CY Q1, not CY Q2. Aligning on the
# end date instead pushes NVDA and AVGO a quarter late and silently compares
# them against the wrong year-ago base.
check("NVDA Jan28~Apr28", sec.calendar_frame("2019-01-28", "2019-04-28"), "CY2019Q1")
check("AVGO May05~Aug03", sec.calendar_frame("2025-05-05", "2025-08-03"), "CY2025Q2")
check("AVGO Feb02~May03", sec.calendar_frame("2026-02-02", "2026-05-03"), "CY2026Q1")
check("MU Feb27~May28", sec.calendar_frame("2026-02-27", "2026-05-28"), "CY2026Q2")
check("TXN Apr01~Jun30", sec.calendar_frame("2026-04-01", "2026-06-30"), "CY2026Q2")
check("full year", sec.calendar_frame("2025-01-01", "2025-12-31"), "CY2025")
check("9M span has no calendar equivalent", sec.calendar_frame("2026-01-01", "2026-09-30"), None)
check("instant at NVDA quarter close", sec.calendar_frame(None, "2026-04-26"), "CY2026Q1")
check("instant at TXN quarter close", sec.calendar_frame(None, "2026-06-30"), "CY2026Q2")

print("frame arithmetic")
check("prior year", sec.prior_year_frame("CY2026Q2"), "CY2025Q2")
check("prior quarter crosses the year", sec.prior_quarter_frame("CY2026Q1"), "CY2025Q4")
check("prior quarter in-year", sec.prior_quarter_frame("CY2026Q3"), "CY2026Q2")
check("quarter end of frame", scan.quarter_end_of_frame("CY2026Q2"), "2026-06-30")
check("quarter end Q4", scan.quarter_end_of_frame("CY2026Q4"), "2026-12-31")
check("Q0 is rejected", scan.quarter_end_of_frame("CY2026Q0"), None)
check("Q5 is rejected", scan.quarter_end_of_frame("CY2026Q5"), None)

# ---------------------------------------------------------------------------
print("\ncash-flow quarters differenced out of year-to-date spans")
# US filers never tag discrete-quarter cash flow — only "six months ended".
# Without the difference, OCF is empty for every quarter except Q1, which reads
# as missing data rather than a different reporting convention.
ytd_facts = {"facts": {"us-gaap": {"NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
    {"start": "2026-01-01", "end": "2026-03-31", "val": 1000, "form": "10-Q", "filed": "2026-04-24"},
    {"start": "2026-01-01", "end": "2026-06-30", "val": 3500, "form": "10-Q", "filed": "2026-07-24"},
    {"start": "2026-01-01", "end": "2026-09-30", "val": 6000, "form": "10-Q", "filed": "2026-10-23"},
    {"start": "2026-01-01", "end": "2026-12-31", "val": 9000, "form": "10-K", "filed": "2027-02-06"},
]}}}}}
ocf = sec.quarter_series(ytd_facts, "ocf")["quarters"]
check("Q1 is already discrete", ocf["CY2026Q1"]["val"], 1000)
check("Q2 = 6M - 3M", ocf["CY2026Q2"]["val"], 2500.0)
check("Q3 = 9M - 6M", ocf["CY2026Q3"]["val"], 2500.0)
check("Q4 = FY - 9M", ocf["CY2026Q4"]["val"], 3000.0)
check("derived rows are labelled", ocf["CY2026Q2"]["derived"], "cumulative_diff")
check("reported rows are not", ocf["CY2026Q1"].get("derived"), None)

print("alias merging across a tag change mid-history")
# A filer that switches revenue tags leaves the old tag with only old rows.
# First-hit-wins returns a series that simply stops a few years ago, which looks
# like "no recent revenue" rather than "wrong tag".
mixed = {"facts": {"us-gaap": {
    "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
        {"start": "2024-01-01", "end": "2024-03-31", "val": 100, "form": "10-Q", "filed": "2024-04-20"}]}},
    "Revenues": {"units": {"USD": [
        {"start": "2026-01-01", "end": "2026-03-31", "val": 300, "form": "10-Q", "filed": "2026-04-20"}]}},
}}}
rev = sec.quarter_series(mixed, "revenue")["quarters"]
check("old tag still resolves", rev["CY2024Q1"]["val"], 100)
check("new tag also resolves", rev["CY2026Q1"]["val"], 300)

print("restatement: newest filing of the preferred alias wins")
restated = {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": [
    {"start": "2026-01-01", "end": "2026-03-31", "val": 50, "form": "10-Q", "filed": "2026-04-20"},
    {"start": "2026-01-01", "end": "2026-03-31", "val": 47, "form": "10-Q", "filed": "2026-07-24"},
]}}}}}
check("restated value wins",
      sec.quarter_series(restated, "net_income")["quarters"]["CY2026Q1"]["val"], 47)

print("XBRL cache marks requested-but-empty frames")


class CaptureStore(Store):
    def __init__(self):
        self.available = True
        self.calls = []

    def _upsert(self, table, columns, keys, rows):
        self.calls.append((table, columns, rows))
        return len(rows)


capture = CaptureStore()
capture.save_facts("IPO", {"series": {}, "frames_covered": [], "missing_concepts": []},
                   keep_frames=["CY2026Q2"])
fact_rows = next(rows for table, _, rows in capture.calls if table == "usearn_xbrl_fact")
check_true("empty frame gets a cache sentinel",
           any(row[1:4] == ("CY2026Q2", "__frame_fetched__", None) for row in fact_rows))

# ---------------------------------------------------------------------------
print("\nfiscal quarter end is read from cached facts, not only fresh ones")
# On every run after the first, XBRL comes from cache and no fact table is built.
# Reading only the fresh table returned None, the caller fell back to the
# calendar quarter end, and an off-calendar filer's own earnings 8-K was then
# rejected as "filed before the quarter ended".
mu_facts = {
    "CY2026Q2": {"revenue": {"val": 41456e6, "end": "2026-05-28"}},
    "CY2025Q2": {"revenue": {"val": 9300e6, "end": "2025-05-29"}},
}
check("uses the filer's own quarter end", scan.expected_quarter_end(mu_facts, "CY2026Q2"),
      "2026-05-28")
check("projects from a year earlier + 364d when the quarter has not landed",
      scan.expected_quarter_end({"CY2025Q2": mu_facts["CY2025Q2"]}, "CY2026Q2"), "2026-05-28")
check("no facts at all returns None", scan.expected_quarter_end({}, "CY2026Q2"), None)
check("MU's own 8-K matches against its real quarter end",
      (scan.match_earnings_8k([{"filing_date": "2026-06-24", "accession": "mu"}],
                              scan.expected_quarter_end(mu_facts, "CY2026Q2")) or {}).get("accession"),
      "mu")
check("and would be rejected against the calendar quarter end",
      scan.match_earnings_8k([{"filing_date": "2026-06-24", "accession": "mu"}], "2026-06-30"), None)
check("statement matching uses the first filing after quarter close",
      (scan.match_statement([
          {"filing_date": "2026-07-24", "accession": "q2"},
          {"filing_date": "2026-10-23", "accession": "q3"},
      ], "2026-06-30") or {}).get("accession"), "q2")

print("\nearnings release matching")
filings = [
    {"filing_date": "2026-04-22", "accession": "a"},
    {"filing_date": "2026-07-22", "accession": "b"},
    {"filing_date": "2026-10-21", "accession": "c"},
]
check("picks the release after the quarter closed",
      (scan.match_earnings_8k(filings, "2026-06-30") or {}).get("accession"), "b")
check("a release more than a quarter later is not claimed",
      scan.match_earnings_8k(filings, "2025-01-31"), None)
check("a release before the quarter closed is not claimed",
      scan.match_earnings_8k(filings[:1], "2026-06-30"), None)
# ADRs file 6-K, not 8-K, and file them constantly (TSM files one for monthly
# revenue), so the anchor is the consensus announcement date.
six_ks = [
    {"filing_date": "2026-07-02", "accession": "x"},
    {"filing_date": "2026-07-16", "accession": "y"},
    {"filing_date": "2026-07-24", "accession": "z"},
]
check("6-K matched to the consensus date",
      (scan.match_foreign_release(six_ks, "2026-07-16") or {}).get("accession"), "y")
check("no consensus date, no match", scan.match_foreign_release(six_ks, None), None)

# ---------------------------------------------------------------------------
print("\nguidance extraction locates forward-looking sentences only")
release = (
    "TI reports second quarter 2026 financial results\n"
    "DALLAS (July 22, 2026) -- Texas Instruments today reported second quarter revenue of "
    "$5.46 billion, net income of $1.98 billion and earnings per share of $2.14.\n"
    "TI's third quarter outlook is for revenue in the range of $5.65 billion to $6.15 billion "
    "and earnings per share between $2.23 and $2.57.\n"
    "The company remains committed to its long-term strategy of investing in manufacturing.\n"
)
ex = scan.extract_guidance_excerpts(release)
texts = " ".join(e["text"] for e in ex)
check("the outlook sentence is captured", "third quarter outlook" in texts, True)
check("the headline results sentence is NOT captured", "today reported second quarter revenue" in texts, False)
check("prose with no numbers is skipped", "long-term strategy" in texts, False)

# ---------------------------------------------------------------------------
print("\nprepared / Q&A boundary")
# The operator announces the Q&A in the first seconds of nearly every call
# ("after the speakers' remarks, there will be a question-and-answer session").
# Matching that phrase labels the whole call as Q&A and empties prepared remarks.
segments = [
    {"idx": 0, "speaker": "Operator", "content":
        "All participants are in listen-only mode. After the speakers' remarks there will be "
        "a question-and-answer session.", "section": "prepared", "sentiment": None, "title": None},
    {"idx": 1, "speaker": "CFO", "content":
        "Revenue was $5.46 billion, up 22.8% year over year.", "section": "prepared",
        "sentiment": None, "title": None},
    {"idx": 2, "speaker": "Operator", "content":
        "Our first question comes from the line of an analyst.", "section": "prepared",
        "sentiment": None, "title": None},
    {"idx": 3, "speaker": "Analyst", "content": "Can you walk through the gross margin bridge?",
     "section": "prepared", "sentiment": None, "title": None},
]
tf.mark_qa_sections(segments)
check("the opening agenda mention does not start Q&A", segments[0]["section"], "prepared")
check("prepared remarks stay prepared", segments[1]["section"], "prepared")
check("the real handoff starts Q&A", segments[2]["section"], "qa")
check("the analyst turn is in Q&A", segments[3]["section"], "qa")

print("Alpha Vantage quarter label follows each company's fiscal calendar")
fiscal_quarter_cases = [
    ("calendar Q1", "2026-03-31", 12, "2026Q1"),
    ("calendar Q2", "2026-06-30", 12, "2026Q2"),
    ("calendar Q3", "2026-09-30", 12, "2026Q3"),
    ("calendar Q4", "2026-12-31", 12, "2026Q4"),
    ("MSFT Q1", "2025-09-30", 6, "2026Q1"),
    ("MSFT Q2", "2025-12-31", 6, "2026Q2"),
    ("MSFT Q3", "2026-03-31", 6, "2026Q3"),
    ("MSFT Q4", "2026-06-30", 6, "2026Q4"),
    ("NVDA Q1", "2026-04-28", 1, "2027Q1"),
    ("AVGO fixed-week Q3", "2026-08-02", 10, "2026Q3"),
    ("MU Q3", "2026-05-28", 8, "2026Q3"),
    ("QCOM Q3", "2026-06-28", 9, "2026Q3"),
    ("QRVO Q1", "2026-06-27", 3, "2027Q1"),
    ("STX fixed-week Q4", "2026-07-03", 7, "2026Q4"),
]
for case_name, quarter_end, fye_month, expected in fiscal_quarter_cases:
    check(case_name, tf.fiscal_quarter_label(sec._parse_ymd(quarter_end), fye_month), expected)

print("legacy Alpha Vantage cache compatibility")
matching_av = {"source": "alpha_vantage", "url":
               "https://www.alphavantage.co/query?function=EARNINGS_CALL_TRANSCRIPT&symbol=NVDA&quarter=2027Q1"}
wrong_av = {"source": "alpha_vantage", "url":
            "https://www.alphavantage.co/query?function=EARNINGS_CALL_TRANSCRIPT&symbol=NVDA&quarter=2026Q2"}
check("requested AV quarter is parsed", tf.av_quarter_from_url(matching_av["url"]), "2027Q1")
check("matching AV cache is reusable", scan.transcript_cache_matches(matching_av, "2027Q1"), True)
check("legacy calendar-quarter AV cache is invalidated",
      scan.transcript_cache_matches(wrong_av, "2027Q1"), False)
check("AV cache without quarter metadata is invalidated",
      scan.transcript_cache_matches({"source": "alpha_vantage", "url": None}, "2027Q1"), False)
check("date-matched Fool cache remains reusable",
      scan.transcript_cache_matches({"source": "motley_fool", "url": "https://fool.test/call"},
                                    "2027Q1"), True)

# ---------------------------------------------------------------------------
print("\nderived metrics")
check("pct_change basic", scan.pct_change(120.0, 100.0), (20.0, ""))
# "Net income grew 350%" off a loss is arithmetic, not growth.
check("negative base is refused", scan.pct_change(50.0, -10.0), (None, "base_nonpositive"))
check("zero base is refused", scan.pct_change(50.0, 0.0), (None, "base_zero"))
check("missing base is flagged", scan.pct_change(50.0, None), (None, "base_missing"))
check("ratio", scan.ratio_pct(61.0, 100.0), 61.0)
check("ratio guards zero denominator", scan.ratio_pct(61.0, 0), None)
check("pp difference", scan.diff_pp(61.4, 57.9), 3.5)
check("USD to millions", scan.to_musd(5_463_000_000), 5463.0)

print("price reaction")
bars = [
    {"date": "2026-07-20", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
    {"date": "2026-07-21", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
    {"date": "2026-07-22", "open": 101, "high": 103, "low": 100, "close": 102, "volume": 1200},
    {"date": "2026-07-23", "open": 108, "high": 110, "low": 107, "close": 109, "volume": 3000},
    {"date": "2026-07-24", "open": 109, "high": 110, "low": 108, "close": 110, "volume": 1500},
]
rx = scan.price_reaction(bars, "2026-07-22")
# US tech reports both pre-open and post-close and the 8-K does not say which,
# so both sessions are reported and neither is called "the" reaction.
check("same-day change", rx["same_day_pct"], 0.99)          # 101 -> 102
check("next-day change", rx["next_day_pct"], 6.86)          # 102 -> 109
check("gap found on the session that moved", rx["gap_open_pct"], 5.88)  # 102 close -> 108 open
check("gap direction", rx["gap_dir"], "up")
check("unfilled gap", rx["gap_status"], "intact")
check("52-week position at the high", rx["position_52w"], 1.0)
check_true("a fresh unfilled gap is flagged as untested", "gap_untested" in (rx["note"] or ""))
check("no bars degrades cleanly", scan.price_reaction([], "2026-07-22")["note"], "no_bars")
check("no announcement date degrades cleanly",
      scan.price_reaction(bars, None)["note"], "no_announce_date")
old_bars = [
    {"date": "2025-07-01", "open": 100, "high": 103, "low": 99, "close": 102, "volume": 1000},
    {"date": "2025-07-02", "open": 110, "high": 112, "low": 109, "close": 111, "volume": 2000},
]
old_rx = scan.price_reaction(old_bars, "2024-05-01")
check("unrelated historical bars are rejected", old_rx["note"], "no_bar_near_announcement")
check("no false historical next-day move", old_rx["next_day_pct"], None)
check("old frames request enough Yahoo history",
      scan.price_history_range("2024-06-30", sec._parse_ymd("2026-07-29")), "5y")

history_bars = [
    {"date": f"{i:03d}", "open": 100 + i, "high": 102 + i,
     "low": 99 + i, "close": 101 + i, "volume": 1_000 + i}
    for i in range(300)
]
history_window = scan.price_history_window(history_bars, "100")
check("K-line window is capped at 120 sessions", len(history_window), 120)
check("K-line window keeps 40 pre-announcement sessions", history_window[0]["date"], "060")
check("K-line window includes the announcement session",
      next(i for i, b in enumerate(history_window) if b["date"] >= "100"), 40)
check("K-line window preserves post-announcement path", history_window[-1]["date"], "179")

print("EPS surprise stability guard")
# Intel missing a 0.10 consensus by 0.20 prints as +200%; the percentage says
# nothing about the size of the beat, so it gets flagged rather than quoted.
check("near-zero consensus is unstable", abs(0.10) < 0.25, True)
check("normal consensus is stable", abs(1.91) < 0.25, False)
screen_growth = {
    "revenue": {"yoy_pct": 1.0, "qoq_pct": 1.0},
    "net_income": {"yoy_pct": 1.0},
}
screen_margins = {"gross": {"yoy_pp": 0.0}}
screen_quality = {
    "ocf_to_net_income_pct": 100.0,
    "receivable_vs_revenue_gap_pp": 0.0,
    "inventory_vs_revenue_gap_pp": 0.0,
}
unstable_screen = scan.screen_block(
    screen_growth, screen_margins, screen_quality,
    {"surprise_pct": 200.0, "surprise_pct_unstable": True},
    {"gap_dir": None, "gap_status": "none"}, scan.DEFAULT_THRESHOLDS)
check("unstable percentage does not trigger eps beat",
      "eps_beat" in unstable_screen["hits"], False)

print("currency preservation")
local_cur = {"revenue": {"val": 1_270_380_000_000, "unit": "TWD"}}
local_growth = scan.growth_block(local_cur, {}, {})
check("local currency is not labelled USD", local_growth["revenue"]["value_musd"], None)
check("local unit is preserved", local_growth["revenue"]["unit"], "TWD")
check("local amount is preserved in millions",
      local_growth["revenue"]["value_local_millions"], 1_270_380.0)

print("period HTML script safety")
unsafe_view = {
    "frame": "CY2026Q2",
    "companies": [{"headline": "</script><script>alert(1)</script>"}],
    "not_reported": [],
    "buckets": [],
}
safe_html = period_html.render_html(unsafe_view)
check("script-closing input is escaped", "</script><script>alert(1)</script>" in safe_html, False)
check("escaped script-closing input remains data", "\\u003c/script\\u003e" in safe_html, True)
check("operational coverage cards are omitted", "Alpha Vantage 余额" in safe_html, False)
check("chain analysis section is omitted", "产业链传导" in safe_html, False)
check_true("filters sit above the list-detail grid",
           safe_html.index('class="toolbar filters"') < safe_html.index("<main>"))
check("filter toolbar is forced to one line", "white-space:nowrap" in safe_html, True)
check("K-line renderer is included", "function klineHtml(c)" in safe_html, True)
check("earnings marker is included", "财报发布 ${esc(c.announced)}" in safe_html, True)

view_with_bars = period_html.build_view(
    "CY2026Q2",
    {"companies": [{
        "ticker": "TEST", "name": "Test", "bucket": "test", "chain_role": "test",
        "announcement": {"date": "100"}, "growth": {}, "margins": {},
        "quality": {}, "balance": {}, "surprise": {}, "price_reaction": {},
        "price_history": history_bars, "transcript": {}, "screen": {},
    }]},
    {}, {},
)
check("period view embeds exactly 120 K-line sessions",
      len(view_with_bars["companies"][0]["price_history"]), 120)
check("period view keeps the earnings marker date visible",
      any(b["date"] >= "100" for b in view_with_bars["companies"][0]["price_history"]), True)

print("verdict context excludes operational and chain analysis")


class _ContextStore:
    def load_verdicts(self, frame):
        return {}

    def transcript_status(self, frames):
        return {}


ctx = verdict.build_context(
    "CY2026Q2",
    {"meta": {"reported_count": 24, "av_budget": {"remaining": 0}},
     "buckets": [{"bucket": "ai_compute_chips"}],
     "transmission_chains": [{"name": "legacy"}],
     "companies": []},
    _ContextStore(),
)
check("coverage metadata is absent from verdict context", "evidence_meta" in ctx, False)
check("bucket aggregates are absent from verdict context", "buckets" in ctx, False)
check("transmission chains are absent from verdict context", "transmission_chains" in ctx, False)
check("record shape only asks for companies", list(ctx["how_to_record"]["shape"]), ["companies"])

# ---------------------------------------------------------------------------
print("\nuniverse")
uni = scan.load_universe(Path(__file__).resolve().parent.parent / "assets" / "universe.yaml")
check_true("universe loads a meaningful number of companies", len(uni["companies"]) >= 90,
           f"got {len(uni['companies'])}")
check_true("every company has a chain role",
           all(c.get("chain_role") for c in uni["companies"].values()))
check("bare ON parsed as a ticker, not a boolean", "ON" in uni["companies"], True)
check_true("MRVL's second bucket is recorded rather than dropped",
           bool(uni["companies"]["MRVL"]["also_in"]))

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
    raise SystemExit(1)
print("all checks passed")
