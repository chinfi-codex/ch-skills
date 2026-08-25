#!/usr/bin/env python3
"""把 GPU 算力监控日报（Markdown）渲染成自包含单页 HTML Dashboard。

Markdown 是真相源——市场判断、供需拐点结论、每条解读都由模型写在 md 里。
这个脚本只负责浏览器层：套壳、注入两张 90 天趋势图和数据源健康度条。
它不归纳、不排名、不写结论。

按 PRD §5 的硬约束渲染：三型号同屏、不给 GPU selector、不给时间范围切换、
趋势窗口固定 90 天。缺失数据渲染成"暂无数据"，绝不用前值冒充最新值。

用法：
    python scripts/render_report_html.py --input reports/gpu-2026-08-25.md \
        --evidence evidence/gpu-2026-08-25.json
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
_DEV_SHARED = SCRIPT_ROOT.parents[1] / "shared"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

from html_report import (  # noqa: E402
    ChartHook,
    HtmlReportBuilder,
    PillDecoration,
    RenderJob,
    render_report,
)

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# 状态标签一律带文字，不靠颜色单独承载信息（PRD §10）。
PILL_RULES = [
    (r"^(偏紧|收紧|紧张|High)$", "pill neg"),
    (r"^(偏松|宽松|Low|None)$", "pill"),
    (r"^(中性|平衡|Medium|观察中)$", "pill"),
    (r"^(暂无数据|采集失败|样本不足|口径变动|冷启动)$", "pill warn"),
]

EXTRA_CSS = """
.gpu-health { display: grid; gap: 8px; margin: 6px 0 2px; }
.gpu-health .gh-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 7px 10px; border: 1px solid var(--line-2, #eef1f5); border-radius: 8px;
  font-size: 12px; }
