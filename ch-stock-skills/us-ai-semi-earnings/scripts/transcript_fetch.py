#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Earnings-call transcript retrieval — two independent free paths.

The call is where the guidance, the customer names and the analyst pushback
live; none of it is in XBRL and most of it is not in the press release either.
There is no free, complete, official source for it, so this module runs two
imperfect ones and always says which answered.

**Path A — Motley Fool (primary).** Free, no key, no quota. Discovery is the
per-ticker quote page `fool.com/quote/{exchange}/{ticker}/`, which links the
last ~8 transcripts; the transcript page then yields the full call as
`Speaker: content` paragraphs. Measured limitation, not a bug to fix: coverage
is *selective and irregular*. TSM's and ASML's 2026-Q2 calls were up within a
day; TXN's, five days after the call, was still absent. Fool is therefore the
cheap first try, never the thing to wait on.

**Path B — Alpha Vantage (fallback + enrichment).** `EARNINGS_CALL_TRANSCRIPT`
returns the same call already split by speaker *with a per-segment sentiment
score*, and its coverage of large caps is more complete and faster than Fool's.
The catch is the free tier's **25 requests/day across all Alpha Vantage
endpoints** — so calls are metered by `AVBudget` and spent newest-and-largest
first. A transcript never changes once published, which is what makes the quota
survivable: each (ticker, quarter) is bought exactly once, ever, and served from
the store forever after.

Quarter identification is by **call date**, never by the quarter label either
source prints. Fool's slug carries the *fiscal* quarter (`nvidia-nvda-q1-2027-…`
is the February–April 2026 quarter) while Alpha Vantage's `quarter` parameter is
the *fiscal* quarter being reported (`2027Q1` for that same call — verified
against off-calendar filers: asking MSFT for `2026Q3` returns the April FQ3
call, not the July FQ4 one). Matching on either label directly mixes the two
conventions; matching on the date the company announced is unambiguous, so
callers pass `call_date` from the earnings 8-K and this module aligns to it.
For the Alpha Vantage path the caller should also pass `av_quarter` — the
company's fiscal quarter label derived from the reported period end and the
fiscal-year-end month (`fiscal_quarter_label`) — because deriving it from the
call date's calendar quarter is wrong for every off-calendar filer.

What this module will not do: infer, summarise, or score. Sentiment from Alpha
Vantage is passed through as the vendor's number and labelled as such. Fool's
own TAKEAWAYS/RISKS/SUMMARY blocks are editorial written by the publisher, not
words spoken on the call — they are captured separately as `publisher_notes` and
must never be quoted as management statements.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

FOOL_QUOTE_URL = "https://www.fool.com/quote/{exchange}/{ticker}/"
FOOL_EXCHANGES = ("nasdaq", "nyse", "nysemkt")
AV_ENDPOINT = "https://www.alphavantage.co/query"

# Alpha Vantage free tier. Overridable for a premium key: ALPHAVANTAGE_DAILY_LIMIT=999999
AV_DAILY_LIMIT = int(os.environ.get("ALPHAVANTAGE_DAILY_LIMIT", "25"))
# Leave headroom so a scan does not consume the entire day's quota on transcripts
# and leave none for the EPS-surprise endpoint.
AV_DEFAULT_RESERVE = int(os.environ.get("ALPHAVANTAGE_RESERVE", "5"))

_UA = {"User-Agent": "Mozilla/5.0"}

# Where prepared remarks end and Q&A begins. The split matters because the two
# halves are read differently: prepared remarks are the company's chosen
# narrative, Q&A is what the sell side would not let go of.
#
# The obvious marker — "question-and-answer session" — is the wrong one. Nearly
# every operator preamble announces the Q&A in the *first* seconds of the call
# ("after the speakers' remarks, there will be a question-and-answer session"),
# so matching it labels the entire call as Q&A and leaves prepared remarks empty.
# Verified against ASML, TSM and AVGO 2026-Q2: the phrase that appears once, at
# the actual handoff, is "first question".
_QA_MARKERS = (
    "first question",
    "question comes from",
    "question will come from",
    "we will now begin the question",
    "we'll now begin the question",
    "open the floor for questions",
    "open the line for questions",
)
_QA_ANTI_MARKERS = ("listen-only", "listen only")


