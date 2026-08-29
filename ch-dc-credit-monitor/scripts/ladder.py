#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信用梯级：档位聚合、跨档距离、离散度、长短端分化。

单看「ORCL 205bp」读不出任何东西。有意义的是它在梯级里的位置，以及这个位置
在移动。这个模块把发行人层的曲线摊成一把标尺，并算出方法论判据 2 和判据 4
要的那几个数。

**档位归属由配置维护，脚本不自动调整。** 标尺本身会漂，重新分档是判断不是计算。
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Sequence


def _cm(issuer_block: Dict[str, Any], tenor: str) -> Optional[float]:
    return ((issuer_block or {}).get("constant_maturity_bp") or {}).get(tenor)


def _bucket(issuer_block: Dict[str, Any], bucket: str) -> Optional[float]:
    return ((issuer_block or {}).get("buckets") or {}).get(bucket, {}).get("mean_bp")


def build_rungs(curves: Dict[str, Dict[str, Any]],
                issuers_cfg: Dict[str, Any],
                rungs_cfg: Dict[Any, Any]) -> List[Dict[str, Any]]:
    """把发行人按档位摊开，每档给一个代表读数（5–10Y 桶均值的中位数）。"""
    grouped: Dict[int, List[str]] = {}
    for key, meta in issuers_cfg.items():
        rung = meta.get("rung")
        if rung is None or key not in curves:
            continue
        grouped.setdefault(int(rung), []).append(key)

    out: List[Dict[str, Any]] = []
    for rung in sorted(grouped):
        members = sorted(grouped[rung])
        readings = {m: _bucket(curves.get(m, {}), "5-10y") for m in members}
        live = [v for v in readings.values() if v is not None]
        meta = rungs_cfg.get(rung) or rungs_cfg.get(str(rung)) or {}
        out.append({
            "rung": rung,
            "name": meta.get("name", f"档 {rung}"),
            "role": meta.get("role"),
            "anchor_5_10y": meta.get("anchor_5_10y"),
            "members": members,
            "readings_5_10y": readings,
            "median_5_10y": round(statistics.median(live), 1) if live else None,
            "drift_vs_anchor_bp": (
                round(statistics.median(live) - meta["anchor_5_10y"], 1)
                if live and meta.get("anchor_5_10y") is not None else None),
        })
    return out


def rung_gaps(curves: Dict[str, Dict[str, Any]],
              gaps_cfg: Sequence[Dict[str, Any]],
              *, utility_members: Sequence[str],
              index_oas_bp: Dict[str, float],
              tenor_bucket: str = "5-10y") -> List[Dict[str, Any]]:
    """跨档距离 —— 判据 2 盯的那几个数。

    两个特殊主体：UTIL_MEDIAN 是受监管公用事业的中位数，IG_INDEX 是指数 OAS。
    它们俩的差是**反向证伪器**：显著转正说明 AI 故事进了受监管电力定价，
    框架要重写。
    """
    def value_of(token: str) -> Optional[float]:
        if token == "UTIL_MEDIAN":
            vals = [_bucket(curves.get(m, {}), tenor_bucket) for m in utility_members]
            vals = [v for v in vals if v is not None]
            return round(statistics.median(vals), 1) if vals else None
        if token == "IG_INDEX":
            return index_oas_bp.get("ig")
        if token == "HY_INDEX":
            return index_oas_bp.get("hy")
        return _bucket(curves.get(token, {}), tenor_bucket)

    out: List[Dict[str, Any]] = []
    for gap in gaps_cfg:
        a, b = value_of(gap["a"]), value_of(gap["b"])
        observed = None if (a is None or b is None) else round(a - b, 1)
        anchor = gap.get("anchor_bp")
        out.append({
            "id": gap["id"],
            "a": gap["a"], "b": gap["b"],
            "a_bp": a, "b_bp": b,
            "observed_bp": observed,
            "anchor_bp": anchor,
            "drift_bp": (None if observed is None or anchor is None
                         else round(observed - anchor, 1)),
            "means": gap.get("means"),
            "quality": "ok" if observed is not None else "regime_na",
        })
    return out


def dispersion(rungs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """梯级离散度 —— 各档代表读数的标准差。

    扩张 + 弱档走宽 = 质量分层；压缩 + 全档同向走宽 = 体系性；
    压缩 + 全档同向收窄 = 追逐收益。**三种读法完全不同，判别留给模型。**
    """
    vals = [r["median_5_10y"] for r in rungs if r.get("median_5_10y") is not None]
    if len(vals) < 3:
        return {"value": None, "n_rungs": len(vals), "quality": "insufficient_rungs"}
    return {
        "value": round(statistics.pstdev(vals), 1),
        "n_rungs": len(vals),
        "min_bp": min(vals),
        "max_bp": max(vals),
        "range_bp": round(max(vals) - min(vals), 1),
        "quality": "ok",
    }


def term_structure_split(curves: Dict[str, Dict[str, Any]],
                         a: str, b: str,
                         short_tenor: int, long_tenor: int) -> Dict[str, Any]:
    """判据 4 · 长短端分化：长端跨档差 − 短端跨档差。

    实测 META 对 GOOGL 在 5–10Y 差 28bp、在 20Y+ 差 72bp——同一个故事在长端
    定价了两倍多。这个差值收窄（短端追上来）= 担忧前移，是升级信号。
    """
    short_a, short_b = _cm(curves.get(a, {}), f"{short_tenor}y"), _cm(curves.get(b, {}), f"{short_tenor}y")
    long_a, long_b = _cm(curves.get(a, {}), f"{long_tenor}y"), _cm(curves.get(b, {}), f"{long_tenor}y")
    short_gap = None if short_a is None or short_b is None else round(short_a - short_b, 1)
    long_gap = None if long_a is None or long_b is None else round(long_a - long_b, 1)
    return {
        "pair": f"{a}-{b}",
        "short_tenor": short_tenor, "long_tenor": long_tenor,
        "short_gap_bp": short_gap, "long_gap_bp": long_gap,
        "split_bp": (None if short_gap is None or long_gap is None
                     else round(long_gap - short_gap, 1)),
        "quality": "ok" if short_gap is not None and long_gap is not None else "regime_na",
    }
