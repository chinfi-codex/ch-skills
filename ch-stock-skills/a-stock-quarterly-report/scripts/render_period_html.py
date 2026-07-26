#!/usr/bin/env python3
"""Render one report period into a single self-contained HTML page.

Structure mirrors the earnings-forecast page (产业结构综述 → filters → left list /
right detail, with K-lines split into lazily loaded shards) because the reading
motion is the same. The content is not: a statutory report can show the quality
triad a forecast cannot — how much profit survives 扣非, how much of it arrived
as operating cash, and whether receivables and inventory outran revenue — so the
detail pane leads with that, then 兑现度 against the company's own guidance,
then the balance-sheet forward signals.

Everything shown is either a deterministic number from the evidence pack or a
model judgement read out of the verdict ledger. The renderer never decides
anything; where a judgement is missing it says so.

    python3 scripts/render_period_html.py --period 20260331
    python3 scripts/render_period_html.py --period 20260331 --require-ann-cutoff 20260501
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from store import Store  # noqa: E402

BEIJING_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
KLINE_BARS = 130
KLINE_SHARDS = 64
TREND_NET = 2
POS_SPLIT = 50.0
MUTED_MAX_PCT = 3.0
DRIFT_PCT = 25.0
NEW_DAYS = 5
_HOT_STATES = ("在场候选", "在场", "主升", "扩散")
_MMDD_Q = {"0331": 1, "0630": 2, "0930": 3, "1231": 4}
PE_BUCKETS = [("lt15", "<15×", None, 15.0), ("15_25", "15–25×", 15.0, 25.0),
              ("25_40", "25–40×", 25.0, 40.0), ("40_60", "40–60×", 40.0, 60.0),
              ("ge60", "≥60×", 60.0, None), ("na", "无PE(亏损/缺市值)", None, None)]


def beijing_today() -> dt.date:
    return dt.datetime.now(BEIJING_TZ).date()


def quarter_of(period: str) -> int:
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


def _pe_bucket(pe: Optional[float]) -> str:
    if pe is None:
        return "na"
    for key, _, lo, hi in PE_BUCKETS:
        if key == "na":
            continue
        if (lo is None or pe >= lo) and (hi is None or pe < hi):
            return key
    return "na"


def pe_bucket_options() -> List[Dict[str, str]]:
    return [{"v": k, "t": t} for k, t, _, _ in PE_BUCKETS]


def _validate_evidence_cutoff(evidence: Dict[str, Any], required: Optional[str] = None) -> Dict[str, Any]:
    """Publish gate: the page must be able to state, verifiably, how far the
    disclosure scan got. A stale evidence file published as today's page is the
    one failure mode that silently misleads."""
    meta = evidence.get("meta") or {}
    cutoff = str(meta.get("ann_cutoff") or "")
    tz = str(meta.get("clock_timezone") or "")
    count = meta.get("ann_cutoff_stock_count")
    if len(cutoff) != 8 or not cutoff.isdigit():
        raise ValueError(f"evidence 缺少合法 meta.ann_cutoff（得到 {cutoff!r}），请重跑 report_scan.py。")
    if tz != "Asia/Shanghai":
        raise ValueError(f"evidence meta.clock_timezone={tz!r}，期望 Asia/Shanghai。")
    if not isinstance(count, int):
        raise TypeError(f"evidence meta.ann_cutoff_stock_count 非整数（得到 {count!r}）。")
    if required and required != cutoff:
        raise ValueError(f"公告截止日门禁不符：evidence={cutoff}，要求={required}。")
    return {
        "ann_cutoff": cutoff, "ann_cutoff_stock_count": count,
        "evidence_generated_at": str(meta.get("generated_at") or ""),
        "released_count": meta.get("released_count"),
        "with_statements": meta.get("with_statements"),
        "disclosure_progress_pct": meta.get("disclosure_progress_pct"),
        "shortlist_rule": meta.get("shortlist_rule"),
    }


def _days_between(ymd: str, today: dt.date) -> Optional[int]:
    if not ymd or len(ymd) != 8:
        return None
    try:
        return (today - dt.datetime.strptime(ymd, "%Y%m%d").date()).days
    except ValueError:
        return None


def _compact_kline(bars: List[Dict[str, Any]], keep: int) -> List[List[Any]]:
    rows = []
    for b in bars[-keep:]:
        if b.get("close") is None:
            continue
        rows.append([str(b["trade_date"]), b.get("open"), b.get("high"),
                     b.get("low"), b.get("close"), b.get("vol")])
    return rows


def _kline_shard_id(ts_code: str, shard_count: int = KLINE_SHARDS) -> str:
    """Stable FNV-1a shard id. Not Python's ``hash`` — its salt changes per
    process, which would scatter a code across shards between renders."""
    if shard_count <= 0:
        raise ValueError("kline shard_count 必须为正整数。")
    h = 2166136261
    for b in ts_code.encode("utf-8"):
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    width = max(2, len(str(shard_count - 1)))
    return f"{h % shard_count:0{width}d}"


def write_kline_assets(out_path: str, klines: Dict[str, List[List[Any]]],
                       shard_count: int = KLINE_SHARDS,
                       generated_at: Optional[str] = None) -> Dict[str, Any]:
    """Write K-line JSON shards next to the HTML so the first paint is not
    blocked by hundreds of price series."""
    html_path = Path(out_path)
    asset_dir = html_path.with_suffix("").with_name(html_path.stem + ".klines")
    asset_dir.mkdir(parents=True, exist_ok=True)

    shard_payloads: Dict[str, Dict[str, List[List[Any]]]] = {}
    code_to_shard: Dict[str, str] = {}
    for ts_code in sorted(klines):
        shard_id = _kline_shard_id(ts_code, shard_count)
        code_to_shard[ts_code] = shard_id
        shard_payloads.setdefault(shard_id, {})[ts_code] = klines[ts_code]

    written = set()
    for shard_id, payload in shard_payloads.items():
        name = f"{shard_id}.json"
        written.add(name)
        (asset_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    written.add("_manifest.json")
    (asset_dir / "_manifest.json").write_text(json.dumps({
        "schema_version": "qreport-kline-shards/v1", "generated_at": generated_at,
        "shard_count": shard_count, "nonempty_shards": len(shard_payloads),
        "stock_count": len(code_to_shard),
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    for old in asset_dir.glob("*.json"):
        if old.name not in written:
            old.unlink()

    return {"asset_dir": str(asset_dir), "asset_base": asset_dir.name,
            "code_to_shard": code_to_shard, "nonempty_shards": len(shard_payloads)}


# --------------------------------------------------------------------------- #
def build_view(period: str, evidence: Dict[str, Any], verdicts: Dict[str, Dict[str, Any]],
               themes: Dict[str, Dict[str, Any]], states: Dict[str, Dict[str, Any]],
               trends: Dict[str, Dict[str, Any]], overview_row: Optional[Dict[str, Any]],
               bars_map: Dict[str, List[Dict[str, Any]]], today: dt.date,
               pos_split: float = POS_SPLIT, muted_max: float = MUTED_MAX_PCT,
               kline_bars: int = KLINE_BARS) -> Dict[str, Any]:
    stocks: List[Dict[str, Any]] = []
    klines: Dict[str, List[List[Any]]] = {}

    for s in evidence.get("stocks", []):
        ts_code = str(s["ts_code"])
        g = s.get("growth", {})
        rev, npf, dedt, ocf = (g.get("revenue") or {}, g.get("np") or {},
                               g.get("dedt") or {}, g.get("ocf") or {})
        m = s.get("margins") or {}
        q = s.get("quality") or {}
        b = s.get("balance_signals") or {}
        val = s.get("valuation") or {}
        pr = s.get("price_reaction") or {}
        sc = s.get("screen") or {}
        ff = (s.get("fulfillment") or {}).get("forecast") or {}
        src = s.get("source") or {}
        v = verdicts.get(ts_code)
        ann = str(s.get("ann_date") or "")

        badges = []
        d_new = _days_between(ann, today)
        if d_new is not None and 0 <= d_new <= NEW_DAYS:
            badges.append("NEW")
        if src.get("cninfo_is_corrected"):
            badges.append("更正后")

        theme_id = (v or {}).get("theme_id")
        theme_name = theme_state = theme_stars = None
        theme_hot = False
        if theme_id and theme_id in themes:
            theme_name = themes[theme_id].get("name", theme_id)
            st = states.get(theme_id, {})
            theme_state, theme_stars = st.get("state"), st.get("stars")
            theme_hot = theme_state in _HOT_STATES

        stale = False
        if v is not None:
            pann = str(v.get("evidence_ann_date") or "")
            pnp = v.get("evidence_np_single_yoy")
            cur_np = npf.get("single_q_yoy_pct")
            if (ann and pann and ann != pann) or (
                    pnp is not None and cur_np is not None and abs(float(cur_np) - float(pnp)) > DRIFT_PCT):
                stale = True
                badges.append("待复判")

        pe_headline = val.get("pe_ttm_dedt") if val.get("pe_ttm_dedt") is not None else val.get("pe_ttm_np")
        gap_dir, gap_status = pr.get("gap_dir"), pr.get("gap_status")
        since = pr.get("since_ann_pct")

        rec = {
            "ts_code": ts_code, "name": s.get("name", ""), "industry": s.get("industry", ""),
            "ann_date": ann,
            "rev_cum_yoy": rev.get("cum_yoy_pct"), "rev_sq_yoy": rev.get("single_q_yoy_pct"),
            "rev_qoq": rev.get("qoq_pct"), "rev_cum_yi": rev.get("cum_yi"),
            "np_cum_yoy": npf.get("cum_yoy_pct"), "np_sq_yoy": npf.get("single_q_yoy_pct"),
            "np_qoq": npf.get("qoq_pct"), "np_cum_yi": npf.get("cum_yi"),
            "np_note": npf.get("single_q_note"),
            "dedt_cum_yoy": dedt.get("cum_yoy_pct"), "dedt_sq_yoy": dedt.get("single_q_yoy_pct"),
            "dedt_qoq": dedt.get("qoq_pct"), "dedt_cum_yi": q.get("dedt_cum_yi"),
            "ocf_cum_yi": q.get("ocf_cum_yi"), "ocf_yoy": ocf.get("cum_yoy_pct"),
            "dedt_ratio": q.get("dedt_ratio_pct"), "non_recurring_yi": q.get("non_recurring_yi"),
            "ocf_to_np": q.get("ocf_to_np_pct"),
            "cash_sales_ratio": q.get("cash_sales_to_revenue_pct"),
            "roe_ann": q.get("roe_annualized_pct"), "roe_cum": q.get("roe_cum_pct"),
            "gm_sq": m.get("gross_margin_single_pct"),
            "gm_sq_yoy_pp": m.get("gross_margin_single_yoy_pp"),
            "gm_sq_qoq_pp": m.get("gross_margin_single_qoq_pp"),
            "nm_sq": m.get("net_margin_single_pct"),
            "exp_ratio": m.get("expense_ratio") or {},
            "rd_yi": m.get("rd_exp_yi"), "rd_yoy": m.get("rd_yoy_pct"),
            "contract_liab": b.get("contract_liab") or {}, "inventories": b.get("inventories") or {},
            "receivables": b.get("receivables") or {}, "cip": b.get("cip") or {},
            "ar_gap_pp": b.get("receivable_vs_revenue_gap_pp"),
            "inv_gap_pp": b.get("inventory_vs_revenue_gap_pp"),
            "debt_ratio": b.get("debt_ratio_pct"), "goodwill_eq_pct": b.get("goodwill_to_equity_pct"),
            "total_mv_yi": val.get("total_mv_yi"), "mv_asof": val.get("mv_asof"),
            "pe_ttm_dedt": val.get("pe_ttm_dedt"), "pe_ttm_np": val.get("pe_ttm_np"),
            "pe_ann_dedt": val.get("pe_annualized_dedt"), "pe_ann_np": val.get("pe_annualized_np"),
            "pe_ttm_market": val.get("pe_ttm_market"),
            "pe_ttm_dedt_note": val.get("pe_ttm_dedt_note"),
            "dedt_ttm_yi": val.get("dedt_ttm_yi"), "np_ttm_yi": val.get("np_ttm_yi"),
            "pe_headline": pe_headline, "pe_bucket": _pe_bucket(pe_headline),
            "ff_in_range": ff.get("in_range"), "ff_vs_median": ff.get("vs_median_pct"),
            "ff_position": ff.get("range_position"), "ff_min": ff.get("np_min_yi"),
            "ff_max": ff.get("np_max_yi"), "ff_type": ff.get("type"),
            "gap_open_pct": pr.get("gap_open_pct"), "gap_dir": gap_dir, "gap_status": gap_status,
            "since_ann_pct": since, "pre_pos": pr.get("pre_pos_pct"),
            "pre_pos_bars": pr.get("pre_pos_bars"), "pre_close": pr.get("pre_close"),
            "reaction_date": pr.get("reaction_date"),
            "days_since_r": pr.get("trading_days_since_r"),
            "hits": sc.get("hits", []), "rank_score": sc.get("rank_score"),
            "tier": (v or {}).get("tier"), "quality_call": (v or {}).get("quality_call"),
            "fulfillment_call": (v or {}).get("fulfillment"),
            "reason": (v or {}).get("reason"), "caveat": (v or {}).get("caveat"),
            "theme_id": theme_id, "theme_name": theme_name, "theme_state": theme_state,
            "theme_stars": theme_stars, "theme_hot": theme_hot,
            "match_confidence": (v or {}).get("match_confidence"),
            "theme_rationale": (v or {}).get("theme_rationale"),
            "badges": badges, "stale": stale,
            "cninfo_title": src.get("cninfo_title"), "cninfo_url": src.get("cninfo_url"),
            "sources": src.get("sources") or [],
        }
        if gap_dir == "up":
            pos = rec["pre_pos"]
            rec["facet"] = "gap_unpos" if pos is None else ("gap_trend" if pos >= pos_split else "gap_low")
        elif gap_dir == "down":
            rec["facet"] = "gap_down"
        elif rec["tier"] in ("强", "中") and gap_status == "none" and since is not None and abs(since) < muted_max:
            rec["facet"] = "muted"
        else:
            rec["facet"] = "other"
        stocks.append(rec)
        kl = _compact_kline(bars_map.get(ts_code, []), kline_bars)
        if kl:
            klines[ts_code] = kl

    # --- theme aggregation: mechanical gap counts + the model's judged trend ---
    theme_trends: Dict[str, Dict[str, Any]] = {}
    for st in stocks:
        tid = st["theme_id"]
        if not tid:
            continue
        tr = theme_trends.setdefault(tid, {
            "theme_id": tid, "theme_name": st["theme_name"], "state": st["theme_state"],
            "stars": st["theme_stars"], "hot": st["theme_hot"],
            "strong": [], "weak": [], "neutral_n": 0})
        brief = {"ts_code": st["ts_code"], "name": st["name"],
                 "gap_open_pct": st["gap_open_pct"], "gap_status": st["gap_status"]}
        if st["gap_dir"] == "up":
            tr["strong"].append(brief)
        elif st["gap_dir"] == "down":
            tr["weak"].append(brief)
        else:
            tr["neutral_n"] += 1
    for tid, tr in theme_trends.items():
        s_n, w_n = len(tr["strong"]), len(tr["weak"])
        tr["net"] = s_n - w_n
        if tr["net"] >= TREND_NET:
            tr["trend"] = "偏强"
        elif tr["net"] <= -TREND_NET:
            tr["trend"] = "偏弱"
        elif s_n > 0 and w_n > 0:
            tr["trend"] = "分歧"
        else:
            tr["trend"] = "中性"
        row = trends.get(tid)
        if row:
            snap = (row.get("evidence_strong_n"), row.get("evidence_weak_n"),
                    row.get("evidence_member_n"))
            tr["judged"] = {
                "direction": row.get("direction"), "strong_common": row.get("strong_common"),
                "weak_common": row.get("weak_common"), "cross_validation": row.get("cross_validation"),
                "confidence": row.get("confidence"), "judged_at": str(row.get("judged_at") or "")[:10],
                "snap_strong": snap[0], "snap_weak": snap[1],
                "stale": snap != (s_n, w_n, s_n + w_n + tr["neutral_n"]),
            }

    facet_rank = {"gap_trend": 0, "gap_low": 1, "gap_unpos": 2, "gap_down": 3, "muted": 4, "other": 5}
    stocks.sort(key=lambda s: (facet_rank[s["facet"]], not s["theme_hot"],
                               -(s["rank_score"] or -99), -(s["np_sq_yoy"] or -1e9)))

    overview = None
    if overview_row:
        rows = evidence.get("industry_summary", [])
        cur = (int((evidence.get("meta") or {}).get("with_statements") or 0),
               sum(int(r.get("growth_n") or 0) for r in rows),
               sum(int(r.get("decline_n") or 0) for r in rows))
        snap = (overview_row.get("evidence_total"), overview_row.get("evidence_growth"),
                overview_row.get("evidence_decline"))
        overview = {"text": overview_row.get("overview") or "",
                    "judged_at": str(overview_row.get("judged_at") or "")[:10],
                    "snap_total": snap[0], "stale": snap != cur}

    industries = sorted({s["industry"] for s in stocks if s["industry"]})
    cutoff_info = _validate_evidence_cutoff(evidence)
    return {
        "period": period, "period_label": period_label(period),
        "updated_at": today.strftime("%Y-%m-%d"),
        **cutoff_info,
        "theme_registry_empty": len(themes) == 0,
        "pos_split": pos_split, "trend_net": TREND_NET,
        "pe_buckets": pe_bucket_options(), "industries": industries,
        "stocks": stocks, "theme_trends": theme_trends, "overview": overview, "klines": klines,
    }


# --------------------------------------------------------------------------- #
# 配色口径（A 股习惯）：红=涨/强/好，绿=跌/弱/差。shared default 的 --neg=红、
# --pos=绿，所以这里用 --up/--down 做领域别名，避免语义打架。
_CSS = """
:root{--s0:var(--bg);--s1:var(--surface-2);--s2:var(--surface);--tx:var(--ink-1);--tx2:var(--ink-3);
--tx3:var(--ink-4);--bd:var(--line-2);--acc:var(--accent);--accbg:var(--accent-soft);
--up:var(--neg);--down:var(--pos);--upbg:var(--neg-soft);--dnbg:var(--pos-soft);
--amb:var(--warn);--ambbg:var(--warn-soft);--gry:var(--muted);--grybg:var(--surface-3);
--warnc:var(--g-red);--warnbg:var(--g-red-soft);--kup:var(--up);--kdn:var(--down)}
.page{width:min(1680px,calc(100vw - 24px))}.report.q-report{max-width:none;padding:28px 20px 34px}
.wrap{max-width:none;margin:0 auto;padding:0}
h1{font-size:24px;font-weight:500;margin:0}.sub{color:var(--tx2);font-size:14px;margin-top:3px}
.ovw{background:var(--accbg);border:1px solid var(--accent-hair);border-left:4px solid var(--acc);
border-radius:var(--r-md);padding:15px 18px;margin-top:18px;font-size:14px;line-height:1.75}
.ovw .ttl{font-size:13px;color:var(--tx3);font-weight:500;margin-bottom:5px;display:flex;gap:8px;
align-items:center;flex-wrap:wrap}.ovw .ovtx{white-space:pre-line;line-height:1.75}
.prog{display:flex;align-items:center;gap:10px;margin-top:14px;font-size:13px;color:var(--tx2)}
.prog .bar{flex:1;max-width:320px;height:7px;border-radius:20px;background:var(--grybg);overflow:hidden}
.prog .bar i{display:block;height:100%;background:var(--acc)}
.ctrl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:16px 0 12px}
input,select{font:inherit;font-size:14px;padding:9px 11px;border:1px solid var(--line-1);
border-radius:var(--r-sm);background:var(--s2);color:var(--tx);outline:none}
input:focus,select:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--accbg)}
.msbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.ms{position:relative}
.ms-btn{font:inherit;font-size:14px;padding:9px 11px;border:1px solid var(--line-1);
border-radius:var(--r-sm);background:var(--s2);color:var(--tx);cursor:pointer;display:flex;
align-items:center;gap:6px;white-space:nowrap}.ms-btn:hover{border-color:var(--acc)}
.ms.on .ms-btn{border-color:var(--acc);background:var(--accbg);color:var(--acc)}
.ms-sum{color:var(--tx3);font-weight:500}.ms.on .ms-sum{color:var(--acc)}.ms-caret{color:var(--tx3);font-size:10px}
.ms-pop{position:absolute;z-index:30;top:calc(100% + 4px);left:0;min-width:190px;max-height:340px;
overflow:auto;background:var(--s2);border:1px solid var(--line-1);border-radius:var(--r-sm);
box-shadow:var(--shadow-2);padding:6px}.ms-pop.rt{left:auto;right:0}
.ms-search{width:100%;box-sizing:border-box;margin-bottom:5px;padding:7px 9px;font-size:13px}
.ms-pop label{display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:6px;
font-size:13.5px;cursor:pointer;white-space:nowrap}.ms-pop label:hover{background:var(--s1)}
.ms-pop label input{flex:none;margin:0;accent-color:var(--acc)}
.ms-pop label .cnt{margin-left:auto;color:var(--tx3);font-size:12px;font-family:'Roboto Mono',monospace}
.ms-empty{color:var(--tx3);font-size:12px;padding:6px 8px}
.fbar-x{font:inherit;font-size:13px;padding:9px 11px;border:1px solid var(--line-1);
border-radius:var(--r-sm);background:transparent;color:var(--tx2);cursor:pointer}
.fbar-x:hover{border-color:var(--acc);color:var(--acc)}.fbar-x[hidden]{display:none}
.fcount{font-size:13px;color:var(--tx2);margin-left:auto;white-space:nowrap}.fcount b{color:var(--tx);font-weight:600}
.panes{display:grid;grid-template-columns:minmax(340px,44%) 1fr;gap:14px;align-items:start}
@media (max-width:860px){.panes{grid-template-columns:1fr}
.detail{position:static!important;max-height:none;overflow:visible;overscroll-behavior:auto}}
.list{min-width:0}
.ghead{font-size:13px;color:var(--tx2);font-weight:500;background:var(--s1);border-radius:8px;
padding:7px 10px;margin:12px 0 6px;display:flex;justify-content:space-between;gap:8px;
align-items:center;flex-wrap:wrap}.ghead:first-child{margin-top:0}
.trend{font-size:11px;padding:1px 8px;border-radius:20px;font-weight:400}
.t-up{background:var(--upbg);color:var(--up)}.t-dn{background:var(--dnbg);color:var(--down)}
.t-mix{background:var(--ambbg);color:var(--amb)}.t-flat{background:var(--grybg);color:var(--tx2)}
.row{background:var(--s2);border:1px solid var(--bd);border-radius:var(--r-md);padding:9px 12px;
margin-bottom:7px;cursor:pointer;transition:border-color .14s ease,box-shadow .14s ease,transform .14s ease}
.row:hover{border-color:var(--line-1);box-shadow:var(--shadow-1);transform:translateY(-1px)}
.row.sel{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc) inset}
.row.gapup{background:var(--upbg)}.row.gapdn{background:var(--dnbg)}
.loadmore{display:block;width:100%;margin:10px 0 4px;padding:10px 12px;border:1px solid var(--bd);
border-radius:var(--r-md);background:var(--s1);color:var(--tx2);cursor:pointer;font:inherit}
.loadmore:hover{border-color:var(--acc);color:var(--tx)}
.r1{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.r1 .nm{font-weight:500}.r1 .cd{color:var(--tx3);font-size:13px}
.r2{font-size:13px;color:var(--tx2);margin-top:3px}
.pill{font-size:12px;padding:2px 8px;border-radius:20px;white-space:nowrap}
.strong{background:var(--upbg);color:var(--up)}.mid{background:var(--ambbg);color:var(--amb)}
.watch{background:var(--grybg);color:var(--gry)}.drop{background:var(--dnbg);color:var(--down)}
.thot{background:var(--accbg);color:var(--acc)}.tcool{background:var(--grybg);color:var(--tx2)}
.badge{font-size:11px;padding:2px 7px;border-radius:20px;margin-left:2px}
.bnew{background:var(--accbg);color:var(--acc)}.bupd{background:var(--ambbg);color:var(--amb)}
.bstale{background:var(--warnbg);color:var(--warnc)}
.gap-up{background:var(--upbg);color:var(--up);font-weight:500}
.gap-dn{background:var(--dnbg);color:var(--down);font-weight:500}
.gap-fade{background:var(--grybg);color:var(--tx2)}.instar{background:var(--accbg);color:var(--acc)}
.q-ok{background:var(--upbg);color:var(--up)}.q-mid{background:var(--ambbg);color:var(--amb)}
.q-bad{background:var(--warnbg);color:var(--warnc)}
.pos{color:var(--up)}.neg{color:var(--down)}.mut{color:var(--tx2)}.acc{color:var(--acc)}
.detail{position:sticky;top:14px;box-sizing:border-box;max-height:calc(100vh - 28px);overflow-y:auto;
overscroll-behavior:contain;scrollbar-gutter:stable;background:var(--s2);border:1px solid var(--bd);
border-radius:var(--r-md);box-shadow:var(--shadow-1);padding:16px 18px;min-height:420px}
.dh{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.dh .nm{font-size:19px;font-weight:500}
.vhero{display:flex;gap:16px;align-items:center;flex-wrap:wrap;background:var(--accbg);
border:1px solid var(--accent-hair);border-left:4px solid var(--acc);border-radius:var(--r-md);
padding:10px 14px;margin-top:12px}
.vhero .vpe{display:flex;align-items:baseline;gap:9px;white-space:nowrap}
.vhero .vk{font-size:12.5px;color:var(--tx2);font-weight:500}
.vhero .vnum{font-size:27px;font-weight:600;line-height:1;color:var(--acc);font-family:'Roboto Mono',monospace}
.vhero .vnum .vx{font-size:14px;font-weight:500}
.vhero .vsub{font-size:12.5px;color:var(--tx2);line-height:1.65;min-width:200px;flex:1}
.triad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}
.tcard{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r-sm);padding:8px 10px}
.tcard .tk{font-size:11.5px;color:var(--tx3)}
.tcard .tv{font-size:19px;font-weight:600;font-family:'Roboto Mono',monospace;line-height:1.35}
.tcard .tn{font-size:11.5px;color:var(--tx2);line-height:1.5}
.dsec{font-size:13px;color:var(--tx3);margin:14px 0 6px;font-weight:500}
.gtab{width:100%;border-collapse:collapse;font-size:13.5px}
.gtab th,.gtab td{padding:5px 6px;border-bottom:1px dashed var(--bd);text-align:right;
font-family:'Roboto Mono',monospace}
.gtab th{color:var(--tx3);font-weight:500;font-family:inherit;font-size:12.5px}
.gtab td:first-child,.gtab th:first-child{text-align:left;font-family:inherit;color:var(--tx2)}
.mgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}
.mrow{display:flex;justify-content:space-between;font-size:14px;padding:4px 0;border-bottom:1px dashed var(--bd)}
.mrow .k{color:var(--tx2)}
.ffbar{position:relative;height:26px;background:var(--grybg);border-radius:var(--r-sm);margin:6px 0 4px}
.ffbar .band{position:absolute;top:0;bottom:0;background:var(--accbg);border-left:1px solid var(--acc);
border-right:1px solid var(--acc)}
.ffbar .mark{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--up)}
.ffbar .lb{position:absolute;font-size:10.5px;color:var(--tx3);top:7px}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.chip{font-size:11px;padding:2px 7px;border-radius:6px;background:var(--s1);color:var(--tx2);
border:1px solid var(--bd);font-family:'Roboto Mono',monospace}
.chip.bad{background:var(--warnbg);color:var(--warnc);border-color:transparent}
.chip.good{background:var(--upbg);color:var(--up);border-color:transparent}
.dtext{font-size:13.5px;color:var(--tx2);background:var(--s1);border-radius:8px;padding:9px 10px;line-height:1.75}
.tline{font-size:13.5px;padding:3px 0}
.foot{color:var(--tx3);font-size:12px;margin-top:14px;line-height:1.75}
.empty{color:var(--tx3);font-size:13px;padding:6px 0}
a.src{color:var(--acc);text-decoration:none}a.src:hover{text-decoration:underline}
"""

_JS = r"""
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=v=>(v===null||v===undefined)?'—':((v>=0?'+':'')+v+'%');
const sign=v=>(v===null||v===undefined)?'—':((v>=0?'+':'')+v);
const num=v=>(v===null||v===undefined)?'—':v;
const fmtYi=v=>(v===null||v===undefined)?'—':(Math.abs(v)>=100?v.toFixed(0):(Math.abs(v)>=10?v.toFixed(1):v.toFixed(2)));
const cls=v=>(v===null||v===undefined)?'mut':(v>=0?'pos':'neg');
const tierPill={'强':'strong','中':'mid','观察':'watch','剔除':'drop'};
const qualPill={'扎实':'q-ok','尚可':'q-mid','存疑':'q-bad','虚高':'q-bad'};
function pill(c,t){return `<span class="pill ${c}">${esc(t)}</span>`;}
function gapBadge(s){
  if(s.gap_dir==='up'){const st=s.gap_status==='intact'?'未回补 D+'+(s.days_since_r||0):'已回补';
    return `<span class="badge ${s.gap_status==='intact'?'gap-up':'gap-fade'}">⬆断层${sign(s.gap_open_pct)}% ${st}</span>`;}
  if(s.gap_dir==='down'){const st=s.gap_status==='intact'?'未回补 D+'+(s.days_since_r||0):'已回补';
    return `<span class="badge ${s.gap_status==='intact'?'gap-dn':'gap-fade'}">⬇断层${sign(s.gap_open_pct)}% ${st}</span>`;}
  return '';
}
function badges(s){return s.badges.map(b=>{const c=b==='NEW'?'bnew':(b==='更正后'?'bupd':'bstale');
  return `<span class="badge ${c}">${esc(b)}</span>`;}).join('');}
