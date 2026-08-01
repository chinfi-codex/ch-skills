#!/usr/bin/env python3
"""Verdict ledger for the quarterly-report skill: tier (强/中/观察/剔除) +
质量成色 + 兑现度 + 主线归属 + 报告期行业趋势.

脑/手 boundary: this script is deterministic and calls no LLM. It only
  - `context`: assembles what the model must judge — the to-judge stock set
    (newly filed + stale-vs-current-evidence), the daily-market-sense theme
    registry as the matching reference, per-theme member briefs split into
    强表现 (up gap) / 弱表现 (down gap) / 无断层.
  - `record`: validates the model's verdict JSON (enums, theme existence,
    snapshots the evidence fingerprint, and upserts into qreport_verdict /
    qreport_theme_trend.

One thing differs from the forecast ledger by design. A forecast ships its own
《业绩变动原因》 text, so attribution could read straight off the evidence. A
statutory report ships numbers; the narrative lives in the MD&A PDF, which this
skill only opens on demand. So the attribution material for industry trends is
the model's own `reason` from the previous round plus the mechanical hits — and
where that is not enough, the answer is to pull the MD&A with `report_pdf.py`,
not to guess.

Usage:
    python3 scripts/verdict.py context --period 20260331
    python3 scripts/verdict.py record  --period 20260331 --input reports/qreport_verdict_20260331.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from store import Store  # noqa: E402

TIERS = ["强", "中", "观察", "剔除"]
CONFIDENCES = ["high", "medium", "low"]
TREND_DIRECTIONS = ["向上", "向下", "分化", "证据不足"]
# 质量成色: how much of the reported profit survives scrutiny (扣非 + 现金流 + 收入质量).
QUALITY_CALLS = ["扎实", "尚可", "存疑", "虚高"]
# 兑现度: how the statutory number landed against the company's own guidance.
FULFILLMENTS = ["超预告上限", "落区间上沿", "符合", "落区间下沿", "低于预告", "无预告"]
REASON_BRIEF = 220
_MMDD_Q = {"0331": 1, "0630": 2, "0930": 3, "1231": 4}
BEIJING_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")


def quarter_of(period: str) -> int:
    if not re.fullmatch(r"\d{8}", str(period)):
        raise ValueError(f"{period} 不是季度末(YYYYMMDD)。")
    if period[4:] not in _MMDD_Q:
        raise ValueError(f"{period} 不是季度末(0331/0630/0930/1231)。")
    return _MMDD_Q[period[4:]]


def period_label(period: str) -> str:
    q = quarter_of(period)
    return {1: f"{period[:4]}Q1", 2: f"{period[:4]}H1",
            3: f"{period[:4]}Q3", 4: f"{period[:4]}年报"}[q]


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


def _brief(s: Dict[str, Any], verdict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compact per-stock card: the numbers a tiering decision actually turns on."""
    g, q = s.get("growth", {}), s.get("quality", {})
    m, b = s.get("margins", {}), s.get("balance_signals", {})
    pr, sc = s.get("price_reaction") or {}, s.get("screen", {})
    ff = (s.get("fulfillment") or {}).get("forecast") or {}
    out = {
        "ts_code": s["ts_code"], "name": s.get("name", ""), "industry": s.get("industry", ""),
        "ann_date": s.get("ann_date"),
        "rev_cum_yoy_pct": g.get("revenue", {}).get("cum_yoy_pct"),
        "rev_single_yoy_pct": g.get("revenue", {}).get("single_q_yoy_pct"),
        "np_cum_yi": g.get("np", {}).get("cum_yi"),
        "np_cum_yoy_pct": g.get("np", {}).get("cum_yoy_pct"),
        "np_single_yoy_pct": g.get("np", {}).get("single_q_yoy_pct"),
        "np_qoq_pct": g.get("np", {}).get("qoq_pct"),
        "dedt_single_yoy_pct": g.get("dedt", {}).get("single_q_yoy_pct"),
        "dedt_ratio_pct": q.get("dedt_ratio_pct"),
        "ocf_to_np_pct": q.get("ocf_to_np_pct"),
        "roe_annualized_pct": q.get("roe_annualized_pct"),
        "gross_margin_single_pct": m.get("gross_margin_single_pct"),
        "gross_margin_single_yoy_pp": m.get("gross_margin_single_yoy_pp"),
        "contract_liab_yoy_pct": (b.get("contract_liab") or {}).get("yoy_pct"),
        "receivable_vs_revenue_gap_pp": b.get("receivable_vs_revenue_gap_pp"),
        "inventory_vs_revenue_gap_pp": b.get("inventory_vs_revenue_gap_pp"),
        "pe_ttm_dedt": (s.get("valuation") or {}).get("pe_ttm_dedt"),
        "forecast_in_range": ff.get("in_range"),
        "forecast_vs_median_pct": ff.get("vs_median_pct"),
        "hits": sc.get("hits", []),
        "rank_score": sc.get("rank_score"),
        "price_reaction": {
            "gap_open_pct": pr.get("gap_open_pct"), "gap_dir": pr.get("gap_dir"),
            "gap_status": pr.get("gap_status"), "since_ann_pct": pr.get("since_ann_pct"),
            "pre_pos_pct": pr.get("pre_pos_pct"), "pre_pos_bars": pr.get("pre_pos_bars"),
            "trading_days_since_r": pr.get("trading_days_since_r"),
        } if pr else None,
    }
    if verdict:
        reason = str(verdict.get("reason") or "")
        out["prev_reason"] = reason[:REASON_BRIEF] + ("…" if len(reason) > REASON_BRIEF else "")
    return out


