#!/usr/bin/env python3
"""Render a ch-news-reporter Markdown report as a self-contained HTML page.

All the generic machinery — CLI parsing, Markdown→HTML, theming, the chart kit
(``window.CK``), text-preservation validation and the pill/hero decorations —
comes from the shared ``html_report`` package (synced into ``scripts/_shared/``
as part of the whole ``shared`` bundle this skill already gets for db_core).

This file owns only the news-reporter-specific bits: inferring the report
profile from the filename, the per-profile pill vocabulary, the "一句话结论"
hero card, and — for ``iran_dynamic`` — an optional path-probability chart read
from a watchboard JSON. There is no judgement here: HTML is a presentation
layer over the finished Markdown, it adds no new conclusions and drops no body
text (the shared validator enforces the latter).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_ROOT = Path(__file__).resolve().parent
_BUNDLED_SHARED = SCRIPT_ROOT / "_shared"
_DEV_SHARED = SCRIPT_ROOT.parents[1] / "shared"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

from html_report import (  # noqa: E402
    ChartHook,
    HeroDecoration,
    HtmlReportBuilder,
    PillDecoration,
    RenderJob,
    render_report,
)


# --------------------------------------------------------------------------- #
# Profile detection (from the report filename, e.g. macro_daily_2026-05-19.md)
# --------------------------------------------------------------------------- #
PROFILE_LABELS = {
    "iran_dynamic": "美以伊动态播报",
    "macro_daily": "每日宏观日报",
    "ai_daily": "AI 日报",
}
# longest / most-specific prefix first so "iran_dynamic" wins before any short alias
_PROFILE_PREFIXES = sorted(PROFILE_LABELS, key=len, reverse=True)


def infer_profile(stem: str) -> Optional[str]:
    for prefix in _PROFILE_PREFIXES:
        if stem.startswith(prefix):
            return prefix
    return None


# --------------------------------------------------------------------------- #
# Per-profile pill vocabulary. Pills only fire on plain-text cells (the shared
# decoration skips cells that already contain markup, e.g. **bolded** ones), so
# these target the un-bolded category columns each report uses.
# --------------------------------------------------------------------------- #
MACRO_PILLS = [
    (r"^数据事件$", "pill violet"),
    (r"^政策表态$", "pill"),
    (r"^预期调整$", "pill warn"),
    (r"^二次解读$", "pill warn"),
    (r"^地缘影响$", "pill neg"),
    (r"^(高位|高)$", "pill neg"),
    (r"^中(性|位)?$", "pill warn"),
    (r"^低(位)?$", "pill pos"),
    (r"^(收紧|偏紧)$", "pill neg"),
    (r"^(宽松|偏松|降温)$", "pill pos"),
]

IRAN_PILLS = [
    (r"^A$", "pill pos"),    # A 续期 / 降级
    (r"^B$", "pill neg"),    # B 交战 / 升级
    (r"^C$", "pill warn"),   # C 僵尸化 / 僵持
    (r"^(升级|恶化)$", "pill neg"),
    (r"^(缓和|降级)$", "pill pos"),
    (r"^(僵持|维持)$", "pill warn"),
]

AI_PILLS = [
    (r"^(收敛|趋势|确认)$", "pill pos"),
    (r"^(热度|噪音|存疑)$", "pill warn"),
]

PROFILE_PILLS = {"macro_daily": MACRO_PILLS, "iran_dynamic": IRAN_PILLS, "ai_daily": AI_PILLS}


# --------------------------------------------------------------------------- #
# iran_dynamic path-probability chart (optional; needs a watchboard JSON).
# --------------------------------------------------------------------------- #
PATH_LABELS = [("A", "A 续期"), ("B", "B 交战"), ("C", "C 僵尸化")]


def load_path_probabilities(watchboard_path: Optional[Path]) -> Optional[Dict[str, object]]:
    if watchboard_path is None or not watchboard_path.exists():
        return None
    try:
        wb = json.loads(watchboard_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    frame = wb.get("frame") if isinstance(wb, dict) else None
    probs = (frame or {}).get("probabilities") if isinstance(frame, dict) else None
    if not isinstance(probs, dict):
        return None
    clean: Dict[str, float] = {}
    for key, _label in PATH_LABELS:
        try:
            clean[key] = float(probs[key])
        except (KeyError, TypeError, ValueError):
            continue
    if not clean:
        return None
    return {"probabilities": clean, "path": str((frame or {}).get("path") or "")}


PATH_PROB_JS = r"""
const probs = (__payload.probabilities) || {};
const curPath = String(__payload.path || "").trim().charAt(0);
const root = document.getElementById("report-body");
if (!root) return;
const rows = [["A", "A 续期"], ["B", "B 交战"], ["C", "C 僵尸化"]].filter(r => Number.isFinite(Number(probs[r[0]])));
if (!rows.length) return;

