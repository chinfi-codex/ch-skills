#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render one self-contained HTML page per report period (master-detail view).

Layout: title → 产业结构综述 (model text from forecast_period_overview, stale-
badged when the sample fingerprint moved) → filter bar → two panes. Left pane is
the stock LIST, grouped by 主线 (default), sorted by 公告发布时间, or flat;
净利润断层 rows are strongly marked (向上=业绩超预期跳空↑/强表现, 向下=不及预期跳空↓/弱表现).
Right pane is the DETAIL for
the selected stock — a prominent 年化PE hero strip (预告中值年化 against the
latest total market cap, with the rolling-PE cross-check) under the header, an
embedded K-line (candles + volume, announcement marker
on the trading bar immediately before ann_date, and pre-announcement close marked)
plus a slim price line (跳空/公告后累计)、
业绩、归属主线, and a **行业趋势分析** block: within the stock's mainline, how many
members are 强表现 (向上断层) vs 弱表现 (向下断层) — the report-period industry
trend read bottom-up from earnings-vs-price gaps — plus, when the model has
judged it, the attributed trend from forecast_theme_trend (direction + 强/弱侧
归因 + cross-validation), marked stale when membership moved since judgment.

Everything comes from the evidence pack + cninfo enrichment + verdict ledger
(incl. theme trends) + live theme states + the skill's own bar cache; the
renderer projects and tallies deterministic facts, it does not judge
opportunities. Self-contained single file, no CDN, dark-mode aware.

Usage:
    python3 scripts/render_period_html.py --period 20260630
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

from store import Store

_MMDD_Q = {"0331": 1, "0630": 2, "0930": 3, "1231": 4}
_HOT_STATES = {"低位启动", "在场候选", "主线确认", "再聚焦", "修复"}
NEW_DAYS = 5           # first disclosed within N calendar days → NEW
DRIFT_PCT = 20.0       # verdict considered stale if cum-YoY moved > N points
POS_SPLIT = 50.0       # 公告前一年分位 >= split → 趋势加速型；< split → 低位启动型
MUTED_MAX_PCT = 3.0    # |公告后累计涨幅| < N% 且无断层 → 未反应观察
TREND_NET = 2          # 主线内 净断层方向 |strong-weak| >= N → 偏强/偏弱
KLINE_BARS = 130       # bars embedded per stock for the detail chart


def quarter_of(period: str) -> int:
    if period[4:] not in _MMDD_Q:
        raise ValueError(f"{period} 不是季度末。")
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


def _pct(v: Any) -> Optional[float]:
    return None if v is None else round(float(v), 2)


def _days_between(ymd: str, today: dt.date) -> Optional[int]:
    try:
        d = dt.datetime.strptime(ymd, "%Y%m%d").date()
        return (today - d).days
    except Exception:  # noqa: BLE001
        return None


def _kf_display(enrich_rec: Optional[Dict[str, Any]]) -> Optional[str]:
    parsed = (enrich_rec or {}).get("parsed") or {}
    kf = parsed.get("kf_net_profit_yi")
    if not kf:
        return None
    if kf.get("low") is not None:
        return f"{kf['low']}~{kf['high']}亿"
    if kf.get("point") is not None:
        return f"{kf['point']}亿"
    return None


def _compact_kline(bars: List[Dict[str, Any]], keep: int) -> List[List[Any]]:
    rows = []
    for b in bars[-keep:]:
        if b.get("close") is None:
            continue
        rows.append([str(b["trade_date"]), b.get("open"), b.get("high"),
                     b.get("low"), b.get("close"), b.get("vol")])
    return rows


