#!/usr/bin/env python3
"""Deterministic pool rules: entry screen, trend metrics, exit hits, entry signals.

Pure functions over plain dicts — no network, no DB — so every rule is unit
testable and back-testable.  Thresholds come from assets/pool_rules.yaml.
The functions report *facts* (which rule fired, at what price); trend
grading, theme attribution, news triage and the wording of a trade reminder
are the model's job (SKILL.md).

Bar dict (ascending by trade_date):
    trade_date 'YYYY-MM-DD', open, high, low, close, pre_close, pct_chg,
    amount (Tushare 千元), adj_factor (optional, defaults to 1.0)

Member dict:
    ts_code, name, entry_date, source ('auto'|'manual'), status
    ('active'|'watch'|'removed'), limit_price, entry_low, launch_price,
    entry_amount, entry_adj_factor, watch_since
"""

from __future__ import annotations

import copy
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Optional

RULES_PATH = Path(__file__).resolve().parents[1] / "assets" / "pool_rules.yaml"

DEFAULT_RULES: dict[str, Any] = {
    "entry": {
        "amount_min_yi": 3,
        "turnover_max_pct": 15,
        "turnover_field": "turnover_rate",
        "first_limit_time_max": "103000",
        "exclude_one_word": True,
        "exclude_st": True,
        "exclude_listed_days": 20,
    },
    "exit": {
        "remove_break_entry_low": True,
        "remove_drawdown_pct": 20,
        "watch_below_ma20_days": 2,
        "watch_big_bear_pct": -5,
        "watch_big_bear_amount_ratio": 2.0,
        "time_stop_days_in_pool": 15,
        "time_stop_days_since_peak": 10,
        "watch_to_remove_days": 3,
    },
    "signal": {
        "breakout": {
            "min_consolidation_days": 3,
            "min_amount_ratio": 1.5,
            "near_pct": 3,
            "near_max_amount_ratio": 0.8,
            "fail_buffer_pct": 3,
        },
        "pullback": {
            "supports": ["limit_price", "ma10", "ma20"],
            "min_pullback_pct": 5,
            "touch_tolerance_pct": 2,
            "hold_tolerance_pct": 1,
            "max_amount_ratio": 0.7,
            "max_amount_vs_entry": 0.5,
            "min_pct_chg": -5,
            "fail_buffer_pct": 3,
        },
    },
    "track": {"lookback_calendar_days": 60},
}

SUPPORT_LABELS = {"limit_price": "涨停价", "ma10": "MA10", "ma20": "MA20"}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_rules(path: Optional[str] = None) -> dict:
    target = Path(path) if path else RULES_PATH
    if not target.exists():
        return copy.deepcopy(DEFAULT_RULES)
    import yaml

    with open(target, "r", encoding="utf-8") as fh:
        return _deep_merge(DEFAULT_RULES, yaml.safe_load(fh) or {})


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
        return out if out == out else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _r(value: Optional[float], nd: int = 2) -> Optional[float]:
    return None if value is None else round(value, nd)


def _pct(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 100, 2)


def round_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def board_limit_pct(ts_code: str, name: str = "") -> float:
    """涨跌幅限制：ST 5%，创业板/科创板 20%，北交所 30%，其余 10%。"""
    code = (ts_code or "").split(".")[0]
    if "ST" in (name or "").upper():
        return 0.05
    if ts_code.endswith(".BJ") or code.startswith(("4", "8", "92")):
        return 0.30
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def board_up_limit(ts_code: str, name: str, pre_close: float) -> float:
    return round_price(pre_close * (1 + board_limit_pct(ts_code, name)))


def is_limit_close(close: Optional[float], up_limit: Optional[float]) -> bool:
    return close is not None and up_limit is not None and close >= up_limit - 0.005


def amount_yi(amount_thousand: Optional[float]) -> Optional[float]:
    return None if amount_thousand is None else amount_thousand / 1e5


