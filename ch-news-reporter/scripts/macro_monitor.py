#!/usr/bin/env python3
"""Fetch macro market evidence for ch-news-reporter.

The script only retrieves structured evidence. It does not write reports or
decide whether a macro signal is bullish, bearish, or important.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests


SHANGHAI = ZoneInfo("Asia/Shanghai")
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY")
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN")
JIN10_AUTH_TOKEN = os.getenv("JIN10_AUTH_TOKEN")

JIN10_QUOTE_CODES = {
    "BRENT": "UKOIL",
    "GOLD": "XAUUSD",
    "NATURAL_GAS": "NGAS",
    "USD_CNY": "USDCNH",
}

STOOQ_BACKUP_URLS = {
    "BTC": "https://stooq.com/q/l/?s=btcusd&i=d",
    "GOLD": "https://stooq.com/q/l/?s=xauusd&i=d",
    "WTI": "https://stooq.com/q/l/?s=cl.f&i=d",
    "BRENT": "https://stooq.com/q/l/?s=bz.f&i=d",
    "NATURAL_GAS": "https://stooq.com/q/l/?s=ng.f&i=d",
    "NASDAQ_FUTURES": "https://stooq.com/q/l/?s=nq.f&i=d",
}
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Symbols fetched with 1y daily history so we can compute 52w high/low,
# YTD performance, and 20/60 day moving averages. These are the "market
# position" anchors required by the macro daily methodology.
YAHOO_POSITION_SYMBOLS: dict[str, str] = {
    "US_TREASURY_10Y": "^TNX",
    "US_TREASURY_5Y": "^FVX",
    "NASDAQ_COMPOSITE": "^IXIC",
    "SHANGHAI_COMPOSITE": "000001.SS",
    "VIX": "^VIX",
    "DXY": "DX-Y.NYB",
    "BTC": "BTC-USD",
}


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def ok_quote(value: Any) -> bool:
    return isinstance(value, dict) and not value.get("_error") and value.get("close") is not None


def fetch_jin10_quotes() -> dict[str, Any]:
    if not JIN10_AUTH_TOKEN:
        return {"sources": {"jin10_mcp": "MISSING_CONFIG"}, "data": {}}

    try:
        from jin10_mcp import Jin10McpClient
    except Exception as exc:
        return {"sources": {"jin10_mcp": "ERROR"}, "data": {}, "error": str(exc)}

    client = Jin10McpClient()
    data: dict[str, Any] = {}
    try:
        for internal_name, code in JIN10_QUOTE_CODES.items():
            try:
                payload = client.get_quote(code)
                row = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                data[internal_name] = {
                    "name": row.get("name") or internal_name,
                    "code": row.get("code") or code,
                    "time": row.get("time") or "",
                    "open": row.get("open"),
                    "close": row.get("close"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "volume": row.get("volume"),
                    "ups_price": row.get("ups_price"),
                    "ups_percent": row.get("ups_percent"),
                    "source": "jin10_mcp",
                }
            except Exception as exc:
                data[internal_name] = {"_error": str(exc), "source": "jin10_mcp"}
    finally:
        client.close()

    status = "OK" if any(ok_quote(value) for value in data.values()) else "ERROR"
    return {"sources": {"jin10_mcp": status}, "data": data}


def alpha_request(params: dict[str, str]) -> dict[str, Any]:
    if not ALPHAVANTAGE_API_KEY:
        return {"_error": "MISSING_CONFIG"}
    query = dict(params)
    query["apikey"] = ALPHAVANTAGE_API_KEY
    response = requests.get("https://www.alphavantage.co/query", params=query, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if "Note" in payload or "Information" in payload:
        return {"_error": "RATE_LIMITED", "raw": payload}
    return payload


def fetch_alpha_vantage_market() -> dict[str, Any]:
    if not ALPHAVANTAGE_API_KEY:
        return {"sources": {"alpha_vantage": "MISSING_CONFIG"}, "data": {}}

    data: dict[str, Any] = {}
    status = "OK"

    try:
        payload = alpha_request(
            {"function": "TREASURY_YIELD", "interval": "daily", "maturity": "10year"}
        )
        if payload.get("_error"):
            status = str(payload["_error"])
        rows = payload.get("data") if isinstance(payload.get("data"), list) else []
        if rows:
            data["US_TREASURY_10Y"] = {
                "value": rows[0].get("value"),
                "date": rows[0].get("date"),
                "source": "alpha_vantage",
            }
    except Exception as exc:
        data["US_TREASURY_10Y"] = {"_error": str(exc), "source": "alpha_vantage"}

    try:
        payload = alpha_request(
            {
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": "BTC",
                "to_currency": "USD",
            }
        )
        rate = payload.get("Realtime Currency Exchange Rate", {})
        if rate:
            data["BTC"] = {
                "close": rate.get("5. Exchange Rate"),
                "time": rate.get("6. Last Refreshed"),
                "source": "alpha_vantage",
            }
    except Exception as exc:
        data["BTC"] = {"_error": str(exc), "source": "alpha_vantage"}

    for key, function_name in (
        ("WTI", "WTI"),
        ("BRENT", "BRENT"),
        ("NATURAL_GAS", "NATURAL_GAS"),
    ):
        try:
            payload = alpha_request({"function": function_name, "interval": "daily"})
            rows = payload.get("data") if isinstance(payload.get("data"), list) else []
            if rows:
                data[key] = {
                    "close": rows[0].get("value"),
                    "date": rows[0].get("date"),
                    "source": "alpha_vantage",
                }
        except Exception as exc:
            data[key] = {"_error": str(exc), "source": "alpha_vantage"}

    if not any(value for value in data.values()):
        status = "ERROR" if status == "OK" else status
    return {"sources": {"alpha_vantage": status}, "data": data}


def fetch_yahoo_chart(
    symbol: str, range_label: str = "1y", interval: str = "1d"
) -> dict[str, Any]:
    """Fetch a Yahoo Finance daily series and derive position metrics.

    Returns close, previous_close, change_pct, 52w high/low, % off 52w high,
    % above 52w low, YTD %, MA20, MA60. Errors are returned as ``_error``.
    """

    url = YAHOO_CHART_URL.format(symbol=symbol)
    try:
        response = requests.get(
            url,
            params={"range": range_label, "interval": interval},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return {
            "_error": str(exc),
            "symbol": symbol,
            "source": "yahoo_finance",
        }

    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        return {"_error": "EMPTY", "symbol": symbol, "source": "yahoo_finance"}

    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quote_list = (result.get("indicators") or {}).get("quote") or [{}]
    quote = quote_list[0] if quote_list else {}
    closes_raw = quote.get("close") or []

    pairs = [
        (int(ts), float(close))
        for ts, close in zip(timestamps, closes_raw)
        if ts is not None and close is not None and not math.isnan(float(close))
    ]
    if not pairs:
        return {"_error": "NO_DATA", "symbol": symbol, "source": "yahoo_finance"}

    closes = [close for _, close in pairs]
    last_ts, last_close = pairs[-1]
    prev_close = (
        pairs[-2][1] if len(pairs) >= 2 else meta.get("chartPreviousClose")
    )

    change_pct = None
    if prev_close:
        try:
            change_pct = (last_close - float(prev_close)) / float(prev_close) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            change_pct = None

    high_52w = max(closes)
    low_52w = min(closes)
    pct_off_high = (
        (last_close - high_52w) / high_52w * 100 if high_52w else None
    )
    pct_above_low = (
        (last_close - low_52w) / low_52w * 100 if low_52w else None
    )

    current_year = datetime.fromtimestamp(last_ts, SHANGHAI).year
    ytd_pairs = [
        (ts, close)
        for ts, close in pairs
        if datetime.fromtimestamp(ts, SHANGHAI).year == current_year
    ]
    ytd_pct = None
    if ytd_pairs:
        first_close = ytd_pairs[0][1]
        if first_close:
            ytd_pct = (last_close - first_close) / first_close * 100

    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None

    trend_vs_ma20 = None
    if ma20:
        trend_vs_ma20 = (last_close - ma20) / ma20 * 100

    return {
        "symbol": symbol,
        "close": round(last_close, 4),
        # `value` alias keeps existing consumers (geopolitical_daily, downstream
        # report templates) reading US_TREASURY_10Y.value unchanged.
        "value": round(last_close, 4),
        "previous_close": round(float(prev_close), 4) if prev_close else None,
        "change_pct": round(change_pct, 3) if change_pct is not None else None,
        "time": datetime.fromtimestamp(last_ts, SHANGHAI).isoformat(
            timespec="seconds"
        ),
        "range_52w_high": round(high_52w, 4),
        "range_52w_low": round(low_52w, 4),
        "pct_off_52w_high": round(pct_off_high, 2)
        if pct_off_high is not None
        else None,
        "pct_above_52w_low": round(pct_above_low, 2)
        if pct_above_low is not None
        else None,
        "ytd_pct": round(ytd_pct, 2) if ytd_pct is not None else None,
        "ma20": round(ma20, 4) if ma20 is not None else None,
        "ma60": round(ma60, 4) if ma60 is not None else None,
        "pct_vs_ma20": round(trend_vs_ma20, 2)
        if trend_vs_ma20 is not None
        else None,
        "history_points": len(closes),
        "source": "yahoo_finance",
    }


def fetch_yahoo_position_quotes() -> dict[str, Any]:
    data: dict[str, Any] = {}
    any_ok = False
    for key, symbol in YAHOO_POSITION_SYMBOLS.items():
        quote = fetch_yahoo_chart(symbol)
        data[key] = quote
        if "_error" not in quote and quote.get("close") is not None:
            any_ok = True
    return {
        "sources": {"yahoo_finance": "OK" if any_ok else "ERROR"},
        "data": data,
    }


def fetch_stooq_price(symbol_key: str) -> dict[str, Any] | None:
    url = STOOQ_BACKUP_URLS.get(symbol_key)
    if not url:
        return None
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text)))
    if not rows:
        return None
    row = rows[-1]
    close = row.get("Close")
    if not close or close.upper() == "N/D":
        return None
    return {
        "close": close,
        "date": row.get("Date"),
        "time": row.get("Time"),
        "source": "stooq_backup",
    }


def fetch_stooq_backup() -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key in STOOQ_BACKUP_URLS:
        try:
            quote = fetch_stooq_price(key)
            if quote:
                data[key] = quote
        except Exception as exc:
            data[key] = {"_error": str(exc), "source": "stooq_backup"}
    status = "OK" if any(ok_quote(value) for value in data.values()) else "ERROR"
    return {"sources": {"stooq_backup": status}, "data": data}


def merge_missing_quotes(base: dict[str, Any], supplement: dict[str, Any]) -> None:
    for key, value in supplement.items():
        if ok_quote(base.get(key)) or (
            key == "US_TREASURY_10Y"
            and isinstance(base.get(key), dict)
            and base[key].get("value") is not None
        ):
            continue
        if value is not None:
            base[key] = value


LIQUIDITY_GROUP = (
    "US_TREASURY_10Y",
    "US_TREASURY_5Y",
    "DXY",
    "USD_CNY",
)
EQUITY_POSITION_GROUP = (
    "NASDAQ_COMPOSITE",
    "SHANGHAI_COMPOSITE",
    "NASDAQ_FUTURES",
)
RISK_APPETITE_GROUP = ("VIX", "BTC", "GOLD")
COMMODITY_GROUP = ("BRENT", "WTI", "NATURAL_GAS")


def build_signal_groups(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Group market data by analysis lens for the macro daily template."""

    def pick(keys: tuple[str, ...]) -> dict[str, Any]:
        return {key: data[key] for key in keys if key in data}

    return {
        "liquidity_rates_fx": pick(LIQUIDITY_GROUP),
        "equity_position": pick(EQUITY_POSITION_GROUP),
        "risk_appetite": pick(RISK_APPETITE_GROUP),
        "commodities": pick(COMMODITY_GROUP),
    }