def build_view(period: str, evidence: Dict[str, Any], enrich: Optional[Dict[str, Any]],
               verdicts: Dict[str, Dict[str, Any]], themes: Dict[str, Dict[str, Any]],
               states: Dict[str, Dict[str, Any]], trends: Dict[str, Dict[str, Any]],
               overview_row: Optional[Dict[str, Any]], bars_map: Dict[str, List[Dict[str, Any]]],
               today: dt.date, pos_split: float = POS_SPLIT, muted_max: float = MUTED_MAX_PCT,
               kline_bars: int = KLINE_BARS) -> Dict[str, Any]:
    en_idx = {str(s["ts_code"]): s for s in (enrich or {}).get("stocks", []) if s.get("found")}

    stocks: List[Dict[str, Any]] = []
    klines: Dict[str, List[List[Any]]] = {}
    for s in evidence.get("stocks", []):
        ts_code = str(s["ts_code"])
        pg = s.get("profit_growth", {})
        rev = s.get("revenue_trailing") or {}
        pr = s.get("price_reaction") or {}
        val = s.get("valuation") or {}
        v = verdicts.get(ts_code)
        first_ann = str(s.get("first_ann_date") or s.get("ann_date") or "")
        ann = str(s.get("ann_date") or "")
        cum = _pct(pg.get("cum_yoy_pct"))

        badges = []
        d_new = _days_between(first_ann, today)
        if d_new is not None and 0 <= d_new <= NEW_DAYS:
            badges.append("NEW")
        if ann and first_ann and ann != first_ann:
            badges.append("更新")

        theme_id = (v or {}).get("theme_id")
        theme_name = theme_state = None
        theme_stars = None
        theme_hot = False
        if theme_id and theme_id in themes:
            theme_name = themes[theme_id].get("name", theme_id)
            st = states.get(theme_id, {})
            theme_state = st.get("state")
            theme_stars = st.get("stars")
            theme_hot = theme_state in _HOT_STATES

        tier = (v or {}).get("tier")
        stale = False
        if v is not None:
            pann = str(v.get("evidence_ann_date") or "")
            pcum = v.get("evidence_cum_yoy")
            if (ann and pann and ann != pann) or (
                    pcum is not None and cum is not None and abs(cum - float(pcum)) > DRIFT_PCT):
                stale = True
                badges.append("待复判")

        gap_status = pr.get("gap_status")
        gap_dir = pr.get("gap_dir")
        pre_pos = _pct(pr.get("pre_pos_1y_pct"))
        since = _pct(pr.get("since_ann_pct"))
        rec = {
            "ts_code": ts_code,
            "name": s.get("name", ""),
            "type": s.get("type", ""),
            "cum_yoy": cum,
            "single_q_yoy": _pct(pg.get("single_q_yoy_pct")),
            "qoq": _pct(pg.get("qoq_pct")),
            "np_median_yi": s.get("net_profit", {}).get("median_yi"),
            "pe_ann": val.get("pe_annualized"),
            "pe_ann_note": val.get("pe_annualized_note"),
            "pe_roll": val.get("pe_rolling"),
            "total_mv_yi": val.get("total_mv_yi"),
            "ann_np_yi": val.get("annualized_np_yi"),
            "ann_label": val.get("annualize_label"),
            "mv_asof": val.get("mv_asof"),
            "kf": _kf_display(en_idx.get(ts_code)),
            "rev_yoy": _pct(rev.get("cum_yoy_pct")),
            "rev_period": (rev or {}).get("period_label"),
            "accelerating": bool(s.get("flags", {}).get("accelerating")),
            "turnaround": bool(s.get("flags", {}).get("turnaround")),
            "change_reason": s.get("change_reason", ""),
            "gap_open_pct": _pct(pr.get("gap_open_pct")),
            "gap_dir": gap_dir,
            "since_ann_pct": since,
            "gap_status": gap_status,
            "pre_pos": pre_pos,
            "days_since_r": pr.get("trading_days_since_r"),
            "reaction_date": pr.get("reaction_date"),
            "pre_ann_close": pr.get("pre_ann_close"),
            "tier": tier,
            "theme_id": theme_id,
            "theme_name": theme_name,
            "theme_state": theme_state,
            "theme_stars": theme_stars,
            "theme_hot": theme_hot,
            "match_confidence": (v or {}).get("match_confidence"),
            "theme_rationale": (v or {}).get("theme_rationale"),
            "reason": (v or {}).get("reason"),
            "caveat": (v or {}).get("caveat"),
            "badges": badges,
            "stale": stale,
            "ann_date": ann,
            "first_ann_date": first_ann,
        }
        # deterministic list facet (opportunity judgment stays with the model)
        if gap_dir == "up":
            rec["facet"] = "gap_unpos" if pre_pos is None else ("gap_trend" if pre_pos >= pos_split else "gap_low")
        elif gap_dir == "down":
            rec["facet"] = "gap_down"
        elif tier in ("强", "中") and gap_status == "none" and since is not None and abs(since) < muted_max:
            rec["facet"] = "muted"
        else:
            rec["facet"] = "other"
        stocks.append(rec)
        kl = _compact_kline(bars_map.get(ts_code, []), kline_bars)
        if kl:
            klines[ts_code] = kl

    # --- 行业趋势 (报告期内按主线聚合断层方向；确定性计数，趋势解读留给模型) ---
    theme_trends: Dict[str, Dict[str, Any]] = {}
    for st in stocks:
        tid = st["theme_id"]
        if not tid:
            continue
        tr = theme_trends.setdefault(tid, {
            "theme_id": tid, "theme_name": st["theme_name"], "state": st["theme_state"],
            "stars": st["theme_stars"], "hot": st["theme_hot"],
            "strong": [], "weak": [], "neutral_n": 0,
        })
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
        # model-judged trend (attribution + cross-validation) joined from the
        # ledger; stale when membership moved since the judgment snapshot.
        row = trends.get(tid)
        if row:
            snap = (row.get("evidence_strong_n"), row.get("evidence_weak_n"),
                    row.get("evidence_member_n"))
            tr["judged"] = {
                "direction": row.get("direction"),
                "strong_common": row.get("strong_common"),
                "weak_common": row.get("weak_common"),
                "cross_validation": row.get("cross_validation"),
                "confidence": row.get("confidence"),
                "judged_at": str(row.get("judged_at") or "")[:10],
                "snap_strong": snap[0], "snap_weak": snap[1],
                "stale": snap != (s_n, w_n, s_n + w_n + tr["neutral_n"]),
            }

    facet_rank = {"gap_trend": 0, "gap_low": 1, "gap_unpos": 2, "gap_down": 3, "muted": 4, "other": 5}
    stocks.sort(key=lambda s: (facet_rank[s["facet"]], not s["theme_hot"],
                               -abs(s["gap_open_pct"] or 0), -(s["cum_yoy"] or -999)))

    # 产业结构综述 (model text from forecast_period_overview); stale when the
    # whole-sample fingerprint (total/positive/negative counts) moved since it
    # was written — same fingerprint verdict.py snapshots at record time.
    overview = None
    if overview_row:
        ev_stocks = evidence.get("stocks", [])
        cur = (len(ev_stocks),
               sum(1 for s in ev_stocks if (s.get("flags") or {}).get("positive_type")),
               sum(1 for s in ev_stocks if (s.get("flags") or {}).get("negative_type")))
        snap = (overview_row.get("evidence_total"), overview_row.get("evidence_positive"),
                overview_row.get("evidence_negative"))
        overview = {
            "text": overview_row.get("overview") or "",
            "judged_at": str(overview_row.get("judged_at") or "")[:10],
            "snap_total": snap[0],
            "stale": snap != cur,
        }

    return {
        "period": period,
        "period_label": period_label(period),
        "updated_at": today.strftime("%Y-%m-%d"),
        "theme_registry_empty": len(themes) == 0,
        "pos_split": pos_split,
        "trend_net": TREND_NET,
        "stocks": stocks,
        "theme_trends": theme_trends,
        "overview": overview,
        "klines": klines,
    }


