#!/usr/bin/env python3
"""Render a pool report with the shared theme and 200-session member K-lines."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED = _SCRIPT_DIR / "_shared"
_DEV = _SCRIPT_DIR.parents[2] / "shared"
sys.path.insert(0, str(_BUNDLED if (_BUNDLED / "html_report").exists() else _DEV))

from html_report import ChartHook, HtmlReportBuilder, PillDecoration, RenderJob, render_report  # noqa: E402

REMOVED_SECTION_TITLES = {"目录", "历史记录", "数据说明"}
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
_NUMBERING_RE = re.compile(
    r"^(?:第?[一二三四五六七八九十百0-9]+[、.．]|[（(][一二三四五六七八九十百0-9]+[）)])\s*"
)

PILL_RULES = [
    (r"^(健康|active)$", "pill"),
    (r"^分歧$", "pill alt"),
    (r"^(预警|watch)$", "pill warn"),
    (r"^(走坏|removed)$", "pill neg"),
]

CHARTS_JS = r"""
const members = Array.isArray(__payload.members) ? __payload.members : [];
if (!members.length) return;

const root = document.querySelector(".report") || document.body;
const heading = CK.findHeading(root, ["池内全景"]);
const tableWrap = heading ? CK.findNextTable(heading) : null;
if (!tableWrap) throw new Error("池内全景表不存在，K 线网格没有插入锚点");

const grid = CK.grid("stock-kline-grid pool-kline-grid");
grid.dataset.columns = "4";
grid.setAttribute("aria-label", "池内股票 200 日 K 线");
members.forEach(member => grid.appendChild(buildKlineCard(member)));
tableWrap.after(grid);

function buildKlineCard(member) {
  const requested = Math.max(1, Number(member.kline_days_requested) || 200);
  const rows = normalizeRows(member.kline_records).slice(-requested);
  const code = String(member.ts_code || "").split(".")[0];
  const name = String(member.name || code || "未命名股票");
  const entryDate = isoDate(member.entry_date);
  const firstDate = rows.length ? isoDate(rows[0].trade_date) : "";
  const lastDate = rows.length ? isoDate(rows[rows.length - 1].trade_date) : "";
  const subtitle = rows.length
    ? `${firstDate} 至 ${lastDate} · ${rows.length}/${requested} 个交易日 · 前复权`
    : "暂无 K 线数据";
  const card = CK.card("kline-card", `${name}（${code}）· ${requested} 日 K 线`, subtitle);
  card.dataset.tsCode = String(member.ts_code || "");
  card.dataset.entryDate = entryDate;
  card.dataset.recordCount = String(rows.length);

  const entryIndex = rows.findIndex(row => isoDate(row.trade_date) === entryDate);
  const entryNote = document.createElement("div");
  entryNote.className = "chart-subtitle pool-entry-note";
  if (!entryDate) entryNote.textContent = "入池日缺失";
  else if (entryIndex >= 0) entryNote.textContent = `入池日 ${entryDate} · 图中已标记`;
  else if (rows.length && entryDate < firstDate) entryNote.textContent = `入池日 ${entryDate} · 早于当前窗口`;
  else entryNote.textContent = `入池日 ${entryDate} · 窗口内无对应交易日`;
  card.appendChild(entryNote);

  if (rows.length) {
    card.appendChild(drawKline(rows, card, member, entryIndex));
    card.appendChild(CK.legend([
      ["K 线", "var(--neg)"],
      ["MA20", "var(--blue)"],
      ["MA60", "var(--orange)"],
      ["入池日", "var(--accent)"],
      ["成交金额", "rgba(100,116,139,0.55)"],
    ]));
  }
  return card;
}

