#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发行人利差曲线：分桶、固定期限插值、斜率与倒挂。

**这是整个 skill 里最容易被跳过、跳过就全错的一块。**

债券在曲线上往下滚（rolldown），利差会自然收窄。跟踪单只 ISIN 的时间序列必然把
rolldown 混进重定价，读出来是「利差在收窄」——一个系统性偏向乐观的错觉。
唯一正确的做法是**固定期限比较**：用发行人自己的曲线插值出恒定 5Y/10Y/30Y 点位
再比时间序列。

样本不足时不拟合。低于 min_bonds 的发行人标 thin_curve，只出分桶点位，
不出曲线、不出斜率——DLR 在样本里只有 3–5 只，它就是这个状态。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# 形状分析的两个下限：短端噪音门槛与相邻两点的最小期限间隔。
_SHAPE_MIN_TENOR = 2.0
_SHAPE_MIN_GAP = 0.25

BUCKETS: List[Dict[str, Any]] = [
    {"id": "2-5y", "lo": 2.0, "hi": 5.0, "label": "2–5Y"},
    {"id": "5-10y", "lo": 5.0, "hi": 10.0, "label": "5–10Y"},
    {"id": "10-20y", "lo": 10.0, "hi": 20.0, "label": "10–20Y"},
    {"id": "20y+", "lo": 20.0, "hi": 60.0, "label": "20Y+"},
]


def bucket_of(years: float) -> Optional[str]:
    for b in BUCKETS:
        if b["lo"] <= years < b["hi"]:
            return b["id"]
    return None