class TranscriptError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get(url: str, *, attempts: int = 3, timeout: int = 40,
         params: Optional[Dict[str, Any]] = None) -> Optional[str]:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            resp = requests.get(url, headers=_UA, params=params, timeout=timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(1.2 * (2 ** i))
    raise TranscriptError(f"GET failed after {attempts} attempts: {url} ({last})")


def _cache_dir() -> Path:
    root = Path(__file__).resolve().parent.parent / ".cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def calendar_quarter(day: dt.date) -> str:
    """`2026Q2` — calendar-quarter fallback, right only for calendar-year filers."""
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def av_quarter_from_url(url: Optional[str]) -> Optional[str]:
    """Read the requested fiscal quarter from a stored Alpha Vantage URL."""
    match = re.search(r"(?:[?&])quarter=(\d{4}Q[1-4])(?:&|$)", url or "", re.IGNORECASE)
    return match.group(1).upper() if match else None


def fiscal_quarter_label(quarter_end: dt.date, fye_month: int = 12) -> str:
    """Alpha Vantage's `quarter` label for the fiscal period ending `quarter_end`.

    AV indexes transcripts by the company's *fiscal* quarter (`2026Q4` for
    MSFT's June-2026 quarter, `2027Q1` for NVDA's Feb–Apr 2026 quarter), not
    by the calendar quarter the call happened in. `fye_month` is the month the
    fiscal year ends (12 for calendar-year filers; SEC submissions expose it as
    `fiscalYearEnd`). Fiscal year is labelled by the year it ends in: a quarter
    ending in or before `fye_month` belongs to that calendar year's fiscal year,
    a quarter ending after it belongs to the next.

    Fixed-weekday fiscal calendars let a quarter close a few days into the
    following month (AVGO's Q3 ends ~Aug 2, STX's year ends ~Jul 3), so a
    period ending in the first week of a month is attributed to the month just
    finished.
    """
    fye_month = int(fye_month or 12)
    d = quarter_end
    if d.day <= 7:
        d = d.replace(day=1) - dt.timedelta(days=1)
    fy = d.year if d.month <= fye_month else d.year + 1
    q = ((d.month - fye_month - 1) % 12) // 3 + 1
    return f"{fy}Q{q}"


def _parse_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


_TAG = re.compile(r"<[^>]+>")


def _text(html_fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", html_fragment))).strip()


def starts_qa(content: str, *, segment_index: int) -> bool:
    """Whether this segment is the handoff into Q&A.

    Segment 0 is excluded: an operator preamble that mentions questions is
    describing the agenda, not opening the floor.
    """
    if segment_index == 0:
        return False
    lowered = content.lower()
    if any(a in lowered for a in _QA_ANTI_MARKERS):
        return False
    return any(k in lowered for k in _QA_MARKERS)


def mark_qa_sections(segments: List[Dict[str, Any]]) -> None:
    """Label every segment `prepared` or `qa`, in place.

    Deliberately a second pass over finished segments rather than a flag flipped
    during parsing: a speaker's turn is assembled from several paragraphs, and
    the handoff sentence often lands in a continuation paragraph appended after
    the turn was created. Deciding mid-parse reads a half-built turn and starts
    Q&A one or two speakers late — which puts the first analyst question in the
    prepared-remarks bucket.
    """
    in_qa = False
    for i, seg in enumerate(segments):
        if not in_qa and starts_qa(seg.get("content") or "", segment_index=i):
            in_qa = True
        seg["section"] = "qa" if in_qa else "prepared"


# ---------------------------------------------------------------------------
# Path A — Motley Fool
# ---------------------------------------------------------------------------

_FOOL_LINK = re.compile(r"/earnings/call-transcripts/(\d{4})/(\d{2})/(\d{2})/([a-z0-9-]+)/")


def discover_fool_transcripts(ticker: str,
                              exchanges: Sequence[str] = FOOL_EXCHANGES) -> List[Dict[str, Any]]:
    """List a ticker's transcript pages from its Fool quote page, newest first.

    Exchange is not known up front and the quote URL requires it, so we try
    nasdaq then nyse; a wrong guess is a cheap 404. Each entry carries the
    publication `date`, which is what callers match a call against — the `q?_????`
    inside the slug is the company's *fiscal* label and is deliberately not used
    for matching.
    """
    out: List[Dict[str, Any]] = []
    for exchange in exchanges:
        html = _get(FOOL_QUOTE_URL.format(exchange=exchange, ticker=ticker.lower()))
        if not html:
            continue
        seen = set()
        for y, m, d, slug in _FOOL_LINK.findall(html):
            path = f"/earnings/call-transcripts/{y}/{m}/{d}/{slug}/"
            if path in seen:
                continue
            seen.add(path)
            fiscal = re.search(r"-q(\d)-(\d{4})-", slug)
            out.append({
                "source": "motley_fool",
                "url": "https://www.fool.com" + path,
                "date": f"{y}-{m}-{d}",
                "slug": slug,
                "fiscal_label": f"FY{fiscal.group(2)}Q{fiscal.group(1)}" if fiscal else None,
                "exchange": exchange,
            })
        if out:
            break
    out.sort(key=lambda e: e["date"], reverse=True)
    return out


_H2 = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I)
_SPEAKER_P = re.compile(
    r"<p[^>]*>\s*<strong[^>]*>(.*?)</strong>\s*:?\s*(.*?)</p>", re.S | re.I)
_PLAIN_P = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)