function drawKline(rows, card, member, entryIndex) {
  const enriched = rows.map((row, idx) => ({
    ...row,
    idx,
    ma20: rollingAverage(rows, idx, 20),
    ma60: rollingAverage(rows, idx, 60),
  }));
  const width = 560, height = 340;
  const pad = { left: 52, right: 16, top: 26, bottom: 26 };
  const usableW = width - pad.left - pad.right;
  const amountPanelH = 58, panelGap = 16;
  const priceH = height - pad.top - pad.bottom - amountPanelH - panelGap;
  const amountTop = pad.top + priceH + panelGap;
  const amountBottom = amountTop + amountPanelH;
  const step = usableW / Math.max(rows.length, 1);
  const candleWidth = Math.max(1, Math.min(8, step * 0.62));
  const x = idx => pad.left + (idx + 0.5) * step;
  const svg = CK.svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `${member.name || member.ts_code}最近 ${rows.length} 个交易日 K 线`,
  });

  const prices = enriched.flatMap(row => [row.high, row.low, row.ma20, row.ma60]).filter(Number.isFinite);
  let min = Math.min(...prices), max = Math.max(...prices);
  if (!Number.isFinite(min) || !Number.isFinite(max)) { min = 0; max = 1; }
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.05;
  max += span * 0.08;
  const y = price => pad.top + (max - price) / (max - min) * priceH;
  const amounts = rows.map(row => row.amount).filter(value => Number.isFinite(value) && value > 0);
  const maxAmount = amounts.length ? Math.max(...amounts) : 1;
  const amountY = value => amountBottom - value / maxAmount * amountPanelH;

  for (let i = 0; i <= 4; i += 1) {
    const gy = pad.top + priceH * i / 4;
    svg.appendChild(CK.svgEl("line", {
      x1: pad.left, x2: width - pad.right, y1: gy, y2: gy, class: "grid-line",
    }));
  }
  svg.appendChild(CK.svgEl("line", {
    x1: pad.left, x2: width - pad.right, y1: amountBottom, y2: amountBottom, class: "axis",
  }));
  svg.appendChild(CK.svgEl("line", {
    x1: pad.left, x2: width - pad.right, y1: amountTop, y2: amountTop,
    class: "grid-line", opacity: 0.55,
  }));

  const tooltip = CK.tooltip(card);
  enriched.forEach(row => {
    const px = x(row.idx);
    const up = row.close >= row.open;
    const cls = up ? "kline-candle-up" : "kline-candle-down";
    if (Number.isFinite(row.amount) && row.amount > 0) {
      const barTop = amountY(row.amount);
      svg.appendChild(CK.svgEl("rect", {
        x: (px - candleWidth / 2).toFixed(2), y: barTop.toFixed(2),
        width: candleWidth.toFixed(2), height: Math.max(1, amountBottom - barTop).toFixed(2),
        class: `amount-bar ${up ? "amount-bar-up" : "amount-bar-down"}`, rx: 1,
      }));
    }
    const bodyTop = y(Math.max(row.open, row.close));
    const bodyBottom = y(Math.min(row.open, row.close));
    svg.appendChild(CK.svgEl("line", {
      x1: px.toFixed(2), x2: px.toFixed(2),
      y1: y(row.high).toFixed(2), y2: y(row.low).toFixed(2),
      class: `kline-wick ${cls}`,
    }));
    svg.appendChild(CK.svgEl("rect", {
      x: (px - candleWidth / 2).toFixed(2), y: bodyTop.toFixed(2),
      width: candleWidth.toFixed(2), height: Math.max(1, bodyBottom - bodyTop).toFixed(2),
      class: cls, rx: 1, opacity: 0.88,
    }));

    const hit = CK.svgEl("rect", {
      x: (px - Math.max(candleWidth, step) / 2).toFixed(2), y: pad.top,
      width: Math.max(candleWidth, step).toFixed(2), height: amountBottom - pad.top,
      fill: "transparent", stroke: "none", style: "cursor:pointer",
    });
    hit.addEventListener("mouseenter", () => {
      tooltip.innerHTML = [
        `<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${isoDate(row.trade_date)}${row.idx === entryIndex ? " · 入池日" : ""}</div>`,
        `<div>开 ${formatPrice(row.open)}　高 ${formatPrice(row.high)}</div>`,
        `<div>低 ${formatPrice(row.low)}　收 ${formatPrice(row.close)}</div>`,
        `<div>涨跌幅 ${formatPercent(row.pct_chg)} · 成交额 ${formatAmount(row.amount)}</div>`,
      ].join("");
      tooltip.style.opacity = "1";
    });
    hit.addEventListener("mousemove", event => CK.moveTip(tooltip, card, event));
    hit.addEventListener("mouseleave", () => { tooltip.style.opacity = "0"; });
    svg.appendChild(hit);
  });

  drawMaLine("ma20", "var(--blue)");
  drawMaLine("ma60", "var(--orange)");

  if (entryIndex >= 0) {
    const entryDate = isoDate(rows[entryIndex].trade_date);
    const px = x(entryIndex);
    const marker = CK.svgEl("g", {
      class: "pool-entry-marker",
      "data-entry-date": entryDate,
      "data-entry-index": entryIndex,
      "aria-label": `入池日 ${entryDate}`,
    });
    marker.appendChild(CK.svgEl("line", {
      x1: px.toFixed(2), x2: px.toFixed(2), y1: pad.top, y2: amountBottom,
      stroke: "var(--accent)", "stroke-width": 1.4, "stroke-dasharray": "5 3", opacity: 0.95,
    }));
    const labelOnLeft = px > width - pad.right - 105;
    marker.appendChild(CK.svgText(
      px + (labelOnLeft ? -4 : 4), pad.top + 11, `入池日 ${entryDate}`,
      labelOnLeft ? "end" : "start", "var(--accent)", 10,
    ));
    svg.appendChild(marker);
  }

  svg.appendChild(CK.svgText(4, pad.top + 4, formatPrice(max), "start", "var(--text-tertiary)", 10));
  svg.appendChild(CK.svgText(4, pad.top + priceH, formatPrice(min), "start", "var(--text-tertiary)", 10));
  svg.appendChild(CK.svgText(4, amountTop + 12, "成交金额", "start", "var(--text-tertiary)", 10));
  svg.appendChild(CK.svgText(4, amountTop + 28, formatAmount(maxAmount), "start", "var(--text-tertiary)", 10));
  svg.appendChild(CK.svgText(pad.left, height - 7, isoDate(rows[0].trade_date), "start", "var(--text-tertiary)", 10));
  svg.appendChild(CK.svgText(width - pad.right, height - 7, isoDate(rows[rows.length - 1].trade_date),
    "end", "var(--text-tertiary)", 10));
  return svg;

  function drawMaLine(key, color) {
    const points = enriched.filter(row => Number.isFinite(row[key]));
    if (!points.length) return;
    const d = points.map((row, idx) =>
      `${idx === 0 ? "M" : "L"} ${x(row.idx).toFixed(2)} ${y(row[key]).toFixed(2)}`).join(" ");
    svg.appendChild(CK.svgEl("path", { d, class: "ma-line", style: `stroke:${color}` }));
  }
}

