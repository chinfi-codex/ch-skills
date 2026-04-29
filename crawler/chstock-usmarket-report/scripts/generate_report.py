#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch Yahoo Finance evidence for a US-market watchlist.

The script intentionally returns structured data only. It does not write the
daily brief, name conclusions, or produce investment advice; the skill user
does that in SKILL.md after reading the evidence package.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests
import yaml


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "assets" / "stock_pool.yaml"
LOOKBACK_DAYS = 15
EVIDENCE_TYPE = "us_market_watchlist_evidence"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}

DEFAULT_CONFIG: Dict[str, Any] = {
    "indices": [
        {"ticker": "QQQ", "name": "纳指100"},
        {"ticker": "SPY", "name": "标普500"},
        {"ticker": "DIA", "name": "道琼斯"},
        {"ticker": "IWM", "name": "罗素2000"},
    ],
    "groups": [],
    "thresholds": {
        "big_drop": -10.0,
        "big_rise": 10.0,
        "warning": -5.0,
        "highlight": 5.0,
    },
    "output": {
        "show_5day_trend": True,
        "show_volume": False,
    },
}


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load watchlist config and merge missing keys from defaults."""
    path = Path(config_path).expanduser().resolve() if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))

    with path.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}

    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def parse_report_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def fetch_chart_history(ticker: str) -> list[Dict[str, Any]]:
    """Fetch one month of daily chart history from Yahoo Finance."""
    response = requests.get(
        YAHOO_CHART_URL.format(ticker=ticker),
        params={
            "range": "1mo",
            "interval": "1d",
            "includePrePost": "false",
            "events": "div,splits",
        },
        headers=REQUEST_HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()

    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise RuntimeError(f"{ticker} 未返回可用行情。")

    chart = result[0]
    timestamps = chart.get("timestamp") or []
    quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    rows: list[Dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        close_value = closes[index] if index < len(closes) else None
        if close_value in (None, 0):
            continue
        volume_value = volumes[index] if index < len(volumes) else None
        rows.append(
            {
                "date": datetime.utcfromtimestamp(timestamp).date(),
                "close": float(close_value),
                "volume": int(volume_value) if volume_value is not None else None,
            }
        )

    if len(rows) < 2:
        raise RuntimeError(f"{ticker} 历史行情不足。")
    return rows[-LOOKBACK_DAYS:]


def get_latest_completed_trading_day(
    history_by_ticker: Dict[str, Optional[list[Dict[str, Any]]]],
    reference_ticker: str = "SPY",
) -> date:
    """Resolve the latest completed trading day from available history."""
    reference_history = history_by_ticker.get(reference_ticker)
    if not reference_history:
        available_histories = [history for history in history_by_ticker.values() if history]
        if not available_histories:
            raise RuntimeError("无法从行情中确定最近交易日。")
        reference_history = available_histories[0]
    return reference_history[-1]["date"]


def resolve_target_row(
    history: list[Dict[str, Any]], requested_date: Optional[date]
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Locate target and previous trading rows in one ticker history."""
    if not history or len(history) < 2:
        raise RuntimeError("历史行情不足，至少需要两个交易日的数据。")

    available_dates = [item["date"] for item in history]
    target_date = requested_date or available_dates[-1]

    if target_date not in available_dates:
        earlier_dates = [item for item in available_dates if item <= target_date]
        if not earlier_dates:
            raise RuntimeError(f"未找到 {target_date} 之前的有效交易日。")
        target_date = earlier_dates[-1]

    target_position = available_dates.index(target_date)
    if target_position == 0:
        raise RuntimeError(f"{target_date} 没有可用的前收盘数据。")

    return history[target_position], history[target_position - 1]