def _split_fool_sections(body: str) -> Dict[str, str]:
    """Cut the article body at its `<h2>` headers into `{HEADER: html}`."""
    marks = [(m.start(), _text(m.group(1)).upper(), m.end()) for m in _H2.finditer(body)]
    sections: Dict[str, str] = {}
    for i, (_, name, end) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        sections[name] = body[end:stop]
    return sections


def parse_fool_transcript(html: str, url: str) -> Dict[str, Any]:
    """Turn a Fool transcript page into speaker-attributed segments.

    A new speaker starts a `<p>` whose leading `<strong>` holds the name; the
    paragraphs that follow with no `<strong>` are the same speaker continuing, so
    they are appended rather than dropped — losing them would cut most calls to
    their first paragraph per turn.
    """
    anchor = html.find('id="article-body-transcript"')
    body = html[anchor:] if anchor >= 0 else html
    body = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
    sections = _split_fool_sections(body)

    transcript_html = ""
    for name, frag in sections.items():
        if "FULL CONFERENCE CALL TRANSCRIPT" in name or name == "TRANSCRIPT":
            transcript_html = frag
            break
    if not transcript_html:
        raise TranscriptError(f"no transcript section found at {url}")
    # Everything from "Read Next" onward is site chrome, not the call.
    for tail in ("Read Next", "Premium Investing Services", "About The Motley Fool"):
        cut = transcript_html.find(f">{tail}<")
        if cut > 0:
            transcript_html = transcript_html[:cut]

    segments: List[Dict[str, Any]] = []
    pos = 0
    for m in _SPEAKER_P.finditer(transcript_html):
        # Paragraphs between the previous speaker tag and this one continue the
        # previous speaker's turn.
        if segments:
            for cont in _PLAIN_P.finditer(transcript_html[pos:m.start()]):
                frag = _text(cont.group(1))
                if frag and "<strong" not in cont.group(1).lower():
                    segments[-1]["content"] += " " + frag
        speaker = _text(m.group(1)).rstrip(":").strip()
        content = _text(m.group(2))
        if not speaker:
            pos = m.end()
            continue
        segments.append({
            "idx": len(segments),
            "speaker": speaker,
            "title": None,
            "section": "prepared",
            "content": content,
            "sentiment": None,
        })
        pos = m.end()
    if segments:
        for cont in _PLAIN_P.finditer(transcript_html[pos:]):
            frag = _text(cont.group(1))
            if frag:
                segments[-1]["content"] += " " + frag
    mark_qa_sections(segments)

    part_html = sections.get("CALL PARTICIPANTS", "")
    participants = [line for line in (_text(p) for p in _PLAIN_P.findall(part_html)) if line]
    participants += [_text(li) for li in
                     re.findall(r"<li[^>]*>(.*?)</li>", part_html, re.S | re.I)]
    participants = [p for p in participants if p and "motley fool" not in p.lower()]
    _apply_titles(segments, participants)

    # Fool's own editorial. Kept, clearly fenced, never mixed into segments.
    publisher_notes = {
        key.lower(): _text(sections[key])[:4000]
        for key in ("TAKEAWAYS", "RISKS", "SUMMARY", "INDUSTRY GLOSSARY")
        if key in sections
    }

    return {
        "source": "motley_fool",
        "url": url,
        "call_datetime_text": _text(sections.get("DATE", ""))[:120] or None,
        "participants": [p for p in participants if p][:40],
        "segments": segments,
        "publisher_notes": publisher_notes,
        "publisher_notes_warning": (
            "TAKEAWAYS / RISKS / SUMMARY are The Motley Fool's own editorial, "
            "not statements made on the call — do not attribute them to management."
        ),
        "has_sentiment": False,
    }