def _to_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).replace("-", "")[:8]
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 1. Entry screen
# --------------------------------------------------------------------------- #
def screen_limit_up(row: dict, rules: dict, trade_date: str) -> tuple[bool, list[str], dict]:
    """Return (passed, reject_reasons, facts) for one stock-day.

    row keys: ts_code, name, open, high, low, close, pre_close, amount,
    up_limit (optional; board rule fallback), turnover_rate[_f],
    first_time ('HHMMSS' or None), list_date.
    """
    cfg = rules["entry"]
    ts_code = row.get("ts_code") or ""
    name = row.get("name") or ""
    close, low, pre_close = _f(row.get("close")), _f(row.get("low")), _f(row.get("pre_close"))
    up_limit = _f(row.get("up_limit"))
    if up_limit is None and pre_close is not None:
        up_limit = board_up_limit(ts_code, name, pre_close)

    reasons: list[str] = []
    if not is_limit_close(close, up_limit):
        reasons.append("not_limit_close")
    if cfg.get("exclude_st") and "ST" in name.upper():
        reasons.append("st")
    if cfg.get("exclude_one_word") and is_limit_close(low, up_limit):
        reasons.append("one_word")

    amt = amount_yi(_f(row.get("amount")))
    if amt is None or amt < float(cfg["amount_min_yi"]):
        reasons.append("amount_below_min")

    turnover = _f(row.get(cfg.get("turnover_field") or "turnover_rate"))
    if turnover is None:
        reasons.append("turnover_missing")
    elif turnover >= float(cfg["turnover_max_pct"]):
        reasons.append("turnover_too_high")

    first_time = str(row.get("first_time") or "").replace(":", "").strip()
    if not first_time:
        reasons.append("first_time_missing")
    elif first_time.zfill(6) > str(cfg["first_limit_time_max"]).zfill(6):
        reasons.append("first_time_after_cutoff")

    listed_days_min = int(cfg.get("exclude_listed_days") or 0)
    list_d, trade_d = _to_date(row.get("list_date")), _to_date(trade_date)
    listed_days = (trade_d - list_d).days if list_d and trade_d else None
    if listed_days_min and listed_days is not None and listed_days < listed_days_min:
        reasons.append("newly_listed")

    facts = {
        "ts_code": ts_code,
        "name": name,
        "close": _r(close),
        "up_limit": _r(up_limit),
        "low": _r(low),
        "pre_close": _r(pre_close),
        "amount_yi": _r(amt),
        "turnover_pct": _r(turnover),
        "first_limit_time": first_time.zfill(6) if first_time else None,
        "listed_days": listed_days,
    }
    return (not reasons), reasons, facts


# --------------------------------------------------------------------------- #
# 2. Trend metrics
# --------------------------------------------------------------------------- #
def _ma(values: list[float], end: int, n: int) -> Optional[float]:
    if end + 1 < n:
        return None
    window = values[end + 1 - n : end + 1]
    return sum(window) / n


def _entry_index(bars: list[dict], entry_date: str) -> Optional[int]:
    for idx, bar in enumerate(bars):
        if str(bar["trade_date"]) >= entry_date:
            return idx
    return None


