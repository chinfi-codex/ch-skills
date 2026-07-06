#!/usr/bin/env python3
"""Verdict ledger for the earnings-forecast skill: tier (强/中/观察/剔除) + 主线归属
+ 报告期行业趋势 (per-theme strong/weak attribution) + 产业结构综述 (period overview).

脑/手 boundary: this script is deterministic and calls no LLM. It only
  - `context`: assembles what the model must judge — the to-judge stock set
    (newly disclosed + stale-vs-current-evidence), the daily-market-sense
    theme registry (name/aliases/current lifecycle state) as the matching
    reference, per-theme member briefs split into 强表现 (up gap) / 弱表现
    (down gap) / 无断层 with each member's change_reason for trend attribution,
    plus the whole-sample industry_summary and the period-overview staleness
    state — into one JSON.
  - `record`: validates the model's verdict JSON (tier enum, theme_id exists in
    theme_registry; trend direction enum; overview is finished text), snapshots
    evidence_asof / member counts / sample fingerprint, and upserts into
    forecast_verdict / forecast_theme_trend / forecast_period_overview.

Which tier, which 主线, the industry trends and the 产业结构综述 are the model's
calls (see references/verdict_and_html.md; methodology.md §九/§十).

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
TREND_DIRECTIONS = ["向上", "向下", "分化", "证据不足"]
REASON_BRIEF = 200  # change_reason chars kept in per-theme member briefs
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


def build_theme_membership(ev_idx: Dict[str, Dict[str, Any]],
                           verdicts: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Per-theme member briefs from the verdict ledger joined to current evidence:
    强表现 (gap up) / 弱表现 (gap down) / quiet (no gap), each with the change_reason
    the model needs for attribution. Deterministic grouping only — what the trend
    means is judged by the model."""
    members: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for ts_code, v in verdicts.items():
        tid = v.get("theme_id")
        s = ev_idx.get(ts_code)
        if not tid or s is None:
            continue
        pr = s.get("price_reaction") or {}
        reason = str(s.get("change_reason") or "")
        brief = {
            "ts_code": ts_code,
            "name": s.get("name", ""),
            "tier": v.get("tier"),
            "gap_open_pct": pr.get("gap_open_pct"),
            "gap_status": pr.get("gap_status"),
            "since_ann_pct": pr.get("since_ann_pct"),
            "change_reason": reason[:REASON_BRIEF] + ("…" if len(reason) > REASON_BRIEF else ""),
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


def sample_fingerprint(evidence: Dict[str, Any]) -> Tuple[int, int, int]:
    """(total, positive_n, negative_n) — the whole-sample fingerprint that makes
    the 产业结构综述 stale when new disclosures shift the sample."""
    stocks = evidence.get("stocks", [])
    pos = sum(1 for s in stocks if (s.get("flags") or {}).get("positive_type"))
    neg = sum(1 for s in stocks if (s.get("flags") or {}).get("negative_type"))
    return len(stocks), pos, neg


# --------------------------------------------------------------------------- #
def build_context(period: str, evidence: Dict[str, Any], enrich: Optional[Dict[str, Any]],
                  verdicts: Dict[str, Dict[str, Any]], themes: Dict[str, Dict[str, Any]],
                  states: Dict[str, Dict[str, Any]], trends: Dict[str, Dict[str, Any]],
                  overview: Optional[Dict[str, Any]], drift: float) -> Dict[str, Any]:
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
                "gap_dir": pr.get("gap_dir"),
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

    # 报告期行业趋势：per-theme member briefs (强表现/弱表现 split + change_reason)
    # for themes whose trend is missing or whose membership moved since it was judged.
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
            continue  # trend judged and membership unchanged
        item: Dict[str, Any] = {
            "theme_id": tid,
            "theme_name": themes.get(tid, {}).get("name", tid),
            "state": states.get(tid, {}).get("state"),
            "stars": states.get(tid, {}).get("stars"),
            "reason_to_judge": why,
            "strong": grp["strong"],
            "weak": grp["weak"],
            "quiet": [{"ts_code": q["ts_code"], "name": q["name"], "tier": q["tier"]}
                      for q in grp["quiet"]],
        }
        if prev is not None:
            item["prev_direction"] = prev.get("direction")
            item["prev_cross_validation"] = prev.get("cross_validation")
        to_judge_themes.append(item)

    # 产业结构综述：whole-sample fingerprint decides whether the overview needs
    # (re)writing; industry_summary from the evidence pack is its raw material.
    total, pos_n, neg_n = sample_fingerprint(evidence)
    if overview is None:
        ov_why: Optional[str] = "no_overview"
    elif (overview.get("evidence_total"), overview.get("evidence_positive"),
          overview.get("evidence_negative")) != (total, pos_n, neg_n):
        ov_why = "stale_sample"
    else:
        ov_why = None
    period_overview = {
        "evidence_total": total,
        "evidence_positive": pos_n,
        "evidence_negative": neg_n,
        "reason_to_judge": ov_why,
        "prev": ({"overview": overview.get("overview"), "judged_at": overview.get("judged_at"),
                  "evidence_total": overview.get("evidence_total")} if overview else None),
    }

    return {
        "period": period,
        "period_label": period_label(period),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "enum": {"tiers": TIERS, "match_confidence": CONFIDENCES,
                 "trend_directions": TREND_DIRECTIONS},
        "already_judged": len(verdicts),
        "to_judge_count": len(to_judge),
        "theme_registry_empty": len(themes) == 0,
        "themes": theme_ref,
        "to_judge": to_judge,
        "to_judge_theme_count": len(to_judge_themes),
        "to_judge_themes": to_judge_themes,
        "period_overview": period_overview,
        "industry_summary": evidence.get("industry_summary", []),
        "notes": [
            "对每只 to_judge 股判 tier(强/中/观察/剔除)与 theme_id(归属主线，对不上填 null=无归属)。",
            "主线匹配靠语义:用 change_reason/行业 比对 themes 的 name/aliases/members_sample;弱匹配 match_confidence=low。",
            "price_reaction 是净利润断层证据：gap_open_pct=公告日跳空(公告日前一交易日收盘→公告日当天开盘)、pre_pos_1y_pct=公告前一年分位"
            "(高位=趋势加速型/低位=低位启动型)、gap_status=intact 未回补。断层且在主线内的胜率锚更高，"
            "回补(filled)或业绩强但股价长期无反应要在 caveat 里点明；判断细则见 references/methodology.md §八。",
            "reason_to_judge=stale_* 的是预告已修订或增速漂移，需复判；其余是新披露。",
            "行业趋势任务：对 to_judge_themes 每条主线写 theme_trends[]——分别读强表现(向上断层=业绩超预期)与弱表现(向下断层=不及预期)"
            "成员的 change_reason 做归因，判每侧是行业级共性还是个体因素，再交叉验证定 direction(向上/向下/分化/证据不足)，"
            "附 strong_common/weak_common/cross_validation/confidence；细则见 references/methodology.md §九。",
            "本轮你把新股归入的主线(即使不在 to_judge_themes 里)也要一并判/重判行业趋势——把新归属成员并入该主线现有成员一起归因。",
            "样本太少(≤2)或强弱两侧都是个体因素时判'证据不足'；强表现靠一次性损益的不算行业级共性；quiet(无断层)成员用来区分全面开花还是龙头独走。",
            "产业结构综述任务：period_overview.reason_to_judge=no_overview/stale_sample 时，据 industry_summary(全样本按行业的确定性计数，"
            "含负向预告)写 period_overview 综述——把行业按业务事实聚成宏观组(如高端制造科技/消费/地产链/周期资源，以样本实际为准)，"
            "正向扎堆组说清共性驱动，负向扎堆组读原因共性并找筑底证据(环比中位、减亏占比)，(a)(b) 式分条+前瞻判断+样本偏差 caveat；"
            "细则见 references/methodology.md §十。",
            "theme_registry_empty=true 时主线台账为空(需先跑 daily-market-sense)，theme_id 一律填 null、theme_trends 留空；产业结构综述不依赖主线台账，照常写。",
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


def validate_trends(trends: List[Dict[str, Any]], themes: Dict[str, Dict[str, Any]],
                    membership: Dict[str, Dict[str, List[Dict[str, Any]]]],
                    ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Enum/existence checks + deterministic member-count snapshot (post-upsert
    ledger membership) so `context` can detect stale trends when members move."""
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
            "period": t.get("period"),  # filled by caller
            "theme_id": tid,
            "direction": direction,
            "strong_common": t.get("strong_common"),
            "weak_common": t.get("weak_common"),
            "cross_validation": t.get("cross_validation"),
            "confidence": conf,
            "evidence_strong_n": s_n,
            "evidence_weak_n": w_n,
            "evidence_member_n": m_n,
        })
    return valid, errors


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Earnings-forecast verdict ledger (tier + 主线归属 + 行业趋势).")
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
            store.load_theme_registry(), store.load_theme_latest_state(),
            store.load_theme_trends(period), store.load_period_overview(period), args.drift,
        )
        out_path = args.out or os.path.join("reports", f"verdict_context_{period}.json")
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(context, fh, ensure_ascii=False, indent=2)
        summary = {"period": period, "to_judge": context["to_judge_count"],
                   "already_judged": context["already_judged"],
                   "themes": len(context["themes"]),
                   "to_judge_themes": context["to_judge_theme_count"],
                   "overview_to_judge": context["period_overview"]["reason_to_judge"],
                   "out": out_path}
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
    registry = store.load_theme_registry()
    valid, errors = validate_records(payload.get("records", []), ev_idx, registry)
    for r in valid:
        r["period"] = period
    store.upsert_verdicts(valid)

    # theme trends are validated against the post-upsert ledger, so members the
    # model just attributed in this same payload count into the snapshot.
    membership = build_theme_membership(ev_idx, store.load_verdicts(period))
    valid_trends, trend_errors = validate_trends(payload.get("theme_trends", []), registry, membership)
    for t in valid_trends:
        t["period"] = period
    store.upsert_theme_trends(valid_trends)

    # period overview (产业结构综述): one finished text per period, snapshotted
    # with the whole-sample fingerprint so context can flag it stale.
    overview_recorded = False
    ov_errors: List[str] = []
    ov = payload.get("period_overview")
    if ov is not None:
        if not isinstance(ov, str) or not ov.strip():
            ov_errors.append("period_overview: 需为非空字符串(综述成文)，跳过。")
        else:
            total, pos_n, neg_n = sample_fingerprint(evidence)
            store.upsert_period_overview({
                "period": period, "overview": ov.strip(),
                "evidence_total": total, "evidence_positive": pos_n,
                "evidence_negative": neg_n,
            })
            overview_recorded = True

    print(json.dumps({
        "period": period, "recorded": len(valid), "skipped": len(errors),
        "trends_recorded": len(valid_trends), "trends_skipped": len(trend_errors),
        "overview_recorded": overview_recorded,
        "cache": "on" if store.available else f"off ({store.reason})",
        "errors": errors + trend_errors + ov_errors,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