def _apply_titles(segments: List[Dict[str, Any]], participants: Sequence[str]) -> None:
    """Attach roles from the CALL PARTICIPANTS block.

    The block's field order is not stable across transcripts — some render
    `Name -- Title`, TSMC's renders `Title - Name` — so rather than assume a
    side, each half is checked against the speakers actually heard on the call
    and whichever half matches is the name.

    Only company representatives appear in this block; sell-side analysts do
    not, so a speaker with no title is usually an analyst. That asymmetry is
    left as-is rather than guessed at.
    """
    speakers = {s["speaker"].lower() for s in segments if s.get("speaker")}
    titles: Dict[str, str] = {}
    for line in participants:
        parts = re.split(r"\s+--\s+|\s+—\s+|\s+–\s+|\s+-\s+", line, maxsplit=1)
        if len(parts) != 2:
            continue
        left, right = parts[0].strip(), parts[1].strip()
        if left.lower() in speakers:
            titles[left.lower()] = right
        elif right.lower() in speakers:
            titles[right.lower()] = left
    for seg in segments:
        seg["title"] = titles.get(seg["speaker"].lower())


def fetch_fool_transcript(url: str) -> Dict[str, Any]:
    html = _get(url, timeout=60)
    if not html:
        raise TranscriptError(f"transcript page not found: {url}")
    return parse_fool_transcript(html, url)


# ---------------------------------------------------------------------------
# Path B — Alpha Vantage (quota-metered)
# ---------------------------------------------------------------------------

class AVBudget:
    """Daily call ledger for Alpha Vantage's 25-requests/day free tier.

    Kept as a small JSON file rather than in PostgreSQL on purpose: the budget
    must still be enforced when the database is down, which is exactly when a
    scan is most likely to retry and burn the day's quota. Alpha Vantage resets
    on UTC day boundaries, so the ledger is keyed by UTC date.
    """

    def __init__(self, path: Optional[Path] = None, *, limit: int = AV_DAILY_LIMIT,
                 reserve: int = AV_DEFAULT_RESERVE):
        self.path = path or (_cache_dir() / "av_budget.json")
        self.limit = limit
        self.reserve = max(0, reserve)
        self._state = self._load()

    def _today(self) -> str:
        return dt.datetime.now(dt.timezone.utc).date().isoformat()

    def _load(self) -> Dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("date") != self._today():
            state = {"date": self._today(), "used": 0, "calls": []}
        return state

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._state), encoding="utf-8")
        except OSError:
            pass

    @property
    def used(self) -> int:
        return int(self._state.get("used", 0))

    def remaining(self, *, respect_reserve: bool = True) -> int:
        cap = self.limit - (self.reserve if respect_reserve else 0)
        return max(0, cap - self.used)

    def spend(self, label: str, *, respect_reserve: bool = True) -> bool:
        """Consume one call. Returns False when the budget is exhausted."""
        if self.remaining(respect_reserve=respect_reserve) <= 0:
            return False
        self._state["used"] = self.used + 1
        self._state.setdefault("calls", []).append(
            {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "label": label})
        self._save()
        return True

    def status(self) -> Dict[str, Any]:
        return {"date": self._state.get("date"), "limit": self.limit, "reserve": self.reserve,
                "used": self.used, "remaining": self.remaining(),
                "remaining_ignoring_reserve": self.remaining(respect_reserve=False)}


