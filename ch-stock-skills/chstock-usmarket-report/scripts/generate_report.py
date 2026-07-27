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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

SCRIPT_ROOT = Path(__file__).resolve().parent


def _shared_root() -> str:
    """Locate the shared bundle root: synced copy first, canonical repo second.

    Tested on the package itself rather than on `_shared/` as a whole, because a
    synced copy predating this bundle still has an `_shared/` directory.
    """
    for candidate in (SCRIPT_ROOT / "_shared", SCRIPT_ROOT.parents[2] / "shared"):
        if (candidate / "yahoo_http").is_dir():
            return str(candidate)
    raise ModuleNotFoundError(
        "yahoo_http shared bundle not found — run `python scripts/skill_sync.py` "
        "in the ch-skills repo to sync it into scripts/_shared/."
    )


sys.path.insert(0, str(SCRIPT_ROOT))
sys.path.insert(0, _shared_root())

from yahoo_http import yahoo_get  # noqa: E402 — shared bundle, needs the path insert above


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "assets" / "stock_pool.yaml"
UNIVERSE_PATH = ROOT_DIR / "assets" / "nasdaq_tech_universe.yaml"
HISTORY_DAYS = 260  # ≈ 52 周的交易日；够算 52 周位置 + 20 日均量 + 20 日趋势
QQQ_KLINE_DAYS = 120  # 嵌入证据包的 QQQ K 线展示窗口（HTML 头部蜡烛图）
GROUP_INDEX_DAYS = QQQ_KLINE_DAYS  # 分组等权 ETF 指数展示窗口（与 QQQ K 线同窗，便于热力墙下方多线对比）
EVIDENCE_TYPE = "us_market_watchlist_evidence"
YAHOO_CHART_PATH = "/v8/finance/chart/{ticker}"  # host chosen per attempt by yahoo_get

