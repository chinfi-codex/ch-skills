#!/usr/bin/env python3
"""市场风格因子（style_factors）——模块 1「市场风格」小节的机判读数。

**来源。** 计算与呈现口径移植自 skillhub `china-market-style-factors` v1.0.0
（references/methodology.md + scripts/analyze.py）：四个风格比值 + 两个市场环境指标，
四个观察窗口，发布后无前视历史分位，全收益敏感性核验与规模稳健性对照。原版纯 AkShare
取数；这里换成 DMS 已有的数据源——Tushare `index_daily`（经 PG `stock_index_daily`
缓存）为主，Baostock 为整序列兜底，二者都不可用时该序列判缺失。

**六个指标（公式与原版逐字一致）。**

  1. 规模       Q = √(小盘成长 × 小盘价值) / √(大盘成长 × 大盘价值)       正向 = 小盘占优
  2. 成长／价值 Q = [(大成/大价) × (中成/中价) × (小成/小价)]^(1/3)          正向 = 成长占优
  3. 小市值扩散 Q = 国证2000 / 国证1000                                    正向 = 向更小市值扩散
  4. 红利偏好   Q = 中证红利价格 / 中证全指价格                              正向 = 红利价格领先
  5. 市场趋势   T = (全指收盘 / 过去 60 个收盘均值 − 1) × 100（含当日）       单位 %
  6. 市场波动   V = std(全指最近 20 个日收益, ddof=1) × √252 × 100          单位 年化%

前四项先按权重加总对数价格得 L；显示值 100 × exp(L_t − L_窗口首日)，N 日相对变化
100 × [exp(L_t − L_{t−N}) − 1]——比值涨跌不是两组简单收益率相减（分子 +10%、分母
+5%，相对变化是 4.7619%，不是 5 个百分点）。

**纪律（照搬原版，违反即判该序列不可用，不填平、不拼接）。**

- 逐序列核验：日期不重复、收盘为正、最后交易日 == asof、起点到 asof 零缺失交易日、
  核心序列至少 756 个观测、起点不晚于「最晚允许起点」（识别静默截断）。
- 整序列换源可以（Tushare → Baostock），拼接不可以；换源写进 `qa[].source/attempts`。
- 历史分位只与当日之前的样本比，排除指数发布前回溯段及其预热不足的窗口，少于 252 个
  历史样本输出空缺。
- 价格指数不含分红。六条国证风格全收益齐全时另做规模与成长价值的敏感性核验；缺一条
  整组停用并披露。**DMS 扩展**：Tushare 另有中证红利全收益 H00922 与中证全指全收益
  H00985，红利偏好因此也能做全收益核验（原版做不到）；H00985 缺发布当日 2011-08-02
  一日，核验区间从 2011-08-03 起，写在 `valid_from` 与 QA 里。

本模块只出确定性读数与规则标签，不给结论；`build_block` 的取数函数由调用方注入
（`market_panel` 提供 PG 缓存 + Tushare/Baostock），单测用假数据即可覆盖全部计算。

用法（独立调试）：
  python scripts/style_factors.py --asof 20260924
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

SOURCE_NOTE = "移植自 skillhub china-market-style-factors v1.0.0；数据源为 Tushare index_daily（PG 缓存）+ Baostock 整序列兜底"

# --------------------------------------------------------------------------- #
# 序列注册表。键沿用原版的符号（sz399372…），权重表可以原样对照。
#   fetch_start  已知首个数据日；请求从这里起，避免缓存左边界每天被当成缺口重拉
#   latest_start 最晚允许起点（原版 methodology.md），晚于它视为静默截断
#   valid_from   只从这天起要求零缺失（H00985 缺发布当日一日）
# --------------------------------------------------------------------------- #
CORE_SERIES: Dict[str, Dict[str, Any]] = {
    "sz399372": {"name": "国证大盘成长", "ts_code": "399372.SZ", "bs_code": "sz.399372", "fetch_start": "20021231", "latest_start": "20030102"},
    "sz399373": {"name": "国证大盘价值", "ts_code": "399373.SZ", "bs_code": "sz.399373", "fetch_start": "20021231", "latest_start": "20030102"},
    "sz399374": {"name": "国证中盘成长", "ts_code": "399374.SZ", "bs_code": "sz.399374", "fetch_start": "20021231", "latest_start": "20030102"},
    "sz399375": {"name": "国证中盘价值", "ts_code": "399375.SZ", "bs_code": "sz.399375", "fetch_start": "20021231", "latest_start": "20030102"},
    "sz399376": {"name": "国证小盘成长", "ts_code": "399376.SZ", "bs_code": "sz.399376", "fetch_start": "20021231", "latest_start": "20030102"},
    "sz399377": {"name": "国证小盘价值", "ts_code": "399377.SZ", "bs_code": "sz.399377", "fetch_start": "20021231", "latest_start": "20030102"},
    "sz399303": {"name": "国证2000", "ts_code": "399303.SZ", "bs_code": "sz.399303", "fetch_start": "20091231", "latest_start": "20100104"},
    "sz399311": {"name": "国证1000", "ts_code": "399311.SZ", "bs_code": "sz.399311", "fetch_start": "20021231", "latest_start": "20030102"},
    "sh000922": {"name": "中证红利", "ts_code": "000922.CSI", "bs_code": "sh.000922", "fetch_start": "20041231", "latest_start": "20050104"},
    # Baostock 指数字典没有中证全指，只能走 Tushare
    "sh000985": {"name": "中证全指", "ts_code": "000985.CSI", "bs_code": None, "fetch_start": "20041231", "latest_start": "20050104"},
    "sh000852": {"name": "中证1000", "ts_code": "000852.SH", "bs_code": "sh.000852", "fetch_start": "20041231", "latest_start": "20141017"},
    "sh000300": {"name": "沪深300", "ts_code": "000300.SH", "bs_code": "sh.000300", "fetch_start": "20020104", "latest_start": "20050408"},
}
TOTAL_RETURN_SERIES: Dict[str, Dict[str, Any]] = {
    "cn2372": {"name": "大盘成长R", "ts_code": "CN2372.CNI", "fetch_start": "20021231", "group": "size_growth", "price_key": "sz399372"},
    "cn2373": {"name": "大盘价值R", "ts_code": "CN2373.CNI", "fetch_start": "20021231", "group": "size_growth", "price_key": "sz399373"},
    "cn2374": {"name": "中盘成长R", "ts_code": "CN2374.CNI", "fetch_start": "20021231", "group": "size_growth", "price_key": "sz399374"},
    "cn2375": {"name": "中盘价值R", "ts_code": "CN2375.CNI", "fetch_start": "20021231", "group": "size_growth", "price_key": "sz399375"},
    "cn2376": {"name": "小盘成长R", "ts_code": "CN2376.CNI", "fetch_start": "20021231", "group": "size_growth", "price_key": "sz399376"},
    "cn2377": {"name": "小盘价值R", "ts_code": "CN2377.CNI", "fetch_start": "20021231", "group": "size_growth", "price_key": "sz399377"},
    # DMS 扩展：原版红利不具备全收益核验
    "h00922": {"name": "中证红利全收益", "ts_code": "H00922.CSI", "fetch_start": "20041231", "group": "dividend", "price_key": "sh000922"},
    "h00985": {"name": "中证全指全收益", "ts_code": "H00985.CSI", "fetch_start": "20041231", "group": "dividend", "price_key": "sh000985",
               "valid_from": "20110803", "valid_note": "H00985 缺发布当日 2011-08-02 一日，核验区间从 2011-08-03 起"},
}
MIN_CORE_OBS = 756

STYLE_KEYS = ("size", "growth", "small_tail", "dividend")
ENV_KEYS = ("market_trend", "volatility")
FACTOR_KEYS = STYLE_KEYS + ENV_KEYS

WEIGHTS: Dict[str, Dict[str, float]] = {
    "size": {"sz399376": 0.5, "sz399377": 0.5, "sz399372": -0.5, "sz399373": -0.5},
    "growth": {"sz399372": 1 / 3, "sz399374": 1 / 3, "sz399376": 1 / 3,
               "sz399373": -1 / 3, "sz399375": -1 / 3, "sz399377": -1 / 3},
    "small_tail": {"sz399303": 1.0, "sz399311": -1.0},
    "dividend": {"sh000922": 1.0, "sh000985": -1.0},
}
# 全收益核验用同一套权重，只换成对应的全收益序列
TR_WEIGHTS: Dict[str, Dict[str, float]] = {
    "size": {"cn2376": 0.5, "cn2377": 0.5, "cn2372": -0.5, "cn2373": -0.5},
    "growth": {"cn2372": 1 / 3, "cn2374": 1 / 3, "cn2376": 1 / 3,
               "cn2373": -1 / 3, "cn2375": -1 / 3, "cn2377": -1 / 3},
    "dividend": {"h00922": 1.0, "h00985": -1.0},
}

META: Dict[str, Dict[str, str]] = {
    "size": {
        "name": "规模风格", "positive": "小盘占优", "negative": "大盘占优", "release": "20100104",
        "formula": "Q = √(小盘成长 × 小盘价值) / √(大盘成长 × 大盘价值)",
        "description": "分别在成长和价值内部比较小盘与大盘，再等权合成，降低成长/价值混杂；仍未做行业或 Beta 中性化。",
        "unit": "相对强弱（基期=100）",
    },
    "growth": {
        "name": "成长／价值", "positive": "成长占优", "negative": "价值占优", "release": "20100104",
        "formula": "Q = [(大盘成长/大盘价值) × (中盘成长/中盘价值) × (小盘成长/小盘价值)]^(1/3)",
        "description": "在大、中、小盘内分别比较成长与价值，再等权合成，降低规模结构干扰；成长按官方基本面风格分类，不等于科技行业。",
        "unit": "相对强弱（基期=100）",
    },
    "small_tail": {
        "name": "小市值扩散", "positive": "向更小市值扩散", "negative": "偏向较大市值", "release": "20140328",
        "formula": "Q = 国证2000 / 国证1000",
        "description": "国证2000覆盖总市值排名前1000之外的2000只合格证券，衡量行情是否向更小公司扩散；它不等于最小市值微盘组合。",
        "unit": "相对强弱（基期=100）",
    },
    "dividend": {
        "name": "红利偏好", "positive": "红利价格占优", "negative": "红利价格落后", "release": "20110802",
        "formula": "Q = 中证红利价格指数 / 中证全指价格指数",
        "description": "反映高股息股票相对市场的股价风格，不含现金分红再投资。除息会机械压低信号，尤其不可用该曲线判断红利长期投资回报。行业、价值和波动暴露未剥离。",
        "unit": "相对强弱（基期=100）",
    },
    "market_trend": {
        "name": "市场趋势", "positive": "高于60日均线", "negative": "低于60日均线", "release": "20110802",
        "formula": "T = (中证全指收盘价 / 过去60个交易日收盘均值 − 1) × 100%",
        "description": "市场整体的时间序列趋势指标，不是“买赢家、卖输家”的横截面动量收益因子。",
        "unit": "相对60日均线偏离（%）",
    },
    "volatility": {
        "name": "市场波动", "positive": "波动较高", "negative": "波动较低", "release": "20110802",
        "formula": "V = std(中证全指过去20个交易日简单收益率，ddof=1) × √252 × 100%",
        "description": "刻画整体市场风险环境，不是低波动股票减高波动股票的多空因子。高波动不直接等于看空。",
        "unit": "20日年化波动率（%）",
    },
}
SIDE_NAMES = {
    "size": ("小盘", "大盘"),
    "growth": ("成长", "价值"),
    "small_tail": ("更小市值", "较大市值"),
    "dividend": ("红利领先", "红利落后"),
}
REL_HORIZONS = (1, 5, 10, 20, 60, 120, 252)
TR_HORIZONS = (10, 20, 60, 120)
CELL_HORIZONS = (20, 60, 120)
PERCENTILE_MIN = 252
VOL_HIGH_PCT = 80.0
VOL_LOW_PCT = 20.0

# 四个观察窗口：三年/一年按日历年回溯，60 日是 60 个收益区间（61 个收盘）
VIEW_DEFS = (
    ("all", "全历史"),
    ("3y", "最近三年"),
    ("1y", "最近一年"),
    ("60d", "最近60个交易日"),
)

COVERAGE_NOTE = "沪深跨市场风格代理；国证2000现行样本空间含北交所，其他指数依各自规则。非沪深京每一只股票等权扫描。"
BOUNDARY_NOTES = [
    "这是跨市场指数风格代理，不是逐股重建的纯 SMB/HML/UMD，不能称作无幸存者偏差的纯因子回测。",
    "未做行业与 Beta 中性化，四个风格因子互有相关性；相对领先不等于资金流入，也不是经过回测验证的交易信号。",
    "价格指数不含现金分红；不得将价格与全收益历史拼接，也不能用红利价格曲线宣称红利投资总回报。",
    "风格相对曲线不是可交易的多空策略净值，不据此承诺胜率或收益。",
    "发布前回溯段保留在曲线里，只用文字说明；历史分位排除这些观测及其预热不足的窗口。",
]

Loader = Callable[[str, str, str], Optional[pd.DataFrame]]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _num(value: Any, digits: int = 4) -> Optional[float]:
    """JSON 安全的数值：NaN/inf 一律写 None，不能让 NaN 进 evidence。"""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return round(out, digits)


def _ymd(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y%m%d")


def _iso(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d")


def _to_calendar(trade_dates: Iterable[str]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(sorted({str(d) for d in trade_dates}), format="%Y%m%d"))


# --------------------------------------------------------------------------- #
# 逐序列核验
# --------------------------------------------------------------------------- #
def validate_series(
    frame: Optional[pd.DataFrame],
    calendar: pd.DatetimeIndex,
    asof: pd.Timestamp,
    spec: Mapping[str, Any],
    core: bool,
) -> Tuple[Optional[pd.Series], Dict[str, Any]]:
    """原版 analyze.read + 逐条质量检查。返回 (收盘序列 | None, 质检记录)。

    接口返回成功不等于数据可用：日期重复、非正价格、末日不是 asof、区间内缺交易日、
    观测太少、起点晚于最晚允许起点，任何一条都判失败。不填平、不截掉早期历史。
    """
    qa: Dict[str, Any] = {"ok": False}
    if frame is None or frame.empty or "trade_date" not in frame.columns or "close" not in frame.columns:
        qa["error"] = "no data returned"
        return None, qa
    df = frame[["trade_date", "close"]].copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str).str.replace("-", "").str[:8], format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["trade_date"])
    df = df.loc[df["trade_date"] <= asof]
    if df.empty:
        qa["error"] = "no rows on or before asof"
        return None, qa
    if df["trade_date"].duplicated().any():
        qa["error"] = "duplicate dates"
        return None, qa
    series = pd.to_numeric(df.set_index("trade_date")["close"], errors="coerce").astype(float).sort_index()
    if series.isna().any() or (series <= 0).any():
        qa["error"] = "invalid prices (NaN or non-positive close)"
        return None, qa

    start, end = series.index.min(), series.index.max()
    qa.update({"start": _iso(start), "end": _iso(end), "rows": int(len(series))})
    check_from = start
    valid_from = spec.get("valid_from")
    if valid_from:
        check_from = max(start, pd.Timestamp(valid_from))
        qa["valid_from"] = _iso(pd.Timestamp(valid_from))
        qa["valid_note"] = spec.get("valid_note")
    expected = calendar[(calendar >= check_from) & (calendar <= asof)]
    missing = expected.difference(series.index)
    qa["missing_trade_days"] = int(len(missing))
    if len(missing):
        qa["missing_sample"] = [_iso(d) for d in missing[:5]]
    returns = series.pct_change(fill_method=None).abs()
    qa["max_abs_daily_return_pct"] = _num(returns.max() * 100, 2)

    if end != asof:
        qa["error"] = f"last trade date {_iso(end)} != asof {_iso(asof)}"
        return None, qa
    if len(missing):
        qa["error"] = f"{len(missing)} missing trade days since {_iso(check_from)}"
        return None, qa
    if core:
        if len(series) < MIN_CORE_OBS:
            qa["error"] = f"only {len(series)} observations (< {MIN_CORE_OBS})"
            return None, qa
        latest_start = spec.get("latest_start")
        if latest_start and start > pd.Timestamp(latest_start):
            qa["error"] = f"history starts {_iso(start)}, later than {_iso(pd.Timestamp(latest_start))} (silent truncation)"
            return None, qa
    if valid_from:
        series = series.loc[series.index >= check_from]
    qa["ok"] = True
    return series, qa


def load_series(
    key: str,
    spec: Mapping[str, Any],
    loaders: Mapping[str, Loader],
    calendar: pd.DatetimeIndex,
    asof: pd.Timestamp,
    core: bool,
    crosscheck: Optional[Callable[[str], Optional[pd.DataFrame]]] = None,
) -> Tuple[Optional[pd.Series], Dict[str, Any]]:
    """按 Tushare → Baostock 的顺序整序列取数，第一条过核验的胜出。

    只允许整序列换源、不拼接；每次尝试（含失败原因）都进 `attempts`。
    """
    attempts: List[Dict[str, Any]] = []
    chosen: Optional[pd.Series] = None
    chosen_qa: Dict[str, Any] = {}
    for source, code_field in (("tushare", "ts_code"), ("baostock", "bs_code")):
        code = spec.get(code_field)
        loader = loaders.get(source)
        if not code or loader is None:
            continue
        try:
            frame = loader(code, spec.get("fetch_start") or "19900101", _ymd(asof))
        except Exception as exc:  # noqa: BLE001 - 取数失败按降级记录
            attempts.append({"source": source, "code": code, "error": str(exc)[:200]})
            continue
        series, qa = validate_series(frame, calendar, asof, spec, core)
        attempts.append({"source": source, "code": code, "ok": qa.get("ok"), "error": qa.get("error")})
        if series is not None:
            chosen, chosen_qa = series, {**qa, "source": source, "code": code}
            break
        if not chosen_qa:
            chosen_qa = {**qa, "source": source, "code": code}

    entry: Dict[str, Any] = {
        "symbol": key,
        "name": spec.get("name"),
        "ts_code": spec.get("ts_code"),
        "role": "core" if core else "total_return",
        **{k: v for k, v in chosen_qa.items() if k != "ok"},
        "ok": chosen is not None,
        "attempts": attempts,
    }
    if not attempts:
        entry["error"] = "no loader available"
    elif chosen is None:
        # 全部尝试都失败：把每一路的原因都写出来，别让「取数抛异常」变成 error=None
        entry["error"] = "；".join(f"{a['source']}: {a.get('error')}" for a in attempts)
    # 交叉源核对（只读已有缓存，不额外联网）：同一指数两家供应商的收盘差，bps
    if chosen is not None and crosscheck is not None:
        other_code = spec.get("bs_code") if entry.get("source") == "tushare" else spec.get("ts_code")
        if other_code:
            try:
                other = crosscheck(other_code)
            except Exception:  # noqa: BLE001
                other = None
            if other is not None and not other.empty:
                o = other[["trade_date", "close"]].copy()
                o["trade_date"] = pd.to_datetime(o["trade_date"].astype(str).str.replace("-", "").str[:8], format="%Y%m%d", errors="coerce")
                o = pd.to_numeric(o.dropna().set_index("trade_date")["close"], errors="coerce")
                both = pd.concat([chosen, o], axis=1, join="inner").dropna()
                if len(both):
                    diff = (both.iloc[:, 0] / both.iloc[:, 1] - 1).abs()
                    entry["crosscheck"] = {
                        "code": other_code,
                        "overlap": int(len(both)),
                        "max_diff_bps": _num(diff.max() * 10000, 2),
                    }
    return chosen, entry


# --------------------------------------------------------------------------- #
# 计算（原版 analyze.py 的逐项移植）
# --------------------------------------------------------------------------- #
def percentile_prior(series: pd.Series, rank_start: pd.Timestamp) -> pd.Series:
    """无前视分位：每天只与严格早于当天、且不早于 rank_start 的样本比较。

    100 × count(历史值 < 当日值) / 历史样本数；同值不计入小于项；少于 252 个
    历史样本输出 NaN。
    """
    hist: List[float] = []
    values: List[float] = []
    for date, x in series.items():
        if pd.isna(x) or date < rank_start:
            values.append(np.nan)
            continue
        fx = float(x)
        values.append(100 * bisect.bisect_left(hist, fx) / len(hist) if len(hist) >= PERCENTILE_MIN else np.nan)
        bisect.insort(hist, fx)
    return pd.Series(values, index=series.index)


def rank_start_for(calendar: pd.DatetimeIndex, release: str, warmup: int) -> Optional[pd.Timestamp]:
    """发布日之后第 warmup 个交易日：窗口里的所有观测都必须在发布之后。"""
    live = calendar[calendar >= pd.Timestamp(release)]
    return live[warmup] if len(live) > warmup else None


def log_contrast(prices: pd.DataFrame, weights: Mapping[str, float]) -> pd.Series:
    """按权重加总对数价格——只在各分量共同有效的日期上算，不插值。"""
    p = prices[list(weights)].dropna()
    return np.log(p).mul(pd.Series(weights)).sum(axis=1)


def rotation_of(signs: Sequence[float]) -> Tuple[str, str]:
    """60/20/10 日方向的确定性读法（原版 rotation_note，外加机读代码）。"""
    s60, s20, s10 = signs
    if s60 == s20 == s10 and s60 != 0:
        return "aligned", "10/20/60日方向一致"
    if s60 * s20 < 0:
        return "reversal_20v60", "20日与60日方向相反，近期风格发生轮动"
    if s20 * s10 < 0:
        return "shift_10v20", "10日与20日方向相反，短期出现变化"
    return "mixed", "各窗口方向不完全一致"


def compute_style_factor(
    key: str,
    prices: pd.DataFrame,
    calendar: pd.DatetimeIndex,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    logq = log_contrast(prices, WEIGHTS[key])
    level = np.exp(logq - logq.iloc[0]) * 100
    df = pd.DataFrame({"level": level, "daily_log_spread": logq.diff()})
    for n in REL_HORIZONS:
        df[f"relative_{n}d_pct"] = np.expm1(logq - logq.shift(n)) * 100
    rank_start = rank_start_for(calendar, META[key]["release"], 60)
    if rank_start is not None:
        df["percentile_60d_prior"] = percentile_prior(df["relative_60d_pct"], rank_start)
        rank_samples = int((df["relative_60d_pct"].dropna().index >= rank_start).sum() - 1)
    else:
        df["percentile_60d_prior"] = np.nan
        rank_samples = 0

    last = df.iloc[-1]

    def side(n: int) -> str:
        x = float(last[f"relative_{n}d_pct"])
        if abs(x) < 1e-10:
            return "持平"
        pos, neg = SIDE_NAMES[key]
        return pos if x > 0 else neg

    signs = [float(np.sign(last[f"relative_{n}d_pct"])) for n in (60, 20, 10)]
    code, note = rotation_of(signs)
    metric = {
        "key": key,
        **{k: v for k, v in META[key].items()},
        "release": _iso(pd.Timestamp(META[key]["release"])),
        "start": _iso(level.index.min()),
        "rows": int(len(level)),
        "level": _num(last["level"]),
        **{f"relative_{n}d_pct": _num(last[f"relative_{n}d_pct"]) for n in REL_HORIZONS},
        "percentile": _num(last["percentile_60d_prior"], 1),
        "percentile_basis": "60日相对变化，发布后历史、只比当日之前",
        "rank_start": _iso(rank_start) if rank_start is not None else None,
        "rank_samples": max(rank_samples, 0),
        "direction": {str(n): side(n) for n in (60, 20, 10)},
        "label": f"60日：{side(60)}；20日：{side(20)}；10日：{side(10)}",
        "rotation_code": code,
        "rotation_note": note,
        "window_start_dates": {
            str(n): _iso(df.index[-n - 1]) if len(df) > n else None for n in REL_HORIZONS
        },
    }
    return df, metric


def compute_env_factor(
    key: str,
    market: pd.Series,
    calendar: pd.DatetimeIndex,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    if key == "market_trend":
        raw = (market / market.rolling(60, min_periods=60).mean() - 1) * 100
        warmup = 60
    else:
        raw = market.pct_change(fill_method=None).rolling(20, min_periods=20).std(ddof=1) * np.sqrt(252) * 100
        warmup = 20
    s = raw.dropna()
    rank_start = rank_start_for(calendar, META[key]["release"], warmup)
    ranks = percentile_prior(s, rank_start) if rank_start is not None else pd.Series(np.nan, index=s.index)
    df = pd.DataFrame({"level": s, "percentile_prior": ranks})
    val = float(s.iloc[-1])
    pct = ranks.iloc[-1]
    if key == "market_trend":
        label = "趋势偏强" if val > 0 else "趋势偏弱"
    elif pd.isna(pct):
        label = "波动分位不足"
    else:
        label = "波动偏高" if pct >= VOL_HIGH_PCT else ("波动偏低" if pct <= VOL_LOW_PCT else "波动中等")
    metric = {
        "key": key,
        **{k: v for k, v in META[key].items()},
        "release": _iso(pd.Timestamp(META[key]["release"])),
        "start": _iso(s.index.min()),
        "rows": int(len(s)),
        "level": _num(val),
        "percentile": _num(pct, 1),
        "percentile_basis": "发布后历史、只比当日之前",
        "rank_start": _iso(rank_start) if rank_start is not None else None,
        "rank_samples": max(int((s.index >= rank_start).sum() - 1), 0) if rank_start is not None else 0,
        "label": label,
        "change_20d_pp": _num(s.iloc[-1] - s.iloc[-21]) if len(s) > 20 else None,
    }
    return df, metric


def compute_total_return(
    key: str,
    tr_prices: pd.DataFrame,
    price_metric: Mapping[str, Any],
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    logq = log_contrast(tr_prices, TR_WEIGHTS[key])
    df = pd.DataFrame({"level": np.exp(logq - logq.iloc[0]) * 100})
    for n in TR_HORIZONS:
        df[f"relative_{n}d_pct"] = np.expm1(logq - logq.shift(n)) * 100
    last = df.iloc[-1]
    row: Dict[str, Any] = {
        "key": key,
        "name": META[key]["name"],
        "start": _iso(df.index.min()),
        **{f"relative_{n}d_pct": _num(last[f"relative_{n}d_pct"]) for n in TR_HORIZONS},
    }
    agree = {}
    for n in TR_HORIZONS:
        tr = row[f"relative_{n}d_pct"]
        px = price_metric.get(f"relative_{n}d_pct")
        agree[str(n)] = None if tr is None or px is None else bool(np.sign(tr) == np.sign(px))
    row["direction_agrees_with_price"] = agree
    row["all_directions_agree"] = all(v is True for v in agree.values())
    return df, row


def window_cutoffs(size_index: pd.DatetimeIndex, asof: pd.Timestamp) -> Dict[str, Optional[pd.Timestamp]]:
    return {
        "all": None,
        "3y": asof - pd.DateOffset(years=3),
        "1y": asof - pd.DateOffset(years=1),
        "60d": size_index[-61] if len(size_index) > 60 else size_index[0],
    }


def view_stats(
    frames: Mapping[str, pd.DataFrame],
    cutoffs: Mapping[str, Optional[pd.Timestamp]],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """四窗口 × 六因子的区间读数（原版 view_snapshots）。

    风格比值在窗口首日重设为 100、区间变化单位 %；趋势与波动保留原单位，
    区间差值单位「个百分点」。
    """
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for view, view_name in VIEW_DEFS:
        cutoff = cutoffs[view]
        out[view] = {}
        for key in FACTOR_KEYS:
            if key not in frames:
                continue
            full = frames[key]["level"].dropna()
            rs = full if cutoff is None else full.loc[cutoff:]
            if rs.empty:
                continue
            is_style = key in STYLE_KEYS
            y = rs / rs.iloc[0] * 100 if is_style else rs
            change = float(y.iloc[-1] - 100) if is_style else float(y.iloc[-1] - y.iloc[0])
            out[view][key] = {
                "view_name": view_name,
                "from": _iso(rs.index[0]),
                "to": _iso(rs.index[-1]),
                "observations": int(len(rs)),
                "return_intervals": int(len(rs) - 1),
                "change": _num(change),
                "display_last": _num(y.iloc[-1]),
                "raw_start": _num(rs.iloc[0], 6),
                "raw_last": _num(rs.iloc[-1], 6),
                "unit": "%" if is_style else "个百分点",
            }
    return out


def style_cells(prices: pd.DataFrame) -> List[Dict[str, Any]]:
    cells = []
    for sym, name in (("sz399372", "大盘成长"), ("sz399373", "大盘价值"), ("sz399374", "中盘成长"),
                      ("sz399375", "中盘价值"), ("sz399376", "小盘成长"), ("sz399377", "小盘价值"),
                      ("sh000985", "中证全指")):
        s = prices[sym].dropna()
        cells.append({
            "name": name,
            "symbol": sym,
            **{f"return_{n}d_pct": _num((s.iloc[-1] / s.iloc[-n - 1] - 1) * 100) if len(s) > n else None
               for n in CELL_HORIZONS},
        })
    return cells


def robustness_check(prices: pd.DataFrame, size_metric: Mapping[str, Any]) -> Dict[str, Any]:
    """传统规模对照：中证1000/沪深300，与主规模代理对方向。"""
    t = prices[["sh000852", "sh000300"]].dropna()
    tq = t["sh000852"] / t["sh000300"]
    rel = {f"relative_{n}d_pct": _num((tq.iloc[-1] / tq.iloc[-n - 1] - 1) * 100) if len(tq) > n else None
           for n in (20, 60, 120)}
    agree: Dict[str, Optional[bool]] = {}
    notes = []
    for n in (20, 60):
        a, b = rel[f"relative_{n}d_pct"], size_metric.get(f"relative_{n}d_pct")
        agree[str(n)] = None if a is None or b is None else bool(np.sign(a) == np.sign(b))
        notes.append(f"{n}日" + ("同向" if agree[str(n)] else "方向冲突，规模结论对指数构造敏感"))
    return {
        "proxy": "中证1000/沪深300",
        **rel,
        "direction_agrees_with_size": agree,
        "note": "；".join(notes),
    }


def factor_correlations(frames: Mapping[str, pd.DataFrame]) -> Dict[str, Dict[str, Optional[float]]]:
    logrets = pd.DataFrame({k: frames[k]["daily_log_spread"] for k in STYLE_KEYS if k in frames}).dropna()
    corr = logrets.corr()
    return {a: {b: _num(corr.loc[a, b], 3) for b in corr.columns} for a in corr.index}


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def _round_list(values: Iterable[Any], digits: int = 4) -> List[Optional[float]]:
    return [_num(v, digits) for v in values]


def build_display_payload(
    asof: pd.Timestamp,
    frames: Mapping[str, pd.DataFrame],
    tr_frames: Mapping[str, pd.DataFrame],
    views: Mapping[str, Any],
    metrics: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """HTML 用的日频曲线（不进 evidence，旁路写 style_factors_YYYYMMDD.json）。"""
    axis = sorted(set().union(*[set(f.index) for f in frames.values()]))
    axis_idx = pd.DatetimeIndex(axis)
    factors: Dict[str, Any] = {}
    for key in FACTOR_KEYS:
        if key not in frames:
            continue
        f = frames[key].reindex(axis_idx)
        entry: Dict[str, Any] = {
            "name": META[key]["name"],
            "positive": META[key]["positive"],
            "negative": META[key]["negative"],
            "unit": META[key]["unit"],
            "is_style": key in STYLE_KEYS,
            "level": _round_list(f["level"]),
            "label": metrics[key].get("label"),
            "percentile": metrics[key].get("percentile"),
        }
        if key in STYLE_KEYS:
            entry["relative_60d"] = _round_list(f["relative_60d_pct"], 3)
            entry["relative_60d_last"] = metrics[key].get("relative_60d_pct")
            if key in tr_frames:
                tr = tr_frames[key].reindex(axis_idx)
                entry["tr_relative_60d"] = _round_list(tr["relative_60d_pct"], 3)
        else:
            entry["level_last"] = metrics[key].get("level")
        factors[key] = entry
    return {
        "as_of": _iso(asof),
        "dates": [_iso(d) for d in axis_idx],
        "factors": factors,
        "views": views,
        "view_defs": [{"key": k, "name": n} for k, n in VIEW_DEFS],
    }


def build_block(
    asof: str,
    trade_dates: Sequence[str],
    loaders: Mapping[str, Loader],
    crosscheck: Optional[Callable[[str], Optional[pd.DataFrame]]] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """返回 (evidence 读数块, HTML 曲线载荷)。核心序列任一不完整 → available=false。"""
    asof_ts = pd.Timestamp(str(asof))
    calendar = _to_calendar(trade_dates)
    calendar = calendar[calendar <= asof_ts]
    block: Dict[str, Any] = {
        "available": False,
        "as_of": _iso(asof_ts),
        "source": SOURCE_NOTE,
        "coverage": COVERAGE_NOTE,
        "boundaries": BOUNDARY_NOTES,
    }
    if len(calendar) == 0 or calendar[-1] != asof_ts:
        block["reason"] = f"trade calendar does not contain asof {_iso(asof_ts)}"
        return block, None

    qa: List[Dict[str, Any]] = []
    prices: Dict[str, pd.Series] = {}
    for key, spec in CORE_SERIES.items():
        series, entry = load_series(key, spec, loaders, calendar, asof_ts, core=True, crosscheck=crosscheck)
        qa.append(entry)
        if series is not None:
            prices[key] = series
    tr_prices: Dict[str, pd.Series] = {}
    for key, spec in TOTAL_RETURN_SERIES.items():
        series, entry = load_series(key, spec, loaders, calendar, asof_ts, core=False)
        qa.append(entry)
        if series is not None:
            tr_prices[key] = series
    block["qa"] = qa

    failed_core = [e["symbol"] for e in qa if e["role"] == "core" and not e["ok"]]
    if failed_core:
        block["reason"] = "核心序列不完整：" + "、".join(
            f"{CORE_SERIES[s]['name']}（{next(e.get('error') for e in qa if e['symbol'] == s)}）" for s in failed_core
        )
        block["quality"] = "FAIL: required histories incomplete"
        return block, None

    P = pd.DataFrame(prices).sort_index()
    frames: Dict[str, pd.DataFrame] = {}
    metrics: Dict[str, Dict[str, Any]] = {}
    for key in STYLE_KEYS:
        frames[key], metrics[key] = compute_style_factor(key, P, calendar)
    market = P["sh000985"].dropna()
    for key in ENV_KEYS:
        frames[key], metrics[key] = compute_env_factor(key, market, calendar)

    # 全收益敏感性核验：组内齐全才用，缺一条整组停用
    tr_frames: Dict[str, pd.DataFrame] = {}
    sensitivity: List[Dict[str, Any]] = []
    tr_status: Dict[str, Any] = {}
    groups = {
        "size_growth": [k for k, s in TOTAL_RETURN_SERIES.items() if s["group"] == "size_growth"],
        "dividend": [k for k, s in TOTAL_RETURN_SERIES.items() if s["group"] == "dividend"],
    }
    TR = pd.DataFrame(tr_prices).sort_index() if tr_prices else pd.DataFrame()
    for group, members in groups.items():
        missing = [m for m in members if m not in tr_prices]
        tr_status[group] = "complete" if not missing else "unavailable: " + "、".join(TOTAL_RETURN_SERIES[m]["name"] for m in missing)
        if missing:
            continue
        for key in (("size", "growth") if group == "size_growth" else ("dividend",)):
            tr_frames[key], row = compute_total_return(key, TR, metrics[key])
            row["extension"] = group == "dividend"
            sensitivity.append(row)
    if sensitivity:
        agree_all = all(r["all_directions_agree"] for r in sensitivity)
        tr_note = "含分红核验已完成；10/20/60/120日方向" + ("全部一致。" if agree_all else "存在差异，需分别解读。")
    else:
        tr_note = "含分红核验未完成，不声称方向一致。"

    cutoffs = window_cutoffs(frames["size"].index, asof_ts)
    views = view_stats(frames, cutoffs)

    block.update({
        "available": True,
        "quality": "PASS: required histories complete to as_of",
        "metrics": {key: metrics[key] for key in FACTOR_KEYS},
        "order": list(FACTOR_KEYS),
        "robustness_checks": {"size_csi1000_over_csi300": robustness_check(P, metrics["size"])},
        "sensitivity_total_return": sensitivity,
        "total_return_status": tr_status,
        "total_return_note": tr_note,
        "style_cells": style_cells(P),
        "factor_correlations": factor_correlations(frames),
        "views": views,
        "window_dates": {
            str(n): metrics["size"]["window_start_dates"].get(str(n)) for n in (10, 20, 60, 120)
        },
    })
    return block, build_display_payload(asof_ts, frames, tr_frames, views, metrics)


def compact(block: Mapping[str, Any]) -> Dict[str, Any]:
    """模块级 JSON 用：去掉四窗口明细与相关矩阵之外的冗余（QA 只留摘要）。"""
    if not block or not block.get("available"):
        return {k: block.get(k) for k in ("available", "as_of", "reason", "quality", "source") if block}
    qa = block.get("qa") or []
    return {
        **{k: v for k, v in block.items() if k not in ("qa",)},
        "qa_summary": [
            {k: e.get(k) for k in ("symbol", "name", "role", "ok", "source", "start", "end", "rows", "missing_trade_days", "error", "valid_note")}
            for e in qa
        ],
    }


# --------------------------------------------------------------------------- #
# CLI（调试用；生产走 daily.build-evidence）
# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compute A-share style factors for one trade date.")
    parser.add_argument("--asof", required=True, help="YYYYMMDD trade date")
    parser.add_argument("--display", action="store_true", help="Also print the HTML display payload size.")
    args = parser.parse_args(argv)

    import market_panel  # 延迟导入：market_panel 反过来会 import 本模块

    pro = market_panel.get_pro()
    block, display = market_panel.build_style_factor_block(pro, market_panel.normalize_date(args.asof))
    print(json.dumps(compact(block), ensure_ascii=False, indent=2))
    if args.display and display is not None:
        print(f"display payload: {len(json.dumps(display, ensure_ascii=False)) / 1024:.1f} KB", file=sys.stderr)
    return 0 if block.get("available") else 1


if __name__ == "__main__":
    raise SystemExit(main())
