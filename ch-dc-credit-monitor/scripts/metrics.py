#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证据包 —— 把库里的观测算成方法论五个判据要的那些确定性量。

**脚本产出证据与阈值穿越事件，不产出结论。** 它可以报「CRWV 曲线在 5.10Y 与
5.89Y 之间出现负斜率」，不可以报「CRWV 出现违约预警」。定档、归因、措辞
全部在 SKILL.md 由模型做。

产出结构（渲染层与模型都读这一份）：

    asof / anchors        锚定日与它比日历日晚几天
    ladder                梯级：各档代表读数、成员、相对锚点的漂移
    gaps                  跨档距离（判据 2）+ 反向证伪器
    dispersion            梯级离散度
    issuers               每个发行人的曲线：分桶、固定期限点、斜率、倒挂
    term_split            长短端分化（判据 4）
    attribution           alpha 分解（判据 1），历史不足时明说
    convertibles          转债层：深度价内则信用不可提取
    gpu_secured           GPU 抵押载体：工具级债务与 VIE 抵押品
    spv                   SPV 台账 + 租户锚（Beignet 的核心读法）
    events                阈值穿越事件清单
    source_health         今天的数据完不完整

用法：
    python scripts/metrics.py --output evidence/dc-2026-08-26.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import attribution                                        # noqa: E402
