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

insertOverview();
insertSectorCharts();
insertPoolCharts();
insertNewDirections();

function insertOverview() {
  const qqq = data.index || {};
  const stats = data.stats || {};
  const top = (data.themes || []).slice().sort((a, b) => (b.dollar_vol_share || 0) - (a.dollar_vol_share || 0))[0];
  const grid = CK.metricGrid([
    {
      title: "QQQ",
      value: fmtPct(CK.num(qqq.change_pct)),
      subtitle: `52周位置 ${pct0(qqq.position_52w)} · 量比 ${ratX(qqq.vol_vs_20d)}`,
      signValue: CK.num(qqq.change_pct)
    },
    {
      title: "观察池",
      value: `${stats.up_count || 0}/${stats.valid_count || 0}`,
      subtitle: `上涨 / 有效 · 异动 ${stats.watchlist_abnormal_count || 0} 只`
    },
    {
      title: "今晚主线",
      value: top ? (stars(top.stars) || "—") : "—",
      subtitle: top ? `${top.name} · ${pct0(top.dollar_vol_share)}` : "无确认主线 / 未落台账"
    }
  ]);
  CK.insertAfter(root, ["大盘", "QQQ"], grid);
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
  const rel = poolRelativeCard(data.pool_relative);
  if (!rel) return;
  const grid = CK.grid("chart-grid");
  grid.appendChild(rel);
  CK.insertAfter(root, ["观察池个股明细", "观察池"], grid);
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
    for item in evidence.get("indices") or []:
        if not isinstance(item, dict):
            continue
        ticker = item.get("ticker") or ((item.get("snapshot") or {}).get("ticker"))
        if str(ticker).upper() == "QQQ":
            qqq = compact_snapshot("QQQ", item.get("snapshot"), name=(item.get("snapshot") or {}).get("name"))
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
