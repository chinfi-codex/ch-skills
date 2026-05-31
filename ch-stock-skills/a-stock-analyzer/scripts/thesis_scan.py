#!/usr/bin/env python3
"""Deterministic thesis & anomaly scanner over an a-stock-analyzer evidence pack.

This script ONLY surfaces — it computes growth/valuation/quality metrics against
fixed thresholds and emits (1) candidate investment-thesis archetypes (including
the 预期差 sub-types A1/A2/B) and (2) anomaly flags, each with a suggested probe
(which原文/dataset to pull next). It makes NO buy/sell or "cheap/expensive"
judgement — confirming a thesis, attributing a cause and all wording are the
model's job, per references/deep_mode.md.

Usage:
    python scripts/thesis_scan.py --evidence reports/evidence_688213.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# --- thresholds (deterministic surfacing knobs; not judgements) -------------- #
T = {
    "growth_cagr_strong": 15.0,        # %: revenue/profit CAGR worth a growth thesis
    "yoy_surge": 50.0,                 # %: |YoY| beyond this = 暴增/暴跌 flag
    "pe_pct_low": 30.0,                # valuation percentile considered low
    "pe_abs_cheap": 25.0,              # absolute PE-TTM considered low
    "pe_abs_elevated": 30.0,           # absolute PE-TTM considered elevated
    "ocf_ni_weak": 0.5,                # OCF/净利 below this = 利润含金量弱
    "margin_swing_pct": 3.0,           # pct-point gross/net margin change to flag
    "recv_vs_rev_gap": 15.0,           # pct: 应收增速 - 营收增速 gap to flag
    "inv_vs_rev_gap": 15.0,            # pct: 存货增速 - 营收增速 gap to flag
    "inv_to_rev_high": 35.0,           # %: inventory/annual revenue high watermark
    "goodwill_to_eqt": 30.0,           # %: 商誉/净资产 impairment-risk watermark
    "debt_to_assets_high": 70.0,      # %: 资产负债率 (非金融) watermark
    "dedt_divergence": 0.7,            # 扣非/归母 below this = 利润质量存疑
    "rd_intensity_rise": 1.0,          # pct-point rise in 研发/营收 = option signal
}


def to_num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if not math.isnan(f) else None


def dataset(evidence: dict, name: str) -> Any:
    entry = (evidence.get("datasets") or {}).get(name)
    if isinstance(entry, dict):
        if entry.get("ok") is False:
            return None
        return entry.get("data", entry)
    return entry


def annual_rows(rows: List[dict]) -> List[dict]:
    # Dedup by end_date (Tushare returns restated/duplicate rows); last wins.
    by_end: Dict[str, dict] = {}
    for r in rows:
        if isinstance(r, dict) and str(r.get("end_date", "")).endswith("1231"):
            by_end[str(r.get("end_date"))] = r
    return [by_end[k] for k in sorted(by_end)]


def cagr(first: Optional[float], last: Optional[float], periods: int) -> Optional[float]:
    if first is None or last is None or first <= 0 or last <= 0 or periods < 1:
        return None
    return (pow(last / first, 1.0 / periods) - 1.0) * 100.0


def latest_window(band: dict, metric: str) -> Dict[str, Any]:
    """Return the longest available percentile window for a metric."""
    m = (band or {}).get(metric) or {}
    windows = m.get("windows") or {}
    for key in ("5y", "3y", "1y"):
        if windows.get(key) and windows[key].get("sample_size"):
            return {"window": key, **windows[key], "current": m.get("current")}
    return {"current": m.get("current")}


def build_scan(evidence: dict) -> Dict[str, Any]:
    ds = lambda n: dataset(evidence, n)  # noqa: E731
    income = ds("income") if isinstance(ds("income"), list) else []
    financial = ds("financial") if isinstance(ds("financial"), list) else []
    balance = ds("balance") if isinstance(ds("balance"), list) else []
    cashflow = ds("cashflow") if isinstance(ds("cashflow"), list) else []
    band = ds("valuation-band") if isinstance(ds("valuation-band"), dict) else {}
    bands = band.get("bands") or {}

    fin_by_end = {str(r.get("end_date")): r for r in financial if isinstance(r, dict) and r.get("end_date")}
    bal_by_end = {str(r.get("end_date")): r for r in balance if isinstance(r, dict) and r.get("end_date")}
    cf_by_end = {str(r.get("end_date")): r for r in cashflow if isinstance(r, dict) and r.get("end_date")}

    ann_inc = annual_rows(income)
    ts_code = band.get("ts_code") or (income[0].get("ts_code") if income else None)

    # ---- growth trajectory (annual) ----
    growth: Dict[str, Any] = {"annual_periods": [r.get("end_date") for r in ann_inc]}
    if len(ann_inc) >= 2:
        n = len(ann_inc) - 1
        rev0, rev1 = to_num(ann_inc[0].get("revenue")), to_num(ann_inc[-1].get("revenue"))
        ni0, ni1 = to_num(ann_inc[0].get("n_income_attr_p")), to_num(ann_inc[-1].get("n_income_attr_p"))
        growth["revenue_cagr_pct"] = round(cagr(rev0, rev1, n), 2) if cagr(rev0, rev1, n) is not None else None
        growth["netprofit_cagr_pct"] = round(cagr(ni0, ni1, n), 2) if cagr(ni0, ni1, n) is not None else None
        growth["years_span"] = n
    rev_yoy = [to_num((fin_by_end.get(str(r.get("end_date"))) or {}).get("tr_yoy")) for r in ann_inc]
    rev_yoy = [v for v in rev_yoy if v is not None]
    growth["revenue_yoy_series"] = rev_yoy
    growth["all_positive_yoy"] = bool(rev_yoy) and all(v > 0 for v in rev_yoy)
    growth["yoy_volatility"] = round(max(rev_yoy) - min(rev_yoy), 2) if len(rev_yoy) >= 2 else None

    # ---- valuation snapshot ----
    pe = latest_window(bands, "pe_ttm")
    pb = latest_window(bands, "pb")
    ps = latest_window(bands, "ps_ttm")
    valuation = {
        "pe_ttm": pe.get("current"),
        "pe_pct": pe.get("current_percentile"),
        "pe_window": pe.get("window"),
        "pb": pb.get("current"),
        "pb_pct": pb.get("current_percentile"),
        "ps_ttm": ps.get("current"),
        "ps_pct": ps.get("current_percentile"),
    }

    flags: List[Dict[str, Any]] = []

    def flag(category, period, metric, value, threshold, direction, severity, probe):
        flags.append({
            "category": category, "period": period, "metric": metric,
            "value": value, "threshold": threshold, "direction": direction,
            "severity": severity, "probe": probe,
        })

    # ---- anomaly flags ----
    # 业绩 YoY surge / collapse (annual + latest quarter)
    for r in (ann_inc[-2:] + ([income[0]] if income else [])):
        ed = str(r.get("end_date"))
        f = fin_by_end.get(ed) or {}
        for key, label in (("tr_yoy", "营收YoY"), ("netprofit_yoy", "归母YoY")):
            v = to_num(f.get(key))
            if v is not None and abs(v) >= T["yoy_surge"]:
                flag("业绩异常", ed, label, round(v, 2), T["yoy_surge"],
                     "暴增" if v > 0 else "暴跌", "high",
                     {"why": "增速极端，需查驱动/可持续性/基数",
                      "cmd": f"fetch report-text {ts_code} --report-type annual --section mda",
                      "and": "联网检索行业景气与新闻"})
        # 扣非 vs 归母 divergence
        dedt, ni = to_num(f.get("profit_dedt")), to_num(r.get("n_income_attr_p"))
        if dedt is not None and ni and ni > 0 and (dedt / ni) < T["dedt_divergence"]:
            flag("利润质量", ed, "扣非/归母", round(dedt / ni, 2), T["dedt_divergence"],
                 "扣非显著低于归母", "med",
                 {"why": "非经常性损益占比高", "cmd": f"fetch income {ts_code} --limit 8",
                  "and": f"fetch report-text {ts_code} --report-type annual --section 财务报告"})

    # margin swings (latest two annuals)
    if len(ann_inc) >= 2:
        for key, label in (("grossprofit_margin", "毛利率"), ("netprofit_margin", "净利率")):
            a = to_num((fin_by_end.get(str(ann_inc[-2].get("end_date"))) or {}).get(key))
            b = to_num((fin_by_end.get(str(ann_inc[-1].get("end_date"))) or {}).get(key))
            if a is not None and b is not None and abs(b - a) >= T["margin_swing_pct"]:
                flag("利润质量", ann_inc[-1].get("end_date"), label, round(b - a, 2),
                     T["margin_swing_pct"], "上行" if b > a else "下行",
                     "med" if b < a else "low",
                     {"why": "利润率拐点", "cmd": f"fetch report-text {ts_code} --report-type annual --section mda"})

    # OCF / 净利 (latest annual)
    if ann_inc:
        ed = str(ann_inc[-1].get("end_date"))
        ocf = to_num((cf_by_end.get(ed) or {}).get("n_cashflow_act"))
        ni = to_num(ann_inc[-1].get("n_income_attr_p"))
        if ocf is not None and ni and ni > 0:
            ratio = ocf / ni
            if ratio < T["ocf_ni_weak"]:
                flag("利润质量", ed, "经营现金流/净利", round(ratio, 2), T["ocf_ni_weak"],
                     "利润未转现金", "high",
                     {"why": "利润含金量弱，查营运资本占用",
                      "cmd": f"fetch report-text {ts_code} --report-type annual --section 财务报告",
                      "and": "看应收/存货附注与信用政策"})

    # 应收 / 存货 vs 营收 growth (latest annual vs prior)
    if len(ann_inc) >= 2:
        ed, ped = str(ann_inc[-1].get("end_date")), str(ann_inc[-2].get("end_date"))
        rev_g = to_num((fin_by_end.get(ed) or {}).get("tr_yoy"))
        def grow(by_end, field):
            a = to_num((by_end.get(ped) or {}).get(field)); b = to_num((by_end.get(ed) or {}).get(field))
            return ((b - a) / a * 100.0) if (a and a > 0 and b is not None) else None
        recv_g = grow(bal_by_end, "accounts_receiv")
        inv_g = grow(bal_by_end, "inventories")
        if rev_g is not None and recv_g is not None and (recv_g - rev_g) >= T["recv_vs_rev_gap"]:
            flag("营运资本", ed, "应收增速-营收增速", round(recv_g - rev_g, 1), T["recv_vs_rev_gap"],
                 "应收扩张快于收入", "med",
                 {"why": "可能放宽信用确认收入", "cmd": f"fetch report-text {ts_code} --report-type annual --section 财务报告"})
        if rev_g is not None and inv_g is not None and (inv_g - rev_g) >= T["inv_vs_rev_gap"]:
            flag("营运资本", ed, "存货增速-营收增速", round(inv_g - rev_g, 1), T["inv_vs_rev_gap"],
                 "存货扩张快于收入", "med",
                 {"why": "去库存/跌价风险", "cmd": f"fetch report-text {ts_code} --report-type annual --section mda"})
        # inventory / revenue ratio
        inv = to_num((bal_by_end.get(ed) or {}).get("inventories"))
        rev = to_num(ann_inc[-1].get("revenue"))
        if inv is not None and rev and rev > 0 and (inv / rev * 100.0) >= T["inv_to_rev_high"]:
            flag("营运资本", ed, "存货/营收", round(inv / rev * 100.0, 1), T["inv_to_rev_high"],
                 "存货占营收高", "low",
                 {"why": "备货高，关注周转与跌价", "cmd": f"fetch balance {ts_code} --limit 8"})

    # 商誉 / 净资产, 资产负债率 (latest annual)
    if ann_inc:
        ed = str(ann_inc[-1].get("end_date"))
        gw = to_num((bal_by_end.get(ed) or {}).get("goodwill"))
        eqt = to_num((bal_by_end.get(ed) or {}).get("total_hldr_eqy_inc_min_int"))
        if gw is not None and eqt and eqt > 0 and (gw / eqt * 100.0) >= T["goodwill_to_eqt"]:
            flag("减值风险", ed, "商誉/净资产", round(gw / eqt * 100.0, 1), T["goodwill_to_eqt"],
                 "商誉占比高", "high",
                 {"why": "并购减值风险", "cmd": f"fetch report-text {ts_code} --report-type annual --section 财务报告",
                  "and": f"fetch announcements {ts_code} --searchkey 收购"})
        dta = to_num((fin_by_end.get(ed) or {}).get("debt_to_assets"))
        if dta is not None and dta >= T["debt_to_assets_high"]:
            flag("资本结构", ed, "资产负债率", round(dta, 1), T["debt_to_assets_high"],
                 "杠杆偏高", "med",
                 {"why": "偿债与财务费用压力", "cmd": f"fetch balance {ts_code} --limit 8"})

    # ---- thesis archetypes ----
    theses: List[Dict[str, Any]] = []
    rev_cagr = growth.get("revenue_cagr_pct")
    ni_cagr = growth.get("netprofit_cagr_pct")
    best_cagr = max([c for c in (rev_cagr, ni_cagr) if c is not None], default=None)
    pe_abs, pe_pct = valuation["pe_ttm"], valuation["pe_pct"]
    stable_grow = growth.get("all_positive_yoy")

    def add_thesis(archetype, subtype, rationale, probes, confidence_hint="待证实"):
        theses.append({"archetype": archetype, "subtype": subtype, "rationale": rationale,
                       "confidence_hint": confidence_hint, "probes": probes})

    if best_cagr is not None and best_cagr >= T["growth_cagr_strong"] and stable_grow:
        # 预期差: growth not (fully) priced — split A1/A2 by absolute PE + percentile
        de_rated = (pe_pct is not None and pe_pct <= T["pe_pct_low"])
        cheap_abs = (pe_abs is not None and pe_abs <= T["pe_abs_cheap"])
        if cheap_abs and de_rated:
            add_thesis("预期差", "A2 低估值+成长",
                       f"营收/利润 CAGR≈{best_cagr}% 且逐年正增长，PE_ttm={pe_abs}（{valuation['pe_window']}分位{pe_pct}%）绝对与分位双低",
                       [{"why": "确认增长未被定价、是否真便宜",
                         "cmd": "长周期日线确认月线横盘 + 主线归属", "and": f"fetch valuation-band {ts_code} --years 5"}],
                       "重点：最干净的预期差")
        elif de_rated and pe_abs is not None and pe_abs >= T["pe_abs_elevated"]:
            add_thesis("预期差", "A1 高估值消化",
                       f"CAGR≈{best_cagr}%，PE_ttm={pe_abs} 仍偏高但已 de-rate 到{valuation['pe_window']}分位{pe_pct}%（盈利追赶估值）",
                       [{"why": "判断消化是否到位、增速能否延续",
                         "cmd": f"fetch report-text {ts_code} --report-type annual --section mda"}],
                       "估值消化型")
        else:
            add_thesis("稳健/高增长", "增长已部分定价",
                       f"CAGR≈{best_cagr}%，PE_ttm={pe_abs}（分位{pe_pct}%）",
                       [{"why": "增速可持续性与透支判断", "cmd": f"fetch report-text {ts_code} --report-type annual --section mda"}])

    # B 期权型: rising R&D intensity / new-business — confirm via filings (always)
    rd_series = []
    for r in ann_inc:
        rev = to_num(r.get("revenue")); rd = to_num(r.get("rd_exp"))
        if rev and rev > 0 and rd is not None:
            rd_series.append(rd / rev * 100.0)
    rd_rising = len(rd_series) >= 2 and (rd_series[-1] - rd_series[0]) >= T["rd_intensity_rise"]
    add_thesis("预期差", "B 未定价期权",
               (f"研发/营收由 {round(rd_series[0],1)}% 升至 {round(rd_series[-1],1)}%，可能有未被定价的新业务/远期技术期权"
                if rd_rising else "需读年报/季报确认是否存在未被定价的新业务/远期技术期权（机器人/AI 等）"),
               [{"why": "捞公司自述的新布局/在研/募投，判断材料性与主线连接",
                 "cmd": f"fetch report-text {ts_code} --report-type q1 --section AI",
                 "and": f"fetch report-text {ts_code} --report-type annual --section mda + 联网检索当前主线"}],
               "必须靠方向二年报挖掘确认")

    notes = []
    if not band:
        notes.append("缺 valuation-band：无法判断估值分位，预期差 A1/A2 分型不可靠。")
    if len(ann_inc) < 3:
        notes.append(f"年度样本仅 {len(ann_inc)} 期：CAGR/稳定性代表性弱，建议 income/financial 提高 --limit。")
    notes.append("月线横盘需长周期日线确认（pack 默认 daily 仅 60 日）。")

    return {
        "ts_code": ts_code,
        "as_of": band.get("latest_trade_date"),
        "growth": growth,
        "valuation": valuation,
        "thesis_candidates": theses,
        "anomaly_flags": flags,
        "notes": notes,
        "model_responsibility": (
            "脚本只做阈值 surfacing：命题原型与异常旗标都是线索，不是结论。"
            "命题证实/证伪、归因、贵贱与买卖判断、措辞，全部由模型按 references/deep_mode.md 完成。"
        ),
    }


def _stderr_summary(scan: dict) -> str:
    """One-line human-readable summary printed to stderr so a Deep run is visible
    in the terminal even when stdout is captured to JSON."""
    code = scan.get("ts_code", "?")
    theses = scan.get("thesis_candidates") or []
    subtypes = [t.get("subtype") or t.get("archetype") or "?" for t in theses]
    thesis_str = "+".join(subtypes) if subtypes else "none"

    flags = scan.get("anomaly_flags") or []
    sev_counts: Dict[str, int] = {}
    for f in flags:
        s = str(f.get("severity") or "?")
        sev_counts[s] = sev_counts.get(s, 0) + 1
    flag_str = " ".join(f"{k}:{v}" for k, v in sorted(sev_counts.items())) or "0"

    probe_count = sum(len(t.get("probes") or []) for t in theses) + sum(
        1 for f in flags if f.get("probe")
    )
    high_priority = sev_counts.get("high", 0) + sum(
        1 for t in theses if "预期差" in (t.get("archetype") or "")
    )
    pdf_budget = max(3, min(10, high_priority + 2))
    web_budget = max(3, min(10, len(theses) * 2 + 1))

    return (
        f"[thesis_scan] {code} thesis={thesis_str} | flags={flag_str} | "
        f"probes={probe_count} | budget hint: ≤{pdf_budget} PDF / ≤{web_budget} web"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic thesis & anomaly scan over an evidence pack.")
    parser.add_argument("--evidence", "-e", required=True, help="Path to evidence_<code>.json from data_fetcher pack.")
    parser.add_argument("--out", "-o", default=None, help="Optional path to write the scan JSON.")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable stderr summary (kept for scripted callers).",
    )
    args = parser.parse_args(argv)

    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    scan = build_scan(evidence)
    text = json.dumps(scan, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    if not args.quiet:
        print(_stderr_summary(scan), file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
