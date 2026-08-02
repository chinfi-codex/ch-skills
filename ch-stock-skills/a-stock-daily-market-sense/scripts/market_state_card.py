#!/usr/bin/env python3
"""市场状态定位卡（market_state）——盘后研报模块 1 的状态定位机判区块。

回答的问题是「当前回撤属于什么量级（权重与成长小盘分层）、
调整是否接近尾声」。脚本只输出五个子块的确定性读数，不做任何定性文字；
状态定性由模型按 `references/methodology/market_state_framework.md` 完成：

  - index_position：六个宽基的位置分层（250 日高点回撤 / 20 日收益 / 60 日线乖离），
    权重（上证、沪深300）与成长小盘（中证1000、创业板、科创50）分开看；
  - breadth：全市场宽度（站上 20/60 日线占比、60 日收益为正占比），
    分母为当日有交易的个股，历史不足的个股从对应指标分母剔除；
  - sw_industries：31 个申万一级（SW2021）行业指数的 60 日收益、250 日高点
    回撤与高点日期，并给出上证综指的 250 日高点日期作基准，供模型比对
    「领涨行业是否先于宽基见顶」；
  - margin_trend：全市场融资余额（rzye，SSE/SZSE/BSE 按日求和，亿元）趋势，
    数据为 T-1 口径，输出块里标注实际 trade_date；
  - liquidity：全 A 成交额与 20 日均额。优先复用 build_panel evidence 里
    market_temperature / sentiment 已算好的数，没有 evidence 时退回
    market_history 自算。

数据口径：
  - 所有计算以 asof（含）为止，禁止未来数据；margin 取 <= asof 的最新 trade_date。
  - 指数与申万行业历史经 market_panel.fetch_index_daily 增量写入
    stock_index_daily 缓存，首次回填约 400 个自然日（≈260 个交易日）。
  - 融资余额回填至 stock_margin（约 280 个交易日窗口），按 trade_date 汇总
    各交易所 rzye。
  - 窗口不足时对应字段给 null 并置 insufficient_history: true，不报错。

失败降级与 trend_state_card 一致：任一子块异常只置该子块
available: false + reason，整体 PG 不可达时整个 block 返回
{"available": false, "reason": ...}，均不阻断主流程。

用法：
  python scripts/market_state_card.py --asof 20260731
  python scripts/market_state_card.py            # 默认今天
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))
from db_core import BACKEND, Backend, get_connection  # noqa: E402

# 宽基位置分层：前两个为权重指数，后四个为中小盘/成长
INDEXES = {
    "000001.SH": "上证综指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
}
BENCHMARK_INDEX = "000001.SH"

# 申万 2021 一级行业兜底清单（index_classify 失败时使用，与接口返回一致）
SW_L1_FALLBACK: List[Tuple[str, str]] = [
    ("801010.SI", "农林牧渔"),
    ("801030.SI", "基础化工"),
    ("801040.SI", "钢铁"),
    ("801050.SI", "有色金属"),
    ("801080.SI", "电子"),
    ("801110.SI", "家用电器"),
    ("801120.SI", "食品饮料"),
    ("801130.SI", "纺织服饰"),
    ("801140.SI", "轻工制造"),
    ("801150.SI", "医药生物"),
    ("801160.SI", "公用事业"),
    ("801170.SI", "交通运输"),
    ("801180.SI", "房地产"),
    ("801200.SI", "商贸零售"),
    ("801210.SI", "社会服务"),
    ("801230.SI", "综合"),
    ("801710.SI", "建筑材料"),
    ("801720.SI", "建筑装饰"),
    ("801730.SI", "电力设备"),
    ("801740.SI", "国防军工"),
    ("801750.SI", "计算机"),
    ("801760.SI", "传媒"),
    ("801770.SI", "通信"),
    ("801780.SI", "银行"),
    ("801790.SI", "非银金融"),
    ("801880.SI", "汽车"),
    ("801890.SI", "机械设备"),
    ("801950.SI", "煤炭"),
    ("801960.SI", "石油石化"),
    ("801970.SI", "环保"),
    ("801980.SI", "美容护理"),
]

HIGH_WINDOW = 250            # 250 日高点/回撤窗口（交易日）
INDEX_BACKFILL_CAL_DAYS = 400  # 首次回填起点：asof 前推约 400 个自然日 ≈ 260+ 交易日
BREADTH_MIN_DAYS = 61        # 60 日收益至少需要 61 个交易日
BREADTH_FETCH_DAYS = 70      # 宽度取数留的余量
MARGIN_BACKFILL_TRADING_DAYS = 280
MARGIN_FIELDS = "trade_date,exchange_id,rzye,rzmre,rzche"

_SW_L1_CACHE: Optional[List[Tuple[str, str]]] = None


# ---------------------------------------------------------------------------
# 纯计算（IO 与计算分离，测试直接注入合成 DataFrame）
# ---------------------------------------------------------------------------
def _normalize_price_frame(frame: pd.DataFrame, asof: str) -> pd.DataFrame:
    df = frame.copy()
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "")
    for column in ("open", "high", "low", "close"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    if "high" not in df.columns:
        df["high"] = df["close"]
    df = df.loc[df["trade_date"] <= asof].dropna(subset=["close"])
    return df.sort_values("trade_date").reset_index(drop=True)


def compute_position_metrics(frame: pd.DataFrame, asof: str) -> Dict[str, Any]:
    """单指数位置指标：250 日高点回撤、20/60 日收益、60 日线乖离。

    frame 至少含 trade_date/close（缺 high 时以 close 代替），只取 <= asof 的行。
    各项按自身最小窗口计算，窗口不足给 null；总行数 < 250 时置
    insufficient_history。
    """
    out: Dict[str, Any] = {
        "trade_date": None,
        "close": None,
        "high_250d": None,
        "high_250d_date": None,
        "drawdown_from_high_250d_pct": None,
        "ret_20d": None,
        "ret_60d": None,
        "close_vs_ma60_pct": None,
        "above_ma60": None,
        "insufficient_history": True,
    }
    df = _normalize_price_frame(frame, asof)
    if df.empty:
        return out

    closes = df["close"].tolist()
    dates = df["trade_date"].tolist()
    latest = float(closes[-1])
    out["trade_date"] = dates[-1]
    out["close"] = round(latest, 2)
    out["insufficient_history"] = len(df) < HIGH_WINDOW

    window = df.tail(HIGH_WINDOW)
    if len(window) >= HIGH_WINDOW:
        high_250 = float(window["high"].max())
        out["high_250d"] = round(high_250, 2)
        out["high_250d_date"] = str(window.loc[window["high"].idxmax(), "trade_date"])
        out["drawdown_from_high_250d_pct"] = round((latest / high_250 - 1.0) * 100.0, 2) if high_250 else None
    if len(closes) >= 21 and closes[-21]:
        out["ret_20d"] = round((latest / float(closes[-21]) - 1.0) * 100.0, 2)
    if len(closes) >= 61 and closes[-61]:
        out["ret_60d"] = round((latest / float(closes[-61]) - 1.0) * 100.0, 2)
    if len(closes) >= 60:
        ma60 = sum(closes[-60:]) / 60.0
        if ma60:
            out["close_vs_ma60_pct"] = round((latest / ma60 - 1.0) * 100.0, 2)
            out["above_ma60"] = bool(latest > ma60)
    return out


def compute_breadth(daily: pd.DataFrame, asof: str) -> Dict[str, Any]:
    """全市场宽度：站上 20/60 日线与 60 日收益为正的个股占比。

    daily 为全市场个股日线（ts_code/trade_date/close），只取 <= asof。
    分母为最新交易日有交易的个股；个股历史不足导致指标为 NaN 的，从该指标
    分母剔除。总窗口不足 61 个交易日时 60 日口径指标给 null。
    """
    out: Dict[str, Any] = {
        "trade_date": None,
        "total": 0,
        "pct_above_ma20": None,
        "above_ma20_count": None,
        "above_ma20_total": None,
        "pct_above_ma60": None,
        "above_ma60_count": None,
        "above_ma60_total": None,
        "pct_positive_ret_60d": None,
        "positive_ret_60d_count": None,
        "positive_ret_60d_total": None,
        "insufficient_history": True,
    }
    if daily is None or daily.empty:
        return out

    df = daily.copy()
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.loc[df["trade_date"] <= asof].dropna(subset=["close"])
    if df.empty:
        return out
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    distinct_days = int(df["trade_date"].nunique())
    out["insufficient_history"] = distinct_days < BREADTH_MIN_DAYS

    grouped = df.groupby("ts_code", group_keys=False)["close"]
    df["close_ma20"] = grouped.transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["close_ma60"] = grouped.transform(lambda s: s.rolling(60, min_periods=60).mean())
    df["ret_60d"] = grouped.pct_change(60) * 100.0

    day = df.loc[df["trade_date"] == df["trade_date"].max()]
    out["trade_date"] = str(day["trade_date"].iloc[0])
    out["total"] = int(len(day))

    def _metric(hit_mask: pd.Series, valid_mask: pd.Series, gated: bool) -> Tuple[Optional[float], Optional[int], Optional[int]]:
        valid_total = int(valid_mask.sum())
        if gated or valid_total == 0:
            return None, None, None
        count = int((hit_mask & valid_mask).sum())
        return round(100.0 * count / valid_total, 2), count, valid_total

    out["pct_above_ma20"], out["above_ma20_count"], out["above_ma20_total"] = _metric(
        day["close"] > day["close_ma20"], day["close_ma20"].notna(), distinct_days < 20
    )
    out["pct_above_ma60"], out["above_ma60_count"], out["above_ma60_total"] = _metric(
        day["close"] > day["close_ma60"], day["close_ma60"].notna(), out["insufficient_history"]
    )
    out["pct_positive_ret_60d"], out["positive_ret_60d_count"], out["positive_ret_60d_total"] = _metric(
        day["ret_60d"] > 0, day["ret_60d"].notna(), out["insufficient_history"]
    )
    return out


def compute_margin_trend(margin: pd.DataFrame, asof: str) -> Dict[str, Any]:
    """融资余额趋势标志位。margin 为按 trade_date 升序的 trade_date/rzye_yi（亿元）。"""
    out: Dict[str, Any] = {
        "trade_date": None,
        "latest": None,
        "chg_5d_pct": None,
        "chg_20d_pct": None,
        "is_new_low_20d": None,
        "days_since_20d_low": None,
        "vs_250d_ago_pct": None,
        "insufficient_history": True,
    }
    if margin is None or margin.empty:
        return out

    df = margin.copy()
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "")
    df["rzye_yi"] = pd.to_numeric(df["rzye_yi"], errors="coerce")
    df = df.loc[df["trade_date"] <= asof].dropna(subset=["rzye_yi"]).sort_values("trade_date")
    if df.empty:
        return out

    values = df["rzye_yi"].tolist()
    dates = df["trade_date"].tolist()
    latest = float(values[-1])
    out["trade_date"] = dates[-1]
    out["latest"] = round(latest, 1)
    out["insufficient_history"] = len(values) < HIGH_WINDOW + 1

    if len(values) >= 6 and values[-6]:
        out["chg_5d_pct"] = round((latest / float(values[-6]) - 1.0) * 100.0, 2)
    if len(values) >= 21 and values[-21]:
        out["chg_20d_pct"] = round((latest / float(values[-21]) - 1.0) * 100.0, 2)
    if len(values) >= 20:
        last20 = [float(v) for v in values[-20:]]
        low = min(last20)
        out["is_new_low_20d"] = bool(latest == low)
        # 距 20 日低点多少个交易日（低点取窗口内最后一次出现，当日即低点为 0）
        out["days_since_20d_low"] = int(last20[::-1].index(low))
    if len(values) >= HIGH_WINDOW + 1 and values[-(HIGH_WINDOW + 1)]:
        out["vs_250d_ago_pct"] = round((latest / float(values[-(HIGH_WINDOW + 1)]) - 1.0) * 100.0, 2)
    return out


def summarize_sw_industries(
    frames: Dict[str, pd.DataFrame],
    names: Dict[str, str],
    asof: str,
    benchmark_frame: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """申万一级结构：逐行业 60 日收益 / 250 日高点回撤 / 高点日期 + 汇总计数。"""
    industries: List[Dict[str, Any]] = []
    for ts_code in sorted(frames):
        frame = frames[ts_code]
        metrics = compute_position_metrics(frame, asof)
        industries.append({
            "ts_code": ts_code,
            "name": names.get(ts_code),
            "ret_60d": metrics["ret_60d"],
            "drawdown_from_high_250d_pct": metrics["drawdown_from_high_250d_pct"],
            "above_ma60": metrics["above_ma60"],
            "high_250d_date": metrics["high_250d_date"],
            "insufficient_history": metrics["insufficient_history"],
        })
    benchmark_high_date = None
    if benchmark_frame is not None and not benchmark_frame.empty:
        benchmark_high_date = compute_position_metrics(benchmark_frame, asof)["high_250d_date"]
    return {
        "benchmark": BENCHMARK_INDEX,
        "benchmark_high_250d_date": benchmark_high_date,
        "count_positive_60d": sum(1 for row in industries if row["ret_60d"] is not None and row["ret_60d"] > 0),
        "count_above_ma60": sum(1 for row in industries if row["above_ma60"]),
        "industries": industries,
    }


# ---------------------------------------------------------------------------
# IO（懒加载 market_panel，离线测试不触网）
# ---------------------------------------------------------------------------
def _mp():
    import market_panel

    return market_panel


def normalize_asof(asof: Optional[str]) -> date:
    if not asof:
        return date.today()
    text = asof.strip().replace("-", "")
    return datetime.strptime(text, "%Y%m%d").date()


def fetch_index_frame(pro, ts_code: str, asof: str) -> pd.DataFrame:
    """经 market_panel.fetch_index_daily 增量补齐 stock_index_daily 缓存后取窗口。"""
    mp = _mp()
    start = (datetime.strptime(asof, "%Y%m%d") - timedelta(days=INDEX_BACKFILL_CAL_DAYS)).strftime("%Y%m%d")
    return mp.fetch_index_daily(pro, ts_code, start, asof)


def fetch_sw_l1_industries(pro) -> Tuple[List[Tuple[str, str]], str]:
    """运行时取 SW2021 一级行业清单（内存缓存），失败用内嵌常量兜底。"""
    global _SW_L1_CACHE
    if _SW_L1_CACHE:
        return _SW_L1_CACHE, "index_classify"
    try:
        df = pro.index_classify(level="L1", src="SW2021")
        codes = sorted(
            (str(row["index_code"]), str(row["industry_name"]))
            for _, row in df.iterrows()
        )
        if codes:
            _SW_L1_CACHE = codes
            return codes, "index_classify"
    except Exception as exc:
        print(f"[warn] index_classify failed, fallback to embedded SW L1 list: {exc}", file=sys.stderr)
    return list(SW_L1_FALLBACK), "embedded_fallback"


def fetch_breadth_frame(asof: str) -> pd.DataFrame:
    """从 stock_daily 取 asof 前最近若干交易日的全市场 ts_code/trade_date/close。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT trade_date FROM stock_daily
                WHERE trade_date <= %s
                ORDER BY trade_date DESC
                LIMIT %s
                """,
                (asof, BREADTH_FETCH_DAYS),
            )
            dates = [row[0] for row in cur.fetchall()]
            if not dates:
                return pd.DataFrame(columns=["ts_code", "trade_date", "close"])
            cur.execute(
                """
                SELECT ts_code, trade_date, close FROM stock_daily
                WHERE trade_date = ANY(%s)
                """,
                (dates,),
            )
            rows = cur.fetchall()
    return pd.DataFrame(rows, columns=["ts_code", "trade_date", "close"])


def _consecutive_ranges(dates: List[str]) -> List[Tuple[str, str]]:
    if not dates:
        return []
    ranges = [(dates[0], dates[0])]
    for prev, cur in zip(dates, dates[1:]):
        gap = (datetime.strptime(cur, "%Y%m%d") - datetime.strptime(prev, "%Y%m%d")).days
        if gap <= 7:
            ranges[-1] = (ranges[-1][0], cur)
        else:
            ranges.append((cur, cur))
    return ranges


def fetch_margin_series(pro, asof: str) -> pd.DataFrame:
    """回填 stock_margin（约 280 个交易日）后按 trade_date 汇总 rzye，返回亿元序列。

    覆盖判定看 rzye 是否非空而非仅看日期存在：fetch_margin_net_buy 只写
    rzmre/rzche 两个字段，它首次落库的日期 rzye 为 NULL，这里视为未覆盖并
    重新补齐全字段。窗口末尾三天每次都强制重取——最新交易日的分交易所
    数据是分批发布的，首次回填可能只有部分交易所。
    """
    mp = _mp()
    _, window = mp.fetch_trade_dates(pro, asof, MARGIN_BACKFILL_TRADING_DAYS, 0, False)
    if not window:
        return pd.DataFrame(columns=["trade_date", "rzye_yi"])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date FROM stock_margin
                WHERE trade_date BETWEEN %s AND %s
                GROUP BY trade_date
                HAVING SUM(rzye) IS NOT NULL
                """,
                (window[0], window[-1]),
            )
            have = {row[0].strftime("%Y%m%d") for row in cur.fetchall()}

    force_refresh = set(window[-3:])
    missing = [d for d in window if d not in have or d in force_refresh]
    for range_start, range_end in _consecutive_ranges(missing):
        try:
            df = pro.margin(start_date=range_start, end_date=range_end, fields=MARGIN_FIELDS)
        except Exception as exc:
            print(f"[warn] margin backfill failed for {range_start}-{range_end}: {exc}", file=sys.stderr)
            continue
        if df is None or df.empty:
            continue
        df["trade_date"] = df["trade_date"].astype(str)
        for trade_date, day_df in df.groupby("trade_date"):
            mp.write_cached_frame("margin", trade_date, day_df.reset_index(drop=True))

    cached = mp.read_dataset(
        "margin", "", "trade_date,exchange_id,rzye",
        date_column="trade_date", start_date=window[0], end_date=window[-1],
    )
    if cached is None or cached.empty:
        return pd.DataFrame(columns=["trade_date", "rzye_yi"])
    cached["rzye"] = pd.to_numeric(cached["rzye"], errors="coerce")
    # 分交易所分批发布：交易所数少于窗口众数的日期（多在最新一天）汇总口径不全，剔除
    counts = cached.groupby("trade_date")["exchange_id"].nunique()
    expected = int(counts.mode().iloc[0])
    complete = counts[counts == expected].index
    cached = cached.loc[cached["trade_date"].isin(complete)]
    series = cached.groupby("trade_date", as_index=False)["rzye"].sum()
    series["rzye_yi"] = series["rzye"] / 1e8
    return series[["trade_date", "rzye_yi"]].sort_values("trade_date").reset_index(drop=True)


