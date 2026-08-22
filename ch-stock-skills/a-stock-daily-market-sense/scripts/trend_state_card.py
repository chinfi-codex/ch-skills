#!/usr/bin/env python3
"""趋势状态卡（trend_state_card）——盘后研报模块 1 的趋势轴机判区块。

与 AlphaVault `system/tools/trend_state.py`（盘前计划工具）同源同参：
五个趋势计数器（破位 / 融资连负 / 缩量 / 反弹质检戳 / 顶背离计数）+
六档状态机（进攻 / 标准 / 谨慎 / 退潮 / 深度退潮 / 冰点）。
升档迟滞只作用于「升出退潮梯队」（退潮/深度退潮/冰点 → 谨慎，须 exit_ok
右侧确认）；梯队内部档位按日调整——非退潮态下跌停占比 ≥ 恐慌线的
恐慌日当日直进退潮（闪崩放量、融资 T-1 滞后时三组难当日凑够两组），
冰点以跌停占比 ≥ 恐慌线进入（退潮态内），以跌停占比回落至解除线以下
且涨>跌（广度修复）当日解除，退回退潮/深度退潮，
避免单日恐慌把状态锁死在冰点多日（融资 T-1 滞后会使 exit_ok 在恐慌
结束后仍长期不可达）。

**阈值口径（2026-08 起）**：所有家数腿都按占全市场家数的比例判，所有融资腿
都按占融资余额的 bp 判，两者再叠一层 500 交易日滚动分位（历史不足 250 天时
退回固定水平）。原因见 `evals/trend_state_review_2026-08.md`：上市家数五年里
从 4050 涨到 5493、融资余额从 1.4 万亿涨到 2.6 万亿，写死的绝对阈值实际严格度
一路漂移，「上涨 >3500 家」从 6.2% 的交易日触发变成 31.2%，谨慎/退潮档因此
虚胖到占掉一半时间。每日生效的阈值随卡一起输出在 `thresholds` 里。
退潮/深度退潮日附带确定性相位修饰 phase（修复中 / 企稳 / 恶化中，规则见
run_state_machine 末尾）：主档保持迟滞、不追单日，相位承载边际方向，
表达「退潮·修复中」这类过渡期语义；冰点与非退潮态 phase 为空。
反弹质检戳的融资腿（T-1 滞后）在恐慌次日（昨日跌停占比 ≥ 恐慌线）
不参判——彼时融资读数反映恐慌日本身，近乎自动触发，只按量能腿判定。
两者唯一差异是时间语义：本脚本为盘后视角，`--asof` **含当日**收盘数据；
盘前工具取计划日之前的数据。参数常量对应经验法则 R-016 / R-017 / R-018，
改参数须与盘前工具及规则页同步。

融资滞后口径（本轮修正的核心，见 margin_data_date / 升档守卫 / 相位配平）：
market_panel 写 market_history 时取的是**前一交易日**的融资，因此第 d 行的融资
读数描述的是 d-1 的杠杆行为，不含当日信息。数据日现在由 `market_history.
margin_data_date` 显式落库（老行没有这列时才退回"前一交易日"的推断）——
过去只靠约定推断，实测线上 174 天里有 80 天存的其实是当日读数。
这条滞后在持续退潮期无害（方向一致），但在**转向日**会把前一日的悲观读数按在
一个盘面相反的日子上。为此：
  1. `margin_data_date` 随读数一起输出，卡面必须标注融资读数的实际数据日；
  2. 升入深度退潮时加守卫——第三组恰为「融资」且当日出现广度反转
     （涨>跌 且 涨停>跌停）时推迟一日再判（`deep_escalation_deferred`），
     等融资数据推进到能反映当日盘面的那一天，与「升档须右侧确认」对称；
  3. 相位改为 5v5 对称配平，当日证据（广度 / 20 日线缺口 / 量能 / 跌停）四票，
     滞后融资一票，滞后腿不能单独决定方向。

口径备注（与 market_history 写入口径一致）：
  - market_history.amount 单位千元；margin_net_buy 单位元（融资为 T-1 滞后口径）。
  - 「成交额 vs 20 日均」为前 20 个交易日均值（不含当日）。
  - 指数 MA20 为含当日的 20 日收盘均线。
  - `ma20_gap` 为双指数对各自 MA20 的最大负偏离（正值 = 在均线下方多少），
    较前日收窄 / 扩大即为相位的「缺口」腿。

输出 JSON 为确定性证据（只含客观读数，不含任何仓位或操作字段）：
available / data_through / state / state_days / counters / ref_day /
phase_score / state_note / exit_progress / history。模型只读、不得改写读数；
趋势定性由模型在模块 1 正文中给出，且同样不写仓位与买卖建议。

用法：
  python scripts/trend_state_card.py --asof 20260717
  python scripts/trend_state_card.py            # 默认今天
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))
from db_core import BACKEND, Backend, get_connection  # noqa: E402

INDEXES = {
    "000001.SH": "上证",
    "399006.SZ": "创业板",
    "000688.SH": "科创50",
}
TREND_PAIR = ("000001.SH", "399006.SZ")  # 破位计数用的双指数

# ---------------------------------------------------------------------------
# R-016 / R-017 / R-018 参数（与 AlphaVault trend_state.py 及规则页一致，改动须三处同步）
#
# 2026-08 口径切换：原先的绝对家数 / 绝对金额阈值一律换成占比与滚动分位。
# 依据是 evals/trend_state_review_2026-08.md 的 5 年回放——上市家数从 4050 只
# 长到 5493 只、融资余额从 1.4 万亿长到 2.6 万亿，同一条写死的规则严格度五年里
# 变了好几倍：
#   - 「上涨 >3500 家」2020 年是 86% 个股（6.2% 的交易日触发），2026 年只剩
#     64%（31.2% 的交易日触发），派发反弹戳的触发率跟着从 4.8% 涨到 19.4%，
#     谨慎档的膨胀一大半由它推动；
#   - 「融资净买入 ≤ −300 亿」2020 年是余额的 212bp，2026 年只有 112bp。
# 现在的口径：家数 → 占全市场家数比例；融资 → 占融资余额的 bp。两者都再叠一层
# 滚动分位（历史够长时用分位，不够时退回下面这组固定水平，取值来自同一份回放的
# 全样本分位数）。
# ---------------------------------------------------------------------------
MARGIN_NEG_DAYS = 3
MARGIN_NEG_CUM_BP = -180.0     # 融资连负累计（占融资余额 bp；旧值 −300 亿 ≈ 2020 年 212bp / 2026 年 112bp）
MARGIN_NEG_CUM_YI = -300.0     # 无融资余额可归一时的兜底（亿元，旧口径）
SHRINK_RATIO = 0.90
SHRINK_DAYS = 3
BREAK_DAYS = 2
BIGUP_RISE_SHARE = 0.78        # 大涨日：上涨家数占比（全样本 90 分位；旧值 3500 家）
BIGUP_RISE_Q = 0.90            # 历史够长时改用滚动 90 分位
BIGUP_IDX_PCT = 2.0
QC_MARGIN_BP = -60.0           # 反弹质检戳的融资腿（≈全样本 5 分位；旧值 −100 亿）
QC_MARGIN_Q = 0.05
DIV_RISE_SHARE = 0.30          # 顶背离的广度腿：上涨家数占比（旧值 1500 家 ≈ 2026 年 27%）
DIV_MARGIN_BP = -90.0          # 顶背离的融资腿（≈全样本 2.5 分位；旧值 −150 亿）
DIV_MARGIN_Q = 0.025
DIV_WINDOW = 10
DIV_TRIGGER = 2
PANIC_LIMIT_DOWN_SHARE = 0.035   # 恐慌日：跌停家数占比（旧值 200 家 ≈ 2020 年 4.9% / 2026 年 3.6%）
ICE_EXIT_LIMIT_DOWN_SHARE = 0.018  # 冰点解除线（入 3.5% / 出 1.8%，迟滞防抖）

BASELINE_CAL_DAYS = 1100       # 滚动分位的取数窗口（≈3 年自然日）
BASELINE_WINDOW = 500          # 滚动分位窗口（交易日）
BASELINE_MIN_PERIODS = 250     # 不足此长度就退回固定水平
HISTORY_DAYS = 30              # 输出的档位轨迹长度（HTML 时间轴按这个宽度画）

# 家数口径兜底：market_history 缺 flat 列（算不出全市场家数）时退回旧的绝对家数。
# 只是防御，不是主口径——正常路径上 total 一定有值。
LEGACY_PANIC_LIMIT_DOWN = 200
LEGACY_ICE_EXIT_LIMIT_DOWN = 100


def default_thresholds() -> Dict[str, Any]:
    """历史不足或离线构造 DayRow 时生效的固定阈值。"""
    return {
        "bigup_rise_share": BIGUP_RISE_SHARE,
        "qc_margin_bp": QC_MARGIN_BP,
        "div_margin_bp": DIV_MARGIN_BP,
        "panic_limit_down_share": PANIC_LIMIT_DOWN_SHARE,
        "ice_exit_limit_down_share": ICE_EXIT_LIMIT_DOWN_SHARE,
        "div_rise_share": DIV_RISE_SHARE,
        "margin_neg_cum_bp": MARGIN_NEG_CUM_BP,
        "source": "fixed_fallback",
        "margin_unit": "yi",
    }


@dataclass
class DayRow:
    d: date
    rise: int
    fall: int
    limit_up: int
    limit_down: int
    amount_qianyuan: float          # 千元
    margin_yi: float                # 亿元（T-1 口径，实际数据日见 margin_data_date）
    margin_data_date: Optional[date] = None  # 该融资读数描述的交易日 = 前一交易日
    total: int = 0                  # 当日有交易的家数（rise+fall+flat），占比口径的分母
    margin_bp: Optional[float] = None   # 融资净买入 / 融资余额（bp），无余额数据时为 None
    thr: dict = field(default_factory=default_thresholds)  # 该日生效的阈值（滚动分位或固定水平）
    amt_ma20_prev: Optional[float] = None   # 前 20 日均（千元，不含当日）
    idx_close: dict = field(default_factory=dict)
    idx_ma20: dict = field(default_factory=dict)
    idx_pct: dict = field(default_factory=dict)
    idx_high20: dict = field(default_factory=dict)

    rise_share: float = 0.0
    limit_down_share: float = 0.0

    below20_both: bool = False
    below20_any: bool = False
    ma20_gap: Optional[float] = None   # 双指数对 MA20 的最大负偏离比例，正值 = 在线下
    break_days: int = 0
    margin_neg_days: int = 0
    margin_neg_cum_yi: float = 0.0
    margin_neg_cum_bp: float = 0.0
    shrink: bool = False
    shrink_days: int = 0
    big_up: bool = False
    rebound_stamp: bool = False     # R-018 派发反弹戳
    divergence: bool = False        # R-017 单日背离
    div_count10: int = 0
    groups_hit: list = field(default_factory=list)
    state: str = "标准"
    phase: str = ""                 # 退潮梯队内的相位修饰：修复中 / 企稳 / 恶化中
    phase_repair: List[str] = field(default_factory=list)   # 相位修复腿命中项
    phase_worsen: List[str] = field(default_factory=list)   # 相位恶化腿命中项
    deep_escalation_deferred: bool = False   # 升入深度退潮被滞后融资守卫推迟
    state_note: str = ""
    exit_progress: str = ""


def _has_column(conn, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
            (table, column),
        )
        return cur.fetchone() is not None


def fetch_margin_balance(conn, start: date, end: date) -> Dict[date, float]:
    """从 stock_margin 取按交易日汇总的融资余额（亿元），用于把净买入归一成 bp。

    stock_margin 是按真实交易日存的分交易所明细，比 market_history 那一列干净：
    后者的滞后口径历史上并不稳定。分交易所分批发布的日期（交易所数少于窗口众数）
    汇总口径不全，直接剔除。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, COUNT(DISTINCT exchange_id) AS n, SUM(rzye) AS rzye
            FROM stock_margin
            WHERE trade_date >= %s AND trade_date <= %s AND rzye IS NOT NULL
            GROUP BY trade_date
            """,
            (start, end),
        )
        rows = cur.fetchall()
    if not rows:
        return {}
    counts: Dict[int, int] = {}
    for _, n, _rzye in rows:
        counts[int(n)] = counts.get(int(n), 0) + 1
    expected = max(counts, key=lambda k: counts[k])
    return {r[0]: float(r[2]) / 1e8 for r in rows if int(r[1]) == expected and r[2] is not None}


def compute_thresholds(series: List[Dict[str, Any]]) -> Dict[date, Dict[str, float]]:
    """逐日算出该日生效的阈值：历史够长用滚动分位，不够用固定水平。

    只用当日及之前的数据（pandas rolling 本身就是向后看的），无未来函数。
    series 按日期升序，每项含 rise_share / margin_metric（bp 优先，退回亿元）。
    """
    import pandas as pd  # 本模块其余部分不依赖 pandas，按需引入

    if not series:
        return {}
    df = pd.DataFrame(series)
    out: Dict[date, Dict[str, float]] = {}

    def roll(col: str, q: float, fallback: float) -> "pd.Series":
        if col not in df.columns:
            return pd.Series([fallback] * len(df))
        s = df[col].rolling(BASELINE_WINDOW, min_periods=BASELINE_MIN_PERIODS).quantile(q)
        return s.fillna(fallback)

    bigup = roll("rise_share", BIGUP_RISE_Q, BIGUP_RISE_SHARE)
    has_bp = "margin_bp" in df.columns and df["margin_bp"].notna().sum() >= BASELINE_MIN_PERIODS
    if has_bp:
        qc = roll("margin_bp", QC_MARGIN_Q, QC_MARGIN_BP)
        div = roll("margin_bp", DIV_MARGIN_Q, DIV_MARGIN_BP)
    else:
        qc = pd.Series([QC_MARGIN_BP] * len(df))
        div = pd.Series([DIV_MARGIN_BP] * len(df))
    source = "rolling_quantile" if len(df) >= BASELINE_MIN_PERIODS else "fixed_fallback"
    for i, d in enumerate(df["d"]):
        out[d] = {
            "bigup_rise_share": float(bigup.iloc[i]),
            "qc_margin_bp": float(qc.iloc[i]),
            "div_margin_bp": float(div.iloc[i]),
            "panic_limit_down_share": PANIC_LIMIT_DOWN_SHARE,
            "ice_exit_limit_down_share": ICE_EXIT_LIMIT_DOWN_SHARE,
            "div_rise_share": DIV_RISE_SHARE,
            "margin_neg_cum_bp": MARGIN_NEG_CUM_BP,
            "source": source,
            "margin_unit": "bp" if has_bp else "yi",
        }
    return out


def fetch_baseline_series(conn, boundary_exclusive: date) -> List[Dict[str, Any]]:
    """滚动分位的基准序列：只取原始读数，不做连续性截断（缺口不影响分位）。"""
    start = boundary_exclusive - timedelta(days=BASELINE_CAL_DAYS)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT date, rise, fall, flat, margin_net_buy
            FROM market_history
            WHERE date >= %s AND date < %s
            ORDER BY date
            """,
            (start, boundary_exclusive),
        )
        rows = cur.fetchall()
    balance = fetch_margin_balance(conn, start, boundary_exclusive)
    # market_history 第 d 行的融资描述前一交易日，余额也要按同一天对齐
    dates = [r[0] for r in rows]
    prev_of = {d: dates[i - 1] for i, d in enumerate(dates) if i > 0}
    series: List[Dict[str, Any]] = []
    for r in rows:
        d, rise, fall, flat, margin = r
        total = (rise or 0) + (fall or 0) + (flat or 0)
        item: Dict[str, Any] = {"d": d, "rise_share": (rise or 0) / total if total else None}
        rzye = balance.get(prev_of.get(d))
        if margin is not None and rzye:
            item["margin_bp"] = float(margin) / 1e8 / rzye * 1e4
        series.append(item)
    return series