def get_av_key() -> str:
    key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
    if key:
        return key
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env"):
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("ALPHAVANTAGE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def av_request(function: str, budget: AVBudget, *, respect_reserve: bool = True,
               **params: Any) -> Tuple[Optional[Any], str]:
    """One budgeted Alpha Vantage call. Returns `(payload, status)`.

    Status is one of `ok`, `no_key`, `budget_exhausted`, `rate_limited`,
    `empty`, `error`. `rate_limited` means the vendor refused despite our ledger
    thinking there was room — the ledger is then fast-forwarded to the cap so the
    rest of the run stops trying.
    """
    key = get_av_key()
    if not key:
        return None, "no_key"
    if not budget.spend(f"{function}:{params.get('symbol', '')}:{params.get('quarter', '')}",
                        respect_reserve=respect_reserve):
        return None, "budget_exhausted"
    query = {"function": function, "apikey": key, **params}
    try:
        resp = requests.get(AV_ENDPOINT, params=query, headers=_UA, timeout=60)
        resp.raise_for_status()
    except requests.RequestException:
        return None, "error"
    if function == "EARNINGS_CALENDAR":  # CSV endpoint
        return resp.text, "ok"
    try:
        data = resp.json()
    except ValueError:
        return None, "error"
    if isinstance(data, dict) and ("Information" in data or "Note" in data):
        budget._state["used"] = budget.limit  # vendor says done for the day
        budget._save()
        return None, "rate_limited"
    if not data:
        return None, "empty"
    return data, "ok"


def fetch_av_transcript(ticker: str, quarter: str, budget: AVBudget,
                        *, respect_reserve: bool = True) -> Dict[str, Any]:
    """`quarter` is the FISCAL quarter being reported, e.g. `2026Q4` for MSFT's
    June-2026 quarter — build it with `fiscal_quarter_label`, never from the
    call date's calendar quarter."""
    data, status = av_request("EARNINGS_CALL_TRANSCRIPT", budget,
                              respect_reserve=respect_reserve,
                              symbol=ticker.upper(), quarter=quarter)
    if status != "ok" or not isinstance(data, dict):
        return {"source": "alpha_vantage", "status": status, "segments": []}
    raw = data.get("transcript") or []
    if not raw:
        return {"source": "alpha_vantage", "status": "not_published", "segments": []}

    segments: List[Dict[str, Any]] = []
    for i, seg in enumerate(raw):
        content = (seg.get("content") or "").strip()
        sentiment = seg.get("sentiment")
        try:
            sentiment = float(sentiment) if sentiment is not None else None
        except (TypeError, ValueError):
            sentiment = None
        segments.append({
            "idx": i,
            "speaker": (seg.get("speaker") or "").strip(),
            "title": (seg.get("title") or "").strip() or None,
            "section": "prepared",
            "content": content,
            "sentiment": sentiment,
        })
    mark_qa_sections(segments)
    return {
        "source": "alpha_vantage",
        "status": "ok",
        "url": f"{AV_ENDPOINT}?function=EARNINGS_CALL_TRANSCRIPT&symbol={ticker.upper()}&quarter={quarter}",
        "quarter_label": data.get("quarter"),
        "call_datetime_text": None,
        "participants": sorted({
            f"{s['speaker']} -- {s['title']}" for s in segments
            if s["speaker"] and s["title"]
        })[:40],
        "segments": segments,
        "publisher_notes": {},
        "has_sentiment": any(s["sentiment"] is not None for s in segments),
        "sentiment_note": "sentiment scores are Alpha Vantage's own model output, not a company disclosure",
    }


