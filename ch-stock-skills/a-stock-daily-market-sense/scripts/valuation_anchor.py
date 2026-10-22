#!/usr/bin/env python3
"""估值锚定读数（valuation_anchor）——第 4 章维度 C·大盘轨的原子取数。

**这个脚本做什么。** 双轨方法（`references/methodology/dual_track.md`）里，大盘权重股
不追动量，用估值回答"下行空间多大、拿不拿得住"。本脚本只出确定性读数：

  1. **指数锚**：沪深300 / 上证50 的 PE-TTM 与 PB 在近 10 年（约 2520 个交易日）里的
     分位。数据走 Tushare `index_dailybasic`，复用 `market_panel.fetch_index_daily_basic`
     的 PG 键控缓存（与创业板 PE 图同一张表）。
  2. **个股锚**：已锁星（★★/★★★）主线内、总市值 ≥ 400 亿的成员，PE-TTM 与 PB 在
     自身近 3 年（约 750 个交易日）里的分位，加当日股息率（dv_ttm）。数据走 Tushare
     `daily_basic`（按 ts_code 拉历史），缓存进 `dms_valuation_daily` 表，之后只补增量。

**纪律。** 脚本只给分位与样本量，不给"低估/偏贵"档位——档位由模型按 dual_track.md
的 30/70 分界结合行业周期判定。PE-TTM 为负或缺失（亏损股）时 `pe_valid=false`，分位
只按正值样本算；样本不足 `MIN_SAMPLES` 的分位写 null，`reason` 写明。分位口径：
历史里 ≤ 当日值的占比（含当日），与创业板 PE 图一致。

用法（产物落 module_context 目录，数字溯源门禁按该路径发现读数）：
  python3 scripts/valuation_anchor.py --asof 20260429 \\
    --context reports/module_context_20260429/module3_money_effect.json \\
    --mapping reports/module_context_20260429/module3_theme_map.json \\
    --output reports/module_context_20260429/valuation_anchor.json
  python3 scripts/valuation_anchor.py --asof 20260429 --codes 002371.SZ,688012.SH
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))
sys.path.insert(0, str(_SCRIPT_DIR))

# 大小盘分界（亿元）：≥ 本值走大盘轨（估值锚定），< 本值走中小盘轨（流动性挖掘）。
LARGE_CAP_MIN_100M = 400.0
INDEX_CODES = (("000300.SH", "沪深300"), ("000016.SH", "上证50"))
INDEX_WINDOW_DAYS = 2520        # 指数分位母体：近 10 年交易日
STOCK_WINDOW_DAYS = 750         # 个股分位母体：近 3 年交易日
MIN_SAMPLES = 250               # 少于此样本不出分位
INDEX_FETCH_YEARS = 11          # 指数取数回看（日历年），覆盖 10 年窗口
STOCK_FETCH_YEARS = 3.2         # 个股取数回看（日历年），覆盖 3 年窗口
STOCK_FIELDS = "ts_code,trade_date,pe_ttm,pb,dv_ttm,total_mv"
MAINLINE_MIN_STARS = 2

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS dms_valuation_daily (
    ts_code     TEXT NOT NULL,
    trade_date  DATE NOT NULL,
    pe_ttm      DOUBLE PRECISION,
    pb          DOUBLE PRECISION,
    dv_ttm      DOUBLE PRECISION,
    total_mv    DOUBLE PRECISION,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ts_code, trade_date)
)
"""


# ---------------------------------------------------------------------------
# 纯计算（可单测，不碰网络与数据库）
# ---------------------------------------------------------------------------
def percentile_of_last(values: Iterable[Any], window: int, positive_only: bool = True) -> Dict[str, Any]:
    """序列末值在最近 window 个有效样本里的分位（≤ 末值的占比，含末值）。

    返回 latest / percentile / samples / reason。末值无效或样本不足时 percentile=None。
    """
    series = pd.to_numeric(pd.Series(list(values), dtype="object"), errors="coerce")
    valid = series.dropna()
    if positive_only:
        valid = valid[valid > 0]
    valid = valid.tail(window)
    raw_last = series.iloc[-1] if len(series) else None
    latest = None if raw_last is None or pd.isna(raw_last) else float(raw_last)
    if latest is None or (positive_only and latest <= 0):
        return {"latest": _round(latest), "percentile": None, "samples": int(len(valid)),
                "reason": "latest value missing or non-positive"}
    if len(valid) < MIN_SAMPLES:
        return {"latest": _round(latest), "percentile": None, "samples": int(len(valid)),
                "reason": f"fewer than {MIN_SAMPLES} valid samples"}
    pct = float((valid <= latest).mean() * 100.0)
    return {"latest": _round(latest), "percentile": round(pct, 1), "samples": int(len(valid)), "reason": None}


