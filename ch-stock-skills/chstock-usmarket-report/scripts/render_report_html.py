#!/usr/bin/env python3
"""Render a US-market watchlist Markdown report as self-contained HTML.

The Markdown report remains the truth source. This wrapper only builds the
browser layer: frontmatter stripping, generic Markdown->HTML rendering, and a
small evidence-driven chart hook for QQQ / watchlist / market-mover context.
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


USMARKET_EXTRA_CSS = """
.usmetric-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin: 16px 0 8px; }
.usmetric-card { background: var(--surface); border: 1px solid var(--line-2); border-radius: var(--r-md); box-shadow: var(--shadow-1); padding: 14px 16px 13px; }
.usmetric-title { color: var(--muted); font-size: 12px; font-weight: 500; margin-bottom: 5px; }
.usmetric-value { font-family: var(--font-mono); font-size: 24px; line-height: 1.2; color: var(--ink-1); }
.usmetric-sub { color: var(--muted); font-size: 12px; line-height: 1.6; margin-top: 5px; }
.usmetric-value.pos { color: var(--neg); }
.usmetric-value.neg { color: var(--pos); }
@media (max-width: 760px) { .usmetric-grid { grid-template-columns: 1fr; } }
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

const { svgEl, svgText } = CK;
const fmtPct = CK.fmt.signedPct;
const fmtNum = CK.fmt.num;
const fmtPrice = v => Number.isFinite(v) ? "$" + v.toFixed(2) : "—";
const fmtCap = v => Number.isFinite(v) ? "$" + v.toFixed(1) + "B" : "—";
const clsFor = v => {
  const n = Number(v);
  if (!Number.isFinite(n) || n === 0) return "";
  return n > 0 ? "pos" : "neg";
};

insertOverview();
insertWatchlistCharts();
insertMoverCharts();

function insertOverview() {
  const qqq = data.index || {};
  const stats = data.stats || {};
  const scan = data.market_scan || {};
  const grid = document.createElement("div");
  grid.className = "usmetric-grid";
  grid.appendChild(metricCard("QQQ", fmtPct(num(qqq.change_pct)), `收盘 ${fmtPrice(num(qqq.close))} · 5日 ${fmtPct(num(qqq.five_day_trend_pct))}`, num(qqq.change_pct)));
  grid.appendChild(metricCard("观察池", `${stats.up_count || 0}/${stats.valid_count || 0}`, `上涨个股 / 有效个股 · 异动 ${stats.watchlist_abnormal_count || 0} 只`, null));
  grid.appendChild(metricCard("全市场扫描", `${scan.rise_count || 0}/${scan.drop_count || 0}`, `上涨/下跌 Top · scan_date=${scan.scan_date || "—"}`, null));
  insertAfter(["大盘", "QQQ"], grid);
}

function insertWatchlistCharts() {
  const groupRows = (data.groups || []).filter(r => Number.isFinite(num(r.avg_change_pct)));
  const topRows = (data.watchlist_top || []).filter(r => Number.isFinite(num(r.change_pct)));
  if (!groupRows.length && !topRows.length) return;

  const grid = CK.grid("chart-grid");
  if (groupRows.length) {
    grid.appendChild(horizontalBarCard(
      "观察池分组均值",
      "按各组有效成员当日涨跌均值",
      groupRows.map(r => ({ label: r.name || "未命名", value: num(r.avg_change_pct), meta: `${r.up_count || 0}/${r.valid_count || 0}` }))
    ));
  }
  if (topRows.length) {
    grid.appendChild(horizontalBarCard(
      "观察池波动前列",
      "按 |当日涨跌幅| 排序，最多 12 只",
      topRows.map(r => ({ label: r.ticker, value: num(r.change_pct), meta: r.group_names || "" }))
    ));
  }
  insertAfter(["观察池个股明细"], grid);
}

function insertMoverCharts() {
  const marketTop = (data.market_top || []).filter(r => Number.isFinite(num(r.change_pct)));
  const abnormal = (data.watchlist_abnormal || []).filter(r => Number.isFinite(num(r.change_pct)));
  if (!marketTop.length && !abnormal.length) return;

  const grid = CK.grid("chart-grid");
  if (abnormal.length) {
    grid.appendChild(horizontalBarCard(
      "观察池 ±7% 异动",
      "进入异动核查流程的自选票",
      abnormal.map(r => ({ label: r.ticker, value: num(r.change_pct), meta: r.group_names || "" }))
    ));
  }
  if (marketTop.length && (data.market_scan || {}).date_aligned !== false) {
    grid.appendChild(horizontalBarCard(
      "全市场大票异动",
      "市值 >= $10B，按 |涨跌幅| 排序",
      marketTop.map(r => ({ label: r.ticker, value: num(r.change_pct), meta: fmtCap(num(r.market_cap_billion)) }))
    ));
  }
  insertAfter(["异动扫描"], grid);
}

function metricCard(title, value, sub, signValue) {
  const card = document.createElement("article");
  card.className = "usmetric-card";
  const titleEl = document.createElement("div");
  titleEl.className = "usmetric-title";
  titleEl.textContent = title;
  const valueEl = document.createElement("div");
  valueEl.className = `usmetric-value ${clsFor(signValue)}`;
  valueEl.textContent = value;
  const subEl = document.createElement("div");
  subEl.className = "usmetric-sub";
  subEl.textContent = sub;
  card.append(titleEl, valueEl, subEl);
  return card;
}