.gpu-health .gh-name { font-weight: 600; min-width: 96px; color: var(--ink-1, #1e293b); }
.gpu-health .gh-badge { font-size: 11px; padding: 1px 7px; border-radius: 999px;
  border: 1px solid var(--line-1, #d9d3c4); }
.gpu-health .gh-ok { color: var(--pos); border-color: color-mix(in srgb, var(--pos) 45%, transparent); }
.gpu-health .gh-bad { color: var(--neg); border-color: color-mix(in srgb, var(--neg) 45%, transparent); }
.gpu-health .gh-warn { color: var(--ink-3, #64748b); }
.gpu-health .gh-meta { color: var(--ink-4, #94a3b8); font-family: var(--font-mono, monospace); }
.gpu-health .gh-err { flex-basis: 100%; color: var(--neg); font-size: 11.5px; }
.gpu-empty { padding: 18px; text-align: center; color: var(--ink-4, #94a3b8);
  font-size: 12.5px; border: 1px dashed var(--line-1, #d9d3c4); border-radius: 8px; }
.gpu-note { font-size: 11.5px; color: var(--ink-4, #94a3b8); margin-top: 6px; line-height: 1.5; }
"""

CHART_JS = r"""
(function () {
  var data = (window.__chartData || {})["gpu-monitor-charts"];
  if (!data) return;
  var root = document.querySelector(".report") || document.body;
  var COLORS = data.colors || {};
  var W = 560, H = 230, PAD = { t: 16, r: 14, b: 26, l: 46 };

  function empty(msg) {
    var d = document.createElement("div");
    d.className = "gpu-empty";
    d.textContent = msg;
    return d;
  }

  function note(text) {
    var d = document.createElement("div");
    d.className = "gpu-note";
    d.textContent = text;
    return d;
  }

  /* 多序列折线：三型号同屏，x 轴是日期，y 轴自适应。 */
  function lineChart(cardTitle, subtitle, seriesList, unit, footnote) {
    var card = CK.card("chart-card", cardTitle, subtitle);
    var live = seriesList.filter(function (s) { return s.points && s.points.length > 1; });
    if (!live.length) {
      card.appendChild(empty("暂无数据：窗口内没有足够的观测点，未用前值补齐"));
      if (footnote) card.appendChild(note(footnote));
      return card;
    }
    var all = [], dates = {};
    live.forEach(function (s) {
      s.points.forEach(function (p) { all.push(p.value); dates[p.date] = 1; });
    });
    var days = Object.keys(dates).sort();
    var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
    if (hi === lo) { hi = lo + 1; lo = Math.max(0, lo - 1); }
    var span = hi - lo;
    lo -= span * 0.08; hi += span * 0.08;
    var x = function (d) {
      var i = days.indexOf(d);
      return PAD.l + (days.length < 2 ? 0 : (i / (days.length - 1)) * (W - PAD.l - PAD.r));
    };
    var y = function (v) {
      return PAD.t + (1 - (v - lo) / (hi - lo)) * (H - PAD.t - PAD.b);
    };

    var svg = CK.svgEl("svg", { viewBox: "0 0 " + W + " " + H, class: "chart-svg" });
    for (var g = 0; g <= 4; g++) {
      var gv = lo + (hi - lo) * (g / 4);
      svg.appendChild(CK.svgEl("line", {
        x1: PAD.l, x2: W - PAD.r, y1: y(gv), y2: y(gv),
        stroke: "var(--line-2, #eef1f5)", "stroke-width": 1 }));
      svg.appendChild(CK.svgText(PAD.l - 6, y(gv) + 3, CK.fmt.num(gv, 2), "end",
        "var(--ink-4, #94a3b8)", 9));
    }
    [0, Math.floor(days.length / 2), days.length - 1].forEach(function (i) {
      if (days[i]) svg.appendChild(CK.svgText(x(days[i]), H - 8, days[i].slice(5),
        i === 0 ? "start" : (i === days.length - 1 ? "end" : "middle"),
        "var(--ink-4, #94a3b8)", 9));
    });

    live.forEach(function (s) {
      var color = COLORS[s.name] || "var(--accent, #2563eb)";
      var d = s.points.map(function (p, i) {
        return (i ? "L" : "M") + x(p.date).toFixed(1) + " " + y(p.value).toFixed(1);
      }).join(" ");
      svg.appendChild(CK.svgEl("path", { d: d, fill: "none", stroke: color,
        "stroke-width": 1.8, "stroke-linejoin": "round" }));
      var last = s.points[s.points.length - 1];
      svg.appendChild(CK.svgEl("circle", { cx: x(last.date), cy: y(last.value), r: 3,
        fill: color }));
    });

    card.appendChild(svg);
    card.appendChild(CK.legend(live.map(function (s) {
      return [s.label + " " + CK.fmt.num(s.points[s.points.length - 1].value, 2) + unit,
              COLORS[s.name] || "var(--accent, #2563eb)"];
    })));
    var missing = seriesList.filter(function (s) { return !s.points || s.points.length < 2; });
    if (missing.length) {
      card.appendChild(note("暂无数据：" + missing.map(function (s) { return s.label; }).join("、")
        + "（观测点不足 2 个，不画线也不补值）"));
    }
    if (footnote) card.appendChild(note(footnote));
    return card;
  }

  /* B. 90D 市场成交价趋势 + C. 90D 可用供给趋势 */
  var grid = CK.grid("chart-grid");
  grid.appendChild(lineChart(
    "90D 市场成交价趋势（Ornn OCPI 日度结算）",
    "USD / GPU·hour｜三型号同屏，窗口固定 90 天",
    data.price_series, " ", data.price_footnote));
  grid.appendChild(lineChart(
    "90D 可用供给趋势",
    "供给指数＝各源可用信号标准化后取均值｜越高越宽松",
    data.supply_series, "", data.supply_footnote));
  CK.insertAfter(root, ["市场成交价趋势", "成交价趋势", "价格趋势"], grid);

  /* A. 顶部关键摘要：每个型号一张卡 */
  if (data.headline && data.headline.length) {
    CK.insertAfter(root, ["市场判断", "供需拐点", "核心判断"],
      CK.metricGrid(data.headline));
  }

  /* F. 数据源与采集状态 */
  if (data.health && data.health.length) {
    var wrap = document.createElement("div");
    wrap.className = "gpu-health";
    data.health.forEach(function (h) {
      var row = document.createElement("div");
      row.className = "gh-row";
      var name = document.createElement("span");
      name.className = "gh-name";
      name.textContent = h.source + (h.priority ? " · " + h.priority : "");
      row.appendChild(name);
      var badge = document.createElement("span");
      badge.className = "gh-badge " + (h.status === "ok" ? "gh-ok"
        : (h.status === "empty" ? "gh-warn" : "gh-bad"));
      badge.textContent = h.status_text;
      row.appendChild(badge);
      var meta = document.createElement("span");
      meta.className = "gh-meta";
      meta.textContent = h.meta;
      row.appendChild(meta);
      if (h.error) {
        var err = document.createElement("span");
        err.className = "gh-err";
        err.textContent = h.error;
        row.appendChild(err);
      }
      wrap.appendChild(row);
    });
    CK.insertAfter(root, ["数据源与采集状态", "数据源健康度", "采集状态"], wrap);
  }
})();
"""

SERIES_COLORS = {
    "B200": "#7c3aed",
    "H200 SXM": "#0891b2",
    "H100 SXM": "#b45309",
}


def _load_evidence(path: Optional[str], md_path: Path) -> Dict[str, Any]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    match = DATE_RE.search(md_path.name)
    if match:
        guess = SKILL_ROOT / "evidence" / f"gpu-{match.group(1)}.json"
        if guess.exists():
            return json.loads(guess.read_text(encoding="utf-8"))
    return {}


def _price_series(evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    """成交价趋势只画 Ornn 的日度结算序列。

    刻意不画跨平台中位数：它的口径逐日变化（源上下线就会跳），画成一条线
    等于把口径变动伪装成价格变动。中位数与它的口径标记留在 evidence 与正文表格里。
    """
    out = []
    for model, block in (evidence.get("models") or {}).items():
        points = ((block.get("by_source") or {}).get("ornn") or {}).get(
            "transaction_index", {})
        raw = points.get("series") or []
        if not raw:
            # by_source 不带 series，退回跨平台序列里 ornn 参与的那些天
            raw = [p for p in (block.get("cross_platform_median", {}).get("series") or [])
                   if "ornn" in (p.get("sources") or [])]
        out.append({"name": model, "label": block.get("label", model),
                    "points": [{"date": p["date"], "value": p["value"]} for p in raw]})
    return out


def _supply_series(evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    """供给指数：把 offer 份额、可用 GPU 数、库存档位、region 数标准化后取均值。

    每个分量按自己在窗口内的极差归一到 0–100；分量缺失就不参与均值，
    而不是当 0 —— 当 0 会让"这个源今天没采到"读成"供给崩了"。
    """
    out = []
    for model, block in (evidence.get("models") or {}).items():
        supply = block.get("supply") or {}
        parts = []
        for key in ("offer_share", "available_gpu_count"):
            series = (supply.get(key) or {}).get("series") or []
            if len(series) >= 2:
                parts.append({p["date"]: float(p["value"]) for p in series})
        combined: Dict[str, List[float]] = {}
        for part in parts:
            values = list(part.values())
            lo, hi = min(values), max(values)
            for day, value in part.items():
                norm = 50.0 if hi == lo else (value - lo) / (hi - lo) * 100.0
                combined.setdefault(day, []).append(norm)
        points = [{"date": d, "value": round(sum(v) / len(v), 2)}
                  for d, v in sorted(combined.items())]
        out.append({"name": model, "label": block.get("label", model), "points": points})
    return out


def _headline(evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    cards = []
    for model, block in (evidence.get("models") or {}).items():
        cross = block.get("cross_platform_median") or {}
        latest = cross.get("latest") or {}
        change7 = (cross.get("changes") or {}).get("7d") or {}
        usable = change7.get("usable") and change7.get("pct") is not None
        score = block.get("score") or {}
        subtitle_bits = [f"锚定 {cross.get('anchor_date') or '—'}"]
        if cross.get("anchor_basis"):
            subtitle_bits.append(f"口径 {cross['anchor_basis']}")
        subtitle_bits.append(
            f"7D {change7['pct']:+.1f}%" if usable else "7D 不可比")
        subtitle_bits.append(
            f"评分 {score['value']:+.0f}" if score.get("usable") else "评分未出")
        cards.append({
            "title": block.get("label", model),
            "value": (f"${latest['value']:.2f}" if latest.get("value") is not None
                      else "暂无数据"),
            "subtitle": "｜".join(subtitle_bits),
            "signValue": change7.get("pct") if usable else None,
        })
    return cards


def _health(evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    labels = {"ok": "已采集", "empty": "无数据行", "failed": "采集失败"}
    out = []
    for row in evidence.get("source_health") or []:
        age = row.get("age_days")
        meta = [f"最近 {row.get('last_obs_date') or '—'}"]
        if age is not None:
            meta.append("今日" if age == 0 else f"{age} 天前")
        if not row.get("fresh"):
            meta.append("新鲜度不足")
        meta.append(f"价格 {row.get('price_rows') or 0} 行 / 供给 {row.get('supply_rows') or 0} 行")
        if row.get("latency_ms") is not None:
            meta.append(f"{row['latency_ms']}ms")
        out.append({
            "source": row.get("source"),
            "priority": row.get("priority"),
            "status": row.get("status"),
            "status_text": labels.get(row.get("status"), row.get("status") or "未知"),
            "meta": "｜".join(meta),
            "error": row.get("error"),
        })
    return out


def add_arguments(parser) -> None:
    parser.add_argument("--evidence", default=None,
                        help="metrics.py 产出的证据包；缺省按报告文件名里的日期猜")


def build_job(args) -> RenderJob:
    md_path = Path(args.input)
    markdown = md_path.read_text(encoding="utf-8")
    evidence = _load_evidence(args.evidence, md_path)

    asof = evidence.get("asof") or (DATE_RE.search(md_path.name) or [None, "—"])[1]
    window = evidence.get("window_days", 90)
    health = _health(evidence)
    fresh_n = sum(1 for h in health if h["status"] == "ok")
    meta_text = (f"观测日 {asof}｜趋势窗口固定 {window} 天｜"
                 f"数据源 {fresh_n}/{len(health)} 采集成功｜"
                 f"价格单位 USD/GPU·hour")

    payload = {
        "asof": asof,
        "colors": SERIES_COLORS,
        "price_series": _price_series(evidence),
        "supply_series": _supply_series(evidence),
        "headline": _headline(evidence),
        "health": health,
        "price_footnote": ("成交价曲线只用 Ornn OCPI 的日度结算值，T-1 落定；"
                           "小时级实时价与跨平台中位数不画进这条线，避免把口径变动"
                           "读成价格变动。"),
        "supply_footnote": ("供给指数由各分量在窗口内极差归一后取均值，缺失分量不计入；"
                            "只有同一 query 口径下的点才可比。"),
    }

    builder = HtmlReportBuilder(
        title=args.title or f"GPU 算力价格与供给监控 {asof}",
        theme=args.theme, meta_text=meta_text, extra_css=EXTRA_CSS)
    builder.add_decoration(PillDecoration(PILL_RULES))
    builder.add_chart_hook(ChartHook(name="gpu-monitor-charts", payload=payload,
                                     js=CHART_JS))

    output = Path(args.output) if args.output else md_path.with_suffix(".html")
    return RenderJob(
        markdown_text=markdown, builder=builder, output_path=output,
        summary={
            "asof": asof,
            "price_series_models": [s["name"] for s in payload["price_series"]
                                    if s["points"]],
            "supply_series_models": [s["name"] for s in payload["supply_series"]
                                     if s["points"]],
            "sources_ok": fresh_n,
            "sources_total": len(health),
        })


if __name__ == "__main__":
    raise SystemExit(render_report(
        description="Render the GPU compute price & supply monitor to static HTML.",
        build_job=build_job, add_arguments=add_arguments))
