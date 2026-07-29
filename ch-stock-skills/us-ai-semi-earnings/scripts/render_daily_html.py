#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the daily brief (a Markdown file the model wrote) to standalone HTML.

Thin by design. The repo-level `shared/html_report` package owns the CLI,
Markdown conversion, themes, text-preservation validation and the chart
toolkit; everything below is the part that is specific to this skill — which
evidence file to read, and which two pictures are worth drawing.

Two charts, because two questions come up every night:

1. **Who moved and on what** — a scatter of EPS surprise against the price
   reaction. The interesting names are the off-diagonal ones: a beat the market
   sold, or a miss it bought, is a guidance story the numbers alone do not tell.
2. **Where the chain stands** — median revenue growth by supply-chain link, in
   transmission order, so a night's arrivals can be read against the links that
   already reported.

The season page (`render_period_html.py`) is the data-driven counterpart: it
projects the ledger for a whole quarter. This one renders one evening's
narrative.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED = _SCRIPT_DIR / "_shared"
_DEV = _SCRIPT_DIR.parents[2] / "shared"
sys.path.insert(0, str(_BUNDLED if (_BUNDLED / "html_report").exists() else _DEV))

from html_report import (  # noqa: E402
    ChartHook, HeroDecoration, HtmlReportBuilder, PillDecoration, RenderJob, render_report,
)

# Table cells that carry a verdict get coloured. The vocabulary is data, so a
# new tier label is a list edit, not a code change.
PILL_RULES = [
    (r"^(强|显著上修|确认)$", "pill"),
    (r"^(中|上修|部分确认)$", "pill alt"),
    (r"^(观察|维持|证据不足|尚早)$", "pill warn"),
    (r"^(剔除|下修|显著下修|背离)$", "pill neg"),
    (r"^(扎实)$", "pill"),
    (r"^(尚可)$", "pill alt"),
    (r"^(存疑|虚高)$", "pill neg"),
    (r"^(仅新闻稿|未取到|待补)$", "pill warn"),
]

CHARTS_JS = r"""
const data = __payload || {};
const root = document.querySelector(".report") || document.body;

// 1. EPS surprise vs the price reaction. Built by hand rather than with a
//    chartkit primitive because the kit has no scatter — the off-diagonal
//    points are the whole point, and only a scatter shows them.
if (Array.isArray(data.reactions) && data.reactions.length) {
  const pts = data.reactions;
  // Both axes are clamped. One company beating a small consensus by a wide
  // margin routinely prints a three-figure surprise, and letting it set the
  // scale collapses everyone else onto the origin — the off-diagonal pattern
  // this chart exists to show. Out-of-range points are pinned to the edge and
  // drawn hollow; the tooltip still carries the true number.
  const XCAP = 40, YCAP = 25;
  const clampedCount = pts.filter(p => Math.abs(p.surprise) > XCAP || Math.abs(p.move) > YCAP).length;
  const c = CK.card("chart-card", "EPS 超预期 vs 公告后股价反应",
    "右下＝超预期却被卖（多半是指引或利润质量的问题）；左上＝不及预期却被买（多半是指引超预期）"
    + (clampedCount ? `；空心点为超出坐标范围、已钉在边缘的 ${clampedCount} 家` : ""));
  const W = 560, H = 320, pad = { l: 52, r: 24, t: 20, b: 40 };
  const xs = pts.map(p => Math.min(Math.abs(p.surprise), XCAP));
  const ys = pts.map(p => Math.min(Math.abs(p.move), YCAP));
  const xMax = Math.max(6, ...xs), yMax = Math.max(6, ...ys);
  const cl = (v, cap) => Math.max(-cap, Math.min(cap, v));
  const X = v => pad.l + (cl(v, xMax) + xMax) / (2 * xMax) * (W - pad.l - pad.r);
  const Y = v => H - pad.b - (cl(v, yMax) + yMax) / (2 * yMax) * (H - pad.t - pad.b);
  const svg = CK.svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  const axis = (x1, y1, x2, y2) => svg.appendChild(CK.svgEl("line",
    { x1, y1, x2, y2, stroke: "rgba(100,116,139,0.45)", "stroke-width": 1 }));
  axis(pad.l, Y(0), W - pad.r, Y(0));
  axis(X(0), pad.t, X(0), H - pad.b);
  svg.appendChild(CK.svgText(W - pad.r, Y(0) - 7, "EPS 超预期 %", "end", "var(--ink-4)", 11));
  svg.appendChild(CK.svgText(X(0) + 6, pad.t + 4, "公告后股价 %", "start", "var(--ink-4)", 11));
  pts.forEach(p => {
    const off = Math.abs(p.surprise) > XCAP || Math.abs(p.move) > YCAP;
    const color = p.move >= 0 ? "var(--green, #16a34a)" : "var(--red, #dc2626)";
    const dot = CK.svgEl("circle", { cx: X(p.surprise), cy: Y(p.move), r: 5 });
    dot.setAttribute("fill", off ? "none" : color);
    dot.setAttribute("stroke", color);
    dot.setAttribute("stroke-width", off ? 1.8 : 0);
    dot.setAttribute("fill-opacity", "0.85");
    const t = CK.svgEl("title");
    t.textContent = `${p.ticker}  EPS ${p.surprise > 0 ? "+" : ""}${p.surprise}% / 股价 ${p.move > 0 ? "+" : ""}${p.move}%`
      + (off ? "（已钉在坐标边缘）" : "");
    dot.appendChild(t);
    svg.appendChild(dot);
    // Nudge the label to whichever side has room so edge-pinned points stay legible.
    const right = X(p.surprise) < W - pad.r - 46;
    svg.appendChild(CK.svgText(X(p.surprise) + (right ? 8 : -8), Y(p.move) + 4, p.ticker,
      right ? "start" : "end", "var(--ink-3)", 10));
  });
  c.appendChild(svg);
  CK.insertAfter(root, ["今晚一句话", "今晚", "谁报了"], c);
}

// 2. The chain, in transmission order — upstream first, so a reader can see
//    whether what the demand end promised has shown up downstream yet.
if (Array.isArray(data.chain) && data.chain.length) {
  const card = CK.horizontalBarCard({
    title: "产业链各环节营收同比中位数（按传导顺序，上游在前）",
    subtitle: "括号内为该环节已披露家数；空白＝该环节本季还没人报，不是零增长",
    // horizontalBarCard formats the value itself with fmt.signedPct, which
    // expects percentage units — yoy is already in percent, so it goes through
    // unscaled.
    rows: data.chain.map(r => ({ label: `${r.bucket} (${r.reported})`, value: r.yoy })),
    maxRows: 14, labelWidth: 190, valueWidth: 66,
  });
  CK.insertAfter(root, ["产业链", "传导", "交叉验证"], card);
}
"""