function normalizeRows(records) {
  return (Array.isArray(records) ? records : []).map(row => ({
    trade_date: isoDate(row.trade_date),
    open: toNumber(row.open),
    high: toNumber(row.high),
    low: toNumber(row.low),
    close: toNumber(row.close),
    pct_chg: toNumber(row.pct_chg),
    amount: toNumber(row.amount),
  })).filter(row => row.trade_date && [row.open, row.high, row.low, row.close].every(Number.isFinite))
    .sort((a, b) => a.trade_date.localeCompare(b.trade_date));
}

function rollingAverage(rows, idx, windowSize) {
  if (idx + 1 < windowSize) return null;
  const values = rows.slice(idx - windowSize + 1, idx + 1).map(row => row.close);
  return values.every(Number.isFinite) ? values.reduce((sum, value) => sum + value, 0) / windowSize : null;
}

function isoDate(value) {
  const text = String(value || "");
  const match = text.match(/^(\d{4})-?(\d{2})-?(\d{2})/);
  return match ? `${match[1]}-${match[2]}-${match[3]}` : "";
}

function toNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatPrice(value) {
  if (!Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  return value.toFixed(abs >= 1000 ? 1 : abs >= 100 ? 2 : 3);
}

function formatPercent(value) {
  return Number.isFinite(value) ? `${value > 0 ? "+" : ""}${value.toFixed(2)}%` : "—";
}

function formatAmount(value) {
  return Number.isFinite(value) ? `${(value / 100000).toFixed(2)} 亿` : "—";
}
"""


def _canonical_heading(raw_title: str) -> str:
    title = re.sub(r"[*_`~]", "", raw_title).strip()
    title = re.sub(r"\s+#+\s*$", "", title).strip()
    return _NUMBERING_RE.sub("", title).strip()


def strip_legacy_sections(markdown_text: str) -> tuple[str, list[str]]:
    """Drop navigation/history sections; never silently discard legacy data caveats."""
    output: list[str] = []
    removed: list[str] = []
    skip_level: int | None = None
    skip_title = ""
    skipped_lines: list[str] = []
    in_fence = False

    def finish_removed_section() -> None:
        if skip_title == "数据说明" and any(line.strip() for line in skipped_lines):
            raise ValueError(
                "旧稿的“数据说明”仍有内容；请先把取数、停牌、主线日期等提示内联到对应章节，"
                "再渲染 HTML，避免静默丢失数据质量信息"
            )

    for line in markdown_text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            if skip_level is None:
                output.append(line)
            else:
                skipped_lines.append(line)
            continue
        heading = None if in_fence else _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = _canonical_heading(heading.group(2))
            if skip_level is not None:
                if level > skip_level:
                    skipped_lines.append(line)
                    continue
                finish_removed_section()
                skip_level = None
                skip_title = ""
                skipped_lines = []
            if level == 2 and title in REMOVED_SECTION_TITLES:
                removed.append(title)
                skip_level = level
                skip_title = title
                skipped_lines = []
                continue
        if skip_level is None:
            output.append(line)
        else:
            skipped_lines.append(line)
    finish_removed_section()
    result = "\n".join(output)
    if markdown_text.endswith(("\n", "\r")):
        result += "\n"
    return result, removed


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--track",
        default=None,
        help="pool_track.py 生成的 track.json；默认读取 Markdown 同目录的 track.json",
    )


def _load_track(md_path: Path, track_arg: str | None) -> tuple[dict[str, Any], Path]:
    track_path = Path(track_arg) if track_arg else md_path.with_name("track.json")
    if not track_path.exists():
        raise FileNotFoundError(
            f"缺少 K 线数据文件：{track_path}；请先运行 pool_track.py --output {track_path}"
        )
    data = json.loads(track_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"track JSON 顶层必须是对象：{track_path}")
    return data, track_path


def _chart_members(track: dict[str, Any]) -> list[dict[str, Any]]:
    members = []
    for member in track.get("members") or []:
        if not isinstance(member, dict):
            continue
        members.append({
            "ts_code": member.get("ts_code"),
            "name": member.get("name"),
            "entry_date": member.get("entry_date"),
            "kline_days_requested": member.get("kline_days_requested", 200),
            "kline_records": member.get("kline_records") or [],
        })
    return members


def build_job(args: argparse.Namespace) -> RenderJob:
    md_path = Path(args.input)
    original_markdown = md_path.read_text(encoding="utf-8")
    markdown_text, removed_sections = strip_legacy_sections(original_markdown)
    track, track_path = _load_track(md_path, args.track)
    members = _chart_members(track)

    asof = str(track.get("asof") or "")
    title = args.title or (f"涨停强势池日报 · {asof}" if asof else md_path.stem)
    builder = HtmlReportBuilder(title=title, theme=args.theme)
    builder.add_decoration(PillDecoration(PILL_RULES))
    if members:
        builder.add_chart_hook(ChartHook(
            name="pool-member-klines",
            payload={"members": members},
            js=CHARTS_JS,
        ))

    output = Path(args.output) if args.output else md_path.with_suffix(".html")
    return RenderJob(
        markdown_text=markdown_text,
        builder=builder,
        output_path=output,
        summary={
            "track": str(track_path),
            "track_loaded": True,
            "removed_sections": removed_sections,
            "kline_members": len(members),
            "kline_records": sum(len(member["kline_records"]) for member in members),
        },
    )


if __name__ == "__main__":
    raise SystemExit(render_report(
        description="Render the limit-up pool report with shared styling and member K-lines.",
        build_job=build_job,
        add_arguments=add_arguments,
    ))