def compute_metrics(bars: list[dict], member: dict) -> Optional[dict]:
    """Facts about the latest bar relative to the member's entry anchors.

    Prices are forward-adjusted to the latest bar (so the latest close stays
    raw) and anchors recorded at entry are adjusted the same way, so 除权
    never moves a defence line by accident.
    """
    if not bars:
        return None
    e = _entry_index(bars, str(member["entry_date"]))
    if e is None:
        return None
    i = len(bars) - 1
    adj_last = _f(bars[i].get("adj_factor")) or 1.0
    factors = [(_f(b.get("adj_factor")) or adj_last) / adj_last for b in bars]
    close = [_f(b["close"]) * k for b, k in zip(bars, factors)]
    high = [_f(b["high"]) * k for b, k in zip(bars, factors)]
    low = [_f(b["low"]) * k for b, k in zip(bars, factors)]
    amount = [_f(b.get("amount")) or 0.0 for b in bars]

    entry_k = (_f(member.get("entry_adj_factor")) or adj_last) / adj_last
    limit_price = (_f(member.get("limit_price")) or _f(bars[e]["close"])) * entry_k
    entry_low = (_f(member.get("entry_low")) or _f(bars[e]["low"])) * entry_k
    launch_price = _f(member.get("launch_price"))
    launch_price = launch_price * entry_k if launch_price is not None else None
    entry_close = close[e]

    post_close = close[e : i + 1]
    peak_close = max(post_close)
    peak_idx = e + max(idx for idx, v in enumerate(post_close) if v == peak_close)

    ma = {n: [_ma(close, k, n) for k in range(i + 1)] for n in (5, 10, 20)}
    # 只数入池之后的日子：涨停当天还在 MA20 下方（超跌反弹）不算走坏
    below_ma20_streak = 0
    for k in range(i, e, -1):
        if ma[20][k] is not None and close[k] < ma[20][k]:
            below_ma20_streak += 1
        else:
            break

    prev5 = amount[max(0, i - 5) : i]
    amount_ratio_5d = amount[i] / (sum(prev5) / len(prev5)) if prev5 and sum(prev5) > 0 else None
    entry_amount = _f(member.get("entry_amount")) or amount[e]
    amount_vs_entry = amount[i] / entry_amount if entry_amount else None

    # 平台：入池日到昨天的最高价，以及它之后横了几天
    if i > e:
        plat_high = max(high[e:i])
        plat_idx = e + max(idx for idx, v in enumerate(high[e:i]) if v == plat_high)
        consolidation_days = (i - 1) - plat_idx
    else:
        plat_high, consolidation_days = None, 0

    last = bars[i]
    up_limit_today = board_up_limit(member["ts_code"], member.get("name") or "", _f(last["pre_close"])) \
        if _f(last.get("pre_close")) else None

    return {
        "trade_date": str(last["trade_date"]),
        "close": _r(close[i]),
        "high": _r(high[i]),
        "low": _r(low[i]),
        "pct_chg": _r(_f(last.get("pct_chg"))),
        "amount_yi": _r(amount_yi(amount[i])),
        "days_in_pool": i - e,
        "entry_close": _r(entry_close),
        "limit_price": _r(limit_price),
        "entry_low": _r(entry_low),
        "launch_price": _r(launch_price),
        "ret_since_entry_pct": _pct(close[i] / entry_close - 1),
        "max_ret_since_entry_pct": _pct(peak_close / entry_close - 1),
        "peak_close": _r(peak_close),
        "drawdown_from_peak_pct": _pct(close[i] / peak_close - 1),
        "days_since_peak": i - peak_idx,
        "ma5": _r(ma[5][i]),
        "ma10": _r(ma[10][i]),
        "ma20": _r(ma[20][i]),
        "above_ma5": None if ma[5][i] is None else close[i] >= ma[5][i],
        "above_ma10": None if ma[10][i] is None else close[i] >= ma[10][i],
        "above_ma20": None if ma[20][i] is None else close[i] >= ma[20][i],
        "below_ma20_streak": below_ma20_streak,
        "amount_ratio_5d": _r(amount_ratio_5d),
        "amount_vs_entry": _r(amount_vs_entry),
        "platform_high": _r(plat_high),
        "consolidation_days": consolidation_days,
        "vs_entry_low_pct": _pct(close[i] / entry_low - 1),
        "vs_launch_pct": _pct(close[i] / launch_price - 1) if launch_price else None,
        "relimit_today": i > e and is_limit_close(_f(last["close"]), up_limit_today),
    }


# --------------------------------------------------------------------------- #
# 3. Exit rules
# --------------------------------------------------------------------------- #
def exit_hits(m: dict, rules: dict) -> list[dict]:
    cfg = rules["exit"]
    hits: list[dict] = []
    if cfg.get("remove_break_entry_low") and m["close"] < m["entry_low"]:
        hits.append({"rule": "break_entry_low", "level": "remove",
                     "fact": f"收盘 {m['close']} 跌破涨停日最低价 {m['entry_low']}"})
    dd_limit = -float(cfg["remove_drawdown_pct"])
    if m["drawdown_from_peak_pct"] is not None and m["drawdown_from_peak_pct"] <= dd_limit:
        hits.append({"rule": "drawdown", "level": "remove",
                     "fact": f"距入池后最高收盘 {m['peak_close']} 回撤 {m['drawdown_from_peak_pct']}%"})
    if m["below_ma20_streak"] >= int(cfg["watch_below_ma20_days"]):
        hits.append({"rule": "below_ma20", "level": "watch",
                     "fact": f"连续 {m['below_ma20_streak']} 日收在 MA20（{m['ma20']}）之下"})
    ratio = m["amount_ratio_5d"]
    if (m["pct_chg"] is not None and m["pct_chg"] <= float(cfg["watch_big_bear_pct"])
            and ratio is not None and ratio >= float(cfg["watch_big_bear_amount_ratio"])):
        hits.append({"rule": "big_bear_volume", "level": "watch",
                     "fact": f"放量大阴线：跌 {m['pct_chg']}%，成交额为前 5 日均额 {ratio} 倍"})
    if (m["days_in_pool"] >= int(cfg["time_stop_days_in_pool"])
            and m["days_since_peak"] >= int(cfg["time_stop_days_since_peak"])
            and m["above_ma10"] is False):
        hits.append({"rule": "time_stop", "level": "watch",
                     "fact": f"入池 {m['days_in_pool']} 日、{m['days_since_peak']} 日未创新高且跌破 MA10（{m['ma10']}）"})
    return hits


