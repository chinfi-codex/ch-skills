#!/usr/bin/env python3
"""极值状态卡（extreme_state_card）——盘后研报模块 1 的第二根轴。

**为什么要有第二根轴。** 趋势卡的六档同时在回答两件在 A 股互相矛盾的事：
「现在危不危险」和「该不该参与」。`evals/trend_state_review_2026-08.md` 的
5 年回放显示，最差的两档恰恰是前瞻收益最好的两档——冰点日之后 20 个交易日
81.8% 的时候在涨、平均涨 8.92%，而退潮档占掉了 46.4% 的时间。一根轴承载不了
这两件事，于是把「极值」单拎出来：趋势轴继续描述所处阶段（慢变量），极值轴
负责抓两端的拐点（快变量），两根轴各说各的，冲突时并列写，不许调和。

输出两个分数，都只给确定性读数，不给结论、不给仓位：

  washout（底部出清分，0~6）——每条命中记 1 分
    w1 站上 20 日线个股占比处于滚动 2 年 5 分位以下
    w2 跌停家数占比处于滚动 2 年 97 分位以上
    w3 创 60 日新低个股占比处于滚动 2 年 95 分位以上
    w4 融资净买入（占融资余额 bp）处于滚动 2 年 5 分位以下
    w5 基准指数距 250 日高点回撤处于滚动 2 年 10 分位以下
    w6 个股中位回撤（距各自 250 日高点）处于滚动 2 年 10 分位以下
    近 10 日内出现过 ≥2 分 = 出清区；当日 ≥3 分 = 出清极值。

  top（顶部拥挤分，0~5）
    p1 宽度背离：基准指数创 20 日新高，但站上 60 日线占比比上一次新高时低 5pct 以上（粘性 15 日）
    p2 赚钱效应衰减：涨停占比 5 日均 < 20 日均，且近 10 日内有过 20 日新高
    p3 杠杆加速：融资余额 20 日变化率处于滚动 2 年 90 分位以上
    p4 量能亢奋：成交额 5 日均 / 20 日均 ≥ 1.20
    p5 宽度过热：站上 20 日线个股占比处于滚动 2 年 95 分位以上
    ≥2 分 = 顶部风险；≥3 分 = 顶部高危。

  **顶部分不含「波动率抬升」和「融资/成交额高位」**，这两条在 A 股实测是看涨
  读数（触发后 60 日分别 +3.16% 和 +7.96%，都高于基准 +1.15%），多出现在出清期
  而非顶部，放进顶部分会反向污染整个分数。

置信度差异必须照实披露：底部侧两个子样本（2021H2~2023 / 2024~2026）都成立，
顶部侧只有「杠杆加速」两段一致且只覆盖 7 个阶段顶里的 3 个。所以卡面对顶部
只能写「风险累积度」，不能升格成方向性结论。

**分位基准怎么来。** 逐日指标落在 `dms_extreme_daily` 表里，分位从这张表的历史
算（滚动 500 个交易日，min_periods 250）。历史不足时退回一组固定水平——取值来自
同一份 5 年回放的全样本分位数，`percentile_source` 字段会写明当天用的是哪种。
首次使用请先跑 `--backfill 300` 把历史补起来。

用法：
  python scripts/extreme_state_card.py --asof 20260807
  python scripts/extreme_state_card.py --asof 20260807 --backfill 300
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))
from db_core import BACKEND, Backend, get_connection  # noqa: E402

BENCHMARK = "000985.CSI"          # 中证全指：与涨跌家数、宽度同尺
BENCHMARK_FALLBACK = "000001.SH"  # 中证全指没落库时退回上证综指
HIGH_WINDOW = 250
NEW_LOW_WINDOW = 60
ROLL_WINDOW = 500                 # 滚动分位窗口（交易日）
ROLL_MIN = 250                    # 不足此长度就退回固定水平
ZONE_LOOKBACK = 10                # 出清区的回看窗口
HISTORY_DAYS = 20                 # 输出的分数轨迹长度（HTML 时间轴按这个宽度画）
DIVERGENCE_STICKY = 15            # 宽度背离的粘性天数
BREADTH_DROP_PCT = 5.0            # 宽度背离的判定落差（百分点）

# 历史不足时的固定水平，取自 evals/trend_state_review_2026-08.md 的 5 年全样本分位
FALLBACK = {
    "w1_breadth_ma20": 14.4,      # 5 分位
    "w2_limit_down_share": 1.84,  # 97 分位
    "w3_new_low_share": 32.1,     # 95 分位
    "w4_margin_bp": -64.3,        # 5 分位
    "w5_index_dd250": -20.1,      # 10 分位
    "w6_median_dd250": -37.7,     # 10 分位
    "p3_rzye_chg20": 6.9,         # 90 分位
    "p5_breadth_ma20": 84.9,      # 95 分位
}
AMT_FEVER = 1.20

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS dms_extreme_daily (
    trade_date          DATE PRIMARY KEY,
    breadth_ma20        DOUBLE PRECISION,
    breadth_ma60        DOUBLE PRECISION,
    new_low_share       DOUBLE PRECISION,
    median_dd250        DOUBLE PRECISION,
    limit_down_share    DOUBLE PRECISION,
    limit_up_share      DOUBLE PRECISION,
    margin_bp           DOUBLE PRECISION,
    rzye_yi             DOUBLE PRECISION,
    rzye_chg20          DOUBLE PRECISION,
    index_close         DOUBLE PRECISION,
    index_dd250         DOUBLE PRECISION,
    index_high20        BOOLEAN,
    amt_ratio_5_20      DOUBLE PRECISION,
    universe            INTEGER,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

METRIC_COLUMNS = [
    "breadth_ma20", "breadth_ma60", "new_low_share", "median_dd250",
    "limit_down_share", "limit_up_share", "margin_bp", "rzye_yi", "rzye_chg20",
    "index_close", "index_dd250", "index_high20", "amt_ratio_5_20", "universe",
]


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def trading_days(conn, asof: date, n: int) -> List[date]:
    """asof（含）往前的 n 个有全市场日线的交易日，升序。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date <= %s "
            "ORDER BY trade_date DESC LIMIT %s",
            (asof, n),
        )
        return sorted(row[0] for row in cur.fetchall())


