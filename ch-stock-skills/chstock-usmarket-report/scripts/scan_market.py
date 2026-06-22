#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scan Nasdaq-listed movers for the day, money-first (by dollar volume).

Pulls Yahoo Finance's predefined `day_gainers` / `day_losers` screeners, keeps
only Nasdaq-listed equities (deterministic `exchange` filter), computes each
name's dollar volume (close × shares) as the US analog of A-share 成交额, and
returns a money-effect candidate pool sorted by dollar volume — not by raw
percentage. The point is to surface *where the money actually went tonight*,
including names outside the watchlist, so the model can group them into themes.

The script deliberately does NOT decide which names are "tech" or what theme
they belong to. The predefined screener does not carry a usable sector field
(verified: `sector`/`industry` come back null), and the custom screener needs
crumb auth, so any taxonomy here would be unreliable. Sector/theme judgment is
the model's job downstream — it reads `ticker` + `name`. The script only does
the deterministic Nasdaq-exchange + liquidity gating, and flags
|change| >= abnormal_pct so the model knows which names need a WebSearch.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests

YAHOO_SCREENER_URL = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
SCREENER_FETCH_COUNT = 250  # Yahoo's hard cap per request; covers the vast majority of movers
EVIDENCE_TYPE = "us_nasdaq_movers_evidence"

# Nasdaq tiers: Global Select (NMS) / Global Market (NGM) / Capital Market (NCM).
NASDAQ_EXCHANGE_CODES: Set[str] = {"NMS", "NGM", "NCM"}

DEFAULT_MIN_CHANGE_PCT = 3.0             # pool inclusion floor: a meaningful daily move (no price limits in US)
DEFAULT_ABNORMAL_PCT = 7.0               # |change| >= this -> is_abnormal (WebSearch trigger)
DEFAULT_MIN_DOLLAR_VOLUME = 50_000_000   # liquidity gate; the "money" floor (DMS 成交额 analog)
DEFAULT_MIN_MARKET_CAP = 0.0             # keep low: dollar volume is the primary liquidity gate, not cap
DEFAULT_LIMIT_PER_SIDE = 60              # money-effect pool can be larger than the old ±7% Top20 tail


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


def is_nasdaq(exchange: Optional[str], full_name: Optional[str], allowed: Set[str]) -> bool:
    """Deterministic Nasdaq membership: prefer the exchange code, fall back to the display name.

    An empty `allowed` set means the filter is disabled (debug `--all-exchanges`).
    """
    if not allowed:
        return True
    if exchange and exchange in allowed:
        return True
    return bool(full_name and str(full_name).lower().startswith("nasdaq"))


def normalize_quote(quote: Dict[str, Any], abnormal_pct: float) -> Dict[str, Any]:
    market_cap = quote.get("marketCap") or 0
    ts = quote.get("regularMarketTime")
    trade_date = (
        datetime.utcfromtimestamp(ts).date().isoformat() if isinstance(ts, (int, float)) else None
    )
    change_pct = float(quote.get("regularMarketChangePercent") or 0.0)
    close = quote.get("regularMarketPrice")
    volume = quote.get("regularMarketVolume")
    dollar_volume = (
        float(close) * float(volume) if close is not None and volume is not None else None
    )
    return {
        "ticker": quote.get("symbol"),
        "name": quote.get("longName") or quote.get("displayName") or quote.get("symbol"),
        "change_pct": round(change_pct, 4),
        "close": close,
        "prev_close": quote.get("regularMarketPreviousClose"),
        "market_cap": market_cap,
        "market_cap_billion": round(market_cap / 1e9, 2) if market_cap else None,
        "volume": volume,
        # dollar volume = close × shares; the US analog of A-share 成交额, and the
        # weight used to rank "where the money went" downstream.
        "dollar_volume": round(dollar_volume, 2) if dollar_volume is not None else None,
        "dollar_volume_million": round(dollar_volume / 1e6, 1) if dollar_volume is not None else None,
        "is_abnormal": abs(change_pct) >= abnormal_pct,
        "exchange": quote.get("exchange"),
        "full_exchange_name": quote.get("fullExchangeName"),
        "currency": quote.get("currency"),
        # marketState (PRE/REGULAR/POST/CLOSED): dollar_volume = price × volume is a
        # post-close turnover proxy; if the session isn't done, the ranking reflects
        # partial-day flow. Surfaced (not gated) so the model can flag an intraday scan.
        "market_state": quote.get("marketState"),
        "trade_date": trade_date,
    }


def filter_and_sort(
    quotes: List[Dict[str, Any]],
    direction: str,
    min_abs_change: float,
    min_market_cap: float,
    min_dollar_volume: float,
    limit: int,
    abnormal_pct: float,
    allowed_exchanges: Set[str],
) -> List[Dict[str, Any]]:
    """Keep Nasdaq names past the change/cap/dollar-volume gates, sort by money, cap."""
    matched: List[Dict[str, Any]] = []
    for raw in quotes:
        if not is_nasdaq(raw.get("exchange"), raw.get("fullExchangeName"), allowed_exchanges):
            continue
        change = float(raw.get("regularMarketChangePercent") or 0.0)
        cap = raw.get("marketCap") or 0
        if cap < min_market_cap:
            continue
        if direction == "rise" and change < min_abs_change:
            continue
        if direction == "drop" and change > -min_abs_change:
            continue
        item = normalize_quote(raw, abnormal_pct)
        if item["dollar_volume"] is None or item["dollar_volume"] < min_dollar_volume:
            continue
        matched.append(item)
    # money-first: rank by dollar volume, not by raw percentage (DMS 成交额优先).
    matched.sort(key=lambda it: it.get("dollar_volume") or 0.0, reverse=True)
    return matched[:limit]