function ffBadge(s){
  if(!s.ff_in_range)return'';
  const map={above:['gap-up','超预告上限'],within:['gap-fade','落预告区间'],below:['gap-dn','低于预告']};
  const [c,t]=map[s.ff_in_range]||['gap-fade',s.ff_in_range];
  return `<span class="badge ${c}">${t}${s.ff_vs_median!==null&&s.ff_vs_median!==undefined?' '+sign(s.ff_vs_median)+'%':''}</span>`;
}
function rowLine2(s){
  const p=['营收单季'+pct(s.rev_sq_yoy),'归母单季'+pct(s.np_sq_yoy),'扣非'+pct(s.dedt_sq_yoy)];
  if(s.ocf_to_np!==null&&s.ocf_to_np!==undefined)p.push('现金覆盖'+s.ocf_to_np+'%');
  if(s.theme_name)p.push(String(s.theme_name).split('/')[0].trim());
  if(s.ann_date)p.push('披露'+s.ann_date.slice(4,6)+'-'+s.ann_date.slice(6));
  return p.join(' · ');
}
function stockRow(s){
  const t=s.tier?pill(tierPill[s.tier],s.tier):'';
  const qc=s.quality_call?pill(qualPill[s.quality_call]||'watch',s.quality_call):'';
  const star=s.theme_hot?'<span class="badge instar">主线内</span>':'';
  const c=s.gap_dir==='up'?' gapup':(s.gap_dir==='down'?' gapdn':'');
  return `<div class="row${c}${SEL===s.ts_code?' sel':''}" data-c="${s.ts_code}">
    <div class="r1"><span class="nm">${esc(s.name)}</span><span class="cd">${s.ts_code.slice(0,6)}</span>${t}${qc}${gapBadge(s)}${ffBadge(s)}${star}${badges(s)}</div>
    <div class="r2">${rowLine2(s)}</div></div>`;
}
function trendPill(tr){
  const c=tr.trend==='偏强'?'t-up':(tr.trend==='偏弱'?'t-dn':(tr.trend==='分歧'?'t-mix':'t-flat'));
  const a=tr.trend==='偏强'?'↑':(tr.trend==='偏弱'?'↓':(tr.trend==='分歧'?'⇅':'·'));
  return `<span class="trend ${c}">${a}${tr.trend} 强${tr.strong.length}/弱${tr.weak.length}</span>`;
}
function judgedPill(tr){
  if(!tr||!tr.judged)return'';const j=tr.judged;
  const c=j.direction==='向上'?'t-up':(j.direction==='向下'?'t-dn':(j.direction==='分化'?'t-mix':'t-flat'));
  return `<span class="trend ${c}" title="${esc(j.cross_validation||'')}">判·${esc(j.direction)}${j.stale?'†':''}</span>`;
}
const REACT_PRED={up:s=>s.gap_dir==='up',down:s=>s.gap_dir==='down',
  intact:s=>s.gap_dir==='up'&&s.gap_status==='intact',muted:s=>s.facet==='muted',
  intheme:s=>Boolean(s.theme_hot)};
