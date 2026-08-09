#!/usr/bin/env python3
"""把 market_history 往前补齐，并把融资列统一到 Tushare 的 T-1 口径。

**为什么需要它。** 两张状态卡的阈值都要 250~500 个交易日的滚动分位才启用，
而 `market_history` 常常只有几个月，阈值只能退回固定水平。另外历史上这张表
换过融资数据源：2026Q2 之前存的是另一个源的**当日**读数（比 Tushare 恒定少
约 14.2 亿），之后才切到 Tushare 的 T-1，同一列里混着三种口径——趋势卡为
"融资是滞后量"设计的相位配平与升档守卫因此作用在了错误的前提上。

本脚本做两件事：

  1. **补历史行**：按 Tushare `daily` 聚合涨跌家数与成交额，涨跌停用 `stk_limit`
     的当日涨跌停价逐股比对收盘价判定（point-in-time，不依赖当前 ST 名单），
     情绪值走 `market_panel.calc_market_sentiment` 同一套公式。已存在的行**不动
     这些字段**，只补空缺日期。
  2. **统一融资列**：所有行（含已存在的）的 `margin_net_buy` 一律重取为
     `margin(前一交易日)` 的 Tushare 值，并把 `margin_data_date` 显式写上。
     顺带把同期 margin 明细写入 `stock_margin`，好让两张卡能算 bp 归一。

未补的字段：`turnover_rate` 需要 `daily_basic` 的市值口径，历史段留空（它只
进情绪表展示，不参与任何状态判定）。

接口限速 200 次/分，分日结果缓存在 `--cache-dir`，可断点续跑；把之前跑过的
缓存目录传进来就不会重复取数。

用法：
  python scripts/backfill_market_history.py --start 20200601 --end 20260807 --dry-run
  python scripts/backfill_market_history.py --start 20200601 --end 20260807
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
_BUNDLED_SHARED = SCRIPT_DIR / "_shared"
_DEV_SHARED = SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

import market_panel  # noqa: E402
from db_core import BACKEND, Backend, get_connection  # noqa: E402

DEFAULT_CACHE = SCRIPT_DIR.parent / "reports" / ".backfill_cache"
MARGIN_FIELDS = "trade_date,exchange_id,rzye,rzmre,rzche"


class RateLimiter:
    """每端点独立令牌桶：最多 n 次 / 60 秒（接口限速 200/min，留余量）。"""

    def __init__(self, n: int = 170):
        self.n, self.calls, self.lock = n, deque(), threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.time()
                while self.calls and now - self.calls[0] > 60:
                    self.calls.popleft()
                if len(self.calls) < self.n:
                    self.calls.append(now)
                    return
                wait = 60 - (now - self.calls[0]) + 0.05
            time.sleep(wait)


LIMITERS = {"daily": RateLimiter(), "limit": RateLimiter()}


def call(endpoint: str, fn, tries: int = 6):
    for i in range(tries):
        LIMITERS[endpoint].acquire()
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(3.0 * (i + 1))
    return None


def cached_frame(cache_dir: Path, kind: str, day: str, fetch) -> pd.DataFrame:
    path = cache_dir / kind / f"{day}.parquet"
    if path.exists():
        return pd.read_parquet(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = call(kind, fetch)
    df = df if df is not None else pd.DataFrame()
    df.to_parquet(path, index=False)
    return df


def build_day_row(daily: pd.DataFrame, limits: pd.DataFrame, day: str) -> Optional[Dict]:
    """一天的盘面聚合。涨跌停用当日涨跌停价精确比对，取不到才退回 ±9.8% 近似。"""
    if daily is None or daily.empty:
        return None
    df = daily.copy()
    for col in ("close", "pre_close", "pct_chg", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if limits is not None and not limits.empty:
        lim = limits[["ts_code", "up_limit", "down_limit"]].copy()
        for col in ("up_limit", "down_limit"):
            lim[col] = pd.to_numeric(lim[col], errors="coerce")
        merged = df.merge(lim, on="ts_code", how="left")
        limit_up = int(((merged["close"] - merged["up_limit"]).abs() < 0.001).sum())
        limit_down = int(((merged["close"] - merged["down_limit"]).abs() < 0.001).sum())
    else:
        limit_up = int((df["pct_chg"] >= 9.8).sum())
        limit_down = int((df["pct_chg"] <= -9.8).sum())
    total = int(len(df))
    up = int((df["pct_chg"] > 0).sum())
    down = int((df["pct_chg"] < 0).sum())
    flat = total - up - down
    sentiment = market_panel.calc_market_sentiment(up, down, flat, limit_up, limit_down)
    return {
        "日期": market_panel.format_history_date(day),
        "上涨": up, "涨停": limit_up, "下跌": down, "跌停": limit_down, "平盘": flat,
        "活跃度": round(sentiment, 2), "情绪值": round(sentiment, 2),
        # tushare daily.amount 单位千元，与 market_history.amount 口径一致
        "成交额": round(float(df["amount"].sum(skipna=True)), 3),
    }


def fetch_margin(pro, start: str, end: str) -> pd.DataFrame:
    """按年分段拉两融明细（单次有行数上限），返回原始分交易所数据。"""
    frames = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        df = pro.margin(start_date=f"{year}0101", end_date=f"{year}1231", fields=MARGIN_FIELDS)
        if df is not None and not df.empty:
            frames.append(df)
        time.sleep(0.3)
    if not frames:
        return pd.DataFrame(columns=MARGIN_FIELDS.split(","))
    out = pd.concat(frames, ignore_index=True)
    out["trade_date"] = out["trade_date"].astype(str)
    for col in ("rzye", "rzmre", "rzche"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.loc[(out["trade_date"] >= start) & (out["trade_date"] <= end)]


def daily_net_buy(margin: pd.DataFrame) -> Dict[str, float]:
    """按交易日汇总净买入（元），剔除口径不全的日子。

    "全"不能用全样本众数判：北交所 2023-02 才进这个接口，五年跨度里应有的
    交易所数本身是变的，用全局众数会把 2023 年之前只有两所的日子全判成缺数
    （实测 1502 天里只剩 846 天）。改成和**此前** 20 个交易日比：那之前最多
    出现过几个所，当天就得有几个。

    用向后看而不是居中窗口，是因为居中窗口会在新交易所刚接入的那段回头把
    之前只有两所的日子也判成缺数——实测这会在 2023-01 挖出 21 天的缺口，而
    趋势卡遇到内部缺口要截断到最后一个缺口之后，可用连续窗口直接从 6 年缩到
    3.5 年。向后看的规则对"新增交易所"只向前抬高门槛，同时照样挡得住最新一天
    分批发布只到一个所的情况。
    """
    if margin.empty:
        return {}
    counts = margin.groupby("trade_date")["exchange_id"].nunique().sort_index()
    expected = counts.rolling(21, min_periods=1).max()
    keep = set(counts.index[counts >= expected])
    sub = margin.loc[margin["trade_date"].isin(keep)]
    grouped = sub.groupby("trade_date").agg(net=("rzmre", "sum"), repay=("rzche", "sum"))
    return {d: float(r.net - r.repay) for d, r in grouped.iterrows()}


def write_stock_margin(margin: pd.DataFrame) -> int:
    """把 margin 明细写进 stock_margin，好让两张卡能按余额做 bp 归一。"""
    if margin.empty:
        return 0
    written = 0
    for trade_date, day_df in margin.groupby("trade_date"):
        market_panel.write_cached_frame("margin", str(trade_date), day_df.reset_index(drop=True))
        written += 1
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐 market_history 并把融资列统一到 Tushare T-1 口径")
    ap.add_argument("--start", required=True, help="起始交易日 YYYYMMDD")
    ap.add_argument("--end", required=True, help="结束交易日 YYYYMMDD")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE), help="分日取数缓存目录，可断点续跑")
    ap.add_argument("--dry-run", action="store_true", help="只报告将要发生什么，不写库")
    args = ap.parse_args()

    if BACKEND != Backend.POSTGRESQL:
        print("error: 需要 PostgreSQL 后端", file=sys.stderr)
        return 1
    cache_dir = Path(args.cache_dir)
    pro = market_panel.get_pro()

    cal = pro.trade_cal(exchange="SSE", start_date=args.start, end_date=args.end, is_open="1")
    days = sorted(cal["cal_date"].astype(str).tolist())
    print(f"[cal] {len(days)} 个交易日 {days[0]}..{days[-1]}", flush=True)

    existing = market_panel.load_market_history_df()
    existing_dates = {
        market_panel.history_date_to_trade_date(v)
        for v in (existing["日期"].tolist() if not existing.empty else [])
    }
    missing = [d for d in days if d not in existing_dates]
    print(f"[plan] 已有 {len(existing_dates)} 行；需要新增 {len(missing)} 行；"
          f"全部 {len(days)} 行的融资列都会按 Tushare T-1 重写", flush=True)

    margin = fetch_margin(pro, args.start, args.end)
    net_by_date = daily_net_buy(margin)
    print(f"[margin] 取到 {len(net_by_date)} 个交易日的净买入", flush=True)

    if args.dry_run:
        sample = [d for d in days if d in existing_dates][:3]
        for d in sample:
            i = days.index(d)
            prev = days[i - 1] if i > 0 else None
            print(f"  {d}  margin_net_buy <- margin({prev}) = "
                  f"{net_by_date.get(prev, float('nan')) / 1e8:.1f} 亿")
        print("[dry-run] 未写库")
        return 0

    # 1) margin 明细落 stock_margin
    print(f"[stock_margin] 写入 {write_stock_margin(margin)} 天明细", flush=True)

    # 2) 补历史行
    def fetch_one(day: str) -> Optional[Dict]:
        daily = cached_frame(cache_dir, "daily", day, lambda: pro.daily(
            trade_date=day, fields="ts_code,trade_date,close,pre_close,pct_chg,amount"))
        limits = cached_frame(cache_dir, "limit", day, lambda: pro.stk_limit(
            trade_date=day, fields="trade_date,ts_code,up_limit,down_limit"))
        return build_day_row(daily, limits, day)

    new_rows: List[Dict] = []
    if missing:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=8) as pool:
            for i, row in enumerate(pool.map(fetch_one, missing)):
                if row:
                    new_rows.append(row)
                if (i + 1) % 100 == 0:
                    print(f"[daily] {i + 1}/{len(missing)} {round(time.time() - t0)}s", flush=True)
        print(f"[daily] 聚合出 {len(new_rows)} 行", flush=True)

    # 3) 合并 + 统一融资列
    frame = existing if not existing.empty else pd.DataFrame()
    for row in new_rows:
        frame = market_panel.merge_market_history_row(frame, row, list(row.keys()))

    prev_of = {d: days[i - 1] for i, d in enumerate(days) if i > 0}
    margin_col, margin_date_col, rewritten = [], [], 0
    for value in frame["日期"].tolist():
        day = market_panel.history_date_to_trade_date(value)
        prev = prev_of.get(day)
        net = net_by_date.get(prev) if prev else None
        if net is None:
            margin_col.append(None)
            margin_date_col.append(None)
        else:
            margin_col.append(round(net, 2))
            margin_date_col.append(market_panel.format_history_date(prev))
            rewritten += 1
    frame["融资净买入"] = margin_col
    frame["融资数据日"] = margin_date_col
    print(f"[margin] 重写 {rewritten}/{len(frame)} 行为 T-1 口径", flush=True)

    market_panel.write_market_history_df(frame)
    print(f"[done] market_history 现有 {len(frame)} 行", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
