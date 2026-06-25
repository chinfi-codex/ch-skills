#!/usr/bin/env python3
"""Render a US-market watchlist Markdown report as self-contained HTML.

The Markdown report remains the truth source. This wrapper only builds the
browser layer: frontmatter stripping, generic Markdown->HTML rendering, and an
evidence-driven chart hook that visualizes the report's three layers —
大盘+广度 / 板块赚钱效应 / 观察池 vs-QQQ + 池外新方向.

Chart data sources, all deterministic:
- evidence JSON (`--evidence`): QQQ + pool snapshots (vs-QQQ / 52w / 量比) +
  `universe_scan` buckets for the breadth/rotation chart.
- themes JSON (`--themes`, default outputs/lifecycle_<date>.json): the same
  structured theme blocks the cross-day ledger consumes — reused here so the
  sector money-effect chart has the model's theme grouping without a separate
  handoff. The renderer never groups themes itself (that's the model's job).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


SCRIPT_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_ROOT.parent
_BUNDLED_SHARED = SCRIPT_ROOT / "_shared"
_DEV_SHARED = SCRIPT_ROOT.parents[2] / "shared"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

from html_report import (  # noqa: E402
    ChartHook,
    HtmlReportBuilder,
    PillDecoration,
    RenderJob,
    render_report,
)


FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*(?:\n|$)", re.DOTALL)
DATE_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2}|\d{8})")


USMARKET_COLOR_CSS = """
td .num-pos, .summary-card .summary-body .num-pos, .metric-value.pos, .bar-value.pos { color: var(--pos); fill: var(--pos); }
td .num-neg, .summary-card .summary-body .num-neg, .metric-value.neg, .bar-value.neg { color: var(--neg); fill: var(--neg); }
.bar-pos { fill: var(--pos); }
.bar-neg { fill: var(--neg); }
.theme-bar { rx: 3; }
.theme-bar-3star { fill: var(--accent, #2563eb); }
.theme-bar-2star { fill: var(--accent, #2563eb); opacity: 0.62; }
.theme-bar-1star { fill: var(--ink-4, #94a3b8); opacity: 0.55; }
.nd-card { border: 1px solid var(--border, #e2e8f0); border-left: 3px solid var(--accent, #2563eb); border-radius: 8px; padding: 10px 12px; }
.nd-card .chart-title { font-size: 13px; }
.nd-card .chart-subtitle { color: var(--ink-4); font-size: 11px; }
.nd-card .nd-body { margin-top: 6px; font-size: 12px; color: var(--ink-2); line-height: 1.5; }
/* 大段之间的视觉分隔：每个一级小节(h3)上方加全宽细线 + 左侧强调短条 + 更大留白 */
.report h3 { margin: 40px 0 14px; padding-top: 22px; border-top: 1.5px solid var(--line-1, #d9d3c4); position: relative; }
.report h3::before { content: ""; position: absolute; top: -1.5px; left: 0; width: 46px; height: 1.5px; background: var(--accent, #b07a52); }
.report h3:first-of-type { border-top: none; padding-top: 4px; margin-top: 18px; }
.report h3:first-of-type::before { display: none; }
/* 观察池总览热力墙：一格=代码+当日涨幅，绿涨红跌、色深=幅度大，★=±7% 异动 */
.pool-heatmap .ph-band + .ph-band { border-top: 1px solid var(--line-2, #eef1f5); margin-top: 12px; padding-top: 12px; }
.pool-heatmap .ph-head { display: flex; align-items: baseline; gap: 10px; margin: 6px 0 8px; flex-wrap: wrap; }
.pool-heatmap .ph-name { font-size: 13px; font-weight: 600; color: var(--ink-1, #1e293b); }
.pool-heatmap .ph-breadth { font-size: 11px; color: var(--ink-4, #94a3b8); }
.pool-heatmap .ph-breadth .num-pos { color: var(--pos); }
.pool-heatmap .ph-breadth .num-neg { color: var(--neg); }
.pool-heatmap .ph-tiles { display: flex; flex-wrap: wrap; gap: 6px; }
.ph-tile { position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center; width: 86px; height: 50px; border-radius: 7px; font-family: var(--font-mono, monospace); line-height: 1.2; cursor: default; }
.ph-tile .ph-tkr { font-size: 13px; font-weight: 600; }
.ph-tile .ph-pct { font-size: 11.5px; margin-top: 2px; }
.ph-tile .ph-star { position: absolute; top: 2px; right: 5px; font-size: 10px; }
.ph-tile .ph-plus { position: absolute; top: 1px; left: 5px; font-size: 11px; opacity: 0.85; }
.ph-tile.ph-na { background: var(--surface-2, #f1f5f9); color: var(--ink-4, #94a3b8); }
.ph-tile.u1 { background: color-mix(in srgb, var(--pos) 14%, var(--surface, #fff)); color: var(--pos); }
.ph-tile.u2 { background: color-mix(in srgb, var(--pos) 34%, var(--surface, #fff)); color: var(--ink-1, #1e293b); }
.ph-tile.u3 { background: color-mix(in srgb, var(--pos) 58%, var(--surface, #fff)); color: var(--ink-1, #1e293b); }
.ph-tile.u4 { background: color-mix(in srgb, var(--pos) 82%, var(--surface, #fff)); color: #fff; }
.ph-tile.u5 { background: var(--pos); color: #fff; }
.ph-tile.d1 { background: color-mix(in srgb, var(--neg) 14%, var(--surface, #fff)); color: var(--neg); }
.ph-tile.d2 { background: color-mix(in srgb, var(--neg) 34%, var(--surface, #fff)); color: var(--ink-1, #1e293b); }
.ph-tile.d3 { background: color-mix(in srgb, var(--neg) 58%, var(--surface, #fff)); color: var(--ink-1, #1e293b); }
.ph-tile.d4 { background: color-mix(in srgb, var(--neg) 82%, var(--surface, #fff)); color: #fff; }
.ph-tile.d5 { background: var(--neg); color: #fff; }
"""


USMARKET_PILL_RULES = [
    (r"^趋势延续$|^同向延续$", "pill"),
    (r"^高位回吐$|^反向修正$", "pill warn"),
    (r"^触及异动$|^已核查$", "pill neg"),
    (r"^未做联网核查$|^未检索到明确催化$|^待核查$", "pill warn"),
    (r"^无$|^无异动$", "pill pos"),
]


USMARKET_CHARTS_JS = r"""
const data = __payload || {};
const root = document.getElementById("report-body");
if (!root || !data || data.missing) return;

const fmtPct = CK.fmt.signedPct;
const pct0 = v => Number.isFinite(CK.num(v)) ? Math.round(CK.num(v) * 100) + "%" : "—";
const ratX = v => Number.isFinite(CK.num(v)) ? CK.num(v).toFixed(1) + "x" : "—";
const stars = n => "★".repeat(Math.max(0, Math.min(3, n || 0)));

insertQqqKline();
insertSectorCharts();
insertPoolCharts();
insertNewDirections();

function insertQqqKline() {
  const k = data.qqq_kline || {};
  const rows = normalizeRows(k.records).slice(-(k.days_requested || 120));
  if (rows.length < 2) return;
  const card = buildKlineCard(rows, k.name || "QQQ", k.days_requested || rows.length);
  CK.insertAfter(root, ["大盘", "QQQ"], card);
}

function normalizeRows(records) {
  return (Array.isArray(records) ? records : [])
    .map(r => ({
      trade_date: String(r.trade_date || ""),
      open: CK.num(r.open), high: CK.num(r.high), low: CK.num(r.low), close: CK.num(r.close),
      pct_chg: CK.num(r.pct_chg), amount: CK.num(r.amount), vol: CK.num(r.vol)
    }))
    .filter(r => r.trade_date && [r.open, r.high, r.low, r.close].every(Number.isFinite))
    .sort((a, b) => a.trade_date.localeCompare(b.trade_date));
}

function buildKlineCard(rows, name, displayDays) {
  const card = CK.card("kline-card", `${name} ${rows.length}日K线`, null);
  card.style.position = "relative";
  const first = rows[0], last = rows[rows.length - 1];
  const sub = document.createElement("div");
  sub.className = "chart-subtitle";
  sub.textContent = `${CK.fmt.date(first.trade_date)} 至 ${CK.fmt.date(last.trade_date)} · ${rows.length}/${displayDays} 个交易日`;
  card.appendChild(sub);
  card.appendChild(drawKline(rows, card));
  card.appendChild(CK.legend([["K线", "var(--neg)"], ["MA20", "var(--blue)"], ["MA60", "var(--orange)"], ["成交额", "rgba(100,116,139,0.55)"]]));
  return card;
}

function drawKline(rows, card) {
  const { svgEl, svgText } = CK;
  const enriched = rows.map((row, idx) => ({ ...row, idx, ma20: rollingAverage(rows, idx, 20), ma60: rollingAverage(rows, idx, 60) }));
  const width = 760, height = 300;
  const pad = { left: 52, right: 14, top: 12, bottom: 22 };
  const usableW = width - pad.left - pad.right;
  const amountPanelH = 52, panelGap = 14;
  const priceH = height - pad.top - pad.bottom - amountPanelH - panelGap;
  const amountTop = pad.top + priceH + panelGap, amountBottom = amountTop + amountPanelH;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });
  const allPrices = enriched.flatMap(r => [r.high, r.low, r.ma20, r.ma60]).filter(Number.isFinite);
  let min = Math.min(...allPrices), max = Math.max(...allPrices);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min; min -= span * 0.05; max += span * 0.05;
  const x = idx => pad.left + (enriched.length <= 1 ? usableW / 2 : idx / (enriched.length - 1) * usableW);
  const y = price => pad.top + (max - price) / (max - min) * priceH;
  const candleWidth = Math.max(2, Math.min(7, usableW / Math.max(enriched.length, 1) * 0.62));
  const amounts = enriched.map(r => r.amount).filter(v => Number.isFinite(v) && v > 0);
  const maxAmount = amounts.length ? Math.max(...amounts) : 1;
  const amountY = v => amountBottom - (v / maxAmount) * amountPanelH;
  for (let i = 0; i <= 4; i += 1) { const gy = pad.top + priceH * i / 4; svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: gy, y2: gy, class: "grid-line" })); }
  svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: pad.top + priceH, y2: pad.top + priceH, class: "axis" }));
  svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: amountBottom, y2: amountBottom, class: "axis" }));
  const tip = CK.tooltip(card);
  enriched.forEach(row => {
    const px = x(row.idx); const up = row.close >= row.open; const cls = up ? "kline-candle-up" : "kline-candle-down";
    if (Number.isFinite(row.amount) && row.amount > 0) {
      const barTop = amountY(row.amount);
      svg.appendChild(svgEl("rect", { x: (px - candleWidth / 2).toFixed(2), y: barTop.toFixed(2), width: candleWidth.toFixed(2), height: Math.max(1, amountBottom - barTop).toFixed(2), class: `amount-bar ${up ? "amount-bar-up" : "amount-bar-down"}`, rx: 1 }));
    }
    const bodyTop = y(Math.max(row.open, row.close)), bodyBottom = y(Math.min(row.open, row.close));
    svg.appendChild(svgEl("line", { x1: px.toFixed(2), x2: px.toFixed(2), y1: y(row.high).toFixed(2), y2: y(row.low).toFixed(2), class: `kline-wick ${cls}` }));
    svg.appendChild(svgEl("rect", { x: (px - candleWidth / 2).toFixed(2), y: bodyTop.toFixed(2), width: candleWidth.toFixed(2), height: Math.max(1, bodyBottom - bodyTop).toFixed(2), class: cls, rx: 1, opacity: 0.88 }));
    const hit = svgEl("rect", { x: (px - Math.max(candleWidth, 4) / 2).toFixed(2), y: pad.top, width: Math.max(candleWidth, 4).toFixed(2), height: amountBottom - pad.top, fill: "transparent", stroke: "none", style: "cursor:pointer" });
    hit.addEventListener("mouseenter", () => {
      tip.innerHTML = [
        `<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${CK.fmt.date(row.trade_date)}</div>`,
        `<div>开 ${klinePrice(row.open)} 高 ${klinePrice(row.high)}</div>`,
        `<div>低 ${klinePrice(row.low)} 收 ${klinePrice(row.close)}</div>`,
        `<div>${fmtPct(row.pct_chg)} · 成交额 ${klineAmount(row.amount)}</div>`
      ].join("");
      tip.style.opacity = "1";
    });
    hit.addEventListener("mousemove", e => CK.moveTip(tip, card, e));
    hit.addEventListener("mouseleave", () => { tip.style.opacity = "0"; });
    svg.appendChild(hit);
  });
  drawMaLine("ma20", "var(--blue)"); drawMaLine("ma60", "var(--orange)");
  svg.appendChild(svgText(4, pad.top + 4, klinePrice(max), "start", "var(--text-tertiary)"));
  svg.appendChild(svgText(4, pad.top + priceH, klinePrice(min), "start", "var(--text-tertiary)"));
  svg.appendChild(svgText(4, amountTop + 12, klineAmount(maxAmount), "start", "var(--text-tertiary)"));
  return svg;
  function drawMaLine(key, color) {
    const pts = enriched.filter(r => Number.isFinite(r[key]));
    if (!pts.length) return;
    const d = pts.map((r, i) => `${i === 0 ? "M" : "L"} ${x(r.idx).toFixed(2)} ${y(r[key]).toFixed(2)}`).join(" ");
    svg.appendChild(svgEl("path", { d, class: "ma-line", style: `stroke: ${color}` }));
  }
}

function rollingAverage(rows, idx, w) {
  if (idx + 1 < w) return null;
  const slice = rows.slice(idx - w + 1, idx + 1).map(r => r.close).filter(Number.isFinite);
  return slice.length === w ? slice.reduce((s, v) => s + v, 0) / w : null;
}
function klinePrice(v) { return Number.isFinite(v) ? "$" + v.toFixed(2) : "—"; }
function klineAmount(v) {
  if (!Number.isFinite(v)) return "—";
  if (v >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
  if (v >= 1e6) return "$" + (v / 1e6).toFixed(0) + "M";
  return "$" + v.toFixed(0);
}

function insertSectorCharts() {
  const grid = CK.grid("chart-grid");
  const sec = sectorMoneyEffectCard(data.themes);
  const uni = universeRotationCard((data.universe || {}).buckets);
  if (sec) grid.appendChild(sec);
  if (uni) grid.appendChild(uni);
  if (grid.children.length) CK.insertAfter(root, ["板块赚钱效应", "板块"], grid);
}

function insertPoolCharts() {
  const heatmap = poolHeatmapCard(data.groups, data.pool_relative);
  const rel = poolRelativeCard(data.pool_relative);
  if (!heatmap && !rel) return;
  const frag = document.createDocumentFragment();
  if (heatmap) frag.appendChild(heatmap);
  if (rel) {
    const grid = CK.grid("chart-grid");
    grid.appendChild(rel);
    frag.appendChild(grid);
  }
  CK.insertAfter(root, ["观察池个股明细", "观察池"], frag);
  if (heatmap) removePoolTables();
}

/* 墙 + tooltip 已承载逐股明细，HTML 里删掉墙下面那几张分组明细表（每组点评保留）。
   Markdown 正文不动——表格仍是真相源，这里只清浏览层的冗余。 */
function removePoolTables() {
  const heading = CK.findHeading(root, ["观察池个股明细", "观察池"]);
  if (!heading) return;
  const level = Number((/^H([1-6])$/.exec(heading.tagName || "") || [])[1]) || 3;
  let cur = heading.nextElementSibling;
  while (cur) {
    const hm = /^H([1-6])$/.exec(cur.tagName || "");
    if (hm && Number(hm[1]) <= level) break;
    const next = cur.nextElementSibling;
    if (cur.classList && cur.classList.contains("table-wrap")) cur.remove();
    cur = next;
  }
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

/* one unified wall: pool tiles grouped by 分组, color = same-day change%, ★=±7% */
function poolHeatmapCard(groups, poolRelative) {
  const rows = (Array.isArray(poolRelative) ? poolRelative : []).filter(r => r && r.ticker);
  if (!rows.length) return null;

  const byGroup = new Map();
  rows.forEach(r => {
    const g = r.group_names || "其他";
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g).push(r);
  });
  const summaryByName = new Map((groups || []).map(g => [g.name, g]));
  const order = (groups || []).map(g => g.name).filter(n => byGroup.has(n));
  byGroup.forEach((_, g) => { if (!order.includes(g)) order.push(g); });

  const c = CK.card("pool-heatmap chart-card", "观察池总览 · 热力墙",
    "每格=代码+当日涨幅 · 绿涨红跌、色深=幅度 · 悬停看 收盘/vs-QQQ/5日/52周/量比 · ＋=跑赢QQQ · ★=±7%异动");
  const tip = CK.tooltip(c);
  order.forEach(name => {
    const members = byGroup.get(name).slice()
      .sort((a, b) => (CK.num(b.change_pct) == null ? -Infinity : CK.num(b.change_pct))
                     - (CK.num(a.change_pct) == null ? -Infinity : CK.num(a.change_pct)));
    const band = document.createElement("div");
    band.className = "ph-band";

    const head = document.createElement("div");
    head.className = "ph-head";
    const sm = summaryByName.get(name) || {};
    const up = sm.up_count, down = sm.down_count;
    const breadth = (up != null || down != null) ? `${up || 0} 涨 ${down || 0} 跌` : "";
    const avg = CK.num(sm.avg_change_pct);
    const avgHtml = Number.isFinite(avg)
      ? ` · 均 <span class="${avg >= 0 ? "num-pos" : "num-neg"}">${fmtPct(avg)}</span>` : "";
    head.innerHTML = `<span class="ph-name">${escapeHtml(name)}</span>`
      + `<span class="ph-breadth">${breadth}${avgHtml}</span>`;
    band.appendChild(head);

    const tiles = document.createElement("div");
    tiles.className = "ph-tiles";
    members.forEach(m => tiles.appendChild(poolTile(m, tip, c)));
    band.appendChild(tiles);
    c.appendChild(band);
  });
  return c;
}

function poolTile(m, tip, card) {
  const v = CK.num(m.change_pct);
  const a = Number.isFinite(v) ? Math.abs(v) : 0;
  const lvl = !Number.isFinite(v) ? 0 : a < 1 ? 1 : a < 3 ? 2 : a < 5 ? 3 : a < 7 ? 4 : 5;
  const dir = (v || 0) >= 0 ? "u" : "d";
  const beat = CK.num(m.vs_qqq_1d);
  const tile = document.createElement("div");
  tile.className = `ph-tile ${Number.isFinite(v) ? dir + lvl : "ph-na"}`;
  tile.innerHTML = (a >= 7 ? `<span class="ph-star">★</span>` : "")
    + (Number.isFinite(beat) && beat > 0 ? `<span class="ph-plus">＋</span>` : "")
    + `<span class="ph-tkr">${escapeHtml(m.ticker)}</span>`
    + `<span class="ph-pct">${fmtPct(v)}</span>`;
  if (tip) {
    tile.addEventListener("mouseenter", () => { tip.innerHTML = poolTipHtml(m); tip.style.opacity = "1"; });
    tile.addEventListener("mousemove", e => CK.moveTip(tip, card, e));
    tile.addEventListener("mouseleave", () => { tip.style.opacity = "0"; });
  }
  return tile;
}

/* hover detail = the columns the per-group table used to carry */
function poolTipHtml(m) {
  const v = CK.num(m.change_pct), d5 = CK.num(m.five_day_trend_pct), vq = CK.num(m.vs_qqq_1d);
  const p = CK.num(m.position_52w), vr = CK.num(m.vol_vs_20d), close = CK.num(m.close);
  const sig = (Number.isFinite(v) ? (v >= 0 ? "↑" : "↓") : "")
    + (Number.isFinite(d5) ? (d5 >= 0 ? "↑" : "↓") : "")
    + (Number.isFinite(vq) && vq > 0 ? "＋" : "")
    + (Number.isFinite(v) && Math.abs(v) >= 7 ? "★" : "");
  const row = (k, val) => `<div style="display:flex;justify-content:space-between;gap:16px;margin-top:1px"><span style="color:#94a3b8">${k}</span><span>${val}</span></div>`;
  const signed = x => Number.isFinite(x) ? (x > 0 ? "+" : "") + x.toFixed(2) : "—";
  return `<div style="font-weight:600;margin-bottom:3px">${escapeHtml(m.ticker)} <span style="color:#cbd5e1;font-weight:400">${sig}</span></div>`
    + row("收盘", Number.isFinite(close) ? "$" + close.toFixed(2) : "—")
    + row("当日", fmtPct(v))
    + row("vs QQQ", signed(vq))
    + row("5 日", fmtPct(d5))
    + row("52周位置", Number.isFinite(p) ? p.toFixed(2) : "—")
    + row("量比", Number.isFinite(vr) ? vr.toFixed(2) + "x" : "—");
}

function insertNewDirections() {
  const nd = (data.themes || []).filter(t => t.is_new_direction);
  if (!nd.length) return;
  const grid = CK.grid("chart-grid");
  nd.slice(0, 6).forEach(t => {
    const c = CK.card("nd-card", `${t.name || ""} ${stars(t.stars)}`.trim(), `${t.state || ""} · 占比 ${pct0(t.dollar_vol_share)}`);
    const body = document.createElement("div");
    body.className = "nd-body";
    body.textContent = (t.members || []).slice(0, 6).join(" / ") || "—";
    c.appendChild(body);
    grid.appendChild(c);
  });
  CK.insertAfter(root, ["池外新方向", "新方向"], grid);
}

/* themes as proportional bars sized by dollar-volume share, colored by ★ */
function sectorMoneyEffectCard(themes) {
  const rows = (themes || []).filter(t => Number.isFinite(CK.num(t.dollar_vol_share)))
    .sort((a, b) => (b.dollar_vol_share || 0) - (a.dollar_vol_share || 0)).slice(0, 10);
  if (!rows.length) return null;
  const c = CK.card("chart-card", "板块赚钱效应", "主题按 dollar-volume 占比 · ★=主线确认度 · 新=池外新方向");
  const W = 520, rowH = 28, top = 12, bottom = 8, labelW = 158, barMax = W - labelW - 78;
  const H = top + rows.length * rowH + bottom;
  const svg = CK.svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  const maxShare = Math.max(0.01, ...rows.map(r => r.dollar_vol_share || 0));
  rows.forEach((r, i) => {
    const y = top + i * rowH;
    const w = Math.max(2, (r.dollar_vol_share || 0) / maxShare * barMax);
    const cls = (r.stars || 0) >= 3 ? "theme-bar-3star" : (r.stars || 0) >= 2 ? "theme-bar-2star" : "theme-bar-1star";
    const name = (r.name || "").slice(0, 14) + (r.is_new_direction ? " ·新" : "");
    svg.appendChild(CK.svgText(6, y + rowH / 2 + 1, name, "start", "var(--ink-2)", 11));
    svg.appendChild(CK.svgEl("rect", { x: labelW, y: (y + 6).toFixed(1), width: w.toFixed(1), height: 14, rx: 3, class: `theme-bar ${cls}` }));
    svg.appendChild(CK.svgText(labelW + w + 6, y + rowH / 2 + 1, `${pct0(r.dollar_vol_share)} ${stars(r.stars)}`, "start", "var(--ink-3)", 10));
  });
  c.appendChild(svg);
  return c;
}

/* bucket rotation: 5d vs-QQQ median, green = money in / red = bleeding out */
function universeRotationCard(buckets) {
  const rows = (buckets || []).filter(b => Number.isFinite(CK.num(b.median_vs_qqq_5d)))
    .sort((a, b) => CK.num(b.median_vs_qqq_5d) - CK.num(a.median_vs_qqq_5d))
    .map(b => ({ label: b.bucket, value: CK.num(b.median_vs_qqq_5d), meta: `${b.up || 0}/${b.down || 0}` }));
  if (!rows.length) return null;
  return CK.horizontalBarCard({
    title: "板块广度与轮动（universe）",
    subtitle: "各 bucket 5日 vs-QQQ 中位 · 绿=钱流入 / 红=失血 · meta=涨/跌家数",
    rows,
    maxRows: 12
  });
}

/* pool members' same-day excess over QQQ — who actually beat the tape */
function poolRelativeCard(rows) {
  const r = (rows || []).filter(x => Number.isFinite(CK.num(x.vs_qqq_1d)))
    .sort((a, b) => Math.abs(CK.num(b.vs_qqq_1d)) - Math.abs(CK.num(a.vs_qqq_1d))).slice(0, 14)
    .map(x => ({ label: x.ticker, value: CK.num(x.vs_qqq_1d), meta: pct0(x.position_52w) }));
  if (!r.length) return null;
  return CK.horizontalBarCard({
    title: "观察池 vs QQQ（当日超额）",
    subtitle: "谁跑赢 / 跑输大盘 · meta = 52周位置",
    rows: r,
    maxRows: 14
  });
}
"""


def strip_frontmatter(markdown_text: str) -> tuple[str, bool]:
    stripped = FRONTMATTER_RE.sub("", markdown_text, count=1)
    return stripped, stripped != markdown_text


def normalize_date(value: str) -> str:
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def date_from_text(value: str) -> Optional[str]:
    match = DATE_RE.search(value)
    if not match:
        return None
    return normalize_date(match.group("date"))


def default_evidence_path(input_path: Path, markdown_text: str) -> Optional[Path]:
    report_date = date_from_text(input_path.name) or date_from_text(markdown_text)
    if not report_date:
        return None
    compact = report_date.replace("-", "")
    candidates = [
        input_path.with_suffix(".json"),
        input_path.parent / f"us-{report_date}.json",
        input_path.parent / f"usmarket_{report_date}.json",
        input_path.parent / f"usmarket_{compact}.json",
        input_path.parent / f"evidence_{compact}.json",
        SKILL_ROOT / "outputs" / f"us-{report_date}.json",
        SKILL_ROOT / "outputs" / f"usmarket_{report_date}.json",
        SKILL_ROOT / "outputs" / f"usmarket_{compact}.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def default_themes_path(input_path: Path, markdown_text: str) -> Optional[Path]:
    """Auto-locate the lifecycle JSON the ledger step writes (theme source)."""
    report_date = date_from_text(input_path.name) or date_from_text(markdown_text)
    if not report_date:
        return None
    compact = report_date.replace("-", "")
    candidates = [
        input_path.parent / f"lifecycle_{compact}.json",
        input_path.parent / f"lifecycle_{report_date}.json",
        SKILL_ROOT / "outputs" / f"lifecycle_{compact}.json",
        SKILL_ROOT / "outputs" / f"lifecycle_{report_date}.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_evidence(path: Optional[Path]) -> dict:
    if path is None or not path.exists():
        return {
            "metadata": {
                "missing": True,
                "source": str(path) if path is not None else "",
            }
        }
    return json.loads(path.read_text(encoding="utf-8"))


def load_themes(path: Optional[Path]) -> List[Dict[str, Any]]:
    """Normalize the lifecycle JSON records into theme blocks for the chart.

    Accepts the model's `record` input format: each record carries a theme name
    (raw_theme_name / new_theme.name / theme_id), stars, dollar_vol_share, state,
    in_pool, is_new_direction, members.
    """
    if path is None or not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    themes: List[Dict[str, Any]] = []
    for rec in payload.get("records", []) or []:
        if not isinstance(rec, dict):
            continue
        new_block = rec.get("new_theme") or {}
        name = rec.get("raw_theme_name") or new_block.get("name") or rec.get("theme_id") or ""
        themes.append(
            {
                "name": str(name),
                "stars": rec.get("stars"),
                "dollar_vol_share": to_number(rec.get("dollar_vol_share")),
                "state": rec.get("state"),
                "in_pool": bool(rec.get("in_pool", False)),
                "is_new_direction": bool(rec.get("is_new_direction", False)),
                "members": rec.get("members") or [],
            }
        )
    return themes


def to_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def compact_snapshot(ticker: str, snapshot: Optional[dict], **extra: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return None
    row: Dict[str, Any] = {
        "ticker": ticker,
        "close": to_number(snapshot.get("close")),
        "change_pct": to_number(snapshot.get("change_pct")),
        "five_day_trend_pct": to_number(snapshot.get("five_day_trend_pct")),
        "vs_qqq_1d": to_number(snapshot.get("vs_qqq_1d")),
        "vs_qqq_5d": to_number(snapshot.get("vs_qqq_5d")),
        "position_52w": to_number(snapshot.get("position_52w")),
        "vol_vs_20d": to_number(snapshot.get("vol_vs_20d")),
        "volume": snapshot.get("volume"),
    }
    row.update(extra)
    return row


def extract_chart_payload(
    evidence: dict, source_path: Optional[Path], themes: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    if (evidence.get("metadata") or {}).get("missing"):
        return {
            "missing": True,
            "metadata": {"source": str(source_path) if source_path else ""},
        }

    qqq: Optional[Dict[str, Any]] = None
    qqq_kline: Optional[Dict[str, Any]] = None
    for item in evidence.get("indices") or []:
        if not isinstance(item, dict):
            continue
        ticker = item.get("ticker") or ((item.get("snapshot") or {}).get("ticker"))
        if str(ticker).upper() == "QQQ":
            qqq = compact_snapshot("QQQ", item.get("snapshot"), name=(item.get("snapshot") or {}).get("name"))
            records = item.get("kline_records") or []
            if records:
                qqq_kline = {
                    "records": records,
                    "name": (item.get("snapshot") or {}).get("name") or "QQQ",
                    "days_requested": item.get("kline_days_requested"),
                }
            break

    groups: List[Dict[str, Any]] = []
    watchlist_rows: List[Dict[str, Any]] = []
    for group in evidence.get("groups") or []:
        if not isinstance(group, dict):
            continue
        summary = group.get("summary") or {}
        group_name = str(group.get("name") or "")
        groups.append(
            {
                "name": group_name,
                "valid_count": summary.get("valid_count") or 0,
                "up_count": summary.get("up_count") or 0,
                "down_count": summary.get("down_count") or 0,
                "avg_change_pct": to_number(summary.get("avg_change_pct")),
            }
        )
        for stock in group.get("stocks") or []:
            if not isinstance(stock, dict):
                continue
            row = compact_snapshot(
                str(stock.get("ticker") or ""),
                stock.get("snapshot"),
                group_names=group_name,
            )
            if row:
                watchlist_rows.append(row)

    abnormal: List[Dict[str, Any]] = []
    for bucket in ("rises", "drops"):
        for item in ((evidence.get("abnormal_moves") or {}).get(bucket) or []):
            if not isinstance(item, dict):
                continue
            row = compact_snapshot(
                str(item.get("ticker") or ""),
                item,
                group_names=" / ".join(item.get("groups") or []),
            )
            if row:
                abnormal.append(row)
    abnormal.sort(key=lambda item: abs(item.get("change_pct") or 0), reverse=True)

    # universe_scan: bucket breadth/rotation (only present with --scan-universe)
    universe_raw = evidence.get("universe_scan") or {}
    universe_buckets: List[Dict[str, Any]] = []
    for bucket in universe_raw.get("buckets") or []:
        if not isinstance(bucket, dict):
            continue
        universe_buckets.append(
            {
                "bucket": bucket.get("bucket"),
                "up": bucket.get("up") or 0,
                "down": bucket.get("down") or 0,
                "median_vs_qqq_5d": to_number(bucket.get("median_vs_qqq_5d")),
                "dollar_volume_million": to_number(bucket.get("dollar_volume_million")),
            }
        )

    valid_count = len([item for item in watchlist_rows if item.get("change_pct") is not None])
    up_count = len([item for item in watchlist_rows if (item.get("change_pct") or 0) > 0])

    return {
        "missing": False,
        "metadata": {
            "source": str(source_path) if source_path else "",
            "date": evidence.get("date"),
            "generated_at": evidence.get("generated_at"),
        },
        "index": qqq or {},
        "qqq_kline": qqq_kline,
        "stats": {
            "valid_count": valid_count,
            "up_count": up_count,
            "watchlist_abnormal_count": len(abnormal),
        },
        "groups": groups,
        "pool_relative": watchlist_rows,
        "watchlist_abnormal": abnormal[:12],
        "universe": {"buckets": universe_buckets},
        "themes": themes or [],
    }


def extract_title(markdown_text: str, fallback: str) -> str:
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def add_arguments(parser: Any) -> None:
    parser.add_argument("--evidence", default=None, help="Evidence JSON path. Defaults to a date-matched outputs/us-YYYY-MM-DD.json if found.")
    parser.add_argument("--themes", default=None, help="Themes JSON (the ledger lifecycle_<date>.json). Defaults to a date-matched file if found; drives the sector money-effect chart.")
    parser.add_argument("--keep-frontmatter", action="store_true", help="Render YAML frontmatter as visible content. Default strips it.")


def build_job(args: Any) -> RenderJob:
    input_path = Path(args.input).expanduser().resolve()
    raw_markdown = input_path.read_text(encoding="utf-8")
    markdown_text, frontmatter_stripped = (raw_markdown, False) if args.keep_frontmatter else strip_frontmatter(raw_markdown)

    evidence_path = Path(args.evidence).expanduser().resolve() if args.evidence else default_evidence_path(input_path, markdown_text)
    evidence = load_evidence(evidence_path)

    themes_path = Path(args.themes).expanduser().resolve() if args.themes else default_themes_path(input_path, markdown_text)
    themes = load_themes(themes_path)

    payload = extract_chart_payload(evidence, evidence_path, themes)

    title = args.title or extract_title(markdown_text, input_path.stem)
    meta_date = payload.get("metadata", {}).get("date") or date_from_text(markdown_text) or "unknown"
    meta_text = f"Nasdaq-Tech Watchlist | date={meta_date}"
    if evidence_path:
        meta_text += f" | evidence={evidence_path.name}"

    builder = HtmlReportBuilder(
        title=title,
        theme=args.theme,
        meta_text=meta_text,
        extra_css=USMARKET_COLOR_CSS,
    )
    builder.add_decoration(PillDecoration(USMARKET_PILL_RULES))
    builder.add_chart_hook(ChartHook(name="usmarket", payload=payload, js=USMARKET_CHARTS_JS))

    output_path = Path(args.output).expanduser().resolve() if args.output else input_path.with_suffix(".html")
    return RenderJob(
        markdown_text=markdown_text,
        builder=builder,
        output_path=output_path,
        summary={
            "evidence": str(evidence_path) if evidence_path else None,
            "themes": str(themes_path) if themes_path else None,
            "data_date": meta_date,
            "frontmatter_stripped": frontmatter_stripped,
            "charts": {
                "themes": len(payload.get("themes") or []),
                "universe_buckets": len((payload.get("universe") or {}).get("buckets") or []),
                "pool_relative": len(payload.get("pool_relative") or []),
            },
        },
    )


if __name__ == "__main__":
    raise SystemExit(
        render_report(
            description="Render a Nasdaq-tech watchlist Markdown report to static HTML.",
            build_job=build_job,
            add_arguments=add_arguments,
        )
    )
