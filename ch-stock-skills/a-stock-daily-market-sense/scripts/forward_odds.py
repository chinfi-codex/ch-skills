#!/usr/bin/env python3
"""前瞻概率层（forward_odds）与情绪脉冲（pulse）——盘后研报模块 1 的第三根轴。

**为什么要有第三根轴。** 趋势轴（六档状态机）和极值轴（出清分 / 顶部分）都在回答
「现在是什么状态」，没有一根在回答「接下来大概率会怎样」。2026-08-19 是这个缺口的
典型样本：当日上涨家数占比 8.1%（6 年 1.6 分位）、跌停占比 2.47%（98.1 分位），
但趋势轴判「谨慎」、极值轴只给 2/6 分。两根轴都没接住，原因是：

  - 状态机的三个条件组（破位 / 融资 / 缩量）全是多日累积型，一天之内塌下来的
    市场一组都凑不齐；唯一的单日入口是恐慌线（跌停占比 ≥ 3.5%），8/19 差一口气。
    而「上涨家数占比」这条最直观的腿，整个状态机里根本不存在。
  - 极值轴六条腿全是熊市底部形态（深回撤、多新低、宽度被打穿）。8/19 是牛市里的
    单日恐慌（指数距 250 日高点只有 −8.2%），结构上确实不像底，但情绪上是六年一遇。

所以补两样东西：`pulse` 测单日情绪冲击强度（快变量，不依赖市场处在什么位置），
`forward_odds` 把「历史上出现同类读数之后发生了什么」变成随卡输出的条件分布。

------------------------------------------------------------------------------
pulse（情绪脉冲）：四腿合取，**不是打分**
------------------------------------------------------------------------------
  s1 上涨家数占比       滚动分位 ≤ 5%
  s2 跌停家数占比       滚动分位 ≥ 95%
  s3 涨停/跌停比        滚动分位 ≤ 5%
  s4 放量杀跌           成交额 ≥ 前 20 日均量 且 上涨占比 ≤ 20%

四条全中才算命中（`gate=true`）。**不得改成 0~4 分再取阈值**——实测前瞻分布根本
不单调：档 0/1/2/3 全部贴基准或跑输（+2 日上涨率 49.2% / 47.8% / 53.3% / 69.2%），
只有档 4 有效（85.7%）。而「分数 ≥ 2」这种自然写法过不了子样本检验（2021~2023 段
+1/+2/+3 日分别 45.8% / 54.2% / 54.2%，全部跑输基准）。最有说服力的是对照组：
命中 s1&s2 但没凑齐四腿的那些日子，+3 日是 44.4%↑ / −1.50%，明显跑输基准——多出来
的两条腿在做实事，不是装饰。

------------------------------------------------------------------------------
forward_odds（前瞻概率）
------------------------------------------------------------------------------
主口径是**中证1000**，不是上证：同一个信号下中证1000 的 +3 日均值约 +3.4%，上证只有
约 +1.4%。买个股的人和买指数的人体感完全不同，指数会系统性低估短线窗口。序列由
`000852.SH`（Tushare，6 年）拼 `sh.000852`（Baostock，新尾）而成——两者在 264 个
重叠交易日上收盘价完全一致（最大绝对差 0.0），可以直接拼。

底部侧输出方向分布（上涨概率 / 收益）、路径分布（触及与不破）、前瞻广度；
顶部侧**只输出回撤风险分布，不输出方向**——实证见 `references/methodology/forward_odds.md`。

发布门槛三条，任一不满足则读数照出但标 `publishable=false`，不得进判断词：
  事件簇去重后 n ≥ 12 ｜ 两个子样本方向一致 ｜ 置换检验 p < 0.05

用法：
  python3 scripts/forward_odds.py --asof 20260819
  python3 scripts/forward_odds.py --asof 20260819 --full   # 带事件清单与留一检验
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))
from db_core import BACKEND, Backend, get_connection  # noqa: E402

# 前瞻收益主口径：中证1000。Tushare 打底（6 年），Baostock 补新尾（Tushare 常滞后数日）。
BENCH_PRIMARY = "000852.SH"
BENCH_TAIL = "sh.000852"
BENCH_LABEL = "中证1000"
BENCH_REF = "000001.SH"          # 参照口径：上证综指
BENCH_REF_LABEL = "上证综指"

ROLL_WINDOW = 500                # 滚动分位窗口（交易日）
ROLL_MIN = 250                   # 不足此长度不出 pulse（宁可不给，也不给一个没有历史支撑的分位）
DEDUP_GAP = 5                    # 事件簇去重间隔：连续几天触发只取首日，否则 n 灌水
HORIZONS = (1, 2, 3, 5)
PATH_WINDOW = 3                  # 路径分布（触及 / 不破）的观察窗
HISTORY_DAYS = 30                # 脉冲轨迹长度，与两张状态卡的 HISTORY_DAYS 对齐（HTML 时间轴共用一条日期轴）
SUBSAMPLE_SPLIT = date(2023, 12, 31)
PERM_DRAWS = 20000
PERM_SEED = 20260822
MIN_EVENTS = 12                  # 发布门槛：事件簇去重后的最小样本量
PERM_ALPHA = 0.05
DRAWDOWN_LEVELS = (-0.02, -0.05)  # 顶部侧回撤风险的观察档

# pulse 四腿的分位线（滚动分位，无前视）
S1_Q = 0.05      # 上涨家数占比
S2_Q = 0.95      # 跌停家数占比
S3_Q = 0.05      # 涨停/跌停比
S4_AMT = 1.00    # 放量杀跌：成交额 / 前 20 日均量
S4_RISE = 0.20   # 放量杀跌：上涨家数占比上限

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS dms_forward_odds_stats (
    signal_key      TEXT NOT NULL,
    stats_through   DATE NOT NULL,
    payload         JSONB NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (signal_key, stats_through)
)
"""


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def _index_series(conn, codes: Sequence[str]) -> pd.DataFrame:
    """按 codes 顺序拼一条指数收盘序列：靠前的优先，靠后的只补缺失日。"""
    frames: List[pd.DataFrame] = []
    for code in codes:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date, close FROM stock_index_daily "
                "WHERE ts_code = %s AND close IS NOT NULL ORDER BY trade_date",
                (code,),
            )
            rows = cur.fetchall()
        if rows:
            frames.append(pd.DataFrame(rows, columns=["trade_date", "close"]))
    if not frames:
        return pd.DataFrame(columns=["trade_date", "close"])
    out = pd.concat(frames).drop_duplicates("trade_date", keep="first")
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    return out.sort_values("trade_date").reset_index(drop=True)