def select_large_cap_members(context: Any, mapping: Any, min_mv: float = LARGE_CAP_MIN_100M) -> List[Dict[str, Any]]:
    """已锁星主线（stars ≥ 2）里总市值 ≥ min_mv 亿的成员，带主线与细分归属。"""
    records = (((context or {}).get("money_effect") or {}).get("theme_grouping_aid") or {}).get("records") or []
    by_code = {r.get("ts_code"): r for r in records if isinstance(r, dict) and r.get("ts_code")}
    picked: List[Dict[str, Any]] = []
    for theme in (mapping or {}).get("themes") or []:
        stars = theme.get("stars")
        if not isinstance(stars, int) or stars < MAINLINE_MIN_STARS:
            continue
        subline_of = {}
        for sub in theme.get("sublines") or []:
            for code in sub.get("members") or []:
                subline_of[code] = sub.get("name")
        for code in theme.get("members") or []:
            record = by_code.get(code) or {}
            mv = _num(record.get("total_mv_100m_yuan"))
            if mv is None or mv < min_mv:
                continue
            picked.append({
                "ts_code": code,
                "name": record.get("name"),
                "theme": theme.get("name"),
                "stars": stars,
                "subline": subline_of.get(code),
                "total_mv_100m_yuan": _round(mv),
            })
    return picked


def stock_reading(history: pd.DataFrame, asof: str) -> Dict[str, Any]:
    """单只个股的估值锚读数（history 为该股 daily_basic 历史）。"""
    if history is None or history.empty:
        return {"available": False, "reason": "no daily_basic history"}
    df = history.copy()
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "")
    df = df.loc[df["trade_date"] <= asof].sort_values("trade_date")
    if df.empty or df["trade_date"].iloc[-1] != asof:
        return {"available": False, "reason": f"no daily_basic row on {asof}"}
    pe = percentile_of_last(df["pe_ttm"], STOCK_WINDOW_DAYS)
    pb = percentile_of_last(df["pb"], STOCK_WINDOW_DAYS)
    dv = _num(df["dv_ttm"].iloc[-1]) if "dv_ttm" in df.columns else None
    return {
        "available": True,
        "as_of": asof,
        "pe_ttm": pe["latest"],
        "pe_valid": pe["latest"] is not None and pe["latest"] > 0,
        "pe_pct_3y": pe["percentile"],
        "pe_samples": pe["samples"],
        "pb": pb["latest"],
        "pb_pct_3y": pb["percentile"],
        "pb_samples": pb["samples"],
        "dv_ttm_pct": _round(dv),
        "reason": pe["reason"] or pb["reason"],
    }


def index_reading(daily_basic: pd.DataFrame, asof: str) -> Dict[str, Any]:
    if daily_basic is None or daily_basic.empty:
        return {"available": False, "reason": "index_dailybasic returned no data"}
    df = daily_basic.copy()
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "")
    df = df.loc[df["trade_date"] <= asof].sort_values("trade_date")
    if df.empty:
        return {"available": False, "reason": f"no index_dailybasic rows on or before {asof}"}
    pe = percentile_of_last(df["pe_ttm"], INDEX_WINDOW_DAYS)
    pb = percentile_of_last(df["pb"], INDEX_WINDOW_DAYS)
    return {
        "available": True,
        "as_of": str(df["trade_date"].iloc[-1]),
        "pe_ttm": pe["latest"],
        "pe_pct_10y": pe["percentile"],
        "pe_samples": pe["samples"],
        "pb": pb["latest"],
        "pb_pct_10y": pb["percentile"],
        "pb_samples": pb["samples"],
        "reason": pe["reason"] or pb["reason"],
    }


# ---------------------------------------------------------------------------
# 取数（Tushare + PG 缓存）
# ---------------------------------------------------------------------------
def _years_before(asof: str, years: float) -> str:
    d = datetime.strptime(asof, "%Y%m%d") - timedelta(days=int(years * 365.25))
    return d.strftime("%Y%m%d")


def fetch_index_history(pro, ts_code: str, asof: str) -> pd.DataFrame:
    import market_panel as mp  # 延迟导入：纯计算单测不需要 tushare

    return mp.fetch_index_daily_basic(pro, ts_code, _years_before(asof, INDEX_FETCH_YEARS), asof)


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(TABLE_DDL)


def read_cached_stock(conn, ts_code: str, start: str, asof: str) -> pd.DataFrame:
    sql = ("SELECT ts_code, trade_date, pe_ttm, pb, dv_ttm, total_mv FROM dms_valuation_daily "
           "WHERE ts_code = %s AND trade_date BETWEEN %s AND %s ORDER BY trade_date")
    with conn.cursor() as cur:
        cur.execute(sql, (ts_code, datetime.strptime(start, "%Y%m%d").date(), datetime.strptime(asof, "%Y%m%d").date()))
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["ts_code", "trade_date", "pe_ttm", "pb", "dv_ttm", "total_mv"])
    if not df.empty:
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
    return df


