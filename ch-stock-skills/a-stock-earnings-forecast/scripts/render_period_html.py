#!/usr/bin/env python3
"""Render one self-contained HTML page per report period (incremental view).

Reads the deterministic evidence pack + cninfo enrichment + the model's verdict
ledger (tier + 主线归属) + the live daily-market-sense theme state, and projects
them into a single `reports/forecast_<period>.html`. Re-rendered from the
accumulated cache each run — the page grows as disclosures accrue; NEW / 更新 /
待复判 markers surface what changed. This is a rendering step only: it adds no
new judgment (tiers and theme attribution come from the verdict ledger).

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
# Theme lifecycle states that mean the mainline is currently "in play" vs fading.
_HOT_STATES = {"低位启动", "在场候选", "主线确认", "再聚焦", "修复"}
NEW_DAYS = 5           # first disclosed within N calendar days → NEW
DRIFT_PCT = 20.0       # verdict considered stale if cum-YoY moved > N points


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


def build_view(period: str, evidence: Dict[str, Any], enrich: Optional[Dict[str, Any]],
               verdicts: Dict[str, Dict[str, Any]], themes: Dict[str, Dict[str, Any]],
               states: Dict[str, Dict[str, Any]], today: dt.date) -> Dict[str, Any]:
    en_idx = {str(s["ts_code"]): s for s in (enrich or {}).get("stocks", []) if s.get("found")}

    stocks: List[Dict[str, Any]] = []
    for s in evidence.get("stocks", []):
        ts_code = str(s["ts_code"])
        pg = s.get("profit_growth", {})
        rev = s.get("revenue_trailing") or {}
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

        stocks.append({
            "ts_code": ts_code,
            "name": s.get("name", ""),
            "type": s.get("type", ""),
            "cum_yoy": cum,
            "single_q_yoy": _pct(pg.get("single_q_yoy_pct")),
            "qoq": _pct(pg.get("qoq_pct")),
            "single_q_note": pg.get("single_q_note"),
            "np_median_yi": s.get("net_profit", {}).get("median_yi"),
            "kf": _kf_display(en_idx.get(ts_code)),
            "rev_yoy": _pct(rev.get("cum_yoy_pct")),
            "accelerating": bool(s.get("flags", {}).get("accelerating")),
            "turnaround": bool(s.get("flags", {}).get("turnaround")),
            "change_reason": s.get("change_reason", ""),
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
        })

    # theme groups (only judged stocks with a theme); plus 无归属 (judged, null theme)
    groups: Dict[str, Dict[str, Any]] = {}
    no_theme: List[Dict[str, Any]] = []
    for st in stocks:
        if st["tier"] is None:
            continue
        if st["theme_id"]:
            g = groups.setdefault(st["theme_id"], {
                "theme_id": st["theme_id"], "theme_name": st["theme_name"],
                "state": st["theme_state"], "stars": st["theme_stars"],
                "hot": st["theme_hot"], "members": [],
            })
            g["members"].append(st)
        else:
            no_theme.append(st)
    group_list = sorted(groups.values(), key=lambda g: (-(g["stars"] or 0), g["theme_id"]))

    judged = [s for s in stocks if s["tier"] is not None]
    kpis = {
        "disclosed": len(stocks),
        "new_today": sum(1 for s in stocks if "NEW" in s["badges"]),
        "with_theme": sum(1 for s in judged if s["theme_id"]),
        "no_theme": sum(1 for s in judged if not s["theme_id"]),
        "enriched": len(en_idx),
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
        "kpis": kpis,
        "theme_groups": group_list,
        "no_theme_group": no_theme,
        "stocks": stocks,
    }


# --------------------------------------------------------------------------- #
_CSS = """
:root{--s0:#faf9f5;--s1:#f2f0e9;--s2:#fff;--tx:#1a1a18;--tx2:#5f5e5a;--tx3:#8a897f;
--bd:#e5e3da;--acc:#185fa5;--accbg:#e6f1fb;--pos:#3b6d11;--neg:#a32d2d;--amb:#854f0b;--ambbg:#faeeda;
--grn:#3b6d11;--grnbg:#eaf3de;--gry:#5f5e5a;--grybg:#f1efe8;--red:#a32d2d;--redbg:#fcebeb}
@media (prefers-color-scheme:dark){:root{--s0:#26251f;--s1:#2f2e27;--s2:#33322b;--tx:#ece9e0;--tx2:#b4b2a9;--tx3:#888780;
--bd:#44443f;--acc:#85b7eb;--accbg:#0c447c;--pos:#97c459;--neg:#f09595;--amb:#fac775;--ambbg:#633806;
--grn:#c0dd97;--grnbg:#27500a;--gry:#b4b2a9;--grybg:#3a3a35;--red:#f09595;--redbg:#501313}}
*{box-sizing:border-box}body{margin:0;background:var(--s0);color:var(--tx);
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
font-size:14px;line-height:1.6}.wrap{max-width:1080px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:21px;font-weight:500;margin:0}.sub{color:var(--tx2);font-size:13px;margin-top:3px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:18px 0}
.kpi{background:var(--s1);border-radius:8px;padding:11px 13px}.kpi .l{font-size:12px;color:var(--tx2);margin-bottom:5px}
.kpi .v{font-size:23px;font-weight:500;line-height:1}.kpi .s{font-size:12px;color:var(--tx3);margin-top:4px}
h2{font-size:16px;font-weight:500;margin:22px 0 10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.card{background:var(--s2);border:1px solid var(--bd);border-radius:12px;padding:12px 14px}
.card.dashed{border-style:dashed}.card h3{font-size:13px;font-weight:500;margin:0}
.chd{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.mem{font-size:13px;padding:2px 0;border-bottom:1px solid var(--bd)}.mem:last-child{border:0}
.mem .nm{font-weight:500}.mem .mt{color:var(--tx2);font-size:12px}
.pill{font-size:11px;padding:2px 8px;border-radius:20px;white-space:nowrap}
.strong{background:var(--grnbg);color:var(--grn)}.mid{background:var(--ambbg);color:var(--amb)}
.watch{background:var(--grybg);color:var(--gry)}.drop{background:var(--redbg);color:var(--red)}
.thot{background:var(--accbg);color:var(--acc)}.tcool{background:var(--grybg);color:var(--tx2)}
.bnew{background:var(--accbg);color:var(--acc)}.bupd{background:var(--ambbg);color:var(--amb)}
.bstale{background:var(--redbg);color:var(--red)}.badge{font-size:10px;padding:1px 6px;border-radius:20px;margin-left:4px}
.ctrl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0 10px}
input,select{font:inherit;padding:6px 9px;border:1px solid var(--bd);border-radius:8px;background:var(--s2);color:var(--tx)}
input{flex:1;min-width:140px}
table{width:100%;border-collapse:collapse}
th{font-size:11px;color:var(--tx3);font-weight:400;text-align:right;padding:7px 6px;border-bottom:1px solid var(--bd);cursor:pointer;user-select:none}
td{font-size:13px;padding:8px 6px;border-bottom:1px solid var(--bd);text-align:right;vertical-align:top}
td.l,th.l{text-align:left}.pos{color:var(--pos)}.neg{color:var(--neg)}.mut{color:var(--tx2)}.acc{color:var(--acc)}
tr.detail td{padding-top:0;border:0}.det{background:var(--s1);border-radius:8px;padding:8px 10px;font-size:12px;color:var(--tx2);margin:2px 0 8px}
.foot{color:var(--tx3);font-size:11px;margin-top:14px;line-height:1.7}
.empty{color:var(--tx3);font-size:12px;padding:6px 0}
"""

_JS = r"""
const fmtPct=(v,t)=>{if(v===null||v===undefined){if(t&&/扭亏|减亏/.test(t))return'扭亏';if(t&&/首亏|续亏|增亏/.test(t))return'增亏';return'—';}return(v>=0?'+':'')+v+'%';};
const tierPill={'强':'strong','中':'mid','观察':'watch','剔除':'drop'};
function pill(cls,txt){return `<span class="pill ${cls}">${txt}</span>`;}
function themeCell(s){
  if(s.tier===null)return '<span class="mut">未判</span>';
  if(!s.theme_id)return '<span class="mut">无归属</span>';
  const pre=s.match_confidence==='low'?'疑似 ':'';
  const stt=s.theme_state?`<span class="pill ${s.theme_hot?'thot':'tcool'}">${s.theme_state}${s.theme_stars?'★'+s.theme_stars:''}</span>`:'';
  return `<span class="acc">${pre}${s.theme_name||s.theme_id}</span> ${stt}`;
}
function badges(s){return s.badges.map(b=>{const c=b==='NEW'?'bnew':b==='更新'?'bupd':'bstale';return `<span class="badge ${c}">${b}</span>`;}).join('');}
function memberRow(s){
  const t=s.tier?pill(tierPill[s.tier],s.tier):'';
  const extra=s.accelerating?`单季${fmtPct(s.single_q_yoy,s.type)}`:(s.turnaround?'扭亏':fmtPct(s.cum_yoy,s.type));
  return `<div class="mem"><span class="nm">${s.name}</span> <span class="mut">${s.ts_code.slice(0,6)}</span> ${t} <span class="mt">${extra}</span></div>`;
}
function renderGroups(){
  let h='';
  for(const g of DATA.theme_groups){
    const stt=g.state?`<span class="pill ${g.hot?'thot':'tcool'}">${g.state}${g.stars?'★'+g.stars:''}</span>`:'';
    h+=`<div class="card"><div class="chd"><h3>${g.theme_name||g.theme_id}</h3>${stt}</div>${g.members.map(memberRow).join('')}</div>`;
  }
  if(DATA.no_theme_group.length){
    h+=`<div class="card dashed"><div class="chd"><h3>无归属主线</h3><span class="pill tcool">业绩强·暂无主线</span></div>${DATA.no_theme_group.map(memberRow).join('')}<div class="mt mut" style="font-size:11px;margin-top:4px">潜在未被市场发现的催化</div></div>`;
  }
  document.getElementById('groups').innerHTML=h||'<div class="empty">尚无判分/主线归属，先跑 verdict.record。</div>';
}
let sortKey='cum_yoy',sortDir=-1;
function renderTable(){
  const q=document.getElementById('q').value.trim().toLowerCase();
  const tf=document.getElementById('tier').value, thf=document.getElementById('theme').value;
  let rows=DATA.stocks.filter(s=>{
    if(tf&&s.tier!==tf)return false;
    if(thf==='__none__'&&s.theme_id)return false;
    if(thf&&thf!=='__none__'&&s.theme_id!==thf)return false;
    if(q&&!(s.name.toLowerCase().includes(q)||s.ts_code.toLowerCase().includes(q)||(s.theme_name||'').toLowerCase().includes(q)))return false;
    return true;});
  rows.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(x===null)x=-1e9;if(y===null)y=-1e9;
    if(typeof x==='string')return sortDir*x.localeCompare(y);return sortDir*(x-y);});
  let h='';
  for(const s of rows){
    const cls=v=>v===null?'mut':(v>=0?'pos':'neg');
    h+=`<tr>
      <td class="l"><span style="font-weight:500">${s.name}</span> <span class="mut">${s.ts_code.slice(0,6)}</span>${badges(s)}</td>
      <td class="mut">${s.type}</td>
      <td class="${cls(s.cum_yoy)}">${fmtPct(s.cum_yoy,s.type)}</td>
      <td class="${cls(s.single_q_yoy)}">${fmtPct(s.single_q_yoy,s.type)}${s.accelerating?' ↑':''}</td>
      <td>${s.kf||'<span class=mut>—</span>'}</td>
      <td class="l">${themeCell(s)}</td>
      <td>${s.tier?pill(tierPill[s.tier],s.tier):'<span class=mut>未判</span>'}</td></tr>`;
    if(s.reason||s.theme_rationale){
      h+=`<tr class="detail"><td colspan="7"><div class="det">${s.reason?'<b style="font-weight:500">判:</b> '+s.reason+(s.caveat?' · caveat: '+s.caveat:''):''}${s.theme_rationale?' <b style="font-weight:500">主线:</b> '+s.theme_rationale:''}</div></td></tr>`;
    }
  }
  document.getElementById('tbody').innerHTML=h||'<tr><td colspan="7" class="empty">无匹配</td></tr>';
}
function initFilters(){
  const th=document.getElementById('theme');
  const seen=new Set();
  for(const s of DATA.stocks){if(s.theme_id&&!seen.has(s.theme_id)){seen.add(s.theme_id);
    const o=document.createElement('option');o.value=s.theme_id;o.textContent=s.theme_name||s.theme_id;th.appendChild(o);}}
  const on=document.createElement('option');on.value='__none__';on.textContent='无归属';th.appendChild(on);
  document.querySelectorAll('th[data-k]').forEach(el=>el.onclick=()=>{const k=el.dataset.k;
    if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=-1;}renderTable();});
  ['q','tier','theme'].forEach(id=>document.getElementById(id).addEventListener('input',renderTable));
}
renderGroups();initFilters();renderTable();
"""


def render_html(view: Dict[str, Any]) -> str:
    k = view["kpis"]
    empty_note = ('<div class="foot">主线台账为空——归属列需先运行 daily-market-sense 填充 theme 台账。</div>'
                 if view["theme_registry_empty"] else "")
    kpi = lambda l, v, s: f'<div class="kpi"><div class="l">{l}</div><div class="v">{v}</div><div class="s">{s}</div></div>'
    kpis_html = "".join([
        kpi("已披露", k["disclosed"], f'今日新增 {k["new_today"]}'),
        kpi("已归属主线", k["with_theme"], "对上在场主线"),
        kpi("无归属", k["no_theme"], "业绩强·暂无主线"),
        kpi("已补扣非", k["enriched"], "cninfo 增强"),
        kpi("分档", f'{k["tier_strong"]}·{k["tier_mid"]}·{k["tier_watch"]}', "强·中·观察"),
        kpi("待判/待复判", f'{k["unjudged"]}/{k["stale"]}', "需模型判分"),
    ])
    data_json = json.dumps({
        "theme_groups": view["theme_groups"], "no_theme_group": view["no_theme_group"],
        "stocks": view["stocks"],
    }, ensure_ascii=False)
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{view['period_label']} 业绩预告 · 报告期观察</title><style>{_CSS}</style></head>
<body><div class="wrap">
<div style="display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px">
<div><h1>{view['period_label']} 业绩预告 · 报告期观察</h1><div class="sub">end_date {view['period']} · 业绩 × 主线交叉 · 一期一页增量更新</div></div>
<div class="sub">更新于 {view['updated_at']}</div></div>
<div class="kpis">{kpis_html}</div>
<h2>按主线分组 · 业绩强票落在哪些在场主线</h2><div class="grid" id="groups"></div>
<h2>个股明细</h2>
<div class="ctrl">
<input id="q" placeholder="搜索 名称 / 代码 / 主线">
<select id="tier"><option value="">全部分档</option><option>强</option><option>中</option><option>观察</option><option>剔除</option></select>
<select id="theme"><option value="">全部主线</option></select>
</div>
<table><thead><tr>
<th class="l">名称 代码</th><th>类型</th><th data-k="cum_yoy">累计同比</th><th data-k="single_q_yoy">单季同比</th>
<th>扣非</th><th class="l">归属主线（当前状态）</th><th>分档</th></tr></thead><tbody id="tbody"></tbody></table>
<div class="foot">净利=预告中值 · 营收/扣非=最近实际报告期(trailing) · 归属主线由模型语义匹配 daily-market-sense 主线台账、状态实时取 theme_daily_state · NEW/更新/待复判据披露日与判分快照派生 · 不含买卖建议</div>
{empty_note}
</div>
<script>const DATA={data_json};{_JS}</script>
</body></html>"""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render one period's earnings-forecast HTML page.")
    ap.add_argument("--period")
    ap.add_argument("--evidence")
    ap.add_argument("--enrich")
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
    view = build_view(period, evidence, enrich, store.load_verdicts(period),
                      store.load_theme_registry(), store.load_theme_latest_state(), today)
    html = render_html(view)

    out_path = args.out or os.path.join("reports", f"forecast_{period}.html")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(json.dumps({"period": period, "stocks": len(view["stocks"]),
                      "theme_groups": len(view["theme_groups"]),
                      "with_theme": view["kpis"]["with_theme"], "out": out_path}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