def load_market_frame(conn) -> pd.DataFrame:
    """market_history（6 年市场宽度）+ 中证1000 主口径 + 上证参照，按交易日内连接。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT date, rise, fall, flat, limit_up, limit_down, amount "
            "FROM market_history ORDER BY date"
        )
        rows = cur.fetchall()
    mh = pd.DataFrame(rows, columns=["date", "rise", "fall", "flat",
                                     "limit_up", "limit_down", "amount"])
    if mh.empty:
        return mh
    mh["date"] = pd.to_datetime(mh["date"])
    for col in ("rise", "fall", "flat", "limit_up", "limit_down", "amount"):
        mh[col] = pd.to_numeric(mh[col], errors="coerce")

    # 融资余额按真实交易日汇总；分交易所分批发布的日子（交易所数少于窗口众数）
    # 汇总口径不全，直接剔除——与 extreme_state_card 的 margin_metrics 同口径。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, COUNT(DISTINCT exchange_id) AS n, SUM(rzye) AS rzye "
            "FROM stock_margin WHERE rzye IS NOT NULL GROUP BY trade_date ORDER BY trade_date"
        )
        mrows = cur.fetchall()
    margin = pd.DataFrame(mrows, columns=["trade_date", "n_exchange", "rzye"])
    if not margin.empty:
        modal = int(margin["n_exchange"].mode().iloc[0])
        margin = margin[margin["n_exchange"] == modal].drop(columns="n_exchange")
        margin["trade_date"] = pd.to_datetime(margin["trade_date"])
        margin["rzye"] = pd.to_numeric(margin["rzye"], errors="coerce")

    bench = _index_series(conn, (BENCH_PRIMARY, BENCH_TAIL)).rename(columns={"close": "bench"})
    ref = _index_series(conn, (BENCH_REF,)).rename(columns={"close": "ref"})
    df = mh.merge(bench, left_on="date", right_on="trade_date", how="inner").drop(columns="trade_date")
    df = df.merge(ref, left_on="date", right_on="trade_date", how="left").drop(columns="trade_date")
    if not margin.empty:
        df = df.merge(margin, left_on="date", right_on="trade_date", how="left").drop(columns="trade_date")
    else:
        df["rzye"] = np.nan
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 特征与信号
# ---------------------------------------------------------------------------
def _rolling_pct_rank(s: pd.Series) -> pd.Series:
    """今天在过去 ROLL_WINDOW 天里的分位（不含今天本身，无前视）。"""
    return s.rolling(ROLL_WINDOW, min_periods=ROLL_MIN).apply(
        lambda x: float((x[:-1] < x[-1]).mean()), raw=True)


def _rolling_quantile(s: pd.Series, q: float) -> pd.Series:
    """过去 ROLL_WINDOW 天（不含今天）的 q 分位，即当日实际生效的阈值。

    窗口切法必须与 `_rolling_pct_rank` 完全一致（同样是 `x[:-1]`）。早先这里用
    `shift(1).rolling(ROLL_WINDOW-1)`，窗口内容在边界上与分位排名差一格，于是
    约 0.3% 的日子会出现「值 8.0、阈值 13.1、却判未命中」这种自相矛盾的卡面。
    命中判定现在直接比阈值（见 add_features），显示与判定由构造保证一致。
    """
    return s.rolling(ROLL_WINDOW, min_periods=ROLL_MIN).apply(
        lambda x: float(np.quantile(x[:-1], q)), raw=True)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["total"] = df["rise"] + df["fall"] + df["flat"]
    df["rise_share"] = df["rise"] / df["total"]
    df["limit_down_share"] = df["limit_down"] / df["total"]
    df["limit_up_share"] = df["limit_up"] / df["total"]
    # 跌停为 0 时按 1 计：比值只需单调且良定义，用 NaN 会污染整个滚动窗口
    df["lu_ld_ratio"] = df["limit_up"] / df["limit_down"].clip(lower=1)
    df["amt_ma20_prev"] = df["amount"].rolling(20).mean().shift(1)
    df["amt_ratio"] = df["amount"] / df["amt_ma20_prev"]

    df["q_rise_share"] = _rolling_pct_rank(df["rise_share"])
    df["q_limit_down_share"] = _rolling_pct_rank(df["limit_down_share"])
    df["q_lu_ld_ratio"] = _rolling_pct_rank(df["lu_ld_ratio"])
    df["thr_rise_share"] = _rolling_quantile(df["rise_share"], S1_Q)
    df["thr_limit_down_share"] = _rolling_quantile(df["limit_down_share"], S2_Q)
    df["thr_lu_ld_ratio"] = _rolling_quantile(df["lu_ld_ratio"], S3_Q)

    # 命中判定比的是阈值本身，不是分位排名——两者在同一窗口上定义，但只有比阈值
    # 才能保证卡面「值 / 阈值 / 命中」三者永远自洽。实测两种口径给出的事件集完全相同。
    df["s1"] = df["rise_share"] <= df["thr_rise_share"]
    df["s2"] = df["limit_down_share"] >= df["thr_limit_down_share"]
    df["s3"] = df["lu_ld_ratio"] <= df["thr_lu_ld_ratio"]
    df["s4"] = (df["amt_ratio"] >= S4_AMT) & (df["rise_share"] <= S4_RISE)
    df["pulse_legs"] = df[["s1", "s2", "s3", "s4"]].sum(axis=1)
    df["pulse_gate"] = df["s1"] & df["s2"] & df["s3"] & df["s4"]
    # 分位没攒满窗口的日子一律按不可判处理，不给「腿数」也不给门槛
    df["pulse_ready"] = df["thr_rise_share"].notna() & df["thr_lu_ld_ratio"].notna()

    # 顶部侧候选（可在 6 年 market_history 上算的三条；p1/p5 依赖 stock_daily，历史不足）
    df["amt_ratio_5_20"] = df["amount"].rolling(5).mean() / df["amount"].rolling(20).mean()
    df["lu_ma5"] = df["limit_up_share"].rolling(5).mean()
    df["lu_ma20"] = df["limit_up_share"].rolling(20).mean()
    df["rzye_chg20"] = df["rzye"] / df["rzye"].shift(20) - 1
    df["bench_high20"] = df["bench"] >= df["bench"].rolling(20).max()
    df["bench_high20_10d"] = df["bench_high20"].rolling(10).max().astype(bool)

    # 前瞻收益：主口径中证1000，参照上证
    for n in HORIZONS:
        df[f"fwd{n}"] = df["bench"].shift(-n) / df["bench"] - 1
        df[f"ref_fwd{n}"] = df["ref"].shift(-n) / df["ref"] - 1
    # 路径分布：未来 N 日的最高 / 最低收盘（相对信号日收盘）
    for n in (PATH_WINDOW, 5, 10):
        stack = pd.concat([df["bench"].shift(-k) for k in range(1, n + 1)], axis=1)
        # 必须走完整个观察窗才是一条有效路径；默认 skipna 会把尾部仅有 1~2 天的
        # 残缺窗口也算进去，导致每日运行时基准与条件分布被未成熟样本污染。
        df[f"max{n}"] = stack.max(axis=1, skipna=False) / df["bench"] - 1
        df[f"min{n}"] = stack.min(axis=1, skipna=False) / df["bench"] - 1
    # 前瞻广度：次日上涨家数是否多于下跌家数
    next_rise = df["rise"].shift(-1)
    next_fall = df["fall"].shift(-1)
    df["next_breadth_up"] = (
        (next_rise > next_fall).where(next_rise.notna() & next_fall.notna()).astype(float)
    )
    return df


def dedup_events(idx: Sequence[int], gap: int = DEDUP_GAP) -> List[int]:
    """事件簇去重：间隔小于 gap 的连续触发只保留首日。"""
    keep: List[int] = []
    last = -10 ** 9
    for i in idx:
        if i - last >= gap:
            keep.append(i)
            last = i
    return keep


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def _pct(x: Any, nd: int = 2) -> Optional[float]:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    return round(float(x) * 100.0, nd)


def permutation_p(observed: np.ndarray, pool: np.ndarray, rng: np.random.Generator) -> Dict[str, Any]:
    """从同期全样本随机抽同样多的交易日，看观测到的均值/上涨率有多难得。

    只校正「这批日子是不是碰巧」，**不校正腿的挑选过程**——四条腿是在同一份历史上
    选出来的，这一层由阈值冻结与逐年留一兜底，见模块文档的过拟合护栏一节。
    """
    k = len(observed)
    if k == 0 or len(pool) <= k:
        return {"p_mean": None, "p_win_rate": None, "draws": 0}
    obs_mean = float(observed.mean())
    obs_win = float((observed > 0).mean())
    hits_mean = 0
    hits_win = 0
    for _ in range(PERM_DRAWS):
        sample = rng.choice(pool, k, replace=False)
        if sample.mean() >= obs_mean:
            hits_mean += 1
        if (sample > 0).mean() >= obs_win:
            hits_win += 1
    return {"p_mean": round(hits_mean / PERM_DRAWS, 4),
            "p_win_rate": round(hits_win / PERM_DRAWS, 4),
            "draws": PERM_DRAWS}


def _distribution(series: pd.Series) -> Dict[str, Any]:
    s = series.dropna()
    if s.empty:
        return {"n": 0}
    return {
        "n": int(len(s)),
        "win_rate_pct": _pct((s > 0).mean(), 1),
        "mean_pct": _pct(s.mean()),
        "median_pct": _pct(s.median()),
    }


def horizon_stats(df: pd.DataFrame, events: List[int], rng: np.random.Generator,
                  valid: pd.Series) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for n in HORIZONS:
        col = f"fwd{n}"
        obs = df.loc[events, col].dropna()
        base = df.loc[valid, col].dropna()
        ref_obs = df.loc[events, f"ref_fwd{n}"].dropna()
        ref_base = df.loc[valid, f"ref_fwd{n}"].dropna()
        row: Dict[str, Any] = {
            "horizon_days": n,
            **_distribution(obs),
            "base_n": int(len(base)),
            "base_win_rate_pct": _pct((base > 0).mean(), 1),
            "base_mean_pct": _pct(base.mean()),
            "ref_win_rate_pct": _pct((ref_obs > 0).mean(), 1) if len(ref_obs) else None,
            "ref_mean_pct": _pct(ref_obs.mean()) if len(ref_obs) else None,
            "ref_base_mean_pct": _pct(ref_base.mean()) if len(ref_base) else None,
        }
        row["permutation"] = permutation_p(obs.to_numpy(), base.to_numpy(), rng)
        out.append(row)
    return out


def subsample_stats(df: pd.DataFrame, events: List[int]) -> List[Dict[str, Any]]:
    """按 SUBSAMPLE_SPLIT 切两段，两段方向一致才算稳。"""
    split = pd.Timestamp(SUBSAMPLE_SPLIT)
    groups = [
        (f"~{SUBSAMPLE_SPLIT.year}", [i for i in events if df.at[i, "date"] <= split]),
        (f"{SUBSAMPLE_SPLIT.year + 1}~", [i for i in events if df.at[i, "date"] > split]),
    ]
    out: List[Dict[str, Any]] = []
    for label, sub in groups:
        entry: Dict[str, Any] = {"label": label, "events": len(sub), "horizons": []}
        for n in HORIZONS:
            entry["horizons"].append({"horizon_days": n, **_distribution(df.loc[sub, f"fwd{n}"])})
        out.append(entry)
    return out


def leave_one_year_out(df: pd.DataFrame, events: List[int], horizon: int) -> List[Dict[str, Any]]:
    years = sorted({int(df.at[i, "date"].year) for i in events})
    out: List[Dict[str, Any]] = []
    for y in years:
        dropped = [i for i in events if int(df.at[i, "date"].year) == y]
        rest = [i for i in events if int(df.at[i, "date"].year) != y]
        out.append({"excluded_year": y, "excluded_events": len(dropped),
                    "horizon_days": horizon, **_distribution(df.loc[rest, f"fwd{horizon}"])})
    return out


def path_stats(df: pd.DataFrame, events: List[int], valid: pd.Series) -> Dict[str, Any]:
    mx, mn = df.loc[events, f"max{PATH_WINDOW}"].dropna(), df.loc[events, f"min{PATH_WINDOW}"].dropna()
    bmx, bmn = df.loc[valid, f"max{PATH_WINDOW}"].dropna(), df.loc[valid, f"min{PATH_WINDOW}"].dropna()
    return {
        "window_days": PATH_WINDOW,
        "touch_up_pct": _pct((mx > 0).mean(), 1) if len(mx) else None,
        "base_touch_up_pct": _pct((bmx > 0).mean(), 1) if len(bmx) else None,
        "mean_max_pct": _pct(mx.mean()) if len(mx) else None,
        "base_mean_max_pct": _pct(bmx.mean()) if len(bmx) else None,
        "hold_pct": _pct((mn > 0).mean(), 1) if len(mn) else None,
        "base_hold_pct": _pct((bmn > 0).mean(), 1) if len(bmn) else None,
        "note": (f"触及 = 未来 {PATH_WINDOW} 个交易日内至少一天收盘高于信号日收盘；"
                 f"不破 = 未来 {PATH_WINDOW} 日最低收盘仍高于信号日收盘。"),
    }


def breadth_stats(df: pd.DataFrame, events: List[int], valid: pd.Series) -> Dict[str, Any]:
    obs = df.loc[events, "next_breadth_up"].dropna()
    base = df.loc[valid, "next_breadth_up"].dropna()
    return {
        "next_day_up_gt_down_pct": _pct(obs.mean(), 1) if len(obs) else None,
        "base_pct": _pct(base.mean(), 1) if len(base) else None,
        "note": "次日上涨家数多于下跌家数的概率——买个股的人真正关心的尺子。",
    }


def drawdown_stats(df: pd.DataFrame, events: List[int], valid: pd.Series) -> Dict[str, Any]:
    """顶部侧口径：未来 N 日最低收盘的分布，**不给方向**。"""
    out: Dict[str, Any] = {"windows": []}
    for n in (5, 10):
        obs, base = df.loc[events, f"min{n}"].dropna(), df.loc[valid, f"min{n}"].dropna()
        if obs.empty:
            continue
        entry: Dict[str, Any] = {
            "window_days": n,
            "n": int(len(obs)),
            "median_pct": _pct(obs.median()),
            "base_median_pct": _pct(base.median()),
            "levels": [],
        }
        for lvl in DRAWDOWN_LEVELS:
            entry["levels"].append({
                "level_pct": _pct(lvl, 1),
                "prob_pct": _pct((obs <= lvl).mean(), 1),
                "base_prob_pct": _pct((base <= lvl).mean(), 1),
            })
        out["windows"].append(entry)
    return out


# ---------------------------------------------------------------------------
# 信号注册表
# ---------------------------------------------------------------------------
def _signal_masks(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """(key, label, side, mask, note) —— side 决定输出方向分布还是回撤分布。"""
    lev_q = _rolling_pct_rank(df["rzye_chg20"])  # 融资余额 20 日增速的滚动分位 = 杠杆加速
    return [
        {
            "key": "pulse_gate",
            "label": "情绪脉冲四腿全中",
            "side": "bottom",
            "mask": df["pulse_gate"] & df["pulse_ready"],
            "note": "s1 上涨占比极低 + s2 跌停占比极高 + s3 涨跌停比极低 + s4 放量杀跌，四条全中。",
        },
        {
            "key": "pulse_two_legs_only",
            "label": "仅 s1&s2 命中、未凑齐四腿（对照组）",
            "side": "bottom",
            "mask": df["s1"] & df["s2"] & (~(df["s3"] & df["s4"])) & df["pulse_ready"],
            "note": "对照组：证明多出来的两条腿在做实事，不是装饰。此组历史上跑输基准。",
            "diagnostic_only": True,
        },
        {
            "key": "top_money_effect_fade",
            "label": "赚钱效应衰减（涨停占比 5 日均 < 20 日均且近 10 日有新高）",
            "side": "top",
            "mask": (df["lu_ma5"] < df["lu_ma20"]) & df["bench_high20_10d"],
            "note": "对应极值轴 p2。",
        },
        {
            "key": "top_volume_fever",
            "label": "量能亢奋（成交额 5 日均 / 20 日均 ≥ 1.20）",
            "side": "top",
            "mask": df["amt_ratio_5_20"] >= 1.20,
            "note": "对应极值轴 p4。",
        },
        {
            "key": "top_limit_up_extreme",
            "label": "涨停占比处于滚动高位极值（≥95 分位）",
            "side": "top",
            "mask": _rolling_pct_rank(df["limit_up_share"]) >= 0.95,
            "note": "赚钱效应过热的直接读数。",
        },
        {
            "key": "top_leverage_accel",
            "label": "杠杆加速（融资余额 20 日增速处于滚动 90 分位以上）",
            "side": "top",
            "mask": lev_q >= 0.90,
            "note": "对应极值轴 p3，是顶部侧五条腿里唯一在旧回放中两个子样本都跑输基准的。",
        },
        {
            "key": "top_leverage_plus_volume",
            "label": "杠杆加速 + 放量（融资增速高位且成交额 5/20 ≥ 1.15）",
            "side": "top",
            "mask": (lev_q >= 0.90) & (df["amt_ratio_5_20"] >= 1.15),
            "note": "叠加组：历史上尾部风险抬升最明显的一组，但样本极薄且集中在子样本后段。",
        },
    ]


def evaluate_signal(df: pd.DataFrame, spec: Dict[str, Any], valid: pd.Series,
                    rng: np.random.Generator, full: bool) -> Dict[str, Any]:
    mask = spec["mask"].fillna(False).astype(bool) & valid
    events = dedup_events(list(df.index[mask]))
    entry: Dict[str, Any] = {
        "key": spec["key"],
        "label": spec["label"],
        "side": spec["side"],
        "note": spec["note"],
        "sample": {
            "events": len(events),
            "raw_hits": int(mask.sum()),
            "dedup_gap_days": DEDUP_GAP,
            "span": (f"{df.loc[valid, 'date'].min():%Y-%m-%d}~{df.loc[valid, 'date'].max():%Y-%m-%d}"
                     if valid.any() else None),
        },
    }
    if not events:
        entry["available"] = False
        entry["reason"] = "历史上没有命中日"
        return entry
    entry["available"] = True

    if spec["side"] == "bottom":
        entry["horizons"] = horizon_stats(df, events, rng, valid)
        entry["path"] = path_stats(df, events, valid)
        entry["breadth"] = breadth_stats(df, events, valid)
        entry["subsample"] = subsample_stats(df, events)
        entry["publishable"], entry["gate_detail"] = _publish_gate(entry)
        if full:
            entry["leave_one_year_out"] = leave_one_year_out(df, events, 3)
    else:
        entry["drawdown"] = drawdown_stats(df, events, valid)
        entry["subsample"] = subsample_stats(df, events)
        # 顶部侧一律不可发布，但门槛明细照算——读者要能看见「差在哪一条」，
        # 而不是只拿到一句"证据不足"。
        n_ok = entry["sample"]["events"] >= MIN_EVENTS
        covered = [g["label"] for g in entry["subsample"] if g["events"] > 0]
        entry["publishable"] = False
        entry["gate_detail"] = {
            "min_events": {"required": MIN_EVENTS, "actual": entry["sample"]["events"], "pass": n_ok},
            "subsample_coverage": {"covered": covered, "pass": len(covered) == len(entry["subsample"])},
            "reason": "顶部侧一律不可发布：本层对顶部只输出回撤风险分布，方向性结论没有证据支撑。",
        }
        entry["direction_note"] = (
            "本组只输出回撤风险分布。**不得写成方向性结论**，也不得进 `==趋势判断==` "
            "的判断词——历史上多数顶部条件之后的前瞻收益仍为正。"
        )
    if full:
        entry["events"] = [f"{df.at[i, 'date']:%Y-%m-%d}" for i in events]
    return entry


def _publish_gate(entry: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """发布门槛三条：样本量 / 子样本一致 / 置换检验。任一不过则不得进判断词。"""
    n_ok = entry["sample"]["events"] >= MIN_EVENTS
    horizons = entry.get("horizons", [])
    perm_ok = any(
        (h.get("permutation") or {}).get("p_mean") is not None
        and h["permutation"]["p_mean"] < PERM_ALPHA
        for h in horizons
    )
    sub = entry.get("subsample", [])
    consistent: List[int] = []
    for n in HORIZONS:
        vals = []
        for grp in sub:
            row = next((h for h in grp["horizons"] if h["horizon_days"] == n), None)
            if row and row.get("n") and row.get("mean_pct") is not None:
                vals.append(row["mean_pct"])
        if len(vals) == len(sub) and vals and all(v > 0 for v in vals):
            consistent.append(n)
    sub_ok = bool(consistent)
    return (n_ok and perm_ok and sub_ok), {
        "min_events": {"required": MIN_EVENTS, "actual": entry["sample"]["events"], "pass": n_ok},
        "permutation": {"alpha": PERM_ALPHA, "pass": perm_ok},
        "subsample_consistent": {"horizons": consistent, "pass": sub_ok},
    }


# ---------------------------------------------------------------------------
# 卡面组装
# ---------------------------------------------------------------------------
def pulse_block(df: pd.DataFrame, row_idx: int) -> Dict[str, Any]:
    r = df.loc[row_idx]
    if not bool(r["pulse_ready"]):
        return {"available": False,
                "reason": f"滚动分位历史不足（需要 ≥{ROLL_MIN} 个交易日）"}

    def leg(key: str, label: str, value: Optional[float], pct: Optional[float],
            thr: Optional[float], line_q: float, op: str, unit: str) -> Dict[str, Any]:
        return {
            "leg": key,
            "label": label,
            "hit": bool(r[key]),
            "value": round(float(value), 3) if value is not None and np.isfinite(value) else None,
            "percentile_pct": _pct(pct, 1) if pct is not None and np.isfinite(pct) else None,
            "threshold_pct": _pct(line_q, 1),
            "threshold_value": round(float(thr), 3) if thr is not None and np.isfinite(thr) else None,
            "direction": op,
            "unit": unit,
        }

    legs = [
        leg("s1", "上涨家数占比处于低位极值", 100 * r["rise_share"], r["q_rise_share"],
            100 * r["thr_rise_share"] if pd.notna(r["thr_rise_share"]) else None, S1_Q, "le", "%"),
        leg("s2", "跌停家数占比处于高位极值", 100 * r["limit_down_share"], r["q_limit_down_share"],
            100 * r["thr_limit_down_share"] if pd.notna(r["thr_limit_down_share"]) else None,
            S2_Q, "ge", "%"),
        leg("s3", "涨停/跌停比处于低位极值", r["lu_ld_ratio"], r["q_lu_ld_ratio"],
            r["thr_lu_ld_ratio"] if pd.notna(r["thr_lu_ld_ratio"]) else None, S3_Q, "le", "倍"),
        {
            "leg": "s4",
            "label": f"放量杀跌（成交额 ≥ 前20日均量 且 上涨占比 ≤ {int(100 * S4_RISE)}%）",
            "hit": bool(r["s4"]),
            "value": round(float(r["amt_ratio"]), 3) if pd.notna(r["amt_ratio"]) else None,
            "rise_share_pct": _pct(r["rise_share"], 1),
            "threshold_value": S4_AMT,
            "direction": "ge",
            "unit": "倍",
        },
    ]
    return {
        "available": True,
        "gate": bool(r["pulse_gate"]),
        "legs_hit": int(r["pulse_legs"]),
        "max_legs": 4,
        "legs": legs,
        "roll_window_days": ROLL_WINDOW,
        "note": ("四腿合取门槛，不是分数。前瞻分布不单调——只有四条全中那一档有效，"
                 "「腿数 ≥ 2」过不了子样本检验，不得据此定性。"),
    }


def pulse_history(df: pd.DataFrame, row_idx: int, days: int = HISTORY_DAYS) -> List[Dict[str, Any]]:
    """最近 N 个交易日的脉冲轨迹，供 HTML 与另外两根轴画在同一条日期轴上。

    只给 `legs`（命中腿数）与 `gate`（四腿全中）两个字段：腿数是**描述**，门槛才是
    判据。渲染层据此把门槛日画成断点，而不是把腿数当连续强度画成渐变——那正是
    方法论里禁止的读法。
    """
    lo = max(0, row_idx - days + 1)
    out: List[Dict[str, Any]] = []
    for i in range(lo, row_idx + 1):
        r = df.loc[i]
        if not bool(r["pulse_ready"]):
            out.append({"date": f"{r['date']:%Y-%m-%d}", "legs": None, "gate": None})
            continue
        out.append({
            "date": f"{r['date']:%Y-%m-%d}",
            "legs": int(r["pulse_legs"]),
            "gate": bool(r["pulse_gate"]),
        })
    return out


def normalize_asof(asof: Optional[str]) -> date:
    if not asof:
        return date.today()
    return datetime.strptime(asof.strip().replace("-", ""), "%Y%m%d").date()


def build_block(asof: Optional[str] = None, full: bool = False) -> Dict[str, Any]:
    """生成模块 1 的 pulse + forward_odds 证据区块。"""
    asof_date = normalize_asof(asof)
    if BACKEND == Backend.SQLITE:
        return {"available": False, "reason": "forward odds requires PostgreSQL backend"}
    try:
        with get_connection() as conn:
            raw = load_market_frame(conn)
            if raw.empty:
                return {"available": False, "reason": "market_history / 基准指数无可用数据"}
            stats = _compute(raw, asof_date, full)
            _persist(conn, stats)
            stats.pop("_persist_signals", None)
    except Exception as exc:  # noqa: BLE001  研报不因这张卡失败而中断
        return {"available": False, "reason": f"forward odds unavailable: {exc}"}
    return stats


def _features_through_asof(df: pd.DataFrame, asof_date: date) -> pd.DataFrame:
    """截断到 asof 后再生成前瞻标签，历史回放不得读取未来已落库行情。"""
    asof_ts = pd.Timestamp(asof_date)
    upto = df[df["date"] <= asof_ts].copy().reset_index(drop=True)
    if upto.empty:
        return upto
    return add_features(upto)


def _compute(df: pd.DataFrame, asof_date: date, full: bool) -> Dict[str, Any]:
    df = _features_through_asof(df, asof_date)
    if df.empty:
        return {"available": False, "reason": f"基准序列在 {asof_date} 之前没有数据"}
    asof_ts = pd.Timestamp(asof_date)
    row_idx = int(df.index[-1])
    data_through = df.at[row_idx, "date"].date()

    # 统计只用 asof 之前的日子；前瞻收益天然只在窗口走完的日子上有值。
    # 顶部侧信号也用同一个 valid 作基准样本——不是因为它们依赖 pulse，而是为了让
    # 所有信号和「全样本基准」共享同一个分母，否则各信号的对照不可比。
    valid = (df["date"] <= asof_ts) & df["pulse_ready"]
    rng = np.random.default_rng(PERM_SEED)
    signals: List[Dict[str, Any]] = []
    full_signals: List[Dict[str, Any]] = []   # 落库用：存根不进表，否则缓存失去意义
    for spec in _signal_masks(df):
        # 与统计口径同源：命中判定也过 valid，避免卡面报了一个统计里不算数的命中
        hit_today = bool((spec["mask"].fillna(False).astype(bool) & valid).iloc[row_idx])
        entry = evaluate_signal(df, spec, valid, rng, full)
        entry["hit_today"] = hit_today
        full_signals.append(entry)
        if spec.get("diagnostic_only") and not full:
            continue
        # 未命中的信号在证据包里只留存根：全量分布对当日判断没有增量，却会吃掉
        # 决策包预算。想看完整分布跑 `--full`，或读 references/methodology/forward_odds.md。
        if not hit_today and not full:
            entry = {
                "key": entry["key"], "label": entry["label"], "side": entry["side"],
                "hit_today": False, "available": entry.get("available", False),
                "sample_events": entry.get("sample", {}).get("events"),
                "publishable": entry.get("publishable", False),
                "detail_omitted": "未命中，分布明细已省略（跑 --full 或读方法论文档）",
            }
        signals.append(entry)

    bench_source = f"{BENCH_PRIMARY}+{BENCH_TAIL}"
    return {
        "available": True,
        "asof": str(asof_date),
        "data_through": str(data_through),
        "is_current": data_through == asof_date,
        "benchmark": {
            "ts_code": bench_source,
            "label": f"{BENCH_LABEL}（Tushare 打底 + Baostock 补新尾）",
            "reference": {"ts_code": BENCH_REF, "label": BENCH_REF_LABEL},
            "why": "指数会系统性低估短线窗口：同一信号下中证1000 的 +3 日均值约为上证的两倍多。",
        },
        "pulse": pulse_block(df, row_idx),
        "pulse_history": pulse_history(df, row_idx),
        "max_legs": 4,
        "signals": signals,
        "_persist_signals": full_signals,
        "method": {
            "roll_window_days": ROLL_WINDOW,
            "roll_min_days": ROLL_MIN,
            "dedup_gap_days": DEDUP_GAP,
            "subsample_split": str(SUBSAMPLE_SPLIT),
            "permutation_draws": PERM_DRAWS,
            "publish_gate": (f"事件簇去重后 n ≥ {MIN_EVENTS} ｜ 两个子样本方向一致 ｜ "
                             f"置换检验 p < {PERM_ALPHA}"),
            "no_lookahead": "分位线只用信号日之前的历史算，阈值随日滚动。",
        },
        "discipline": (
            "本层输出的是条件分布，不是买卖建议。引用时必须带样本量与基准对照；"
            "`publishable=false` 的信号只能作为背景读数，不得进 `==趋势判断==` 的判断词。"
        ),
    }


def _persist(conn, stats: Dict[str, Any]) -> None:
    """把当日统计落库，供审计与回归比对；失败不影响出卡。"""
    if not stats.get("available"):
        return
    try:
        with conn.cursor() as cur:
            cur.execute(TABLE_DDL)
            for sig in stats.get("_persist_signals") or stats.get("signals", []):
                cur.execute(
                    "INSERT INTO dms_forward_odds_stats (signal_key, stats_through, payload) "
                    "VALUES (%s, %s, %s) ON CONFLICT (signal_key, stats_through) "
                    "DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()",
                    (sig["key"], stats["data_through"], json.dumps(sig, ensure_ascii=False, default=str)),
                )
    except Exception as exc:  # noqa: BLE001
        print(f"[forward_odds] 落库跳过：{exc}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="前瞻概率层：情绪脉冲 pulse + 条件分布 forward_odds")
    ap.add_argument("--asof", default=None, help="分析日 YYYYMMDD 或 YYYY-MM-DD，默认今天；含当日数据")
    ap.add_argument("--full", action="store_true", help="附带事件清单与逐年留一检验（体积较大）")
    args = ap.parse_args()
    block = build_block(args.asof, args.full)
    print(json.dumps(block, ensure_ascii=False, indent=2, default=str))
    return 0 if block.get("available") else 1


if __name__ == "__main__":
    sys.exit(main())