function horizontalBarCard(title, subtitle, rows) {
  rows = rows.slice(0, 12);
  const card = CK.card("chart-card", title, subtitle);
  const W = 520, rowH = 24, top = 18, bottom = 20;
  const H = top + rows.length * rowH + bottom;
  const pad = { l: 94, r: 58, t: top, b: bottom };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  const maxAbs = Math.max(1, ...rows.map(r => Math.abs(num(r.value) || 0)));
  const zeroX = pad.l + (W - pad.l - pad.r) / 2;
  const scale = (W - pad.l - pad.r) / 2 / maxAbs;

  svg.appendChild(svgEl("line", { x1: zeroX, x2: zeroX, y1: pad.t - 8, y2: H - pad.b + 4, class: "grid-line" }));
  rows.forEach((row, i) => {
    const y = pad.t + i * rowH;
    const value = num(row.value) || 0;
    const width = Math.abs(value) * scale;
    const x = value >= 0 ? zeroX : zeroX - width;
    svg.appendChild(svgText(8, y + 13, row.label || "", "start", "var(--ink-2)", 10));
    svg.appendChild(svgEl("rect", {
      x: x.toFixed(2),
      y: (y + 3).toFixed(2),
      width: Math.max(1, width).toFixed(2),
      height: 12,
      rx: 3,
      fill: value >= 0 ? "var(--neg)" : "var(--pos)",
      opacity: 0.82
    }));
    svg.appendChild(svgText(W - 8, y + 13, fmtPct(value), "end", value >= 0 ? "var(--neg)" : "var(--pos)", 10));
    if (row.meta) svg.appendChild(svgText(pad.l - 8, y + 13, row.meta, "end", "var(--ink-4)", 9));
  });
  card.appendChild(svg);
  return card;
}

function insertAfter(texts, node) {
  const heading = CK.findHeading(root, texts);
  if (heading) {
    heading.after(node);
    return;
  }
  const h1 = root.querySelector("h1");
  if (h1 && h1.nextElementSibling) h1.nextElementSibling.before(node);
  else root.prepend(node);
}

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
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


def load_evidence(path: Optional[Path]) -> dict:
    if path is None or not path.exists():
        return {
            "metadata": {
                "missing": True,
                "source": str(path) if path is not None else "",
            }
        }
    return json.loads(path.read_text(encoding="utf-8"))


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
        "volume": snapshot.get("volume"),
    }
    row.update(extra)
    return row


def extract_chart_payload(evidence: dict, source_path: Optional[Path]) -> Dict[str, Any]:
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

    watchlist_rows.sort(key=lambda item: abs(item.get("change_pct") or 0), reverse=True)

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

    market_wide = evidence.get("market_wide_movers") or {}
    market_top: List[Dict[str, Any]] = []
    for bucket in ("rises", "drops"):
        for item in (market_wide.get(bucket) or []):
            if not isinstance(item, dict):
                continue
            market_top.append(
                {
                    "ticker": item.get("ticker"),
                    "name": item.get("name"),
                    "change_pct": to_number(item.get("change_pct")),
                    "market_cap_billion": to_number(item.get("market_cap_billion")),
                }
            )
    market_top.sort(key=lambda item: abs(item.get("change_pct") or 0), reverse=True)

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
        "watchlist_top": watchlist_rows[:12],
        "watchlist_abnormal": abnormal[:12],
        "market_top": market_top[:12],
        "market_scan": {
            "scan_date": market_wide.get("scan_date"),
            "date_aligned": market_wide.get("date_aligned"),
            "rise_count": len(market_wide.get("rises") or []),
            "drop_count": len(market_wide.get("drops") or []),
        },
    }


def extract_title(markdown_text: str, fallback: str) -> str:
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def add_arguments(parser: Any) -> None:
    parser.add_argument("--evidence", default=None, help="Evidence JSON path. Defaults to a date-matched outputs/us-YYYY-MM-DD.json if found.")
    parser.add_argument("--keep-frontmatter", action="store_true", help="Render YAML frontmatter as visible content. Default strips it.")


def build_job(args: Any) -> RenderJob:
    input_path = Path(args.input).expanduser().resolve()
    raw_markdown = input_path.read_text(encoding="utf-8")
    markdown_text, frontmatter_stripped = (raw_markdown, False) if args.keep_frontmatter else strip_frontmatter(raw_markdown)

    evidence_path = Path(args.evidence).expanduser().resolve() if args.evidence else default_evidence_path(input_path, markdown_text)
    evidence = load_evidence(evidence_path)
    payload = extract_chart_payload(evidence, evidence_path)

    title = args.title or extract_title(markdown_text, input_path.stem)
    meta_date = payload.get("metadata", {}).get("date") or date_from_text(markdown_text) or "unknown"
    meta_text = f"US Market Watchlist | date={meta_date}"
    if evidence_path:
        meta_text += f" | evidence={evidence_path.name}"

    builder = HtmlReportBuilder(
        title=title,
        theme=args.theme,
        meta_text=meta_text,
        extra_css=USMARKET_EXTRA_CSS,
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
            "data_date": meta_date,
            "frontmatter_stripped": frontmatter_stripped,
            "charts": {
                "groups": len(payload.get("groups") or []),
                "watchlist_top": len(payload.get("watchlist_top") or []),
                "market_top": len(payload.get("market_top") or []),
            },
        },
    )


if __name__ == "__main__":
    raise SystemExit(
        render_report(
            description="Render a US-market watchlist Markdown report to static HTML.",
            build_job=build_job,
            add_arguments=add_arguments,
        )
    )
