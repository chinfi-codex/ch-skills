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

另外输出 confirmation 子块：把手册里「调整接近尾声」的三要素各自的**阈值算术**
落成 hit true/false，第三要素的伴随条件「盈利上修」脚本无数据源，恒为
hit: null + source: external。这里只做确定性比较，不做整体定性——
「是否接近尾声」仍由模型按三要素的「且」关系判断。

数据口径：
  - 所有计算以 asof（含）为止，禁止未来数据；margin 取 <= asof 的最新 trade_date。
  - 宽基历史经 market_panel.fetch_index_daily、申万行业历史经
    market_panel.fetch_sw_daily 获取，并增量写入同一 stock_index_daily 缓存；
    首次回填约 400 个自然日（≈260 个交易日）。
  - 回撤口径为「当日收盘 vs 250 交易日区间**盘中最高**」，两端不同源是刻意的：
    高点取盘中最高才是真实的最大回撤参照。口径写在 index_position.caliber 里，
    卡面与正文都按它表述，不要换算成收盘价口径再比阈值。
  - 宽度用 **qfq 前复权** 收盘价算 MA20/MA60/60 日收益（stock_daily 存的是未复权
    原始价，6-8 月除权除息密集期会把 MA60 系统性抬高、把「站上 60 日线」压低约
    3pct，正好压在手册 50% 那条确认线的方向上）。复权因子取 stock_adj_factor，
    以 asof 当日因子为基准；缺因子的个股按未复权价参与并计入 adj_missing。
  - 融资余额回填至 stock_margin（约 280 个交易日窗口），按 trade_date 汇总
    各交易所 rzye。
  - 窗口不足时对应字段给 null 并置 insufficient_history: true，不报错。

新鲜度：每个子块输出 data_through + stale_trading_days（读数实际数据日落后
asof 几个交易日）。这条不是装饰——sw_daily 是权限接口，token 掉权限后 fetch
静默失败、compute 继续吃 PG 里的旧缓存，"60 日收益为正 1/31" 会以今天的名义
写进研报而没有任何异常。融资本身是 T-1，容忍 1 个交易日；其余子块容忍 0。
超出容忍即 stale: true，模板必须把数据日写进正文。

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

# 分层是手册的核心表述（"权重护盘、成长出清"），机器侧就要把归属固定下来，
# 而不是让模板每天靠指数名字重新分一次。
INDEX_GROUPS = {
    "000001.SH": "weight",
    "000300.SH": "weight",
    "000905.SH": "growth_small",
    "000852.SH": "growth_small",
    "399006.SZ": "growth_small",
    "000688.SH": "growth_small",
}
GROUP_LABELS = {"weight": "权重", "growth_small": "成长小盘"}

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

DRAWDOWN_CALIBER = "close_vs_intraday_high_250d"
DRAWDOWN_TIERS = ((10.0, "调整"), (20.0, "深度调整"), (float("inf"), "接近技术性熊市"))

# 手册 market_state_framework.md「状态确认三要素」的阈值。改这里必须同步改手册，
# 反过来也一样——阈值只有一处定义，卡面直接显示它，不让模板每天重述一遍。
BREADTH_CONFIRM_PCT = 50.0
BREADTH_LOW_PCT = 17.0
INDUSTRY_CONFIRM_COUNT = 10

# 新鲜度容忍（交易日）：融资是 T-1 口径；申万行业走 AKShare 的申万宏源源，
# 官方发布本身滞后一个交易日（盘后跑 D 日最多拿到 D-1），两者都按 1 放行、
# 超出才报 stale。其余子块用当日数据，0 容忍。
STALE_TOLERANCE = {"index_position": 0, "breadth": 0, "sw_industries": 1, "margin_trend": 1, "liquidity": 0}

_SW_L1_CACHE: Optional[List[Tuple[str, str]]] = None


