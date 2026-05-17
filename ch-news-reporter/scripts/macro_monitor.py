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


def fetch_market_signals(use_backup: bool = False) -> dict[str, Any]:
    results = {"timestamp": now_iso(), "sources": {}, "data": {}}

    jin10 = fetch_jin10_quotes()
    results["sources"].update(jin10["sources"])
    results["data"].update(jin10["data"])

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
