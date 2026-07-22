#!/usr/bin/env python3
"""趋势状态卡（trend_state_card）——盘后研报模块 1 的趋势轴机判区块。

与 AlphaVault `system/tools/trend_state.py`（盘前计划工具）同源同参：
五个趋势计数器（破位 / 融资连负 / 缩量 / 反弹质检戳 / 顶背离计数）+
六档状态机（进攻 / 标准 / 谨慎 / 退潮 / 深度退潮 / 冰点）。
升档迟滞只作用于「升出退潮梯队」（退潮/深度退潮/冰点 → 谨慎，须 exit_ok
右侧确认）；梯队内部档位按日调整——非退潮态下跌停 ≥PANIC_LIMIT_DOWN 的
恐慌日当日直进退潮（闪崩放量、融资 T-1 滞后时三组难当日凑够两组），
冰点以跌停 ≥PANIC_LIMIT_DOWN 进入（退潮态内），以跌停回落至
ICE_EXIT_LIMIT_DOWN 以下且涨>跌（广度修复）当日解除，退回退潮/深度退潮，
避免单日恐慌把状态锁死在冰点多日（融资 T-1 滞后会使 exit_ok 在恐慌
结束后仍长期不可达）。
退潮/深度退潮日附带确定性相位修饰 phase（修复中 / 企稳 / 恶化中，规则见
run_state_machine 末尾）：主档保持迟滞、不追单日，相位承载边际方向，
表达「退潮·修复中」这类过渡期语义；冰点与非退潮态 phase 为空。
反弹质检戳的融资腿（T-1 滞后）在恐慌次日（昨日跌停 ≥PANIC_LIMIT_DOWN）
不参判——彼时融资读数反映恐慌日本身，近乎自动触发，只按量能腿判定。
两者唯一差异是时间语义：本脚本为盘后视角，`--asof` **含当日**收盘数据；
盘前工具取计划日之前的数据。参数常量对应经验法则 R-016 / R-017 / R-018，
改参数须与盘前工具及规则页同步。

口径备注（与 market_history 写入口径一致）：
  - market_history.amount 单位千元；margin_net_buy 单位元（融资为 T-1 滞后口径）。
  - 「成交额 vs 20 日均」为前 20 个交易日均值（不含当日）。
  - 指数 MA20 为含当日的 20 日收盘均线。

输出 JSON 为确定性证据（只含客观读数，不含任何仓位或操作字段）：
available / data_through / state / state_days / counters / ref_day /
exit_progress / history。模型只读、不得改写读数；趋势定性由模型在模块 1
正文中给出，且同样不写仓位与买卖建议。

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

# R-016 / R-017 / R-018 参数（与 AlphaVault trend_state.py 及规则页一致，改动须三处同步）
MARGIN_NEG_DAYS = 3
MARGIN_NEG_CUM_YI = -300.0
SHRINK_RATIO = 0.90
SHRINK_DAYS = 3
BREAK_DAYS = 2
BIGUP_RISE = 3500
BIGUP_IDX_PCT = 2.0
QC_MARGIN_YI = -100.0
DIV_RISE = 1500
DIV_MARGIN_YI = -150.0
DIV_WINDOW = 10
DIV_TRIGGER = 2
PANIC_LIMIT_DOWN = 200
ICE_EXIT_LIMIT_DOWN = 100   # 冰点解除线：跌停回落到此线以下且涨>跌即解除（入 200 / 出 100，迟滞防抖）


@dataclass
class DayRow:
    d: date
    rise: int
    fall: int
    limit_up: int
    limit_down: int
    amount_qianyuan: float          # 千元
    margin_yi: float                # 亿元
    amt_ma20_prev: Optional[float] = None   # 前 20 日均（千元，不含当日）
    idx_close: dict = field(default_factory=dict)
    idx_ma20: dict = field(default_factory=dict)
    idx_pct: dict = field(default_factory=dict)
    idx_high20: dict = field(default_factory=dict)

    below20_both: bool = False
    below20_any: bool = False
    break_days: int = 0
    margin_neg_days: int = 0
    margin_neg_cum_yi: float = 0.0
    shrink: bool = False
    shrink_days: int = 0
    big_up: bool = False
    rebound_stamp: bool = False     # R-018 派发反弹戳
    divergence: bool = False        # R-017 单日背离
    div_count10: int = 0
    groups_hit: list = field(default_factory=list)
    state: str = "标准"
    phase: str = ""                 # 退潮梯队内的相位修饰：修复中 / 企稳 / 恶化中
    exit_progress: str = ""


def fetch(conn, boundary_exclusive: date, lookback_cal_days: int = 240) -> Tuple[List[DayRow], List[date]]:
    """返回 (days, gaps)。gaps 是位于最后可用日之前、因缺量额/融资/指数数据
    被剔除的交易日；存在缺口时 days 已截断为最后缺口之后的干净连续段。"""
    start = boundary_exclusive - timedelta(days=lookback_cal_days)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT date, rise, fall, limit_up, limit_down, amount, margin_net_buy
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

    days: List[DayRow] = []
    dates = sorted(d for d in mh if d in idx_by_day and all(ts in idx_by_day[d] for ts in TREND_PAIR))
    amounts: List[float] = []
    for d in dates:
        _, rise, fall, lu, ld, amount, margin = mh[d]
        row = DayRow(
            d=d, rise=rise or 0, fall=fall or 0, limit_up=lu or 0, limit_down=ld or 0,
            amount_qianyuan=float(amount), margin_yi=float(margin) / 1e8,
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


def derive(days: List[DayRow]) -> None:
    for i, r in enumerate(days):
        prev = days[i - 1] if i > 0 else None

        below = {
            ts: (r.idx_ma20.get(ts) is not None and r.idx_close.get(ts, 0) < r.idx_ma20[ts])
            for ts in TREND_PAIR
        }
        r.below20_both = all(below.values())
        r.below20_any = any(below.values())
        r.break_days = (prev.break_days + 1) if (r.below20_both and prev) else (1 if r.below20_both else 0)

        if r.margin_yi < 0:
            r.margin_neg_days = (prev.margin_neg_days + 1) if prev and prev.margin_neg_days else 1
            r.margin_neg_cum_yi = (prev.margin_neg_cum_yi if prev and prev.margin_neg_days else 0.0) + r.margin_yi
        else:
            r.margin_neg_days = 0
            r.margin_neg_cum_yi = 0.0

        r.shrink = r.amt_ma20_prev is not None and r.amount_qianyuan < SHRINK_RATIO * r.amt_ma20_prev
        r.shrink_days = (prev.shrink_days + 1) if (r.shrink and prev) else (1 if r.shrink else 0)

        idx_pct_max = max((r.idx_pct.get(ts, 0) for ts in TREND_PAIR), default=0)
        r.big_up = r.rise > BIGUP_RISE or idx_pct_max > BIGUP_IDX_PCT
        weak_volume = r.amt_ma20_prev is not None and r.amount_qianyuan < r.amt_ma20_prev
        # 融资腿（T-1 滞后）在恐慌次日失真：跌停 ≥PANIC 的次日，融资读数反映的是
        # 恐慌日本身，近乎自动触发，此时只按量能腿判定反弹戳
        panic_yesterday = prev is not None and prev.limit_down >= PANIC_LIMIT_DOWN
        margin_leg = r.margin_yi < QC_MARGIN_YI and not panic_yesterday
        r.rebound_stamp = r.big_up and (weak_volume or margin_leg)

        new_high = any(r.idx_high20.get(ts) for ts in INDEXES)
        r.divergence = new_high and (r.rise < DIV_RISE or r.margin_yi < DIV_MARGIN_YI)
        r.div_count10 = sum(1 for x in days[max(0, i - DIV_WINDOW + 1): i + 1] if x.divergence)

        groups = []
        if r.break_days >= BREAK_DAYS:
            groups.append("破位")
        if r.margin_neg_days >= MARGIN_NEG_DAYS or r.margin_neg_cum_yi <= MARGIN_NEG_CUM_YI:
            groups.append("融资")
        if r.shrink_days >= SHRINK_DAYS:
            groups.append("缩量")
        r.groups_hit = groups


def run_state_machine(days: List[DayRow]) -> None:
    state = "标准"
    for i, r in enumerate(days):
        prev = days[i - 1] if i > 0 else None
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

        if in_retreat:
            if exit_ok:
                state = "谨慎"
            else:
                if r.limit_down >= PANIC_LIMIT_DOWN:
                    state = "冰点"
                elif state == "冰点":
                    # 冰点解除：恐慌退潮（跌停 < 解除线）且广度修复（涨>跌），
                    # 当日退回退潮梯队；不满足则维持冰点
                    ice_clear = r.limit_down < ICE_EXIT_LIMIT_DOWN and r.rise > r.fall
                    if not ice_clear:
                        state = "冰点"
                    else:
                        state = "深度退潮" if len(r.groups_hit) == 3 else "退潮"
                else:
                    state = "深度退潮" if len(r.groups_hit) == 3 else "退潮"
                r.exit_progress = (
                    f"融资转正 {'2/2' if margin_pos_2d else ('1/2' if r.margin_yi > 0 else '0/2')}｜"
                    f"收复20日线 {'✓' if above_both else '✗'}｜量能≥20日均 {'✓' if vol_ok else '✗'}"
                    + ("｜本日盖派发反弹戳，解除进度清零" if r.rebound_stamp else "")
                    + (f"｜冰点维持：跌停 {r.limit_down}（解除须跌停<{ICE_EXIT_LIMIT_DOWN} 且涨>跌）"
                       if state == "冰点" else "")
                )
        elif r.limit_down >= PANIC_LIMIT_DOWN or len(r.groups_hit) >= 2:
            # 恐慌日（跌停 ≥ PANIC_LIMIT_DOWN）当日直进退潮梯队：闪崩日常放量、
            # 融资 T-1 滞后，三组难以当日凑够两组，恐慌单独构成退潮入口；
            # 次日恐慌若持续，由退潮态分支再升冰点
            state = "深度退潮" if len(r.groups_hit) == 3 else "退潮"
        else:
            caution = (
                r.below20_any
                # 与「融资」组口径一致：连负天数或连负累计额任一触发
                or r.margin_neg_days >= MARGIN_NEG_DAYS
                or r.margin_neg_cum_yi <= MARGIN_NEG_CUM_YI
                or r.rebound_stamp
                or (prev is not None and prev.rebound_stamp)
                or r.div_count10 >= DIV_TRIGGER
            )
            if caution:
                state = "谨慎"
            elif state == "谨慎":
                prev_caution = prev is not None and (
                    prev.below20_any or prev.margin_neg_days >= MARGIN_NEG_DAYS
                    or prev.margin_neg_cum_yi <= MARGIN_NEG_CUM_YI
                    or prev.rebound_stamp or prev.div_count10 >= DIV_TRIGGER
                )
                state = "谨慎" if prev_caution else "标准"
            else:
                # 进攻档：双指数站上 20 日线 + 放量 ≥20 日均 + 融资连正 ≥2 日 +
                # 涨>跌家数；仅从标准态进入（与金库 trend_state.py 同口径）
                attack_ok = (
                    above_both and vol_ok and margin_pos_2d and r.rise > r.fall
                )
                state = "进攻" if attack_ok else "标准"
        r.state = state

        # 相位修饰（仅退潮/深度退潮）：主档保持迟滞，相位承载边际方向。
        # 修复证据：涨>跌 / 自冰点解除当日 / 融资转正 / 收复双指数 20 日线；
        # 恶化证据：跌停回升至解除线以上 / 当日新破位 / 缩量 / 融资大卖（<-100 亿）。
        # 任一方向证据 ≥2 且严格多于对方才定向，否则为企稳。
        if state in ("退潮", "深度退潮"):
            repair = (
                (r.rise > r.fall)
                + (prev is not None and prev.state == "冰点")
                + (r.margin_yi > 0)
                + above_both
            )
            worsen = (
                (r.limit_down >= ICE_EXIT_LIMIT_DOWN)
                + (r.break_days == 1)
                + r.shrink
                + (r.margin_yi < QC_MARGIN_YI)
            )
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
            "shrink_days": ref.shrink_days,
            "rebound_stamp": ref.rebound_stamp,
            "div_count10": ref.div_count10,
            "groups_hit": ref.groups_hit,
            "groups_hit_count": len(ref.groups_hit),
        },
        "ref_day": {
            "rise": ref.rise,
            "fall": ref.fall,
            "limit_up": ref.limit_up,
            "limit_down": ref.limit_down,
            "amount_wanyi": round(ref.amount_qianyuan / 1e9, 3),
            "amount_vs_ma20_pct": round(100 * ref.amount_qianyuan / ref.amt_ma20_prev, 1) if ref.amt_ma20_prev else None,
            "margin_yi": round(ref.margin_yi, 1),
        },
        "exit_progress": ref.exit_progress or ("不适用（未处退潮态）" if ref.state in ("进攻", "标准", "谨慎") else ""),
        "history": [
            {
                "date": str(d.d),
                "state": d.state,
                "phase": d.phase or None,
                "groups": d.groups_hit,
                "rebound_stamp": d.rebound_stamp,
                "div10": d.div_count10,
            }
            for d in days[-10:]
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