// anchor right after the 一句话结论 hero card, else after the first heading
const anchor = root.querySelector(".summary-card") || root.querySelector("h2, h3");
if (!anchor) return;

const grid = CK.grid("chart-grid");
const card = CK.card("chart-card", "路径概率", curPath ? `当前路径 ${curPath}` : "");
const colors = { A: "var(--pos)", B: "var(--neg)", C: "var(--orange)" };
const W = 480, padL = 88, padR = 52, padT = 12, rowH = 34;
const H = padT * 2 + rows.length * rowH;
const trackW = W - padL - padR;
const svg = CK.svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
rows.forEach((r, i) => {
  const [key, label] = r;
  const v = Number(probs[key]) || 0;
  const y = padT + i * rowH;
  const barW = Math.max(1, trackW * (v / 100));
  const active = key === curPath;
  svg.appendChild(CK.svgText(padL - 8, y + 17, label, "end", "var(--ink-3)", 12));
  svg.appendChild(CK.svgEl("rect", { x: padL, y: y + 5, width: trackW.toFixed(1), height: 18, rx: 4, fill: "var(--accent-soft)", opacity: 0.3 }));
  svg.appendChild(CK.svgEl("rect", { x: padL, y: y + 5, width: barW.toFixed(1), height: 18, rx: 4, fill: colors[key] || "var(--accent)", opacity: active ? 1 : 0.6 }));
  svg.appendChild(CK.svgText(padL + barW + 7, y + 18, v + "%", "start", active ? "var(--ink-2)" : "var(--ink-4)", 12));
});
card.appendChild(svg);
grid.appendChild(card);
anchor.after(grid);
"""


# --------------------------------------------------------------------------- #
# Title / meta derived from the report's H1 line (e.g. "# 每日宏观日报 | 2026-05-19").
# --------------------------------------------------------------------------- #
def derive_title_meta(markdown_text: str, stem: str, profile: Optional[str]) -> Tuple[str, str]:
    h1 = ""
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            h1 = line[2:].strip()
            break
    name, date = (stem, "")
    if h1:
        parts = [p.strip() for p in h1.split("|", 1)]
        name = parts[0] or stem
        date = parts[1] if len(parts) > 1 else ""
    label = PROFILE_LABELS.get(profile or "", "")
    meta_bits = [bit for bit in [label if label and label != name else "", date] if bit]
    return name, " · ".join(meta_bits)


# --------------------------------------------------------------------------- #
# Thin manifest.
# --------------------------------------------------------------------------- #
def add_arguments(parser) -> None:
    parser.add_argument(
        "--profile",
        default=None,
        choices=sorted(PROFILE_LABELS),
        help="Report profile. Defaults to the prefix inferred from the input filename.",
    )
    parser.add_argument(
        "--watchboard",
        default=None,
        help="Optional watchboard JSON. For iran_dynamic, its frame.probabilities draws a path-probability chart.",
    )


def build_job(args) -> RenderJob:
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_suffix(".html")
    markdown_text = input_path.read_text(encoding="utf-8")
    profile = args.profile or infer_profile(input_path.stem)
    title_name, meta_text = derive_title_meta(markdown_text, input_path.stem, profile)
    title = args.title or title_name

    builder = HtmlReportBuilder(title=title, theme=args.theme, meta_text=meta_text)
    # Every news report leads with "一句话结论" — promote it into a hero card.
    builder.add_decoration(HeroDecoration(heading_prefix="一句话结论", stop_mode="section"))
    pill_rules = PROFILE_PILLS.get(profile or "")
    if pill_rules:
        builder.add_decoration(PillDecoration(pill_rules))

    chart_added = False
    if profile == "iran_dynamic":
        wb_path = Path(args.watchboard) if args.watchboard else None
        payload = load_path_probabilities(wb_path)
        if payload is not None:
            builder.add_chart_hook(ChartHook(name="path-probabilities", payload=payload, js=PATH_PROB_JS))
            chart_added = True

    return RenderJob(
        markdown_text=markdown_text,
        builder=builder,
        output_path=output_path,
        summary={
            "profile": profile,
            "pills": bool(pill_rules),
            "path_probability_chart": chart_added,
        },
    )


if __name__ == "__main__":
    raise SystemExit(
        render_report(
            description="Render a ch-news-reporter Markdown report to static HTML.",
            build_job=build_job,
            add_arguments=add_arguments,
        )
    )