def fetch_liquidity_from_history(asof: str) -> Dict[str, Any]:
    """无 evidence 时的降级路径：从 market_history 自算成交额与 20 日均额（千元 → 亿元）。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT date, amount FROM market_history
                WHERE date <= %s AND amount IS NOT NULL
                ORDER BY date DESC
                LIMIT 20
                """,
                (asof,),
            )
            rows = cur.fetchall()
    if not rows:
        return {"available": False, "reason": "market_history has no amount rows on or before asof"}
    amounts = [float(row[1]) / 1e5 for row in rows]  # 千元 → 亿元
    today = amounts[0]
    ma20 = sum(amounts) / len(amounts)
    return {
        "available": True,
        "source": "market_history",
        "trade_date": rows[0][0].strftime("%Y%m%d"),
        "amount_today_yi": round(today, 1),
        "amount_ma20_yi": round(ma20, 1),
        "ratio": round(today / ma20, 3) if ma20 else None,
        "ma20_window_days": len(amounts),
    }


def liquidity_from_evidence(evidence: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """复用 build_panel 已算好的 market_temperature / sentiment 数，不重复取数。"""
    if not evidence:
        return None
    temperature = evidence.get("market_temperature") or {}
    sentiment = (evidence.get("market_trend") or {}).get("sentiment") or {}
    today_yi = temperature.get("total_amount_100m_yuan")
    ma20_qianyuan = (sentiment.get("rolling") or {}).get("amount_ma20")
    if today_yi is None or ma20_qianyuan is None:
        return None
    ma20_yi = float(ma20_qianyuan) / 1e5  # 千元 → 亿元
    return {
        "available": True,
        "source": "evidence.market_temperature+sentiment",
        "trade_date": sentiment.get("trade_date"),
        "amount_today_yi": round(float(today_yi), 1),
        "amount_ma20_yi": round(ma20_yi, 1),
        "ratio": round(float(today_yi) / ma20_yi, 3) if ma20_yi else None,
        "ma20_window_days": 20,
    }


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def _attempt(fn) -> Dict[str, Any]:
    """子块降级守卫：异常只置该子块 unavailable，不拖垮整张卡。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)}


def build_block(asof: Optional[str] = None, evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """生成模块 1 的 market_state 证据区块（含 asof 当日数据，融资为 T-1 口径）。"""
    asof_date = normalize_asof(asof)
    asof_ymd = asof_date.strftime("%Y%m%d")
    if BACKEND == Backend.SQLITE:
        return {"available": False, "reason": "market state requires PostgreSQL backend"}
    try:
        pro = _mp().get_pro()
    except Exception as exc:
        return {"available": False, "reason": f"tushare unavailable: {exc}"}

    def _index_position() -> Dict[str, Any]:
        frames: Dict[str, pd.DataFrame] = {}
        entries = []
        for ts_code, name in INDEXES.items():
            frame = fetch_index_frame(pro, ts_code, asof_ymd)
            frames[ts_code] = frame
            metrics = compute_position_metrics(frame, asof_ymd)
            entries.append({
                "ts_code": ts_code,
                "name": name,
                **{k: metrics[k] for k in (
                    "trade_date", "close", "high_250d", "drawdown_from_high_250d_pct",
                    "ret_20d", "close_vs_ma60_pct", "above_ma60", "insufficient_history",
                )},
            })
        return {
            "available": True,
            "high_window_days": HIGH_WINDOW,
            "indexes": entries,
            "_frames": frames,
        }

    def _breadth() -> Dict[str, Any]:
        return {"available": True, **compute_breadth(fetch_breadth_frame(asof_ymd), asof_ymd)}

    def _margin() -> Dict[str, Any]:
        return {
            "available": True,
            "unit": "亿元",
            "note": "融资余额为 T-1 口径，trade_date 为实际数据日",
            **compute_margin_trend(fetch_margin_series(pro, asof_ymd), asof_ymd),
        }

    def _liquidity() -> Dict[str, Any]:
        reused = liquidity_from_evidence(evidence)
        return reused if reused is not None else fetch_liquidity_from_history(asof_ymd)

    index_position = _attempt(_index_position)
    breadth = _attempt(_breadth)
    margin_trend = _attempt(_margin)
    liquidity = _attempt(_liquidity)

    def _sw_industries() -> Dict[str, Any]:
        codes, source = fetch_sw_l1_industries(pro)
        names = dict(codes)
        frames = {ts_code: fetch_index_frame(pro, ts_code, asof_ymd) for ts_code, _ in codes}
        benchmark_frame = (index_position.get("_frames") or {}).get(BENCHMARK_INDEX)
        return {
            "available": True,
            "industry_list_source": source,
            **summarize_sw_industries(frames, names, asof_ymd, benchmark_frame),
        }

    sw_industries = _attempt(_sw_industries)
    index_position.pop("_frames", None)

    sub_blocks = {
        "index_position": index_position,
        "breadth": breadth,
        "sw_industries": sw_industries,
        "margin_trend": margin_trend,
        "liquidity": liquidity,
    }
    return {
        "available": any(block.get("available") for block in sub_blocks.values()),
        "asof": str(asof_date),
        **sub_blocks,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="市场状态定位卡：宽基位置 / 宽度 / 申万结构 / 融资趋势 / 流动性")
    ap.add_argument("--asof", default=None, help="分析日 YYYYMMDD 或 YYYY-MM-DD，默认今天；含当日数据")
    args = ap.parse_args()
    block = build_block(args.asof)
    print(json.dumps(block, ensure_ascii=False, indent=2))
    return 0 if block.get("available") else 1


if __name__ == "__main__":
    sys.exit(main())