const HIT_OPTS=[
  {v:'np_accelerating',t:'归母单季加速'},{v:'rev_accelerating',t:'营收单季加速'},
  {v:'dedt_clean',t:'扣非占比高'},{v:'ocf_backed',t:'现金流覆盖'},
  {v:'ocf_negative_while_profitable',t:'盈利但经营现金为负'},
  {v:'margin_expanding',t:'毛利率扩张'},{v:'margin_compressing',t:'毛利率收缩'},
  {v:'orderbook_building',t:'合同负债在增'},{v:'capex_cycle',t:'在建工程扩张'},
  {v:'receivable_outpacing_revenue',t:'应收跑赢营收'},{v:'inventory_outpacing_revenue',t:'存货跑赢营收'},
  {v:'roe_ge',t:'年化ROE达标'},{v:'loss_making',t:'亏损'},{v:'revenue_declining',t:'营收下滑'}];
const THEME_OPTS=(()=>{const seen=new Map();let none=false;
  for(const s of DATA.stocks){if(s.theme_id){if(!seen.has(s.theme_id))seen.set(s.theme_id,s.theme_name||s.theme_id);}else none=true;}
  const o=[...seen.entries()].sort((a,b)=>String(a[1]).localeCompare(String(b[1]),'zh')).map(([v,t])=>({v,t}));
  if(none)o.push({v:'__none__',t:'无归属主线'});return o;})();