def add_arguments(parser: Any) -> None:
    parser.add_argument("--evidence", default=None,
                        help="scan JSON; charts are skipped when omitted")
    parser.add_argument("--frame", default=None, help="only used to locate the default evidence file")


def _charts(evidence: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not evidence:
        return {}
    reactions: List[Dict[str, Any]] = []
    for row in evidence.get("companies") or []:
        s = row.get("surprise") or {}
        surprise = s.get("surprise_pct")
        price = row.get("price_reaction") or {}
        # The move is whichever session actually reacted: US tech reports both
        # pre-open and post-close, so picking one session blanks out half the set.
        move = price.get("next_day_pct")
        if move is None or (price.get("same_day_pct") is not None
                            and abs(price["same_day_pct"]) > abs(move)):
            move = price.get("same_day_pct")
        if surprise is None or move is None:
            continue
        # A percentage surprise computed against a near-zero consensus is not a
        # comparable quantity — Intel beating 0.10 by 0.20 plots at +200% and
        # flattens every other company onto the origin. Those points are dropped
        # from the scatter rather than silently distorting it; the cents are
        # still in the evidence and in the table.
        if s.get("surprise_pct_unstable"):
            continue
        reactions.append({"ticker": row["ticker"], "surprise": round(surprise, 1),
                          "move": round(move, 1)})

    order: List[str] = []
    for chain in evidence.get("transmission_chains") or []:
        for link in chain.get("links") or []:
            if link["bucket"] not in order:
                order.append(link["bucket"])
    by_bucket = {b["bucket"]: b for b in evidence.get("buckets") or []}
    chain_rows = [{"bucket": b, "yoy": by_bucket[b]["median_revenue_yoy_pct"],
                   "reported": f"{by_bucket[b]['reported']}/{by_bucket[b]['members']}"}
                  for b in order if b in by_bucket]
    return {"reactions": reactions, "chain": chain_rows}


def build_job(args: Any) -> RenderJob:
    md_path = Path(args.input)
    markdown_text = md_path.read_text(encoding="utf-8")

    evidence = None
    ev_path = args.evidence
    if not ev_path and args.frame:
        ev_path = str(_SCRIPT_DIR.parent / "reports" / f"usearn_scan_{args.frame.upper()}.json")
    if ev_path and Path(ev_path).exists():
        try:
            evidence = json.loads(Path(ev_path).read_text(encoding="utf-8"))
        except ValueError:
            evidence = None

    meta = (evidence or {}).get("meta") or {}
    meta_bits = []
    if meta.get("frame"):
        meta_bits.append(f"报告期 {meta['frame']}")
    if meta.get("as_of"):
        meta_bits.append(f"数据截至 {meta['as_of']}")
    if meta.get("reported_count") is not None:
        meta_bits.append(f"已披露 {meta['reported_count']}/{meta.get('universe_size')}")
    ts = meta.get("transcript_stats") or {}
    if ts:
        meta_bits.append(f"电话会议 {ts.get('cached', 0) + ts.get('fetched', 0)} 份"
                         + (f"（{ts['pending']} 份待补）" if ts.get("pending") else ""))

    builder = HtmlReportBuilder(
        title=args.title or md_path.stem,
        theme=args.theme,
        meta_text=" · ".join(meta_bits) or None,
    )
    builder.add_decoration(PillDecoration(PILL_RULES))
    builder.add_decoration(HeroDecoration(heading_prefix="今晚一句话"))
    charts = _charts(evidence)
    if charts.get("reactions") or charts.get("chain"):
        builder.add_chart_hook(ChartHook(name="usearn-daily", payload=charts, js=CHARTS_JS))

    out = Path(args.output) if args.output else md_path.with_suffix(".html")
    return RenderJob(
        markdown_text=markdown_text, builder=builder, output_path=out,
        summary={"evidence": ev_path, "reaction_points": len(charts.get("reactions") or []),
                 "chain_links": len(charts.get("chain") or [])},
    )


if __name__ == "__main__":
    raise SystemExit(render_report(
        description="Render the daily US AI/semiconductor earnings brief to standalone HTML.",
        build_job=build_job, add_arguments=add_arguments))
