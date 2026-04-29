#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股日报生成脚本。"""

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
DEFAULT_CONFIG_PATH = ROOT_DIR / "stock_pool.yaml"
LOOKBACK_DAYS = 15
REPORT_TYPE = "us_market_report"
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
    """加载配置文件。"""
    path = Path(config_path).expanduser().resolve() if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        return DEFAULT_CONFIG

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
    """解析命令行传入的日期。"""
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def fetch_chart_history(ticker: str) -> list[Dict[str, Any]]:
    """从 Yahoo chart 接口获取单只股票日线。"""
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
    return rows


def get_latest_completed_trading_day(history_by_ticker: Dict[str, list[Dict[str, Any]]], reference_ticker: str = "SPY") -> date:
    """根据日线数据确定最近一个已结束交易日。"""
    reference_history = history_by_ticker.get(reference_ticker)
    if not reference_history:
        available_histories = [history for history in history_by_ticker.values() if history]
        if not available_histories:
            raise RuntimeError("无法从行情中确定最近交易日。")
        reference_history = available_histories[0]
    return reference_history[-1]["date"]


def resolve_target_row(history: list[Dict[str, Any]], requested_date: Optional[date]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """从日线数据中定位目标交易日和前一交易日。"""
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


def build_stock_snapshot(ticker: str, history: Optional[list[Dict[str, Any]]], report_date: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """从历史日线构造单只股票在目标交易日的快照。"""
    try:
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
            "price": close_price,
            "change_pct": change_pct,
            "prev_close": prev_close,
            "after_hours": None,
            "after_hours_change": None,
            "five_day_trend": five_day_trend,
            "volume": target_row.get("volume"),
            "trade_date": target_row["date"].isoformat(),
        }
    except Exception:
        return None


def format_price(price: Optional[float]) -> str:
    if price is None:
        return "N/A"
    return f"${price:,.2f}"


def format_change(pct: Optional[float]) -> str:
    if pct is None:
        return "N/A"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def format_volume(volume: Optional[int]) -> str:
    if volume is None:
        return "N/A"
    if volume >= 100_000_000:
        return f"{volume / 100_000_000:.2f}亿"
    if volume >= 10_000:
        return f"{volume / 10_000:.2f}万"
    return str(volume)


def get_trend_desc(change_pct: float) -> str:
    if change_pct > 3:
        return "强势上涨"
    if change_pct > 1:
        return "温和上涨"
    if change_pct > -1:
        return "横盘震荡"
    if change_pct > -3:
        return "温和下跌"
    return "弱势下跌"


def generate_market_overview(indices_data: list[Dict[str, Any]], show_5day_trend: bool) -> str:
    """生成大盘概览。"""
    lines = ["## 【大盘概览】", ""]
    for data in indices_data:
        ticker = data["ticker"]
        name = data.get("name", ticker)
        trend_emoji = "📈" if data["change_pct"] >= 0 else "📉"
        lines.append(f"**{name} ({ticker})** {trend_emoji}")
        lines.append(f"- 收盘价: {format_price(data['price'])} ({format_change(data['change_pct'])})")
        if show_5day_trend and data.get("five_day_trend") is not None:
            trend_desc = get_trend_desc(data["five_day_trend"])
            lines.append(f"- 5日走势: {trend_desc} ({format_change(data['five_day_trend'])})")
        lines.append("")
    return "\n".join(lines)


def generate_group_section(group: Dict[str, Any], stocks_data: Dict[str, Dict[str, Any]]) -> str:
    """生成分组板块。"""
    name = group["name"]
    emoji = group.get("emoji", "📊")
    lines = [f"**{emoji} {name}**", "", "```", "| 股票 | 收盘价 | 涨跌幅 |", "|-----|--------|--------|"]

    for ticker in group["stocks"]:
        data = stocks_data.get(ticker)
        if data is None:
            lines.append(f"| {ticker} | N/A | N/A |")
            continue

        change_str = format_change(data["change_pct"])
        if data["change_pct"] <= -5:
            change_str += " ⚠️"
        elif data["change_pct"] >= 5:
            change_str += " 🚀"

        lines.append(f"| {ticker} | {format_price(data['price'])} | **{change_str}** |")

    lines.extend(["```", ""])
    return "\n".join(lines)


def generate_abnormal_scan(all_stocks_data: Dict[str, Dict[str, Any]], thresholds: Dict[str, float]) -> str:
    """生成异动扫描。"""
    big_drop_threshold = thresholds.get("big_drop", -10.0)
    big_rise_threshold = thresholds.get("big_rise", 10.0)
    warning_threshold = thresholds.get("warning", -5.0)

    big_drops = []
    big_rises = []
    warnings = []

    for ticker, data in all_stocks_data.items():
        if not data:
            continue
        change = data["change_pct"]
        if change <= big_drop_threshold:
            big_drops.append((ticker, change))
        elif change >= big_rise_threshold:
            big_rises.append((ticker, change))
        elif change <= warning_threshold:
            warnings.append((ticker, change))

    lines = ["## 【异动扫描】", ""]

    if big_drops:
        lines.extend(
            [
                f"**🔴 跌幅超过 {abs(big_drop_threshold):.0f}% 的个股**",
                "",
                "```",
                "| 股票 | 跌幅 | 备注 |",
                "|-----|------|------|",
            ]
        )
        for ticker, change in sorted(big_drops, key=lambda item: item[1]):
            lines.append(f"| **{ticker}** | **{change:.2f}%** | 建议复盘业绩、指引或事件催化 |")
        lines.extend(["```", ""])

    if big_rises:
        lines.extend(
            [
                f"**🟢 涨幅超过 {big_rise_threshold:.0f}% 的个股**",
                "",
                "```",
                "| 股票 | 涨幅 | 备注 |",
                "|-----|------|------|",
            ]
        )
        for ticker, change in sorted(big_rises, key=lambda item: item[1], reverse=True):
            lines.append(f"| **{ticker}** | **+{change:.2f}%** | 建议追踪财报、订单或政策催化 |")
        lines.extend(["```", ""])

    if warnings and not big_drops:
        warning_list = [f"{ticker} ({change:.2f}%)" for ticker, change in sorted(warnings, key=lambda item: item[1])[:5]]
        lines.append(f"**⚠️ 跌幅超过 {abs(warning_threshold):.0f}% 的关注名单**")
        lines.append(", ".join(warning_list))
        lines.append("")

    if not big_drops and not big_rises and not warnings:
        lines.append("当日波动整体可控，未出现显著异动个股。")
        lines.append("")

    return "\n".join(lines)


def generate_sector_observation(groups_data: Dict[str, list[str]], all_stocks_data: Dict[str, Dict[str, Any]]) -> str:
    """生成板块观察。"""
    lines = ["## 【板块观察】", ""]
    for group_name, tickers in groups_data.items():
        changes = [all_stocks_data[ticker]["change_pct"] for ticker in tickers if all_stocks_data.get(ticker)]
        if not changes:
            lines.append(f"- **{group_name}**: 缺少有效行情数据")
            continue

        avg_change = sum(changes) / len(changes)
        up_count = sum(1 for item in changes if item > 0)
        down_count = sum(1 for item in changes if item < 0)
        trend = "📈" if avg_change > 1 else "📉" if avg_change < -1 else "➡️"
        lines.append(f"- **{group_name}**: {trend} 平均 {format_change(avg_change)} (上涨 {up_count} / 下跌 {down_count})")

    lines.append("")
    return "\n".join(lines)


def generate_summary(all_stocks_data: Dict[str, Dict[str, Any]]) -> str:
    """生成一句话摘要。"""
    valid_items = [(ticker, data) for ticker, data in all_stocks_data.items() if data]
    if not valid_items:
        return "## 【一句话总结】\n\n目标股票池暂无可用数据。\n"

    sorted_items = sorted(valid_items, key=lambda item: item[1]["change_pct"])
    weakest_ticker, weakest_data = sorted_items[0]
    strongest_ticker, strongest_data = sorted_items[-1]
    return (
        "## 【一句话总结】\n\n"
        f"关注池中最强个股为 **{strongest_ticker}** ({format_change(strongest_data['change_pct'])})，"
        f"最弱个股为 **{weakest_ticker}** ({format_change(weakest_data['change_pct'])})，"
        "建议优先复盘极端波动标的对应的财报、指引和盘后消息。\n"
    )


def generate_full_report(config: Dict[str, Any], report_date: Optional[date] = None) -> tuple[str, str]:
    """生成完整日报。"""
    output_config = config.get("output", {})
    show_5day_trend = bool(output_config.get("show_5day_trend", True))
    show_volume = bool(output_config.get("show_volume", False))

    all_tickers = []
    for index_item in config.get("indices", []):
        all_tickers.append(index_item["ticker"])
    for group in config.get("groups", []):
        all_tickers.extend(group["stocks"])
    unique_tickers = list(dict.fromkeys(all_tickers))

    history_by_ticker = {}
    for ticker in unique_tickers:
        try:
            history_by_ticker[ticker] = fetch_chart_history(ticker)
        except Exception:
            history_by_ticker[ticker] = None

    if not any(history_by_ticker.values()):
        raise RuntimeError("所有股票的行情拉取都失败了，请稍后重试。")

    resolved_date = report_date or get_latest_completed_trading_day(history_by_ticker)

    lines = [f"# 美股日报 - {resolved_date.isoformat()}", "", "---", ""]

    indices_data = []
    for index_item in config.get("indices", []):
        data = build_stock_snapshot(index_item["ticker"], history_by_ticker.get(index_item["ticker"]), resolved_date)
        if data:
            data["name"] = index_item.get("name", index_item["ticker"])
            indices_data.append(data)
    lines.append(generate_market_overview(indices_data, show_5day_trend))

    all_stocks_data: Dict[str, Dict[str, Any]] = {}
    groups_data: Dict[str, list[str]] = {}
    lines.extend(["## 【目标股票池表现】", ""])

    for group in config.get("groups", []):
        group_name = group["name"]
        groups_data[group_name] = group["stocks"]
        for ticker in group["stocks"]:
            if ticker not in all_stocks_data:
                all_stocks_data[ticker] = build_stock_snapshot(ticker, history_by_ticker.get(ticker), resolved_date)
        lines.append(generate_group_section(group, all_stocks_data))

    lines.append(generate_abnormal_scan(all_stocks_data, config.get("thresholds", {})))
    lines.append(generate_sector_observation(groups_data, all_stocks_data))
    lines.append(generate_summary(all_stocks_data))

    if show_volume:
        lines.extend(["## 【成交量补充】", ""])
        for ticker, data in all_stocks_data.items():
            if data:
                lines.append(f"- {ticker}: {format_volume(data.get('volume'))}")
        lines.append("")

    return "\n".join(lines), resolved_date.isoformat()


def build_output_payload(report: str, report_date: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """构造 JSON 输出。"""
    return {
        "type": REPORT_TYPE,
        "date": report_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "content": report,
        "config": config,
    }


def main() -> None:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="生成美股日报")
    parser.add_argument("--config", "-c", help="配置文件路径", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--date", "-d", help="报告日期，格式 YYYY-MM-DD；默认取最近一个已结束交易日", default=None)
    parser.add_argument("--output", "-o", help="输出文件路径", default=None)
    parser.add_argument("--json", "-j", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    config = load_config(args.config)
    report, report_date = generate_full_report(config, parse_report_date(args.date))

    if args.json:
        print(json.dumps(build_output_payload(report, report_date, config), ensure_ascii=False, indent=2))
        return

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        print(json.dumps({"status": "success", "output": str(output_path), "date": report_date}, ensure_ascii=False))
        return

    print(report)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    main()
