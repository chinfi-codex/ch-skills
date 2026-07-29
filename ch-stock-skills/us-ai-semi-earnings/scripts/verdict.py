#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The model's ledger: what it decided, and the integrity checks on that.

Two commands, mirroring the pattern the A-share sibling settled on:

    verdict.py context --frame CY2026Q2      # what still needs judging
    verdict.py record  --frame CY2026Q2 --input <verdict.json>

`context` answers "what is new since I last judged" — companies that reported
after the last verdict was written, plus companies already judged whose evidence
has since moved (the 10-Q landed, or the transcript finally appeared, so the
earlier call was made on less). During a reporting season the pending set is a
handful a day, not the whole universe.

`record` validates and stores. The validation is the point of the file: it will
not let the ledger claim more certainty than the evidence supports.

- `transcript_read: true` is rejected when no transcript is cached for that
  company and quarter. Reading the call is the difference between a real
  guidance judgement and a guess dressed as one, so the claim has to be backed.
- `guidance_call` other than `未给指引` is rejected when the evidence pack found
  no guidance excerpts *and* no transcript — with neither, there is nothing a
  guidance verdict could have been formed from.

Nothing here decides anything. Every judgement in this file arrives from the
model through `--input`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sec_client as sec  # noqa: E402
from store import Store  # noqa: E402

TIERS = ["强", "中", "观察", "剔除"]
# 无法判断 exists because the alternative is worse: an ADR that files no 10-Q has
# no cash-flow statement at all, and forcing that into 存疑 would record a
# negative finding where there is simply no evidence either way.
QUALITY_CALLS = ["扎实", "尚可", "存疑", "虚高", "无法判断"]
GUIDANCE_CALLS = ["显著上修", "上修", "维持", "下修", "显著下修", "未给指引"]

_SCRIPT_DIR = Path(__file__).resolve().parent


def _load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _brief(row: Dict[str, Any]) -> Dict[str, Any]:
    """The compact per-company card the model judges from.

    Deliberately lossy: the full evidence stays in the scan JSON, and the
    context file is meant to be read end-to-end in one pass. Everything kept
    here is something a tier decision actually turns on.
    """
    g = row.get("growth") or {}
    m = row.get("margins") or {}
    q = row.get("quality") or {}
    b = row.get("balance") or {}
    s = row.get("surprise") or {}
    p = row.get("price_reaction") or {}
    t = row.get("transcript") or {}

    def gv(concept: str, key: str) -> Any:
        return ((g.get(concept) or {}).get(key))

    return {
        "ticker": row["ticker"],
        "name": row.get("name"),
        "bucket": row.get("bucket"),
        "chain_role": row.get("chain_role"),
        "data_stage": row.get("data_stage"),
        "statement_source": row.get("statement_source"),
        "announced": (row.get("announcement") or {}).get("date"),
        "revenue_musd": gv("revenue", "value_musd"),
        "revenue_yoy_pct": gv("revenue", "yoy_pct"),
        "revenue_qoq_pct": gv("revenue", "qoq_pct"),
        "net_income_musd": gv("net_income", "value_musd"),
        "net_income_yoy_pct": gv("net_income", "yoy_pct"),
        "gross_margin_pct": (m.get("gross") or {}).get("pct"),
        "gross_margin_yoy_pp": (m.get("gross") or {}).get("yoy_pp"),
        "sbc_pct_of_revenue": (m.get("sbc") or {}).get("pct"),
        "ocf_to_net_income_pct": q.get("ocf_to_net_income_pct"),
        "fcf_musd": q.get("free_cash_flow_musd"),
        "inventory_gap_pp": q.get("inventory_vs_revenue_gap_pp"),
        "receivable_gap_pp": q.get("receivable_vs_revenue_gap_pp"),
        "capex_to_revenue_pct": q.get("capex_to_revenue_pct"),
        "rpo_yoy_pct": b.get("rpo_yoy_pct"),
        "eps_surprise_pct": s.get("surprise_pct"),
        "eps_reported": s.get("eps_reported"),
        "eps_estimated": s.get("eps_estimated"),
        "price_same_day_pct": p.get("same_day_pct"),
        "price_next_day_pct": p.get("next_day_pct"),
        "gap_open_pct": p.get("gap_open_pct"),
        "gap_status": p.get("gap_status"),
        "position_52w": p.get("position_52w"),
        "guidance_excerpt_count": len(row.get("guidance_excerpts") or []),
        "guidance_excerpts": [e["text"] for e in (row.get("guidance_excerpts") or [])][:6],
        "transcript_status": t.get("status"),
        "transcript_source": t.get("source"),
        "transcript_segments": (t.get("stats") or {}).get("segment_count"),
        "screen_hits": (row.get("screen") or {}).get("hits"),
        "screen_penalty": (row.get("screen") or {}).get("penalty"),
        "rank_score": (row.get("screen") or {}).get("rank_score"),
        "press_release_head": (row.get("press_release_head") or "")[:600] or None,
    }