# --------------------------------------------------------------------------- #
# 配色口径（A 股习惯）：红=涨/强/好，绿=跌/弱/差。shared default 的
# --neg=红、--pos=绿，因此这里用 --ef-up/--ef-down 做领域别名，避免混淆。
_CSS = """
:root{--s0:var(--bg);--s1:var(--surface-2);--s2:var(--surface);--tx:var(--ink-1);--tx2:var(--ink-3);--tx3:var(--ink-4);
--bd:var(--line-2);--acc:var(--accent);--accbg:var(--accent-soft);--ef-up:var(--neg);--ef-down:var(--pos);
--amb:var(--warn);--ambbg:var(--warn-soft);--grn:var(--ef-up);--grnbg:var(--neg-soft);--gry:var(--muted);
--grybg:var(--surface-3);--red:var(--ef-down);--redbg:var(--pos-soft);--warn:var(--g-red);--warnbg:var(--g-red-soft);
--kup:var(--ef-up);--kdn:var(--ef-down);--upbg:var(--neg-soft);--dnbg:var(--pos-soft)}
.page{width:min(1680px,calc(100vw - 24px))}.report.earnings-report{max-width:none;padding:28px 20px 34px}.wrap{max-width:none;margin:0 auto;padding:0}
h1{font-size:24px;font-weight:500;margin:0}.sub{color:var(--tx2);font-size:14px;margin-top:3px}
.ovw{background:var(--accbg);border:1px solid var(--accent-hair);border-left:4px solid var(--acc);border-radius:var(--r-md);padding:15px 18px;margin-top:18px;font-size:14px;line-height:1.75}
.ovw .ttl{font-size:13px;color:var(--tx3);font-weight:500;margin-bottom:5px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ovw .ovtx{white-space:pre-line;line-height:1.75}
.ctrl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:16px 0 12px}
input,select{font:inherit;font-size:14px;padding:9px 11px;border:1px solid var(--line-1);border-radius:var(--r-sm);background:var(--s2);color:var(--tx);outline:none}
input:focus,select:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--accbg)}
input{flex:1;min-width:130px}
.panes{display:grid;grid-template-columns:minmax(340px,44%) 1fr;gap:14px;align-items:start}
@media (max-width:860px){.panes{grid-template-columns:1fr}.detail{position:static!important}}
.list{min-width:0}
.ghead{font-size:13px;color:var(--tx2);font-weight:500;background:var(--s1);border-radius:8px;padding:7px 10px;margin:12px 0 6px;display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap}
.ghead:first-child{margin-top:0}
.trend{font-size:11px;padding:1px 8px;border-radius:20px;font-weight:400}
.t-up{background:var(--grnbg);color:var(--grn)}.t-dn{background:var(--redbg);color:var(--red)}
.t-mix{background:var(--ambbg);color:var(--amb)}.t-flat{background:var(--grybg);color:var(--tx2)}
.row{background:var(--s2);border:1px solid var(--bd);border-radius:var(--r-md);padding:9px 12px;margin-bottom:7px;cursor:pointer;transition:border-color .14s ease,box-shadow .14s ease,transform .14s ease}
.row:hover{border-color:var(--line-1);box-shadow:var(--shadow-1);transform:translateY(-1px)}
.row.sel{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc) inset}
.row.gapup{background:var(--upbg)}.row.gapdn{background:var(--dnbg)}
.r1{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.r1 .nm{font-weight:500}.r1 .cd{color:var(--tx3);font-size:13px}
.r2{font-size:13px;color:var(--tx2);margin-top:3px}
.pill{font-size:12px;padding:2px 8px;border-radius:20px;white-space:nowrap}
.strong{background:var(--grnbg);color:var(--grn)}.mid{background:var(--ambbg);color:var(--amb)}
.watch{background:var(--grybg);color:var(--gry)}.drop{background:var(--redbg);color:var(--red)}
.thot{background:var(--accbg);color:var(--acc)}.tcool{background:var(--grybg);color:var(--tx2)}
.badge{font-size:11px;padding:2px 7px;border-radius:20px;margin-left:2px}
.bnew{background:var(--accbg);color:var(--acc)}.bupd{background:var(--ambbg);color:var(--amb)}
.bstale{background:var(--warnbg);color:var(--warn)}
.gap-up{background:var(--grnbg);color:var(--grn);font-weight:500}
.gap-dn{background:var(--redbg);color:var(--red);font-weight:500}
.gap-fade{background:var(--grybg);color:var(--tx2)}
.instar{background:var(--accbg);color:var(--acc)}
.pos{color:var(--ef-up)}.neg{color:var(--ef-down)}.mut{color:var(--tx2)}.acc{color:var(--acc)}
.detail{position:sticky;top:14px;background:var(--s2);border:1px solid var(--bd);border-radius:var(--r-md);box-shadow:var(--shadow-1);padding:16px 18px;min-height:420px}
.dh{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.dh .nm{font-size:19px;font-weight:500}
.vhero{display:flex;gap:16px;align-items:center;flex-wrap:wrap;background:var(--accbg);border:1px solid var(--accent-hair);border-left:4px solid var(--acc);border-radius:var(--r-md);padding:10px 14px;margin-top:12px}
.vhero .vpe{display:flex;align-items:baseline;gap:9px;white-space:nowrap}
.vhero .vk{font-size:12.5px;color:var(--tx2);font-weight:500}
.vhero .vnum{font-size:27px;font-weight:600;line-height:1;color:var(--acc);font-family:'Roboto Mono',monospace}
.vhero .vnum .vx{font-size:14px;font-weight:500}
.vhero .vsub{font-size:12.5px;color:var(--tx2);line-height:1.65;min-width:200px;flex:1}
.dsec{font-size:13px;color:var(--tx3);margin:14px 0 6px;font-weight:500}
.mgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}
.mrow{display:flex;justify-content:space-between;font-size:14px;padding:4px 0;border-bottom:1px dashed var(--bd)}
.mrow .k{color:var(--tx2)}
.dtext{font-size:13.5px;color:var(--tx2);background:var(--s1);border-radius:8px;padding:9px 10px;line-height:1.75}
.tline{font-size:13.5px;padding:3px 0}
.foot{color:var(--tx3);font-size:12px;margin-top:14px;line-height:1.75}
.empty{color:var(--tx3);font-size:13px;padding:6px 0}
"""

