#!/usr/bin/env python3
"""Render a ch-news-reporter Markdown report as a self-contained HTML page.

All the generic machinery — CLI parsing, Markdown→HTML, theming, the chart kit
(``window.CK``), text-preservation validation and the pill/hero decorations —
comes from the shared ``html_report`` package (synced into ``scripts/_shared/``
as part of the whole ``shared`` bundle this skill already gets for db_core).

This file owns only the news-reporter-specific bits: inferring the report
profile from the filename, the per-profile pill vocabulary, the "一句话结论"
hero card, optional probability charts, and the ai_daily two-axis figures —
all configured in report_profiles.yaml. There is no judgement here: HTML is a
presentation layer over the finished Markdown, it adds no new conclusions and
drops no body text (the shared validator enforces the latter).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
from profile_config import DEFAULT_PROFILE_CONFIG, load_profile, load_profiles, render_config  # noqa: E402


# --------------------------------------------------------------------------- #
# Profile detection (from the report filename, e.g. macro_daily_2026-05-19.md)
# --------------------------------------------------------------------------- #
def profile_prefixes(config_path: Path) -> list[str]:
    return sorted(load_profiles(config_path), key=len, reverse=True)


def infer_profile(stem: str, config_path: Path) -> Optional[str]:
    for prefix in profile_prefixes(config_path):
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

PILL_SETS = {"macro": MACRO_PILLS, "geopolitical": IRAN_PILLS, "ai": AI_PILLS}


# --------------------------------------------------------------------------- #
# Generic probability chart (optional; needs a watchboard JSON).
# --------------------------------------------------------------------------- #
DEFAULT_PROBABILITY_LABELS = [("A", "A"), ("B", "B"), ("C", "C")]


def configured_probability_labels(render: dict[str, Any]) -> list[tuple[str, str]]:
    raw = render.get("probability_labels") or DEFAULT_PROBABILITY_LABELS
    labels: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, dict) and item.get("key"):
            labels.append((str(item["key"]), str(item.get("label") or item["key"])))
        elif isinstance(item, (list, tuple)) and item:
            key = str(item[0])
            labels.append((key, str(item[1] if len(item) > 1 else key)))
    return labels or DEFAULT_PROBABILITY_LABELS


def load_probabilities(
    watchboard_path: Optional[Path],
    labels: list[tuple[str, str]],
) -> Optional[Dict[str, object]]:
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
    for key, _label in labels:
        try:
            clean[key] = float(probs[key])
        except (KeyError, TypeError, ValueError):
            continue
    if not clean:
        return None
    return {
        "probabilities": clean,
        "path": str((frame or {}).get("path") or ""),
        "labels": [{"key": key, "label": label} for key, label in labels],
    }


PATH_PROB_JS = r"""
const probs = (__payload.probabilities) || {};
const curPath = String(__payload.path || "").trim().charAt(0);
const root = document.getElementById("report-body");
if (!root) return;
const labels = (__payload.labels || []).map(r => [String(r.key || ""), String(r.label || r.key || "")]);
const rows = labels.filter(r => r[0] && Number.isFinite(Number(probs[r[0]])));
if (!rows.length) return;

// anchor right after the 一句话结论 hero card, else after the first heading
const anchor = root.querySelector(".summary-card") || root.querySelector("h2, h3");
if (!anchor) return;