const MS_DEFS=[
  {key:'ind',label:'行业',opts:(DATA.industries||[]).map(v=>({v,t:v})),match:(s,v)=>s.industry===v,searchable:true},
  {key:'theme',label:'主线',opts:THEME_OPTS,match:(s,v)=>(s.theme_id||'__none__')===v,searchable:true},
  {key:'tier',label:'分档',opts:[{v:'强',t:'强'},{v:'中',t:'中'},{v:'观察',t:'观察'},{v:'剔除',t:'剔除'}],match:(s,v)=>s.tier===v},
  {key:'qual',label:'质量成色',opts:[{v:'扎实',t:'扎实'},{v:'尚可',t:'尚可'},{v:'存疑',t:'存疑'},{v:'虚高',t:'虚高'}],match:(s,v)=>s.quality_call===v},
  {key:'ff',label:'兑现度',opts:[{v:'above',t:'超预告上限'},{v:'within',t:'落预告区间'},{v:'below',t:'低于预告'},{v:'__none__',t:'无预告'}],match:(s,v)=>(s.ff_in_range||'__none__')===v},
  {key:'react',label:'反应',opts:[{v:'up',t:'向上断层(强)'},{v:'down',t:'向下断层(弱)'},{v:'intact',t:'跳空未回补'},{v:'muted',t:'未反应'},{v:'intheme',t:'主线内'}],match:(s,v)=>REACT_PRED[v](s)},
  {key:'hit',label:'信号',opts:HIT_OPTS,match:(s,v)=>(s.hits||[]).includes(v),searchable:true},
  {key:'pe',label:'扣非TTM PE',opts:DATA.pe_buckets||[],match:(s,v)=>s.pe_bucket===v},
];
function optCount(d,v){let n=0;for(const s of DATA.stocks)if(d.match(s,v))n++;return n;}
function groupSel(k){const m=document.querySelector('.ms[data-key="'+k+'"]');return m?[...m.querySelectorAll('input:checked')].map(cb=>cb.value):[];}
function filtered(){const sel={};MS_DEFS.forEach(d=>sel[d.key]=groupSel(d.key));
  return DATA.stocks.filter(s=>MS_DEFS.every(d=>!sel[d.key].length||sel[d.key].some(v=>d.match(s,v))));}
