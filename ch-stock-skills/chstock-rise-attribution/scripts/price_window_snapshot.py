#!/usr/bin/env python3
"""Price + sector snapshot for single-day rise attribution.

Pulls Tushare `daily` + `daily_basic` for the target ts_code over a T-N ~ T+1
window, then fetches `stock_basic` to resolve the SW level-1 industry and
computes same-industry pct_chg mean on the trade date. The output is purely
deterministic — no commentary, no L2 broker-detail (Longhubang) — so the model
can use it as an evidence card without re-interpreting.

Outputs JSON to stdout (or --output)::

    {
      "ts_code": "...",
      "name": "...",
      "industry": "...",          # SW level-1 from stock_basic
      "trade_date": "YYYY-MM-DD",
      "window": [...trading dates...],
      "target_daily": [ {trade_date, open, high, low, close, pre_close, pct_chg, vol, amount}, ... ],
      "target_basic":  [ {trade_date, turnover_rate, turnover_rate_f, volume_ratio, pe, pb, total_mv, circ_mv}, ... ],
      "industry_peers_on_date": {
          "industry": "...",
          "peer_count": N,
          "mean_pct_chg": X.XX,
          "median_pct_chg": X.XX,
          "limit_up_count": K,         # |pct_chg| >= 9.8 上方
          "limit_down_count": K
      },
      "limit_up_flag": bool,           # target close = pre_close * 1.10 ±0.005 (主板) or 1.20 (科创/创业)
      "notes": [ ... soft warnings, e.g. "industry mapping missing" ... ]
    }

Dependencies: `pip install tushare`. Reads TUSHARE_TOKEN from env or .env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def get_token() -> str:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "TUSHARE_TOKEN":
                return value.strip().strip('"').strip("'")
    raise SystemExit("Missing TUSHARE_TOKEN. Set it in env or cwd/.env.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="个股 T-N~T+1 量价 + 申万一级同行业 pct_chg 均值")
    parser.add_argument("--ts-code", required=True, help="600519.SH / 300308.SZ")
    parser.add_argument("--trade-date", required=True, help="目标交易日 YYYY-MM-DD")
    parser.add_argument("--window", type=int, default=5, help="窗口长度（向前 N 个自然日，向后 1 个），默认 5")
    parser.add_argument("--output", default="", help="输出 JSON 文件路径，缺省 stdout")
    return parser.parse_args()


def date_to_tushare(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").strftime("%Y%m%d")


def emit(payload: dict[str, Any], output: str) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text)


def detect_limit_up(close: float, pre_close: float, ts_code: str) -> bool:
    """Heuristic limit-up check; not a substitute for the official 涨跌停 table."""
    if pre_close is None or close is None or pre_close <= 0:
        return False
    ratio = close / pre_close
    is_star = ts_code.startswith(("688", "8")) or ts_code.startswith("300") or ts_code.startswith("301")
    target = 1.20 if is_star else 1.10
    return abs(ratio - target) <= 0.005


def main() -> int:
    args = parse_args()

    try:
        import tushare as ts
    except ImportError:
        raise SystemExit("Missing dependency: pip install tushare")

    pro = ts.pro_api(get_token())

    trade_dt = datetime.strptime(args.trade_date, "%Y-%m-%d")
    start_dt = trade_dt - timedelta(days=args.window + 5)  # buffer for weekends
    end_dt = trade_dt + timedelta(days=3)

    start_str = start_dt.strftime("%Y%m%d")
    end_str = end_dt.strftime("%Y%m%d")
    target_date_str = date_to_tushare(args.trade_date)

    notes: list[str] = []

    target_daily = pro.daily(ts_code=args.ts_code, start_date=start_str, end_date=end_str)
    target_basic = pro.daily_basic(ts_code=args.ts_code, start_date=start_str, end_date=end_str)

    basic = pro.stock_basic(
        exchange="",
        list_status="L",
        fields="ts_code,name,industry,area,market,list_date",
    )
    row = basic[basic["ts_code"] == args.ts_code]
    if row.empty:
        notes.append(f"stock_basic 未命中 {args.ts_code}；同行业对照跳过。")
        name = ""
        industry = ""
    else:
        name = str(row.iloc[0]["name"])
        industry = str(row.iloc[0]["industry"]) if row.iloc[0]["industry"] is not None else ""

    industry_payload: dict[str, Any] | None = None
    if industry:
        peers = basic[basic["industry"] == industry]
        peer_codes = peers["ts_code"].tolist()
        if peer_codes:
            day_df = pro.daily(trade_date=target_date_str)
            day_df = day_df[day_df["ts_code"].isin(peer_codes)]
            if day_df.empty:
                notes.append(f"{args.trade_date} 行业 {industry} 当日 daily 为空（可能非交易日）。")
            else:
                mean_pct = float(day_df["pct_chg"].mean())
                median_pct = float(day_df["pct_chg"].median())
                limit_up = int((day_df["pct_chg"] >= 9.8).sum())
                limit_down = int((day_df["pct_chg"] <= -9.8).sum())
                industry_payload = {
                    "industry": industry,
                    "peer_count": int(len(day_df)),
                    "mean_pct_chg": round(mean_pct, 4),
                    "median_pct_chg": round(median_pct, 4),
                    "limit_up_count": limit_up,
                    "limit_down_count": limit_down,
                }
    else:
        notes.append("申万行业字段缺失；同行业对照跳过。")

    target_daily_records = target_daily.sort_values("trade_date").to_dict(orient="records")
    target_basic_records = target_basic.sort_values("trade_date").to_dict(orient="records")

    limit_up_flag = False
    for rec in target_daily_records:
        if str(rec.get("trade_date")) == target_date_str:
            limit_up_flag = detect_limit_up(
                float(rec.get("close") or 0),
                float(rec.get("pre_close") or 0),
                args.ts_code,
            )
            break
    else:
        notes.append(f"目标日 {args.trade_date} 在 daily 中缺失（可能非交易日或停牌）。")

    emit(
        {
            "ts_code": args.ts_code,
            "name": name,
            "industry": industry,
            "trade_date": args.trade_date,
            "window_days": args.window,
            "target_daily": target_daily_records,
            "target_basic": target_basic_records,
            "industry_peers_on_date": industry_payload,
            "limit_up_flag": limit_up_flag,
            "notes": notes,
        },
        args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
