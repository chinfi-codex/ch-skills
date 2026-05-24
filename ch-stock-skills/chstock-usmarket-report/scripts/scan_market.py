#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scan the whole US market for daily movers above a threshold.

Pulls Yahoo Finance's predefined `day_gainers` / `day_losers` screeners and
filters client-side by absolute change % and market cap. The script does not
explain why a ticker moved — that's left to the model via WebSearch.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

YAHOO_SCREENER_URL = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
SCREENER_FETCH_COUNT = 250  # Yahoo's hard cap is ~250 per request; covers the vast majority of >±7% movers
EVIDENCE_TYPE = "us_market_wide_movers_evidence"


def fetch_screener(scr_id: str, count: int = SCREENER_FETCH_COUNT) -> List[Dict[str, Any]]:
    """Pull one Yahoo predefined screener (day_gainers | day_losers)."""
    response = requests.get(
        YAHOO_SCREENER_URL,
        params={
            "formatted": "false",
            "lang": "en-US",
            "region": "US",
            "scrIds": scr_id,
            "count": count,
        },
        headers=REQUEST_HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    results = (payload.get("finance") or {}).get("result") or []
    if not results:
        return []
    return results[0].get("quotes") or []


def normalize_quote(quote: Dict[str, Any]) -> Dict[str, Any]:
    market_cap = quote.get("marketCap") or 0
    ts = quote.get("regularMarketTime")
    trade_date = (
        datetime.utcfromtimestamp(ts).date().isoformat() if isinstance(ts, (int, float)) else None
    )
    change_pct = quote.get("regularMarketChangePercent") or 0.0
    return {
        "ticker": quote.get("symbol"),
        "name": quote.get("longName") or quote.get("displayName") or quote.get("symbol"),
        "change_pct": round(float(change_pct), 4),
        "close": quote.get("regularMarketPrice"),
        "prev_close": quote.get("regularMarketPreviousClose"),
        "market_cap": market_cap,
        "market_cap_billion": round(market_cap / 1e9, 2) if market_cap else None,
        "volume": quote.get("regularMarketVolume"),
        "exchange": quote.get("exchange"),
        "currency": quote.get("currency"),
        "trade_date": trade_date,
    }


def filter_and_sort(
    quotes: List[Dict[str, Any]],
    direction: str,
    min_abs_change: float,
    min_market_cap: float,
    limit: int,
) -> List[Dict[str, Any]]:
    """Filter raw quotes by direction + thresholds, sort, and cap."""
    matched: List[Dict[str, Any]] = []
    for raw in quotes:
        change = raw.get("regularMarketChangePercent") or 0.0
        cap = raw.get("marketCap") or 0
        if cap < min_market_cap:
            continue
        if direction == "rise" and change < min_abs_change:
            continue
        if direction == "drop" and change > -min_abs_change:
            continue
        matched.append(normalize_quote(raw))
    matched.sort(key=lambda item: item["change_pct"], reverse=(direction == "rise"))
    return matched[:limit]


def scan_movers(
    min_change_pct: float = 7.0,
    min_market_cap_usd: float = 10_000_000_000,
    limit_per_side: int = 20,
) -> Dict[str, Any]:
    """Run both gainers and losers screeners, filter, and return evidence."""
    errors: Dict[str, str] = {}

    try:
        gainers = fetch_screener("day_gainers")
    except Exception as exc:
        gainers, errors["day_gainers"] = [], str(exc)
    try:
        losers = fetch_screener("day_losers")
    except Exception as exc:
        losers, errors["day_losers"] = [], str(exc)

    rises = filter_and_sort(gainers, "rise", min_change_pct, min_market_cap_usd, limit_per_side)
    drops = filter_and_sort(losers, "drop", min_change_pct, min_market_cap_usd, limit_per_side)

    # Pull a representative trade_date from the first available match
    scan_date = None
    for item in rises + drops:
        if item.get("trade_date"):
            scan_date = item["trade_date"]
            break

    return {
        "type": EVIDENCE_TYPE,
        "scan_date": scan_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "thresholds": {
            "min_change_pct": min_change_pct,
            "min_market_cap_usd": min_market_cap_usd,
            "min_market_cap_billion": round(min_market_cap_usd / 1e9, 2),
            "limit_per_side": limit_per_side,
        },
        "screener_raw_counts": {
            "day_gainers": len(gainers),
            "day_losers": len(losers),
        },
        "rises": rises,
        "drops": drops,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan US market for daily movers (Yahoo screener).")
    parser.add_argument("--min-change", type=float, default=7.0, help="|涨跌幅| 阈值（百分比），默认 7.0")
    parser.add_argument(
        "--min-cap-billion",
        type=float,
        default=10.0,
        help="市值阈值（10亿美元为单位），默认 10（即 $10B）",
    )
    parser.add_argument("--limit", type=int, default=20, help="每个方向最多保留几只，默认 20")
    parser.add_argument("--output", "-o", help="输出 JSON 文件路径")
    args = parser.parse_args()

    evidence = scan_movers(
        min_change_pct=args.min_change,
        min_market_cap_usd=args.min_cap_billion * 1e9,
        limit_per_side=args.limit,
    )
    content = json.dumps(evidence, ensure_ascii=False, indent=2, default=str)

    if args.output:
        path = Path(args.output).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": "success",
                    "output": str(path),
                    "scan_date": evidence.get("scan_date"),
                    "rises": len(evidence.get("rises", [])),
                    "drops": len(evidence.get("drops", [])),
                },
                ensure_ascii=False,
            )
        )
        return
    print(content)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    main()