def stock_aggregates(conn, asof: date, window: List[date]) -> Dict[str, Any]:
    """一次聚合扫描算出宽度 / 新低 / 中位回撤，重活留在 SQL 里。

    把 20/60/250 日的均值、极值放进同一个 GROUP BY，避免把上百万行日线拉进
    Python；窗口长度不够的个股按各自指标从分母里剔除（COUNT 判定）。
    """
    if len(window) < 21:
        return {"available": False, "reason": f"insufficient stock_daily history ({len(window)} days)"}
    d_start, d_end = window[0], window[-1]
    d20 = window[-20]
    d60 = window[-NEW_LOW_WINDOW] if len(window) >= NEW_LOW_WINDOW else None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code,
                   AVG(close) FILTER (WHERE trade_date >= %(d20)s)          AS ma20,
                   COUNT(*)   FILTER (WHERE trade_date >= %(d20)s)          AS n20,
                   AVG(close) FILTER (WHERE trade_date >= %(d60)s)          AS ma60,
                   MIN(close) FILTER (WHERE trade_date >= %(d60)s)          AS low60,
                   COUNT(*)   FILTER (WHERE trade_date >= %(d60)s)          AS n60,
                   MAX(close)                                               AS high250,
                   COUNT(*)                                                 AS n250,
                   MAX(close) FILTER (WHERE trade_date = %(end)s)           AS last_close
            FROM stock_daily
            WHERE trade_date BETWEEN %(start)s AND %(end)s AND close IS NOT NULL
            GROUP BY ts_code
            """,
            {"d20": d20, "d60": d60 or d_start, "start": d_start, "end": d_end},
        )
        rows = cur.fetchall()
    if not rows:
        return {"available": False, "reason": "stock_daily returned no rows in window"}

    df = pd.DataFrame(rows, columns=[
        "ts_code", "ma20", "n20", "ma60", "low60", "n60", "high250", "n250", "last_close"])
    for col in ("ma20", "ma60", "low60", "high250", "last_close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    traded = df["last_close"].notna()

    def share(hit: pd.Series, valid: pd.Series) -> Optional[float]:
        total = int((valid & traded).sum())
        if not total:
            return None
        return round(100.0 * int((hit & valid & traded).sum()) / total, 2)

    out: Dict[str, Any] = {
        "available": True,
        "universe": int(traded.sum()),
        "breadth_ma20": share(df["last_close"] > df["ma20"], df["n20"].eq(20)),
        "breadth_ma60": share(df["last_close"] > df["ma60"], df["n60"].eq(NEW_LOW_WINDOW))
        if len(window) >= NEW_LOW_WINDOW else None,
        "new_low_share": share(df["last_close"] <= df["low60"], df["n60"].eq(NEW_LOW_WINDOW))
        if len(window) >= NEW_LOW_WINDOW else None,
        "median_dd250": None,
    }
    deep = df["n250"] >= min(200, len(window))
    valid_dd = deep & traded & df["high250"].gt(0)
    if int(valid_dd.sum()):
        dd = (df.loc[valid_dd, "last_close"] / df.loc[valid_dd, "high250"] - 1.0) * 100.0
        out["median_dd250"] = round(float(dd.median()), 2)
    out["median_dd250_window_days"] = len(window)
    return out


def market_row(conn, asof: date) -> Dict[str, Any]:
    """market_history 里的当日盘面读数 + 成交额 5/20 日比值。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT date, rise, fall, flat, limit_up, limit_down, amount
            FROM market_history WHERE date <= %s ORDER BY date DESC LIMIT 21
            """,
            (asof,),
        )
        rows = cur.fetchall()
    if not rows:
        return {"available": False, "reason": "market_history has no rows on or before asof"}
    rows = list(reversed(rows))
    today = rows[-1]
    total = (today[1] or 0) + (today[2] or 0) + (today[3] or 0)
    amounts = [float(r[6]) for r in rows if r[6] is not None]
    out: Dict[str, Any] = {
        "available": True,
        "trade_date": today[0],
        "universe_mh": total,
        "limit_down_share": round(100.0 * (today[5] or 0) / total, 3) if total else None,
        "limit_up_share": round(100.0 * (today[4] or 0) / total, 3) if total else None,
        "amt_ratio_5_20": None,
    }
    if len(amounts) >= 20:
        ma5 = sum(amounts[-5:]) / 5
        ma20 = sum(amounts[-20:]) / 20
        out["amt_ratio_5_20"] = round(ma5 / ma20, 3) if ma20 else None
    return out


def margin_metrics(conn, asof: date) -> Dict[str, Any]:
    """融资：净买入占余额 bp（T-1 口径）与余额 20 日变化率。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, COUNT(DISTINCT exchange_id) AS n,
                   SUM(rzye) AS rzye, SUM(rzmre) - SUM(rzche) AS net
            FROM stock_margin
            WHERE trade_date <= %s AND rzye IS NOT NULL
            GROUP BY trade_date ORDER BY trade_date DESC LIMIT 40
            """,
            (asof,),
        )
        rows = cur.fetchall()
    if not rows:
        return {"available": False, "reason": "stock_margin has no rzye rows on or before asof"}
    counts: Dict[int, int] = {}
    for _, n, _r, _net in rows:
        counts[int(n)] = counts.get(int(n), 0) + 1
    expected = max(counts, key=lambda k: counts[k])
    series = [(r[0], float(r[2]) / 1e8, float(r[3]) / 1e8 if r[3] is not None else None)
              for r in reversed(rows) if int(r[1]) == expected]
    if not series:
        return {"available": False, "reason": "no margin day has the modal exchange count"}
    d_latest, rzye, net = series[-1]
    out: Dict[str, Any] = {
        "available": True,
        "trade_date": d_latest,
        "rzye_yi": round(rzye, 1),
        "margin_bp": round(net / rzye * 1e4, 1) if net is not None and rzye else None,
        "rzye_chg20": None,
    }
    if len(series) >= 21 and series[-21][1]:
        out["rzye_chg20"] = round((rzye / series[-21][1] - 1) * 100, 2)
    return out


def index_metrics(conn, asof: date) -> Dict[str, Any]:
    """基准指数：收盘、距 250 日高点回撤、是否创 20 日新高。"""
    for code in (BENCHMARK, BENCHMARK_FALLBACK):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date, close FROM stock_index_daily "
                "WHERE ts_code = %s AND trade_date <= %s ORDER BY trade_date DESC LIMIT %s",
                (code, asof, HIGH_WINDOW),
            )
            rows = list(reversed(cur.fetchall()))
        if len(rows) < 21:
            continue
        closes = [float(r[1]) for r in rows]
        last = closes[-1]
        high250 = max(closes)
        return {
            "available": True, "ts_code": code, "trade_date": rows[-1][0],
            "index_close": round(last, 2),
            "index_dd250": round((last / high250 - 1) * 100, 2) if high250 else None,
            "index_high20": bool(last >= max(closes[-20:])),
            "window_days": len(rows),
        }
    return {"available": False, "reason": "no benchmark index history in stock_index_daily"}


# ---------------------------------------------------------------------------
# 落库与分位
# ---------------------------------------------------------------------------
def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(TABLE_DDL)


def upsert_metrics(conn, trade_date: date, metrics: Dict[str, Any]) -> None:
    cols = [c for c in METRIC_COLUMNS if c in metrics]
    if not cols:
        return
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    placeholders = ", ".join(["%s"] * (len(cols) + 1))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO dms_extreme_daily (trade_date, {', '.join(cols)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (trade_date) DO UPDATE SET {assignments}, updated_at = NOW()",
            [trade_date] + [metrics[c] for c in cols],
        )


def load_history(conn, asof: date) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT trade_date, {', '.join(METRIC_COLUMNS)} FROM dms_extreme_daily "
            "WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT %s",
            (asof, ROLL_WINDOW),
        )
        rows = cur.fetchall()
    df = pd.DataFrame(list(reversed(rows)), columns=["trade_date"] + METRIC_COLUMNS)
    for col in METRIC_COLUMNS:
        if col != "index_high20":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def quantile_or_fallback(history: pd.DataFrame, column: str, q: float,
                         fallback_key: str, direction: str) -> Dict[str, Any]:
    """够长用滚动分位，不够用固定水平——两种情况都把用了哪个写进读数。

    样本还没攒满一个完整窗口（ROLL_MIN ≤ n < ROLL_WINDOW）时取两者中**更严**的
    一侧：`le` 腿取较低值、`ge` 腿取较高值。否则一段温和行情会把极值线抬得很松——
    实测只有 260 天历史时，"距 250 日高点回撤的 10 分位"只有 −6.6%，宽度 86% 的
    日子照样能命中出清腿，读数就废了。攒满 500 天后完全交给滚动分位。
    """
    series = history[column].dropna() if column in history.columns else pd.Series(dtype=float)
    n = int(len(series))
    if n < ROLL_MIN:
        return {"value": FALLBACK[fallback_key], "source": "fixed_fallback", "sample_days": n}
    rolling = round(float(series.quantile(q)), 2)
    if n >= ROLL_WINDOW:
        return {"value": rolling, "source": "rolling_quantile", "sample_days": n}
    reference = FALLBACK[fallback_key]
    stricter = min(rolling, reference) if direction == "le" else max(rolling, reference)
    return {"value": stricter, "source": "rolling_quantile_capped",
            "sample_days": n, "rolling": rolling, "reference": reference}


# ---------------------------------------------------------------------------
# 计分
# ---------------------------------------------------------------------------
def breadth_divergence(history: pd.DataFrame) -> Dict[str, Any]:
    """指数创 20 日新高但站上 60 日线占比比上一次新高时明显更低，粘性 15 日。"""
    if history.empty or "index_high20" not in history.columns:
        return {"hit": False, "reason": "no history"}
    last_breadth: Optional[float] = None
    flags: List[bool] = []
    detail: Optional[Dict[str, Any]] = None
    for _, r in history.iterrows():
        hit = False
        if bool(r["index_high20"]) and pd.notna(r["breadth_ma60"]):
            b = float(r["breadth_ma60"])
            if last_breadth is not None and b < last_breadth - BREADTH_DROP_PCT:
                hit = True
                detail = {"date": str(r["trade_date"]), "breadth_now": round(b, 2),
                          "breadth_prev_high": round(last_breadth, 2)}
            last_breadth = b
        flags.append(hit)
    sticky = any(flags[-DIVERGENCE_STICKY:])
    return {"hit": bool(sticky), "last_divergence": detail}


def score(history: pd.DataFrame, today: Dict[str, Any]) -> Dict[str, Any]:
    thr = {
        "w1": quantile_or_fallback(history, "breadth_ma20", 0.05, "w1_breadth_ma20", "le"),
        "w2": quantile_or_fallback(history, "limit_down_share", 0.97, "w2_limit_down_share", "ge"),
        "w3": quantile_or_fallback(history, "new_low_share", 0.95, "w3_new_low_share", "ge"),
        "w4": quantile_or_fallback(history, "margin_bp", 0.05, "w4_margin_bp", "le"),
        "w5": quantile_or_fallback(history, "index_dd250", 0.10, "w5_index_dd250", "le"),
        "w6": quantile_or_fallback(history, "median_dd250", 0.10, "w6_median_dd250", "le"),
        "p3": quantile_or_fallback(history, "rzye_chg20", 0.90, "p3_rzye_chg20", "ge"),
        "p5": quantile_or_fallback(history, "breadth_ma20", 0.95, "p5_breadth_ma20", "ge"),
    }

    def leg(key: str, value: Optional[float], op: str, label: str) -> Dict[str, Any]:
        line = thr[key]["value"]
        if value is None:
            return {"leg": key, "label": label, "hit": None, "reason": "读数缺失",
                    "threshold": line, "threshold_source": thr[key]["source"]}
        hit = value <= line if op == "le" else value >= line
        return {"leg": key, "label": label, "hit": bool(hit), "value": value,
                "threshold": line, "threshold_source": thr[key]["source"]}

    washout_legs = [
        leg("w1", today.get("breadth_ma20"), "le", "站上20日线占比处于低位极值"),
        leg("w2", today.get("limit_down_share"), "ge", "跌停占比处于高位极值"),
        leg("w3", today.get("new_low_share"), "ge", "60日新低占比处于高位极值"),
        leg("w4", today.get("margin_bp"), "le", "融资净买入(bp)处于低位极值"),
        leg("w5", today.get("index_dd250"), "le", "指数距250日高点回撤处于深位"),
        leg("w6", today.get("median_dd250"), "le", "个股中位回撤处于深位"),
    ]

    div = breadth_divergence(history)
    limit_up_hist = history["limit_up_share"].dropna() if "limit_up_share" in history else pd.Series(dtype=float)
    p2_hit: Optional[bool] = None
    p2_detail: Dict[str, Any] = {}
    if len(limit_up_hist) >= 20:
        ma5, ma20 = limit_up_hist.tail(5).mean(), limit_up_hist.tail(20).mean()
        near_high = bool(history["index_high20"].tail(10).astype(bool).any())
        p2_hit = bool(ma5 < ma20 and near_high)
        p2_detail = {"limit_up_ma5": round(float(ma5), 3), "limit_up_ma20": round(float(ma20), 3),
                     "index_high20_within_10d": near_high}
    amt = today.get("amt_ratio_5_20")

    top_legs = [
        {"leg": "p1", "label": "宽度背离（指数新高但宽度走低）", "hit": div["hit"],
         "detail": div.get("last_divergence")},
        {"leg": "p2", "label": "赚钱效应衰减（涨停占比5日均<20日均且近期有新高）",
         "hit": p2_hit, **({"detail": p2_detail} if p2_detail else {"reason": "涨停占比历史不足 20 天"})},
        leg("p3", today.get("rzye_chg20"), "ge", "杠杆加速（融资余额20日增速处于高位）"),
        {"leg": "p4", "label": f"量能亢奋（成交额5日/20日 ≥ {AMT_FEVER}）",
         "hit": (amt >= AMT_FEVER) if amt is not None else None, "value": amt,
         "threshold": AMT_FEVER, "threshold_source": "fixed"},
        leg("p5", today.get("breadth_ma20"), "ge", "宽度过热（站上20日线占比处于高位极值）"),
    ]

    washout_score = sum(1 for x in washout_legs if x.get("hit"))
    top_score = sum(1 for x in top_legs if x.get("hit"))
    sources = {x.get("threshold_source") for x in washout_legs + top_legs
               if x.get("threshold_source") and x.get("threshold_source") != "fixed"}
    return {
        "washout": {
            "score": washout_score,
            "max_score": len(washout_legs),
            "extreme": washout_score >= 3,
            "legs": washout_legs,
        },
        "top": {
            "score": top_score,
            "max_score": len(top_legs),
            "risk": top_score >= 2,
            "high_risk": top_score >= 3,
            "legs": top_legs,
            "confidence_note": (
                "顶部侧的证据强度明显弱于底部：5 年回放里只有「杠杆加速」在两个子样本"
                "都跑输基准，且只覆盖 7 个阶段顶中的 3 个。本分数只表示风险累积度，"
                "不构成方向性结论。"
            ),
        },
        "percentile_source": sorted(sources)[0] if len(sources) == 1 else "mixed",
        "percentile_source_detail": {k: v["source"] for k, v in thr.items()},
    }


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def compute_day(conn, asof: date) -> Dict[str, Any]:
    window = trading_days(conn, asof, HIGH_WINDOW)
    if not window:
        return {"available": False, "reason": "stock_daily has no rows on or before asof"}
    agg = stock_aggregates(conn, asof, window)
    mkt = market_row(conn, asof)
    mgn = margin_metrics(conn, asof)
    idx = index_metrics(conn, asof)
    metrics: Dict[str, Any] = {}
    for block in (agg, mkt, mgn, idx):
        if block.get("available"):
            for key in METRIC_COLUMNS:
                if key in block and block[key] is not None:
                    metrics[key] = block[key]
    if agg.get("available"):
        metrics["universe"] = agg.get("universe")
    return {"available": bool(metrics), "trade_date": window[-1], "metrics": metrics,
            "sub_blocks": {"stock_aggregates": agg, "market_row": mkt,
                           "margin": mgn, "index": idx}}


def jsonable(value: Any) -> Any:
    """把 date / numpy 标量压成 JSON 能吃的类型。

    这张卡的读数会被塞进 evidence 一起 `json.dumps`，调用方不一定带 default=str，
    所以在出口就压平，别把序列化责任推给下游。
    """
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if hasattr(value, "item"):          # numpy / pandas 标量
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def normalize_asof(asof: Optional[str]) -> date:
    if not asof:
        return date.today()
    return datetime.strptime(asof.strip().replace("-", ""), "%Y%m%d").date()


def build_block(asof: Optional[str] = None, backfill: int = 0) -> Dict[str, Any]:
    """生成模块 1 的 extreme_state 证据区块。"""
    asof_date = normalize_asof(asof)
    if BACKEND == Backend.SQLITE:
        return {"available": False, "reason": "extreme state requires PostgreSQL backend"}
    try:
        with get_connection() as conn:
            ensure_table(conn)
            if backfill:
                days = trading_days(conn, asof_date, backfill)
                for i, d in enumerate(days):
                    day = compute_day(conn, d)
                    if day.get("available"):
                        upsert_metrics(conn, day["trade_date"], day["metrics"])
                    if (i + 1) % 20 == 0:
                        print(f"[backfill] {i + 1}/{len(days)} ({d})", file=sys.stderr)
            today = compute_day(conn, asof_date)
            if not today.get("available"):
                return {"available": False, "reason": today.get("reason", "no metrics computed"),
                        "sub_blocks": today.get("sub_blocks")}
            upsert_metrics(conn, today["trade_date"], today["metrics"])
            history = load_history(conn, asof_date)
    except Exception as exc:  # noqa: BLE001  研报不因这张卡失败而中断
        return {"available": False, "reason": f"extreme state unavailable: {exc}"}

    result = score(history, today["metrics"])
    # 出清区看近 10 日：恐慌不必当天见底，等确认反而会把最肥的一段让掉
    recent = recent_scores(history, HISTORY_DAYS)
    zone_window = [x for x in recent[-ZONE_LOOKBACK:]]
    result["washout"]["zone"] = bool(
        result["washout"]["score"] >= 2 or any(x["washout"] >= 2 for x in zone_window)
    )
    result["washout"]["zone_lookback_days"] = ZONE_LOOKBACK
    result["recent"] = recent
    return jsonable({
        "available": True,
        "asof": str(asof_date),
        "data_through": str(today["trade_date"]),
        "is_current": today["trade_date"] == asof_date,
        "benchmark": (today["sub_blocks"]["index"] or {}).get("ts_code"),
        "readings": today["metrics"],
        "history_days": int(len(history)),
        **result,
        "sub_blocks": today["sub_blocks"],
    })


def recent_scores(history: pd.DataFrame, days: int) -> List[Dict[str, Any]]:
    """最近 N 个交易日各自的出清分 / 顶部分（每天只用当天及之前的历史算阈值）。

    既用来判"近 10 日内是否进过出清区"，也是 HTML 极值轴时间轴的数据源。
    """
    out: List[Dict[str, Any]] = []
    for _, row in history.tail(days).iterrows():
        day_metrics = {c: (float(row[c]) if pd.notna(row[c]) else None)
                       for c in METRIC_COLUMNS if c != "index_high20"}
        past = history[history["trade_date"] <= row["trade_date"]]
        s = score(past, day_metrics)
        out.append({
            "date": str(row["trade_date"]),
            "washout": s["washout"]["score"],
            "top": s["top"]["score"],
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="极值状态卡：底部出清分 + 顶部拥挤分（占比/分位口径）")
    ap.add_argument("--asof", default=None, help="分析日 YYYYMMDD 或 YYYY-MM-DD，默认今天；含当日数据")
    ap.add_argument("--backfill", type=int, default=0,
                    help="先回填最近 N 个交易日的指标再出卡（首次使用建议 300）")
    args = ap.parse_args()
    block = build_block(args.asof, args.backfill)
    print(json.dumps(block, ensure_ascii=False, indent=2, default=str))
    return 0 if block.get("available") else 1


if __name__ == "__main__":
    sys.exit(main())