def fetch_market_signals(use_backup: bool = False) -> dict[str, Any]:
    results = {"timestamp": now_iso(), "sources": {}, "data": {}}

    jin10 = fetch_jin10_quotes()
    results["sources"].update(jin10["sources"])
    results["data"].update(jin10["data"])

    yahoo = fetch_yahoo_position_quotes()
    results["sources"].update(yahoo["sources"])
    merge_missing_quotes(results["data"], yahoo["data"])

    alpha = fetch_alpha_vantage_market()
    results["sources"].update(alpha["sources"])
    merge_missing_quotes(results["data"], alpha["data"])

    needs_backup = use_backup or any(
        key not in results["data"] or not ok_quote(results["data"].get(key))
        for key in ("BRENT", "GOLD", "NATURAL_GAS", "BTC", "NASDAQ_FUTURES")
    )
    if needs_backup:
        backup = fetch_stooq_backup()
        results["sources"].update(backup["sources"])
        merge_missing_quotes(results["data"], backup["data"])

    results["groups"] = build_signal_groups(results["data"])
    return results


def tushare_pro():
    if not TUSHARE_TOKEN:
        return None
    import tushare as ts

    return ts.pro_api(TUSHARE_TOKEN)


def clean_scalar(value: Any) -> Any:
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except TypeError:
        pass
    if hasattr(value, "item"):
        try:
            return clean_scalar(value.item())
        except Exception:
            return value
    return value