def bucket_spreads(points: Sequence[Dict[str, Any]], *,
                   exclude_option: bool = True) -> Dict[str, Dict[str, Any]]:
    """按期限桶取平均利差。

    points: [{"years": float, "gspread_bp": float, "has_embedded_option": bool}, ...]
    含权券默认剔除——它们的 G-spread 有偏，混进桶均值会污染整档的读数。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for b in BUCKETS:
        vals = [p["gspread_bp"] for p in points
                if b["lo"] <= p["years"] < b["hi"]
                and p.get("gspread_bp") is not None
                and not (exclude_option and p.get("has_embedded_option"))]
        out[b["id"]] = {
            "label": b["label"],
            "mean_bp": round(sum(vals) / len(vals), 1) if vals else None,
            "n": len(vals),
        }
    return out


def constant_maturity(points: Sequence[Dict[str, Any]], tenor: float, *,
                      exclude_option: bool = True,
                      max_extrapolate_years: float = 3.0) -> Optional[float]:
    """在发行人自己的曲线上插值出恒定期限的利差。

    两端只允许小幅外推（默认 3 年）。超出就返回 None——一个最长只到 7 年的
    发行人不该被推出一个 30 年点位，那是编的。
    """
    live = [(p["years"], p["gspread_bp"]) for p in points
            if p.get("gspread_bp") is not None
            and not (exclude_option and p.get("has_embedded_option"))]
    if len(live) < 2:
        return None
    live.sort()
    xs = [x for x, _ in live]
    if tenor < xs[0] - max_extrapolate_years or tenor > xs[-1] + max_extrapolate_years:
        return None
    if tenor <= xs[0]:
        return round(live[0][1], 1)
    if tenor >= xs[-1]:
        return round(live[-1][1], 1)
    for (x0, y0), (x1, y1) in zip(live, live[1:]):
        if x0 <= tenor <= x1:
            span = x1 - x0
            w = 0.0 if span == 0 else (tenor - x0) / span
            return round(y0 + (y1 - y0) * w, 1)
    return None


def issuer_curve(points: Sequence[Dict[str, Any]], *, tenors: Sequence[int],
                 min_bonds: int, inversion_threshold_bp: float) -> Dict[str, Any]:
    """一个发行人的曲线视图：分桶、固定期限点、斜率、是否倒挂。

    倒挂判定用**实际观测点的首尾**而不是插值点：插值本身会把形状抹平，
    一个只在中段倒挂的曲线（CRWV 现在就是）会被插值掩盖掉。
    """
    usable = [p for p in points if p.get("gspread_bp") is not None
              and not p.get("has_embedded_option")]
    n = len(usable)
    span = (max(p["years"] for p in usable) - min(p["years"] for p in usable)) \
        if len(usable) >= 2 else 0.0

    # thin_curve 只管一件事：点太少，连形状都读不出来。
    # **期限覆盖不够不该用只数来拦**——CRWV 只有 4 只债，但它们落在 3.8–5.9Y，
    # 5Y 那个固定期限点是真插值不是外推，而 10Y/30Y 由 constant_maturity 自己的
    # 外推守卫拒掉。用「只数」一刀切会把判据 3 最关键的对象整个屏蔽掉。
    thin = n < min_bonds

    cm = {f"{t}y": (None if thin else constant_maturity(points, float(t)))
          for t in tenors}

    # 形状分析只用 ≥2Y 的点。2 年以内的券定价被货币市场因素和个券流动性主导，
    # 放进来会让「负斜率段」在几乎每个发行人身上都触发——实测 AMZN 的
    # 1.71Y→2.24Y、AEP 的 1.22Y→1.72Y 都是这种噪音，不是曲线形状。
    ordered = sorted([p for p in usable if p["years"] >= _SHAPE_MIN_TENOR],
                     key=lambda p: p["years"])
    slope_bp = None
    inverted = False
    inversion_detail = None
    if not thin and len(ordered) >= 2:
        slope_bp = round(ordered[-1]["gspread_bp"] - ordered[0]["gspread_bp"], 1)
        # 逐段找负斜率：整体向上但中段掉头，也是形状信息。
        # 两点必须真的隔开一段期限——同一年份上的两只债价差是横截面离散，
        # 不是曲线斜率（实测 META 有两只都在 6.72Y，差 13bp）。
        worst = None
        for a, b in zip(ordered, ordered[1:]):
            if b["years"] - a["years"] < _SHAPE_MIN_GAP:
                continue
            drop = a["gspread_bp"] - b["gspread_bp"]
            if drop > inversion_threshold_bp and (worst is None or drop > worst[0]):
                worst = (drop, a, b)
        if worst:
            drop, a, b = worst
            inversion_detail = (f"{a['years']:.2f}Y {a['gspread_bp']:.0f}bp → "
                                f"{b['years']:.2f}Y {b['gspread_bp']:.0f}bp"
                                f"（−{drop:.0f}bp）")
            # 真倒挂看的是首尾：短端反超长端才是市场在定价近期事件。
            inverted = ordered[0]["gspread_bp"] - ordered[-1]["gspread_bp"] > inversion_threshold_bp
    # 分桶单调性才是关于**整条曲线**的形状陈述。相邻两只债的价差是横截面离散
    # （票息、流动性、144A 与注册券的差别），在几十只债的曲线上必然存在——
    # 实测按相邻点报事件会让 11 个发行人里 8 个触发，那种告警等于没有。
    # 所以：相邻负斜率段只作描述留在这里，**事件用分桶单调性**。
    bucket_means = [(b["id"], out_bucket["mean_bp"])
                    for b in BUCKETS
                    for out_bucket in [bucket_spreads(points).get(b["id"], {})]
                    if out_bucket.get("mean_bp") is not None]
    breaks = [f"{a[0]} {a[1]:.0f}bp → {b[0]} {b[1]:.0f}bp"
              for a, b in zip(bucket_means, bucket_means[1:]) if b[1] < a[1]]

    return {
        "n_bonds": n,
        "bucket_monotonic": not breaks,
        "bucket_breaks": breaks,
        "n_excluded_option": len(points) - n,
        "thin_curve": thin,
        "tenor_span_years": round(span, 2),
        "tenor_min_years": round(min(p["years"] for p in usable), 2) if usable else None,
        "tenor_max_years": round(max(p["years"] for p in usable), 2) if usable else None,
        "buckets": bucket_spreads(points),
        "constant_maturity_bp": cm,
        "slope_bp": slope_bp,
        "curve_inverted": inverted,
        "negative_segment": inversion_detail,
        "quality": "thin_curve" if thin else "ok",
        # 输出**全部**可用点，不套用形状分析那个 ≥2Y 的过滤——
        # 那个过滤只为了让斜率检测别被短端噪音带偏，图上该画的是完整曲线。
        "points": [{"years": round(p["years"], 2),
                    "gspread_bp": round(p["gspread_bp"], 1),
                    "name": p.get("display_name"),
                    "isin": p.get("isin")}
                   for p in sorted(usable, key=lambda x: x["years"])],
        "shape_points_used": len(ordered),
    }
