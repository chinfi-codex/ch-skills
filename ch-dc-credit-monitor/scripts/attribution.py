#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判据 1 · alpha 分解：先剥两层 beta，剩下的才是这家公司的信息。

利差走宽 30bp，可能三件事都不是这家公司的：市场整体在走宽、这一档整体在走宽、
或者才是它自己。所以每个发行人的变动强制分解成

    Δ总 = Δ市场beta + Δ档位beta + Δalpha

Δ市场beta 是对应指数 OAS 的变动；Δ档位beta 是**同档其他主体**的中位数变动
（剔除自己，否则单主体档会自己解释自己）；剩下的是 alpha。

两条纪律写在代码里，不靠注释约束：

* **单日 alpha 一律不定性。** ETF 估值有粘滞，单日残差里混着估值噪音。
  所以只有累积 alpha 才带 notable 标记。
* **带 stale 的点不进分解。** 价格连续多日不动往往是管理人没重估，不是市场
  没变；把它当成「利差没变」会让残差系统性偏小。
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any, Dict, List, Optional, Sequence


def _series_lookup(series: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    return {str(p["date"]): float(p["value"]) for p in series
            if p.get("value") is not None}


def _delta(lookup: Dict[str, float], start: str, end: str) -> Optional[float]:
    if start in lookup and end in lookup:
        return lookup[end] - lookup[start]
    return None


def decompose(issuer: str, *,
              issuer_series: Sequence[Dict[str, Any]],
              peer_series: Dict[str, Sequence[Dict[str, Any]]],
              index_series: Sequence[Dict[str, Any]],
              start: str, end: str) -> Optional[Dict[str, Any]]:
    """把 [start, end] 区间的利差变动拆成三段。

    peer_series 是**同档其他主体**的固定期限序列（调用方负责剔除 issuer 自己）。
    """
    issuer_lookup = _series_lookup(issuer_series)
    total = _delta(issuer_lookup, start, end)
    if total is None:
        return None

    beta_market = _delta(_series_lookup(index_series), start, end)
    peer_deltas = []
    for key, series in peer_series.items():
        if key == issuer:
            continue
        d = _delta(_series_lookup(series), start, end)
        if d is not None:
            peer_deltas.append(d)
    peer_move = statistics.median(peer_deltas) if peer_deltas else None

    if beta_market is None:
        beta_market = 0.0
        market_quality = "no_index_point"
    else:
        market_quality = "ok"

    if peer_move is None:
        beta_tier = 0.0
        tier_quality = "no_peer"          # 单主体档：无法剥离档位 beta
    else:
        # 档位 beta 是同档移动里超出市场的那部分，避免与市场 beta 重复计数。
        beta_tier = peer_move - beta_market
        tier_quality = "ok"

    alpha = total - beta_market - beta_tier
    return {
        "issuer": issuer,
        "start": start, "end": end,
        "total_bp": round(total, 1),
        "beta_market_bp": round(beta_market, 1),
        "beta_tier_bp": round(beta_tier, 1),
        "alpha_bp": round(alpha, 1),
        "n_peers": len(peer_deltas),
        "closure_residual_bp": round(
            total - (beta_market + beta_tier + alpha), 6),
        "quality": ("ok" if market_quality == "ok" and tier_quality == "ok"
                    else f"{market_quality}|{tier_quality}"),
    }


def cumulative(issuer: str, *,
               issuer_series: Sequence[Dict[str, Any]],
               peer_series: Dict[str, Sequence[Dict[str, Any]]],
               index_series: Sequence[Dict[str, Any]],
               windows: Sequence[int],
               min_days: int) -> Dict[str, Any]:
    """按窗口算累积 alpha。窗口内点数不足就不出数，标 regime_na，不填 0。"""
    dates = sorted(_series_lookup(issuer_series))
    out: Dict[str, Any] = {"issuer": issuer, "n_points": len(dates), "windows": {}}
    if len(dates) < min_days:
        out["quality"] = "insufficient_history"
        for w in windows:
            out["windows"][f"{w}d"] = {"value_bp": None, "quality": "insufficient_history"}
        return out

    out["quality"] = "ok"
    end = dates[-1]
    for w in windows:
        target = (dt.date.fromisoformat(end) - dt.timedelta(days=w)).isoformat()
        earlier = [d for d in dates if d <= target]
        if not earlier:
            out["windows"][f"{w}d"] = {"value_bp": None, "quality": "insufficient_history"}
            continue
        start = earlier[-1]
        block = decompose(issuer, issuer_series=issuer_series,
                          peer_series=peer_series, index_series=index_series,
                          start=start, end=end)
        out["windows"][f"{w}d"] = (
            {"value_bp": None, "quality": "no_pair"} if block is None else
            {"value_bp": block["alpha_bp"], "total_bp": block["total_bp"],
             "beta_market_bp": block["beta_market_bp"],
             "beta_tier_bp": block["beta_tier_bp"],
             "start": start, "end": end, "quality": block["quality"]})
    return out


def notable_events(cum: Dict[str, Any], *, segment: str,
                   thresholds: Dict[str, float]) -> List[Dict[str, Any]]:
    """把超过量级的累积 alpha 列成事件。

    **这是事件不是结论。** 脚本报「20 日累积 alpha +38bp，超过 IG 的 15bp 量级」，
    不报「该主体信用恶化」。
    """
    limit = thresholds.get(segment)
    if limit is None:
        return []
    out = []
    for window, block in (cum.get("windows") or {}).items():
        value = block.get("value_bp")
        if value is None or abs(value) < limit:
            continue
        out.append({
            "subject": cum["issuer"],
            "rule_id": f"alpha_cum_{window}",
            "criterion": "判据1·alpha分解",
            "observed": round(value, 1),
            "threshold": limit,
            "unit": "bp",
            "detail": (f"{window} 累积 alpha {value:+.1f}bp"
                       f"（同期总变动 {block.get('total_bp', 0):+.1f}bp，"
                       f"其中市场 {block.get('beta_market_bp', 0):+.1f}、"
                       f"同档 {block.get('beta_tier_bp', 0):+.1f}）"),
        })
    return out