def row_payload(row: Any) -> dict[str, Any]:
    raw = {key: clean_scalar(value) for key, value in row.to_dict().items()}
    return raw


def first_present(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is None:
            value = raw.get(key.upper())
        if value is not None and value != "":
            return value
    return None


def latest_china_indicator(pro: Any, indicator: str) -> dict[str, Any] | None:
    if indicator == "CPI":
        frame = pro.cn_cpi(limit=1)
        if frame.empty:
            return None
        row = frame.iloc[0]
        raw = row_payload(row)
        return {
            "value": first_present(raw, ("cpi", "nt_val", "value")),
            "yoy": first_present(raw, ("cpi_yoy", "nt_yoy", "yoy")),
            "date": first_present(raw, ("month", "date")),
            "raw": raw,
        }
    if indicator == "PPI":
        frame = pro.cn_ppi(limit=1)
        if frame.empty:
            return None
        row = frame.iloc[0]
        raw = row_payload(row)
        return {
            "value": first_present(raw, ("ppi", "value")),
            "yoy": first_present(raw, ("ppi_yoy", "yoy")),
            "date": first_present(raw, ("month", "date")),
            "raw": raw,
        }
    if indicator == "SOCI":
        frame = pro.cn_soci(limit=1)
        if frame.empty:
            return None
        row = frame.iloc[0]
        raw = row_payload(row)
        return {
            "value": first_present(raw, ("total", "socialfin", "value")),
            "date": first_present(raw, ("month", "date")),
            "raw": raw,
        }
    if indicator == "PMI":
        frame = pro.cn_pmi(limit=1)
        if frame.empty:
            return None
        row = frame.iloc[0]
        raw = row_payload(row)
        return {
            "value": first_present(raw, ("pmi", "pmi010000", "value")),
            "date": first_present(raw, ("month", "date")),
            "raw": raw,
        }
    raise ValueError(f"Unsupported China macro indicator: {indicator}")


def fetch_china_monthly(indicators: list[str]) -> dict[str, Any]:
    requested = [indicator.upper() for indicator in indicators if indicator]
    if not requested:
        return {"sources": {"tushare": "SKIPPED"}, "data": {}}
    if not TUSHARE_TOKEN:
        return {"sources": {"tushare": "MISSING_CONFIG"}, "data": {}}

    data: dict[str, Any] = {}
    try:
        pro = tushare_pro()
        if pro is None:
            return {"sources": {"tushare": "ERROR"}, "data": {}}
        for indicator in requested:
            try:
                value = latest_china_indicator(pro, indicator)
                if value:
                    data[indicator] = {**value, "source": "tushare"}
            except Exception as exc:
                data[indicator] = {"_error": str(exc), "source": "tushare"}
    except Exception as exc:
        return {"sources": {"tushare": "ERROR"}, "data": {}, "error": str(exc)}

    status = "OK" if any(not value.get("_error") for value in data.values()) else "ERROR"
    return {"sources": {"tushare": status}, "data": data}


def fetch_dataset(
    dataset: str,
    use_backup: bool = False,
    china_indicators: list[str] | None = None,
) -> dict[str, Any]:
    if dataset == "market":
        return fetch_market_signals(use_backup=use_backup)
    if dataset == "china-monthly":
        return fetch_china_monthly(china_indicators or [])
    if dataset == "all":
        market = fetch_market_signals(use_backup=use_backup)
        china = fetch_china_monthly(china_indicators or [])
        return {
            "timestamp": now_iso(),
            "sources": {**market.get("sources", {}), **china.get("sources", {})},
            "data": {**market.get("data", {}), "CHINA_MACRO": china.get("data", {})},
        }
    raise ValueError(f"Unsupported dataset: {dataset}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch macro evidence as JSON.")
    parser.add_argument(
        "dataset",
        nargs="?",
        default="market",
        choices=["market", "china-monthly", "all"],
    )
    parser.add_argument(
        "--china-indicator",
        action="append",
        choices=["CPI", "PPI", "SOCI", "PMI"],
        help="China monthly indicator to fetch. Repeatable.",
    )
    parser.add_argument("--use-backup", action="store_true", help="Force Stooq fallback.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = fetch_dataset(
        args.dataset,
        use_backup=args.use_backup,
        china_indicators=args.china_indicator,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    raise SystemExit(main())