_JS = r"""
const fmtPct=(v,t)=>{if(v===null||v===undefined){if(t&&/扭亏|减亏/.test(t))return'扭亏';if(t&&/首亏|续亏|增亏/.test(t))return'增亏';return'—';}return(v>=0?'+':'')+v+'%';};
const sign=v=>(v===null||v===undefined)?'—':((v>=0?'+':'')+v);
const tierPill={'强':'strong','中':'mid','观察':'watch','剔除':'drop'};
function pill(cls,txt){return `<span class="pill ${cls}">${txt}</span>`;}
function gapBadge(s){
  if(s.gap_dir==='up'){const st=s.gap_status==='intact'?'未回补 D+'+(s.days_since_r||0):'已回补';return `<span class="badge ${s.gap_status==='intact'?'gap-up':'gap-fade'}">⬆断层${sign(s.gap_open_pct)}% ${st}</span>`;}
  if(s.gap_dir==='down'){const st=s.gap_status==='intact'?'未回补 D+'+(s.days_since_r||0):'已回补';return `<span class="badge ${s.gap_status==='intact'?'gap-dn':'gap-fade'}">⬇断层${sign(s.gap_open_pct)}% ${st}</span>`;}
  return '';
}
function badges(s){return s.badges.map(b=>{const c=b==='NEW'?'bnew':b==='更新'?'bupd':'bstale';return `<span class="badge ${c}">${b}</span>`;}).join('');}
function rowLine2(s){
  const parts=[s.type||'—','累计'+fmtPct(s.cum_yoy,s.type),'单季'+fmtPct(s.single_q_yoy,s.type)+(s.accelerating?'↑':'')];
  if(s.since_ann_pct!==null&&s.since_ann_pct!==undefined)parts.push('后'+sign(s.since_ann_pct)+'%');
  if(s.theme_name)parts.push(s.theme_name.split('/')[0].trim());
  if(s.ann_date)parts.push('披露'+s.ann_date.slice(4,6)+'-'+s.ann_date.slice(6));
  return parts.join(' · ');
}
function stockRow(s){
  const t=s.tier?pill(tierPill[s.tier],s.tier):'';
  const star=s.theme_hot?'<span class="badge instar">主线内</span>':'';
  const cls=s.gap_dir==='up'?' gapup':(s.gap_dir==='down'?' gapdn':'');
  return `<div class="row${cls}${SEL===s.ts_code?' sel':''}" data-c="${s.ts_code}">
    <div class="r1"><span class="nm">${s.name}</span><span class="cd">${s.ts_code.slice(0,6)}</span>${t}${gapBadge(s)}${star}${badges(s)}</div>
    <div class="r2">${rowLine2(s)}</div></div>`;
}
function trendPill(tr){
  const cls=tr.trend==='偏强'?'t-up':(tr.trend==='偏弱'?'t-dn':(tr.trend==='分歧'?'t-mix':'t-flat'));
  const arrow=tr.trend==='偏强'?'↑':(tr.trend==='偏弱'?'↓':(tr.trend==='分歧'?'⇅':'·'));
  return `<span class="trend ${cls}">${arrow}${tr.trend} 强${tr.strong.length}/弱${tr.weak.length}</span>`;
}
function judgedPill(tr){
  if(!tr||!tr.judged)return'';
  const j=tr.judged;
  const cls=j.direction==='向上'?'t-up':(j.direction==='向下'?'t-dn':(j.direction==='分化'?'t-mix':'t-flat'));
  return `<span class="trend ${cls}" title="${(j.cross_validation||'').replace(/"/g,'&quot;')}">判·${j.direction}${j.stale?'†':''}</span>`;
}
function filtered(){
  const q=document.getElementById('q').value.trim().toLowerCase();
  const tf=document.getElementById('tier').value, rf=document.getElementById('react').value;
  return DATA.stocks.filter(s=>{
    if(tf&&s.tier!==tf)return false;
    if(rf==='up'&&s.gap_dir!=='up')return false;
    if(rf==='down'&&s.gap_dir!=='down')return false;
    if(rf==='intact'&&!(s.gap_dir==='up'&&s.gap_status==='intact'))return false;
    if(rf==='muted'&&s.facet!=='muted')return false;
    if(rf==='intheme'&&!s.theme_hot)return false;
    if(q&&!(s.name.toLowerCase().includes(q)||s.ts_code.toLowerCase().includes(q)||(s.theme_name||'').toLowerCase().includes(q)))return false;
    return true;});
}
function renderList(){
  const mode=document.getElementById('grp').value;
  const rows=filtered();
  let h='';
  if(mode==='theme'){
    const seen=new Map();
    for(const s of rows){const k=s.theme_id||(s.tier?'__none__':'__unjudged__');if(!seen.has(k))seen.set(k,[]);seen.get(k).push(s);}
    const keys=[...seen.keys()].sort((a,b)=>{
      const ta=DATA.theme_trends[a],tb=DATA.theme_trends[b];
      const na=ta?ta.net:(a.startsWith('__')?-99:0), nb=tb?tb.net:(b.startsWith('__')?-99:0);
      return nb-na;});
    for(const k of keys){
      const grp=seen.get(k),s0=grp[0],tr=DATA.theme_trends[k];
      const label=k==='__none__'?'无归属主线':(k==='__unjudged__'?'未判':`${s0.theme_name} ${s0.theme_state||''}${s0.theme_stars?'★'+s0.theme_stars:''}`);
      h+=`<div class="ghead"><span>${label}</span><span style="display:flex;gap:4px;flex-wrap:wrap">${tr?trendPill(tr)+judgedPill(tr):`<span>${grp.length}</span>`}</span></div>`+grp.map(stockRow).join('');
    }
  }else if(mode==='ann'){
    const byDate=[...rows].sort((a,b)=>(b.ann_date||b.first_ann_date||'').localeCompare(a.ann_date||a.first_ann_date||''));
    const seen=new Map();
    for(const s of byDate){const d=s.ann_date||s.first_ann_date||'未披露日期';if(!seen.has(d))seen.set(d,[]);seen.get(d).push(s);}
    for(const [d,grp] of seen.entries()){
      const label=d==='未披露日期'?d:`${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6)}`;
      h+=`<div class="ghead"><span>${label}</span><span>${grp.length}</span></div>`+grp.map(stockRow).join('');
    }
  }else{h=rows.map(stockRow).join('');}
  document.getElementById('list').innerHTML=h||'<div class="empty">无匹配</div>';
  document.querySelectorAll('.row').forEach(el=>el.onclick=()=>select(el.dataset.c));
  if(rows.length&&!rows.some(s=>s.ts_code===SEL))select(rows[0].ts_code);
}
let SEL=null;
function select(code){SEL=code;document.querySelectorAll('.row').forEach(el=>el.classList.toggle('sel',el.dataset.c===code));renderDetail();}
function mrow(k,v,cls){return `<div class="mrow"><span class="k">${k}</span><span class="${cls||''}">${v}</span></div>`;}
const fmtYi=v=>(v===null||v===undefined)?'—':(Math.abs(v)>=100?v.toFixed(0):(Math.abs(v)>=10?v.toFixed(1):v.toFixed(2)));
function peHero(s){
  let big='—',note='';
  if(s.pe_ann!==null&&s.pe_ann!==undefined)big=`${s.pe_ann}<span class="vx">×</span>`;
  else if(s.pe_ann_note==='np_nonpositive')note='年化净利≤0（仍亏损），PE 无意义';
  else if(s.pe_ann_note==='mv_missing')note='未取到总市值（daily_basic），PE 缺失';
  else if(s.pe_ann_note==='np_missing')note='预告未披露净利区间，无法年化';
  else note='无估值证据（evidence 为旧版扫描，重跑 forecast_scan 后生成）';
  const asof=s.mv_asof?`(${s.mv_asof.slice(4,6)}-${s.mv_asof.slice(6)})`:'';
  const subs=[`总市值 ${s.total_mv_yi!==null&&s.total_mv_yi!==undefined?fmtYi(s.total_mv_yi)+'亿':'—'}${asof}`];
  if(s.ann_np_yi!==null&&s.ann_np_yi!==undefined)subs.push(`年化净利 ${fmtYi(s.ann_np_yi)}亿（${s.ann_label||'预告中值年化'}）`);
  if(s.pe_roll!==null&&s.pe_roll!==undefined)subs.push(`滚动PE ${s.pe_roll}×（上年年报+本期中值−上年同期）`);
  return `<div class="vhero"><div class="vpe"><span class="vk">年化PE·预告中值</span><span class="vnum">${big}</span></div><div class="vsub">${subs.join(' · ')}${note?`<br>${note}`:''}</div></div>`;
}
function memberLine(m){const c=m.gap_open_pct>=0?'pos':'neg';return `<span class="${c}">${m.name}${sign(m.gap_open_pct)}%${m.gap_status==='filled'?'(回补)':''}</span>`;}
function renderDetail(){
  const s=DATA.stocks.find(x=>x.ts_code===SEL);
  const el=document.getElementById('detail');
  if(!s){el.innerHTML='<div class="empty">点击左侧个股查看详情</div>';return;}
  const cls=v=>(v===null||v===undefined)?'mut':(v>=0?'pos':'neg');
  let h=`<div class="dh"><span class="nm">${s.name}</span><span class="cd mut">${s.ts_code}</span><span class="mut" style="font-size:12px">${s.type}</span>${s.tier?pill(tierPill[s.tier],s.tier):''}${gapBadge(s)}${badges(s)}</div>`;
  h+=peHero(s);
  h+=`<div class="klwrap" id="kl"></div>`;
  h+=`<div class="dsec">股价断层</div><div class="mgrid">`;
  h+=mrow('公告日跳空',s.gap_open_pct===null?'—':sign(s.gap_open_pct)+'%',cls(s.gap_open_pct));
  h+=mrow('公告后累计',s.since_ann_pct===null?'—':sign(s.since_ann_pct)+'%',cls(s.since_ann_pct));
  h+='</div>';
  // 行业趋势分析：所属主线内 强表现(向上断层) vs 弱表现(向下断层)
  h+=`<div class="dsec">所属主线 · 报告期行业趋势</div>`;
  const tr=s.theme_id?DATA.theme_trends[s.theme_id]:null;
  if(!tr){h+=`<div class="dtext">${s.theme_id?'':(s.tier?'无归属主线 —— 不纳入行业趋势聚合。':'未判分 —— 先跑 verdict.record 归属主线。')}</div>`;}
  else{
    h+=`<div class="tline">${tr.theme_name} ${tr.state||''}${tr.stars?'★'+tr.stars:''} ${trendPill(tr)}</div>`;
    if(tr.strong.length)h+=`<div class="tline"><span class="pos">强表现·向上断层 ${tr.strong.length}</span>：${tr.strong.map(memberLine).join('、')}</div>`;
    if(tr.weak.length)h+=`<div class="tline"><span class="neg">弱表现·向下断层 ${tr.weak.length}</span>：${tr.weak.map(memberLine).join('、')}</div>`;
    if(tr.neutral_n)h+=`<div class="tline mut">无断层 ${tr.neutral_n} 只</div>`;
    h+=`<div class="tline mut" style="font-size:11px">净方向=强−弱=${tr.net>=0?'+':''}${tr.net}（|净|≥${DATA.trend_net} 记偏强/偏弱；机械计数）</div>`;
    const j=tr.judged;
    if(j){
      h+=`<div class="tline" style="margin-top:4px">${judgedPill(tr)}${j.confidence?` <span class="mut" style="font-size:11px">confidence=${j.confidence}</span>`:''} <span class="mut" style="font-size:11px">判于 ${j.judged_at}（当时强${j.snap_strong}/弱${j.snap_weak}）${j.stale?' · 成员已变化，待复判':''}</span></div>`;
      if(j.strong_common)h+=`<div class="tline"><span class="pos">强侧归因</span>：${j.strong_common}</div>`;
      if(j.weak_common)h+=`<div class="tline"><span class="neg">弱侧归因</span>：${j.weak_common}</div>`;
      if(j.cross_validation)h+=`<div class="dtext" style="margin-top:3px">交叉验证：${j.cross_validation}</div>`;
    }else{
      h+=`<div class="tline mut" style="font-size:11px">行业趋势归因待判——verdict 判分时对该主线写 theme_trends（强/弱侧归因+交叉验证）。</div>`;
    }
  }
  h+=`<div class="dsec">业绩（预告中值口径）</div><div class="mgrid">`;
  h+=mrow('归母净利中值',s.np_median_yi!=null?s.np_median_yi+'亿':'—');
  h+=mrow('当年累计同比',fmtPct(s.cum_yoy,s.type),cls(s.cum_yoy));
  h+=mrow('单季度同比',fmtPct(s.single_q_yoy,s.type)+(s.accelerating?' ↑加速':''),cls(s.single_q_yoy));
  h+=mrow('环比',fmtPct(s.qoq,s.type),cls(s.qoq));
  h+=mrow('扣非净利(cninfo)',s.kf||'—');
  h+=mrow('营收'+(s.rev_period?`(${s.rev_period}实际)`:''),s.rev_yoy===null?'—':sign(s.rev_yoy)+'%',cls(s.rev_yoy));
  h+='</div>';
  const themeLine=s.theme_id?`<span class="acc">${s.match_confidence==='low'?'疑似 ':''}${s.theme_name}</span> <span class="pill ${s.theme_hot?'thot':'tcool'}">${s.theme_state||''}${s.theme_stars?'★'+s.theme_stars:''}</span>`:(s.tier?'<span class="mut">无归属主线（潜在未被市场发现）</span>':'<span class="mut">未判</span>');
  h+=`<div class="dsec">归属主线</div><div style="font-size:13px">${themeLine}</div>`;
  if(s.theme_rationale)h+=`<div class="dtext" style="margin-top:4px">${s.theme_rationale}</div>`;
  if(s.reason||s.caveat)h+=`<div class="dsec">判分</div><div class="dtext">${s.reason||''}${s.caveat?'<br>caveat: '+s.caveat:''}</div>`;
  if(s.change_reason)h+=`<div class="dsec">业绩变动原因（公司口径）</div><div class="dtext">${s.change_reason}</div>`;
  el.innerHTML=h;
  drawKline(document.getElementById('kl'),DATA.klines[s.ts_code],s);
}
function drawKline(el,bars,s){
  if(!bars||bars.length<2){el.innerHTML='<div class="empty">无K线数据（--no-price 或日线缓存为空）</div>';return;}
  const W=620,PH=210,VH=52,PADL=44,PADR=8,PADT=10,GAPV=14,H=PADT+PH+GAPV+VH+18;
  const n=bars.length,step=(W-PADL-PADR)/n,cw=Math.max(1.5,step*0.62);
  let lo=Infinity,hi=-Infinity,vmax=0;
  for(const b of bars){if(b[3]!==null&&b[3]<lo)lo=b[3];if(b[2]!==null&&b[2]>hi)hi=b[2];if(b[5]>vmax)vmax=b[5];}
  if(s.pre_ann_close!==null&&s.pre_ann_close!==undefined){lo=Math.min(lo,s.pre_ann_close);hi=Math.max(hi,s.pre_ann_close);}
  if(!isFinite(lo)||!isFinite(hi)){el.innerHTML='<div class="empty">无K线数据</div>';return;}
  vmax=Math.max(vmax,1);
  const pad=(hi-lo)*0.04||1;lo-=pad;hi+=pad;
  const y=p=>PADT+PH-(p-lo)/(hi-lo)*PH;
  const vy=v=>PADT+PH+GAPV+VH-(v/vmax)*VH;
  let g='';
  for(let i=0;i<4;i++){const p=lo+(hi-lo)*i/3,yy=y(p);
    g+=`<line x1="${PADL}" y1="${yy}" x2="${W-PADR}" y2="${yy}" stroke="var(--bd)" stroke-width="0.6"/>`+
       `<text x="${PADL-4}" y="${yy+3.5}" font-size="10" fill="var(--tx3)" text-anchor="end">${p.toFixed(p>=100?0:2)}</text>`;}
  let k='',v='',rIdx=-1,annIdx=-1;
  for(let i=0;i<n;i++){const b=bars[i],x=PADL+i*step+step/2;
    if(b[0]===s.reaction_date)rIdx=i;
    if(annIdx<0&&s.ann_date&&b[0]>=s.ann_date)annIdx=i;
    if(b[1]===null||b[2]===null||b[3]===null||b[4]===null)continue;
    const up=b[4]>=b[1],c=up?'var(--kup)':'var(--kdn)';
    k+=`<line x1="${x}" y1="${y(b[2])}" x2="${x}" y2="${y(b[3])}" stroke="${c}" stroke-width="1"/>`;
    const yo=y(Math.max(b[1],b[4])),yc=y(Math.min(b[1],b[4]));
    k+=`<rect x="${x-cw/2}" y="${yo}" width="${cw}" height="${Math.max(1,yc-yo)}" fill="${up?'none':c}" stroke="${c}" stroke-width="1"/>`;
    if(b[5])v+=`<rect x="${x-cw/2}" y="${vy(b[5])}" width="${cw}" height="${PADT+PH+GAPV+VH-vy(b[5])}" fill="${c}" opacity="0.55"/>`;}
  let m='';
  if(s.pre_ann_close!==null&&s.pre_ann_close!==undefined){const yy=y(s.pre_ann_close);
    m+=`<line x1="${PADL}" y1="${yy}" x2="${W-PADR}" y2="${yy}" stroke="var(--amb)" stroke-width="1" stroke-dasharray="4 3"/>`+
       `<text x="${W-PADR}" y="${yy-4}" font-size="10" fill="var(--amb)" text-anchor="end">公告前收盘 ${s.pre_ann_close}</text>`;}
  const anchorIdx=annIdx>=0?annIdx:rIdx;
  if(anchorIdx>=0){const markerIdx=Math.max(0,anchorIdx-1),x=PADL+markerIdx*step+step/2;
    m+=`<line x1="${x}" y1="${PADT}" x2="${x}" y2="${PADT+PH+GAPV+VH}" stroke="var(--acc)" stroke-width="1" stroke-dasharray="4 3"/>`+
       `<text x="${x+3}" y="${PADT+10}" font-size="10" fill="var(--acc)">公告日</text>`;}
  const d0=bars[0][0],d1=bars[n-1][0];
  const ax=`<text x="${PADL}" y="${H-4}" font-size="10" fill="var(--tx3)">${d0.slice(0,4)}-${d0.slice(4,6)}-${d0.slice(6)}</text>`+
    `<text x="${W-PADR}" y="${H-4}" font-size="10" fill="var(--tx3)" text-anchor="end">${d1.slice(0,4)}-${d1.slice(4,6)}-${d1.slice(6)}</text>`;
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto" role="img" aria-label="${s.name} 日K线，公告日标注在公告日前一交易日，并标注公告前收盘">${g}${k}${v}${m}${ax}</svg>`;
}
['q','tier','react','grp'].forEach(id=>document.getElementById(id).addEventListener('input',renderList));
renderList();
"""


