#!/usr/bin/env python3
"""中小盘流动性扫描（liquidity_scan）——第 4 章维度 C·中小盘轨的原子取数。

**这个脚本做什么。** 双轨方法（`references/methodology/dual_track.md`）里，中小盘看
"新进资金在挖哪里"。本脚本只做流动性门槛这一层确定性筛选，边际变化（业绩/订单、
新技术、新细分）由模型判断：

  门槛（全部来自 PG 的 stock_daily / stock_daily_basic，不调网络）：
    - 总市值 < 400 亿（大小盘分界，与 valuation_anchor 同一条线）；
    - 当日成交额 ≥ 前 20 日均额的 1.8 倍，**或**当日换手率 ≥ 前 20 日均换手的 2 倍；
    - 近 5 日里放量日（当日成交额 > 前 20 日均额 1.5 倍，与 `sustained_volume_days_5`
      同口径）≥ 3 天——排除一日游。

给了 `--mapping`（已锁星的 module3_theme_map.json）时，按成员关系标出每只候选所属的
主线与细分线路（`in_mainline` = 所属主题 ★★ 及以上），方便模型把候选分成"主线内"
与"主线外潜在新线索"两组；这只是确定性的成员连接，不替模型下结论。涨跌方向不作
门槛（放量下跌同样是流动性事件），`pct_chg` / `ret_5d` 照实给出，由模型取舍。

用法：
  python3 scripts/liquidity_scan.py --asof 20260429 \\
    --mapping reports/module_context_20260429/module3_theme_map.json \\
    --output reports/module_context_20260429/liquidity_scan.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

SMALL_CAP_MAX_100M = 400.0      # 与 valuation_anchor.LARGE_CAP_MIN_100M 同一条分界
AMOUNT_RATIO_MIN = 1.8          # 当日成交额 / 前 20 日均额
TURNOVER_RATIO_MIN = 2.0        # 当日换手 / 前 20 日均换手
HEAVY_DAY_RATIO = 1.5           # 放量日定义，与 market_panel.sustained_volume_days_5 一致
HEAVY_DAYS_MIN = 3              # 近 5 日放量日下限
BASE_WINDOW = 20
BASE_MIN_OBS = 15
LOOKBACK_DAYS = BASE_WINDOW + 5 + 1   # 5 个观察日各需前 20 日基准
OUTSIDE_LIMIT = 60              # 主线外候选按放量倍数截取前 N 只
YI_FROM_THOUSAND = 1.0e5        # stock_daily.amount 千元 → 亿元
YI_FROM_WAN = 1.0e4             # total_mv 万元 → 亿元


# ---------------------------------------------------------------------------
# 纯计算（可单测，不碰数据库）
# ---------------------------------------------------------------------------
def compute_liquidity(daily: pd.DataFrame, basic: pd.DataFrame, asof: str) -> pd.DataFrame:
    """逐股算 asof 当日的流动性读数，返回全部有读数的股票（未过门槛）。

    daily 需含 ts_code/trade_date/pct_chg/amount；basic 需含 ts_code/trade_date/
    turnover_rate/total_mv。trade_date 统一成 YYYYMMDD 字符串。
    """
    d = daily.copy()
    d["trade_date"] = d["trade_date"].astype(str).str.replace("-", "")
    b = basic.copy()
    b["trade_date"] = b["trade_date"].astype(str).str.replace("-", "")
    df = d.merge(b[["ts_code", "trade_date", "turnover_rate", "total_mv"]], on=["ts_code", "trade_date"], how="left")
    for col in ("pct_chg", "amount", "turnover_rate", "total_mv"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values(["ts_code", "trade_date"])
    g = df.groupby("ts_code", group_keys=False)
    df["amount_base"] = g["amount"].transform(lambda s: s.shift(1).rolling(BASE_WINDOW, min_periods=BASE_MIN_OBS).mean())
    df["turnover_base"] = g["turnover_rate"].transform(
        lambda s: s.shift(1).rolling(BASE_WINDOW, min_periods=BASE_MIN_OBS).mean()
    )
    df["amount_ratio_20d"] = df["amount"] / df["amount_base"].where(df["amount_base"] > 0)
    df["turnover_ratio_20d"] = df["turnover_rate"] / df["turnover_base"].where(df["turnover_base"] > 0)
    df["heavy_day"] = df["amount_ratio_20d"].gt(HEAVY_DAY_RATIO)
    g = df.groupby("ts_code", group_keys=False)
    df["heavy_days_5"] = g["heavy_day"].transform(lambda s: s.rolling(5, min_periods=1).sum())
    # 5 日涨幅用 pct_chg 连乘，避开除权缺口（stock_daily 是未复权价）
    df["ret_5d"] = g["pct_chg"].transform(
        lambda s: ((1 + s / 100.0).rolling(5, min_periods=5).apply(lambda x: x.prod(), raw=True) - 1) * 100.0
    )
    today = df.loc[df["trade_date"] == asof].copy()
    today = today.dropna(subset=["amount_ratio_20d"])
    today["total_mv_100m_yuan"] = today["total_mv"] / YI_FROM_WAN
    today["amount_100m_yuan"] = today["amount"] / YI_FROM_THOUSAND
    return today


def apply_thresholds(today: pd.DataFrame) -> pd.DataFrame:
    small = today["total_mv_100m_yuan"] < SMALL_CAP_MAX_100M
    surge = today["amount_ratio_20d"].ge(AMOUNT_RATIO_MIN) | today["turnover_ratio_20d"].ge(TURNOVER_RATIO_MIN)
    sustained = today["heavy_days_5"].ge(HEAVY_DAYS_MIN)
    return today.loc[small & surge & sustained].sort_values("amount_ratio_20d", ascending=False)


def membership_index(mapping: Any) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for theme in (mapping or {}).get("themes") or []:
        subline_of = {}
        for sub in theme.get("sublines") or []:
            for code in sub.get("members") or []:
                subline_of[code] = sub.get("name")
        for code in theme.get("members") or []:
            stars = theme.get("stars")
            index[code] = {
                "theme": theme.get("name"),
                "stars": stars,
                "subline": subline_of.get(code),
                "in_mainline": isinstance(stars, int) and stars >= 2,
            }
    return index


def build_scan(today: pd.DataFrame, names: Dict[str, str], mapping: Any, asof: str) -> Dict[str, Any]:
    passed = apply_thresholds(today)
    members = membership_index(mapping)
    inside: List[Dict[str, Any]] = []
    outside: List[Dict[str, Any]] = []
    for row in passed.itertuples(index=False):
        tag = members.get(row.ts_code) or {"theme": None, "stars": None, "subline": None, "in_mainline": False}
        name = names.get(row.ts_code)
        item = {
            "ts_code": row.ts_code,
            "name": name,
            "is_st": bool(name and "ST" in name.upper()),
            "total_mv_100m_yuan": _round(row.total_mv_100m_yuan),
            "amount_100m_yuan": _round(row.amount_100m_yuan),
            "amount_ratio_20d": _round(row.amount_ratio_20d),
            "turnover_rate": _round(row.turnover_rate),
            "turnover_ratio_20d": _round(row.turnover_ratio_20d),
            "heavy_days_5": int(row.heavy_days_5),
            "pct_chg": _round(row.pct_chg),
            "ret_5d": _round(row.ret_5d),
            **tag,
        }
        (inside if tag["in_mainline"] else outside).append(item)
    return {
        "asof": asof,
        "thresholds": {
            "small_cap_max_100m_yuan": SMALL_CAP_MAX_100M,
            "amount_ratio_20d_min": AMOUNT_RATIO_MIN,
            "turnover_ratio_20d_min": TURNOVER_RATIO_MIN,
            "heavy_day_ratio": HEAVY_DAY_RATIO,
            "heavy_days_5_min": HEAVY_DAYS_MIN,
            "logic": "市值 < 400 亿 且 (成交额比 ≥ 1.8 或 换手比 ≥ 2.0) 且 近 5 日放量日 ≥ 3",
        },
        "universe": int(len(today)),
        "passed_count": int(len(passed)),
        "mapping_applied": bool(mapping),
        "mainline_candidates": inside,
        "outside_candidates": outside[:OUTSIDE_LIMIT],
        "outside_total": len(outside),
        "model_responsibility": (
            "脚本只过流动性门槛。边际变化（业绩/订单边际、新技术、新细分板块）由模型按 "
            "dual_track.md 判断；找不到边际变化的标「逻辑待验证」。主线外候选只能写进"
            "「潜在新线索」，不进主线表、不评星。"
        ),
    }


# ---------------------------------------------------------------------------
# 取数（PG）
# ---------------------------------------------------------------------------
def load_frames(conn, asof: str):
    asof_date = datetime.strptime(asof, "%Y%m%d").date()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT %s",
            (asof_date, LOOKBACK_DAYS),
        )
        days = sorted(row[0] for row in cur.fetchall())
    if not days or days[-1] != asof_date:
        raise RuntimeError(f"stock_daily has no rows on {asof}; run daily.build-evidence first")
    start = days[0]
    daily = pd.read_sql(
        "SELECT ts_code, trade_date, pct_chg, amount FROM stock_daily WHERE trade_date BETWEEN %s AND %s",
        conn, params=(start, asof_date),
    )
    basic = pd.read_sql(
        "SELECT ts_code, trade_date, turnover_rate, total_mv FROM stock_daily_basic WHERE trade_date BETWEEN %s AND %s",
        conn, params=(start, asof_date),
    )
    names = pd.read_sql("SELECT ts_code, name FROM stock_basic", conn)
    for frame in (daily, basic):
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y%m%d")
    return daily, basic, dict(zip(names["ts_code"], names["name"])), len(days)


def _round(value: Any, digits: int = 2) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="中小盘流动性门槛扫描")
    parser.add_argument("--asof", required=True, help="YYYYMMDD")
    parser.add_argument("--mapping", type=Path, help="已锁星的 module3_theme_map.json（可选）")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    asof = args.asof.replace("-", "")
    mapping = json.loads(args.mapping.read_text(encoding="utf-8")) if args.mapping else None

    from db_core import get_connection

    with get_connection() as conn:
        daily, basic, names, n_days = load_frames(conn, asof)
    today = compute_liquidity(daily, basic, asof)
    payload = build_scan(today, names, mapping, asof)
    payload["history_days_loaded"] = n_days
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