DEFAULT_CONFIG: Dict[str, Any] = {
    "indices": [
        {"ticker": "QQQ", "name": "纳指100"},
    ],
    "groups": [],
    "thresholds": {
        "drop": -7.0,
        "rise": 7.0,
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
    """Fetch one year of daily chart history from Yahoo Finance.

    A year of history (vs the old one month) is what lets snapshots carry 52-week
    position, drawdown-from-high, 20-day trend and a 20-day volume ratio — the
    fields the sector money-effect rubric leans on. The call count is unchanged
    (still one request per ticker); only the `range` widens.
    """
    response = yahoo_get(
        YAHOO_CHART_PATH.format(ticker=ticker),
        params={
            "range": "1y",
            "interval": "1d",
            "includePrePost": "false",
            "events": "div,splits",
        },
    )
    payload = response.json()

    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise RuntimeError(f"{ticker} 未返回可用行情。")

    chart = result[0]
    timestamps = chart.get("timestamp") or []
    quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    def _at(seq: list, i: int) -> Optional[float]:
        value = seq[i] if i < len(seq) else None
        return float(value) if value is not None else None

    rows: list[Dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        close_value = closes[index] if index < len(closes) else None
        if close_value in (None, 0):
            continue
        volume_value = volumes[index] if index < len(volumes) else None
        rows.append(
            {
                "date": datetime.utcfromtimestamp(timestamp).date(),
                "open": _at(opens, index),
                "high": _at(highs, index),
                "low": _at(lows, index),
                "close": float(close_value),
                "volume": int(volume_value) if volume_value is not None else None,
            }
        )

    if len(rows) < 2:
        raise RuntimeError(f"{ticker} 历史行情不足。")
    return rows[-HISTORY_DAYS:]


def get_latest_completed_trading_day(
    history_by_ticker: Dict[str, Optional[list[Dict[str, Any]]]],
    reference_ticker: str = "QQQ",
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


def _return_pct(history: list[Dict[str, Any]], target_index: int, bars_back: int) -> Optional[float]:
    """Return % from `bars_back` sessions ago to the target session (None if short)."""
    if target_index < bars_back:
        return None
    base = float(history[target_index - bars_back]["close"])
    if base == 0:
        return None
    cur = float(history[target_index]["close"])
    return (cur - base) / base * 100


def _position_in_range(
    history: list[Dict[str, Any]], target_index: int, window: int = 252
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Close's position within the trailing 52w close range + drawdown from that high.

    Returns (position_0to1, drawdown_from_high_pct, high, low). Close-based, to
    stay consistent with the rest of the snapshot (no intraday high/low).
    """
    start = max(0, target_index - window + 1)
    closes = [float(r["close"]) for r in history[start : target_index + 1] if r.get("close")]
    if len(closes) < 2:
        return None, None, None, None
    high, low, cur = max(closes), min(closes), closes[-1]
    position = (cur - low) / (high - low) if high > low else None
    drawdown = (cur - high) / high * 100 if high else None
    return position, drawdown, high, low


def _volume_ratio_20d(
    history: list[Dict[str, Any]], target_index: int, window: int = 20
) -> Optional[float]:
    """Target session's volume vs the average of the prior `window` sessions (量比)."""
    cur_vol = history[target_index].get("volume")
    if not cur_vol:
        return None
    start = max(0, target_index - window)
    prior = [r.get("volume") for r in history[start:target_index] if r.get("volume")]
    if len(prior) < 5:
        return None
    avg = sum(prior) / len(prior)
    return cur_vol / avg if avg else None


def build_stock_snapshot(
    ticker: str,
    history: Optional[list[Dict[str, Any]]],
    report_date: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Build a deterministic price snapshot for one ticker.

    Beyond same-day change and the 5-day trend, the snapshot carries the
    medium-term context the sector rubric reads: 20-day trend, 52-week position,
    drawdown from the 52w high, and a 20-day volume ratio. Relative-to-QQQ excess
    is injected later (see inject_relative_fields) once the benchmark is known.
    """
    if not history:
        return None
    target_row, prev_row = resolve_target_row(history, report_date)

    close_price = float(target_row["close"])
    prev_close = float(prev_row["close"])
    if prev_close == 0:
        return None

    change_pct = (close_price - prev_close) / prev_close * 100
    target_index = history.index(target_row)
    five_day_trend = _return_pct(history, target_index, 4)
    twenty_day_trend = _return_pct(history, target_index, 19)
    position_52w, drawdown_high, high_52w, low_52w = _position_in_range(history, target_index)
    vol_ratio = _volume_ratio_20d(history, target_index)

    return {
        "ticker": ticker,
        "trade_date": target_row["date"].isoformat(),
        "close": round(close_price, 4),
        "prev_close": round(prev_close, 4),
        "change_pct": round(change_pct, 4),
        "five_day_trend_pct": round(five_day_trend, 4) if five_day_trend is not None else None,
        "trend_20d_pct": round(twenty_day_trend, 4) if twenty_day_trend is not None else None,
        "position_52w": round(position_52w, 4) if position_52w is not None else None,
        "drawdown_from_high_pct": round(drawdown_high, 2) if drawdown_high is not None else None,
        "high_52w": round(high_52w, 4) if high_52w is not None else None,
        "low_52w": round(low_52w, 4) if low_52w is not None else None,
        "vol_vs_20d": round(vol_ratio, 2) if vol_ratio is not None else None,
        "volume": target_row.get("volume"),
    }


def inject_relative_fields(
    snapshot: Optional[Dict[str, Any]], benchmark: Optional[Dict[str, Any]]
) -> None:
    """Add same-day and 5-day excess vs a benchmark (QQQ) to the snapshot in place.

    A curated watchlist exists to beat the index, so the excess over QQQ is more
    informative than the raw move — most names just echo the tape. Mutating in
    place means the fields also flow into the group rows and abnormal buckets,
    which reference the same snapshot dicts.
    """
    if not snapshot or not benchmark:
        return
    bench_1d = benchmark.get("change_pct")
    if snapshot.get("change_pct") is not None and bench_1d is not None:
        snapshot["vs_qqq_1d"] = round(snapshot["change_pct"] - bench_1d, 4)
    bench_5d = benchmark.get("five_day_trend_pct")
    if snapshot.get("five_day_trend_pct") is not None and bench_5d is not None:
        snapshot["vs_qqq_5d"] = round(snapshot["five_day_trend_pct"] - bench_5d, 4)


def build_kline_records(history: list[Dict[str, Any]], days: int = QQQ_KLINE_DAYS) -> list[Dict[str, Any]]:
    """Last `days` OHLCV rows for a candlestick chart (the HTML header QQQ K-line).

    `amount` is close × shares (the dollar-volume proxy the kit's volume panel
    draws); `pct_chg` is day-over-day so the tooltip can show it.
    """
    records: list[Dict[str, Any]] = []
    prev_close: Optional[float] = None
    for row in history:
        close = row.get("close")
        vol = row.get("volume")
        pct = ((close - prev_close) / prev_close * 100) if (prev_close and close is not None) else None
        records.append(
            {
                "trade_date": row["date"].isoformat(),
                "open": round(row["open"], 4) if row.get("open") is not None else None,
                "high": round(row["high"], 4) if row.get("high") is not None else None,
                "low": round(row["low"], 4) if row.get("low") is not None else None,
                "close": round(close, 4) if close is not None else None,
                "pct_chg": round(pct, 4) if pct is not None else None,
                "amount": round(close * vol, 2) if (close is not None and vol) else None,
                "vol": vol,
            }
        )
        if close is not None:
            prev_close = close
    return records[-days:] if days else records


def build_group_indices(
    config: Dict[str, Any],
    history_by_ticker: Dict[str, Optional[list[Dict[str, Any]]]],
    window_days: int = GROUP_INDEX_DAYS,
    benchmark_ticker: str = "QQQ",
    base: float = 100.0,
    report_date: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Build one equal-weight, daily-rebalanced synthetic index per watchlist group.

    Each group's constituents are folded into a single "ETF-like" index so the HTML
    layer can overlay groups in one normalized comparison chart (热力墙下方那张图).
    Construction is the textbook equal-weight method: a session's index return is the
    mean of constituents' daily returns — only names with both that day's and the
    prior day's close contribute, so ragged histories (newer listings) are handled —
    compounded forward from `base`. All series share one trading calendar so the lines
    align. The window is anchored at `report_date` (when given) so a historical
    look-back doesn't leak sessions after the report day. This is deterministic data
    prep (no judgement); the model never touches it.
    """
    # 真实基准(QQQ)历史：既定日历轴，也是后面那条虚线基准线的唯一来源。
    benchmark_history = history_by_ticker.get(benchmark_ticker)
    # 日历轴优先用基准；基准缺失才退回最长的成员历史（仅借它定交易日，不冒充基准线）。
    calendar_history = benchmark_history or max(
        (h for h in history_by_ticker.values() if h), key=len, default=None
    )
    if not calendar_history or len(calendar_history) < 2:
        return None
    calendar_dates = [row["date"] for row in calendar_history]
    if report_date is not None:
        calendar_dates = [d for d in calendar_dates if d <= report_date]
    dates = calendar_dates[-window_days:]
    if len(dates) < 2:
        return None

    def _aligned_closes(history: Optional[list[Dict[str, Any]]]) -> list[Optional[float]]:
        by_date = {row["date"]: row.get("close") for row in (history or [])}
        return [by_date.get(d) for d in dates]

    def _records(levels: list[float]) -> list[Dict[str, Any]]:
        return [{"trade_date": dates[i].isoformat(), "index": round(level, 4)} for i, level in enumerate(levels)]

    def _equal_weight_levels(members_closes: list[list[Optional[float]]]) -> list[float]:
        levels = [base]
        for k in range(1, len(dates)):
            day_returns = []
            for closes in members_closes:
                prev_c, cur_c = closes[k - 1], closes[k]
                if prev_c and cur_c and prev_c > 0:
                    day_returns.append(cur_c / prev_c - 1.0)
            step = sum(day_returns) / len(day_returns) if day_returns else 0.0
            levels.append(levels[-1] * (1.0 + step))
        return levels

    series: list[Dict[str, Any]] = []
    for group in config.get("groups", []):
        tickers = list(dict.fromkeys(group.get("stocks", [])))
        members_closes = []
        for ticker in tickers:
            history = history_by_ticker.get(ticker)
            if not history:
                continue
            aligned = _aligned_closes(history)
            if any(c for c in aligned):
                members_closes.append(aligned)
        if not members_closes:
            continue  # 空组或成员行情全失败 → 整组省略，不画空线
        levels = _equal_weight_levels(members_closes)
        series.append(
            {
                "name": group.get("name", ""),
                "emoji": group.get("emoji", ""),
                "member_count": len(tickers),
                "valid_member_count": len(members_closes),
                "records": _records(levels),
                "total_return_pct": round(levels[-1] - base, 4),
            }
        )

    if not series:
        return None

    # benchmark (QQQ) 自身收盘价归一为同窗基准线（虚线），缺口前向填充保持连续。
    # 只有拿到真实 QQQ 历史时才画——基准缺失就不画基准线，绝不拿某只成员的归一线冒充 QQQ。
    benchmark_block: Optional[Dict[str, Any]] = None
    if benchmark_history:
        bench_aligned = _aligned_closes(benchmark_history)
        base_close = next((c for c in bench_aligned if c), None)
        if base_close:
            last = base
            bench_levels = []
            for close in bench_aligned:
                if close:
                    last = base * close / base_close
                bench_levels.append(last)
            benchmark_block = {
                "ticker": benchmark_ticker,
                "records": _records(bench_levels),
                "total_return_pct": round(bench_levels[-1] - base, 4),
            }

    return {
        "window_days": window_days,
        "base": base,
        "start_date": dates[0].isoformat(),
        "end_date": dates[-1].isoformat(),
        "benchmark": benchmark_block,
        "series": series,
    }


def collect_unique_tickers(config: Dict[str, Any]) -> list[str]:
    tickers: list[str] = []
    for index_item in config.get("indices", []):
        tickers.append(index_item["ticker"])
    for group in config.get("groups", []):
        tickers.extend(group.get("stocks", []))
    return list(dict.fromkeys(tickers))


def classify_move(change_pct: float, thresholds: Dict[str, float]) -> str:
    if change_pct <= thresholds.get("drop", -7.0):
        return "drop"
    if change_pct >= thresholds.get("rise", 7.0):
        return "rise"
    return "normal"


def build_group_evidence(
    config: Dict[str, Any],
    snapshots: Dict[str, Optional[Dict[str, Any]]],
) -> list[Dict[str, Any]]:
    groups: list[Dict[str, Any]] = []
    for group in config.get("groups", []):
        rows = []
        changes = []
        # 组内去重：飞书同步偶尔把同一 ticker 写进同组两次（如 CRDO），
        # 同票同组重复没有任何信息量，保序去重避免正文出现重复行。
        group_tickers = list(dict.fromkeys(group.get("stocks", [])))
        for ticker in group_tickers:
            snapshot = snapshots.get(ticker)
            rows.append({"ticker": ticker, "snapshot": snapshot})
            if snapshot:
                changes.append(snapshot["change_pct"])

        groups.append(
            {
                "name": group.get("name", ""),
                "emoji": group.get("emoji", ""),
                "tickers": group_tickers,
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


def build_ticker_group_map(config: Dict[str, Any]) -> Dict[str, list[str]]:
    mapping: Dict[str, list[str]] = {}
    for group in config.get("groups", []):
        name = group.get("name", "")
        for ticker in group.get("stocks", []):
            names = mapping.setdefault(ticker, [])
            # 飞书同步偶尔把同一 ticker 在同组写两遍（如 CRDO）；同组去重避免
            # 异动 / 归属渲染成 "AI硬件 / AI硬件"，但保留跨组成员（不同组都留）。
            if name not in names:
                names.append(name)
    return mapping


def build_abnormal_evidence(
    snapshots: Dict[str, Optional[Dict[str, Any]]],
    thresholds: Dict[str, float],
    ticker_group_map: Optional[Dict[str, list[str]]] = None,
) -> Dict[str, list[Dict[str, Any]]]:
    ticker_group_map = ticker_group_map or {}
    buckets: Dict[str, list[Dict[str, Any]]] = {
        "drops": [],
        "rises": [],
    }
    for ticker, snapshot in snapshots.items():
        if not snapshot:
            continue
        label = classify_move(snapshot["change_pct"], thresholds)
        item = {"ticker": ticker, "groups": ticker_group_map.get(ticker, []), **snapshot}
        if label == "drop":
            buckets["drops"].append(item)
        elif label == "rise":
            buckets["rises"].append(item)

    buckets["drops"].sort(key=lambda item: item["change_pct"])
    buckets["rises"].sort(key=lambda item: item["change_pct"], reverse=True)
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
        entry = {"ticker": index_item["ticker"], "snapshot": snapshot}
        index_history = history_by_ticker.get(index_item["ticker"])
        if index_history:
            # 锚定报告日：历史回看时不把报告日之后的“未来”K线画进去，
            # 与下方分组 ETF 指数图同口径（默认最近交易日时此截断为空操作）。
            kline_history = [row for row in index_history if row["date"] <= resolved_date] or index_history
            entry["kline_records"] = build_kline_records(kline_history, QQQ_KLINE_DAYS)
            entry["kline_days_requested"] = QQQ_KLINE_DAYS
        index_snapshots.append(entry)

    stock_snapshots: Dict[str, Optional[Dict[str, Any]]] = {}
    for group in config.get("groups", []):
        for ticker in group.get("stocks", []):
            if ticker not in stock_snapshots:
                try:
                    stock_snapshots[ticker] = build_stock_snapshot(ticker, history_by_ticker.get(ticker), resolved_date)
                except Exception as exc:
                    stock_snapshots[ticker] = None
                    errors[ticker] = str(exc)

    # Excess vs QQQ, injected in place so it flows into groups + abnormal buckets.
    qqq_snapshot = next(
        (item["snapshot"] for item in index_snapshots if item["ticker"] == "QQQ"), None
    )
    for snapshot in stock_snapshots.values():
        inject_relative_fields(snapshot, qqq_snapshot)

    # Per-group equal-weight ETF-style index series (drives the HTML 热力墙下方 multi-line
    # comparison chart). Reuses the histories already fetched above — no extra requests.
    benchmark_ticker = "QQQ"
    configured_index_tickers = [item.get("ticker") for item in config.get("indices", []) if item.get("ticker")]
    if benchmark_ticker not in configured_index_tickers and configured_index_tickers:
        benchmark_ticker = configured_index_tickers[0]
    group_indices = build_group_indices(
        config, history_by_ticker, benchmark_ticker=benchmark_ticker, report_date=resolved_date
    )

    # Authoritative data date = the resolved trading session (QQQ snapshot), not the
    # requested --date: a weekend/holiday/future date backfills to an earlier session,
    # so the evidence must be labeled with the day the data actually came from (else the
    # report header and the scan `date_aligned` check both compare against a stale date).
    data_date = (qqq_snapshot or {}).get("trade_date") or resolved_date.isoformat()

    return {
        "type": EVIDENCE_TYPE,
        "date": data_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config_path": str(DEFAULT_CONFIG_PATH),
        "thresholds": config.get("thresholds", {}),
        "indices": index_snapshots,
        "groups": build_group_evidence(config, stock_snapshots),
        "group_indices": group_indices,
        "abnormal_moves": build_abnormal_evidence(
            stock_snapshots,
            config.get("thresholds", {}),
            build_ticker_group_map(config),
        ),
        "errors": errors,
    }


def build_enrich_evidence(
    tickers: list[str],
    report_date: Optional[date] = None,
    benchmark_ticker: str = "QQQ",
) -> Dict[str, Any]:
    """Fetch a one-off ticker list (+ QQQ) and return enriched snapshots.

    Used after the model has grouped the Nasdaq scan pool into themes: the scan
    hits carry only a same-day snapshot, so the leading-theme members the model
    wants to confirm get their 52w position / 20d trend / vol ratio / vs-QQQ
    excess backfilled here. The script computes fields; it does not pick themes.
    """
    fetch_list = list(dict.fromkeys([benchmark_ticker] + [t for t in tickers]))
    history_by_ticker: Dict[str, Optional[list[Dict[str, Any]]]] = {}
    errors: Dict[str, str] = {}
    for ticker in fetch_list:
        try:
            history_by_ticker[ticker] = fetch_chart_history(ticker)
        except Exception as exc:
            history_by_ticker[ticker] = None
            errors[ticker] = str(exc)

    if not any(history_by_ticker.values()):
        raise RuntimeError("enrich：所有 ticker 的行情拉取都失败了。")

    resolved_date = report_date or get_latest_completed_trading_day(history_by_ticker, benchmark_ticker)
    benchmark_snapshot = build_stock_snapshot(
        benchmark_ticker, history_by_ticker.get(benchmark_ticker), resolved_date
    )

    rows = []
    for ticker in tickers:
        snapshot = None
        try:
            snapshot = build_stock_snapshot(ticker, history_by_ticker.get(ticker), resolved_date)
        except Exception as exc:
            errors[ticker] = str(exc)
        if snapshot:
            inject_relative_fields(snapshot, benchmark_snapshot)
        rows.append({"ticker": ticker, "snapshot": snapshot})

    return {
        "type": "us_enrich_evidence",
        "date": resolved_date.isoformat(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "benchmark": {"ticker": benchmark_ticker, "snapshot": benchmark_snapshot},
        "tickers": rows,
        "errors": errors,
    }


def load_universe(path: str) -> Dict[str, list[str]]:
    """Load the curated Nasdaq-tech universe (coarse buckets, not themes)."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise RuntimeError(f"universe 配置不存在：{resolved}")
    data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    cleaned: Dict[str, list[str]] = {}
    for name, tickers in (data.get("buckets", {}) or {}).items():
        items: list[str] = []
        for ticker in tickers or []:
            # YAML parses bare ON/NO/YES/OFF as booleans — catch it loudly so a
            # mangled ticker fails the config rather than 404-ing as "True".
            if isinstance(ticker, bool):
                raise RuntimeError(
                    f"universe bucket {name!r} 有被 YAML 当成布尔的 ticker（如裸 ON/NO/YES）；请加引号，例如 \"ON\"。"
                )
            items.append(str(ticker).strip().upper())
        cleaned[name] = items
    return cleaned


def fetch_histories_concurrent(
    tickers: list[str], workers: int = 8
) -> tuple[Dict[str, Optional[list[Dict[str, Any]]]], Dict[str, str]]:
    """Fetch many tickers' history in parallel (the universe scan is the big pass)."""
    out: Dict[str, Optional[list[Dict[str, Any]]]] = {}
    errors: Dict[str, str] = {}

    def _one(ticker: str):
        try:
            return ticker, fetch_chart_history(ticker), None
        except Exception as exc:  # noqa: BLE001 — per-ticker failure is non-fatal
            return ticker, None, str(exc)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for ticker, history, err in pool.map(_one, tickers):
            out[ticker] = history
            if err:
                errors[ticker] = err
    return out, errors


def _median(values: list[Optional[float]]) -> Optional[float]:
    nums = sorted(v for v in values if v is not None)
    if not nums:
        return None
    mid = len(nums) // 2
    return nums[mid] if len(nums) % 2 else round((nums[mid - 1] + nums[mid]) / 2, 4)


def build_universe_scan(
    buckets: Dict[str, list[str]],
    report_date: Optional[date] = None,
    workers: int = 8,
    min_mover_change: float = 3.0,
    benchmark_ticker: str = "QQQ",
) -> Dict[str, Any]:
    """Fetch the whole curated universe and aggregate breadth + money by bucket.

    This is the "stable denominator" the screener-only pool can't give: it sees
    every tracked name, so quietly-bid names that never cracked the day_gainers
    top-250 still surface here. Buckets are for breadth only — theme naming stays
    the model's job. Heavy (one fetch per name), so it is opt-in (`--scan-universe`).
    """
    all_tickers = list(dict.fromkeys([benchmark_ticker] + [t for names in buckets.values() for t in names]))
    history_by_ticker, errors = fetch_histories_concurrent(all_tickers, workers)
    if not any(history_by_ticker.values()):
        raise RuntimeError("universe scan：所有 ticker 的行情拉取都失败了。")

    resolved_date = report_date or get_latest_completed_trading_day(history_by_ticker, benchmark_ticker)
    benchmark = build_stock_snapshot(benchmark_ticker, history_by_ticker.get(benchmark_ticker), resolved_date)

    snapshots: Dict[str, Optional[Dict[str, Any]]] = {}
    for ticker in all_tickers:
        if ticker == benchmark_ticker:
            continue
        snapshot = None
        try:
            snapshot = build_stock_snapshot(ticker, history_by_ticker.get(ticker), resolved_date)
        except Exception as exc:  # noqa: BLE001
            errors[ticker] = str(exc)
        if snapshot:
            inject_relative_fields(snapshot, benchmark)
            vol = snapshot.get("volume")
            snapshot["dollar_volume_million"] = (
                round(snapshot["close"] * vol / 1e6, 1) if vol else None
            )
        snapshots[ticker] = snapshot

    bucket_rows = []
    movers = []
    for name, tickers in buckets.items():
        members = [snapshots.get(t) for t in dict.fromkeys(tickers) if snapshots.get(t)]
        changes = [m["change_pct"] for m in members]
        dollar_volume = round(sum(m.get("dollar_volume_million") or 0 for m in members), 1)
        leaders = sorted(members, key=lambda m: m.get("dollar_volume_million") or 0, reverse=True)[:3]
        bucket_rows.append(
            {
                "bucket": name,
                "valid": len(members),
                "up": sum(1 for c in changes if c > 0),
                "down": sum(1 for c in changes if c < 0),
                "dollar_volume_million": dollar_volume,
                "median_change_pct": _median(changes),
                "median_vs_qqq_5d": _median([m.get("vs_qqq_5d") for m in members]),
                "leaders": [
                    {"ticker": m["ticker"], "change_pct": m["change_pct"], "dollar_volume_million": m.get("dollar_volume_million")}
                    for m in leaders
                ],
            }
        )
        for m in members:
            if m["change_pct"] >= min_mover_change:
                movers.append(
                    {
                        "ticker": m["ticker"],
                        "bucket": name,
                        "change_pct": m["change_pct"],
                        "vs_qqq_1d": m.get("vs_qqq_1d"),
                        "vs_qqq_5d": m.get("vs_qqq_5d"),
                        "position_52w": m.get("position_52w"),
                        "vol_vs_20d": m.get("vol_vs_20d"),
                        "dollar_volume_million": m.get("dollar_volume_million"),
                    }
                )
    movers.sort(key=lambda x: x.get("dollar_volume_million") or 0, reverse=True)

    return {
        "type": "us_nasdaq_universe_scan",
        "date": (benchmark or {}).get("trade_date") or resolved_date.isoformat(),
        "min_mover_change_pct": min_mover_change,
        "benchmark": {"ticker": benchmark_ticker, "snapshot": benchmark},
        "buckets": bucket_rows,
        "movers": movers,
        "errors": errors,
    }


def maybe_sync_from_lark(config_path: str) -> Optional[str]:
    """Try to sync the yaml from the configured Lark sheet. Return note or None.

    Failure is non-fatal: we log to stderr and keep using the existing yaml.
    """
    try:
        existing = load_config(config_path)
    except Exception:
        return None
    token = (existing.get("lark_sheet") or {}).get("spreadsheet_token")
    if not token:
        return "lark_sheet.spreadsheet_token 未配置，使用本地 yaml。"

    try:
        from sync_from_lark import sync as _sync  # noqa: WPS433 (local import on purpose)
    except Exception as exc:
        sys.stderr.write(f"[sync] 无法导入 sync_from_lark: {exc}\n")
        return f"sync_from_lark 不可用：{exc}"

    try:
        _sync(Path(config_path).expanduser().resolve())
        return "已从飞书表格同步最新观察池。"
    except Exception as exc:
        sys.stderr.write(f"[sync] 同步失败，沿用本地 yaml：{exc}\n")
        return f"飞书同步失败：{exc}（沿用本地 yaml）"


def maybe_scan_market(
    enabled: bool,
    min_change: float,
    abnormal_pct: float,
    min_dollar_volume_million: float,
    min_cap_billion: float,
    limit: int,
    evidence_date: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Optionally pull Nasdaq movers (money-first) via scan_market.scan_movers.

    Returns the scan evidence dict (with an extra `date_aligned` field) or None.
    The pool is Nasdaq-exchange filtered and ranked by dollar volume; the model
    decides tech/theme downstream. Failure is non-fatal: log to stderr and skip.
    """
    if not enabled:
        return None
    try:
        from scan_market import scan_movers as _scan  # noqa: WPS433
    except Exception as exc:
        sys.stderr.write(f"[scan] 无法导入 scan_market: {exc}\n")
        return None
    try:
        result = _scan(
            min_change_pct=min_change,
            abnormal_pct=abnormal_pct,
            min_dollar_volume_usd=min_dollar_volume_million * 1e6,
            min_market_cap_usd=min_cap_billion * 1e9,
            limit_per_side=limit,
        )
    except Exception as exc:
        sys.stderr.write(f"[scan] 纳斯达克异动扫描失败：{exc}\n")
        return None
    # Tag whether the scan window matches the report's date.
    scan_date = result.get("scan_date")
    result["date_aligned"] = bool(scan_date and evidence_date and scan_date == evidence_date)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch structured evidence for a US-market watchlist")
    parser.add_argument("--config", "-c", help="配置文件路径", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--date", "-d", help="交易日，格式 YYYY-MM-DD；默认取最近一个已结束交易日")
    parser.add_argument("--output", "-o", help="输出 JSON 文件路径")
    parser.add_argument("--json", "-j", action="store_true", help="兼容旧参数；当前默认总是输出 JSON")
    parser.add_argument("--no-sync", action="store_true", help="跳过从飞书表格同步配置")
    parser.add_argument(
        "--enrich-tickers",
        help="逗号分隔 ticker：只对这些票 + QQQ 取一年历史，回补 52周位置/20日趋势/量比/vs-QQQ，用于领先主题成员确认；不跑全量观察池",
    )
    parser.add_argument("--no-market-scan", action="store_true", help="跳过纳斯达克异动/赚钱效应扫描")
    parser.add_argument("--scan-universe", action="store_true", help="额外扫 curated 纳指科技 universe（重 pass，给广度分母 + 捞安静被买的票），默认关")
    parser.add_argument("--universe-path", default=str(UNIVERSE_PATH), help="universe 配置路径")
    parser.add_argument("--universe-workers", type=int, default=8, help="universe 扫描并发取数线程数，默认 8")
    parser.add_argument("--scan-limit", type=int, default=60, help="纳斯达克扫描每个方向最多保留几只（按成交额排序后截断），默认 60")
    parser.add_argument("--scan-min-change", type=float, default=3.0, help="纳斯达克扫描进池 |涨跌幅| 阈值，默认 3.0")
    parser.add_argument("--scan-abnormal-pct", type=float, default=7.0, help="|涨跌幅| ≥ 此值标记为异动（触发联网核查），默认 7.0")
    parser.add_argument("--scan-min-dollar-volume-million", type=float, default=50.0, help="纳斯达克扫描成交额下限（百万美元），默认 50（即 $50M）")
    parser.add_argument(
        "--scan-min-cap-billion",
        type=float,
        default=0.0,
        help="纳斯达克扫描市值下限（10亿美元为单位），默认 0（靠成交额过滤，不靠市值，保留小盘新方向）",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))

    # Enrich mode: a focused backfill for leading-theme members, not a full run.
    if args.enrich_tickers:
        tickers = [t.strip().upper() for t in args.enrich_tickers.split(",") if t.strip()]
        enrich = build_enrich_evidence(tickers, parse_report_date(args.date))
        content = json.dumps(enrich, ensure_ascii=False, indent=2, default=str)
        if args.output:
            output_path = Path(args.output).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content, encoding="utf-8")
            print(json.dumps({"status": "success", "output": str(output_path), "date": enrich["date"], "tickers": len(tickers)}, ensure_ascii=False))
            return
        print(content)
        return

    sync_note: Optional[str] = None
    if not args.no_sync:
        sync_note = maybe_sync_from_lark(args.config)

    config = load_config(args.config)
    evidence = build_market_evidence(config, parse_report_date(args.date))
    if sync_note:
        evidence["sync_note"] = sync_note

    market_wide = maybe_scan_market(
        enabled=not args.no_market_scan,
        min_change=args.scan_min_change,
        abnormal_pct=args.scan_abnormal_pct,
        min_dollar_volume_million=args.scan_min_dollar_volume_million,
        min_cap_billion=args.scan_min_cap_billion,
        limit=args.scan_limit,
        evidence_date=evidence.get("date"),
    )
    if market_wide is not None:
        evidence["market_wide_movers"] = market_wide

    if args.scan_universe:
        try:
            buckets = load_universe(args.universe_path)
            evidence["universe_scan"] = build_universe_scan(
                buckets, parse_report_date(args.date), workers=args.universe_workers
            )
        except Exception as exc:  # noqa: BLE001 — universe scan is opt-in and non-fatal
            sys.stderr.write(f"[universe] 扫描失败，跳过：{exc}\n")

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
