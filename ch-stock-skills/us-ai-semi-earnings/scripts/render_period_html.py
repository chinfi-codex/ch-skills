#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The accumulating season page: one calendar quarter, one self-contained HTML.

A US reporting season is six weeks of a few companies a night, so a report
written once is stale by the next morning. This renders the whole quarter from
the evidence pack plus the verdict ledger every time it runs, which means a
company judged in week one keeps its verdict while week five's arrivals slot in
beside it.

The page is a projection, never a source of judgement: every tier, quality call
and chain state on it was written by the model into `usearn_verdict` /
`usearn_chain_verdict`. Companies with no verdict yet are shown as `未判`
rather than hidden, so the gap between what has reported and what has been read
is visible instead of implied.

Self-contained by construction — inline CSS and JS, no CDN, no external fonts —
so the file can be mailed or dropped on a static host as-is.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from store import Store  # noqa: E402

_SCRIPT_DIR = Path(__file__).resolve().parent

TIER_ORDER = {"强": 0, "中": 1, "观察": 2, "剔除": 3, "未判": 4}
STAGE_LABEL = {
    "xbrl": "10-Q 已落地",
    "press_release_only": "仅新闻稿",
    "reported_no_numbers": "已报未取到数",
    "not_reported": "未披露",
}


def _n(v: Any, suffix: str = "", digits: int = 1) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:+.{digits}f}{suffix}" if suffix in ("pp", "%") and v > 0 else f"{v:.{digits}f}{suffix}"
    return escape(str(v))


def _money(v: Optional[float]) -> str:
    """Millions in, human units out."""
    if v is None:
        return "—"
    return f"${v / 1000:.2f}B" if abs(v) >= 1000 else f"${v:.0f}M"


def _growth_value(row: Dict[str, Any], *, eps: bool = False) -> str:
    if eps:
        return _n(row.get("value_musd"), "", 2)
    if row.get("value_musd") is not None:
        return _money(row["value_musd"])
    local = row.get("value_local_millions")
    unit = str(row.get("unit") or "").strip()
    if local is None or not unit:
        return "—"
    amount = f"{local / 1000:.2f}B" if abs(local) >= 1000 else f"{local:.0f}M"
    return f"{unit} {amount}"