import curve as curve_mod                                 # noqa: E402
import db_adapter as db                                   # noqa: E402
import ladder as ladder_mod                               # noqa: E402
import pricing                                            # noqa: E402
from collectors.base import load_config                   # noqa: E402


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _issuer_points(instruments: List[Dict[str, Any]],
                   spreads: Dict[str, float],
                   years: Dict[str, float]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for inst in instruments:
        key = inst["instrument_key"]
        if key not in spreads or key not in years:
            continue
        if inst.get("regime") == "convertible":
            continue                      # 转债不进公开债曲线，见 convertibles 段
        grouped[inst["issuer_parent_key"]].append({
            "years": years[key],
            "gspread_bp": spreads[key],
            "has_embedded_option": bool(inst.get("has_embedded_option")),
            "display_name": inst.get("display_name"),
            "isin": inst.get("isin"),
            "instrument_type": inst.get("instrument_type"),
        })
    return grouped


def _series_by_issuer(metric_prefix: str, since: str) -> Dict[str, List[Dict[str, Any]]]:
    """从库里读发行人级固定期限序列（供 alpha 分解用）。"""
    rows = db.load_metric_prefix(metric_prefix, since)
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = str(row["instrument_key"])
        if not key.startswith("ISSUER:"):
            continue
        out[key.split(":", 1)[1]].append(
            {"date": str(row["asof_date"]), "value": row["value"]})
    for series in out.values():
        series.sort(key=lambda p: p["date"])
    return out


def _history(instrument_key: str, metric: str, asof: str,
             lookback_days: int = 400) -> Dict[str, float]:
    since = (dt.date.fromisoformat(asof) - dt.timedelta(days=lookback_days)).isoformat()
    rows = db.load_observations(metric, since, [instrument_key])
    return {str(r["asof_date"]): float(r["value"])
            for r in rows if r.get("value") is not None}


def _deltas(series: Dict[str, float], asof: str) -> Dict[str, Optional[float]]:
    """1D / 1W / 1M 变化。**取不到就是 None，绝不填 0**——0 会被读成「没变化」。

    「1D」取的是序列里上一个真实观测日，不是日历前一天：持仓文件周末不更新，
    硬按日历找会在周一永远落空。
    """
    out: Dict[str, Optional[float]] = {"1d": None, "1w": None, "1m": None}
    if asof not in series:
        return out
    today = series[asof]
    days = sorted(d for d in series if d < asof)
    if not days:
        return out
    out["1d"] = round(today - series[days[-1]], 1)
    for label, back in (("1w", 7), ("1m", 30)):
        target = (dt.date.fromisoformat(asof) - dt.timedelta(days=back)).isoformat()
        earlier = [d for d in days if d <= target]
        if earlier:
            out[label] = round(today - series[earlier[-1]], 1)
    return out


def _member_charts(curves: Dict[str, Any], issuers_cfg: Dict[str, Any], asof: str,
                   *, window_days: int, min_points: int) -> List[Dict[str, Any]]:
    """子项（发行人）5–10Y 的曲线数据。**渲染用，不参与任何判据。**

    序列取的是 `drv.bucket_5-10y`——发行人自己 5–10Y 桶的均值。这里刻意不用单只
    ISIN 的历史：债券在曲线上往下滚，利差会自然收窄，跟踪单只债必然把 rolldown
    混进重定价，读出来是一个系统性偏乐观的「利差在收窄」。

    **点数不够就不画线。** 两个点连起来是一条直线，看上去像趋势，其实什么都不是；
    这跟 `disclosure_once` 拒绝把一次性披露画成时间序列是同一条纪律。所以这里只
    如实报 `series` 与 `series_days`，够不够画由 `quality` 说了算，渲染层照办。

    价格层没有历史也补不回来（SPDR 只给当日快照，见 sources.yaml 的
    not_available），所以冷启动阶段这里必然是 `insufficient_series`——
    那是正确状态，不是故障。
    """
    out: List[Dict[str, Any]] = []
    for issuer, block in curves.items():
        meta = issuers_cfg.get(issuer) or {}
        bucket = ((block.get("buckets") or {}).get("5-10y") or {})
        value = bucket.get("mean_bp")
        anchor = meta.get("anchor_5_10y")
        series = sorted(_history(f"ISSUER:{issuer}", "drv.bucket_5-10y", asof,
                                 lookback_days=window_days).items())
        if value is None:
            quality = "no_reading"
        elif len(series) < min_points:
            quality = "insufficient_series"
        else:
            quality = "ok"
        out.append({
            "issuer": issuer,
            "name": meta.get("name", issuer),
            "rung": meta.get("rung"),
            "value": _round(value),
            "anchor": anchor,
            "drift_vs_anchor_bp": (None if value is None or anchor is None
                                   else round(float(value) - float(anchor), 1)),
            "sample_n": bucket.get("n"),
            "window_days": window_days,
            "series_days": len(series),
            "min_points_for_line": min_points,
            "series": [[d, round(float(v), 1)] for d, v in series],
            "quality": quality,
        })
    out.sort(key=lambda m: (m["rung"] if m["rung"] is not None else 99, m["issuer"]))
    return out


def _build_dials(asof: str, rungs: List[Dict[str, Any]], gaps: List[Dict[str, Any]],
                 curves: Dict[str, Any], spv_blocks: List[Dict[str, Any]],
                 disp: Dict[str, Any], universe: Dict[str, Any]) -> List[Dict[str, Any]]:
    """核心刻度 —— 日频追踪页上唯一该有的那张表。

    选择标准只有一条：**这个数每天看一眼，值不值得。** 水平值本身信息量很低，
    真正驱动判断的是它相对昨天、上周、以及相对锚点的位移，所以每一行都带三档变化。
    发行人层的 11×12 明细表、转债逐只、GPU 工具级债务这些属于查证深度，
    留在证据包与报告正文里，不上日频页。
    """
    dials: List[Dict[str, Any]] = []

    def add(*, group, name, key, metric, value, anchor=None, note=None,
            unit="bp", rung=None, excess=None, anchor_excess=None,
            excess_quality=None, bench=None):
        d = _deltas(_history(key, metric, asof), asof)
        dials.append({
            "group": group, "name": name, "rung": rung, "value": _round(value),
            "unit": unit, "anchor": anchor,
            "vs_anchor": (None if value is None or anchor is None
                          else round(float(value) - float(anchor), 1)),
            # 超额口径：同一个锚点漂移，但先减掉对应指数 OAS。两列并排看才读得出
            # 「整个信用市场在走宽」和「这一档自己在走宽」的区别。
            "bench": bench,
            "excess": _round(excess),
            "anchor_excess": anchor_excess,
            "vs_anchor_excess": (None if excess is None or anchor_excess is None
                                 else round(float(excess) - float(anchor_excess), 1)),
            "excess_quality": excess_quality,
            "d1": d["1d"], "d7": d["1w"], "d30": d["1m"],
            "note": note,
            "quality": "ok" if value is not None else "regime_na",
        })

    for r in rungs:
        add(group="梯级", name=f"档{r['rung']} {r['name']}",
            key=f"RUNG:{r['rung']}", metric="drv.rung_median_5_10y",
            value=r.get("median_5_10y"), anchor=r.get("anchor_5_10y"),
            note="、".join(r["members"]), rung=r["rung"],
            excess=r.get("excess_5_10y"), anchor_excess=r.get("anchor_excess_5_10y"),
            excess_quality=r.get("excess_quality"), bench=r.get("bench"))
    gap_meta = {g["id"]: g for g in universe.get("rung_gaps", [])}
    for g in gaps:
        cfg = gap_meta.get(g["id"], {})
        add(group="跨档距离", name=cfg.get("label") or f"{g['a']} − {g['b']}",
            key=f"GAP:{g['id']}", metric="drv.rung_gap_bp",
            value=g.get("observed_bp"), anchor=g.get("anchor_bp"),
            note=" · ".join(x for x in (cfg.get("detail"), g.get("means")) if x))
    add(group="结构", name="梯级离散度", key="SYSTEM", metric="drv.dispersion_bp",
        value=disp.get("value"),
        note="扩张=质量分层；压缩+同向走宽=体系性；压缩+同向收窄=追逐收益")
    spv_meta = universe.get("spv") or {}
    for s_block in spv_blocks:
        gap = s_block.get("coupon_vs_tenant_bp")
        shown = (spv_meta.get(s_block["id"], {}).get("display_name")
                 or s_block["id"].title())
        add(group="结构", name=f"{shown} 票息 vs 租户 {s_block.get('tenant')}",
            key=f"SPV:{s_block['id']}", metric="drv.coupon_vs_tenant_bp",
            value=gap,
            note=(f"票息 {s_block.get('coupon_pct')}% vs 租户同期限 "
                  f"{s_block.get('tenant_matched_yield_pct')}%；正数=SPV 更便宜，补偿偏薄"))
    return dials


def build(asof: Optional[str] = None, window_days: int = 90,
          persist: bool = True) -> Dict[str, Any]:
    universe = load_config("universe.yaml")
    thresholds = load_config("thresholds.yaml")
    sources_cfg = load_config("sources.yaml")

    asof = asof or db.latest_asof("yld.gspread_bp")
    if asof is None:
        raise SystemExit("error: 库里没有利差观测，先跑 scripts/collect.py")
    since = (dt.date.fromisoformat(asof) - dt.timedelta(days=window_days)).isoformat()

    instruments = db.load_instruments()
    spread_rows = db.load_observations("yld.gspread_bp", asof)
    year_rows = db.load_observations("ref.years_to_maturity", asof)
    price_rows = db.load_observations("px.clean", asof)
    spreads = {str(r["instrument_key"]): float(r["value"])
               for r in spread_rows if str(r["asof_date"]) == asof and r["value"] is not None}
    years = {str(r["instrument_key"]): float(r["value"])
             for r in year_rows if str(r["asof_date"]) == asof and r["value"] is not None}
    prices = {str(r["instrument_key"]): float(r["value"])
              for r in price_rows if str(r["asof_date"]) == asof and r["value"] is not None}
    quality_by_key = {str(r["instrument_key"]): str(r["quality"])
                      for r in spread_rows if str(r["asof_date"]) == asof}

    # 基准层：**先读库再用实时 FRED 覆盖**。库里的那份由 collect.py 每天写、
    # backfill.py 一次性补满；实时的那份负责当天与 FRED 的回溯修订，重叠日期以它
    # 为准。这个顺序让 FRED 挂掉的当天仍然有基准可用——判据 1 的市场 beta 不会
    # 因为一次网络失败整段消失。
    from collectors import fred as fred_mod
    anchor_asof = universe.get("anchor_asof")
    bench_since = min(d for d in (since, anchor_asof) if d)
    need_days = (dt.date.today() - dt.date.fromisoformat(bench_since)).days + 30
    bench = db.benchmark_history(bench_since)
    db_days = len(bench["curve"])
    try:
        live = fred_mod.fetch_benchmarks(sources_cfg, history_days=need_days)
        for day, tenors in live["curve"].items():
            bench["curve"].setdefault(day, {}).update(tenors)
        for day, segments in live["index_oas_bp"].items():
            bench["index_oas_bp"].setdefault(day, {}).update(segments)
        bench_note = f"库 {db_days} 天 + FRED 实时 {len(live['curve'])} 天"
    except Exception as exc:                              # noqa: BLE001
        bench_note = f"FRED 实时取不到（{exc}）；只用库里的 {db_days} 天"
    bench["latest"] = max(bench["curve"]) if bench["curve"] else None

    index_anchor = pricing.nearest_curve_day(bench["index_oas_bp"], asof)
    index_oas = bench["index_oas_bp"].get(index_anchor or "", {})
    curve_anchor = pricing.nearest_curve_day(bench["curve"], asof)

    # 锚点那一天的指数 OAS。超额口径的漂移是「今天的超额 − 锚点日的超额」，
    # 所以这里必须是锚点当天的指数值，不能拿今天的顶替——顶替之后算出来的
    # 就又是 G-spread 口径的漂移，白做一遍。锚点日碰上假期时向前找最近的有数日，
    # 找不到（超过 5 天）整个超额口径出 None 而不是猜一个。
    anchor_index_day = (pricing.nearest_curve_day(bench["index_oas_bp"], anchor_asof)
                        if anchor_asof else None)
    anchor_index_oas = bench["index_oas_bp"].get(anchor_index_day or "", {})

    # --- 发行人曲线 ---------------------------------------------------------
    cfg_curve = thresholds["curve"]
    points = _issuer_points(instruments, spreads, years)
    curves: Dict[str, Any] = {}
    for issuer, pts in points.items():
        block = curve_mod.issuer_curve(
            pts, tenors=cfg_curve["constant_maturities"],
            min_bonds=cfg_curve["min_bonds_for_curve"],
            inversion_threshold_bp=cfg_curve["inversion_threshold_bp"])
        block["issuer"] = issuer
        block["name"] = (universe["issuers"].get(issuer) or {}).get("name", issuer)
        block["rung"] = (universe["issuers"].get(issuer) or {}).get("rung")
        block["note"] = (universe["issuers"].get(issuer) or {}).get("note")
        curves[issuer] = block

    # --- 梯级、跨档距离、离散度、长短端分化 ---------------------------------
    rungs = ladder_mod.build_rungs(curves, universe["issuers"], universe["rungs"],
                                   index_oas_bp=index_oas,
                                   anchor_index_oas_bp=anchor_index_oas)
    gaps = ladder_mod.rung_gaps(curves, universe["rung_gaps"],
                                utility_members=universe["utility_median_members"],
                                index_oas_bp=index_oas)
    disp = ladder_mod.dispersion(rungs)
    ts_cfg = thresholds["term_structure"]
    term_split = [ladder_mod.term_structure_split(curves, g["a"], g["b"],
                                                  ts_cfg["short_tenor"], ts_cfg["long_tenor"])
                  for g in universe["rung_gaps"] if g["a"] in curves and g["b"] in curves]

    # --- alpha 分解（判据 1）------------------------------------------------
    attr_cfg = thresholds["attribution"]
    cm_series = _series_by_issuer("drv.cm_spread_", since)
    index_series = [{"date": d, "value": v.get("ig")}
                    for d, v in sorted(bench["index_oas_bp"].items())
                    if d >= since and v.get("ig") is not None]
    attributions: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for issuer, block in curves.items():
        rung = block.get("rung")
        peers = {k: cm_series.get(k, []) for k, v in curves.items()
                 if v.get("rung") == rung and k != issuer}
        cum = attribution.cumulative(
            issuer, issuer_series=cm_series.get(issuer, []), peer_series=peers,
            index_series=index_series, windows=attr_cfg["cum_windows"],
            min_days=attr_cfg["min_days_for_alpha"])
        segment = "hy" if rung == 7 else "ig"
        cum["segment"] = segment
        attributions.append(cum)
        events.extend(attribution.notable_events(
            cum, segment=segment, thresholds=attr_cfg["notable_cum_bp"]))

    # --- 曲线形状事件（判据 3）---------------------------------------------
    for issuer, block in curves.items():
        if block.get("curve_inverted"):
            events.append({
                "subject": issuer, "rule_id": "curve_inverted",
                "criterion": "判据3·曲线形状",
                "observed": block.get("slope_bp"), "threshold": -cfg_curve["inversion_threshold_bp"],
                "unit": "bp",
                "detail": f"短端利差反超长端；负斜率段 {block.get('negative_segment')}",
            })
        elif not block.get("bucket_monotonic") and block.get("bucket_breaks"):
            # 分桶层面的非单调 = 关于整条曲线的形状陈述，不是两只债的价差。
            events.append({
                "subject": issuer, "rule_id": "curve_non_monotonic",
                "criterion": "判据3·曲线形状",
                "observed": None, "threshold": None, "unit": "bp",
                "detail": ("曲线整体未倒挂，但分桶不单调："
                           + "；".join(block["bucket_breaks"])),
            })

    # --- 跨档距离漂移事件（判据 2）-----------------------------------------
    for gap in gaps:
        drift = gap.get("drift_bp")
        if drift is not None and abs(drift) >= thresholds["ladder"]["notable_gap_move_bp"]:
            events.append({
                "subject": gap["id"], "rule_id": "rung_gap_drift",
                "criterion": "判据2·梯级离散度",
                "observed": gap["observed_bp"], "threshold": gap["anchor_bp"],
                "unit": "bp",
                "detail": (f"{gap['a']}−{gap['b']} 现 {gap['observed_bp']}bp，"
                           f"较锚点 {gap['anchor_bp']}bp 漂移 {drift:+.1f}bp"),
            })

    # --- 转债层 -------------------------------------------------------------
    q_cfg = thresholds["quality"]
    convertibles: List[Dict[str, Any]] = []
    for inst in instruments:
        if inst.get("regime") != "convertible":
            continue
        key = inst["instrument_key"]
        price = prices.get(key)
        if price is None:
            continue
        issuer = inst["issuer_parent_key"]
        cb_spread = spreads.get(key)
        cb_years = years.get(key)

        # 把转债的 G-spread 直接当信用利差是错的：1.75% 票息的债贴着面值，
        # 按直债折现算出来的收益率远低于国债，G-spread 会是负几百 bp——
        # 那不是「信用极好」，那是**期权价值**被算进了折价里。
        #
        # 没有转股比例就无法正经剥离期权（SPDR 持仓不给），但可以用发行人
        # 自己的直债曲线做参照：同期限直债利差 − 转债 G-spread = 期权价值的下界。
        # 这个数越大，说明这只转债越是靠股票定价。
        straight_ref = None
        option_value_bp = None
        ref_quality = "no_straight_curve"
        if cb_years is not None and issuer in curves:
            straight_ref = curve_mod.constant_maturity(points.get(issuer, []), cb_years)
            if straight_ref is not None and cb_spread is not None:
                option_value_bp = round(straight_ref - cb_spread, 1)
                ref_quality = "ok"

        deep_itm = price >= q_cfg["convertible_deep_itm_price"]
        near_floor = price <= q_cfg["convertible_credit_extractable_max_price"]
        # 只有「贴近债底」**且**期权价值确实小时，信用信息才算可提取。
        # 光看价格不够——CRWV 1.75% 2031 报 104.6 看着贴债底，
        # 但它的期权价值仍有上千 bp。
        extractable = bool(near_floor and option_value_bp is not None
                           and option_value_bp <= q_cfg["convertible_max_option_value_bp"])
        if deep_itm:
            reading = "深度价内，delta 接近 1，价格变动是股票信息，信用不可提取"
        elif extractable:
            reading = "贴近债底且期权价值有限，信用信息仍在"
        elif near_floor:
            reading = "价格贴债底但期权价值仍占主导，信用信息被稀释"
        else:
            reading = "介于两者之间，信用信息被期权价值稀释"
        convertibles.append({
            "issuer": issuer,
            "isin": inst.get("isin"),
            "name": inst.get("display_name"),
            "coupon": _round(inst.get("coupon"), 3),
            "maturity": str(inst.get("maturity")),
            "years": _round(cb_years, 2),
            "price": _round(price, 2),
            "deep_itm": deep_itm,
            "near_floor": near_floor,
            "credit_extractable": extractable,
            "gspread_bp": _round(cb_spread) if extractable else None,
            # 直接把转债的 G-spread 当信用利差展示会误导，所以只在可提取时才给。
            "raw_gspread_bp": _round(cb_spread),
            "straight_curve_ref_bp": _round(straight_ref),
            "option_value_bp": option_value_bp,
            "option_value_quality": ref_quality,
            # 转股比例不在持仓文件里，parity 与转股溢价率只能标 paywalled。
            "parity": None,
            "parity_quality": "paywalled",
            "reading": reading,
        })
    convertibles.sort(key=lambda c: (c["issuer"], -(c["price"] or 0)))

    # --- GPU 抵押载体 -------------------------------------------------------
    gpu_rows = db.load_metric_prefix("col.", since)
    gpu_secured: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in gpu_rows:
        key = str(row["instrument_key"])
        issuer = key.split(":", 1)[0]
        gpu_secured[issuer].append({
            "facility": key.split(":", 1)[1] if ":" in key else key,
            "metric": str(row["metric"]).replace("col.", ""),
            "value": _round(row["value"], 2),
            "unit": str(row["unit"]),
            "asof": str(row["asof_date"]),
            "ref": row.get("raw_ref"),
        })

    # --- SPV 层 + 租户锚（Beignet 的核心读法）------------------------------
    spv_blocks = []
    for spv_id, meta in (universe.get("spv") or {}).items():
        tenant = meta.get("tenant")
        tenant_pts = points.get(tenant, [])
        maturity = meta.get("maturity")
        tenant_yield = None
        tenant_spread = None
        tenor_years = None
        if maturity and tenant_pts and curve_anchor:
            tenor_years = pricing.year_fraction(maturity, asof)
            window = [p for p in tenant_pts
                      if abs(p["years"] - tenor_years) <= 5 and not p["has_embedded_option"]]
            if window:
                tenant_spread = round(statistics.mean(p["gspread_bp"] for p in window), 1)
                ust = fred_mod.interpolate(bench["curve"].get(curve_anchor, {}), tenor_years)
                if ust is not None:
                    tenant_yield = round(ust + tenant_spread / 100, 3)
        coupon = meta.get("coupon_pct")
        gap_bp = (None if tenant_yield is None or coupon is None
                  else round((tenant_yield - coupon) * 100, 1))
        spv_blocks.append({
            "id": spv_id,
            "legal_name": meta.get("legal_name"),
            "tenant": tenant,
            "sponsor": meta.get("sponsor_ticker"),
            "notes_outstanding_usd_mn": meta.get("notes_outstanding_usd_mn"),
            "coupon_pct": coupon,
            "maturity": maturity,
            "tenor_years": _round(tenor_years, 1),
            "disclosure_date": meta.get("disclosure_date"),
            "disclosed_in": meta.get("disclosed_in"),
            "quality": meta.get("quality", "disclosure_once"),
            "tenant_matched_spread_bp": tenant_spread,
            "tenant_matched_yield_pct": tenant_yield,
            "coupon_vs_tenant_bp": gap_bp,
            "tenant_sample_n": len([p for p in tenant_pts
                                    if tenor_years is not None
                                    and abs(p["years"] - tenor_years) <= 5
                                    and not p["has_embedded_option"]]),
            "portfolio_proxy": {
                "line": meta.get("portfolio_line"),
                "investments": meta.get("portfolio_investments"),
                "properties": meta.get("portfolio_properties"),
                "fv_usd_mn": meta.get("portfolio_fv_usd_mn"),
                "asof": meta.get("portfolio_fv_asof"),
            },
        })
        if gap_bp is not None:
            events.append({
                "subject": f"SPV:{spv_id}", "rule_id": "spv_tenant_carry",
                "criterion": "SPV·租户锚",
                "observed": gap_bp, "threshold": 0, "unit": "bp",
                "detail": (f"票息 {coupon}% vs 租户 {tenant} 同期限 {tenant_yield}%，"
                           f"{'低' if gap_bp > 0 else '高'} {abs(gap_bp):.0f}bp"),
            })

    # --- 派生序列落库 -------------------------------------------------------
    # **这一步以前漏了，是个真 bug**：固定期限点算完就扔，attribution 每天都读不到
    # 历史，于是 alpha 分解永远返回 insufficient_history。日频追踪页要的是变化量，
    # 而变化量的前提是这些派生值本身有序列。
    derived_rows: List[Dict[str, Any]] = []

    def _emit(key: str, metric: str, value: Optional[float], unit: str = "bp") -> None:
        if value is None:
            return
        derived_rows.append({
            "asof_date": asof, "instrument_key": key, "metric": metric,
            "value": round(float(value), 4), "value_text": None, "unit": unit,
            "method": "derived", "source_id": "metrics", "obs_date": asof,
            "staleness_days": 0, "quality": "ok", "raw_ref": None,
        })

    for issuer, block in curves.items():
        for tenor, value in (block.get("constant_maturity_bp") or {}).items():
            _emit(f"ISSUER:{issuer}", f"drv.cm_spread_{tenor}", value)
        for bucket, cell in (block.get("buckets") or {}).items():
            _emit(f"ISSUER:{issuer}", f"drv.bucket_{bucket}", cell.get("mean_bp"))
    for r in rungs:
        _emit(f"RUNG:{r['rung']}", "drv.rung_median_5_10y", r.get("median_5_10y"))
        # 超额口径也要有自己的序列，否则「剔市场之后这一档的 1W 变化」永远算不出来。
        _emit(f"RUNG:{r['rung']}", "drv.rung_excess_5_10y", r.get("excess_5_10y"))
    for g in gaps:
        _emit(f"GAP:{g['id']}", "drv.rung_gap_bp", g.get("observed_bp"))
    _emit("SYSTEM", "drv.dispersion_bp", disp.get("value"))
    for s_block in spv_blocks:
        _emit(f"SPV:{s_block['id']}", "drv.coupon_vs_tenant_bp",
              s_block.get("coupon_vs_tenant_bp"))
    if persist:
        db.upsert_observations(derived_rows)

    # --- 核心刻度：这才是日频追踪要看的东西 ---------------------------------
    dials = _build_dials(asof, rungs, gaps, curves, spv_blocks, disp, universe)

    # 子项曲线。窗口与判据窗口分开：判据固定 90 天，曲线画 200 天只为看形状。
    chart_cfg = thresholds.get("chart") or {}
    member_charts = _member_charts(
        curves, universe["issuers"], asof,
        window_days=int(chart_cfg.get("member_series_days", 200)),
        min_points=int(chart_cfg.get("min_points_for_line", 10)))

    # --- 数据源健康度 -------------------------------------------------------
    asof_lag = (dt.date.fromisoformat(asof) - dt.date.fromisoformat(curve_anchor)).days \
        if curve_anchor else None
    thin = [k for k, v in curves.items() if v.get("thin_curve")]
    stale_n = sum(1 for q in quality_by_key.values() if q == "stale")
    option_n = sum(1 for q in quality_by_key.values() if q == "option_biased")
    source_health = [
        {"source": "spdr", "status": "ok" if spreads else "empty",
         "detail": f"{len(spreads)} 只债有利差，{len(instruments)} 只在库"},
        {"source": "fred", "status": "ok" if curve_anchor else "empty",
         "detail": (f"国债曲线锚 {curve_anchor}，指数 OAS 锚 {index_anchor}；"
                    f"{bench_note}；校准锚点 {anchor_asof} 的指数 OAS 取自 "
                    f"{anchor_index_day or '缺'}")},
        {"source": "sec", "status": "ok" if gpu_secured else "empty",
         "detail": f"{sum(len(v) for v in gpu_secured.values())} 条抵押品/债务事实"},
        {"source": "spv_ledger", "status": "ok" if spv_blocks else "empty",
         "detail": f"{len(spv_blocks)} 个 SPV（一次性条款，非序列）"},
    ]

    return {
        "asof": asof,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "window_days": window_days,
        "anchors": {
            "holdings_asof": asof,
            "treasury_curve": curve_anchor,
            "index_oas": index_anchor,
            "curve_lag_days": asof_lag,
            "index_oas_bp": {k: _round(v) for k, v in index_oas.items()},
            # 校准锚点：哪一天、那天的指数 OAS 是多少。超额口径的漂移全部相对它算，
            # 所以它必须在证据包里可查，而不是只活在 universe.yaml 的注释里。
            "anchor_asof": anchor_asof,
            "anchor_index_oas_day": anchor_index_day,
            "anchor_index_oas_bp": {k: _round(v) for k, v in anchor_index_oas.items()},
            "benchmark_source": bench_note,
        },
        "dials": dials,
        # 渲染用的曲线数据，**模型读证据时可以整段跳过**：里面全是点，没有判断。
        "member_charts": member_charts,
        "ladder": rungs,
        "gaps": gaps,
        "dispersion": disp,
        "issuers": curves,
        "term_split": term_split,
        "attribution": attributions,
        "convertibles": convertibles,
        "gpu_secured": {k: v for k, v in gpu_secured.items()},
        "spv": spv_blocks,
        "events": events,
        "events_mode": thresholds["events"]["mode"],
        "quality_summary": {
            "thin_curve_issuers": thin,
            "stale_prices": stale_n,
            "option_biased": option_n,
            "instruments_total": len(instruments),
            "instruments_priced": len(spreads),
        },
        "source_health": source_health,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="产出 DC 信用监控证据包。")
    parser.add_argument("--asof", default=None)
    parser.add_argument("--window", type=int, default=90)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    evidence = build(args.asof, args.window)
    out = Path(args.output) if args.output else (
        SKILL_ROOT / "evidence" / f"dc-{evidence['asof']}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "output": str(out),
        "asof": evidence["asof"],
        "curve_lag_days": evidence["anchors"]["curve_lag_days"],
        "issuers": len(evidence["issuers"]),
        "rungs": len(evidence["ladder"]),
        "events": len(evidence["events"]),
        "thin_curve": evidence["quality_summary"]["thin_curve_issuers"],
        "convertibles": len(evidence["convertibles"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
