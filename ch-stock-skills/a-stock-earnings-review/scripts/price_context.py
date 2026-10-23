#!/usr/bin/env python3
"""Price & valuation evidence for earnings cross-validation (基本面 × 资金面 × 定价).

For each requested stock this computes, deterministically:

- **events**: one block per earnings anchor (业绩预告首次披露 / 正式报告披露),
  each with the run-up *before* the market knew (20/60/120/250 交易日 absolute
  return and excess vs 沪深300, 一年区间位置, 距高点回撤, 量能变化), the
  reaction on the first trading day on/after the anchor (gap / day return /
  volume ratio / 回补状态) and the drift since.
- **current**: where the stock sits today (区间涨幅与超额, 位置, 均线).
- **valuation**: PE_TTM / PB / PS current values and 1/3/5-year percentile
  bands, plus a market-cap-vs-PE decomposition of the last 120 trading days
  (how much of the move was earnings, how much was re-rating).

No judgement here: whether earnings were already priced in, which multiple is
the right anchor, cheap vs dear — all of that is the model's call
(references/price_validation.md, references/pricing.md).

Usage (run inside the working directory that holds reports/):
    python3 scripts/price_context.py --period 20260930 --codes 300604.SZ,000626.SZ
    python3 scripts/price_context.py --codes 300604.SZ --anchor 300604.SZ:forecast:20261022
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import market_data  # noqa: E402

PRE_WINDOWS = (20, 60, 120, 250)
BAR_COUNT = 520            # ~2 years: a 250-day pre-window ending a year back still fits
DECOMP_WINDOW = 120


def _r(v: Optional[float], nd: int = 2) -> Optional[float]:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(v, nd)


def _px(bar: Dict[str, Any], field: str = "close") -> float:
    return bar[field] * bar.get("adj", 1.0)


def _iso(ymd: str) -> str:
    ymd = ymd.replace("-", "")
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"


# --------------------------------------------------------------------------- #
# pure computations (bars ascending, adj-aware)
# --------------------------------------------------------------------------- #
class Benchmark:
    def __init__(self, bars: List[Dict[str, Any]]):
        self.dates = [b["date"] for b in bars]
        self.close = [b["close"] for b in bars]

    def close_on(self, date: str) -> Optional[float]:
        i = bisect_right(self.dates, date) - 1  # last benchmark close on/before date
        return self.close[i] if i >= 0 else None

    def ret(self, d0: str, d1: str) -> Optional[float]:
        c0, c1 = self.close_on(d0), self.close_on(d1)
        return (c1 / c0 - 1.0) * 100.0 if c0 and c1 else None


def window_return(bars: List[Dict[str, Any]], end: int, n: int,
                  bench: Optional[Benchmark]) -> Dict[str, Optional[float]]:
    start = end - n
    if start < 0 or end >= len(bars):
        return {"ret_pct": None, "excess_pp": None}
    ret = (_px(bars[end]) / _px(bars[start]) - 1.0) * 100.0
    b = bench.ret(bars[start]["date"], bars[end]["date"]) if bench else None
    return {"ret_pct": _r(ret), "excess_pp": _r(ret - b) if b is not None else None}


def position_stats(bars: List[Dict[str, Any]], end: int, n: int = 250) -> Dict[str, Any]:
    window = bars[max(0, end - n + 1): end + 1]
    if len(window) < 2:
        return {"pos_pct": None, "from_high_pct": None, "bars": len(window)}
    hi = max(_px(b, "high") for b in window)
    lo = min(_px(b, "low") for b in window)
    close = _px(bars[end])
    pos = (close - lo) / (hi - lo) * 100.0 if hi > lo else None
    return {"pos_pct": _r(pos, 1), "from_high_pct": _r((close / hi - 1.0) * 100.0), "bars": len(window)}


def _avg_vol(bars: List[Dict[str, Any]], end: int, n: int) -> Optional[float]:
    window = [b["vol"] for b in bars[max(0, end - n + 1): end + 1] if b.get("vol")]
    return sum(window) / len(window) if len(window) >= max(5, n // 2) else None


def event_block(bars: List[Dict[str, Any]], anchor: str, bench: Optional[Benchmark],
                gap_min: float) -> Dict[str, Any]:
    """Run-up before / reaction on / drift after one earnings anchor (YYYY-MM-DD)."""
    dates = [b["date"] for b in bars]
    idx = bisect_left(dates, anchor)  # reaction day = first trading day on/after the anchor
    if idx >= len(bars):
        return {"anchor": anchor, "status": "pending", "note": "公告后尚无交易日"}
    if idx == 0:
        return {"anchor": anchor, "status": "no_history", "note": "公告前无可用 K 线"}
    pre_i = idx - 1
    pre, r = bars[pre_i], bars[idx]
    pre_close = _px(pre)
    run_up = {f"d{n}": window_return(bars, pre_i, n, bench) for n in PRE_WINDOWS}
    v20, v120 = _avg_vol(bars, pre_i, 20), _avg_vol(bars, pre_i, 120)
    gap = (_px(r, "open") / pre_close - 1.0) * 100.0
    gap_dir = "up" if gap >= gap_min else "down" if gap <= -gap_min else None
    after = bars[idx:]
    if gap_dir == "up":
        gap_status = "filled" if min(_px(b, "low") for b in after) < pre_close else "intact"
    elif gap_dir == "down":
        gap_status = "filled" if max(_px(b, "high") for b in after) > pre_close else "intact"
    else:
        gap_status = "none"
    last = bars[-1]
    since = (_px(last) / pre_close - 1.0) * 100.0
    b_since = bench.ret(pre["date"], last["date"]) if bench else None
    post_high = max(_px(b, "high") for b in after)
    return {
        "anchor": anchor,
        "status": "ok",
        "reaction_date": r["date"],
        "pre_close_date": pre["date"],
        "run_up": run_up,
        "pre_position": position_stats(bars, pre_i),
        "pre_vol_ratio_20_120": _r(v20 / v120) if v20 and v120 else None,
        "reaction": {
            "gap_open_pct": _r(gap),
            "day_pct": _r((_px(r) / pre_close - 1.0) * 100.0),
            "vol_ratio_20d": _r(r["vol"] / v20) if v20 else None,
            "gap_dir": gap_dir,
            "gap_status": gap_status,
        },
        "post": {
            "since_pct": _r(since),
            "since_excess_pp": _r(since - b_since) if b_since is not None else None,
            "from_post_high_pct": _r((_px(last) / post_high - 1.0) * 100.0),
            "trading_days": len(after) - 1,
        },
    }


def current_block(bars: List[Dict[str, Any]], bench: Optional[Benchmark]) -> Dict[str, Any]:
    end = len(bars) - 1
    closes = [_px(b) for b in bars]

    def ma(n: int, at: int) -> Optional[float]:
        return sum(closes[at - n + 1: at + 1]) / n if at - n + 1 >= 0 else None

    out: Dict[str, Any] = {
        "date": bars[end]["date"],
        "returns": {f"d{n}": window_return(bars, end, n, bench) for n in (20, 60, 120, 250)},
        "position": position_stats(bars, end),
        "vol_ratio_20_120": (lambda a, b: _r(a / b) if a and b else None)(
            _avg_vol(bars, end, 20), _avg_vol(bars, end, 120)),
    }
    for n in (20, 60, 120):
        m = ma(n, end)
        out[f"vs_ma{n}_pct"] = _r((closes[end] / m - 1.0) * 100.0) if m else None
    m60_now, m60_prev = ma(60, end), ma(60, end - 20)
    out["ma60_slope_20d_pct"] = _r((m60_now / m60_prev - 1.0) * 100.0) if m60_now and m60_prev else None
    return out


def _band(values: List[float], current: Optional[float]) -> Dict[str, Any]:
    if len(values) < 20:
        return {"samples": len(values)}
    s = sorted(values)

    def q(p: float) -> float:
        k = (len(s) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        return s[lo] + (s[hi] - s[lo]) * (k - lo)

    out = {"samples": len(s), "min": _r(s[0]), "p25": _r(q(0.25)), "median": _r(q(0.5)),
           "p75": _r(q(0.75)), "max": _r(s[-1])}
    if current is not None:
        out["current_pct"] = _r(bisect_right(s, current) / len(s) * 100.0, 1)
    return out


def valuation_block(rows: List[Dict[str, Any]], bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"status": "unavailable"}
    latest = rows[-1]
    last_date = dt.date.fromisoformat(latest["date"])
    out: Dict[str, Any] = {
        "date": latest["date"], "board": latest.get("board"), "total_mv_yi": _r(latest.get("total_mv_yi")),
        "current": {k: _r(latest.get(k)) for k in ("pe_ttm", "pb", "ps_ttm", "pcf_ocf_ttm")},
        "bands": {},
    }
    for metric in ("pe_ttm", "pb", "ps_ttm"):
        cur = latest.get(metric)
        cur = cur if cur is not None and cur > 0 else None  # loss-making PE is not "cheap"
        per: Dict[str, Any] = {}
        for years in (1, 3, 5):
            since = (last_date - dt.timedelta(days=int(365.25 * years))).isoformat()
            vals = [r[metric] for r in rows if r["date"] >= since and r.get(metric) and r[metric] > 0]
            per[f"y{years}"] = _band(vals, cur)
        out["bands"][metric] = per
    out["history_years"] = _r((last_date - dt.date.fromisoformat(rows[0]["date"])).days / 365.25, 1)

    # Market-cap move = earnings move × multiple move: split the last N bars.
    if len(bars) > DECOMP_WINDOW:
        d0 = bars[-1 - DECOMP_WINDOW]["date"]
        i0 = bisect_right([r["date"] for r in rows], d0) - 1
        start = rows[i0] if i0 >= 0 else None
        if start and start.get("pe_ttm") and latest.get("pe_ttm") and start.get("total_mv_yi") \
                and latest.get("total_mv_yi") and start["pe_ttm"] > 0 and latest["pe_ttm"] > 0:
            mv_chg = latest["total_mv_yi"] / start["total_mv_yi"]
            pe_chg = latest["pe_ttm"] / start["pe_ttm"]
            eps_chg = mv_chg / pe_chg
            l_mv, l_pe, l_eps = math.log(mv_chg), math.log(pe_chg), math.log(eps_chg)
            # A share is only a share when both parts push the same way as the move,
            # and the move is big enough that the ratio is not noise.
            if abs(l_mv) < math.log(1.10):
                share, share_note = None, "市值变化不足 10%，份额无意义"
            elif l_pe * l_mv < 0 or l_eps * l_mv < 0:
                share, share_note = None, "估值与盈利反向变化，份额无意义，直接读两项变化"
            else:
                share, share_note = l_pe / l_mv, None
            out["decomposition"] = {
                "window_bars": DECOMP_WINDOW, "from": start["date"], "to": latest["date"],
                "mv_change_pct": _r((mv_chg - 1) * 100), "pe_ttm_change_pct": _r((pe_chg - 1) * 100),
                "ttm_earnings_change_pct": _r((eps_chg - 1) * 100),
                "rerating_share": _r(share, 2),
                **({"rerating_note": share_note} if share_note else {}),
            }
        else:
            out["decomposition"] = {"window_bars": DECOMP_WINDOW,
                                    "note": "起点或当前 PE_TTM 非正（亏损），无法拆分"}
    return out


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def load_anchors(period: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """{ts_code: {"name", "anchors": {"forecast": iso, "formal": iso}}} from season_<period>.json."""
    if not period:
        return {}
    path = os.path.join("reports", f"season_{period}.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        season = json.load(fh)
    out = {}
    for s in season.get("stocks", []):
        anchors = {k: _iso(v) for k, v in (s.get("anchors") or {}).items() if v}
        out[s["ts_code"]] = {"name": s.get("name"), "anchors": anchors}
    return out


def build_one(code: str, anchors: Dict[str, str], bench: Optional[Benchmark], source: str,
              gap_min: float, name: Optional[str]) -> Dict[str, Any]:
    rec: Dict[str, Any] = {"ts_code": code, "name": name, "errors": []}
    try:
        got = market_data.stock_bars(code, BAR_COUNT, source)
        bars = got["bars"]
        rec["price_source"] = got["source"]
        rec["adjusted"] = got["adjusted"]
        if not got["adjusted"]:
            rec["errors"].append("复权因子不可得：涨幅用不复权价，跨越除权/送转的窗口会失真")
        rec["events"] = {kind: event_block(bars, day, bench, gap_min) for kind, day in sorted(anchors.items())}
        rec["current"] = current_block(bars, bench)
    except market_data.DataUnavailable as exc:
        bars = []
        rec["errors"].append(f"K 线不可得: {str(exc)[:160]}")
    try:
        val = market_data.valuation_history(code, source)
        rec["valuation_source"] = val["source"]
        rec["valuation"] = valuation_block(val["rows"], bars)
        rec["name"] = rec["name"] or val["rows"][-1].get("name")
    except market_data.DataUnavailable as exc:
        rec["valuation"] = {"status": "unavailable"}
        rec["errors"].append(f"估值历史不可得: {str(exc)[:160]}")
    return rec


def parse_manual_anchors(items: List[str]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for item in items or []:
        code, kind, day = item.split(":")
        if kind not in ("forecast", "formal", "express", "other"):
            raise ValueError(f"anchor kind must be forecast/formal/express/other: {item}")
        out.setdefault(code.upper(), {})[kind] = _iso(day)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="业绩 × 资金面 × 定价：K 线与估值证据")
    ap.add_argument("--period", help="报告期 YYYYMMDD；从 reports/season_<period>.json 读取公告锚点")
    ap.add_argument("--codes", required=True, help="逗号分隔的 ts_code")
    ap.add_argument("--anchor", action="append", default=[],
                    help="手动锚点 CODE:KIND:YYYYMMDD（KIND=forecast/formal/express/other），可重复")
    ap.add_argument("--source", choices=["auto", "public", "tushare"], default="auto")
    ap.add_argument("--gap-min", type=float, default=2.0, help="跳空 >= N%% 记为断层（默认 2）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]
    season = load_anchors(args.period)
    manual = parse_manual_anchors(args.anchor)
    notes: List[str] = []
    try:
        idx = market_data.index_bars(BAR_COUNT, args.source)
        bench: Optional[Benchmark] = Benchmark(idx["bars"])
        bench_meta = {"code": idx["code"], "name": idx["name"], "source": idx["source"]}
    except market_data.DataUnavailable as exc:
        bench, bench_meta = None, None
        notes.append(f"基准指数不可得，超额收益为空: {str(exc)[:120]}")

    def job(code: str) -> Dict[str, Any]:
        anchors = {**(season.get(code) or {}).get("anchors", {}), **manual.get(code, {})}
        return build_one(code, anchors, bench, args.source, args.gap_min,
                         (season.get(code) or {}).get("name"))

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, len(codes)))) as ex:
        stocks = list(ex.map(job, codes))

    missing_anchor = [s["ts_code"] for s in stocks if not s.get("events")]
    if missing_anchor:
        notes.append(f"{len(missing_anchor)} 只没有公告锚点（未在 season 文件中、也未手动给出），"
                     "只有当前位置与估值：" + ", ".join(missing_anchor[:10]))
    payload = {
        "meta": {
            "period": args.period,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "benchmark": bench_meta,
            "pre_windows": list(PRE_WINDOWS),
            "gap_min_pct": args.gap_min,
            "conventions": {
                "reaction_day": "公告日当天或之后的第一个交易日",
                "run_up": "截至反应日前一交易日收盘的 N 日涨幅（后复权）与同期沪深300 的差（pp）",
                "gap_status": "向上跳空其后最低价跌破公告前收盘=filled；向下跳空其后最高价涨回=filled",
                "valuation_bands": "PE/PB/PS 只统计正值样本；current_pct=当前值在窗口内的分位",
                "decomposition": "近120交易日：市值变化=TTM盈利变化×PE_TTM变化；rerating_share=估值变化占市值变化的对数份额",
            },
            "notes": notes,
        },
        "stocks": stocks,
    }
    out = args.out or os.path.join("reports", f"price_context_{args.period or 'adhoc'}.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(json.dumps({
        "out": out, "stocks": len(stocks),
        "with_events": sum(1 for s in stocks if s.get("events")),
        "errors": {s["ts_code"]: s["errors"] for s in stocks if s["errors"]},
        "benchmark": bench_meta,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