def build_view(frame: str, evidence: Dict[str, Any], verdicts: Dict[str, Dict[str, Any]],
               chain_verdicts: Dict[str, Dict[str, Any]],
               transcripts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    companies: List[Dict[str, Any]] = []
    for row in evidence.get("companies") or []:
        ticker = row["ticker"]
        v = verdicts.get(ticker) or {}
        g, m = row.get("growth") or {}, row.get("margins") or {}
        q, b = row.get("quality") or {}, row.get("balance") or {}
        s, p = row.get("surprise") or {}, row.get("price_reaction") or {}
        t = transcripts.get(ticker) or row.get("transcript") or {}

        def gv(c: str, k: str) -> Any:
            return ((g.get(c) or {}).get(k))

        companies.append({
            "ticker": ticker,
            "name": row.get("name"),
            "bucket": row.get("bucket"),
            "chain_role": row.get("chain_role"),
            "announced": (row.get("announcement") or {}).get("date"),
            "announce_url": (row.get("announcement") or {}).get("url"),
            "provenance": (row.get("announcement") or {}).get("provenance"),
            "data_stage": row.get("data_stage"),
            "statement_source": row.get("statement_source"),
            "statement_note": row.get("statement_source_note"),
            "tier": v.get("tier") or "未判",
            "quality_call": v.get("quality_call"),
            "guidance_call": v.get("guidance_call"),
            "theme": v.get("theme"),
            "headline": v.get("headline"),
            "reasons": v.get("reasons") or [],
            "watch_items": v.get("watch_items") or [],
            "transcript_read": bool(v.get("transcript_read")),
            "growth": [
                {"line": label,
                 "value": _growth_value(g.get(c) or {}, eps=c == "eps_diluted"),
                 "yoy": gv(c, "yoy_pct"), "qoq": gv(c, "qoq_pct"),
                 "derived": gv(c, "derived")}
                for c, label in (("revenue", "营收"), ("gross_profit", "毛利"),
                                 ("operating_income", "经营利润"), ("net_income", "净利润"),
                                 ("ocf", "经营现金流"), ("eps_diluted", "摊薄 EPS"))
            ],
            "margins": [
                {"name": label, "pct": (m.get(k) or {}).get("pct"),
                 "yoy_pp": (m.get(k) or {}).get("yoy_pp"),
                 "qoq_pp": (m.get(k) or {}).get("qoq_pp")}
                for k, label in (("gross", "毛利率"), ("operating", "经营利润率"),
                                 ("net", "净利率"), ("rnd", "研发费用率"),
                                 ("sbc", "股权激励/营收"))
            ],
            "quality": {
                "ocf_to_ni": q.get("ocf_to_net_income_pct"),
                "fcf": q.get("free_cash_flow_musd"),
                "inv_gap": q.get("inventory_vs_revenue_gap_pp"),
                "ar_gap": q.get("receivable_vs_revenue_gap_pp"),
                "capex_ratio": q.get("capex_to_revenue_pct"),
            },
            "balance": {
                "net_cash": b.get("net_cash_musd"),
                "inventory": b.get("inventory_musd"),
                "rpo": b.get("rpo_musd"),
                "rpo_yoy": b.get("rpo_yoy_pct"),
                "deferred": b.get("deferred_revenue_musd"),
            },
            "surprise": {
                "reported": s.get("eps_reported"),
                "estimated": s.get("eps_estimated"),
                "pct": None if s.get("surprise_pct_unstable") else s.get("surprise_pct"),
                "pct_unstable": bool(s.get("surprise_pct_unstable")),
            },
            "price": {"same_day": p.get("same_day_pct"), "next_day": p.get("next_day_pct"),
                      "gap": p.get("gap_open_pct"), "gap_status": p.get("gap_status"),
                      "vol_ratio": p.get("vol_ratio"), "pos52": p.get("position_52w"),
                      "since": p.get("since_announce_pct"), "days": p.get("trading_days_since"),
                      "note": p.get("note")},
            "guidance": [e["text"] for e in (row.get("guidance_excerpts") or [])][:8],
            "press_head": row.get("press_release_head"),
            "transcript": {
                "status": t.get("status"), "source": t.get("source"), "url": t.get("url"),
                "segments": (t.get("stats") or {}).get("segment_count"),
                "prepared": (t.get("stats") or {}).get("prepared_segments"),
                "qa": (t.get("stats") or {}).get("qa_segments"),
                "participants": (t.get("participants") or [])[:12],
            },
            "screen": row.get("screen") or {},
            "sources": row.get("sources") or [],
        })

    companies.sort(key=lambda c: (TIER_ORDER.get(c["tier"], 9), -(c["screen"].get("rank_score") or -999)))

    chains = []
    for chain in evidence.get("transmission_chains") or []:
        cv = chain_verdicts.get(chain["name"]) or {}
        chains.append({**chain, "state": cv.get("state") or "未判",
                       "confirmed_by": cv.get("confirmed_by") or [],
                       "contradicted_by": cv.get("contradicted_by") or [],
                       "note": cv.get("note")})

    meta = evidence.get("meta") or {}
    judged = sum(1 for c in companies if c["tier"] != "未判")
    read = sum(1 for c in companies if c["transcript"]["status"] == "ok")
    return {
        "frame": frame,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "meta": meta,
        "coverage": {
            "universe": meta.get("universe_size"),
            "reported": meta.get("reported_count"),
            "judged": judged,
            "transcripts": read,
            "stages": meta.get("data_stage_counts") or {},
        },
        "buckets": evidence.get("buckets") or [],
        "chains": chains,
        "companies": companies,
        "not_reported": evidence.get("not_reported") or [],
    }


_CSS = """
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1d212a;--line:#2a2f3a;--fg:#e6e8ec;--dim:#9aa2b1;
--pos:#4ade80;--neg:#f87171;--warn:#fbbf24;--accent:#60a5fa;--chip:#232833}
@media(prefers-color-scheme:light){:root{--bg:#f6f7f9;--panel:#fff;--panel2:#f0f2f5;--line:#dfe3ea;
--fg:#161a20;--dim:#5d6672;--pos:#15803d;--neg:#b91c1c;--warn:#a16207;--accent:#1d4ed8;--chip:#eef1f6}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",
"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
header{padding:18px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{margin:0 0 6px;font-size:19px;letter-spacing:.3px}
.sub{color:var(--dim);font-size:12.5px}
.cov{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px}
.cov div{background:var(--chip);border:1px solid var(--line);border-radius:8px;padding:6px 11px;font-size:12.5px}
.cov b{font-size:15px;margin-right:5px}
main{display:grid;grid-template-columns:330px 1fr;gap:0;min-height:calc(100vh - 132px)}
@media(max-width:900px){main{grid-template-columns:1fr}#detail{border-left:none;border-top:1px solid var(--line)}}
#side{border-right:1px solid var(--line);background:var(--panel);overflow:auto;max-height:calc(100vh - 132px)}
.filters{padding:11px 13px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel);z-index:2}
.filters select,.filters input{width:100%;margin-bottom:6px;padding:6px 8px;border-radius:6px;
border:1px solid var(--line);background:var(--panel2);color:var(--fg);font-size:12.5px}
.row{padding:10px 13px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover{background:var(--panel2)}
.row.on{background:var(--panel2);box-shadow:inset 3px 0 0 var(--accent)}
.row .t{font-weight:600}
.row .meta{color:var(--dim);font-size:11.5px;display:flex;gap:7px;flex-wrap:wrap;margin-top:3px}
#detail{padding:20px 24px;overflow:auto;max-height:calc(100vh - 132px)}
.tag{display:inline-block;border-radius:99px;padding:1px 8px;font-size:11px;border:1px solid var(--line);
background:var(--chip);color:var(--dim)}
.tag.s0{color:var(--pos);border-color:var(--pos)} .tag.s1{color:var(--accent);border-color:var(--accent)}
.tag.s2{color:var(--warn);border-color:var(--warn)} .tag.s3{color:var(--neg);border-color:var(--neg)}
.pos{color:var(--pos)} .neg{color:var(--neg)} .dim{color:var(--dim)}
table{border-collapse:collapse;width:100%;margin:9px 0 16px;font-size:13px}
th,td{text-align:right;padding:6px 9px;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{color:var(--dim);font-weight:500;font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
h2{font-size:14px;margin:22px 0 4px;padding-bottom:5px;border-bottom:1px solid var(--line);letter-spacing:.3px}
h2:first-of-type{margin-top:6px}
.hl{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--accent);
border-radius:6px;padding:11px 14px;margin:10px 0}
.quote{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:9px 12px;
margin:6px 0;font-size:12.5px;color:var(--fg)}
ul{margin:6px 0;padding-left:20px} li{margin:3px 0}
.chains{padding:14px 22px;border-bottom:1px solid var(--line);background:var(--panel2)}
.chain{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin:7px 0;font-size:12.5px}
.link{background:var(--chip);border:1px solid var(--line);border-radius:6px;padding:4px 9px;white-space:nowrap}
.arrow{color:var(--dim)}
.empty{color:var(--dim);padding:40px 0;text-align:center}
footer{padding:14px 22px;color:var(--dim);font-size:11.5px;border-top:1px solid var(--line)}
code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12px}
"""

_JS = r"""
const V = window.__VIEW__;
const tierClass = t => ({'强':'s0','中':'s1','观察':'s2','剔除':'s3'}[t] || '');
const num = (v, s='', d=1) => v===null||v===undefined ? '<span class="dim">—</span>'
  : `<span class="${v>0?'pos':v<0?'neg':''}">${v>0&&(s==='%'||s==='pp')?'+':''}${(+v).toFixed(d)}${s}</span>`;
const plain = (v, s='', d=1) => v===null||v===undefined ? '<span class="dim">—</span>' : `${(+v).toFixed(d)}${s}`;
const esc = s => String(s??'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function matches(c){
  const b = document.getElementById('f-bucket').value;
  const t = document.getElementById('f-tier').value;
  const s = document.getElementById('f-stage').value;
  const k = document.getElementById('f-q').value.trim().toLowerCase();
  if(b && c.bucket !== b) return false;
  if(t && c.tier !== t) return false;
  if(s === 'transcript' && c.transcript.status !== 'ok') return false;
  if(s && s !== 'transcript' && c.data_stage !== s) return false;
  if(k && !((c.ticker+' '+(c.name||'')+' '+(c.theme||'')+' '+(c.headline||'')).toLowerCase().includes(k))) return false;
  return true;
}

function renderList(){
  const list = V.companies.filter(matches);
  document.getElementById('count').textContent = `${list.length} / ${V.companies.length}`;
  document.getElementById('rows').innerHTML = list.map(c => `
    <div class="row" data-t="${c.ticker}">
      <div class="t">${c.ticker} <span class="tag ${tierClass(c.tier)}">${c.tier}</span>
        ${c.transcript.status==='ok'?'<span class="tag">电话会</span>':''}
        ${c.data_stage==='press_release_only'?'<span class="tag s2">仅新闻稿</span>':''}</div>
      <div class="meta"><span>${c.announced||'—'}</span><span>${c.bucket}</span>
        <span>营收 ${c.growth[0].yoy===null?'—':(c.growth[0].yoy>0?'+':'')+c.growth[0].yoy+'%'}</span>
        <span>EPS ${c.surprise.pct===null||c.surprise.pct===undefined?'—':(c.surprise.pct>0?'+':'')+c.surprise.pct+'%'}</span></div>
    </div>`).join('') || '<div class="empty">没有符合条件的公司</div>';
  document.querySelectorAll('.row').forEach(r => r.onclick = () => select(r.dataset.t));
  if(list.length) select(list[0].ticker);
  else document.getElementById('detail').innerHTML = '<div class="empty">没有符合条件的公司</div>';
}

function select(ticker){
  const c = V.companies.find(x => x.ticker === ticker);
  if(!c) return;
  document.querySelectorAll('.row').forEach(r => r.classList.toggle('on', r.dataset.t === ticker));
  const tr = c.transcript;
  document.getElementById('detail').innerHTML = `
    <h1 style="font-size:20px;margin:0 0 4px">${c.ticker} <span class="dim" style="font-size:14px">${esc(c.name||'')}</span>
      <span class="tag ${tierClass(c.tier)}">${c.tier}</span>
      ${c.quality_call?`<span class="tag">质量 ${c.quality_call}</span>`:''}
      ${c.guidance_call?`<span class="tag">指引 ${c.guidance_call}</span>`:''}</h1>
    <div class="sub">${c.bucket} · ${esc(c.chain_role||'')} · 公告 ${c.announced||'—'}
      ${c.announce_url?` · <a href="${c.announce_url}" style="color:var(--accent)">原文</a>`:''}
      · ${esc(c.provenance||'')} · <code>${c.data_stage}</code></div>
    ${c.statement_note?`<div class="hl" style="border-left-color:var(--warn)">${esc(c.statement_note)}</div>`:''}
    ${c.headline?`<div class="hl"><b>${esc(c.headline)}</b>${c.theme?` <span class="tag">${esc(c.theme)}</span>`:''}
      ${c.reasons.length?`<ul>${c.reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:''}
      ${c.watch_items.length?`<div class="dim" style="margin-top:6px">需要跟踪：</div>
        <ul>${c.watch_items.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:''}</div>`
      : '<div class="hl dim">尚未判分——跑 verdict.py context / record 后重渲染</div>'}

    <h2>三口径</h2>
    <table><thead><tr><th>科目</th><th>本季</th><th>同比</th><th>环比</th></tr></thead><tbody>
    ${c.growth.map(g=>`<tr><td>${g.line}${g.derived?` <span class="tag">${g.derived}</span>`:''}</td>
	      <td>${esc(g.value)}</td><td>${num(g.yoy,'%')}</td><td>${num(g.qoq,'%')}</td></tr>`).join('')}
    </tbody></table>

    <h2>利润率与结构</h2>
    <table><thead><tr><th>指标</th><th>本季</th><th>同比</th><th>环比</th></tr></thead><tbody>
    ${c.margins.map(m=>`<tr><td>${m.name}</td><td>${plain(m.pct,'%')}</td>
      <td>${num(m.yoy_pp,'pp')}</td><td>${num(m.qoq_pp,'pp')}</td></tr>`).join('')}
    </tbody></table>

    <h2>利润成色与资产负债</h2>
    <table><tbody>
      <tr><td>经营现金流 / 净利润</td><td>${plain(c.quality.ocf_to_ni,'%')}</td>
          <td>自由现金流</td><td>${c.quality.fcf===null?'—':'$'+(c.quality.fcf/1000).toFixed(2)+'B'}</td></tr>
      <tr><td>存货增速 − 营收增速</td><td>${num(c.quality.inv_gap,'pp')}</td>
          <td>应收增速 − 营收增速</td><td>${num(c.quality.ar_gap,'pp')}</td></tr>
      <tr><td>资本开支 / 营收</td><td>${plain(c.quality.capex_ratio,'%')}</td>
          <td>净现金</td><td>${c.balance.net_cash===null?'—':'$'+(c.balance.net_cash/1000).toFixed(2)+'B'}</td></tr>
      <tr><td>RPO（在手合同）</td><td>${c.balance.rpo===null?'—':'$'+(c.balance.rpo/1000).toFixed(2)+'B'}</td>
          <td>RPO 同比</td><td>${num(c.balance.rpo_yoy,'%')}</td></tr>
    </tbody></table>

    <h2>市场预期与股价反应</h2>
    <table><tbody>
      <tr><td>EPS 实际 / 预期</td><td>${plain(c.surprise.reported,'',2)} / ${plain(c.surprise.estimated,'',2)}</td>
	          <td>超预期幅度</td><td>${c.surprise.pct_unstable?'<span class="dim">一致预期近零，仅看美分差</span>':num(c.surprise.pct,'%')}</td></tr>
      <tr><td>公告当日</td><td>${num(c.price.same_day,'%',2)}</td>
          <td>次日</td><td>${num(c.price.next_day,'%',2)}</td></tr>
      <tr><td>跳空</td><td>${num(c.price.gap,'%',2)} <span class="tag">${c.price.gap_status||'—'}</span></td>
          <td>量比</td><td>${plain(c.price.vol_ratio,'x',2)}</td></tr>
      <tr><td>52 周位置</td><td>${plain(c.price.pos52===null?null:c.price.pos52*100,'%')}</td>
          <td>公告以来</td><td>${num(c.price.since,'%',2)} <span class="dim">(${c.price.days??'—'} 个交易日)</span></td></tr>
    </tbody></table>
    ${c.price.note?`<div class="dim" style="font-size:12px">${esc(c.price.note)}</div>`:''}

    <h2>指引原文（新闻稿摘出，未经解读）</h2>
    ${c.guidance.length ? c.guidance.map(g=>`<div class="quote">${esc(g)}</div>`).join('')
      : '<div class="dim">新闻稿里没有匹配到前瞻性数字句 —— 指引可能只在电话会议里给</div>'}

    <h2>电话会议</h2>
    ${tr.status === 'ok' ? `<div class="quote">来源 <b>${tr.source}</b> ·
        ${tr.segments} 段（预备发言 ${tr.prepared} / 问答 ${tr.qa}）
        ${tr.url?` · <a href="${tr.url}" style="color:var(--accent)">原文</a>`:''}
        ${c.transcript_read?' · <span class="tag s0">已读并入判</span>':' · <span class="tag s2">已抓取未入判</span>'}
        ${tr.participants.length?`<div class="dim" style="margin-top:5px">${tr.participants.map(esc).join('；')}</div>`:''}
        <div class="dim" style="margin-top:5px">读全文：<code>read_source.py transcript ${c.ticker} --frame ${V.frame}</code></div></div>`
      : `<div class="quote dim">未取到（${esc(tr.status||'not_fetched')}）—— 两个源都还没发布，不要据此推断会议内容</div>`}

    <h2>机械漏斗（不是结论）</h2>
    <div class="dim">命中 ${c.screen.hits&&c.screen.hits.length?c.screen.hits.map(esc).join('、'):'无'}
      ${c.screen.penalty&&c.screen.penalty.length?` · 扣分 ${c.screen.penalty.map(esc).join('、')}`:''}
      · rank ${c.screen.rank_score ?? '—'}</div>
    <div class="dim" style="margin-top:6px">数据来源：${(c.sources||[]).map(esc).join('、')||'—'}</div>`;
}

document.addEventListener('DOMContentLoaded', () => {
  const buckets = [...new Set(V.companies.map(c => c.bucket))].sort();
  document.getElementById('f-bucket').innerHTML =
    '<option value="">全部环节</option>' + buckets.map(b => `<option>${b}</option>`).join('');
  ['f-bucket','f-tier','f-stage'].forEach(id => document.getElementById(id).onchange = renderList);
  document.getElementById('f-q').oninput = renderList;
  renderList();
});
"""


_CHAIN_STATE_CLASS = {"确认": "s0", "部分确认": "s2", "背离": "s3"}


def _chain_html(chain: Dict[str, Any]) -> str:
    links = []
    for link in chain.get("links") or []:
        yoy = link.get("median_revenue_yoy_pct")
        shown = "—" if yoy is None else f"{yoy:+.0f}%"
        links.append(f'<span class="link">{escape(link["bucket"])} <b>{shown}</b> '
                     f'<span class="dim">{escape(str(link.get("reported") or ""))}</span></span>')
    state = chain.get("state") or "未判"
    note = f'<span class="dim">{escape(chain.get("note") or "")}</span>' if chain.get("note") else ""
    return (f'<div class="chain"><span class="tag">{escape(chain.get("name") or "")}</span>'
            f'<span class="tag {_CHAIN_STATE_CLASS.get(state, "")}">{escape(state)}</span>'
            + '<span class="arrow">→</span>'.join(links) + note + "</div>")


def render_html(view: Dict[str, Any]) -> str:
    cov = view["coverage"]
    stages = " · ".join(f"{STAGE_LABEL.get(k, k)} {v}" for k, v in (cov["stages"] or {}).items())
    chains_html = "".join(_chain_html(c) for c in view["chains"])
    meta = view["meta"]
    safe_view_json = (json.dumps(view, ensure_ascii=False)
                      .replace("&", "\\u0026")
                      .replace("<", "\\u003c")
                      .replace(">", "\\u003e"))
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>美股 AI/半导体财报季 {escape(view['frame'])}</title><style>{_CSS}</style></head><body>
<header>
  <h1>美股 AI / 半导体财报季 · {escape(view['frame'])}</h1>
  <div class="sub">数据截至 {escape(str(meta.get('as_of') or '—'))} ·
    生成于 {escape(view['generated_at'])} · {escape(stages)}</div>
  <div class="cov">
    <div><b>{cov['reported'] or 0}</b>/{cov['universe'] or 0} 已披露</div>
    <div><b>{cov['judged']}</b> 已判分</div>
    <div><b>{cov['transcripts']}</b> 已取电话会议</div>
    <div>Alpha Vantage 余额 <b>{(meta.get('av_budget') or {}).get('remaining', '—')}</b>/{(meta.get('av_budget') or {}).get('limit', '—')}</div>
  </div>
</header>
<div class="chains"><div class="sub" style="margin-bottom:4px">产业链传导（中位数营收同比，模型判定状态）</div>{chains_html}</div>
<main>
  <div id="side">
    <div class="filters">
      <select id="f-bucket"></select>
      <select id="f-tier"><option value="">全部分档</option><option>强</option><option>中</option>
        <option>观察</option><option>剔除</option><option>未判</option></select>
      <select id="f-stage"><option value="">全部数据阶段</option><option value="xbrl">10-Q 已落地</option>
        <option value="press_release_only">仅新闻稿</option><option value="transcript">有电话会议</option></select>
      <input id="f-q" placeholder="搜索代码 / 名称 / 主题">
      <div class="sub" id="count"></div>
    </div>
    <div id="rows"></div>
  </div>
  <div id="detail"></div>
</main>
<footer>脚本只产出确定性证据；分档、质量成色、指引判断与产业链状态均由模型写入台账。不构成投资建议。</footer>
<script>window.__VIEW__ = {safe_view_json};</script>
<script>{_JS}</script></body></html>"""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render the accumulating earnings-season page")
    ap.add_argument("--frame", required=True)
    ap.add_argument("--evidence", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    frame = args.frame.upper()
    if not re.fullmatch(r"CY\d{4}Q[1-4]", frame):
        print(f"[error] --frame must look like CY2026Q2, got {frame!r}", file=sys.stderr)
        return 2
    ev_path = Path(args.evidence or (_SCRIPT_DIR.parent / "reports" / f"usearn_scan_{frame}.json"))
    try:
        evidence = json.loads(ev_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[error] cannot read {ev_path}: {exc}", file=sys.stderr)
        return 2
    evidence_frame = str((evidence.get("meta") or {}).get("frame") or "").upper()
    if evidence_frame != frame:
        print(f"[error] evidence frame mismatch: --frame={frame}, "
              f"evidence.meta.frame={evidence_frame or '<missing>'}", file=sys.stderr)
        return 2

    store = Store()
    verdicts = store.load_verdicts(frame) if store.available else {}
    chain_verdicts = store.load_chain_verdicts(frame) if store.available else {}
    transcripts = {t: v for (t, f), v in
                   (store.transcript_status([frame]).items() if store.available else [])
                   if f == frame}
    for ticker, rec in transcripts.items():
        payload = store.load_transcript(ticker, frame) if store.available else None
        if payload:
            rec["participants"] = payload.get("participants") or []
        rec["stats"] = {"segment_count": rec.get("segment_count"),
                        "prepared_segments": rec.get("prepared_segments"),
                        "qa_segments": rec.get("qa_segments")}

    view = build_view(frame, evidence, verdicts, chain_verdicts, transcripts)
    out = Path(args.out or (_SCRIPT_DIR.parent / "reports" / f"usearn_{frame}.html"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(view), encoding="utf-8")
    print(f"[html] {view['coverage']['reported']} reported, {view['coverage']['judged']} judged, "
          f"{view['coverage']['transcripts']} transcripts -> {out}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
