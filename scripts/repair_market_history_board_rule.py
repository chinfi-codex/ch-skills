#!/usr/bin/env python3
"""One-off repair: recompute market_history counts under the board-rule limit
methodology (commit 7749d6f).

Historical rows were written with the ±9.8% pct_chg approximation (or mixed
legu/sohu sources), which inflates limit counts on 20cm-heavy days and misses
all 5%-band ST stocks. This script recomputes, for every date already present
in market_history, from PG stock_daily:

  - 上涨 / 下跌 / 平盘   pct_chg sign counts (uniform universe = pro.daily)
  - 涨停 / 跌停          exact board-rule flags via compute_limit_flags
  - 情绪值 / 活跃度      calc_market_sentiment on the recomputed inputs
  - 成交额               filled only when the stored value is blank

成交额(已有值)/融资净买入/全市场换手率 keep their stored values. A pre-repair
backup of the full table is written next to the report path printed at the end.

Usage:
    python3 scripts/repair_market_history_board_rule.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = REPO_ROOT / "ch-stock-skills" / "a-stock-daily-market-sense" / "scripts"
sys.path.insert(0, str(REPO_ROOT / "shared" / "data"))
sys.path.insert(0, str(SKILL_SCRIPTS))

import market_panel as mp  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    args = parser.parse_args()

    history = mp.load_market_history_df()
    if history.empty:
        print("market_history is empty; nothing to repair.")
        return 0

    history = history.copy()
    history["_trade_date"] = history["日期"].map(mp.history_date_to_trade_date)
    dates = sorted(d for d in history["_trade_date"].dropna().unique())
    print(f"market_history rows: {len(history)}, dates {dates[0]}..{dates[-1]}")

    backup_path = Path(f"/tmp/market_history_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    history.drop(columns=["_trade_date"]).to_csv(backup_path, index=False, encoding="utf-8-sig")

    pro = mp.get_pro()
    daily = mp.fetch_by_trade_dates(
        pro, "daily", dates, mp.DEFAULT_DAILY_FIELDS, sleep_seconds=0.12,
        cache_enabled=True, max_workers=4,
    )
    if daily.empty:
        raise RuntimeError("stock_daily returned no rows for the history window.")
    loaded = set(daily["trade_date"].astype(str).unique())
    missing = [d for d in dates if d not in loaded]
    if missing:
        print(f"[warn] {len(missing)} dates missing in stock_daily, left untouched: {missing}")

    basic = mp.fetch_stock_basic(pro)
    flags = mp.compute_limit_flags(daily, basic)

    daily = daily.copy()
    daily["trade_date"] = daily["trade_date"].astype(str)
    daily["pct_chg"] = pd.to_numeric(daily["pct_chg"], errors="coerce")
    daily["amount"] = pd.to_numeric(daily["amount"], errors="coerce")
    daily_by_date = dict(tuple(daily.groupby("trade_date")))
    flags_by_date = dict(tuple(flags.groupby("trade_date")))

    changes = []
    for idx, row in history.iterrows():
        trade_date = row["_trade_date"]
        day = daily_by_date.get(trade_date)
        if day is None or day.empty:
            continue
        flag_day = flags_by_date.get(trade_date)

        total = int(len(day))
        up = int((day["pct_chg"] > 0).sum())
        down = int((day["pct_chg"] < 0).sum())
        flat = total - up - down
        limit_up = int(flag_day["is_limit_up"].sum()) if flag_day is not None else 0
        limit_down = int(flag_day["is_limit_down"].sum()) if flag_day is not None else 0
        sentiment = round(mp.calc_market_sentiment(up, down, flat, limit_up, limit_down), 2)

        old = {col: row.get(col) for col in ("上涨", "下跌", "平盘", "涨停", "跌停", "情绪值", "活跃度")}
        history.at[idx, "上涨"] = up
        history.at[idx, "下跌"] = down
        history.at[idx, "平盘"] = flat
        history.at[idx, "涨停"] = limit_up
        history.at[idx, "跌停"] = limit_down
        history.at[idx, "情绪值"] = sentiment
        history.at[idx, "活跃度"] = sentiment
        if mp.is_blank_value(row.get("成交额")):
            total_amount = float(day["amount"].sum(skipna=True))
            if total_amount > 0:
                history.at[idx, "成交额"] = round(total_amount, 3)

        old_lu = pd.to_numeric(pd.Series([old["涨停"]]), errors="coerce").iloc[0]
        old_ld = pd.to_numeric(pd.Series([old["跌停"]]), errors="coerce").iloc[0]
        changes.append({
            "date": trade_date,
            "limit_up_old": None if pd.isna(old_lu) else int(old_lu),
            "limit_up_new": limit_up,
            "limit_down_old": None if pd.isna(old_ld) else int(old_ld),
            "limit_down_new": limit_down,
        })

    diff = pd.DataFrame(changes)
    diff["lu_delta"] = diff["limit_up_new"] - diff["limit_up_old"].fillna(0)
    diff["ld_delta"] = diff["limit_down_new"] - diff["limit_down_old"].fillna(0)
    print(f"\nrepaired dates: {len(diff)}")
    print(f"涨停 改动行数: {(diff['lu_delta'] != 0).sum()}, 平均改动 {diff['lu_delta'].mean():+.1f}, 最大 {diff['lu_delta'].abs().max():.0f}")
    print(f"跌停 改动行数: {(diff['ld_delta'] != 0).sum()}, 平均改动 {diff['ld_delta'].mean():+.1f}, 最大 {diff['ld_delta'].abs().max():.0f}")
    print("\n涨停改动最大的 8 个交易日:")
    cols = ["date", "limit_up_old", "limit_up_new", "limit_down_old", "limit_down_new"]
    print(diff.reindex(diff["lu_delta"].abs().sort_values(ascending=False).index)[cols].head(8).to_string(index=False))

    if args.dry_run:
        print("\n[dry-run] no writes performed.")
        return 0

    history = history.drop(columns=["_trade_date"])
    mp.write_market_history_df(history)
    print(f"\nwritten to {'PostgreSQL market_history + derived JSON' if mp.BACKEND == mp.Backend.POSTGRESQL else 'CSV'}")
    print(f"backup: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
