#!/usr/bin/env python3
"""成交额集中度卡（turnover_concentration）——盘后研报模块 1 的第四张机判卡。

**这张卡解决什么。** 原始成交额集中度 CR_p（成交额前 ceil(N×p) 名个股之和占全市场
成交额之比，p 取 1% 与 5%）会被大盘量能污染：缩量市里尾部小票先失血，头部占比机械
抬升；放量市里雨露均沾，头部占比机械回落。直接拿 CR 的历史分位做判读，会把"大盘
缩量"误读成"资金向头部集中"。这张卡做两件事把大盘因素剥掉：

  1. **环境匹配分位**：历史日按「当日总成交额 / 自身 20 日均总成交额」分缩量
     （<0.85）/ 常态 / 放量（>1.15）三桶，今天的 CR 分位只在同桶历史日内算——
     回答"跟量能环境相似的日子比，今天集中得反常吗"。用比值而不是绝对额分桶，
     是因为绝对额有长期漂移（两年前的天量是今天的常态），比值是平稳的。
  2. **增速差分解**：Δlog CR = Δlog head − Δlog total。大盘整体涨缩在减法里
     约掉，剩下的就是"头部相对全市场的超额吸筹"。判读据此区分三种驱动：

       被动集中（passive）      head 5 日增速 ≤ 0——头部自身没增量，CR 升纯粹
                               因为尾部掉得更快，是缩量市的机械现象，不是过热。
       存量搬家（rotation）    head > 0 而 total ≤ 0——头部从全市场虹吸流动性。
       增量过热（overheat）    head > 0 且 total > 0 且头部绝对额历史分位 ≥ 85
                               ——全市场放量、头部吸得更快、绝对水位也高。

  状态机（5 日窗为主、20 日窗为确认，两窗同态才算 confirmed）：

      无条件分位 < 70            → normal（常态集中，不解读）
      ≥ 70 且 head_5d ≤ 0        → passive
      ≥ 70 且 total_5d ≤ 0       → rotation（head_5d > 0 隐含）
      ≥ 70 且 head 分位 < 85     → inflow（温和主动）
      其余                        → overheat

**口径与纪律。** 逐日指标落 `dms_concentration_daily` 表；CR 分位的滚动窗口与极值卡
同纪律（500 日窗、最少 250 日、来源披露）。本卡只给确定性读数与状态标签，不给方向
结论；判读规则未做前瞻回测（那是独立工程，同 `evals/trend_state_review_2026-08.md`
对极值卡做过的那种），`confidence_note` 必须照实写明。首次使用先跑
`--backfill 500` 把历史补起来；环境桶内样本不足 60 日时条件分位回退无条件分位，
`source` 字段写明。

用法：
  python scripts/turnover_concentration.py --asof 20260807
  python scripts/turnover_concentration.py --asof 20260807 --backfill 500
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))
from db_core import BACKEND, Backend, get_connection  # noqa: E402

# 集中度档位：1% ≈ 50 只（与既有 Top50 口径衔接，核心头部），5% ≈ 260 只（宽头部）。
# skillhub 原版还有 3%，与 5% 信息高度重叠，不取。
P_LEVELS = (("1pct", 0.01), ("5pct", 0.05))

# 量能环境桶：当日总成交额 / 自身 20 日均。绝对阈值而不是分位阈值——"缩量 15%"
# 的语义比"处于 1/3 分位"更可解释，且桶的稳定性让条件分位的历史样本可比。
ENV_SHRINK_MAX = 0.85
ENV_EXPAND_MIN = 1.15
ENV_WARMUP = 20                 # 环境比值需要的前置天数

ROLL_WINDOW = 500               # 分位窗口（交易日），与极值卡同纪律
ROLL_MIN = 250                  # 不足此长度不出分位
ENV_BUCKET_MIN = 60             # 环境桶内样本下限，不足回退无条件分位

HIGH_PCT = 70.0                 # 无条件分位过此线才进入被动/主动判读
ANOMALY_PCT = 80.0              # 环境匹配分位过此线标记"剥离环境后仍反常"
OVERHEAT_HEAD_PCT = 85.0        # 头部绝对额历史分位过此线才允许"过热"表述
PRIMARY_WINDOW = 5              # 主判读增速窗口（日）
CONFIRM_WINDOW = 20             # 确认窗口（日）

YI = 1.0e5                      # stock_daily.amount 千元 → 亿元

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS dms_concentration_daily (
    trade_date    DATE PRIMARY KEY,
    universe      INTEGER,
    k_1pct        INTEGER,
    k_5pct        INTEGER,
    total_amt     DOUBLE PRECISION,
    head_amt_1pct DOUBLE PRECISION,
    head_amt_5pct DOUBLE PRECISION,
    cr_1pct       DOUBLE PRECISION,
    cr_5pct       DOUBLE PRECISION,
    amt_env_ratio DOUBLE PRECISION,
    env_bucket    TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

METRIC_COLUMNS = [
    "universe", "k_1pct", "k_5pct", "total_amt", "head_amt_1pct", "head_amt_5pct",
    "cr_1pct", "cr_5pct", "amt_env_ratio", "env_bucket",
]


# ---------------------------------------------------------------------------
# 取数与逐日指标
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


def daily_concentration(conn, days: List[date]) -> pd.DataFrame:
    """一批交易日的集中度指标，bulk 拉取后一次算完。

    环境比值需要当日总成交额的 20 日均，所以额外取 `days` 之外的前 ENV_WARMUP
    个交易日做预热——但只输出 `days` 范围内的行。
    """
    warmup_days = trading_days(conn, days[0], ENV_WARMUP + 1)
    fetch_days = sorted(set(warmup_days[:-1] + days)) if warmup_days else list(days)
    placeholders = ", ".join(["%s"] * len(fetch_days))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT trade_date, amount FROM stock_daily "
            f"WHERE trade_date IN ({placeholders}) AND amount IS NOT NULL AND amount > 0",
            fetch_days,
        )
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=["trade_date"] + METRIC_COLUMNS)

    df = pd.DataFrame(rows, columns=["trade_date", "amount"])
    for col in ("trade_date",):
        df[col] = df[col].astype(str)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df = df.dropna(subset=["amount"])

    # 同日按成交额降序后 cumcount/cumsum，一次拿到任意 k 的头部合计
    df = df.sort_values(["trade_date", "amount"], ascending=[True, False], kind="mergesort")
    grp = df.groupby("trade_date", sort=True)["amount"]
    df["rank"] = grp.cumcount() + 1
    df["cum_amt"] = grp.cumsum()
    total = grp.sum().rename("total")
    universe = grp.size().rename("universe")
    daily = pd.concat([universe, total], axis=1)

    for suffix, p in P_LEVELS:
        k = ceil_series(daily["universe"] * p)
        daily[f"k_{suffix}"] = k
        cum = df.set_index(["trade_date", "rank"])["cum_amt"]
        head = cum.reindex([(d, int(kk)) for d, kk in k.items()])
        daily[f"head_amt_{suffix}"] = head.values
        daily[f"cr_{suffix}"] = 100.0 * daily[f"head_amt_{suffix}"] / daily["total"]

    daily = daily.rename(columns={"total": "total_amt"})
    # 环境比值：当日总成交额 / 自身 20 日均（预热日参与均值，但不输出）
    ma20 = daily["total_amt"].rolling(ENV_WARMUP, min_periods=ENV_WARMUP).mean()
    daily["amt_env_ratio"] = (daily["total_amt"] / ma20).round(4)
    daily["env_bucket"] = daily["amt_env_ratio"].map(env_bucket_of)
    for col in ("total_amt", "head_amt_1pct", "head_amt_5pct"):
        daily[col] = (daily[col] / YI).round(2)
    for suffix, _p in P_LEVELS:
        daily[f"cr_{suffix}"] = daily[f"cr_{suffix}"].round(3)

    keep = daily.loc[daily.index.isin({str(d) for d in days})].copy()
    keep = keep.reset_index().rename(columns={"index": "trade_date"})
    ordered = ["trade_date"] + METRIC_COLUMNS
    return keep[[c for c in ordered if c in keep.columns]]


def ceil_series(series: pd.Series) -> pd.Series:
    """math.ceil 的向量化。减 1e-9 消浮点误差：5400×0.01 会算成 54.00000000000001，
    直接 ceil 会把恰好整数的 k 抬大 1。"""
    return series.map(lambda v: math.ceil(v - 1e-9) if pd.notna(v) else None).astype("Int64")


def env_bucket_of(ratio: Optional[float]) -> Optional[str]:
    if ratio is None or pd.isna(ratio):
        return None
    if ratio < ENV_SHRINK_MAX:
        return "shrink"
    if ratio > ENV_EXPAND_MIN:
        return "expand"
    return "normal"


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(TABLE_DDL)


def upsert_frame(conn, frame: pd.DataFrame) -> int:
    cols = [c for c in METRIC_COLUMNS if c in frame.columns]
    if frame.empty or not cols:
        return 0

    def coerce(col: str, value: Any) -> Any:
        if pd.isna(value):
            return None
        if col in ("universe", "k_1pct", "k_5pct"):
            return int(value)
        if col == "env_bucket":
            return str(value)
        return float(value)

    rows = [[str(r["trade_date"])] + [coerce(c, r.get(c)) for c in cols]
            for _, r in frame.iterrows()]
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    placeholders = ", ".join(["%s"] * (len(cols) + 1))
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO dms_concentration_daily (trade_date, {', '.join(cols)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (trade_date) DO UPDATE SET {assignments}, updated_at = NOW()",
            rows,
        )
    return len(rows)


def load_history(conn, asof: date) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT trade_date, {', '.join(METRIC_COLUMNS)} "
            "FROM dms_concentration_daily WHERE trade_date <= %s "
            "ORDER BY trade_date DESC LIMIT %s",
            (asof, ROLL_WINDOW + CONFIRM_WINDOW),
        )
        rows = cur.fetchall()
    df = pd.DataFrame(list(reversed(rows)), columns=["trade_date"] + METRIC_COLUMNS)
    df["trade_date"] = df["trade_date"].astype(str)
    numeric = [c for c in METRIC_COLUMNS if c not in ("env_bucket",)]
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# 分位与判读（纯函数，单测覆盖）
# ---------------------------------------------------------------------------
def percentile_of(history: pd.Series, value: float, min_n: int = ROLL_MIN) -> Optional[float]:
    """value 在 history（不含当日）中的百分位分位，0~100。样本不足返回 None。"""
    series = history.dropna()
    if len(series) < min_n:
        return None
    return round(100.0 * float((series < value).sum()) / len(series), 1)


def conditional_percentile(history: pd.DataFrame, column: str, value: float,
                           bucket: Optional[str]) -> Tuple[Optional[float], str, int]:
    """环境匹配分位：只在同量能环境桶的历史日里比分位。

    前置：整体历史 ≥ ROLL_MIN（与无条件分位同一资格线）。桶内样本不足
    ENV_BUCKET_MIN 或当日无桶时回退无条件分位，来源照实写。
    """
    if bucket and len(history[column].dropna()) >= ROLL_MIN:
        sub = history.loc[history["env_bucket"] == bucket, column].dropna()
        if len(sub) >= ENV_BUCKET_MIN:
            pct = percentile_of(sub, value, min_n=ENV_BUCKET_MIN)
            if pct is not None:
                return pct, "env_matched", int(len(sub))
    uncond = percentile_of(history[column], value)
    source = "unconditional_fallback" if bucket else "unconditional_no_bucket"
    return uncond, source, 0


def growth(series: pd.Series, window: int) -> Optional[float]:
    """末值相对 window 日前的涨幅（%）。样本不足返回 None。"""
    vals = series.dropna()
    if len(vals) < window + 1:
        return None
    base, last = float(vals.iloc[-(window + 1)]), float(vals.iloc[-1])
    if base <= 0:
        return None
    return round(100.0 * (last / base - 1.0), 2)


def classify(uncond_pct: Optional[float], head_g: Optional[float],
             total_g: Optional[float], head_amt_pct: Optional[float]) -> str:
    """状态机：常态 / 被动集中 / 存量搬家 / 温和主动 / 增量过热。"""
    if uncond_pct is None or head_g is None or total_g is None:
        return "insufficient_history"
    if uncond_pct < HIGH_PCT:
        return "normal"
    if head_g <= 0:
        return "passive"
    if total_g <= 0:
        return "rotation"
    if head_amt_pct is not None and head_amt_pct >= OVERHEAT_HEAD_PCT:
        return "overheat"
    return "inflow"


STATE_LABELS = {
    "normal": "常态集中（不解读）",
    "passive": "被动集中（尾部失血，机械抬升）",
    "rotation": "存量搬家型主动集中（头部虹吸流动性）",
    "inflow": "主动集中（温和）",
    "overheat": "增量型主动过热（放量+头部加速吸筹+绝对水位高）",
    "insufficient_history": "历史不足，不出判读",
}

# HTML 轨迹图回看窗口：与极值卡 / 状态卡时间轴同宽（30 日）
RECENT_DAYS = 30


def recent_states(history: pd.DataFrame, days: int = RECENT_DAYS) -> List[Dict[str, Any]]:
    """最近 N 个交易日各自的 CR 读数与驱动状态（每天只用当天之前的历史算分位）。

    与极值卡的 `recent_scores` 同构：既给 HTML 的集中度轨迹图喂逐日数据，也让
    模型能引用近几日的状态演变。增速按交易日位序取（表预期连续）。
    """
    out: List[Dict[str, Any]] = []
    if history.empty:
        return out
    head = history["head_amt_1pct"]
    total = history["total_amt"]
    head_g5 = (head / head.shift(PRIMARY_WINDOW) - 1.0) * 100.0
    total_g5 = (total / total.shift(PRIMARY_WINDOW) - 1.0) * 100.0
    for pos in range(max(0, len(history) - days), len(history)):
        row = history.iloc[pos]
        past = history.iloc[:pos]
        cr = row.get("cr_1pct")
        if pd.isna(cr):
            continue
        uncond_pct = percentile_of(past["cr_1pct"], float(cr))
        bucket = row.get("env_bucket")
        if isinstance(bucket, float) and pd.isna(bucket):
            bucket = None
        _cond, _src, _n = conditional_percentile(past, "cr_1pct", float(cr), bucket)
        head_amt = row.get("head_amt_1pct")
        head_pct = percentile_of(past["head_amt_1pct"], float(head_amt)) \
            if pd.notna(head_amt) else None
        state = classify(
            uncond_pct,
            None if pd.isna(head_g5.iloc[pos]) else round(float(head_g5.iloc[pos]), 2),
            None if pd.isna(total_g5.iloc[pos]) else round(float(total_g5.iloc[pos]), 2),
            head_pct,
        )
        cr5 = row.get("cr_5pct")
        out.append({
            "date": str(row["trade_date"]),
            "cr_1pct": round(float(cr), 3),
            "cr_5pct": round(float(cr5), 3) if pd.notna(cr5) else None,
            "env_bucket": bucket,
            "state": state,
        })
    return out


def reading_for(history: pd.DataFrame, suffix: str) -> Dict[str, Any]:
    """单个 p 档的完整读数：水平、两层分位、增速分解、双窗状态。"""
    cr_col, head_col = f"cr_{suffix}", f"head_amt_{suffix}"
    if history.empty or pd.isna(history[cr_col].iloc[-1]):
        return {"available": False,
                "reason": f"no {cr_col} reading for the latest day"}
    cr = float(history[cr_col].iloc[-1])
    bucket = history["env_bucket"].iloc[-1] if "env_bucket" in history else None
    if isinstance(bucket, float) and pd.isna(bucket):
        bucket = None

    uncond_pct = percentile_of(history[cr_col].iloc[:-1], cr)
    uncond_sample = int(history[cr_col].iloc[:-1].dropna().shape[0])
    cond_pct, cond_source, cond_sample = conditional_percentile(
        history.iloc[:-1], cr_col, cr, bucket)
    head_amt = history[head_col].iloc[-1]
    head_amt_pct = percentile_of(history[head_col].iloc[:-1], float(head_amt)) \
        if pd.notna(head_amt) else None

    windows = {}
    for wname, w in (("g5", PRIMARY_WINDOW), ("g20", CONFIRM_WINDOW)):
        head_g = growth(history[head_col], w)
        total_g = growth(history["total_amt"], w)
        windows[wname] = {
            "head_growth_pct": head_g,
            "total_growth_pct": total_g,
            "state": classify(uncond_pct, head_g, total_g, head_amt_pct),
        }
    state = windows["g5"]["state"]
    confirmed = state != "normal" and state == windows["g20"]["state"]

    return {
        "available": True,
        "cr_pct": round(cr, 3),
        "k": int(history[f"k_{suffix}"].iloc[-1]),
        "universe": int(history["universe"].iloc[-1]),
        "unconditional_percentile": uncond_pct,
        "unconditional_sample_days": uncond_sample,
        "env_matched_percentile": cond_pct,
        "env_matched_source": cond_source,
        "env_matched_sample_days": cond_sample,
        "anomaly_after_env_adjust": bool(cond_pct is not None and cond_pct >= ANOMALY_PCT),
        "head_amt_yi": round(float(head_amt), 2) if pd.notna(head_amt) else None,
        "head_amt_percentile": head_amt_pct,
        "windows": windows,
        "state": state,
        "state_label": STATE_LABELS[state],
        "confirmed_20d": confirmed,
    }


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def compute_day_rows(conn, asof: date, n_days: int) -> pd.DataFrame:
    days = trading_days(conn, asof, n_days)
    if not days:
        return pd.DataFrame(columns=["trade_date"] + METRIC_COLUMNS)
    return daily_concentration(conn, days)


def build_block(asof: Optional[str] = None, backfill: int = 0) -> Dict[str, Any]:
    """生成模块 1 的 turnover_concentration 证据区块。"""
    asof_date = normalize_asof(asof)
    if BACKEND == Backend.SQLITE:
        return {"available": False, "reason": "turnover concentration requires PostgreSQL backend"}
    try:
        with get_connection() as conn:
            ensure_table(conn)
            if backfill:
                frame = compute_day_rows(conn, asof_date, backfill)
                n = upsert_frame(conn, frame)
                print(f"[backfill] upserted {n} days through {asof_date}", file=sys.stderr)
            # 当日行：已有且未过期就复用，缺失则补算（增量路径只算一天）
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM dms_concentration_daily WHERE trade_date = %s",
                    (asof_date,),
                )
                have_today = bool(cur.fetchone()[0])
            if not have_today:
                last_days = trading_days(conn, asof_date, 1)
                if last_days:
                    frame = daily_concentration(conn, last_days)
                    upsert_frame(conn, frame)
            history = load_history(conn, asof_date)
    except Exception as exc:  # noqa: BLE001  研报不因这张卡失败而中断
        return {"available": False, "reason": f"turnover concentration unavailable: {exc}"}

    if history.empty:
        return {"available": False,
                "reason": "no concentration history; run with --backfill 500 first",
                "hint": "python scripts/turnover_concentration.py --asof <date> --backfill 500"}

    data_through = str(history["trade_date"].iloc[-1])
    total = history["total_amt"].iloc[-1]
    env_ratio = history["amt_env_ratio"].iloc[-1]
    bucket = history["env_bucket"].iloc[-1]
    readings = {suffix: reading_for(history, suffix) for suffix, _p in P_LEVELS}
    ma20 = None
    if pd.notna(total) and pd.notna(env_ratio) and env_ratio:
        ma20 = round(float(total) / float(env_ratio), 2)

    return jsonable({
        "available": True,
        "asof": str(asof_date),
        "data_through": data_through,
        "is_current": data_through == str(asof_date),
        "market_env": {
            "total_amt_yi": round(float(total), 2) if pd.notna(total) else None,
            "total_amt_ma20_yi": ma20,
            "amt_env_ratio": round(float(env_ratio), 4) if pd.notna(env_ratio) else None,
            "env_bucket": None if (isinstance(bucket, float) and pd.isna(bucket)) else bucket,
            "env_bucket_thresholds": {"shrink_below": ENV_SHRINK_MAX,
                                      "expand_above": ENV_EXPAND_MIN},
        },
        "readings": readings,
        "recent": recent_states(history),
        "history_days": int(len(history)),
        "percentile_window_days": ROLL_WINDOW,
        "percentile_min_days": ROLL_MIN,
        "confidence_note": (
            "本卡的判读规则未做前瞻回测（极值卡那种 5 年回放是独立工程），状态标签"
            "是确定性分解的描述，不是预测；被动集中≠看空，主动过热是风险累积度的"
            "表述而非方向结论。引用分位时必须带口径（无条件 / 环境匹配）与样本量。"
        ),
    })


def jsonable(value: Any) -> Any:
    """把 date / numpy / pandas 标量压成 JSON 能吃的类型（同极值卡出口纪律）。"""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def normalize_asof(asof: Optional[str]) -> date:
    if not asof:
        return date.today()
    return datetime.strptime(asof.strip().replace("-", ""), "%Y%m%d").date()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="成交额集中度卡：CR_1%/5% + 环境匹配分位 + 被动/主动分解")
    ap.add_argument("--asof", default=None, help="分析日 YYYYMMDD 或 YYYY-MM-DD，默认今天；含当日数据")
    ap.add_argument("--backfill", type=int, default=0,
                    help="先回填最近 N 个交易日的指标再出卡（首次使用建议 500）")
    args = ap.parse_args()
    block = build_block(args.asof, args.backfill)
    print(json.dumps(block, ensure_ascii=False, indent=2, default=str))
    return 0 if block.get("available") else 1


if __name__ == "__main__":
    sys.exit(main())