def write_cached_stock(conn, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    from psycopg2.extras import execute_values

    records = [
        (r.ts_code, datetime.strptime(str(r.trade_date), "%Y%m%d").date(),
         _num(r.pe_ttm), _num(r.pb), _num(r.dv_ttm), _num(r.total_mv))
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO dms_valuation_daily (ts_code, trade_date, pe_ttm, pb, dv_ttm, total_mv) VALUES %s "
            "ON CONFLICT (ts_code, trade_date) DO UPDATE SET pe_ttm = EXCLUDED.pe_ttm, pb = EXCLUDED.pb, "
            "dv_ttm = EXCLUDED.dv_ttm, total_mv = EXCLUDED.total_mv, updated_at = NOW()",
            records,
        )


def fetch_stock_history(pro, conn, ts_code: str, asof: str) -> pd.DataFrame:
    """缓存优先：只向 Tushare 补缓存末日之后（或整段缺失）的区间。"""
    start = _years_before(asof, STOCK_FETCH_YEARS)
    cached = read_cached_stock(conn, ts_code, start, asof) if conn is not None else pd.DataFrame()
    fetch_from = start
    if not cached.empty:
        if cached["trade_date"].iloc[-1] >= asof and cached["trade_date"].iloc[0] <= _years_before(asof, 2.9):
            return cached
        last = datetime.strptime(cached["trade_date"].iloc[-1], "%Y%m%d") + timedelta(days=1)
        fetch_from = last.strftime("%Y%m%d") if cached["trade_date"].iloc[0] <= _years_before(asof, 2.9) else start
    try:
        fresh = pro.daily_basic(ts_code=ts_code, start_date=fetch_from, end_date=asof, fields=STOCK_FIELDS)
    except Exception as exc:  # 网络/权限失败：保留缓存读数，原因进 stderr
        print(f"[warn] daily_basic failed for {ts_code}: {exc}", file=sys.stderr)
        fresh = None
    if fresh is not None and not fresh.empty:
        fresh = fresh.copy()
        fresh["trade_date"] = fresh["trade_date"].astype(str)
        if conn is not None:
            write_cached_stock(conn, fresh)
        cached = pd.concat([cached, fresh], ignore_index=True) if not cached.empty else fresh
        cached = cached.drop_duplicates(subset=["ts_code", "trade_date"], keep="last").sort_values("trade_date")
    return cached


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def build_anchor(asof: str, members: List[Dict[str, Any]], pro, conn) -> Dict[str, Any]:
    indices = []
    for code, name in INDEX_CODES:
        try:
            reading = index_reading(fetch_index_history(pro, code, asof), asof)
        except Exception as exc:
            reading = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        indices.append({"ts_code": code, "name": name, **reading})
    stocks = []
    for member in members:
        try:
            reading = stock_reading(fetch_stock_history(pro, conn, member["ts_code"], asof), asof)
        except Exception as exc:
            reading = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        stocks.append({**member, **reading})
    return {
        "asof": asof,
        "large_cap_min_100m_yuan": LARGE_CAP_MIN_100M,
        "index_window_days": INDEX_WINDOW_DAYS,
        "stock_window_days": STOCK_WINDOW_DAYS,
        "min_samples": MIN_SAMPLES,
        "indices": indices,
        "stocks": stocks,
        "percentile_rule": "历史里 ≤ 当日值的占比（含当日），PE/PB 只取正值样本",
        "model_responsibility": (
            "脚本只给分位与样本量。低估（≤30）/合理（30–70）/偏贵（≥70）由模型按 "
            "dual_track.md 结合行业周期判定；pe_valid=false 时改看 PB。估值只回答下行空间，"
            "不回答涨跌，不得写成买卖建议。"
        ),
    }


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="大盘轨估值锚定读数（指数 10 年 / 个股 3 年分位）")
    parser.add_argument("--asof", required=True, help="YYYYMMDD")
    parser.add_argument("--context", type=Path, help="module3_money_effect.json")
    parser.add_argument("--mapping", type=Path, help="已锁星的 module3_theme_map.json")
    parser.add_argument("--codes", help="调试用：逗号分隔的 ts_code，跳过主线筛选")
    parser.add_argument("--min-mv", type=float, default=LARGE_CAP_MIN_100M, help="大盘轨市值下限（亿元）")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-cache", action="store_true", help="不读写 dms_valuation_daily")
    args = parser.parse_args(argv)
    asof = args.asof.replace("-", "")

    if args.codes:
        members = [{"ts_code": c.strip(), "name": None, "theme": None, "stars": None, "subline": None,
                    "total_mv_100m_yuan": None} for c in args.codes.split(",") if c.strip()]
    else:
        if not (args.context and args.mapping):
            parser.error("--context 与 --mapping 必须同时给出（或用 --codes 调试）")
        context = json.loads(args.context.read_text(encoding="utf-8"))
        mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
        unlocked = [t.get("name") for t in mapping.get("themes") or [] if t.get("stars") is None]
        if unlocked:
            parser.error(f"主题映射尚未锁星：{unlocked}——先跑 compute-theme-stats 并锁定 stars")
        members = select_large_cap_members(context, mapping, args.min_mv)

    import market_panel as mp

    pro = mp.get_pro()
    conn_ctx = None
    if not args.no_cache:
        from db_core import get_connection

        conn_ctx = get_connection()
    if conn_ctx is not None:
        with conn_ctx as conn:
            ensure_table(conn)
            payload = build_anchor(asof, members, pro, conn)
    else:
        payload = build_anchor(asof, members, pro, None)

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