def build_theme_membership(ev_idx: Dict[str, Dict[str, Any]],
                           verdicts: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Per-theme member briefs grouped by gap direction. Deterministic grouping
    only — what the trend means is judged by the model."""
    members: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for ts_code, v in verdicts.items():
        tid = v.get("theme_id")
        s = ev_idx.get(ts_code)
        if not tid or s is None:
            continue
        pr = s.get("price_reaction") or {}
        reason = str(v.get("reason") or "")
        brief = {
            "ts_code": ts_code, "name": s.get("name", ""),
            "tier": v.get("tier"), "quality_call": v.get("quality_call"),
            "gap_open_pct": pr.get("gap_open_pct"), "gap_status": pr.get("gap_status"),
            "since_ann_pct": pr.get("since_ann_pct"),
            "np_single_yoy_pct": (s.get("growth", {}).get("np") or {}).get("single_q_yoy_pct"),
            "rev_single_yoy_pct": (s.get("growth", {}).get("revenue") or {}).get("single_q_yoy_pct"),
            "gross_margin_single_yoy_pp": (s.get("margins") or {}).get("gross_margin_single_yoy_pp"),
            "hits": (s.get("screen") or {}).get("hits", []),
            "model_reason": reason[:REASON_BRIEF] + ("…" if len(reason) > REASON_BRIEF else ""),
        }
        grp = members.setdefault(tid, {"strong": [], "weak": [], "quiet": []})
        gap_dir = pr.get("gap_dir")
        if gap_dir == "up":
            grp["strong"].append(brief)
        elif gap_dir == "down":
            grp["weak"].append(brief)
        else:
            grp["quiet"].append(brief)
    return members


def theme_member_counts(membership: Dict[str, Dict[str, List[Dict[str, Any]]]],
                        tid: str) -> Tuple[int, int, int]:
    grp = membership.get(tid, {"strong": [], "weak": [], "quiet": []})
    s_n, w_n = len(grp["strong"]), len(grp["weak"])
    return s_n, w_n, s_n + w_n + len(grp["quiet"])


# --------------------------------------------------------------------------- #
def build_context(period: str, evidence: Dict[str, Any], verdicts: Dict[str, Dict[str, Any]],
                   themes: Dict[str, Dict[str, Any]], states: Dict[str, Dict[str, Any]],
                   trends: Dict[str, Dict[str, Any]], drift: float) -> Dict[str, Any]:
    ev_idx = _evidence_index(evidence)

    to_judge: List[Dict[str, Any]] = []
    for ts_code, s in ev_idx.items():
        g = s.get("growth", {})
        cur_ann = str(s.get("ann_date") or "")
        cur_np = (g.get("np") or {}).get("single_q_yoy_pct")
        cur_rev = (g.get("revenue") or {}).get("single_q_yoy_pct")
        v = verdicts.get(ts_code)
        if v is None:
            why = "new"
        else:
            prev_ann = str(v.get("evidence_ann_date") or "")
            prev_np = v.get("evidence_np_single_yoy")
            drifted = (prev_np is not None and cur_np is not None
                       and abs(float(cur_np) - float(prev_np)) > drift)
            if cur_ann and prev_ann and cur_ann != prev_ann:
                # A changed filing date on a statutory report means a restatement
                # or a corrected filing — always worth re-reading.
                why = "stale_restated"
            elif drifted:
                why = "stale_drift"
            else:
                continue
        item = _brief(s, v)
        item["reason_to_judge"] = why
        if v is not None:
            item["prev_tier"] = v.get("tier")
            item["prev_quality_call"] = v.get("quality_call")
            item["prev_theme_id"] = v.get("theme_id")
        to_judge.append(item)
    to_judge.sort(key=lambda x: -(x.get("rank_score") or -99))

    theme_ref = []
    for tid, meta in themes.items():
        st = states.get(tid, {})
        theme_ref.append({
            "theme_id": tid, "name": meta.get("name", ""), "aliases": meta.get("aliases", []),
            "state": st.get("state"), "stars": st.get("stars"), "position": st.get("position"),
            "crowding": st.get("crowding"), "asof": st.get("trade_date"),
            "members_sample": st.get("members_sample", []),
        })
    theme_ref.sort(key=lambda t: (-(t["stars"] or 0), t["theme_id"]))

    membership = build_theme_membership(ev_idx, verdicts)
    to_judge_themes: List[Dict[str, Any]] = []
    for tid in sorted(membership):
        grp = membership[tid]
        s_n, w_n, m_n = theme_member_counts(membership, tid)
        prev = trends.get(tid)
        if prev is None:
            why = "no_trend"
        elif (prev.get("evidence_strong_n"), prev.get("evidence_weak_n"),
              prev.get("evidence_member_n")) != (s_n, w_n, m_n):
            why = "stale_membership"
        else:
            continue
        item: Dict[str, Any] = {
            "theme_id": tid, "theme_name": themes.get(tid, {}).get("name", tid),
            "state": states.get(tid, {}).get("state"), "stars": states.get(tid, {}).get("stars"),
            "reason_to_judge": why,
            "strong": grp["strong"], "weak": grp["weak"],
            "quiet": [{"ts_code": q["ts_code"], "name": q["name"], "tier": q["tier"],
                       "np_single_yoy_pct": q["np_single_yoy_pct"]} for q in grp["quiet"]],
        }
        if prev is not None:
            item["prev_direction"] = prev.get("direction")
            item["prev_cross_validation"] = prev.get("cross_validation")
        to_judge_themes.append(item)

    meta = evidence.get("meta") or {}
    return {
        "period": period, "period_label": period_label(period),
        "generated_at": dt.datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
        "enum": {"tiers": TIERS, "quality_calls": QUALITY_CALLS, "fulfillments": FULFILLMENTS,
                 "match_confidence": CONFIDENCES, "trend_directions": TREND_DIRECTIONS},
        "disclosure": {
            "released_count": meta.get("released_count"),
            "with_statements": meta.get("with_statements"),
            "progress_pct": meta.get("disclosure_progress_pct"),
            "ann_cutoff": meta.get("ann_cutoff"),
            "shortlist_rule": meta.get("shortlist_rule"),
        },
        "already_judged": len(verdicts),
        "to_judge_count": len(to_judge),
        "theme_registry_empty": len(themes) == 0,
        "themes": theme_ref,
        "to_judge": to_judge,
        "to_judge_theme_count": len(to_judge_themes),
        "to_judge_themes": to_judge_themes,
        "notes": [
            "对每只 to_judge 股判 tier(强/中/观察/剔除)、quality_call(扎实/尚可/存疑/虚高)、"
            "fulfillment(超预告上限/落区间上沿/符合/落区间下沿/低于预告/无预告)与 theme_id(对不上填 null=无归属)。",
            "quality_call 是季报独有的判断：扣非占比、经营现金流对净利的覆盖、应收/存货是否跑赢营收——"
            "三项一致向好才叫『扎实』；净利高增但 OCF 为负或应收暴涨叫『存疑』甚至『虚高』。细则见 references/methodology.md §三。",
            "to_judge 里的 hits 是机械阈值命中(不是结论)，rank_score 只是把全市场收敛成候选池的排序，别当优秀度。",
            "数字之外的原因(分产品拆分、管理层解释、非经常损益构成)不在结构化数据里——需要时用 "
            "`report_pdf.py --code … --sections segment,mdna,nonrecurring` 按需取，不要凭数字编故事。",
            "主线匹配靠语义：用业务实质比对 themes 的 name/aliases/members_sample；弱匹配 match_confidence=low，对不上填 null。",
            "reason_to_judge=stale_restated 是公告日变了(追溯调整/更正报告)，stale_drift 是单季同比漂移超阈值，都要复判。",
            "行业趋势任务：对 to_judge_themes 每条主线写 theme_trends[]——分别读强表现(向上断层)与弱表现(向下断层)成员，"
            "归因每侧是行业级共性还是个体因素，交叉验证定 direction，附 strong_common/weak_common/cross_validation/confidence。",
            "本轮你新归入某主线的股票(即使不在 to_judge_themes 里)也要一并判/重判该主线趋势。",
            "theme_registry_empty=true 时主线台账为空(需先跑 daily-market-sense)，theme_id 一律 null、theme_trends 留空。",
        ],
    }


# --------------------------------------------------------------------------- #
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
        if not str(r.get("reason") or "").strip():
            errors.append(f"{ts_code}: reason 不能为空，跳过。")
            continue
        qc = r.get("quality_call")
        if qc not in QUALITY_CALLS:
            errors.append(f"{ts_code}: quality_call={qc!r} 非法(应为 {QUALITY_CALLS})，跳过。")
            continue
        ff = r.get("fulfillment")
        if ff not in FULFILLMENTS:
            errors.append(f"{ts_code}: fulfillment={ff!r} 非法(应为 {FULFILLMENTS})，跳过。")
            continue
        conf = r.get("match_confidence")
        if conf is not None and conf not in CONFIDENCES:
            errors.append(f"{ts_code}: match_confidence={conf!r} 非法(应为 {CONFIDENCES} 或 null)，跳过。")
            continue
        theme_id = r.get("theme_id")
        if theme_id and theme_id not in themes:
            errors.append(f"{ts_code}: theme_id={theme_id!r} 不在 theme_registry(合法: {sorted(themes)[:6]}…)，跳过。")
            continue
        if theme_id and (conf not in CONFIDENCES or not str(
                r.get("theme_rationale") or "").strip()):
            errors.append(
                f"{ts_code}: 有 theme_id 时必须填写合法 match_confidence 和 theme_rationale，跳过。")
            continue
        s = ev_idx[ts_code]
        g = s.get("growth", {})
        valid.append({
            "period": None,  # filled by caller
            "ts_code": ts_code, "tier": tier,
            "reason": r.get("reason"), "caveat": r.get("caveat"),
            "quality_call": qc, "fulfillment": ff,
            "theme_id": theme_id or None, "theme_rationale": r.get("theme_rationale"),
            "match_confidence": conf,
            "evidence_ann_date": str(s.get("ann_date") or ""),
            "evidence_np_single_yoy": (g.get("np") or {}).get("single_q_yoy_pct"),
            "evidence_rev_single_yoy": (g.get("revenue") or {}).get("single_q_yoy_pct"),
        })
    return valid, errors


def validate_trends(trends: List[Dict[str, Any]], themes: Dict[str, Dict[str, Any]],
                    membership: Dict[str, Dict[str, List[Dict[str, Any]]]],
                    ) -> Tuple[List[Dict[str, Any]], List[str]]:
    valid: List[Dict[str, Any]] = []
    errors: List[str] = []
    for t in trends:
        tid = str(t.get("theme_id") or "")
        if tid not in themes:
            errors.append(f"trend {tid or '(空)'}: theme_id 不在 theme_registry，跳过。")
            continue
        direction = t.get("direction")
        if direction not in TREND_DIRECTIONS:
            errors.append(f"trend {tid}: direction={direction!r} 非法(应为 {TREND_DIRECTIONS})，跳过。")
            continue
        conf = t.get("confidence")
        if conf is not None and conf not in CONFIDENCES:
            errors.append(f"trend {tid}: confidence={conf!r} 非法(应为 {CONFIDENCES} 或 null)，跳过。")
            continue
        s_n, w_n, m_n = theme_member_counts(membership, tid)
        if m_n == 0:
            errors.append(f"trend {tid}: 台账中该主线本期无归属成员，趋势无观察对象，跳过。")
            continue
        valid.append({
            "period": None,  # filled by caller
            "theme_id": tid, "direction": direction,
            "strong_common": t.get("strong_common"), "weak_common": t.get("weak_common"),
            "cross_validation": t.get("cross_validation"), "confidence": conf,
            "evidence_strong_n": s_n, "evidence_weak_n": w_n, "evidence_member_n": m_n,
        })
    return valid, errors


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="正式季报判分台账（分档 + 质量成色 + 兑现度 + 主线 + 行业趋势）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ctx = sub.add_parser("context", help="出待判集合 + 主线注册表 + 综述状态")
    ctx.add_argument("--period")
    ctx.add_argument("--evidence")
    ctx.add_argument("--drift", type=float, default=25.0,
                     help="单季同比漂移超过 N 个百分点判为待复判（默认 25）")
    ctx.add_argument("--out")
    ctx.add_argument("--stdout", action="store_true")

    rec = sub.add_parser("record", help="校验并落库模型的 verdict JSON")
    rec.add_argument("--period")
    rec.add_argument("--input", required=True)
    rec.add_argument("--evidence")

    args = ap.parse_args(argv)
    period = args.period or latest_quarter_end(
        dt.datetime.now(BEIJING_TZ).date())
    quarter_of(period)
    store = Store()

    if args.cmd == "context":
        ev_path = args.evidence or os.path.join("reports", f"qreport_scan_{period}.json")
        evidence = _load_json(ev_path)
        if evidence is None:
            print(json.dumps({"error": f"evidence 不存在: {ev_path}，请先运行 report_scan.py。"},
                             ensure_ascii=False))
            return 2
        if str((evidence.get("meta") or {}).get("period") or "") != period:
            print(json.dumps({"error": f"evidence 报告期与 --period={period} 不符。"},
                             ensure_ascii=False))
            return 2
        context = build_context(
            period, evidence, store.load_verdicts(period),
            store.load_theme_registry(), store.load_theme_latest_state(),
            store.load_theme_trends(period), args.drift,
        )
        out_path = args.out or os.path.join("reports", f"qreport_verdict_context_{period}.json")
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(context, fh, ensure_ascii=False, indent=2)
        summary = {"period": period, "to_judge": context["to_judge_count"],
                    "already_judged": context["already_judged"],
                    "themes": len(context["themes"]),
                    "to_judge_themes": context["to_judge_theme_count"],
                    "out": out_path}
        print(json.dumps(context if args.stdout else summary, ensure_ascii=False, indent=2))
        return 0

    payload = _load_json(args.input)
    if payload is None:
        print(json.dumps({"error": f"输入不存在: {args.input}"}, ensure_ascii=False))
        return 2
    payload_period = payload.get("period")
    if payload_period and args.period and payload_period != args.period:
        print(json.dumps({
            "error": f"payload period={payload_period} 与 --period={args.period} 不符。"
        }, ensure_ascii=False))
        return 2
    period = payload_period or period
    quarter_of(period)
    ev_path = args.evidence or os.path.join("reports", f"qreport_scan_{period}.json")
    evidence = _load_json(ev_path)
    if evidence is None:
        print(json.dumps({"error": f"evidence 不存在: {ev_path}，record 需要它快照证据指纹。"},
                         ensure_ascii=False))
        return 2
    if str((evidence.get("meta") or {}).get("period") or "") != period:
        print(json.dumps({"error": f"evidence 报告期与 record period={period} 不符。"},
                         ensure_ascii=False))
        return 2
    if not store.available:
        print(json.dumps({
            "error": f"台账存储不可用，拒绝伪报成功：{store.reason}"
        }, ensure_ascii=False))
        return 2
    ev_idx = _evidence_index(evidence)
    registry = store.load_theme_registry()
    valid, errors = validate_records(payload.get("records", []), ev_idx, registry)
    for r in valid:
        r["period"] = period
    verdict_ok = not valid or store.upsert_verdicts(valid)
    if not verdict_ok:
        errors.append("records: 数据库写入失败。")

    # Trends are validated against the post-upsert ledger so members attributed in
    # this same payload count into the snapshot.
    membership = build_theme_membership(ev_idx, store.load_verdicts(period))
    valid_trends, trend_errors = validate_trends(payload.get("theme_trends", []), registry, membership)
    for t in valid_trends:
        t["period"] = period
    trends_ok = not valid_trends or store.upsert_theme_trends(valid_trends)
    if not trends_ok:
        trend_errors.append("theme_trends: 数据库写入失败。")

    print(json.dumps({
        "period": period, "recorded": len(valid), "skipped": len(errors),
        "trends_recorded": len(valid_trends), "trends_skipped": len(trend_errors),
        "cache": "on" if store.available else f"off ({store.reason})",
        "errors": errors + trend_errors,
    }, ensure_ascii=False, indent=2))
    return 0 if verdict_ok and trends_ok else 2


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