def build_stock_snapshot(
    ticker: str,
    history: Optional[list[Dict[str, Any]]],
    report_date: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Build a deterministic price snapshot for one ticker."""
    if not history:
        return None
    target_row, prev_row = resolve_target_row(history, report_date)

    close_price = float(target_row["close"])
    prev_close = float(prev_row["close"])
    if prev_close == 0:
        return None

    change_pct = (close_price - prev_close) / prev_close * 100
    five_day_trend = None
    target_index = history.index(target_row)
    if target_index >= 4:
        base_close = float(history[target_index - 4]["close"])
        if base_close != 0:
            five_day_trend = (close_price - base_close) / base_close * 100

    return {
        "ticker": ticker,
        "trade_date": target_row["date"].isoformat(),
        "close": round(close_price, 4),
        "prev_close": round(prev_close, 4),
        "change_pct": round(change_pct, 4),
        "five_day_trend_pct": round(five_day_trend, 4) if five_day_trend is not None else None,
        "volume": target_row.get("volume"),
    }


def collect_unique_tickers(config: Dict[str, Any]) -> list[str]:
    tickers: list[str] = []
    for index_item in config.get("indices", []):
        tickers.append(index_item["ticker"])
    for group in config.get("groups", []):
        tickers.extend(group.get("stocks", []))
    return list(dict.fromkeys(tickers))


def classify_move(change_pct: float, thresholds: Dict[str, float]) -> str:
    if change_pct <= thresholds.get("big_drop", -10.0):
        return "big_drop"
    if change_pct >= thresholds.get("big_rise", 10.0):
        return "big_rise"
    if change_pct <= thresholds.get("warning", -5.0):
        return "warning_drop"
    if change_pct >= thresholds.get("highlight", 5.0):
        return "highlight_rise"
    return "normal"


def build_group_evidence(
    config: Dict[str, Any],
    snapshots: Dict[str, Optional[Dict[str, Any]]],
) -> list[Dict[str, Any]]:
    groups: list[Dict[str, Any]] = []
    for group in config.get("groups", []):
        rows = []
        changes = []
        for ticker in group.get("stocks", []):
            snapshot = snapshots.get(ticker)
            rows.append({"ticker": ticker, "snapshot": snapshot})
            if snapshot:
                changes.append(snapshot["change_pct"])

        groups.append(
            {
                "name": group.get("name", ""),
                "emoji": group.get("emoji", ""),
                "tickers": group.get("stocks", []),
                "stocks": rows,
                "summary": {
                    "valid_count": len(changes),
                    "up_count": sum(1 for item in changes if item > 0),
                    "down_count": sum(1 for item in changes if item < 0),
                    "avg_change_pct": round(sum(changes) / len(changes), 4) if changes else None,
                },
            }
        )
    return groups


def build_abnormal_evidence(
    snapshots: Dict[str, Optional[Dict[str, Any]]],
    thresholds: Dict[str, float],
) -> Dict[str, list[Dict[str, Any]]]:
    buckets: Dict[str, list[Dict[str, Any]]] = {
        "big_drops": [],
        "big_rises": [],
        "warning_drops": [],
        "highlight_rises": [],
    }
    for ticker, snapshot in snapshots.items():
        if not snapshot:
            continue
        label = classify_move(snapshot["change_pct"], thresholds)
        item = {"ticker": ticker, **snapshot}
        if label == "big_drop":
            buckets["big_drops"].append(item)
        elif label == "big_rise":
            buckets["big_rises"].append(item)
        elif label == "warning_drop":
            buckets["warning_drops"].append(item)
        elif label == "highlight_rise":
            buckets["highlight_rises"].append(item)

    buckets["big_drops"].sort(key=lambda item: item["change_pct"])
    buckets["big_rises"].sort(key=lambda item: item["change_pct"], reverse=True)
    buckets["warning_drops"].sort(key=lambda item: item["change_pct"])
    buckets["highlight_rises"].sort(key=lambda item: item["change_pct"], reverse=True)
    return buckets


def build_market_evidence(config: Dict[str, Any], report_date: Optional[date] = None) -> Dict[str, Any]:
    """Fetch all configured tickers and return a JSON-serializable evidence package."""
    unique_tickers = collect_unique_tickers(config)
    history_by_ticker: Dict[str, Optional[list[Dict[str, Any]]]] = {}
    errors: Dict[str, str] = {}

    for ticker in unique_tickers:
        try:
            history_by_ticker[ticker] = fetch_chart_history(ticker)
        except Exception as exc:
            history_by_ticker[ticker] = None
            errors[ticker] = str(exc)

    if not any(history_by_ticker.values()):
        raise RuntimeError("所有股票的行情拉取都失败了，请稍后重试。")

    resolved_date = report_date or get_latest_completed_trading_day(history_by_ticker)

    index_snapshots = []
    for index_item in config.get("indices", []):
        snapshot = build_stock_snapshot(index_item["ticker"], history_by_ticker.get(index_item["ticker"]), resolved_date)
        if snapshot:
            snapshot["name"] = index_item.get("name", index_item["ticker"])
        index_snapshots.append({"ticker": index_item["ticker"], "snapshot": snapshot})

    stock_snapshots: Dict[str, Optional[Dict[str, Any]]] = {}
    for group in config.get("groups", []):
        for ticker in group.get("stocks", []):
            if ticker not in stock_snapshots:
                try:
                    stock_snapshots[ticker] = build_stock_snapshot(ticker, history_by_ticker.get(ticker), resolved_date)
                except Exception as exc:
                    stock_snapshots[ticker] = None
                    errors[ticker] = str(exc)

    return {
        "type": EVIDENCE_TYPE,
        "date": resolved_date.isoformat(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config_path": str(DEFAULT_CONFIG_PATH),
        "thresholds": config.get("thresholds", {}),
        "indices": index_snapshots,
        "groups": build_group_evidence(config, stock_snapshots),
        "abnormal_moves": build_abnormal_evidence(stock_snapshots, config.get("thresholds", {})),
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch structured evidence for a US-market watchlist")
    parser.add_argument("--config", "-c", help="配置文件路径", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--date", "-d", help="交易日，格式 YYYY-MM-DD；默认取最近一个已结束交易日")
    parser.add_argument("--output", "-o", help="输出 JSON 文件路径")
    parser.add_argument("--json", "-j", action="store_true", help="兼容旧参数；当前默认总是输出 JSON")
    args = parser.parse_args()

    config = load_config(args.config)
    evidence = build_market_evidence(config, parse_report_date(args.date))
    content = json.dumps(evidence, ensure_ascii=False, indent=2, default=str)

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        print(json.dumps({"status": "success", "output": str(output_path), "date": evidence["date"]}, ensure_ascii=False))
        return

    print(content)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    main()