# ---------------------------------------------------------------------------
# unified entry point
# ---------------------------------------------------------------------------

def summarise(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic shape stats — never a content judgement."""
    segs = payload.get("segments") or []
    prepared = [s for s in segs if s["section"] == "prepared"]
    qa = [s for s in segs if s["section"] == "qa"]
    speakers = sorted({s["speaker"] for s in segs if s.get("speaker")})
    return {
        "segment_count": len(segs),
        "char_count": sum(len(s.get("content") or "") for s in segs),
        "prepared_segments": len(prepared),
        "qa_segments": len(qa),
        "qa_split_found": bool(qa),
        "speakers": speakers[:40],
        "has_sentiment": bool(payload.get("has_sentiment")),
    }


def get_transcript(ticker: str, *, call_date: Optional[str] = None,
                   av_quarter: Optional[str] = None,
                   budget: Optional[AVBudget] = None,
                   prefer: str = "fool", date_tolerance_days: int = 6,
                   allow_av: bool = True,
                   respect_reserve: bool = True) -> Dict[str, Any]:
    """Fetch one call, trying the free path first.

    `call_date` (YYYY-MM-DD, normally the earnings 8-K filing date) is how the
    right call is identified: a Fool page is accepted when published within
    `date_tolerance_days` of it. For Alpha Vantage, pass `av_quarter` — the
    fiscal-quarter label of the reported period from `fiscal_quarter_label`;
    without it the calendar quarter of `call_date` is used, which is wrong for
    every off-calendar filer (MSFT, MU, LRCX, KLAC, NVDA…). Without `call_date`,
    the newest available call is returned and `matched_by` says
    `latest_available` so the caller knows the alignment was not verified.

    Always returns a dict; `status` is `ok`, `pending`, or an error label.
    Nothing is fabricated when both paths miss — `pending` means the call has not
    been published by either source yet, which during earnings week is the normal
    state for the first day or two.
    """
    budget = budget or AVBudget()
    target = _parse_date(call_date)
    attempts: List[Dict[str, Any]] = []

    def _try_fool() -> Optional[Dict[str, Any]]:
        try:
            candidates = discover_fool_transcripts(ticker)
        except TranscriptError as exc:
            attempts.append({"source": "motley_fool", "status": "error", "detail": str(exc)})
            return None
        if not candidates:
            attempts.append({"source": "motley_fool", "status": "no_listing"})
            return None
        pick, matched_by = None, "latest_available"
        if target:
            near = [
                (abs((_parse_date(c["date"]) - target).days), c) for c in candidates
                if _parse_date(c["date"])
            ]
            near = [(d, c) for d, c in near if d <= date_tolerance_days]
            if near:
                near.sort(key=lambda t: t[0])
                pick, matched_by = near[0][1], "call_date"
        else:
            pick = candidates[0]
        if pick is None:
            attempts.append({"source": "motley_fool", "status": "no_match_for_date",
                             "detail": f"latest listed {candidates[0]['date']}"})
            return None
        try:
            payload = fetch_fool_transcript(pick["url"])
        except TranscriptError as exc:
            attempts.append({"source": "motley_fool", "status": "parse_failed", "detail": str(exc)})
            return None
        if not payload["segments"]:
            attempts.append({"source": "motley_fool", "status": "empty_body"})
            return None
        payload.update({"status": "ok", "matched_by": matched_by,
                        "published_date": pick["date"], "fiscal_label": pick["fiscal_label"]})
        return payload

    def _try_av() -> Optional[Dict[str, Any]]:
        if not allow_av:
            attempts.append({"source": "alpha_vantage", "status": "disabled"})
            return None
        quarter = av_quarter or calendar_quarter(target or dt.date.today())
        if av_quarter is None:
            attempts.append({"source": "alpha_vantage", "status": "calendar_quarter_fallback",
                             "detail": "av_quarter not supplied; wrong for off-calendar filers"})
        payload = fetch_av_transcript(ticker, quarter, budget, respect_reserve=respect_reserve)
        if payload.get("status") != "ok":
            attempts.append({"source": "alpha_vantage", "status": payload.get("status"),
                             "detail": f"quarter={quarter}"})
            return None
        payload.update({"matched_by": "call_date" if target else "latest_available",
                        "published_date": None})
        return payload

    order = (_try_fool, _try_av) if prefer == "fool" else (_try_av, _try_fool)
    for fn in order:
        payload = fn()
        if payload:
            payload["ticker"] = ticker.upper()
            payload["call_date_hint"] = call_date
            payload["attempts"] = attempts
            payload["stats"] = summarise(payload)
            payload["av_budget"] = budget.status()
            return payload

    return {
        "ticker": ticker.upper(), "status": "pending", "segments": [],
        "call_date_hint": call_date, "attempts": attempts,
        "av_budget": budget.status(),
        "stats": {"segment_count": 0, "char_count": 0, "prepared_segments": 0,
                  "qa_segments": 0, "qa_split_found": False, "speakers": [],
                  "has_sentiment": False},
        "note": "no transcript available from either source yet — report as pending, do not infer call content",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Earnings-call transcript fetch (Fool primary, Alpha Vantage fallback)")
    ap.add_argument("ticker")
    ap.add_argument("--call-date", default=None, help="earnings announcement date YYYY-MM-DD (from the 8-K)")
    ap.add_argument("--prefer", choices=("fool", "av"), default="fool")
    ap.add_argument("--no-av", action="store_true", help="never spend Alpha Vantage quota")
    ap.add_argument("--ignore-reserve", action="store_true", help="allow dipping into the reserved quota")
    ap.add_argument("--list", action="store_true", help="only list Fool's transcript pages for the ticker")
    ap.add_argument("--budget-status", action="store_true", help="print the Alpha Vantage day ledger and exit")
    ap.add_argument("--out", default=None, help="write the full transcript JSON here")
    ap.add_argument("--head", type=int, default=6, help="segments to preview")
    args = ap.parse_args(argv)

    budget = AVBudget()
    if args.budget_status:
        print(json.dumps(budget.status(), indent=2))
        return 0

    if args.list:
        for c in discover_fool_transcripts(args.ticker):
            print(f"{c['date']}  {c['fiscal_label'] or '-':10}  {c['url']}")
        return 0

    payload = get_transcript(args.ticker, call_date=args.call_date, budget=budget,
                             prefer=args.prefer, allow_av=not args.no_av,
                             respect_reserve=not args.ignore_reserve)
    stats = payload["stats"]
    print(f"{payload['ticker']} status={payload['status']} source={payload.get('source', '-')} "
          f"matched_by={payload.get('matched_by', '-')} published={payload.get('published_date', '-')}")
    print(f"  segments={stats['segment_count']} chars={stats['char_count']} "
          f"prepared={stats['prepared_segments']} qa={stats['qa_segments']} sentiment={stats['has_sentiment']}")
    if payload.get("attempts"):
        for a in payload["attempts"]:
            print(f"  attempt {a['source']}: {a['status']} {a.get('detail', '')}")
    print(f"  av budget: {json.dumps(payload['av_budget'])}")
    for seg in (payload.get("segments") or [])[:args.head]:
        title = f" ({seg['title']})" if seg.get("title") else ""
        sent = f" sentiment={seg['sentiment']}" if seg.get("sentiment") is not None else ""
        print(f"  [{seg['section']}] {seg['speaker']}{title}{sent}: {seg['content'][:180]}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  -> {args.out}")
    return 0 if payload["status"] == "ok" else 3


if __name__ == "__main__":
    raise SystemExit(main())