def decide_status(member: dict, hits: list[dict], watch_days: int, rules: dict) -> dict:
    """Map today's hits to a status transition.

    watch_days = trading days already spent in watch before today (0 if not
    in watch).  Manual members never auto-remove: a remove-level hit puts them
    in watch with suggest_remove so the user decides.
    """
    current = member.get("status") or "active"
    manual = member.get("source") == "manual"
    removes = [h for h in hits if h["level"] == "remove"]
    watches = [h for h in hits if h["level"] == "watch"]
    limit = int(rules["exit"]["watch_to_remove_days"])

    new, reason, suggest_remove = current, None, False
    if removes:
        reason = "；".join(h["fact"] for h in removes)
        if manual:
            new, suggest_remove = "watch", True
        else:
            new = "removed"
    elif watches:
        reason = "；".join(h["fact"] for h in watches)
        if current == "watch" and watch_days + 1 >= limit:
            if manual:
                suggest_remove = True
            else:
                new = "removed"
                reason = f"预警 {watch_days + 1} 日未修复：{reason}"
        else:
            new = "watch"
    elif current == "watch":
        new, reason = "active", "预警条件全部解除"
    return {"from": current, "to": new, "changed": new != current,
            "reason": reason, "suggest_remove": suggest_remove}


# --------------------------------------------------------------------------- #
# 4. Entry signals（交易介入提醒参考：突破 / 缩量回踩）
# --------------------------------------------------------------------------- #
def entry_signals(m: dict, rules: dict) -> list[dict]:
    out: list[dict] = []
    if m["days_in_pool"] < 1:
        return out
    b = rules["signal"]["breakout"]
    ratio = m["amount_ratio_5d"]
    plat = m["platform_high"]

    if plat and m["consolidation_days"] >= int(b["min_consolidation_days"]):
        if m["close"] > plat and ratio is not None and ratio >= float(b["min_amount_ratio"]):
            out.append({
                "type": "breakout", "label": "放量突破平台",
                "trigger_price": plat,
                "invalidation_price": _r(plat * (1 - float(b["fail_buffer_pct"]) / 100)),
                "fact": (f"收盘 {m['close']} 站上入池后平台高点 {plat}（平台横 {m['consolidation_days']} 日），"
                         f"成交额为前 5 日均额 {ratio} 倍"),
            })
        elif (plat * (1 - float(b["near_pct"]) / 100) <= m["close"] <= plat
              and ratio is not None and ratio <= float(b["near_max_amount_ratio"])):
            out.append({
                "type": "breakout_near", "label": "临近突破（缩量蓄势）",
                "trigger_price": plat,
                "invalidation_price": None,
                "fact": (f"收盘 {m['close']} 距平台高点 {plat} 不到 {b['near_pct']}%，"
                         f"成交额缩到前 5 日均额 {ratio} 倍"),
            })

    p = rules["signal"]["pullback"]
    dd = m["drawdown_from_peak_pct"]
    shrink = ((ratio is not None and ratio <= float(p["max_amount_ratio"]))
              or (m["amount_vs_entry"] is not None and m["amount_vs_entry"] <= float(p["max_amount_vs_entry"])))
    if (dd is not None and dd <= -float(p["min_pullback_pct"]) and shrink
            and m["pct_chg"] is not None and m["pct_chg"] > float(p["min_pct_chg"])):
        touched = []
        for key in p["supports"]:
            level = m.get(key)
            if not level:
                continue
            touch = m["low"] <= level * (1 + float(p["touch_tolerance_pct"]) / 100)
            hold = m["close"] >= level * (1 - float(p["hold_tolerance_pct"]) / 100)
            if touch and hold:
                touched.append((abs(m["low"] / level - 1), key, level))
        if touched:
            _, key, level = min(touched)
            out.append({
                "type": "pullback", "label": f"缩量回踩{SUPPORT_LABELS.get(key, key)}",
                "support": key,
                "trigger_price": level,
                "invalidation_price": _r(level * (1 - float(p["fail_buffer_pct"]) / 100)),
                "fact": (f"自高点回撤 {dd}%，最低 {m['low']} 踩到{SUPPORT_LABELS.get(key, key)} {level} 后收 {m['close']}；"
                         f"成交额为前 5 日均额 {ratio} 倍、涨停日的 {m['amount_vs_entry']} 倍"),
                "also_touched": [k for _, k, _ in sorted(touched)[1:]],
            })
    return out