function updateMS(m){const ck=[...m.querySelectorAll('input:checked')],sum=m.querySelector('.ms-sum');
  m.classList.toggle('on',ck.length>0);
  if(!ck.length)sum.textContent='全部';
  else{const l=ck.map(cb=>cb.closest('label').querySelector('.tt').textContent);
    sum.textContent=ck.length<=2?l.join('、'):ck.length+' 项';}
  const c=document.getElementById('fclear');if(c)c.hidden=!document.querySelector('.ms.on');}
function closeAllPops(x){document.querySelectorAll('.ms .ms-pop').forEach(p=>{if(!x||p.parentElement!==x)p.hidden=true;});}
function buildMS(){
  const bar=document.getElementById('msbar');bar.innerHTML='';
  for(const def of MS_DEFS){
    const w=document.createElement('div');w.className='ms';w.dataset.key=def.key;
    const search=def.searchable?`<input class="ms-search" placeholder="过滤${esc(def.label)}…">`:'';
    const rows=def.opts.filter(o=>optCount(def,o.v)>0).map(o=>`<label data-t="${esc(String(o.t).toLowerCase())}"><input type="checkbox" value="${esc(o.v)}"><span class="tt">${esc(o.t)}</span><span class="cnt">${optCount(def,o.v)}</span></label>`).join('');
    w.innerHTML=`<button class="ms-btn" type="button">${esc(def.label)} <span class="ms-sum">全部</span><span class="ms-caret">▾</span></button>`+
      `<div class="ms-pop" hidden>${search}<div class="ms-list">${rows||'<div class="ms-empty">本期无</div>'}</div></div>`;
    bar.appendChild(w);
    const btn=w.querySelector('.ms-btn'),pop=w.querySelector('.ms-pop');
    btn.addEventListener('click',e=>{e.stopPropagation();const open=pop.hidden;closeAllPops(open?w:null);pop.hidden=!open;
      if(open){pop.classList.remove('rt');if(pop.getBoundingClientRect().right>window.innerWidth-8)pop.classList.add('rt');
        const sb=pop.querySelector('.ms-search');if(sb){sb.value='';pop.querySelectorAll('.ms-list label').forEach(l=>l.style.display='');sb.focus();}}});
    pop.addEventListener('click',e=>e.stopPropagation());
    w.querySelectorAll('input[type=checkbox]').forEach(cb=>cb.addEventListener('change',()=>{updateMS(w);renderList();}));
    const sb=pop.querySelector('.ms-search');
    if(sb)sb.addEventListener('input',()=>{const q=sb.value.trim().toLowerCase();
      pop.querySelectorAll('.ms-list label').forEach(l=>{l.style.display=(!q||l.dataset.t.includes(q))?'':'none';});});
  }
}
function clearFilters(){document.querySelectorAll('.ms input:checked').forEach(cb=>cb.checked=false);
  document.querySelectorAll('.ms').forEach(updateMS);closeAllPops(null);renderList();}
const LIST_PAGE=120;let LIST_SHOWN=LIST_PAGE;let SEL=null;
function orderedRows(mode,rows){
  if(mode==='ann')return [...rows].sort((a,b)=>(b.ann_date||'').localeCompare(a.ann_date||''));
  if(mode==='rank')return [...rows].sort((a,b)=>(b.rank_score||-99)-(a.rank_score||-99));
  if(mode==='theme'){const rank=s=>{const k=s.theme_id||'__none__',tr=DATA.theme_trends[k];return tr?tr.net:-99;};
    return [...rows].sort((a,b)=>rank(b)-rank(a)||String(a.theme_name||'').localeCompare(String(b.theme_name||''),'zh')||a.ts_code.localeCompare(b.ts_code));}
  if(mode==='ind')return [...rows].sort((a,b)=>String(a.industry||'').localeCompare(String(b.industry||''),'zh')||(b.rank_score||-99)-(a.rank_score||-99));
  return rows;
}
function renderList(reset=true){
  const mode=document.getElementById('grp').value;
  const all=orderedRows(mode,filtered());
  if(reset)LIST_SHOWN=LIST_PAGE;
  const rows=all.slice(0,LIST_SHOWN);
  const fc=document.getElementById('fcount');
  if(fc)fc.innerHTML=`<b>${all.length}</b> / ${DATA.stocks.length} 只 · 已显示 ${rows.length}`;
  let h='';
  if(mode==='theme'||mode==='ind'||mode==='ann'){
    const key=s=>mode==='theme'?(s.theme_id||'__none__'):(mode==='ind'?(s.industry||'未分类'):(s.ann_date||'未知日期'));
    const seen=new Map();
    for(const s of rows){const k=key(s);if(!seen.has(k))seen.set(k,[]);seen.get(k).push(s);}
    for(const [k,grp] of seen.entries()){
      let label=k,extra=`<span>${grp.length}</span>`;
      if(mode==='theme'){const s0=grp[0],tr=DATA.theme_trends[k];
        label=k==='__none__'?'无归属主线':`${s0.theme_name} ${s0.theme_state||''}${s0.theme_stars?'★'+s0.theme_stars:''}`;
        if(tr)extra=`<span style="display:flex;gap:4px;flex-wrap:wrap">${trendPill(tr)}${judgedPill(tr)}</span>`;}
      else if(mode==='ann'&&k.length===8)label=`${k.slice(0,4)}-${k.slice(4,6)}-${k.slice(6)}`;
      h+=`<div class="ghead"><span>${esc(label)}</span>${extra}</div>`+grp.map(stockRow).join('');
    }
  }else{h=rows.map(stockRow).join('');}
  if(rows.length<all.length){const n=Math.min(LIST_PAGE,all.length-rows.length);
    h+=`<button id="loadmore" class="loadmore" type="button">再显示 ${n} 家（剩余 ${all.length-rows.length} 家）</button>`;}
  document.getElementById('list').innerHTML=h||'<div class="empty">无匹配</div>';
  document.querySelectorAll('.row').forEach(el=>el.onclick=()=>select(el.dataset.c));
  const more=document.getElementById('loadmore');if(more)more.onclick=()=>{LIST_SHOWN+=LIST_PAGE;renderList(false);};
  if(rows.length&&!rows.some(s=>s.ts_code===SEL))select(rows[0].ts_code);
}
function select(c){SEL=c;document.querySelectorAll('.row').forEach(el=>el.classList.toggle('sel',el.dataset.c===c));renderDetail();}
function mrow(k,v,c){return `<div class="mrow"><span class="k">${k}</span><span class="${c||''}">${v}</span></div>`;}
function peHero(s){
  const asof=s.mv_asof?`(${s.mv_asof.slice(4,6)}-${s.mv_asof.slice(6)})`:'';
  const subs=[`总市值 ${s.total_mv_yi!=null?fmtYi(s.total_mv_yi)+'亿':'—'}${asof}`];
  let big='—',label='扣非TTM PE',note='';
  if(s.pe_ttm_dedt!=null){
    big=`${s.pe_ttm_dedt}<span class="vx">×</span>`;
    subs.push(`扣非TTM净利 ${fmtYi(s.dedt_ttm_yi)}亿`);
    if(s.pe_ttm_np!=null)subs.push(`归母TTM PE ${s.pe_ttm_np}×（对照）`);
    if(s.pe_ann_dedt!=null)subs.push(`扣非年化PE ${s.pe_ann_dedt}×`);
  }else if(s.pe_ttm_np!=null){
    label='归母TTM PE';big=`${s.pe_ttm_np}<span class="vx">×</span>`;
    note='扣非TTM≤0 或缺失 —— 利润里非经常性成分占比高，扣非口径给不出 PE，这本身是含金量警示';
    subs.push(`归母TTM净利 ${fmtYi(s.np_ttm_yi)}亿`);
  }else{
    note=s.pe_ttm_dedt_note==='np_nonpositive'?'TTM 仍亏损，PE 无意义':
      (s.pe_ttm_dedt_note==='mv_missing'?'未取到总市值，PE 缺失':'TTM 分母缺失（次新股基期不全）');
  }
  if(s.pe_ttm_market!=null)subs.push(`市场PE-TTM ${s.pe_ttm_market}×`);
  return `<div class="vhero"><div class="vpe"><span class="vk">${label}</span><span class="vnum">${big}</span></div>
    <div class="vsub">${subs.join(' · ')}${note?`<br>${note}`:''}</div></div>`;
}
function triad(s){
  // 质量三件套：扣非占比 / 经营现金对净利的覆盖 / 应收+存货是否跑赢营收。
  // 三格都是确定性数字，颜色只按阈值上色，定性(扎实/存疑)由模型写在判分里。
  const c1=s.dedt_ratio==null?'mut':(s.dedt_ratio>=80?'pos':(s.dedt_ratio>=50?'':'neg'));
  const c2=s.ocf_to_np==null?'mut':(s.ocf_to_np>=60?'pos':(s.ocf_to_np>=0?'':'neg'));
  const worst=Math.max(s.ar_gap_pp==null?-999:s.ar_gap_pp,s.inv_gap_pp==null?-999:s.inv_gap_pp);
  const c3=worst===-999?'mut':(worst>=20?'neg':(worst<=0?'pos':''));
  return `<div class="triad">
   <div class="tcard"><div class="tk">扣非占归母</div><div class="tv ${c1}">${s.dedt_ratio==null?'—':s.dedt_ratio+'%'}</div>
     <div class="tn">非经常 ${s.non_recurring_yi==null?'—':fmtYi(s.non_recurring_yi)+'亿'}</div></div>
   <div class="tcard"><div class="tk">经营现金/归母</div><div class="tv ${c2}">${s.ocf_to_np==null?'—':s.ocf_to_np+'%'}</div>
     <div class="tn">OCF ${fmtYi(s.ocf_cum_yi)}亿 · 销售收现${s.cash_sales_ratio==null?'—':s.cash_sales_ratio+'%'}</div></div>
   <div class="tcard"><div class="tk">应收/存货 vs 营收</div><div class="tv ${c3}">${worst===-999?'—':sign(Math.round(worst))+'pp'}</div>
     <div class="tn">应收${s.ar_gap_pp==null?'—':sign(s.ar_gap_pp)+'pp'} · 存货${s.inv_gap_pp==null?'—':sign(s.inv_gap_pp)+'pp'}</div></div>
  </div>`;
}
function growthTable(s){
  const r=(lab,a,b,c)=>`<tr><td>${lab}</td><td class="${cls(a)}">${pct(a)}</td><td class="${cls(b)}">${pct(b)}</td><td class="${cls(c)}">${pct(c)}</td></tr>`;
  return `<table class="gtab"><thead><tr><th>口径</th><th>累计同比</th><th>单季同比</th><th>环比</th></tr></thead><tbody>
    ${r('营业收入',s.rev_cum_yoy,s.rev_sq_yoy,s.rev_qoq)}
    ${r('归母净利',s.np_cum_yoy,s.np_sq_yoy,s.np_qoq)}
    ${r('扣非净利',s.dedt_cum_yoy,s.dedt_sq_yoy,s.dedt_qoq)}
    </tbody></table>`;
}
function ffBlock(s){
  if(s.ff_in_range==null&&s.ff_min==null)return '<div class="dtext mut">本期无业绩预告可对照（不强制预告的公司属常态，不代表业绩差）。</div>';
  const lo=s.ff_min,hi=s.ff_max,p=s.ff_position;
  let bar='';
  if(lo!=null&&hi!=null&&p!=null){
    const clamp=Math.max(-0.25,Math.min(1.25,p));
    const left=((clamp+0.25)/1.5*100).toFixed(1);
    bar=`<div class="ffbar"><div class="band" style="left:16.7%;right:16.7%"></div>
      <div class="mark" style="left:${left}%"></div>
      <span class="lb" style="left:16.7%">下限 ${fmtYi(lo)}亿</span>
      <span class="lb" style="right:16.7%">上限 ${fmtYi(hi)}亿</span></div>`;
  }
  return bar+`<div class="mgrid">
    ${mrow('预告类型',esc(s.ff_type||'—'))}
    ${mrow('实际 vs 中值',s.ff_vs_median==null?'—':sign(s.ff_vs_median)+'%',cls(s.ff_vs_median))}
    ${mrow('落点',s.ff_in_range==null?'—':({above:'超上限',within:'区间内',below:'低于下限'}[s.ff_in_range]||s.ff_in_range))}
    ${mrow('区间分位',s.ff_position==null?'—':(s.ff_position*100).toFixed(0)+'%')}
  </div>`+(s.fulfillment_call?`<div class="tline">模型判兑现度：<b>${esc(s.fulfillment_call)}</b></div>`:'');
}
function memberLine(m){const c=m.gap_open_pct>=0?'pos':'neg';
  return `<span class="${c}">${esc(m.name)}${sign(m.gap_open_pct)}%${m.gap_status==='filled'?'(回补)':''}</span>`;}
