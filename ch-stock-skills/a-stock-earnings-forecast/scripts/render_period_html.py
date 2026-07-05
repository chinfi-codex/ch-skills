#!/usr/bin/env python3
"""Render one self-contained HTML page per report period (master-detail view).

Layout: KPI strip → filter bar → two panes. Left pane is the stock LIST,
groupable by 断层 facets (趋势加速型 / 低位启动型 / 未反应…) or by 主线 or flat;
right pane is the DETAIL for the selected stock — full metrics plus an embedded
K-line (candles + volume) with the announcement marked (vertical line at the
reaction day, dashed horizontal at the pre-announcement close) so the 净利润断层
is visible at a glance.

Everything comes from the evidence pack + cninfo enrichment + verdict ledger +
live theme states + the skill's own bar cache; the renderer projects, it does
not judge. Self-contained single file, no CDN, dark-mode aware.

Usage:
    python3 scripts/render_period_html.py --period 20260630
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional

from store import Store

_MMDD_Q = {"0331": 1, "0630": 2, "0930": 3, "1231": 4}
_HOT_STATES = {"低位启动", "在场候选", "主线确认", "再聚焦", "修复"}
NEW_DAYS = 5           # first disclosed within N calendar days → NEW
DRIFT_PCT = 20.0       # verdict considered stale if cum-YoY moved > N points
POS_SPLIT = 50.0       # 公告前一年分位 >= split → 趋势加速型；< split → 低位启动型
MUTED_MAX_PCT = 3.0    # 公告后累计涨幅 < N% 且无断层 → 未反应观察
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
    """[[trade_date, open, high, low, close, vol], ...] ascending, last `keep`."""
    rows = []
    for b in bars[-keep:]:
        if b.get("close") is None:
            continue
        rows.append([
            str(b["trade_date"]),
            b.get("open"), b.get("high"), b.get("low"), b.get("close"),
            b.get("vol"),
        ])
    return rows


def build_view(period: str, evidence: Dict[str, Any], enrich: Optional[Dict[str, Any]],
               verdicts: Dict[str, Dict[str, Any]], themes: Dict[str, Dict[str, Any]],
               states: Dict[str, Dict[str, Any]], bars_map: Dict[str, List[Dict[str, Any]]],
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
        pre_pos = _pct(pr.get("pre_pos_1y_pct"))
        rec = {
            "ts_code": ts_code,
            "name": s.get("name", ""),
            "type": s.get("type", ""),
            "cum_yoy": cum,
            "single_q_yoy": _pct(pg.get("single_q_yoy_pct")),
            "qoq": _pct(pg.get("qoq_pct")),
            "np_median_yi": s.get("net_profit", {}).get("median_yi"),
            "kf": _kf_display(en_idx.get(ts_code)),
            "rev_yoy": _pct(rev.get("cum_yoy_pct")),
            "rev_period": (rev or {}).get("period_label"),
            "accelerating": bool(s.get("flags", {}).get("accelerating")),
            "turnaround": bool(s.get("flags", {}).get("turnaround")),
            "change_reason": s.get("change_reason", ""),
            "gap_open_pct": _pct(pr.get("gap_open_pct")),
            "r_day_pct": _pct(pr.get("r_day_pct")),
            "r_vol_ratio": _pct(pr.get("r_vol_ratio")),
            "since_ann_pct": _pct(pr.get("since_ann_pct")),
            "gap_status": gap_status,
            "pre_pos": pre_pos,
            "pre_mom": _pct(pr.get("pre_mom_20d_pct")),
            "days_since_r": pr.get("trading_days_since_r"),
            "reaction_date": pr.get("reaction_date"),
            "pre_ann_date": pr.get("pre_ann_date"),
            "pre_ann_close": pr.get("pre_ann_close"),
            "latest_close": pr.get("latest_close"),
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
            "first_ann_date": first_ann,
            "ann_date": ann,
        }
        # deterministic list facet (opportunity judgment stays with the model)
        if gap_status in ("intact", "filled"):
            if pre_pos is None:
                rec["facet"] = "gap_unpos"
            elif pre_pos >= pos_split:
                rec["facet"] = "gap_trend"
            else:
                rec["facet"] = "gap_low"
        elif tier in ("强", "中") and gap_status == "none" \
                and rec["since_ann_pct"] is not None and rec["since_ann_pct"] < muted_max:
            rec["facet"] = "muted"
        else:
            rec["facet"] = "other"
        stocks.append(rec)
        kl = _compact_kline(bars_map.get(ts_code, []), kline_bars)
        if kl:
            klines[ts_code] = kl

    facet_rank = {"gap_trend": 0, "gap_low": 1, "gap_unpos": 2, "muted": 3, "other": 4}
    stocks.sort(key=lambda s: (facet_rank[s["facet"]], not s["theme_hot"],
                               -(s["gap_open_pct"] or -999), -(s["cum_yoy"] or -999)))

    judged = [s for s in stocks if s["tier"] is not None]
    gap_stocks = [s for s in stocks if s["facet"].startswith("gap_")]
    kpis = {
        "disclosed": len(stocks),
        "new_today": sum(1 for s in stocks if "NEW" in s["badges"]),
        "gap_total": len(gap_stocks),
        "gap_trend": sum(1 for s in stocks if s["facet"] == "gap_trend"),
        "gap_low": sum(1 for s in stocks if s["facet"] == "gap_low"),
        "gap_intact": sum(1 for s in gap_stocks if s["gap_status"] == "intact"),
        "muted": sum(1 for s in stocks if s["facet"] == "muted"),
        "with_theme": sum(1 for s in judged if s["theme_id"]),
        "no_theme": sum(1 for s in judged if not s["theme_id"]),
        "tier_strong": sum(1 for s in stocks if s["tier"] == "强"),
        "tier_mid": sum(1 for s in stocks if s["tier"] == "中"),
        "tier_watch": sum(1 for s in stocks if s["tier"] == "观察"),
        "unjudged": sum(1 for s in stocks if s["tier"] is None),
        "stale": sum(1 for s in stocks if s["stale"]),
    }
    return {
        "period": period,
        "period_label": period_label(period),
        "updated_at": today.strftime("%Y-%m-%d"),
        "theme_registry_empty": len(themes) == 0,
        "pos_split": pos_split,
        "muted_max": muted_max,
        "kpis": kpis,
        "stocks": stocks,
        "klines": klines,
    }


# --------------------------------------------------------------------------- #
_CSS = """
:root{--s0:#faf9f5;--s1:#f2f0e9;--s2:#fff;--tx:#1a1a18;--tx2:#5f5e5a;--tx3:#8a897f;
--bd:#e5e3da;--acc:#185fa5;--accbg:#e6f1fb;--pos:#3b6d11;--neg:#a32d2d;--amb:#854f0b;--ambbg:#faeeda;
--grn:#3b6d11;--grnbg:#eaf3de;--gry:#5f5e5a;--grybg:#f1efe8;--red:#a32d2d;--redbg:#fcebeb;
--kup:#c2453e;--kdn:#1d9e75}
@media (prefers-color-scheme:dark){:root{--s0:#26251f;--s1:#2f2e27;--s2:#33322b;--tx:#ece9e0;--tx2:#b4b2a9;--tx3:#888780;
--bd:#44443f;--acc:#85b7eb;--accbg:#0c447c;--pos:#97c459;--neg:#f09595;--amb:#fac775;--ambbg:#633806;
--grn:#c0dd97;--grnbg:#27500a;--gry:#b4b2a9;--grybg:#3a3a35;--red:#f09595;--redbg:#501313;
--kup:#e06c66;--kdn:#5dcaa5}}
*{box-sizing:border-box}body{margin:0;background:var(--s0);color:var(--tx);
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
font-size:14px;line-height:1.6}.wrap{max-width:1200px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:21px;font-weight:500;margin:0}.sub{color:var(--tx2);font-size:13px;margin-top:3px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:10px;margin:18px 0 14px}
.kpi{background:var(--s1);border-radius:8px;padding:10px 12px}.kpi .l{font-size:12px;color:var(--tx2);margin-bottom:4px}
.kpi .v{font-size:22px;font-weight:500;line-height:1}.kpi .s{font-size:11px;color:var(--tx3);margin-top:3px}
.ctrl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 12px}
input,select{font:inherit;font-size:13px;padding:6px 9px;border:1px solid var(--bd);border-radius:8px;background:var(--s2);color:var(--tx)}
input{flex:1;min-width:130px}
.panes{display:grid;grid-template-columns:minmax(330px,42%) 1fr;gap:14px;align-items:start}
@media (max-width:860px){.panes{grid-template-columns:1fr}.detail{position:static!important}}
.list{min-width:0}
.ghead{font-size:12px;color:var(--tx2);font-weight:500;background:var(--s1);border-radius:8px;padding:5px 10px;margin:10px 0 6px;display:flex;justify-content:space-between}
.ghead:first-child{margin-top:0}
.row{background:var(--s2);border:1px solid var(--bd);border-radius:10px;padding:8px 11px;margin-bottom:6px;cursor:pointer}
.row:hover{border-color:var(--tx3)}
.row.sel{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc) inset}
.r1{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.r1 .nm{font-weight:500}.r1 .cd{color:var(--tx3);font-size:12px}
.r2{font-size:12px;color:var(--tx2);margin-top:2px}
.pill{font-size:11px;padding:1px 8px;border-radius:20px;white-space:nowrap}
.strong{background:var(--grnbg);color:var(--grn)}.mid{background:var(--ambbg);color:var(--amb)}
.watch{background:var(--grybg);color:var(--gry)}.drop{background:var(--redbg);color:var(--red)}
.thot{background:var(--accbg);color:var(--acc)}.tcool{background:var(--grybg);color:var(--tx2)}
.badge{font-size:10px;padding:1px 6px;border-radius:20px;margin-left:2px}
.bnew{background:var(--accbg);color:var(--acc)}.bupd{background:var(--ambbg);color:var(--amb)}
.bstale{background:var(--redbg);color:var(--red)}
.g-intact{background:var(--grnbg);color:var(--grn)}.g-filled{background:var(--redbg);color:var(--red)}
.instar{background:var(--accbg);color:var(--acc)}
.pos{color:var(--pos)}.neg{color:var(--neg)}.mut{color:var(--tx2)}.acc{color:var(--acc)}
.detail{position:sticky;top:14px;background:var(--s2);border:1px solid var(--bd);border-radius:12px;padding:14px 16px;min-height:420px}
.dh{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.dh .nm{font-size:17px;font-weight:500}
.dsec{font-size:12px;color:var(--tx3);margin:14px 0 6px;font-weight:500}
.mgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}
.mrow{display:flex;justify-content:space-between;font-size:13px;padding:3px 0;border-bottom:1px dashed var(--bd)}
.mrow .k{color:var(--tx2)}
.dtext{font-size:12.5px;color:var(--tx2);background:var(--s1);border-radius:8px;padding:8px 10px;line-height:1.7}
.klwrap{margin-top:6px}
.foot{color:var(--tx3);font-size:11px;margin-top:14px;line-height:1.7}
.empty{color:var(--tx3);font-size:12px;padding:6px 0}
"""

_JS = r"""
const fmtPct=(v,t)=>{if(v===null||v===undefined){if(t&&/扭亏|减亏/.test(t))return'扭亏';if(t&&/首亏|续亏|增亏/.test(t))return'增亏';return'—';}return(v>=0?'+':'')+v+'%';};
const sign=v=>(v===null||v===undefined)?'—':((v>=0?'+':'')+v);
const tierPill={'强':'strong','中':'mid','观察':'watch','剔除':'drop'};
const FACETS=[['gap_trend','趋势加速型断层 · 公告前高位续升'],['gap_low','低位启动型断层 · 业绩点火'],['gap_unpos','断层 · 位置数据不足(次新)'],['muted','业绩强但股价未反应 · 潜在未定价'],['other','其余']];
function pill(cls,txt){return `<span class="pill ${cls}">${txt}</span>`;}
function gapBadge(s){
  if(s.gap_status==='intact')return `<span class="badge g-intact">未回补 D+${s.days_since_r||0}</span>`;
  if(s.gap_status==='filled')return `<span class="badge g-filled">已回补</span>`;
  return '';
}
function badges(s){return s.badges.map(b=>{const c=b==='NEW'?'bnew':b==='更新'?'bupd':'bstale';return `<span class="badge ${c}">${b}</span>`;}).join('');}
function rowLine2(s){
  const parts=[s.type||'—'];
  parts.push('累计'+fmtPct(s.cum_yoy,s.type));
  parts.push('单季'+fmtPct(s.single_q_yoy,s.type)+(s.accelerating?'↑':''));
  if(s.gap_status==='intact'||s.gap_status==='filled')parts.push('跳空'+sign(s.gap_open_pct)+'%');
  else if(s.since_ann_pct!==null&&s.since_ann_pct!==undefined)parts.push('后'+sign(s.since_ann_pct)+'%');
  if(s.theme_name)parts.push(s.theme_name.split('/')[0].trim());
  return parts.join(' · ');
}
function stockRow(s){
  const t=s.tier?pill(tierPill[s.tier],s.tier):'';
  const star=s.theme_hot?'<span class="badge instar">主线内</span>':'';
  return `<div class="row${SEL===s.ts_code?' sel':''}" data-c="${s.ts_code}">
    <div class="r1"><span class="nm">${s.name}</span><span class="cd">${s.ts_code.slice(0,6)}</span>${t}${gapBadge(s)}${star}${badges(s)}</div>
    <div class="r2">${rowLine2(s)}</div></div>`;
}
function filtered(){
  const q=document.getElementById('q').value.trim().toLowerCase();
  const tf=document.getElementById('tier').value, rf=document.getElementById('react').value;
  return DATA.stocks.filter(s=>{
    if(tf&&s.tier!==tf)return false;
    if(rf==='gap'&&!s.facet.startsWith('gap_'))return false;
    if(rf==='intact'&&s.gap_status!=='intact')return false;
    if(rf==='muted'&&s.facet!=='muted')return false;
    if(rf==='intheme'&&!s.theme_hot)return false;
    if(q&&!(s.name.toLowerCase().includes(q)||s.ts_code.toLowerCase().includes(q)||(s.theme_name||'').toLowerCase().includes(q)))return false;
    return true;});
}
function renderList(){
  const mode=document.getElementById('grp').value;
  const rows=filtered();
  let h='';
  if(mode==='facet'){
    for(const [key,label] of FACETS){
      const grp=rows.filter(s=>s.facet===key);
      if(!grp.length)continue;
      h+=`<div class="ghead"><span>${label}</span><span>${grp.length}</span></div>`+grp.map(stockRow).join('');
    }
  }else if(mode==='theme'){
    const seen=new Map();
    for(const s of rows){const k=s.theme_id||(s.tier?'__none__':'__unjudged__');if(!seen.has(k))seen.set(k,[]);seen.get(k).push(s);}
    const keys=[...seen.keys()].sort((a,b)=>{
      const sa=a.startsWith('__')?-1:(seen.get(a)[0].theme_stars||0), sb=b.startsWith('__')?-1:(seen.get(b)[0].theme_stars||0);
      return sb-sa;});
    for(const k of keys){
      const grp=seen.get(k); const s0=grp[0];
      const label=k==='__none__'?'无归属主线':(k==='__unjudged__'?'未判':`${s0.theme_name} ${s0.theme_state||''}${s0.theme_stars?'★'+s0.theme_stars:''}`);
      h+=`<div class="ghead"><span>${label}</span><span>${grp.length}</span></div>`+grp.map(stockRow).join('');
    }
  }else{
    h=rows.map(stockRow).join('');
  }
  document.getElementById('list').innerHTML=h||'<div class="empty">无匹配</div>';
  document.querySelectorAll('.row').forEach(el=>el.onclick=()=>select(el.dataset.c));
  if(rows.length&&!rows.some(s=>s.ts_code===SEL))select(rows[0].ts_code);
}
let SEL=null;
function select(code){
  SEL=code;
  document.querySelectorAll('.row').forEach(el=>el.classList.toggle('sel',el.dataset.c===code));
  renderDetail();
}
function mrow(k,v,cls){return `<div class="mrow"><span class="k">${k}</span><span class="${cls||''}">${v}</span></div>`;}
function renderDetail(){
  const s=DATA.stocks.find(x=>x.ts_code===SEL);
  const el=document.getElementById('detail');
  if(!s){el.innerHTML='<div class="empty">点击左侧个股查看详情</div>';return;}
  const cls=v=>(v===null||v===undefined)?'mut':(v>=0?'pos':'neg');
  const themeLine=s.theme_id?`<span class="acc">${s.match_confidence==='low'?'疑似 ':''}${s.theme_name}</span> <span class="pill ${s.theme_hot?'thot':'tcool'}">${s.theme_state||''}${s.theme_stars?'★'+s.theme_stars:''}</span>`:(s.tier?'<span class="mut">无归属主线（潜在未被市场发现）</span>':'<span class="mut">未判</span>');
  let h=`<div class="dh"><span class="nm">${s.name}</span><span class="cd mut">${s.ts_code}</span><span class="mut" style="font-size:12px">${s.type}</span>${s.tier?pill(tierPill[s.tier],s.tier):''}${gapBadge(s)}${badges(s)}</div>`;
  h+=`<div class="klwrap" id="kl"></div>`;
  h+=`<div class="dsec">股价反应（首次披露 ${s.first_ann_date||'—'}）</div><div class="mgrid">`;
  h+=mrow('公告次日跳空',s.gap_open_pct===null?'—':sign(s.gap_open_pct)+'%',cls(s.gap_open_pct));
  h+=mrow('反应日全天',s.r_day_pct===null?'—':sign(s.r_day_pct)+'%',cls(s.r_day_pct));
  h+=mrow('反应日量比',s.r_vol_ratio?s.r_vol_ratio+'x':'—');
  h+=mrow('公告后累计',s.since_ann_pct===null?'—':sign(s.since_ann_pct)+'%',cls(s.since_ann_pct));
  h+=mrow('断层状态',s.gap_status==='intact'?`未回补 D+${s.days_since_r||0}`:(s.gap_status==='filled'?'已回补':(s.gap_status==='pending'?'待反应':'无跳空')),s.gap_status==='intact'?'pos':(s.gap_status==='filled'?'neg':'mut'));
  h+=mrow('公告前位置',s.pre_pos===null?'—':s.pre_pos+'% 年内分位');
  h+=mrow('公告前20日',s.pre_mom===null?'—':sign(s.pre_mom)+'%',cls(s.pre_mom));
  h+=mrow('公告前收盘',s.pre_ann_close??'—');
  h+='</div>';
  h+=`<div class="dsec">业绩（预告中值口径）</div><div class="mgrid">`;
  h+=mrow('归母净利中值',s.np_median_yi!=null?s.np_median_yi+'亿':'—');
  h+=mrow('当年累计同比',fmtPct(s.cum_yoy,s.type),cls(s.cum_yoy));
  h+=mrow('单季度同比',fmtPct(s.single_q_yoy,s.type)+(s.accelerating?' ↑加速':''),cls(s.single_q_yoy));
  h+=mrow('环比',fmtPct(s.qoq,s.type),cls(s.qoq));
  h+=mrow('扣非净利(cninfo)',s.kf||'—');
  h+=mrow('营收'+(s.rev_period?`(${s.rev_period} 实际)`:''),s.rev_yoy===null?'—':sign(s.rev_yoy)+'%',cls(s.rev_yoy));
  h+='</div>';
  h+=`<div class="dsec">归属主线</div><div style="font-size:13px">${themeLine}</div>`;
  if(s.theme_rationale)h+=`<div class="dtext" style="margin-top:4px">${s.theme_rationale}</div>`;
  if(s.reason||s.caveat){h+=`<div class="dsec">判分</div><div class="dtext">${s.reason||''}${s.caveat?'<br>caveat: '+s.caveat:''}</div>`;}
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
  let k='',v='';
  let rIdx=-1;
  for(let i=0;i<n;i++){
    const b=bars[i],x=PADL+i*step+step/2;
    if(b[0]===s.reaction_date)rIdx=i;
    if(b[1]===null||b[2]===null||b[3]===null||b[4]===null)continue;
    const up=b[4]>=b[1],c=up?'var(--kup)':'var(--kdn)';
    k+=`<line x1="${x}" y1="${y(b[2])}" x2="${x}" y2="${y(b[3])}" stroke="${c}" stroke-width="1"/>`;
    const yo=y(Math.max(b[1],b[4])),yc=y(Math.min(b[1],b[4]));
    k+=`<rect x="${x-cw/2}" y="${yo}" width="${cw}" height="${Math.max(1,yc-yo)}" fill="${up?'none':c}" stroke="${c}" stroke-width="1"/>`;
    if(b[5])v+=`<rect x="${x-cw/2}" y="${vy(b[5])}" width="${cw}" height="${PADT+PH+GAPV+VH-vy(b[5])}" fill="${c}" opacity="0.55"/>`;
  }
  let m='';
  if(s.pre_ann_close!==null&&s.pre_ann_close!==undefined){
    const yy=y(s.pre_ann_close);
    m+=`<line x1="${PADL}" y1="${yy}" x2="${W-PADR}" y2="${yy}" stroke="var(--amb)" stroke-width="1" stroke-dasharray="4 3"/>`+
       `<text x="${W-PADR}" y="${yy-4}" font-size="10" fill="var(--amb)" text-anchor="end">公告前收盘 ${s.pre_ann_close}</text>`;
  }
  if(rIdx>=0){
    const x=PADL+rIdx*step+step/2;
    m+=`<line x1="${x}" y1="${PADT}" x2="${x}" y2="${PADT+PH+GAPV+VH}" stroke="var(--acc)" stroke-width="1" stroke-dasharray="4 3"/>`+
       `<text x="${x+3}" y="${PADT+10}" font-size="10" fill="var(--acc)">公告反应日</text>`;
  }
  const d0=bars[0][0],d1=bars[n-1][0];
  const ax=`<text x="${PADL}" y="${H-4}" font-size="10" fill="var(--tx3)">${d0.slice(0,4)}-${d0.slice(4,6)}-${d0.slice(6)}</text>`+
    `<text x="${W-PADR}" y="${H-4}" font-size="10" fill="var(--tx3)" text-anchor="end">${d1.slice(0,4)}-${d1.slice(4,6)}-${d1.slice(6)}</text>`;
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto" role="img" aria-label="${s.name} 日K线，标注业绩预告反应日与公告前收盘">${g}${k}${v}${m}${ax}</svg>`;
}
['q','tier','react','grp'].forEach(id=>document.getElementById(id).addEventListener('input',renderList));
renderList();
"""


def render_html(view: Dict[str, Any]) -> str:
    k = view["kpis"]
    empty_note = ('<div class="foot">主线台账为空——归属列需先运行 daily-market-sense 填充 theme 台账。</div>'
                  if view["theme_registry_empty"] else "")
    kpi = lambda l, v, s: f'<div class="kpi"><div class="l">{l}</div><div class="v">{v}</div><div class="s">{s}</div></div>'
    kpis_html = "".join([
        kpi("已披露", k["disclosed"], f'今日新增 {k["new_today"]}'),
        kpi("断层", k["gap_total"], f'加速{k["gap_trend"]}·低位{k["gap_low"]}'),
        kpi("未回补", k["gap_intact"], "断层保持中"),
        kpi("未反应", k["muted"], "强/中但价未动"),
        kpi("已归属主线", k["with_theme"], f'无归属 {k["no_theme"]}'),
        kpi("分档", f'{k["tier_strong"]}·{k["tier_mid"]}·{k["tier_watch"]}', f'待判{k["unjudged"]}·复判{k["stale"]}'),
    ])
    data_json = json.dumps({
        "pos_split": view["pos_split"], "muted_max": view["muted_max"],
        "stocks": view["stocks"], "klines": view["klines"],
    }, ensure_ascii=False, separators=(",", ":"))
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{view['period_label']} 业绩预告 · 报告期观察</title><style>{_CSS}</style></head>
<body><div class="wrap">
<div style="display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px">
<div><h1>{view['period_label']} 业绩预告 · 报告期观察</h1><div class="sub">end_date {view['period']} · 业绩 × 股价断层 × 主线 · 列表+详情 · 一期一页增量更新</div></div>
<div class="sub">更新于 {view['updated_at']}</div></div>
<div class="kpis">{kpis_html}</div>
<div class="ctrl">
<input id="q" placeholder="搜索 名称 / 代码 / 主线">
<select id="grp"><option value="facet">按断层分组</option><option value="theme">按主线分组</option><option value="flat">平铺</option></select>
<select id="react"><option value="">全部反应</option><option value="gap">有断层</option><option value="intact">断层未回补</option><option value="muted">未反应</option><option value="intheme">主线内</option></select>
<select id="tier"><option value="">全部分档</option><option>强</option><option>中</option><option>观察</option><option>剔除</option></select>
</div>
<div class="panes">
<div class="list" id="list"></div>
<div class="detail" id="detail"><div class="empty">点击左侧个股查看详情</div></div>
</div>
<div class="foot">净利=预告中值 · 断层以首次披露日为锚：跳空=公告后首个交易日开盘 vs 公告前收盘，未回补=其后最低价未跌破公告前收盘，D+n=断层后交易日数（新断层未经时间检验）· 公告前位置=近一年高低区间分位（两型按 {view['pos_split']:.0f} 分位机械二分）· K线红涨绿跌，蓝虚线=公告反应日、橙虚线=公告前收盘 · 归属主线由模型语义匹配 daily-market-sense 主线台账 · 断层且在主线内为高胜率锚，仅作观察、不含买卖建议</div>
{empty_note}
</div>
<script>const DATA={data_json};{_JS}</script>
</body></html>"""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render one period's earnings-forecast HTML page (master-detail).")
    ap.add_argument("--period")
    ap.add_argument("--evidence")
    ap.add_argument("--enrich")
    ap.add_argument("--pos-split", type=float, default=POS_SPLIT,
                    help="公告前一年分位阈值：>=为趋势加速型、<为低位启动型(默认 50)。")
    ap.add_argument("--muted-max", type=float, default=MUTED_MAX_PCT,
                    help="未反应观察的公告后累计涨幅上限%%（默认 3）。")
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
                      store.load_theme_registry(), store.load_theme_latest_state(), bars_map, today,
                      pos_split=args.pos_split, muted_max=args.muted_max, kline_bars=args.kline_days)
    html = render_html(view)

    out_path = args.out or os.path.join("reports", f"forecast_{period}.html")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(json.dumps({"period": period, "stocks": len(view["stocks"]),
                      "klines": len(view["klines"]), "gap": view["kpis"]["gap_total"],
                      "muted": view["kpis"]["muted"], "out": out_path}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