def drawdown_tier(pct: Optional[float]) -> Optional[str]:
    """回撤量级分层。pct 为负的回撤百分比（-8.42 表示回撤 8.42%）。"""
    if pct is None:
        return None
    depth = abs(float(pct))
    for bound, label in DRAWDOWN_TIERS:
        if depth < bound:
            return label
    return DRAWDOWN_TIERS[-1][1]


# ---------------------------------------------------------------------------
# 纯计算（IO 与计算分离，测试直接注入合成 DataFrame）
# ---------------------------------------------------------------------------
def stamp_freshness(
    block: Dict[str, Any],
    data_through: Optional[str],
    asof: str,
    trading_days: Optional[List[str]],
    tolerance: int = 0,
) -> Dict[str, Any]:
    """给子块盖数据日与滞后天数，超出容忍即 stale: true。

    滞后按交易日算（trading_days 为升序的交易日历，含 asof）；日历不可用时
    退回自然日并标 basis。data_through 缺失本身就是 stale——一个说不出自己
    数据日的读数不该被当成当日读数使用。
    """
    block["data_through"] = data_through
    if not data_through:
        block["stale"] = True
        block["stale_reason"] = "block reports no data_through"
        block["stale_trading_days"] = None
        return block
    if trading_days and data_through in trading_days and asof in trading_days:
        lag = trading_days.index(asof) - trading_days.index(data_through)
        basis = "trading_days"
    else:
        lag = (datetime.strptime(asof, "%Y%m%d") - datetime.strptime(data_through, "%Y%m%d")).days
        basis = "calendar_days"
    block["stale_trading_days"] = lag
    block["stale_basis"] = basis
    block["stale"] = lag > tolerance
    if block["stale"]:
        block["stale_reason"] = f"data_through={data_through} lags asof={asof} by {lag} {basis}"
    return block


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