def fetch(conn, boundary_exclusive: date, lookback_cal_days: int = 240) -> Tuple[List[DayRow], List[date]]:
    """返回 (days, gaps)。gaps 是位于最后可用日之前、因缺量额/融资/指数数据
    被剔除的交易日；存在缺口时 days 已截断为最后缺口之后的干净连续段。"""
    start = boundary_exclusive - timedelta(days=lookback_cal_days)
    has_margin_date = _has_column(conn, "market_history", "margin_data_date")
    margin_date_col = "margin_data_date" if has_margin_date else "NULL::date AS margin_data_date"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT date, rise, fall, limit_up, limit_down, amount, margin_net_buy,
                   flat, {margin_date_col}
            FROM market_history
            WHERE date >= %s AND date < %s
            ORDER BY date
            """,
            (start, boundary_exclusive),
        )
        all_mh_dates: List[date] = []
        mh = {}
        for r in cur.fetchall():
            all_mh_dates.append(r[0])
            if r[5] is not None and r[6] is not None:
                mh[r[0]] = r
        cur.execute(
            """
            SELECT ts_code, trade_date, close, pct_chg
            FROM stock_index_daily
            WHERE trade_date >= %s AND trade_date < %s AND ts_code = ANY(%s)
            ORDER BY trade_date
            """,
            (start, boundary_exclusive, list(INDEXES)),
        )
        idx: Dict[str, list] = {k: [] for k in INDEXES}
        for ts, td, close, pct in cur.fetchall():
            idx[ts].append((td, float(close), float(pct or 0)))

    idx_by_day: Dict[date, dict] = {}
    for ts, rows in idx.items():
        closes = [c for _, c, _ in rows]
        for i, (td, close, pct) in enumerate(rows):
            window = closes[max(0, i - 19): i + 1]
            info = idx_by_day.setdefault(td, {})
            info[ts] = {
                "close": close,
                "pct": pct,
                "ma20": sum(window) / len(window) if len(window) == 20 else None,
                "high20": len(window) == 20 and close >= max(window),
            }

    thresholds = compute_thresholds(fetch_baseline_series(conn, boundary_exclusive))
    balance = fetch_margin_balance(conn, start, boundary_exclusive)

    days: List[DayRow] = []
    dates = sorted(d for d in mh if d in idx_by_day and all(ts in idx_by_day[d] for ts in TREND_PAIR))
    amounts: List[float] = []
    for d in dates:
        _, rise, fall, lu, ld, amount, margin, flat, margin_date = mh[d]
        total = (rise or 0) + (fall or 0) + (flat or 0)
        row = DayRow(
            d=d, rise=rise or 0, fall=fall or 0, limit_up=lu or 0, limit_down=ld or 0,
            amount_qianyuan=float(amount), margin_yi=float(margin) / 1e8, total=total,
        )
        if len(amounts) >= 20:
            row.amt_ma20_prev = sum(amounts[-20:]) / 20
        info = idx_by_day[d]
        for ts in INDEXES:
            if ts in info:
                row.idx_close[ts] = info[ts]["close"]
                row.idx_ma20[ts] = info[ts]["ma20"]
                row.idx_pct[ts] = info[ts]["pct"]
                row.idx_high20[ts] = info[ts]["high20"]
        # 融资数据日优先读显式落库的列；老行没有这一列时才退回"前一交易日"的推断
        row.margin_data_date = margin_date or (days[-1].d if days else None)
        rzye = balance.get(row.margin_data_date)
        if rzye:
            row.margin_bp = row.margin_yi / rzye * 1e4
        row.thr = thresholds.get(d, {
            "bigup_rise_share": BIGUP_RISE_SHARE,
            "qc_margin_bp": QC_MARGIN_BP,
            "div_margin_bp": DIV_MARGIN_BP,
            "panic_limit_down_share": PANIC_LIMIT_DOWN_SHARE,
            "ice_exit_limit_down_share": ICE_EXIT_LIMIT_DOWN_SHARE,
            "div_rise_share": DIV_RISE_SHARE,
            "margin_neg_cum_bp": MARGIN_NEG_CUM_BP,
            "source": "fixed_fallback",
            "margin_unit": "bp" if rzye else "yi",
        })
        amounts.append(float(amount))
        days.append(row)
    universe = sorted(set(all_mh_dates) | set(idx_by_day))
    kept = set(dates)
    gaps = [d for d in universe if dates and d < dates[-1] and d not in kept]
    if gaps:
        # 内部缺口会污染连续计数器：截断到最后一个缺口之后，只用干净的连续段
        days = [r for r in days if r.d > gaps[-1]]
        amounts = [r.amount_qianyuan for r in days]
        for i, r in enumerate(days):
            r.amt_ma20_prev = sum(amounts[i - 20:i]) / 20 if i >= 20 else None
    return days, gaps


def is_panic(r: DayRow) -> bool:
    """恐慌日：跌停占比越线。缺全市场家数时退回旧的绝对家数口径。"""
    if r.total:
        return r.limit_down_share >= r.thr["panic_limit_down_share"]
    return r.limit_down >= LEGACY_PANIC_LIMIT_DOWN


def limit_down_easing(r: DayRow) -> bool:
    """跌停回落到冰点解除线以下。"""
    if r.total:
        return r.limit_down_share < r.thr["ice_exit_limit_down_share"]
    return r.limit_down < LEGACY_ICE_EXIT_LIMIT_DOWN


def _margin_below(r: DayRow, bp_threshold: float) -> bool:
    """融资腿：有余额数据就按 bp 判，没有就退回旧的亿元口径。

    bp 阈值到亿元的换算按"2024 年前后融资余额约 1.5 万亿"取，只在缺余额数据
    时兜底，不作为主口径。
    """
    if r.margin_bp is not None:
        return r.margin_bp < bp_threshold
    return r.margin_yi < bp_threshold / 1e4 * 15000.0


def derive(days: List[DayRow]) -> None:
    for i, r in enumerate(days):
        prev = days[i - 1] if i > 0 else None
        r.rise_share = (r.rise / r.total) if r.total else 0.0
        r.limit_down_share = (r.limit_down / r.total) if r.total else 0.0

        below = {
            ts: (r.idx_ma20.get(ts) is not None and r.idx_close.get(ts, 0) < r.idx_ma20[ts])
            for ts in TREND_PAIR
        }
        r.below20_both = all(below.values())
        r.below20_any = any(below.values())
        r.break_days = (prev.break_days + 1) if (r.below20_both and prev) else (1 if r.below20_both else 0)

        # 对 MA20 的最大负偏离：破位期间 above_both 恒为假，用缺口的收窄/扩大
        # 承载「指数正在往均线修复 / 继续离开均线」这一当日边际方向
        gaps = [
            (r.idx_ma20[ts] - r.idx_close[ts]) / r.idx_ma20[ts]
            for ts in TREND_PAIR
            if r.idx_ma20.get(ts) and ts in r.idx_close
        ]
        r.ma20_gap = max(gaps) if len(gaps) == len(TREND_PAIR) else None

        if r.margin_yi < 0:
            r.margin_neg_days = (prev.margin_neg_days + 1) if prev and prev.margin_neg_days else 1
            r.margin_neg_cum_yi = (prev.margin_neg_cum_yi if prev and prev.margin_neg_days else 0.0) + r.margin_yi
            r.margin_neg_cum_bp = (
                (prev.margin_neg_cum_bp if prev and prev.margin_neg_days else 0.0)
                + (r.margin_bp if r.margin_bp is not None else 0.0)
            )
        else:
            r.margin_neg_days = 0
            r.margin_neg_cum_yi = 0.0
            r.margin_neg_cum_bp = 0.0

        r.shrink = r.amt_ma20_prev is not None and r.amount_qianyuan < SHRINK_RATIO * r.amt_ma20_prev
        r.shrink_days = (prev.shrink_days + 1) if (r.shrink and prev) else (1 if r.shrink else 0)

        idx_pct_max = max((r.idx_pct.get(ts, 0) for ts in TREND_PAIR), default=0)
        r.big_up = r.rise_share > r.thr["bigup_rise_share"] or idx_pct_max > BIGUP_IDX_PCT
        weak_volume = r.amt_ma20_prev is not None and r.amount_qianyuan < r.amt_ma20_prev
        # 融资腿（T-1 滞后）在恐慌次日失真：跌停占比 ≥PANIC 的次日，融资读数反映的是
        # 恐慌日本身，近乎自动触发，此时只按量能腿判定反弹戳
        panic_yesterday = prev is not None and is_panic(prev)
        margin_leg = _margin_below(r, r.thr["qc_margin_bp"]) and not panic_yesterday
        r.rebound_stamp = r.big_up and (weak_volume or margin_leg)

        new_high = any(r.idx_high20.get(ts) for ts in INDEXES)
        r.divergence = new_high and (
            r.rise_share < r.thr["div_rise_share"] or _margin_below(r, r.thr["div_margin_bp"])
        )
        r.div_count10 = sum(1 for x in days[max(0, i - DIV_WINDOW + 1): i + 1] if x.divergence)

        groups = []
        if r.break_days >= BREAK_DAYS:
            groups.append("破位")
        margin_cum_hit = (
            r.margin_neg_cum_bp <= r.thr["margin_neg_cum_bp"] if r.margin_bp is not None
            else r.margin_neg_cum_yi <= MARGIN_NEG_CUM_YI
        )
        if r.margin_neg_days >= MARGIN_NEG_DAYS or margin_cum_hit:
            groups.append("融资")
        if r.shrink_days >= SHRINK_DAYS:
            groups.append("缩量")
        r.groups_hit = groups


def retreat_tier(r: DayRow, prev_state: str) -> str:
    """退潮梯队内定档：三组全中记深度退潮，否则退潮；带滞后融资升档守卫。

    守卫动机：`margin_yi` 是 T-1 口径，第 d 行描述的是 d-1 的杠杆行为。持续退潮
    期间方向一致、无害；但在转向日，它会让「融资」组带着前一日的悲观读数，在一个
    盘面已经反向的日子上把档位从退潮压到深度退潮——这一票里没有任何当日信息。
    因此当第三组恰为「融资」、且当日出现广度反转（涨>跌 且 涨停>跌停）时，本日
    不升入深度退潮，推迟一日：次日的融资读数正好描述今日，届时若杠杆确实在撤，
    深度退潮照记不误。这与「升出退潮梯队须右侧确认」是同一条对称原则——降档同样
    不该由不含当日信息的读数单独推动。已在深度退潮的日子不受守卫影响（非升档）。
    """
    if len(r.groups_hit) < 3:
        return "退潮"
    breadth_reversal = r.rise > r.fall and r.limit_up > r.limit_down
    if "融资" in r.groups_hit and prev_state != "深度退潮" and breadth_reversal:
        r.deep_escalation_deferred = True
        margin_day = r.margin_data_date.strftime("%m-%d") if r.margin_data_date else "前一交易日"
        r.state_note = (
            f"条件组 3/3，但第三组「融资」来自 {margin_day} 的 T-1 读数、不含当日；"
            f"当日广度反转（涨 {r.rise}/跌 {r.fall}、涨停 {r.limit_up}/跌停 {r.limit_down}），"
            "深度退潮升档推迟一日，待融资数据推进到能反映当日盘面再判"
        )
        return "退潮"
    return "深度退潮"


def run_state_machine(days: List[DayRow]) -> None:
    state = "标准"
    for i, r in enumerate(days):
        prev = days[i - 1] if i > 0 else None
        # 幂等：允许对同一批 DayRow 重复跑状态机而不累积相位命中项
        r.phase, r.phase_repair, r.phase_worsen = "", [], []
        r.deep_escalation_deferred, r.state_note, r.exit_progress = False, "", ""
        in_retreat = state in ("退潮", "深度退潮", "冰点")

        margin_pos_2d = r.margin_yi > 0 and prev is not None and prev.margin_yi > 0
        above_both = all(
            r.idx_ma20.get(ts) is not None and r.idx_close.get(ts, 0) > r.idx_ma20[ts]
            for ts in TREND_PAIR
        )
        vol_ok = r.amt_ma20_prev is not None and r.amount_qianyuan >= r.amt_ma20_prev
        # exit_ok 末项 not rebound_stamp 为防御性写法：反弹戳要求缩量或融资 < -100 亿，
        # 与 vol_ok / margin_pos_2d 互斥，逻辑上冗余，保留以显式表达"弱质反弹不算解除"
        exit_ok = margin_pos_2d and above_both and vol_ok and not r.rebound_stamp

        panic = is_panic(r)
        ice_exit_share = r.thr["ice_exit_limit_down_share"]

        if in_retreat:
            if exit_ok:
                state = "谨慎"
            else:
                if panic:
                    state = "冰点"
                elif state == "冰点":
                    # 冰点解除：恐慌退潮（跌停占比 < 解除线）且广度修复（涨>跌），
                    # 当日退回退潮梯队；不满足则维持冰点
                    ice_clear = limit_down_easing(r) and r.rise > r.fall
                    if not ice_clear:
                        state = "冰点"
                    else:
                        state = retreat_tier(r, state)
                else:
                    state = retreat_tier(r, state)
                r.exit_progress = (
                    f"融资转正 {'2/2' if margin_pos_2d else ('1/2' if r.margin_yi > 0 else '0/2')}｜"
                    f"收复20日线 {'✓' if above_both else '✗'}｜量能≥20日均 {'✓' if vol_ok else '✗'}"
                    + ("｜本日盖派发反弹戳，解除进度清零" if r.rebound_stamp else "")
                    + (f"｜冰点维持：跌停占比 {100*r.limit_down_share:.2f}%"
                       f"（解除须 <{100*ice_exit_share:.1f}% 且涨>跌）"
                       if state == "冰点" else "")
                )
        elif panic or len(r.groups_hit) >= 2:
            # 恐慌日（跌停占比 ≥ 阈值）当日直进退潮梯队：闪崩日常放量、
            # 融资 T-1 滞后，三组难以当日凑够两组，恐慌单独构成退潮入口；
            # 次日恐慌若持续，由退潮态分支再升冰点
            state = retreat_tier(r, state)
        else:
            def _caution_legs(x: Optional[DayRow]) -> bool:
                if x is None:
                    return False
                cum_hit = (
                    x.margin_neg_cum_bp <= x.thr["margin_neg_cum_bp"] if x.margin_bp is not None
                    else x.margin_neg_cum_yi <= MARGIN_NEG_CUM_YI
                )
                # 与「融资」组口径一致：连负天数或连负累计额任一触发
                return bool(
                    x.below20_any or x.margin_neg_days >= MARGIN_NEG_DAYS or cum_hit
                    or x.rebound_stamp or x.div_count10 >= DIV_TRIGGER
                )

            caution = _caution_legs(r) or (prev is not None and prev.rebound_stamp)
            if caution:
                state = "谨慎"
            elif state == "谨慎":
                state = "谨慎" if _caution_legs(prev) else "标准"
            else:
                # 进攻档：双指数站上 20 日线 + 放量 ≥20 日均 + 融资连正 ≥2 日 +
                # 涨>跌家数；仅从标准态进入（与金库 trend_state.py 同口径）
                attack_ok = (
                    above_both and vol_ok and margin_pos_2d and r.rise > r.fall
                )
                state = "进攻" if attack_ok else "标准"
        r.state = state

        # 相位修饰（仅退潮/深度退潮）：主档保持迟滞，相位承载边际方向。
        # 5v5 镜像证据，四对取自当日、一对取自 T-1 融资，滞后腿因此无法单独定向：
        #   广度（涨>跌 / 跌>涨）、20 日线缺口（收窄 / 扩大或当日新破位）、
        #   量能（站回 20 日均九成 / 缩量）、跌停（收敛 / 抬升至解除线上）、
        #   融资（转正 / 大卖 <-100 亿，滞后一日）。
        # 旧口径把「收复双指数 20 日线」当修复腿，但它与「破位」组定义互斥——
        # 破位命中时该腿恒为假，修复侧结构性封顶，方向实际由滞后融资单独决定；
        # 改用缺口收窄后，指数向均线修复这件事在破位期间也能被记到。
        # 任一方向证据 ≥2 且严格多于对方才定向，否则为企稳。
        if state in ("退潮", "深度退潮"):
            gap_narrowing = (
                r.ma20_gap is not None and prev is not None
                and prev.ma20_gap is not None and r.ma20_gap < prev.ma20_gap
            )
            gap_widening = (
                r.ma20_gap is not None and prev is not None
                and prev.ma20_gap is not None and r.ma20_gap > prev.ma20_gap
            )
            vol_recovered = r.amt_ma20_prev is not None and not r.shrink
            ld_easing = (
                limit_down_easing(r)
                and prev is not None and r.limit_down < prev.limit_down
            )
            margin_day = r.margin_data_date.strftime("%m-%d") if r.margin_data_date else "T-1"
            for hit, label in (
                (r.rise > r.fall, "广度转正"),
                (gap_narrowing, "20日线缺口收窄"),
                (vol_recovered, "量能站回20日均九成"),
                (ld_easing, "跌停收敛"),
                (r.margin_yi > 0, f"融资转正（{margin_day}）"),
            ):
                if hit:
                    r.phase_repair.append(label)
            for hit, label in (
                (r.fall > r.rise, "广度恶化"),
                (gap_widening or r.break_days == 1, "20日线缺口扩大"),
                (r.shrink, "缩量"),
                (not limit_down_easing(r), "跌停抬升"),
                (_margin_below(r, r.thr["qc_margin_bp"]), f"融资大卖（{margin_day}）"),
            ):
                if hit:
                    r.phase_worsen.append(label)
            repair, worsen = len(r.phase_repair), len(r.phase_worsen)
            if repair >= 2 and repair > worsen:
                r.phase = "修复中"
            elif worsen >= 2 and worsen > repair:
                r.phase = "恶化中"
            else:
                r.phase = "企稳"


def normalize_asof(asof: Optional[str]) -> date:
    if not asof:
        return date.today()
    text = asof.strip().replace("-", "")
    return datetime.strptime(text, "%Y%m%d").date()


def build_block(asof: Optional[str] = None) -> Dict[str, Any]:
    """生成模块 1 的 trend_state_card 证据区块（含 asof 当日数据）。"""
    asof_date = normalize_asof(asof)
    if BACKEND == Backend.SQLITE:
        return {"available": False, "reason": "trend axis requires PostgreSQL backend"}
    try:
        with get_connection() as conn:
            days, gaps = fetch(conn, asof_date + timedelta(days=1))
    except Exception as exc:  # PG 不可达时研报不阻断，模块 1 显式披露趋势轴不可用
        return {"available": False, "reason": f"market_history unavailable: {exc}"}
    if len(days) < 45:
        reason = f"insufficient continuous history ({len(days)} days) for 20-day baselines"
        if gaps:
            reason += f"; truncated after internal data gaps ({len(gaps)} trading days, e.g. {gaps[-1]})"
        return {"available": False, "reason": reason}

    derive(days)
    run_state_machine(days)
    ref = days[-1]

    state_days = 0
    for d in reversed(days):
        if d.state != ref.state:
            break
        state_days += 1

    return {
        "available": True,
        "asof": str(asof_date),
        "data_through": str(ref.d),
        "is_current": ref.d == asof_date,
        "state": ref.state,
        "state_days": state_days,
        "phase": ref.phase or None,
        "counters": {
            "break_days": ref.break_days,
            "margin_neg_days": ref.margin_neg_days,
            "margin_neg_cum_yi": round(ref.margin_neg_cum_yi, 1),
            "margin_neg_cum_bp": round(ref.margin_neg_cum_bp, 1) if ref.margin_bp is not None else None,
            "shrink_days": ref.shrink_days,
            "rebound_stamp": ref.rebound_stamp,
            "div_count10": ref.div_count10,
            "groups_hit": ref.groups_hit,
            "groups_hit_count": len(ref.groups_hit),
            # 融资读数为 T-1 口径：卡面必须连同数据日一起标注，不得当作当日读数使用
            "margin_data_date": str(ref.margin_data_date) if ref.margin_data_date else None,
            "deep_escalation_deferred": ref.deep_escalation_deferred,
        },
        "ref_day": {
            "rise": ref.rise,
            "fall": ref.fall,
            "limit_up": ref.limit_up,
            "limit_down": ref.limit_down,
            "total": ref.total,
            "rise_share_pct": round(100 * ref.rise_share, 1),
            "limit_down_share_pct": round(100 * ref.limit_down_share, 2),
            "amount_wanyi": round(ref.amount_qianyuan / 1e9, 3),
            "amount_vs_ma20_pct": round(100 * ref.amount_qianyuan / ref.amt_ma20_prev, 1) if ref.amt_ma20_prev else None,
            "margin_yi": round(ref.margin_yi, 1),
            "margin_bp": round(ref.margin_bp, 1) if ref.margin_bp is not None else None,
        },
        # 阈值随日滚动，卡面要能自证"今天这条线画在哪儿"——否则占比化之后读者
        # 无法判断某个计数器为什么触发
        "thresholds": {
            "source": ref.thr.get("source"),
            # 融资腿实际按哪个口径比：当日拿得到余额就是 bp，拿不到才退回亿元，
            # 与分位基准是否攒够无关
            "margin_unit": "bp" if ref.margin_bp is not None else "yi",
            "bigup_rise_share_pct": round(100 * ref.thr["bigup_rise_share"], 1),
            "panic_limit_down_share_pct": round(100 * ref.thr["panic_limit_down_share"], 1),
            "ice_exit_limit_down_share_pct": round(100 * ref.thr["ice_exit_limit_down_share"], 1),
            "div_rise_share_pct": round(100 * ref.thr["div_rise_share"], 1),
            "qc_margin_bp": round(ref.thr["qc_margin_bp"], 1),
            "div_margin_bp": round(ref.thr["div_margin_bp"], 1),
            "margin_neg_cum_bp": round(ref.thr["margin_neg_cum_bp"], 1),
        },
        "phase_score": {
            "repair": len(ref.phase_repair),
            "worsen": len(ref.phase_worsen),
            "repair_hits": ref.phase_repair,
            "worsen_hits": ref.phase_worsen,
        } if ref.phase else None,
        "state_note": ref.state_note or None,
        "exit_progress": ref.exit_progress or ("不适用（未处退潮态）" if ref.state in ("进攻", "标准", "谨慎") else ""),
        # 30 天而非 10 天：HTML 状态卡的档位时间轴按 HISTORY_DAYS 画，正文只引用
        # 最近 5 天的轨迹，多出来的那段是给图看的
        "history": [
            {
                "date": str(d.d),
                "state": d.state,
                "phase": d.phase or None,
                "groups": d.groups_hit,
                "rebound_stamp": d.rebound_stamp,
                "div10": d.div_count10,
                "deep_deferred": d.deep_escalation_deferred or None,
            }
            for d in days[-HISTORY_DAYS:]
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="趋势状态卡：五计数器 + 六档状态机（盘后含当日口径）")
    ap.add_argument("--asof", default=None, help="分析日 YYYYMMDD 或 YYYY-MM-DD，默认今天；含当日数据")
    args = ap.parse_args()
    block = build_block(args.asof)
    print(json.dumps(block, ensure_ascii=False, indent=2))
    return 0 if block.get("available") else 1


if __name__ == "__main__":
    sys.exit(main())