function renderDetail(){
  const s=DATA.stocks.find(x=>x.ts_code===SEL);
  const el=document.getElementById('detail');
  if(!s){el.innerHTML='<div class="empty">点击左侧个股查看详情</div>';return;}
  let h=`<div class="dh"><span class="nm">${esc(s.name)}</span><span class="cd mut">${s.ts_code}</span>
    <span class="mut" style="font-size:12px">${esc(s.industry||'')}</span>
    ${s.tier?pill(tierPill[s.tier],s.tier):''}${s.quality_call?pill(qualPill[s.quality_call]||'watch',s.quality_call):''}
    ${gapBadge(s)}${ffBadge(s)}${badges(s)}</div>`;
  h+=peHero(s);
  h+=`<div class="dsec">利润质量三件套（确定性数字，定性看下方判分）</div>`+triad(s);
  h+=`<div class="klwrap" id="kl"></div>`;
  h+=`<div class="dsec">增长（三口径 × 三指标）</div>`+growthTable(s);
  h+=`<div class="dsec">盈利能力边际</div><div class="mgrid">`;
  h+=mrow('单季毛利率',s.gm_sq==null?'—':s.gm_sq+'%');
  h+=mrow('毛利率同比',s.gm_sq_yoy_pp==null?'—':sign(s.gm_sq_yoy_pp)+'pp',cls(s.gm_sq_yoy_pp));
  h+=mrow('毛利率环比',s.gm_sq_qoq_pp==null?'—':sign(s.gm_sq_qoq_pp)+'pp',cls(s.gm_sq_qoq_pp));
  h+=mrow('单季净利率',s.nm_sq==null?'—':s.nm_sq+'%');
  h+=mrow('年化ROE',s.roe_ann==null?'—':s.roe_ann+'%');
  h+=mrow('研发投入',s.rd_yi==null?'—':fmtYi(s.rd_yi)+'亿'+(s.rd_yoy==null?'':' ('+sign(s.rd_yoy)+'%)'));
  const e=s.exp_ratio||{};
  h+=mrow('销售费用率',e.sell_pct==null?'—':e.sell_pct+'% '+(e.sell_yoy_pp==null?'':'('+sign(e.sell_yoy_pp)+'pp)'));
  h+=mrow('管理费用率',e.admin_pct==null?'—':e.admin_pct+'% '+(e.admin_yoy_pp==null?'':'('+sign(e.admin_yoy_pp)+'pp)'));
  h+='</div>';
  h+=`<div class="dsec">资产负债表前瞻信号</div><div class="mgrid">`;
  const bs=(lab,o)=>mrow(lab,o&&o.yi!=null?fmtYi(o.yi)+'亿 '+(o.yoy_pct==null?'':'同比'+sign(o.yoy_pct)+'%'):'—',cls(o?o.yoy_pct:null));
  h+=bs('合同负债/预收',s.contract_liab);
  h+=bs('存货',s.inventories);
  h+=bs('应收(含票据)',s.receivables);
  h+=bs('在建工程',s.cip);
  h+=mrow('资产负债率',s.debt_ratio==null?'—':s.debt_ratio+'%');
  h+=mrow('商誉/净资产',s.goodwill_eq_pct==null?'—':s.goodwill_eq_pct+'%');
  h+='</div>';
  if(s.hits&&s.hits.length){
    const good=new Set(['np_accelerating','rev_accelerating','np_qoq_positive','dedt_clean','ocf_backed','roe_ge','margin_expanding','orderbook_building','capex_cycle','gap_up','beat_forecast','rev_single_yoy_ge','np_single_yoy_ge','dedt_single_yoy_ge']);
    h+=`<div class="chips">`+s.hits.map(x=>`<span class="chip ${good.has(x)?'good':'bad'}">${esc(x)}</span>`).join('')+`</div>`;
  }
  h+=`<div class="dsec">兑现度（实际 vs 公司预告）</div>`+ffBlock(s);
  h+=`<div class="dsec">股价反应</div><div class="mgrid">`;
  h+=mrow('公告日跳空',s.gap_open_pct==null?'—':sign(s.gap_open_pct)+'%',cls(s.gap_open_pct));
  h+=mrow('公告后累计',s.since_ann_pct==null?'—':sign(s.since_ann_pct)+'%',cls(s.since_ann_pct));
  h+=mrow('公告前位置',s.pre_pos==null?'—':s.pre_pos+'% 分位'+(s.pre_pos_bars?`(${s.pre_pos_bars}根)`:''));
  h+='</div>';
  h+=`<div class="dsec">所属主线 · 报告期行业趋势</div>`;
  const tr=s.theme_id?DATA.theme_trends[s.theme_id]:null;
  if(!tr){h+=`<div class="dtext">${s.tier?'无归属主线 —— 不纳入行业趋势聚合（业绩强但暂无主线关注，本身是信号）。':'未判分 —— 先跑 verdict record 归属主线。'}</div>`;}
  else{
    h+=`<div class="tline">${esc(tr.theme_name)} ${esc(tr.state||'')}${tr.stars?'★'+tr.stars:''} ${trendPill(tr)}</div>`;
    if(tr.strong.length)h+=`<div class="tline"><span class="pos">强表现·向上断层 ${tr.strong.length}</span>：${tr.strong.map(memberLine).join('、')}</div>`;
    if(tr.weak.length)h+=`<div class="tline"><span class="neg">弱表现·向下断层 ${tr.weak.length}</span>：${tr.weak.map(memberLine).join('、')}</div>`;
    if(tr.neutral_n)h+=`<div class="tline mut">无断层 ${tr.neutral_n} 只</div>`;
    h+=`<div class="tline mut" style="font-size:11px">净方向=强−弱=${tr.net>=0?'+':''}${tr.net}（|净|≥${DATA.trend_net} 记偏强/偏弱；机械计数）</div>`;
    const j=tr.judged;
    if(j){
      h+=`<div class="tline" style="margin-top:4px">${judgedPill(tr)}${j.confidence?` <span class="mut" style="font-size:11px">confidence=${esc(j.confidence)}</span>`:''} <span class="mut" style="font-size:11px">判于 ${esc(j.judged_at)}（当时强${j.snap_strong}/弱${j.snap_weak}）${j.stale?' · 成员已变化，待复判':''}</span></div>`;
      if(j.strong_common)h+=`<div class="tline"><span class="pos">强侧归因</span>：${esc(j.strong_common)}</div>`;
      if(j.weak_common)h+=`<div class="tline"><span class="neg">弱侧归因</span>：${esc(j.weak_common)}</div>`;
      if(j.cross_validation)h+=`<div class="dtext" style="margin-top:3px">交叉验证：${esc(j.cross_validation)}</div>`;
    }else h+=`<div class="tline mut" style="font-size:11px">行业趋势归因待判 —— verdict 判分时对该主线写 theme_trends。</div>`;
  }
  const themeLine=s.theme_id?`<span class="acc">${s.match_confidence==='low'?'疑似 ':''}${esc(s.theme_name)}</span> <span class="pill ${s.theme_hot?'thot':'tcool'}">${esc(s.theme_state||'')}${s.theme_stars?'★'+s.theme_stars:''}</span>`:(s.tier?'<span class="mut">无归属主线</span>':'<span class="mut">未判</span>');
  h+=`<div class="dsec">归属主线</div><div style="font-size:13px">${themeLine}</div>`;
  if(s.theme_rationale)h+=`<div class="dtext" style="margin-top:4px">${esc(s.theme_rationale)}</div>`;
  if(s.reason||s.caveat)h+=`<div class="dsec">判分</div><div class="dtext">${esc(s.reason||'')}${s.caveat?'<br>caveat: '+esc(s.caveat):''}</div>`;
  const srcTxt=`Tushare 结构化报表（${(s.sources||[]).join('/')||'—'}）`;
  h+=`<div class="dsec">数据来源</div><div class="tline mut" style="font-size:12px">${srcTxt}`;
  if(s.cninfo_url)h+=` · <a class="src" href="${esc(s.cninfo_url)}" target="_blank" rel="noopener">${esc(s.cninfo_title||'巨潮原文 PDF')}</a>`;
  h+=`<br>需要分产品收入 / 管理层讨论 / 非经常损益明细时按需取 PDF：<code>report_pdf.py --code ${s.ts_code} --period ${DATA.period} --sections segment,mdna,nonrecurring</code></div>`;
  el.innerHTML=h;
  renderKline(s);
}
const KL_CACHE=new Map(),KL_PENDING=new Map();
async function loadKline(code){
  const shard=DATA.kline_shards[code];
  if(shard===undefined||shard===null)return null;
  if(KL_CACHE.has(shard))return KL_CACHE.get(shard)[code]||null;
  if(!KL_PENDING.has(shard)){
    const url=new URL(`${DATA.kline_asset_base}/${shard}.json`,document.baseURI);
    KL_PENDING.set(shard,fetch(url,{cache:'force-cache'}).then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json();})
      .then(p=>{KL_CACHE.set(shard,p);return p;}).finally(()=>KL_PENDING.delete(shard)));
  }
  const p=await KL_PENDING.get(shard);return p[code]||null;
}
async function renderKline(s){
  const el=document.getElementById('kl');if(!el)return;
  el.innerHTML='<div class="empty">K线加载中…</div>';
  try{const bars=await loadKline(s.ts_code);if(SEL!==s.ts_code)return;drawKline(el,bars,s);}
  catch(err){if(SEL!==s.ts_code)return;
    const hint=location.protocol==='file:'?'；本地文件请通过 HTTP 预览':'';
    el.innerHTML=`<div class="empty">K线分片加载失败${hint}（${esc(err&&err.message?err.message:'未知错误')}）</div>`;}
}
function drawKline(el,bars,s){
  if(!bars||bars.length<2){el.innerHTML='<div class="empty">无K线数据（--no-price 或日线缓存为空）</div>';return;}
  const W=620,PH=210,VH=52,PADL=44,PADR=8,PADT=10,GAPV=14,H=PADT+PH+GAPV+VH+18;
  const n=bars.length,step=(W-PADL-PADR)/n,cw=Math.max(1.5,step*0.62);
  let lo=Infinity,hi=-Infinity,vmax=0;
  for(const b of bars){if(b[3]!==null&&b[3]<lo)lo=b[3];if(b[2]!==null&&b[2]>hi)hi=b[2];if(b[5]>vmax)vmax=b[5];}
  if(s.pre_close!=null){lo=Math.min(lo,s.pre_close);hi=Math.max(hi,s.pre_close);}
  if(!isFinite(lo)||!isFinite(hi)){el.innerHTML='<div class="empty">无K线数据</div>';return;}
  vmax=Math.max(vmax,1);const pad=(hi-lo)*0.04||1;lo-=pad;hi+=pad;
  const y=p=>PADT+PH-(p-lo)/(hi-lo)*PH,vy=v=>PADT+PH+GAPV+VH-(v/vmax)*VH;
  let g='';
  for(let i=0;i<4;i++){const p=lo+(hi-lo)*i/3,yy=y(p);
    g+=`<line x1="${PADL}" y1="${yy}" x2="${W-PADR}" y2="${yy}" stroke="var(--bd)" stroke-width="0.6"/>`+
       `<text x="${PADL-4}" y="${yy+3.5}" font-size="10" fill="var(--tx3)" text-anchor="end">${p.toFixed(p>=100?0:2)}</text>`;}
  let k='',v='',annIdx=-1;
  for(let i=0;i<n;i++){const b=bars[i],x=PADL+i*step+step/2;
    if(annIdx<0&&s.ann_date&&b[0]>=s.ann_date)annIdx=i;
    if(b[1]===null||b[2]===null||b[3]===null||b[4]===null)continue;
    const up=b[4]>=b[1],c=up?'var(--kup)':'var(--kdn)';
    k+=`<line x1="${x}" y1="${y(b[2])}" x2="${x}" y2="${y(b[3])}" stroke="${c}" stroke-width="1"/>`;
    const yo=y(Math.max(b[1],b[4])),yc=y(Math.min(b[1],b[4]));
    k+=`<rect x="${x-cw/2}" y="${yo}" width="${cw}" height="${Math.max(1,yc-yo)}" fill="${up?'none':c}" stroke="${c}" stroke-width="1"/>`;
    if(b[5])v+=`<rect x="${x-cw/2}" y="${vy(b[5])}" width="${cw}" height="${PADT+PH+GAPV+VH-vy(b[5])}" fill="${c}" opacity="0.55"/>`;}
  let m='';
  if(s.pre_close!=null){const yy=y(s.pre_close);
    m+=`<line x1="${PADL}" y1="${yy}" x2="${W-PADR}" y2="${yy}" stroke="var(--amb)" stroke-width="1" stroke-dasharray="4 3"/>`+
       `<text x="${W-PADR}" y="${yy-4}" font-size="10" fill="var(--amb)" text-anchor="end">公告前收盘 ${s.pre_close}</text>`;}
  if(annIdx>=0){const mi=Math.max(0,annIdx-1),x=PADL+mi*step+step/2;
    m+=`<line x1="${x}" y1="${PADT}" x2="${x}" y2="${PADT+PH+GAPV+VH}" stroke="var(--acc)" stroke-width="1" stroke-dasharray="4 3"/>`+
       `<text x="${x+3}" y="${PADT+10}" font-size="10" fill="var(--acc)">披露日</text>`;}
  const d0=bars[0][0],d1=bars[n-1][0];
  const ax=`<text x="${PADL}" y="${H-4}" font-size="10" fill="var(--tx3)">${d0.slice(0,4)}-${d0.slice(4,6)}-${d0.slice(6)}</text>`+
    `<text x="${W-PADR}" y="${H-4}" font-size="10" fill="var(--tx3)" text-anchor="end">${d1.slice(0,4)}-${d1.slice(4,6)}-${d1.slice(6)}</text>`;
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto" role="img" aria-label="${esc(s.name)} 日K线，披露日标注在披露日前一交易日">${g}${k}${v}${m}${ax}</svg>`;
}
buildMS();
document.getElementById('grp').addEventListener('change',renderList);
document.getElementById('fclear').addEventListener('click',clearFilters);
document.addEventListener('click',e=>{if(!e.target.closest('.ms'))closeAllPops(null);});
renderList();
"""


def _load_shared_theme(theme: str) -> str:
    """Load the canonical shared html_report theme in dev and synced packages."""
    script_dir = Path(__file__).resolve().parent
    bundled = script_dir / "_shared" / "html_report" / "themes" / f"{theme}.css"
    dev = script_dir.parents[2] / "shared" / "html_report" / "themes" / f"{theme}.css"
    path = bundled if bundled.is_file() else dev
    if not path.is_file():
        raise FileNotFoundError(f"shared html_report theme 不存在: {path}")
    return path.read_text(encoding="utf-8")


def render_html(view: Dict[str, Any], kline_assets: Optional[Dict[str, Any]] = None) -> str:
    empty_note = ('<div class="foot">主线台账为空——归属与行业趋势需先运行 daily-market-sense 填充 theme 台账、'
                  '再跑 verdict 判分归属。</div>' if view["theme_registry_empty"] else "")
    ov = view.get("overview")
    if ov:
        stale = '<span class="badge bstale">样本已更新，待复判</span>' if ov["stale"] else ""
        overview_html = ('<div class="ovw"><div class="ttl">报告期产业结构综述（模型判断，落台账）'
                         f'<span>判于 {escape(ov["judged_at"])} · 当时样本 {ov["snap_total"]} 家</span>{stale}</div>'
                         f'<div class="ovtx">{escape(ov["text"])}</div></div>')
    else:
        overview_html = ""

    cutoff = view["ann_cutoff"]
    cutoff_display = f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:]}"
    progress = view.get("disclosure_progress_pct")
    prog_html = ""
    if progress is not None:
        prog_html = (f'<div class="prog"><span>披露进度 {view.get("released_count") or 0} 家</span>'
                     f'<span class="bar"><i style="width:{min(100.0, float(progress)):.1f}%"></i></span>'
                     f'<span>{progress}% · 已出证据 {view.get("with_statements") or 0} 家 · '
                     f'页面收录 {len(view["stocks"])} 家（{escape(str(view.get("shortlist_rule") or ""))}）</span></div>')

    kline_assets = kline_assets or {
        "asset_base": f"qreport_{view['period']}.klines",
        "code_to_shard": {code: _kline_shard_id(code) for code in view.get("klines", {})},
    }
    data_json = json.dumps({
        "period": view["period"], "trend_net": view["trend_net"],
        "pe_buckets": view["pe_buckets"], "industries": view["industries"],
        "stocks": view["stocks"], "theme_trends": view["theme_trends"],
        "kline_asset_base": kline_assets["asset_base"],
        "kline_shards": kline_assets["code_to_shard"],
    }, ensure_ascii=False, separators=(",", ":"))
    shared_theme = _load_shared_theme("default")
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{view['period_label']} 正式财报 · 报告期观察</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;600;700&family=Roboto+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{shared_theme}
{_CSS}</style></head>
<body><main class="page"><div class="doc-head"><span class="dh-title">Quarterly Report</span><span class="dh-meta">{view['period_label']} · 披露截止 {cutoff_display} · updated {view['updated_at']}</span></div>
<section class="section report q-report"><div class="wrap">
<div style="display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px">
<div><h1>{view['period_label']} 正式财报 · 报告期观察</h1>
<div class="sub">end_date {view['period']} · 披露扫描截至 {cutoff_display}（当日 {view['ann_cutoff_stock_count']} 家）· Tushare 结构化报表为数值权威 · PDF 按需 · 增长 × 利润质量 × 兑现度 × 股价断层</div></div>
<div class="sub">更新于 {view['updated_at']}</div></div>
{prog_html}
{overview_html}
<div class="ctrl">
<select id="grp" title="排序 / 分组（单选）"><option value="rank">按机械得分排序</option><option value="ann">按披露时间</option><option value="theme">按主线分组</option><option value="ind">按行业分组</option><option value="flat">平铺</option></select>
<div id="msbar" class="msbar"></div>
<button id="fclear" class="fbar-x" type="button" hidden>清空筛选</button>
<span id="fcount" class="fcount"></span>
</div>
<div class="panes">
<div class="list" id="list"><div class="empty">正在加载 {len(view['stocks'])} 家财报数据…</div></div>
<div class="detail" id="detail"><div class="empty">点击左侧个股查看详情</div></div>
</div>
<div class="foot">数值全部来自 Tushare 结构化报表（income / fina_indicator / cashflow / balancesheet），单季 = 本期累计 − 上一累计期，
Q1 的单季即累计、Q1 的环比基期是上年 Q4 · 利润质量三件套：扣非占归母（非经常性损益占比）、经营现金流对归母的覆盖率、应收与存货同比减营收同比（正值越大越要追问收入质量）——
数字是确定性的，「扎实/尚可/存疑/虚高」的定性由模型写在判分里 · 兑现度 = 实际归母 vs 公司自己的业绩预告区间，落点与偏离都是机械计算 ·
断层以披露日为锚：跳空 = 披露日开盘 vs 前一交易日收盘，未回补 = 其后价格未回到披露前收盘的另一侧，D+n = 断层后交易日数（新断层未经时间检验）·
行业趋势 ↑↓⇅ 为机械计数，「判·方向」是模型对强/弱侧成因的归因与交叉验证（落 verdict 台账，† = 成员已变化待复判）·
扣非TTM PE = 最新总市值 ÷（上年年报 + 本期累计 − 上年同期）扣非净利，年报期即当期；年化PE 未调季节性，淡旺季分明的行业会被系统性高/低估 ·
分产品收入、管理层讨论、非经常性损益明细不在结构化数据里，需要时用 report_pdf.py 按需取原文 ·
K线为前复权（qfq），红涨绿跌，蓝虚线 = 披露日标注（落在披露日前一交易日）、橙虚线 = 披露前收盘 · 仅作观察、不含买卖建议</div>
{empty_note}
</div>
<script>const DATA={data_json};{_JS}</script>
</section></main></body></html>"""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="渲染一个报告期的正式财报 HTML（主从布局）")
    ap.add_argument("--period")
    ap.add_argument("--evidence")
    ap.add_argument("--pos-split", type=float, default=POS_SPLIT,
                    help="披露前位置分位阈值：>= 为趋势加速型、< 为低位启动型（默认 50）")
    ap.add_argument("--muted-max", type=float, default=MUTED_MAX_PCT,
                    help="未反应观察：已判强/中、无断层且 |披露后累计| < N%%（默认 3）")
    ap.add_argument("--kline-days", type=int, default=KLINE_BARS, help="详情页 K 线根数（默认 130）")
    ap.add_argument("--require-ann-cutoff",
                    help="严格要求 evidence 披露截止日为 YYYYMMDD；不一致则拒绝渲染")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    today = beijing_today()
    period = args.period or latest_quarter_end(today)
    quarter_of(period)

    ev_path = args.evidence or os.path.join("reports", f"qreport_scan_{period}.json")
    evidence = _load_json(ev_path)
    if evidence is None:
        print(json.dumps({"error": f"evidence 不存在: {ev_path}，请先运行 report_scan.py。"},
                         ensure_ascii=False))
        return 2
    try:
        cutoff_info = _validate_evidence_cutoff(evidence, args.require_ann_cutoff)
    except (TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "evidence": ev_path}, ensure_ascii=False))
        return 2

    store = Store()
    codes = [str(s["ts_code"]) for s in evidence.get("stocks", [])]
    bars_map = store.load_bars_many(codes)
    view = build_view(period, evidence, store.load_verdicts(period),
                      store.load_theme_registry(), store.load_theme_latest_state(),
                      store.load_theme_trends(period), store.load_period_overview(period),
                      bars_map, today, pos_split=args.pos_split, muted_max=args.muted_max,
                      kline_bars=args.kline_days)
    out_path = args.out or os.path.join("reports", f"qreport_{period}.html")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    kline_assets = write_kline_assets(out_path, view["klines"],
                                      generated_at=cutoff_info["evidence_generated_at"])
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(render_html(view, kline_assets))
    trends = view["theme_trends"]
    print(json.dumps({
        "period": period, "stocks": len(view["stocks"]), "klines": len(view["klines"]),
        "themes_with_trend": len(trends),
        "themes_judged": sum(1 for t in trends.values() if t.get("judged")),
        "overview": bool(view.get("overview")),
        "ann_cutoff": cutoff_info["ann_cutoff"],
        "ann_cutoff_stock_count": cutoff_info["ann_cutoff_stock_count"],
        "evidence_generated_at": cutoff_info["evidence_generated_at"],
        "kline_asset_dir": kline_assets["asset_dir"],
        "kline_shards": kline_assets["nonempty_shards"],
        "html_bytes": os.path.getsize(out_path), "out": out_path,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
