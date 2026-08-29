#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FRED —— 国债曲线与 ICE BofA 指数 OAS。keyless CSV，不需要 API key。

这一路是整个监控的基准层。没有它，利差是绝对数，读不出「走宽是 AI 的事
还是整个市场的事」——判据 1 的市场 beta 就是从这里来的。

指数 OAS 的单位是**百分数**（0.81 表示 81bp），必须 ×100。这是抄错概率最高的地方。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from .base import http_get, load_config


def _fetch_series(base_url: str, series_id: str) -> List[Tuple[str, float]]:
    text = http_get(f"{base_url}?id={series_id}", timeout=30)
    out: List[Tuple[str, float]] = []
    for line in text.strip().split("\n")[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        day, raw = parts[0], parts[1]
        if raw in (".", ""):
            continue
        try:
            out.append((day, float(raw)))
        except ValueError:
            continue
    return out


def fetch_benchmarks(cfg: Optional[Dict[str, Any]] = None,
                     history_days: int = 400) -> Dict[str, Any]:
    """拉国债曲线与两条指数 OAS，返回按日期索引的结构。

    返回:
        {
          "curve": {"2026-08-25": {2: 4.17, 5: 4.35, ...}, ...},
          "index_oas_bp": {"2026-08-25": {"ig": 81.0, "hy": 270.0}, ...},
          "latest": "2026-08-25",
        }
    """
    cfg = cfg or load_config("sources.yaml")
    src = cfg["sources"]["fred"]
    base = src["base_url"]
    cutoff = (dt.date.today() - dt.timedelta(days=history_days)).isoformat()

    curve: Dict[str, Dict[int, float]] = {}
    for tenor, sid in src["treasury_series"].items():
        for day, value in _fetch_series(base, sid):
            if day >= cutoff:
                curve.setdefault(day, {})[int(tenor)] = value

    index: Dict[str, Dict[str, float]] = {}
    for segment, sid in src["index_series"].items():
        for day, value in _fetch_series(base, sid):
            if day >= cutoff:
                # FRED 给的是百分数，指标层一律用 bp。
                index.setdefault(day, {})[segment] = value * 100.0

    latest = max(curve) if curve else None
    return {"curve": curve, "index_oas_bp": index, "latest": latest}


def interpolate(curve_day: Dict[int, float], years: float) -> Optional[float]:
    """线性插值出任意期限的国债收益率。曲线两端不外推，直接钳住。"""
    if not curve_day:
        return None
    tenors = sorted(curve_day)
    if years <= tenors[0]:
        return curve_day[tenors[0]]
    if years >= tenors[-1]:
        return curve_day[tenors[-1]]
    for lo, hi in zip(tenors, tenors[1:]):
        if lo <= years <= hi:
            span = hi - lo
            w = 0.0 if span == 0 else (years - lo) / span
            return curve_day[lo] + (curve_day[hi] - curve_day[lo]) * w
    return None
