#!/usr/bin/env python3
"""Verdict ledger for the earnings-forecast skill: tier (强/中/观察/剔除) + 主线归属.

脑/手 boundary: this script is deterministic and calls no LLM. It only
  - `context`: assembles what the model must judge — the to-judge stock set
    (newly disclosed + stale-vs-current-evidence) plus the daily-market-sense
    theme registry (name/aliases/current lifecycle state) as the matching
    reference — into one JSON.
  - `record`: validates the model's verdict JSON (tier enum, theme_id exists in
    theme_registry), snapshots evidence_asof from the evidence pack, and upserts
    into forecast_verdict.

Which tier, which 主线, and why are the model's calls (see references/verdict_and_html.md).

Usage:
    python3 scripts/verdict.py context --period 20260630
    python3 scripts/verdict.py record  --period 20260630 --input reports/verdict_20260630.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from store import Store

TIERS = ["强", "中", "观察", "剔除"]
CONFIDENCES = ["high", "medium", "low"]
_MMDD_Q = {"0331": 1, "0630": 2, "0930": 3, "1231": 4}


def quarter_of(period: str) -> int:
    if period[4:] not in _MMDD_Q:
        raise ValueError(f"{period} 不是季度末(0331/0630/0930/1231)。")
    return _MMDD_Q[period[4:]]


def period_label(period: str) -> str:
    q = quarter_of(period)
    return f"{period[:4]}H1" if q == 2 else f"{period[:4]}Q{q}"


def latest_quarter_end(today: dt.date) -> str:
    ymd = today.strftime("%Y%m%d")
    for c in sorted([f"{today.year}0331", f"{today.year}0630", f"{today.year}0930",
                     f"{today.year}1231", f"{today.year - 1}1231"], reverse=True):
        if c <= ymd:
            return c
    return f"{today.year - 1}1231"


def _load_json(path: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _evidence_index(evidence: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(s["ts_code"]): s for s in evidence.get("stocks", [])}


def _enrich_index(enrich: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not enrich:
        return {}
    return {str(s["ts_code"]): s for s in enrich.get("stocks", []) if s.get("found")}


def _kf_summary(rec: Dict[str, Any]) -> Optional[Any]:
    parsed = (rec or {}).get("parsed") or {}
    kf = parsed.get("kf_net_profit_yi")
    if not kf:
        return None
    return {"low": kf.get("low"), "high": kf.get("high"), "point": kf.get("point"),
            "confidence": kf.get("confidence")}


# --------------------------------------------------------------------------- #
def build_context(period: str, evidence: Dict[str, Any], enrich: Optional[Dict[str, Any]],
                  verdicts: Dict[str, Dict[str, Any]], themes: Dict[str, Dict[str, Any]],
                  states: Dict[str, Dict[str, Any]], drift: float) -> Dict[str, Any]:
    ev_idx = _evidence_index(evidence)
    en_idx = _enrich_index(enrich)

    to_judge: List[Dict[str, Any]] = []
    for ts_code, s in ev_idx.items():
        pg = s.get("profit_growth", {})
        cur_ann = str(s.get("ann_date") or "")
        cur_cum = pg.get("cum_yoy_pct")
        v = verdicts.get(ts_code)
        if v is None:
            why = "new"
        else:
            prev_ann = str(v.get("evidence_ann_date") or "")
            prev_cum = v.get("evidence_cum_yoy")
            drifted = (prev_cum is not None and cur_cum is not None
                       and abs(float(cur_cum) - float(prev_cum)) > drift)
            if cur_ann and prev_ann and cur_ann != prev_ann:
                why = "stale_revised"
            elif drifted:
                why = "stale_drift"
            else:
                continue  # already judged and still current
        pr = s.get("price_reaction") or {}
        item = {
            "ts_code": ts_code,
            "name": s.get("name", ""),
            "industry": s.get("industry", ""),
            "type": s.get("type", ""),
            "cum_yoy_pct": cur_cum,
            "single_q_yoy_pct": pg.get("single_q_yoy_pct"),
            "qoq_pct": pg.get("qoq_pct"),
            "net_profit_median_yi": s.get("net_profit", {}).get("median_yi"),
            "change_reason": s.get("change_reason", ""),
            "kf_net_profit_yi": _kf_summary(en_idx.get(ts_code)),
            "price_reaction": {
                "gap_open_pct": pr.get("gap_open_pct"),
                "r_day_pct": pr.get("r_day_pct"),
                "since_ann_pct": pr.get("since_ann_pct"),
                "gap_status": pr.get("gap_status"),
                "pre_pos_1y_pct": pr.get("pre_pos_1y_pct"),
                "pre_mom_20d_pct": pr.get("pre_mom_20d_pct"),
            } if pr else None,
            "reason_to_judge": why,
        }
        if v is not None:
            item["prev_tier"] = v.get("tier")
            item["prev_theme_id"] = v.get("theme_id")
        to_judge.append(item)

    theme_ref = []
    for tid, meta in themes.items():
        st = states.get(tid, {})
        theme_ref.append({
            "theme_id": tid,
            "name": meta.get("name", ""),
            "aliases": meta.get("aliases", []),
            "state": st.get("state"),
            "stars": st.get("stars"),
            "position": st.get("position"),
            "crowding": st.get("crowding"),
            "asof": st.get("trade_date"),
            "members_sample": st.get("members_sample", []),
        })
    theme_ref.sort(key=lambda t: (-(t["stars"] or 0), t["theme_id"]))

    return {
        "period": period,
        "period_label": period_label(period),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "enum": {"tiers": TIERS, "match_confidence": CONFIDENCES},
        "already_judged": len(verdicts),
        "to_judge_count": len(to_judge),
        "theme_registry_empty": len(themes) == 0,
        "themes": theme_ref,
        "to_judge": to_judge,
        "notes": [
            "对每只 to_judge 股判 tier(强/中/观察/剔除)与 theme_id(归属主线，对不上填 null=无归属)。",
            "主线匹配靠语义:用 change_reason/行业 比对 themes 的 name/aliases/members_sample;弱匹配 match_confidence=low。",
            "price_reaction 是净利润断层证据：gap_open_pct=公告次日跳空、pre_pos_1y_pct=公告前一年分位"
            "(高位=趋势加速型/低位=低位启动型)、gap_status=intact 未回补。断层且在主线内的胜率锚更高，"
            "回补(filled)或业绩强但股价长期无反应要在 caveat 里点明；判断细则见 references/methodology.md §八。",
            "reason_to_judge=stale_* 的是预告已修订或增速漂移，需复判；其余是新披露。",
            "theme_registry_empty=true 时主线台账为空(需先跑 daily-market-sense)，theme_id 一律填 null。",
        ],
    }


def validate_records(records: List[Dict[str, Any]], ev_idx: Dict[str, Dict[str, Any]],
                     themes: Dict[str, Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    valid: List[Dict[str, Any]] = []
    errors: List[str] = []
    for r in records:
        ts_code = str(r.get("ts_code") or "")
        if ts_code not in ev_idx:
            errors.append(f"{ts_code or '(空)'}: 不在本期 evidence 中，跳过。")
            continue
        tier = r.get("tier")
        if tier not in TIERS:
            errors.append(f"{ts_code}: tier={tier!r} 非法(应为 {TIERS})，跳过。")
            continue
        conf = r.get("match_confidence")
        if conf is not None and conf not in CONFIDENCES:
            errors.append(f"{ts_code}: match_confidence={conf!r} 非法(应为 {CONFIDENCES} 或 null)，跳过。")
            continue
        theme_id = r.get("theme_id")
        if theme_id and theme_id not in themes:
            errors.append(f"{ts_code}: theme_id={theme_id!r} 不在 theme_registry(合法: {sorted(themes)[:6]}…)，跳过。")
            continue
        pg = ev_idx[ts_code].get("profit_growth", {})
        valid.append({
            "period": r.get("period"),  # filled by caller
            "ts_code": ts_code,
            "tier": tier,
            "reason": r.get("reason"),
            "caveat": r.get("caveat"),
            "theme_id": theme_id or None,
            "theme_rationale": r.get("theme_rationale"),
            "match_confidence": conf,
            "evidence_ann_date": str(ev_idx[ts_code].get("ann_date") or ""),
            "evidence_cum_yoy": pg.get("cum_yoy_pct"),
        })
    return valid, errors


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Earnings-forecast verdict ledger (tier + 主线归属).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ctx = sub.add_parser("context", help="Assemble the to-judge set + theme reference.")
    ctx.add_argument("--period")
    ctx.add_argument("--evidence")
    ctx.add_argument("--enrich")
    ctx.add_argument("--drift", type=float, default=20.0, help="累计同比漂移超过 N 个百分点判为待复判(默认 20)。")
    ctx.add_argument("--out")
    ctx.add_argument("--stdout", action="store_true")

    rec = sub.add_parser("record", help="Validate + upsert the model's verdict JSON.")
    rec.add_argument("--period")
    rec.add_argument("--input", required=True)
    rec.add_argument("--evidence")

    args = ap.parse_args(argv)
    today = dt.date.today()
    period = args.period or latest_quarter_end(today)
    quarter_of(period)

    store = Store()

    if args.cmd == "context":
        ev_path = args.evidence or os.path.join("reports", f"forecast_scan_{period}.json")
        evidence = _load_json(ev_path)
        if evidence is None:
            print(json.dumps({"error": f"evidence 不存在: {ev_path}，请先运行 forecast_scan.py。"}, ensure_ascii=False))
            return 2
        en_path = args.enrich or os.path.join("reports", f"cninfo_enrich_{period}.json")
        enrich = _load_json(en_path)
        context = build_context(
            period, evidence, enrich, store.load_verdicts(period),
            store.load_theme_registry(), store.load_theme_latest_state(), args.drift,
        )
        out_path = args.out or os.path.join("reports", f"verdict_context_{period}.json")
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(context, fh, ensure_ascii=False, indent=2)
        summary = {"period": period, "to_judge": context["to_judge_count"],
                   "already_judged": context["already_judged"],
                   "themes": len(context["themes"]), "out": out_path}
        print(json.dumps(context if args.stdout else summary, ensure_ascii=False, indent=2))
        return 0

    # record
    payload = _load_json(args.input)
    if payload is None:
        print(json.dumps({"error": f"输入不存在: {args.input}"}, ensure_ascii=False))
        return 2
    period = payload.get("period") or period
    ev_path = args.evidence or os.path.join("reports", f"forecast_scan_{period}.json")
    evidence = _load_json(ev_path)
    if evidence is None:
        print(json.dumps({"error": f"evidence 不存在: {ev_path}，record 需要它快照 evidence_asof。"}, ensure_ascii=False))
        return 2
    ev_idx = _evidence_index(evidence)
    valid, errors = validate_records(payload.get("records", []), ev_idx, store.load_theme_registry())
    for r in valid:
        r["period"] = period
    store.upsert_verdicts(valid)
    print(json.dumps({
        "period": period, "recorded": len(valid), "skipped": len(errors),
        "cache": "on" if store.available else f"off ({store.reason})",
        "errors": errors,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