def build_context(frame: str, evidence: Dict[str, Any], store: Store,
                  *, include_all: bool = False) -> Dict[str, Any]:
    verdicts = store.load_verdicts(frame)
    transcripts = store.transcript_status([frame])
    rows = evidence.get("companies") or []

    pending: List[Dict[str, Any]] = []
    revisit: List[Dict[str, Any]] = []
    for row in rows:
        ticker = row["ticker"]
        prior = verdicts.get(ticker)
        card = _brief(row)
        if prior is None:
            pending.append(card)
            continue
        # Already judged. It comes back only if the ground under it moved.
        reasons: List[str] = []
        if not prior.get("transcript_read") and (row.get("transcript") or {}).get("status") == "ok":
            reasons.append("transcript now available — the earlier call was made without it")
        if row.get("data_stage") == "xbrl" and (prior.get("evidence_digest") or {}).get(
                "data_stage") == "press_release_only":
            reasons.append("10-Q landed — cash flow and balance sheet are now readable")
        prior_rev = (prior.get("evidence_digest") or {}).get("revenue_yoy_pct")
        cur_rev = card["revenue_yoy_pct"]
        if prior_rev is not None and cur_rev is not None and abs(cur_rev - prior_rev) >= 3.0:
            reasons.append(f"revenue YoY restated {prior_rev}% -> {cur_rev}%")
        if reasons or include_all:
            revisit.append({**card, "prior_verdict": prior, "revisit_reasons": reasons})

    return {
        "frame": frame,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "enums": {"tier": TIERS, "quality_call": QUALITY_CALLS,
                  "guidance_call": GUIDANCE_CALLS},
        "pending": pending,
        "revisit": revisit,
        "already_judged": sorted(set(verdicts) - {r["ticker"] for r in revisit}),
        "transcript_availability": {
            t: v.get("status") for (t, f), v in transcripts.items() if f == frame},
        "how_to_record": {
            "write": "reports/usearn_verdict_<frame>.json",
            "shape": {
                "companies": [{
                    "ticker": "TXN", "tier": "|".join(TIERS),
                    "quality_call": "|".join(QUALITY_CALLS),
                    "guidance_call": "|".join(GUIDANCE_CALLS),
                    "transcript_read": "true only if a transcript is cached for this frame",
                    "theme": "short phrase, e.g. 'analog cycle recovery'",
                    "headline": "one sentence, plain language",
                    "reasons": ["evidence-backed bullets"],
                    "watch_items": ["what would change this call"],
                }],
            },
            "then": f"verdict.py record --frame {frame} --input <that file>",
        },
    }