def render_html(view: Dict[str, Any]) -> str:
    empty_note = ('<div class="foot">主线台账为空——归属与行业趋势需先运行 daily-market-sense 填充 theme 台账、再跑 verdict 判分归属。</div>'
                  if view["theme_registry_empty"] else "")
    ov = view.get("overview")
    if ov:
        stale_badge = '<span class="badge bstale">样本已更新，待复判</span>' if ov["stale"] else ""
        overview_html = ('<div class="ovw"><div class="ttl">报告期产业结构综述（模型判断，落台账）'
                         f'<span>判于 {ov["judged_at"]} · 当时样本 {ov["snap_total"]} 家</span>{stale_badge}</div>'
                         f'<div class="ovtx">{escape(ov["text"])}</div></div>')
    else:
        overview_html = ""
    data_json = json.dumps({
        "pos_split": view["pos_split"], "trend_net": view["trend_net"],
        "stocks": view["stocks"], "theme_trends": view["theme_trends"], "klines": view["klines"],
    }, ensure_ascii=False, separators=(",", ":"))
    shared_theme = _load_shared_theme("default")
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{view['period_label']} 业绩预告 · 报告期观察</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;600;700&family=Roboto+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{shared_theme}\n{_CSS}</style></head>
<body><main class="page"><div class="doc-head"><span class="dh-title">Earnings Forecast</span><span class="dh-meta">{view['period_label']} · updated {view['updated_at']}</span></div>
<section class="section report earnings-report"><div class="wrap">
<div style="display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px">
<div><h1>{view['period_label']} 业绩预告 · 报告期观察</h1><div class="sub">end_date {view['period']} · 业绩 × 股价断层 × 主线行业趋势 · 列表+详情 · 一期一页增量更新</div></div>
<div class="sub">更新于 {view['updated_at']}</div></div>
{overview_html}
<div class="ctrl">
<input id="q" placeholder="搜索 名称 / 代码 / 主线">
<select id="grp"><option value="ann">按发布时间排序</option><option value="theme">按主线分组</option><option value="flat">平铺</option></select>
<select id="react"><option value="">全部反应</option><option value="up">向上断层(强)</option><option value="down">向下断层(弱)</option><option value="intact">跳空未回补</option><option value="muted">未反应</option><option value="intheme">主线内</option></select>
<select id="tier"><option value="">全部分档</option><option>强</option><option>中</option><option>观察</option><option>剔除</option></select>
</div>
<div class="panes">
<div class="list" id="list"></div>
<div class="detail" id="detail"><div class="empty">点击左侧个股查看详情</div></div>
</div>
<div class="foot">净利=预告中值 · 断层以首次披露日为锚(预告多在披露日前一交易日盘后发出，反应落在披露日当天)：跳空=公告日当天开盘 vs 公告日前一交易日收盘；向上=业绩超预期跳空(强表现)、向下=不及预期跳空下跌(弱表现)；未回补=其后价格未回到公告前收盘另一侧，D+n=断层后交易日数(新断层未经检验) · 行业趋势=报告期内按主线聚合成员的断层方向(强表现向上/弱表现向下)自下而上归纳：↑↓⇅为机械计数，「判·方向」为模型对强/弱成员变动原因的归因交叉验证(落 verdict 台账，†=成员已变化待复判) · 页首产业结构综述=模型基于全样本行业聚合(industry_summary，含负向预告)的结构判断，样本随披露累积、综述会随之更新 · K线使用前复权(qfq)口径，红涨绿跌，蓝虚线=公告日标注(落在公告日前一交易日，如公告日7.3则标7.2)、橙虚线=公告前收盘 · 年化PE=最新交易日总市值÷年化净利，年化净利=预告中值÷报告期季数×4(简单年化，未调季节性，年报预告即中值)；滚动PE分母=上年年报实际+本期中值−上年同期实际(季节性对照)；年化净利≤0不给PE，扭亏小基数会把PE推到数百倍、一次性损益会让PE虚低，与行情软件静态PE/PE-TTM口径不同 · 归属主线由模型语义匹配 daily-market-sense 主线台账 · 仅作观察、不含买卖建议</div>
{empty_note}
</div>
<script>const DATA={data_json};{_JS}</script>
</section></main></body></html>"""


def _load_shared_theme(theme: str) -> str:
    """Load the canonical shared html_report theme in dev and synced packages."""
    script_dir = Path(__file__).resolve().parent
    bundled = script_dir / "_shared" / "html_report" / "themes" / f"{theme}.css"
    dev = script_dir.parents[2] / "shared" / "html_report" / "themes" / f"{theme}.css"
    path = bundled if bundled.is_file() else dev
    if not path.is_file():
        raise FileNotFoundError(f"shared html_report theme 不存在: {path}")
    return path.read_text(encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render one period's earnings-forecast HTML page (master-detail).")
    ap.add_argument("--period")
    ap.add_argument("--evidence")
    ap.add_argument("--enrich")
    ap.add_argument("--pos-split", type=float, default=POS_SPLIT,
                    help="公告前一年分位阈值：>=为趋势加速型、<为低位启动型(默认 50)。")
    ap.add_argument("--muted-max", type=float, default=MUTED_MAX_PCT,
                    help="未反应观察：已判强/中、无断层且 |公告后累计| < N%%(默认 3)。")
    ap.add_argument("--kline-days", type=int, default=KLINE_BARS, help="详情页K线根数(默认 130)。")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    today = dt.date.today()
    period = args.period or latest_quarter_end(today)
    quarter_of(period)

    ev_path = args.evidence or os.path.join("reports", f"forecast_scan_{period}.json")
    evidence = _load_json(ev_path)
    if evidence is None:
        print(json.dumps({"error": f"evidence 不存在: {ev_path}，请先运行 forecast_scan.py。"}, ensure_ascii=False))
        return 2
    enrich = _load_json(args.enrich or os.path.join("reports", f"cninfo_enrich_{period}.json"))

    store = Store()
    codes = [str(s["ts_code"]) for s in evidence.get("stocks", [])]
    bars_map = store.load_bars_many(codes)
    view = build_view(period, evidence, enrich, store.load_verdicts(period),
                      store.load_theme_registry(), store.load_theme_latest_state(),
                      store.load_theme_trends(period), store.load_period_overview(period),
                      bars_map, today,
                      pos_split=args.pos_split, muted_max=args.muted_max, kline_bars=args.kline_days)
    html = render_html(view)

    out_path = args.out or os.path.join("reports", f"forecast_{period}.html")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    trends = view["theme_trends"]
    print(json.dumps({"period": period, "stocks": len(view["stocks"]),
                      "klines": len(view["klines"]), "themes_with_trend": len(trends),
                      "themes_judged": sum(1 for t in trends.values() if t.get("judged")),
                      "overview": bool(view.get("overview")),
                      "out": out_path}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
