#!/usr/bin/env python3
"""Render an a-stock-analyzer Markdown report as a self-contained HTML page.

All the generic machinery — CLI parsing, Markdown→HTML, theming, the chart kit
(``window.CK``), text-preservation validation and the pill/hero decorations —
comes from the shared ``html_report`` package (synced into ``scripts/_shared/``).
This file owns only the analyzer-specific bits: evidence loading, the chart
payload extraction (PE/PB/PS valuation bands + financial trends), the pill
vocabulary and the JS that draws those two charts.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_ROOT = Path(__file__).resolve().parent
_BUNDLED_SHARED = SCRIPT_ROOT / "_shared"
_DEV_SHARED = SCRIPT_ROOT.parents[2] / "shared"
_SHARED_PATHS = [str(p) for p in (_DEV_SHARED, _BUNDLED_SHARED) if p.exists()]
sys.path[:0] = [p for p in _SHARED_PATHS if p not in sys.path]

from html_report import (  # noqa: E402
    ChartHook,
    HeroDecoration,
    HtmlReportBuilder,
    PillDecoration,
    RenderJob,
    render_report,
)


# --------------------------------------------------------------------------- #
# Evidence loading + chart payload extraction (analyzer-specific)
# --------------------------------------------------------------------------- #
def default_evidence_path(input_path: Path) -> Optional[Path]:
    match = re.match(r"^report_(.+)$", input_path.stem)
    if not match:
        return None
    candidate = input_path.with_name(f"evidence_{match.group(1)}.json")
    return candidate if candidate.exists() else None


def load_evidence(path: Optional[Path]) -> dict:
    if path is None or not path.exists():
        return {"metadata": {"missing": True, "source": str(path) if path is not None else ""}, "datasets": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _dataset(evidence: dict, name: str) -> Any:
    entry = (evidence.get("datasets") or {}).get(name)
    if isinstance(entry, dict):
        if entry.get("ok") is False:
            return None
        return entry.get("data", entry)
    return entry


def _to_number(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num == num else None  # drop NaN


def _resolve_name(evidence: dict, ts_code: str) -> Optional[str]:
    company = _dataset(evidence, "company")
    if isinstance(company, dict):
        for key in ("name", "com_name"):
            if company.get(key):
                return str(company[key])
    return None


def extract_chart_payload(evidence: dict, source_path: Optional[Path]) -> Dict[str, Any]:
    """Pull the chart datasets from evidence into a compact, JS-ready payload."""
    band_data = _dataset(evidence, "valuation-band")
    bands = (band_data or {}).get("bands") if isinstance(band_data, dict) else {}
    val_series = (band_data or {}).get("series") if isinstance(band_data, dict) else []
    val_series = val_series if isinstance(val_series, list) else []
    ts_code = (band_data or {}).get("ts_code") if isinstance(band_data, dict) else None

    # Financial trends: merge income (revenue / 归母净利) with financial (ROE / margins / yoy) by end_date.
    income = _dataset(evidence, "income")
    income = income if isinstance(income, list) else []
    financial = _dataset(evidence, "financial")
    financial = financial if isinstance(financial, list) else []
    fin_by_end = {str(r.get("end_date")): r for r in financial if isinstance(r, dict) and r.get("end_date")}
    trends_by_end: Dict[str, dict] = {}
    for row in income:
        if not isinstance(row, dict) or not row.get("end_date"):
            continue
        end_date = str(row["end_date"])
        fin = fin_by_end.get(end_date, {})
        trends_by_end[end_date] = {
            "end_date": end_date,
            "revenue": _to_number(row.get("revenue") or row.get("total_revenue")),
            "n_income": _to_number(row.get("n_income_attr_p")),
            "tr_yoy": _to_number(fin.get("tr_yoy") or fin.get("or_yoy")),
            "netprofit_yoy": _to_number(fin.get("netprofit_yoy")),
            "roe": _to_number(fin.get("roe") or fin.get("roe_waa")),
            "grossprofit_margin": _to_number(fin.get("grossprofit_margin")),
            "netprofit_margin": _to_number(fin.get("netprofit_margin")),
        }
    trends = sorted(trends_by_end.values(), key=lambda r: r["end_date"])[-8:]

    return {
        "metadata": {
            "source": str(source_path) if source_path is not None else "",
            "missing": bool((evidence.get("metadata") or {}).get("missing")),
            "ts_code": ts_code,
            "name": _resolve_name(evidence, ts_code or ""),
            "latest_trade_date": (band_data or {}).get("latest_trade_date") if isinstance(band_data, dict) else None,
        },
        "valuation_bands": bands if isinstance(bands, dict) else {},
        "valuation_series": val_series,
        "financial_trends": trends,
    }


# --------------------------------------------------------------------------- #
# Decorations: pill vocabulary + "核心判断" hero card (mechanism lives in the
# shared package; here we only declare the analyzer-specific data).
# --------------------------------------------------------------------------- #
ANALYZER_PILL_RULES = [
    (r"^(成长股|成熟龙头|红利价值)$", "pill"),
    (r"^(强连接|核心标的)$", "pill neg"),
    (r"^(弱连接|边缘受益)$", "pill warn"),
    (r"^(无连接|脱离主线)$", "pill"),
    (r"^(深度低估|合理偏低)$", "pill pos"),
    (r"^(偏高|透支|极度乐观)$", "pill neg"),
    (r"^(合理|合理定价)$", "pill"),
    (r"^(强|高)$", "pill neg"),
    (r"^(中)$", "pill warn"),
    (r"^(弱|低)$", "pill pos"),
    (r"^(基准|乐观|压力|Bull|Bear|Base)$", "pill violet"),
]


# --------------------------------------------------------------------------- #
# Chart drawing JS: valuation bands + financial trends.
# Runs inside builder-supplied IIFE; reads its slice from __payload, draws with
# the shared chart kit (window.CK).
# --------------------------------------------------------------------------- #
STOCK_CHARTS_JS = r"""
const charts = __payload || {};
const root = document.getElementById("report-body");
if (!root) return;