def apply_breadth_qfq(daily: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """把 stock_daily 的未复权 close 换算成以窗口末日为基准的前复权价。

    为什么必须复权：MA20 / MA60 / 60 日收益都是跨日比价，而 stock_daily 存的
    是 Tushare `daily` 的原始价。除权除息当天原始价直接跳空下移，均线却还挂在
    除权前的价位上，于是「站上 60 日线」被系统性低估——方向是单边的，因为
    除权只会向下。2026-08-06 实测：未复权 26.14% vs 前复权 29.64%，差 3.5pct，
    而手册判「宽度修复」的确认线正是 50%，低估的方向恰好压在这条线上。

    daily 需含 adj_factor 列（缺列即视为拿不到因子，原样返回并说明原因）。
    """
    meta: Dict[str, Any] = {"price_adjustment": "qfq", "adj_missing": 0}
    if daily is None or daily.empty or "adj_factor" not in daily.columns:
        meta["price_adjustment"] = "none"
        meta["reason"] = "adj_factor column absent"
        return daily, meta

    df = daily.copy()
    df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce")
    base = (
        df.dropna(subset=["adj_factor"])
        .sort_values("trade_date")
        .groupby("ts_code")["adj_factor"]
        .last()
        .rename("base_adj_factor")
    )
    if base.empty:
        meta["price_adjustment"] = "none"
        meta["reason"] = "no usable adj_factor rows in window"
        return daily.drop(columns=["adj_factor"]), meta

    df = df.join(base, on="ts_code")
    ratio = df["adj_factor"] / df["base_adj_factor"]
    usable = ratio.notna() & (ratio > 0)
    meta["adj_missing"] = int((~usable).sum())
    meta["adj_coverage"] = round(float(usable.mean()), 4)
    df.loc[usable, "close"] = df.loc[usable, "close"] * ratio[usable]
    return df.drop(columns=["adj_factor", "base_adj_factor"]), meta


def compute_breadth(daily: pd.DataFrame, asof: str) -> Dict[str, Any]:
    """全市场宽度：站上 20/60 日线与 60 日收益为正的个股占比。

    daily 为全市场个股日线（ts_code/trade_date/close，可选 adj_factor），
    只取 <= asof。分母为最新交易日有交易的个股；个股历史不足导致指标为 NaN
    的，从该指标分母剔除。总窗口不足 61 个交易日时 60 日口径指标给 null。

    同时给出各比例相对**上一交易日**的变化（`*_delta_1d`）：状态卡要回答的是
    「修复还是恶化」，只给一个静态百分比读不出方向。
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
        "pct_above_ma20_delta_1d": None,
        "pct_above_ma60_delta_1d": None,
        "pct_positive_ret_60d_delta_1d": None,
        "prev_trade_date": None,
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
    df, adj_meta = apply_breadth_qfq(df)
    out.update(adj_meta)
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    distinct_days = int(df["trade_date"].nunique())
    out["insufficient_history"] = distinct_days < BREADTH_MIN_DAYS

    grouped = df.groupby("ts_code", group_keys=False)["close"]
    df["close_ma20"] = grouped.transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["close_ma60"] = grouped.transform(lambda s: s.rolling(60, min_periods=60).mean())
    df["ret_60d"] = grouped.pct_change(60) * 100.0

    def _metric(hit_mask: pd.Series, valid_mask: pd.Series, gated: bool):
        valid_total = int(valid_mask.sum())
        if gated or valid_total == 0:
            return None, None, None
        count = int((hit_mask & valid_mask).sum())
        return round(100.0 * count / valid_total, 2), count, valid_total

    def _day_ratios(day: pd.DataFrame) -> Dict[str, Any]:
        return {
            "ma20": _metric(day["close"] > day["close_ma20"], day["close_ma20"].notna(), distinct_days < 20),
            "ma60": _metric(day["close"] > day["close_ma60"], day["close_ma60"].notna(), out["insufficient_history"]),
            "ret60": _metric(day["ret_60d"] > 0, day["ret_60d"].notna(), out["insufficient_history"]),
        }

    dates = sorted(df["trade_date"].unique())
    day = df.loc[df["trade_date"] == dates[-1]]
    out["trade_date"] = str(dates[-1])
    out["total"] = int(len(day))
    today = _day_ratios(day)
    out["pct_above_ma20"], out["above_ma20_count"], out["above_ma20_total"] = today["ma20"]
    out["pct_above_ma60"], out["above_ma60_count"], out["above_ma60_total"] = today["ma60"]
    out["pct_positive_ret_60d"], out["positive_ret_60d_count"], out["positive_ret_60d_total"] = today["ret60"]

    # 上一交易日同口径重算；窗口边界处 MA60 依赖的历史比今日少一天，
    # 但比例的分母各自独立，做差仍是同口径对比。
    if len(dates) >= 2:
        prev = df.loc[df["trade_date"] == dates[-2]]
        out["prev_trade_date"] = str(dates[-2])
        yesterday = _day_ratios(prev)
        for key, field in (("ma20", "pct_above_ma20"), ("ma60", "pct_above_ma60"), ("ret60", "pct_positive_ret_60d")):
            now, before = today[key][0], yesterday[key][0]
            if now is not None and before is not None:
                out[f"{field}_delta_1d"] = round(now - before, 2)
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
    """申万一级结构：逐行业 60 日收益 / 250 日高点回撤 / 高点日期 + 汇总计数。

    每个行业带上自己的 trade_date，块级给 data_through 取其中最早的一天：
    sw_daily 是权限接口，掉权限后 fetch 静默失败、compute 继续吃旧缓存，
    没有数据日就没人看得出「1/31」其实是上周的读数。

    两个计数分别给出有效分母（`*_total`）。旧版 count_above_ma60 把
    above_ma60=None（历史不足）与 False（在均线下方）算成同一类，
    再按 /31 展示——缺数和利空被混成了一个数。
    """
    industries: List[Dict[str, Any]] = []
    for ts_code in sorted(frames):
        frame = frames[ts_code]
        metrics = compute_position_metrics(frame, asof)
        industries.append({
            "ts_code": ts_code,
            "name": names.get(ts_code),
            "trade_date": metrics["trade_date"],
            "ret_60d": metrics["ret_60d"],
            "drawdown_from_high_250d_pct": metrics["drawdown_from_high_250d_pct"],
            "above_ma60": metrics["above_ma60"],
            "high_250d_date": metrics["high_250d_date"],
            "insufficient_history": metrics["insufficient_history"],
        })
    benchmark_high_date = None
    if benchmark_frame is not None and not benchmark_frame.empty:
        benchmark_high_date = compute_position_metrics(benchmark_frame, asof)["high_250d_date"]
    dates = [row["trade_date"] for row in industries if row["trade_date"]]
    positive_valid = [row for row in industries if row["ret_60d"] is not None]
    ma60_valid = [row for row in industries if row["above_ma60"] is not None]
    return {
        "benchmark": BENCHMARK_INDEX,
        "benchmark_high_250d_date": benchmark_high_date,
        "count_positive_60d": sum(1 for row in positive_valid if row["ret_60d"] > 0),
        "count_positive_60d_total": len(positive_valid),
        "count_above_ma60": sum(1 for row in ma60_valid if row["above_ma60"]),
        "count_above_ma60_total": len(ma60_valid),
        "latest_trade_date": max(dates) if dates else None,
        "oldest_trade_date": min(dates) if dates else None,
        "industries": industries,
    }


def build_sw_industries_block(
    frames: Dict[str, pd.DataFrame],
    names: Dict[str, str],
    asof: str,
    benchmark_frame: Optional[pd.DataFrame],
    source: str,
    prev_asof: Optional[str] = None,
) -> Dict[str, Any]:
    """Mark the block unavailable when the dedicated SW endpoint yields no data."""
    available_count = sum(
        1 for frame in frames.values() if frame is not None and not frame.empty
    )
    if available_count == 0:
        return {
            "available": False,
            "reason": "sw_daily returned no industry history",
            "industry_list_source": source,
        }
    summary = summarize_sw_industries(frames, names, asof, benchmark_frame)
    block = {
        "available": True,
        "industry_list_source": source,
        "data_available_count": available_count,
        "data_missing_count": len(frames) - available_count,
        **summary,
    }
    # 扩散的方向比它的水平更有信息量：1/31 是在爬还是在退，决定了这条要素
    # 该写「修复进行中」还是「继续收缩」。上一交易日在同一批 frame 上重算。
    if prev_asof:
        prev = summarize_sw_industries(frames, names, prev_asof, None)
        block["count_positive_60d_prev"] = prev["count_positive_60d"]
        block["count_above_ma60_prev"] = prev["count_above_ma60"]
        block["prev_trade_date"] = prev["latest_trade_date"]
    return block


def build_confirmation(
    breadth: Dict[str, Any],
    sw_industries: Dict[str, Any],
    margin_trend: Dict[str, Any],
) -> Dict[str, Any]:
    """把手册「状态确认三要素」的阈值算术落成 hit，不做整体定性。

    每条只回答「这条阈值今天过没过」——纯比较，模型无须重算。整体是否
    「调整接近尾声」是三要素的「且」，仍由模型判断并解释；第三要素的伴随
    条件「盈利上修」脚本没有数据源，恒为 hit: null + source: external，
    模型必须引外部证据或写明缺这条腿。
    """
    checks: List[Dict[str, Any]] = []

    margin_ok = margin_trend.get("available") and margin_trend.get("is_new_low_20d") is not None
    days_since = margin_trend.get("days_since_20d_low")
    checks.append({
        "key": "margin_stop_new_low",
        "name": "融资停止创新低",
        "threshold": "is_new_low_20d = false",
        "reading": (
            f"{margin_trend.get('latest')} 亿｜5 日 {margin_trend.get('chg_5d_pct')}%｜"
            + ("当日即 20 日新低" if margin_trend.get("is_new_low_20d") else f"距 20 日低点 {days_since} 日")
            if margin_ok else None
        ),
        "hit": (not margin_trend.get("is_new_low_20d")) if margin_ok else None,
        "source": "script",
        "as_of": margin_trend.get("trade_date"),
    })

    pct60 = breadth.get("pct_above_ma60") if breadth.get("available") else None
    checks.append({
        "key": "breadth_recovery",
        "name": f"宽度回到 {BREADTH_CONFIRM_PCT:.0f}% 以上",
        "threshold": f"pct_above_ma60 >= {BREADTH_CONFIRM_PCT:.0f}",
        "reading": (
            f"站上 60 日线 {pct60}%"
            + (f"（较前日 {breadth.get('pct_above_ma60_delta_1d'):+.2f}pct）"
               if breadth.get("pct_above_ma60_delta_1d") is not None else "")
            if pct60 is not None else None
        ),
        "hit": (pct60 >= BREADTH_CONFIRM_PCT) if pct60 is not None else None,
        "source": "script",
        "as_of": breadth.get("trade_date"),
    })

    count_pos = sw_industries.get("count_positive_60d") if sw_industries.get("available") else None
    total = sw_industries.get("count_positive_60d_total")
    prev_pos = sw_industries.get("count_positive_60d_prev")
    checks.append({
        "key": "industry_diffusion",
        "name": f"行业扩散 ≥{INDUSTRY_CONFIRM_COUNT} 个 60 日收益为正",
        "threshold": f"count_positive_60d >= {INDUSTRY_CONFIRM_COUNT}",
        "reading": (
            f"{count_pos}/{total} 个行业为正"
            + (f"（前一交易日 {prev_pos}）" if prev_pos is not None else "")
            if count_pos is not None else None
        ),
        "hit": (count_pos >= INDUSTRY_CONFIRM_COUNT) if count_pos is not None else None,
        "source": "script",
        "as_of": sw_industries.get("data_through") or sw_industries.get("latest_trade_date"),
    })

    checks.append({
        "key": "earnings_revision",
        "name": "盈利上修（第三要素的伴随条件）",
        "threshold": "外部证据（业绩预告 / 券商一致预期）",
        "reading": None,
        "hit": None,
        "source": "external",
        "as_of": None,
    })

    scriptable = [c for c in checks if c["source"] == "script"]
    return {
        "available": True,
        "framework": "references/methodology/market_state_framework.md",
        "relation": "and",
        "checks": checks,
        "hits": sum(1 for c in scriptable if c["hit"] is True),
        "scriptable": len(scriptable),
        "undetermined": sum(1 for c in checks if c["hit"] is None),
        "note": "三要素为「且」关系；hit=null 表示脚本无数据源，须由模型引外部证据。是否「接近尾声」由模型判断，本块不给结论。",
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


def fetch_sw_frame(pro, ts_code: str, asof: str) -> pd.DataFrame:
    """经 Tushare sw_daily 增量补齐 stock_index_daily 缓存后取窗口。"""
    mp = _mp()
    start = (datetime.strptime(asof, "%Y%m%d") - timedelta(days=INDEX_BACKFILL_CAL_DAYS)).strftime("%Y%m%d")
    return mp.fetch_sw_daily(pro, ts_code, start, asof)


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
    """从 stock_daily 取 asof 前最近若干交易日的全市场 ts_code/trade_date/close。

    左连 stock_adj_factor 带出复权因子（compute_breadth 里做 qfq 换算）。
    用 LEFT JOIN 是刻意的：因子缺失只该让那只票退回未复权价，不该把它从
    宽度分母里整只删掉——那会悄悄改变分母口径。
    """
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
                return pd.DataFrame(columns=["ts_code", "trade_date", "close", "adj_factor"])
            cur.execute(
                """
                SELECT d.ts_code, d.trade_date, d.close, a.adj_factor
                FROM stock_daily d
                LEFT JOIN stock_adj_factor a
                  ON a.ts_code = d.ts_code AND a.trade_date = d.trade_date
                WHERE d.trade_date = ANY(%s)
                """,
                (dates,),
            )
            rows = cur.fetchall()
    return pd.DataFrame(rows, columns=["ts_code", "trade_date", "close", "adj_factor"])


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

    # 交易日历只为「滞后几个交易日」这一个用途取一次；拿不到就退回自然日，
    # 不因为日历不可用而放弃新鲜度检查。
    try:
        _, calendar = _mp().fetch_trade_dates(pro, asof_ymd, 30, 0, False)
    except Exception:  # noqa: BLE001
        calendar = None
    prev_trading_day = calendar[-2] if calendar and len(calendar) >= 2 else None

    def _index_position() -> Dict[str, Any]:
        frames: Dict[str, pd.DataFrame] = {}
        entries = []
        for ts_code, name in INDEXES.items():
            frame = fetch_index_frame(pro, ts_code, asof_ymd)
            frames[ts_code] = frame
            metrics = compute_position_metrics(frame, asof_ymd)
            drawdown = metrics["drawdown_from_high_250d_pct"]
            prev_drawdown = (
                compute_position_metrics(frame, prev_trading_day)["drawdown_from_high_250d_pct"]
                if prev_trading_day else None
            )
            entries.append({
                "ts_code": ts_code,
                "name": name,
                "group": INDEX_GROUPS.get(ts_code),
                "group_label": GROUP_LABELS.get(INDEX_GROUPS.get(ts_code, "")),
                **{k: metrics[k] for k in (
                    "trade_date", "close", "high_250d", "high_250d_date",
                    "drawdown_from_high_250d_pct",
                    "ret_20d", "close_vs_ma60_pct", "above_ma60", "insufficient_history",
                )},
                "tier": drawdown_tier(drawdown),
                "drawdown_delta_1d": (
                    round(drawdown - prev_drawdown, 2)
                    if drawdown is not None and prev_drawdown is not None else None
                ),
            })
        dates = [e["trade_date"] for e in entries if e["trade_date"]]
        return {
            "available": True,
            "high_window_days": HIGH_WINDOW,
            "caliber": DRAWDOWN_CALIBER,
            "caliber_note": "回撤 = 当日收盘 / 250 交易日区间盘中最高 - 1；高点取盘中最高，故略深于收盘价口径",
            "tier_bounds_pct": [bound for bound, _ in DRAWDOWN_TIERS[:-1]],
            "tier_labels": [label for _, label in DRAWDOWN_TIERS],
            "groups": GROUP_LABELS,
            "indexes": entries,
            "latest_trade_date": max(dates) if dates else None,
            "oldest_trade_date": min(dates) if dates else None,
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
        frames = {ts_code: fetch_sw_frame(pro, ts_code, asof_ymd) for ts_code, _ in codes}
        benchmark_frame = (index_position.get("_frames") or {}).get(BENCHMARK_INDEX)
        return build_sw_industries_block(
            frames, names, asof_ymd, benchmark_frame, source, prev_asof=prev_trading_day
        )

    sw_industries = _attempt(_sw_industries)
    index_position.pop("_frames", None)

    sub_blocks = {
        "index_position": index_position,
        "breadth": breadth,
        "sw_industries": sw_industries,
        "margin_trend": margin_trend,
        "liquidity": liquidity,
    }
    # 每个子块自报数据日与滞后；不可用的子块跳过（它已经有 reason 了）。
    data_through_field = {
        "index_position": "latest_trade_date",
        "breadth": "trade_date",
        "sw_industries": "oldest_trade_date",   # 31 个行业里最旧的一天才是这个计数真正的口径日
        "margin_trend": "trade_date",
        "liquidity": "trade_date",
    }
    for key, block in sub_blocks.items():
        if block.get("available"):
            stamp_freshness(
                block, block.get(data_through_field[key]), asof_ymd, calendar,
                tolerance=STALE_TOLERANCE[key],
            )

    confirmation = _attempt(lambda: build_confirmation(breadth, sw_industries, margin_trend))
    stale_blocks = [key for key, block in sub_blocks.items() if block.get("stale")]
    return {
        "available": any(block.get("available") for block in sub_blocks.values()),
        "asof": str(asof_date),
        "stale_blocks": stale_blocks,
        **sub_blocks,
        "confirmation": confirmation,
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