def count_nasdaq(quotes: List[Dict[str, Any]], allowed: Set[str]) -> Dict[str, int]:
    """How many raw screener hits survived the Nasdaq-exchange gate (transparency)."""
    nas = sum(1 for q in quotes if is_nasdaq(q.get("exchange"), q.get("fullExchangeName"), allowed))
    return {"nasdaq": nas, "other": len(quotes) - nas}


def scan_movers(
    min_change_pct: float = DEFAULT_MIN_CHANGE_PCT,
    abnormal_pct: float = DEFAULT_ABNORMAL_PCT,
    min_dollar_volume_usd: float = DEFAULT_MIN_DOLLAR_VOLUME,
    min_market_cap_usd: float = DEFAULT_MIN_MARKET_CAP,
    limit_per_side: int = DEFAULT_LIMIT_PER_SIDE,
    allowed_exchanges: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Run both screeners, keep Nasdaq names, rank by dollar volume, return evidence."""
    allowed = allowed_exchanges if allowed_exchanges is not None else NASDAQ_EXCHANGE_CODES
    errors: Dict[str, str] = {}

    try:
        gainers = fetch_screener("day_gainers")
    except Exception as exc:
        gainers, errors["day_gainers"] = [], str(exc)
    try:
        losers = fetch_screener("day_losers")
    except Exception as exc:
        losers, errors["day_losers"] = [], str(exc)

    rises = filter_and_sort(
        gainers, "rise", min_change_pct, min_market_cap_usd, min_dollar_volume_usd,
        limit_per_side, abnormal_pct, allowed,
    )
    drops = filter_and_sort(
        losers, "drop", min_change_pct, min_market_cap_usd, min_dollar_volume_usd,
        limit_per_side, abnormal_pct, allowed,
    )

    scan_date = None
    for item in rises + drops:
        if item.get("trade_date"):
            scan_date = item["trade_date"]
            break

    # Post-close report expects CLOSED/POST; surface the states present so the model
    # can warn when a scan was run mid-session (REGULAR) and the ranking is partial-day.
    market_states = sorted({it.get("market_state") for it in rises + drops if it.get("market_state")})

    return {
        "type": EVIDENCE_TYPE,
        "scope_note": (
            "Nasdaq 交易所过滤为确定性脚本逻辑；是否属于科技、归属哪个主题，"
            "由模型读 ticker + 名称在下游判断。池子按 dollar_volume 降序（成交额优先）。"
        ),
        "scan_date": scan_date,
        "market_states": market_states,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "thresholds": {
            "min_change_pct": min_change_pct,
            "abnormal_pct": abnormal_pct,
            "min_dollar_volume_usd": min_dollar_volume_usd,
            "min_dollar_volume_million": round(min_dollar_volume_usd / 1e6, 1),
            "min_market_cap_usd": min_market_cap_usd,
            "limit_per_side": limit_per_side,
            "nasdaq_exchanges": sorted(allowed),
        },
        "screener_raw_counts": {
            "day_gainers": len(gainers),
            "day_losers": len(losers),
        },
        # How many raw hits were Nasdaq vs other — shows the exchange filter's bite.
        "exchange_breakdown": {
            "gainers": count_nasdaq(gainers, allowed),
            "losers": count_nasdaq(losers, allowed),
        },
        "rises": rises,
        "drops": drops,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan Nasdaq movers for the day, ranked by dollar volume.")
    parser.add_argument("--min-change", type=float, default=DEFAULT_MIN_CHANGE_PCT, help="进池 |涨跌幅| 阈值（百分比），默认 3.0")
    parser.add_argument("--abnormal-pct", type=float, default=DEFAULT_ABNORMAL_PCT, help="|涨跌幅| ≥ 此值标记为异动（触发联网核查），默认 7.0")
    parser.add_argument("--min-dollar-volume-million", type=float, default=DEFAULT_MIN_DOLLAR_VOLUME / 1e6, help="成交额下限（百万美元），默认 50（即 $50M）")
    parser.add_argument("--min-cap-billion", type=float, default=DEFAULT_MIN_MARKET_CAP / 1e9, help="市值下限（10亿美元为单位），默认 0（靠成交额过滤，不靠市值）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT_PER_SIDE, help="每个方向最多保留几只（按成交额排序后截断），默认 60")
    parser.add_argument("--all-exchanges", action="store_true", help="调试用：关闭 Nasdaq 过滤，保留全部交易所")
    parser.add_argument("--output", "-o", help="输出 JSON 文件路径")
    args = parser.parse_args()

    evidence = scan_movers(
        min_change_pct=args.min_change,
        abnormal_pct=args.abnormal_pct,
        min_dollar_volume_usd=args.min_dollar_volume_million * 1e6,
        min_market_cap_usd=args.min_cap_billion * 1e9,
        limit_per_side=args.limit,
        allowed_exchanges=set() if args.all_exchanges else None,
    )
    if args.all_exchanges:
        # With the gate off, is_nasdaq() short-circuits to True for everything.
        evidence["thresholds"]["nasdaq_exchanges"] = "ALL (filter disabled)"
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