const { svgEl, svgText } = CK;
const fmtDate = CK.fmt.date;
const fmtNum = CK.fmt.num;
const fmtYi = v => Number.isFinite(v) ? (v / 1e8).toFixed(2) + "亿" : "—";
const findHeading = (texts) => CK.findHeading(root, texts);
const insertAfterBlock = (heading, node) => { heading.after(node); };
const mkCard = CK.card;
const mkLegend = CK.legend;
const mkTooltip = CK.tooltip;
const moveTip = CK.moveTip;

drawValuationBands();
drawFinancialTrends();

function drawValuationBands() {
  const series = charts.valuation_series || [];
  const bands = charts.valuation_bands || {};
  if (series.length < 2) return;
  const heading = findHeading(["估值快照与历史分位", "估值快照", "历史分位", "估值模式"]);
  if (!heading) return;

  const specs = [
    { key: "pe_ttm", title: "PE-TTM 估值带" },
    { key: "pb", title: "PB 估值带" },
    { key: "ps_ttm", title: "PS-TTM 估值带" }
  ];
  const grid = document.createElement("div");
  grid.className = "chart-grid";
  specs.forEach(spec => {
    const card = buildBandTimeCard(spec.key, spec.title);
    if (card) grid.appendChild(card);
  });
  if (grid.children.length) insertAfterBlock(heading, grid);

  function buildBandTimeCard(key, title) {
    const pts = series
      .map((r, i) => ({ i, date: String(r.trade_date || ""), v: Number(r[key]) }))
      .filter(p => Number.isFinite(p.v) && p.v > 0);
    if (pts.length < 2) return null;
    const band = bands[key] || {};
    const windows = band.windows || {};
    const wkey = ["5y", "3y", "1y"].find(k => windows[k] && windows[k].sample_size);
    const win = wkey ? windows[wkey] : null;
    const current = Number.isFinite(Number(band.current)) ? Number(band.current) : pts[pts.length - 1].v;
    const pctText = win && Number.isFinite(win.current_percentile) ? ` · 分位 ${win.current_percentile.toFixed(0)}%` : "";
    const metricName = title.split(" ")[0];
    const card = mkCard("chart-card", title, `当前 ${fmtNum(current)}${pctText}`);
    const tip = mkTooltip(card);

    const W = 480, H = 220, pad = { l: 38, r: 40, t: 12, b: 28 };
    const usableW = W - pad.l - pad.r, usableH = H - pad.t - pad.b;
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });

    const levels = win ? [win.min, win.p25, win.median, win.p75, win.max].filter(Number.isFinite) : [];
    const vals = pts.map(p => p.v).concat(levels);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (lo === hi) { lo -= 1; hi += 1; }
    const sp = hi - lo; lo -= sp * 0.06; hi += sp * 0.06;
    const n = pts.length;
    const x = i => pad.l + (n <= 1 ? usableW / 2 : i / (n - 1) * usableW);
    const y = v => pad.t + (hi - v) / (hi - lo) * usableH;

    if (win && Number.isFinite(win.p25) && Number.isFinite(win.p75)) {
      svg.appendChild(svgEl("rect", { x: pad.l, y: y(win.p75).toFixed(2), width: usableW, height: Math.max(1, y(win.p25) - y(win.p75)).toFixed(2), class: "band-box" }));
    }
    const bandLines = win ? [
      ["max", win.max, "var(--neg)"],
      ["P75", win.p75, "var(--ink-4)"],
      ["中位", win.median, "var(--ink-3)"],
      ["P25", win.p25, "var(--ink-4)"],
      ["min", win.min, "var(--pos)"]
    ].filter(l => Number.isFinite(l[1])) : [];
    bandLines.forEach(item => {
      const v = item[1], color = item[2];
      svg.appendChild(svgEl("line", { x1: pad.l, x2: W - pad.r, y1: y(v).toFixed(2), y2: y(v).toFixed(2), stroke: color, "stroke-width": 1, "stroke-dasharray": "3 3", opacity: 0.65 }));
      svg.appendChild(svgText(W - pad.r + 3, y(v) + 3, fmtNum(v), "start", color, 9));
    });

    const d = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${x(i).toFixed(2)} ${y(p.v).toFixed(2)}`).join(" ");
    svg.appendChild(svgEl("path", { d, class: "series-line", style: "stroke: var(--accent)" }));
    const last = pts[pts.length - 1];
    svg.appendChild(svgEl("circle", { cx: x(n - 1).toFixed(2), cy: y(last.v).toFixed(2), r: 3.5, fill: "var(--orange)", stroke: "#fff", "stroke-width": 1.5 }));

    const ticks = 4;
    for (let t = 0; t <= ticks; t++) {
      const idx = Math.round(t / ticks * (n - 1));
      svg.appendChild(svgText(x(idx), H - pad.b + 14, fmtDate(pts[idx].date).slice(0, 7), "middle", "var(--ink-4)", 9));
    }
    svg.appendChild(svgEl("line", { x1: pad.l, x2: W - pad.r, y1: pad.t + usableH, y2: pad.t + usableH, class: "axis" }));

    const cursor = svgEl("line", { x1: 0, x2: 0, y1: pad.t, y2: pad.t + usableH, stroke: "var(--ink-4)", "stroke-width": 1, opacity: 0 });
    svg.appendChild(cursor);
    const hit = svgEl("rect", { x: pad.l, y: pad.t, width: usableW, height: usableH, fill: "transparent", style: "cursor:crosshair" });
    hit.addEventListener("mousemove", e => {
      const r = svg.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width * W;
      let idx = Math.round((px - pad.l) / usableW * (n - 1));
      idx = Math.max(0, Math.min(n - 1, idx));
      const p = pts[idx];
      cursor.setAttribute("x1", x(idx)); cursor.setAttribute("x2", x(idx)); cursor.setAttribute("opacity", "1");
      tip.innerHTML = `<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${fmtDate(p.date)}</div><div>${metricName}: ${fmtNum(p.v)}</div>`;
      tip.style.opacity = "1";
      moveTip(tip, card, e);
    });
    hit.addEventListener("mouseleave", () => { cursor.setAttribute("opacity", "0"); tip.style.opacity = "0"; });
    svg.appendChild(hit);

    card.appendChild(svg);
    card.appendChild(mkLegend([[(wkey ? wkey.toUpperCase() : "") + "分位带", "var(--accent-soft)"], ["估值", "var(--accent)"], ["当前", "var(--orange)"]]));
    return card;
  }
}

function drawFinancialTrends() {
  const rows = (charts.financial_trends || []).filter(r => r.end_date);
  if (rows.length < 2) return;
  const heading = findHeading(["成长性与财务质量诊断", "成长性与财务质量", "财务质量诊断", "成长性诊断"]);
  if (!heading) return;

  const labels = rows.map(r => String(r.end_date).replace(/^(\d{4})(\d{2})(\d{2})$/, "$1/$2"));
  const grid = document.createElement("div");
  grid.className = "chart-grid";
  const configs = [
    { title: "营收 / 归母净利 (亿元)", type: "bars", fmt: fmtYi, fields: [
      { key: "revenue", label: "营收", color: "var(--blue)", div: 1e8 },
      { key: "n_income", label: "归母净利", color: "var(--orange)", div: 1e8 } ]},
    { title: "同比增速 (%)", type: "line", fmt: v => fmtNum(v) + "%", fields: [
      { key: "tr_yoy", label: "营收YoY", color: "var(--blue)" },
      { key: "netprofit_yoy", label: "归母YoY", color: "var(--orange)" } ]},
    { title: "盈利能力 (%)", type: "line", fmt: v => fmtNum(v) + "%", fields: [
      { key: "roe", label: "ROE", color: "var(--violet)" },
      { key: "grossprofit_margin", label: "毛利率", color: "var(--pos)" },
      { key: "netprofit_margin", label: "净利率", color: "var(--cyan)" } ]}
  ];
  configs.forEach(cfg => {
    const hasData = cfg.fields.some(f => rows.some(r => Number.isFinite(r[f.key])));
    if (hasData) grid.appendChild(buildTrendCard(cfg, rows, labels));
  });
  if (grid.children.length) insertAfterBlock(heading, grid);

  function buildTrendCard(cfg, rows, labels) {
    const card = mkCard("chart-card", cfg.title, `${labels[0]} – ${labels[labels.length - 1]}`);
    const tip = mkTooltip(card);
    const W = 480, H = 210, pad = { l: 40, r: 14, t: 14, b: 30 };
    const usableW = W - pad.l - pad.r, usableH = H - pad.t - pad.b;
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
    const series = cfg.fields.map(f => ({
      field: f,
      pts: rows.map((r, i) => ({ i, date: labels[i], v: Number.isFinite(r[f.key]) ? r[f.key] / (f.div || 1) : null })).filter(p => Number.isFinite(p.v))
    })).filter(s => s.pts.length);
    if (!series.length) { svg.appendChild(svgText(W / 2, H / 2, "暂无数据", "middle")); card.appendChild(svg); return card; }
    const vals = series.flatMap(s => s.pts.map(p => p.v));
    let lo = Math.min(...vals, 0), hi = Math.max(...vals, 0);
    if (lo === hi) { lo -= 1; hi += 1; }
    const sp = hi - lo; lo -= sp * 0.08; hi += sp * 0.08;
    const n = rows.length;
    const x = i => pad.l + (n <= 1 ? usableW / 2 : (i + 0.5) / n * usableW);
    const y = v => pad.t + (hi - v) / (hi - lo) * usableH;
    for (let i = 0; i <= 4; i++) {
      const gy = pad.t + usableH * i / 4;
      svg.appendChild(svgEl("line", { x1: pad.l, x2: W - pad.r, y1: gy, y2: gy, class: "grid-line" }));
    }
    const y0 = y(0);
    if (y0 >= pad.t && y0 <= pad.t + usableH) svg.appendChild(svgEl("line", { x1: pad.l, x2: W - pad.r, y1: y0, y2: y0, class: "axis" }));

    if (cfg.type === "bars") {
      const groupW = usableW / n, bw = Math.min(18, groupW / (series.length + 0.6));
      series.forEach((s, si) => {
        s.pts.forEach(p => {
          const cx = pad.l + (p.i + 0.5) / n * usableW + (si - (series.length - 1) / 2) * bw;
          const py = y(p.v), barY = p.v >= 0 ? py : y0;
          svg.appendChild(svgEl("rect", { x: (cx - bw / 2).toFixed(2), y: barY.toFixed(2), width: bw.toFixed(2), height: Math.max(1, Math.abs(py - y0)).toFixed(2), fill: s.field.color, rx: 2, opacity: 0.85 }));
        });
      });
    } else {
      series.forEach(s => {
        const d = s.pts.map((p, i) => `${i === 0 ? "M" : "L"} ${x(p.i).toFixed(2)} ${y(p.v).toFixed(2)}`).join(" ");
        svg.appendChild(svgEl("path", { d, class: "series-line", style: `stroke:${s.field.color}` }));
        s.pts.forEach(p => svg.appendChild(svgEl("circle", { cx: x(p.i).toFixed(2), cy: y(p.v).toFixed(2), r: 3, fill: s.field.color, stroke: "#fff", "stroke-width": 1.5 })));
      });
    }

    rows.forEach((r, i) => {
      svg.appendChild(svgText(x(i), H - pad.b + 14, labels[i], "middle", "var(--ink-4)", 9.5));
      const hit = svgEl("rect", { x: (pad.l + i / n * usableW).toFixed(2), y: pad.t, width: (usableW / n).toFixed(2), height: usableH, fill: "transparent", style: "cursor:pointer" });
      hit.addEventListener("mouseenter", () => {
        tip.innerHTML = `<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${labels[i]}</div>` +
          cfg.fields.map(f => {
            const raw = r[f.key];
            if (!Number.isFinite(raw)) return "";
            return `<div>${f.label}: ${cfg.fmt(raw)}</div>`;
          }).join("");
        tip.style.opacity = "1";
      });
      hit.addEventListener("mousemove", e => moveTip(tip, card, e));
      hit.addEventListener("mouseleave", () => { tip.style.opacity = "0"; });
      svg.appendChild(hit);
    });
    svg.appendChild(svgText(4, pad.t + 4, cfg.fmt(hi)));
    svg.appendChild(svgText(4, pad.t + usableH, cfg.fmt(lo)));
    card.appendChild(svg);
    card.appendChild(mkLegend(cfg.fields.map(f => [f.label, f.color])));
    return card;
  }
}
"""


# --------------------------------------------------------------------------- #
# Tracking: per-stock valuation-model tracking table (alpha_data via
# tracking_store). Renders one card listing every tracked field with its latest
# value + an "updated MM-DD" badge that expands to the field's dated history;
# and turns inline ``{{track:field_id}}`` markers in the prose into the same
# badge with a click-to-open history popover. All var()s carry fallbacks so the
# print theme (which lacks --surface-2 / --font-mono / --pill / --shadow-2)
# still renders.
# --------------------------------------------------------------------------- #
TRACKING_CSS = """
.track-card { grid-column: 1 / -1; margin: 16px 0 22px; }
.track-tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.track-tbl td { padding: 7px 10px; border-bottom: 1px solid var(--line-2, #e8eaed); vertical-align: middle; }
.track-tbl tr:last-child td { border-bottom: none; }
.track-row { cursor: pointer; transition: background .12s ease; }
.track-row:hover { background: var(--surface-2, #f4f6f8); }
.track-c-label { color: var(--ink-1, #202124); font-weight: 500; }
.track-group { display: inline-block; font-size: 11px; font-weight: 500; padding: 1px 7px; border-radius: var(--pill, 999px); background: var(--accent-soft, #e8f0fe); color: var(--accent-ink, #0b57d0); margin-right: 6px; }
.track-c-val { font-family: var(--font-mono, ui-monospace, monospace); color: var(--ink-1, #202124); white-space: nowrap; }
.track-c-chip { text-align: right; white-space: nowrap; }
.track-c-exp { width: 20px; text-align: center; color: var(--ink-4, #9aa0a6); font-size: 11px; }
.track-chip { display: inline-flex; align-items: center; gap: 4px; font-family: var(--font-mono, ui-monospace, monospace); font-size: 11px; line-height: 1.6; padding: 1px 8px; border-radius: var(--pill, 999px); border: 1px solid var(--accent-hair, #c2d7f7); background: var(--accent-soft, #e8f0fe); color: var(--accent-ink, #0b57d0); cursor: pointer; }
.track-chip.hot { background: var(--accent, #1a73e8); border-color: var(--accent, #1a73e8); color: #fff; }
.track-chip.inline { vertical-align: baseline; margin: 0 2px; font-size: 10.5px; }
.track-retired .track-c-label, .track-retired .track-c-val { color: var(--ink-4, #9aa0a6); }
.track-retired-tag { font-size: 10px; color: var(--ink-4, #9aa0a6); margin-left: 6px; }
.track-hist td { background: var(--surface-2, #f6f8fa); padding: 4px 10px 10px; }
.track-hist-list { display: flex; flex-direction: column; gap: 2px; padding: 4px 0 2px; }
.track-hist-item { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; font-size: 12px; color: var(--ink-3, #5f6368); padding: 3px 8px; border-left: 2px solid var(--line-1, #dadce0); }
.track-hist-item.latest { color: var(--ink-1, #202124); font-weight: 500; border-left-color: var(--accent, #1a73e8); }
.track-hist-item .th-date { font-family: var(--font-mono, ui-monospace, monospace); font-size: 11px; color: var(--ink-4, #9aa0a6); min-width: 42px; }
.track-hist-item.latest .th-date { color: var(--accent-ink, #0b57d0); }
.track-hist-item .th-val { font-family: var(--font-mono, ui-monospace, monospace); }
.track-hist-item .th-note { color: var(--ink-3, #5f6368); }
.track-hist-item .th-meta { color: var(--ink-4, #9aa0a6); font-size: 11px; }
.track-pop { position: absolute; z-index: 999; max-width: 340px; background: var(--surface, #fff); border: 1px solid var(--line-1, #dadce0); border-radius: var(--r-md, 12px); box-shadow: var(--shadow-2, 0 6px 20px rgba(0,0,0,0.16)); padding: 10px 12px; }
.track-pop-head { font-size: 12px; font-weight: 600; color: var(--ink-1, #202124); margin-bottom: 6px; padding-bottom: 5px; border-bottom: 1px solid var(--line-2, #e8eaed); }
@media (max-width: 900px) { .track-c-val, .track-chip { white-space: normal; } }
"""

TRACKING_JS = r"""
const data = __payload || {};
const t = data.table;
const root = document.getElementById("report-body");
if (!root || !t || !Array.isArray(t.fields) || !t.fields.length) return;

const fieldsById = {};
t.fields.forEach(f => { fieldsById[f.id] = f; });

const esc = s => String(s == null ? "" : s);
const mmdd = d => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(d || "")); return m ? m[2] + "-" + m[3] : String(d || ""); };
const latestOf = f => { const h = f.history || []; return h.length ? h[h.length - 1] : null; };
function badgeText(f) { const l = latestOf(f); if (!l) return "无记录"; const n = (f.history || []).length; return mmdd(l.date) + " 更新" + (n > 1 ? " · " + n + "次" : ""); }

function histList(field) {
  const wrap = document.createElement("div");
  wrap.className = "track-hist-list";
  const hist = (field.history || []).slice().reverse();
  if (!hist.length) {
    const empty = document.createElement("div");
    empty.className = "track-hist-item";
    empty.textContent = "暂无更新记录";
    wrap.appendChild(empty);
    return wrap;
  }
  hist.forEach((e, i) => {
    const row = document.createElement("div");
    row.className = "track-hist-item" + (i === 0 ? " latest" : "");
    const dt = document.createElement("span"); dt.className = "th-date"; dt.textContent = mmdd(e.date);
    const val = document.createElement("span"); val.className = "th-val";
    val.textContent = esc(e.value) + (field.unit ? " " + field.unit : "");
    row.appendChild(dt); row.appendChild(val);
    if (e.note) { const n = document.createElement("span"); n.className = "th-note"; n.textContent = e.note; row.appendChild(n); }
    const meta = [e.source, e.confidence ? "置信" + e.confidence : ""].filter(Boolean).join(" · ");
    if (meta) { const m = document.createElement("span"); m.className = "th-meta"; m.textContent = meta; row.appendChild(m); }
    wrap.appendChild(row);
  });
  return wrap;
}

buildTrackingCard();
mountInlineMarkers();

function buildTrackingCard() {
  const card = CK.card("chart-card track-card", "估值建模跟踪表", buildSubtitle());
  const tbl = document.createElement("table"); tbl.className = "track-tbl";
  const tb = document.createElement("tbody");
  t.fields.forEach(f => {
    const tr = document.createElement("tr");
    tr.className = "track-row" + (f.status === "retired" ? " track-retired" : "");
    const c1 = document.createElement("td"); c1.className = "track-c-label";
    const g = document.createElement("span"); g.className = "track-group"; g.textContent = f.group || "其他"; c1.appendChild(g);
    c1.appendChild(document.createTextNode(" " + (f.label || f.id)));
    if (f.status === "retired") { const rr = document.createElement("span"); rr.className = "track-retired-tag"; rr.textContent = "已退役"; c1.appendChild(rr); }
    const c2 = document.createElement("td"); c2.className = "track-c-val";
    const lv = latestOf(f); c2.textContent = lv ? (esc(lv.value) + (f.unit ? " " + f.unit : "")) : "—";
    const c3 = document.createElement("td"); c3.className = "track-c-chip";
    const chip = document.createElement("span");
    chip.className = "track-chip" + (((f.history || []).length) > 1 ? " hot" : "");
    chip.textContent = badgeText(f); c3.appendChild(chip);
    const c4 = document.createElement("td"); c4.className = "track-c-exp"; c4.textContent = "▸";
    tr.appendChild(c1); tr.appendChild(c2); tr.appendChild(c3); tr.appendChild(c4);
    const hr = document.createElement("tr"); hr.className = "track-hist"; hr.hidden = true;
    const hc = document.createElement("td"); hc.colSpan = 4; hc.appendChild(histList(f)); hr.appendChild(hc);
    tr.addEventListener("click", () => { const open = hr.hidden; hr.hidden = !open; c4.textContent = open ? "▾" : "▸"; });
    tb.appendChild(tr); tb.appendChild(hr);
  });
  tbl.appendChild(tb); card.appendChild(tbl);
  const anchor = CK.findHeading(root, ["估值建模跟踪", "持续跟踪", "跟踪数据表", "估值结论", "估值快照与历史分位"]);
  if (anchor) anchor.after(card); else root.appendChild(card);
}

function buildSubtitle() {
  const nf = t.fields.length;
  const nu = t.fields.reduce((s, f) => s + ((f.history || []).length), 0);
  const bits = [((t.name || "") + " " + (t.ts_code || "")).trim(), nf + " 字段", nu + " 次更新"];
  if (t.updated) bits.push("最近 " + t.updated);
  return bits.filter(Boolean).join(" · ");
}

let openPop = null;
function closePop() { if (openPop) { openPop.remove(); openPop = null; document.removeEventListener("click", outside, true); document.removeEventListener("keydown", onKey, true); } }
function outside(e) { if (openPop && !openPop.contains(e.target) && !(e.target.classList && e.target.classList.contains("track-chip"))) closePop(); }
function onKey(e) { if (e.key === "Escape") closePop(); }
function openPopover(field, anchorEl) {
  closePop();
  const pop = document.createElement("div"); pop.className = "track-pop";
  const h = document.createElement("div"); h.className = "track-pop-head";
  h.textContent = (field.label || field.id) + (field.unit ? " (" + field.unit + ")" : "");
  pop.appendChild(h); pop.appendChild(histList(field));
  document.body.appendChild(pop);
  const r = anchorEl.getBoundingClientRect();
  const top = window.scrollY + r.bottom + 6;
  let left = window.scrollX + r.left;
  const maxLeft = window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 12;
  if (left > maxLeft) left = Math.max(window.scrollX + 8, maxLeft);
  pop.style.top = top + "px"; pop.style.left = left + "px";
  openPop = pop;
  setTimeout(() => { document.addEventListener("click", outside, true); document.addEventListener("keydown", onKey, true); }, 0);
}

function mountInlineMarkers() {
  const re = /\{\{track:([a-z][a-z0-9_]*)\}\}/g;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
  const targets = [];
  let node;
  while ((node = walker.nextNode())) { if (node.nodeValue && node.nodeValue.indexOf("{{track:") !== -1) targets.push(node); }
  targets.forEach(textNode => {
    const text = textNode.nodeValue; re.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const field = fieldsById[m[1]];
      if (field) {
        const chip = document.createElement("button"); chip.type = "button";
        chip.className = "track-chip inline" + (((field.history || []).length) > 1 ? " hot" : "");
        chip.textContent = badgeText(field);
        chip.addEventListener("click", ev => { ev.stopPropagation(); openPopover(field, chip); });
        frag.appendChild(chip);
      }
      last = m.index + m[0].length;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    if (frag.childNodes.length) textNode.parentNode.replaceChild(frag, textNode);
  });
}
"""


# --------------------------------------------------------------------------- #
# Thin manifest: declare inputs, decorations and charts; shared runner does
# the rest (parse args, render, validate, write, print summary).
# --------------------------------------------------------------------------- #
def _normalize_ts_code(raw: str) -> Optional[str]:
    """Best-effort map a 6-digit code or ts_code to Tushare ts_code form."""
    s = str(raw or "").strip().upper()
    if not s:
        return None
    if "." in s:
        return s
    if s.isdigit() and len(s) == 6:
        if s[0] in "03":
            return f"{s}.SZ"
        if s[0] in "69":
            return f"{s}.SH"
        if s[0] in "48":
            return f"{s}.BJ"
    return None


def _code_from_stem(stem: str) -> Optional[str]:
    match = re.search(r"(\d{6})", stem or "")
    return _normalize_ts_code(match.group(1)) if match else None


def load_tracking(args, charts: Dict[str, Any], input_path: Path) -> Optional[dict]:
    """Resolve the stock code and read its tracking table from alpha_data.
    Best-effort: any failure (no --tracking-code + no code in evidence, DB down,
    missing tables, no rows) returns None and the report renders without it.
    """
    if getattr(args, "no_tracking", False):
        return None
    code = (
        _normalize_ts_code(getattr(args, "tracking_code", None) or "")
        or (charts.get("metadata") or {}).get("ts_code")
        or _code_from_stem(input_path.stem)
    )
    if not code:
        return None
    try:
        import tracking_store
    except Exception:
        return None
    return tracking_store.load_table_safe(code)


def add_arguments(parser) -> None:
    parser.add_argument(
        "--evidence",
        default=None,
        help="Evidence JSON path. Defaults to sibling evidence_<code>.json when input is report_<code>.md.",
    )
    parser.add_argument(
        "--tracking-code",
        default=None,
        help="ts_code whose alpha_data tracking table to mount (default: from evidence / report filename).",
    )
    parser.add_argument(
        "--no-tracking",
        action="store_true",
        help="Skip the valuation-model tracking table even if one exists.",
    )


def build_job(args) -> RenderJob:
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_suffix(".html")
    evidence_path = Path(args.evidence) if args.evidence else default_evidence_path(input_path)
    markdown_text = input_path.read_text(encoding="utf-8")
    title = args.title or input_path.stem
    evidence = load_evidence(evidence_path)
    charts = extract_chart_payload(evidence, evidence_path)
    meta = charts.get("metadata") or {}
    sub_bits = [bit for bit in [meta.get("name"), meta.get("ts_code")] if bit]
    header_sub = " · ".join(sub_bits) if sub_bits else ""
    latest = str(meta.get("latest_trade_date") or "")
    meta_text = header_sub + (" · " + latest if (latest and header_sub) else latest)

    tracking = load_tracking(args, charts, input_path)
    extra_css = TRACKING_CSS if tracking else ""

    builder = HtmlReportBuilder(title=title, theme=args.theme, meta_text=meta_text, extra_css=extra_css)
    builder.add_decoration(PillDecoration(ANALYZER_PILL_RULES))
    # stop_mode="section": a subheading *inside* 核心判断 keeps collecting (the
    # original analyzer behaviour), rather than truncating the hero card.
    builder.add_decoration(HeroDecoration(heading_prefix="核心判断", stop_mode="section"))
    builder.add_chart_hook(ChartHook(name="stock-charts", payload=charts, js=STOCK_CHARTS_JS))
    if tracking:
        builder.add_chart_hook(ChartHook(name="tracking", payload={"table": tracking}, js=TRACKING_JS))

    tracking_fields = tracking.get("fields") if tracking else None
    return RenderJob(
        markdown_text=markdown_text,
        builder=builder,
        output_path=output_path,
        summary={
            "evidence": str(evidence_path) if evidence_path is not None else None,
            "valuation_series_points": len(charts.get("valuation_series") or []),
            "valuation_bands": sorted((charts.get("valuation_bands") or {}).keys()),
            "financial_periods": len(charts.get("financial_trends") or []),
            "tracking_code": (tracking or {}).get("ts_code"),
            "tracking_fields": len(tracking_fields) if tracking_fields else 0,
            "tracking_updates": sum(len(f.get("history") or []) for f in (tracking_fields or [])),
        },
    )


if __name__ == "__main__":
    raise SystemExit(
        render_report(
            description="Render a Markdown stock report to static HTML.",
            build_job=build_job,
            add_arguments=add_arguments,
        )
    )
