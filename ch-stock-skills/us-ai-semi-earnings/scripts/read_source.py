#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""On-demand reader for the two long-form sources the scan only summarises.

The evidence pack carries derived numbers, guidance excerpts and transcript
*statistics* — not 50,000 characters of call transcript per company, which
would make the pack unreadable and mostly unread. When a specific question
needs the words themselves, this pulls them from the cache:

    read_source.py press-release TXN --frame CY2026Q2
    read_source.py transcript TSM --frame CY2026Q2 --section qa
    read_source.py transcript NVDA --frame CY2026Q1 --find "gross margin,supply"
    read_source.py search "CoWoS" --frame CY2026Q2

`search` is the cross-company one and the reason segments are stored per turn:
it answers "who else talked about this, and did they volunteer it or did an
analyst have to ask" in a single query across every cached call.

Nothing is summarised here. The output is the source text with its speaker,
section and citation, so anything quoted downstream can be attributed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from store import Store  # noqa: E402


def _print_segments(segments: Sequence[Dict[str, Any]], *, ticker: str, frame: str,
                    find: Optional[List[str]], max_chars: int,
                    speaker: Optional[str]) -> int:
    shown = 0
    for seg in segments:
        content = seg.get("content") or ""
        if speaker and speaker.lower() not in (seg.get("speaker") or "").lower():
            continue
        if find and not any(k.lower() in content.lower() for k in find):
            continue
        title = f" ({seg['title']})" if seg.get("title") else ""
        sent = f" [sentiment {seg['sentiment']}]" if seg.get("sentiment") is not None else ""
        print(f"\n--- {ticker} {frame} #{seg.get('idx')} [{seg.get('section')}] "
              f"{seg.get('speaker')}{title}{sent}")
        print(content[:max_chars] + ("…" if len(content) > max_chars else ""))
        shown += 1
    return shown


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Read cached press releases and transcripts")
    sub = ap.add_subparsers(dest="mode", required=True)

    pr = sub.add_parser("press-release", help="print a cached 8-K/6-K earnings release")
    pr.add_argument("ticker")
    pr.add_argument("--frame", required=True)
    pr.add_argument("--find", default=None, help="only lines containing these terms (comma separated)")
    pr.add_argument("--max-chars", type=int, default=20000)

    tr = sub.add_parser("transcript", help="print a cached earnings call")
    tr.add_argument("ticker")
    tr.add_argument("--frame", required=True)
    tr.add_argument("--section", choices=("prepared", "qa"), default=None)
    tr.add_argument("--speaker", default=None, help="only turns by this speaker")
    tr.add_argument("--find", default=None, help="only turns containing these terms (comma separated)")
    tr.add_argument("--max-chars", type=int, default=4000, help="per segment")
    tr.add_argument("--stats", action="store_true", help="print shape stats and speakers only")

    se = sub.add_parser("search", help="keyword search across every cached call")
    se.add_argument("term")
    se.add_argument("--frame", default=None, help="restrict to one calendar quarter")
    se.add_argument("--tickers", default=None)
    se.add_argument("--section", choices=("prepared", "qa"), default=None)
    se.add_argument("--limit", type=int, default=40)
    se.add_argument("--max-chars", type=int, default=700)
    se.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    store = Store()
    if not store.available:
        print(f"[error] cache unavailable: {store.error}", file=sys.stderr)
        return 1

    if args.mode == "press-release":
        rows = store._query(
            "SELECT accession, filing_date, exhibit_type, url, text FROM usearn_press_release "
            "WHERE ticker = %s AND frame = %s ORDER BY filing_date DESC",
            (args.ticker.upper(), args.frame.upper()))
        if not rows:
            print(f"[none] no cached press release for {args.ticker} {args.frame} — "
                  f"run earnings_scan.py for that frame first", file=sys.stderr)
            return 3
        acc, fdate, etype, url, text = rows[0]
        print(f"# {args.ticker.upper()} {args.frame.upper()} {etype or ''} filed {fdate}\n# {url}\n")
        if args.find:
            terms = [t.strip().lower() for t in args.find.split(",") if t.strip()]
            for line in text.split("\n"):
                if any(t in line.lower() for t in terms):
                    print(line)
        else:
            print(text[:args.max_chars] + ("…" if len(text) > args.max_chars else ""))
        return 0

    if args.mode == "transcript":
        payload = store.load_transcript(args.ticker.upper(), args.frame.upper())
        if not payload:
            print(f"[none] no cached transcript for {args.ticker} {args.frame}", file=sys.stderr)
            return 3
        if payload.get("status") != "ok":
            print(f"[pending] status={payload.get('status')} — the call has not been "
                  f"published by either source", file=sys.stderr)
            return 3
        segments = payload["segments"]
        print(f"# {args.ticker.upper()} {args.frame.upper()} via {payload.get('source')} "
              f"({payload.get('url')})")
        if payload.get("participants"):
            print("# participants: " + "; ".join(payload["participants"]))
        if payload.get("publisher_notes"):
            print("# NOTE: publisher_notes on this record are the transcript site's own "
                  "editorial, not words spoken on the call — never attribute them to management.")
        if args.stats:
            speakers: Dict[str, int] = {}
            for s in segments:
                speakers[s["speaker"]] = speakers.get(s["speaker"], 0) + 1
            print(json.dumps({
                "segments": len(segments),
                "prepared": sum(1 for s in segments if s["section"] == "prepared"),
                "qa": sum(1 for s in segments if s["section"] == "qa"),
                "chars": sum(len(s["content"] or "") for s in segments),
                "speakers": speakers,
            }, indent=2, ensure_ascii=False))
            return 0
        if args.section:
            segments = [s for s in segments if s["section"] == args.section]
        find = [t.strip() for t in args.find.split(",")] if args.find else None
        shown = _print_segments(segments, ticker=args.ticker.upper(), frame=args.frame.upper(),
                                find=find, max_chars=args.max_chars, speaker=args.speaker)
        if not shown:
            print("[none] no segment matched the filter", file=sys.stderr)
            return 3
        return 0

    hits = store.search_segments(
        args.term,
        frames=[args.frame.upper()] if args.frame else None,
        tickers=[t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None,
        section=args.section, limit=args.limit)
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0 if hits else 3
    if not hits:
        print(f"[none] '{args.term}' not found in any cached call "
              f"(frame={args.frame or 'any'})", file=sys.stderr)
        return 3
    by_ticker: Dict[str, int] = {}
    for h in hits:
        by_ticker[h["ticker"]] = by_ticker.get(h["ticker"], 0) + 1
    print(f"# '{args.term}': {len(hits)} turns across {len(by_ticker)} companies "
          f"({', '.join(f'{k}×{v}' for k, v in sorted(by_ticker.items()))})")
    for h in hits:
        excerpt = h["content"]
        m = re.search(re.escape(args.term), excerpt, re.I)
        if m:
            lo = max(0, m.start() - args.max_chars // 3)
            excerpt = ("…" if lo else "") + excerpt[lo:lo + args.max_chars]
        title = f" ({h['title']})" if h.get("title") else ""
        print(f"\n--- {h['ticker']} {h['frame']} #{h['idx']} [{h['section']}] {h['speaker']}{title}")
        print(excerpt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