const grid = CK.grid("chart-grid");
const card = CK.card("chart-card", "路径概率", curPath ? `当前路径 ${curPath}` : "");
const palette = ["var(--pos)", "var(--orange)", "var(--neg)", "var(--accent)", "var(--violet)"];
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
  const color = palette[i % palette.length];
  svg.appendChild(CK.svgText(padL - 8, y + 17, label, "end", "var(--ink-3)", 12));
  svg.appendChild(CK.svgEl("rect", { x: padL, y: y + 5, width: trackW.toFixed(1), height: 18, rx: 4, fill: "var(--accent-soft)", opacity: 0.3 }));
  svg.appendChild(CK.svgEl("rect", { x: padL, y: y + 5, width: barW.toFixed(1), height: 18, rx: 4, fill: color, opacity: active ? 1 : 0.6 }));
  svg.appendChild(CK.svgText(padL + barW + 7, y + 18, v + "%", "start", active ? "var(--ink-2)" : "var(--ink-4)", 12));
});
card.appendChild(svg);
grid.appendChild(card);
anchor.after(grid);
"""


# --------------------------------------------------------------------------- #
# ai_daily two-axis figures (optional; needs a watchboard JSON, and PostgreSQL
# for the cross-day parts). Static SVG rather than a ChartHook so the charts
# survive readers that block inline scripts — pushplus / WeChat being the ones
# these reports actually get read in.
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def report_date(watchboard: Dict[str, Any], stem: str) -> Optional[str]:
    for candidate in (str(watchboard.get("as_of") or ""), stem):
        found = DATE_RE.search(candidate)
        if found:
            return found.group(1)
    return None


def load_state_history(
    profile_name: str, end_date: Optional[str], days: int
) -> List[Tuple[str, Dict[str, Any]]]:
    """Oldest→newest (date_key, payload) for the trailing window.

    Best-effort: any database problem degrades to an empty history, which drops
    the migration band and the movement arrows but keeps the map itself.
    """
    if not end_date or days < 2:
        return []
    try:
        from db_adapter import get_connection, get_report_state_series  # noqa: E402

        start = (date.fromisoformat(end_date) - timedelta(days=days - 1)).isoformat()
        with get_connection() as conn:
            rows = get_report_state_series(conn, profile_name, start, end_date)
    except Exception:  # noqa: BLE001 — charts must never break the render
        return []
    history: List[Tuple[str, Dict[str, Any]]] = []
    for row in rows:
        raw = row.get("payload")
        payload = raw if isinstance(raw, dict) else json.loads(str(raw or "{}"))
        if isinstance(payload, dict):
            history.append((str(row.get("date_key")), payload))
    return history


def load_axis_figures(
    args, profile_name: Optional[str], profile_render: Dict[str, Any], stem: str
) -> List[Any]:
    if not (profile_render.get("axis_chart") and args.watchboard and profile_name):
        return []
    path = Path(args.watchboard)
    if not path.exists():
        return []
    try:
        watchboard = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if not isinstance(watchboard, dict):
        return []
    from ai_axis_charts import build_figures  # noqa: E402

    history = load_state_history(profile_name, report_date(watchboard, stem), args.history_days)
    return build_figures(watchboard, history)


# --------------------------------------------------------------------------- #
# Title / meta derived from the report's H1 line (e.g. "# 每日宏观日报 | 2026-05-19").
# --------------------------------------------------------------------------- #
def derive_title_meta(
    markdown_text: str,
    stem: str,
    profile_name: Optional[str],
    profile: Optional[dict[str, Any]],
) -> Tuple[str, str]:
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
    label = str((profile or {}).get("title") or profile_name or "")
    meta_bits = [bit for bit in [label if label and label != name else "", date] if bit]
    return name, " · ".join(meta_bits)


# --------------------------------------------------------------------------- #
# Thin manifest.
# --------------------------------------------------------------------------- #
def add_arguments(parser) -> None:
    parser.add_argument(
        "--config", default=str(DEFAULT_PROFILE_CONFIG), help="Report profiles YAML path."
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Report profile. Defaults to the prefix inferred from the input filename.",
    )
    parser.add_argument(
        "--watchboard",
        default=None,
        help="Optional watchboard JSON. Profiles can enable a probability chart "
             "or the ai_daily two-axis figures in config.",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=16,
        help="Trailing window (days) of report_state used by the ai_daily migration band.",
    )


def build_job(args) -> RenderJob:
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_suffix(".html")
    markdown_text = input_path.read_text(encoding="utf-8")
    config_path = Path(args.config)
    profile_name = args.profile or infer_profile(input_path.stem, config_path)
    profile = load_profile(profile_name, config_path) if profile_name else None
    profile_render = render_config(profile or {}) if profile else {}
    title_name, meta_text = derive_title_meta(markdown_text, input_path.stem, profile_name, profile)
    title = args.title or title_name

    builder = HtmlReportBuilder(title=title, theme=args.theme, meta_text=meta_text)
    # Every news report leads with "一句话结论" — promote it into a hero card.
    builder.add_decoration(HeroDecoration(heading_prefix="一句话结论", stop_mode="section"))
    pill_set = str(profile_render.get("pill_set") or "")
    pill_rules = PILL_SETS.get(pill_set)
    if pill_rules:
        builder.add_decoration(PillDecoration(pill_rules))

    chart_added = False
    if bool(profile_render.get("probability_chart")):
        wb_path = Path(args.watchboard) if args.watchboard else None
        payload = load_probabilities(wb_path, configured_probability_labels(profile_render))
        if payload is not None:
            builder.add_chart_hook(ChartHook(name="profile-probabilities", payload=payload, js=PATH_PROB_JS))
            chart_added = True

    figures = load_axis_figures(args, profile_name, profile_render, input_path.stem)
    for figure in figures:
        builder.add_figure(figure)

    return RenderJob(
        markdown_text=markdown_text,
        builder=builder,
        output_path=output_path,
        summary={
            "profile": profile_name,
            "pills": bool(pill_rules),
            "probability_chart": chart_added,
            "axis_figures": len(figures),
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