def validate_companies(records: Sequence[Dict[str, Any]], evidence_index: Dict[str, Dict[str, Any]],
                       transcripts: Dict[str, str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    ok: List[Dict[str, Any]] = []
    errors: List[str] = []
    for i, rec in enumerate(records):
        ticker = str(rec.get("ticker", "")).strip().upper()
        where = f"companies[{i}]{' ' + ticker if ticker else ''}"
        if not ticker:
            errors.append(f"{where}: missing ticker")
            continue
        row = evidence_index.get(ticker)
        if row is None:
            errors.append(f"{where}: not in this frame's reported set")
            continue
        if rec.get("tier") not in TIERS:
            errors.append(f"{where}: tier must be one of {TIERS}, got {rec.get('tier')!r}")
        if rec.get("quality_call") not in QUALITY_CALLS:
            errors.append(f"{where}: quality_call must be one of {QUALITY_CALLS}, "
                          f"got {rec.get('quality_call')!r}")
        guidance = rec.get("guidance_call")
        if guidance not in GUIDANCE_CALLS:
            errors.append(f"{where}: guidance_call must be one of {GUIDANCE_CALLS}, "
                          f"got {guidance!r}")

        raw_claimed_read = rec.get("transcript_read")
        if not isinstance(raw_claimed_read, bool):
            errors.append(f"{where}: transcript_read must be a JSON boolean")
            claimed_read = False
        else:
            claimed_read = raw_claimed_read
        available = transcripts.get(ticker) == "ok"
        if claimed_read and not available:
            errors.append(
                f"{where}: transcript_read=true but no transcript is cached for this frame — "
                f"fetch it first or set transcript_read=false")
        has_guidance_evidence = bool(row.get("guidance_excerpts")) or available
        if guidance and guidance != "未给指引" and not has_guidance_evidence:
            errors.append(
                f"{where}: guidance_call={guidance!r} but the evidence has neither guidance "
                f"excerpts nor a transcript — nothing to base it on")
        if not str(rec.get("headline", "")).strip():
            errors.append(f"{where}: headline is required")
        reasons = rec.get("reasons")
        if not isinstance(reasons, list) or not reasons:
            errors.append(f"{where}: at least one reason is required")
        watch_items = rec.get("watch_items")
        if watch_items is not None and not isinstance(watch_items, list):
            errors.append(f"{where}: watch_items must be a list")

        ok.append({
            "ticker": ticker,
            "tier": rec.get("tier"),
            "quality_call": rec.get("quality_call"),
            "guidance_call": guidance,
            "transcript_read": claimed_read,
            "theme": rec.get("theme"),
            "headline": rec.get("headline"),
            "reasons": reasons,
            "watch_items": watch_items,
            # Snapshot of what the call was made against, so a later run can tell
            # whether the evidence moved underneath a verdict.
            "evidence_digest": {
                "data_stage": row.get("data_stage"),
                "revenue_yoy_pct": ((row.get("growth") or {}).get("revenue") or {}).get("yoy_pct"),
                "eps_surprise_pct": (row.get("surprise") or {}).get("surprise_pct"),
                "transcript_status": (row.get("transcript") or {}).get("status"),
                "announced": (row.get("announcement") or {}).get("date"),
            },
        })
    return ok, errors


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Model verdict ledger for the earnings scan")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ctx = sub.add_parser("context", help="what still needs judging this frame")
    ctx.add_argument("--frame", required=True)
    ctx.add_argument("--evidence", default=None, help="scan JSON (default: reports/usearn_scan_<frame>.json)")
    ctx.add_argument("--out", default=None)
    ctx.add_argument("--all", action="store_true", help="include already-judged companies")

    rec = sub.add_parser("record", help="validate and store a verdict JSON")
    rec.add_argument("--frame", required=True)
    rec.add_argument("--input", required=True)
    rec.add_argument("--evidence", default=None)
    rec.add_argument("--dry-run", action="store_true", help="validate without writing")

    args = ap.parse_args(argv)
    frame = args.frame.upper()
    if not sec.CALENDAR_QUARTER_RE.fullmatch(frame):
        print(f"[error] --frame must look like CY2026Q2, got {frame!r}", file=sys.stderr)
        return 2

    ev_path = args.evidence or str(_SCRIPT_DIR.parent / "reports" / f"usearn_scan_{frame}.json")
    evidence = _load_json(ev_path)
    if not evidence:
        print(f"[error] cannot read evidence pack {ev_path} — run earnings_scan.py first",
              file=sys.stderr)
        return 2
    evidence_frame = str((evidence.get("meta") or {}).get("frame") or "").upper()
    if evidence_frame != frame:
        print(f"[error] evidence frame mismatch: --frame={frame}, "
              f"evidence.meta.frame={evidence_frame or '<missing>'}", file=sys.stderr)
        return 2

    store = Store()
    if not store.available:
        print(f"[error] the ledger needs the database: {store.error}", file=sys.stderr)
        return 1

    if args.cmd == "context":
        out = build_context(frame, evidence, store, include_all=args.all)
        path = Path(args.out or (_SCRIPT_DIR.parent / "reports" / f"usearn_ctx_{frame}.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[context] pending={len(out['pending'])} revisit={len(out['revisit'])} "
              f"already_judged={len(out['already_judged'])} -> {path}")
        return 0

    payload = _load_json(args.input)
    if not payload:
        print(f"[error] cannot read {args.input}", file=sys.stderr)
        return 2
    payload_frame = str(payload.get("frame") or "").upper()
    if payload_frame and payload_frame != frame:
        print(f"[error] verdict input frame mismatch: --frame={frame}, "
              f"input.frame={payload_frame}", file=sys.stderr)
        return 2

    index = {r["ticker"]: r for r in (evidence.get("companies") or [])}
    transcripts = {t: v.get("status") for (t, f), v in
                   store.transcript_status([frame]).items() if f == frame}
    companies, errs = validate_companies(payload.get("companies") or [], index, transcripts)
    errors = errs
    if errors:
        print(f"[reject] {len(errors)} problem(s) — nothing was written:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"[ok] {len(companies)} company verdicts validate; "
              f"--dry-run so nothing was written")
        return 0

    n = store.save_verdicts(frame, companies)
    print(f"[record] {n} company verdicts stored for {frame}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
