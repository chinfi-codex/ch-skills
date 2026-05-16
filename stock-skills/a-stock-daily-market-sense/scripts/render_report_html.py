#!/usr/bin/env python3
"""Render a daily-market-sense Markdown report as a self-contained HTML page."""

from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


SCRIPT_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_ROOT.parent
DEFAULT_MARKET_DATA = SKILL_ROOT / "reference" / "market_data.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a Markdown daily market report to static HTML.")
    parser.add_argument("--input", "-i", required=True, help="Markdown report path, e.g. reports/report_YYYYMMDD.md.")
    parser.add_argument("--output", "-o", default=None, help="HTML output path. Defaults to input path with .html suffix.")
    parser.add_argument("--market-data", default=str(DEFAULT_MARKET_DATA), help="Derived market_data.json path.")
    parser.add_argument("--title", default=None, help="HTML document title.")
    parser.add_argument("--no-validate", action="store_true", help="Skip Markdown text preservation validation.")
    return parser


def is_table_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and set(stripped) <= {"|", "-", ":", " "}


def inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    code_values: List[str] = []

    def keep_code(match: re.Match[str]) -> str:
        code_values.append(f"<code>{match.group(1)}</code>")
        return f"@@CODE{len(code_values) - 1}@@"

    escaped = re.sub(r"`([^`]+)`", keep_code, escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)
    for idx, value in enumerate(code_values):
        escaped = escaped.replace(f"@@CODE{idx}@@", value)
    return escaped


def flush_paragraph(parts: List[str], out: List[str]) -> None:
    if not parts:
        return
    text = " ".join(part.strip() for part in parts if part.strip())
    if text:
        out.append(f"<p>{inline_markdown(text)}</p>")
    parts.clear()


def render_table(lines: List[str]) -> str:
    rows: List[List[str]] = []
    aligns: List[str] = []
    for idx, line in enumerate(lines):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if idx == 1 and is_table_separator(line):
            for cell in cells:
                if cell.startswith(":") and cell.endswith(":"):
                    aligns.append("center")
                elif cell.endswith(":"):
                    aligns.append("right")
                else:
                    aligns.append("left")
            continue
        rows.append(cells)

    if not rows:
        return ""

    header = rows[0]
    body = rows[1:]
    html_rows = ["<div class=\"table-wrap\"><table>"]
    html_rows.append("<thead><tr>")
    for idx, cell in enumerate(header):
        align = aligns[idx] if idx < len(aligns) else "left"
        html_rows.append(f"<th class=\"align-{align}\">{inline_markdown(cell)}</th>")
    html_rows.append("</tr></thead>")
    html_rows.append("<tbody>")
    for row in body:
        html_rows.append("<tr>")
        for idx, cell in enumerate(row):
            align = aligns[idx] if idx < len(aligns) else "left"
            html_rows.append(f"<td class=\"align-{align}\">{inline_markdown(cell)}</td>")
        html_rows.append("</tr>")
    html_rows.append("</tbody></table></div>")
    return "".join(html_rows)


def render_markdown(markdown_text: str) -> str:
    lines = markdown_text.splitlines()
    out: List[str] = []
    paragraph: List[str] = []
    idx = 0
    in_code = False
    code_lines: List[str] = []
    list_open = False

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_paragraph(paragraph, out)
            close_list()
            if in_code:
                out.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            idx += 1
            continue

        if in_code:
            code_lines.append(line)
            idx += 1
            continue

        if not stripped:
            flush_paragraph(paragraph, out)
            close_list()
            idx += 1
            continue

        if stripped.startswith("=="):
            flush_paragraph(paragraph, out)
            close_list()
            callout_parts = [stripped]
            while not callout_parts[-1].endswith("==") and idx + 1 < len(lines):
                idx += 1
                callout_parts.append(lines[idx].strip())
            callout = " ".join(callout_parts).strip()
            callout = callout.removeprefix("==").removesuffix("==").strip()
            out.append(f"<div class=\"callout\">{inline_markdown(callout)}</div>")
            idx += 1
            continue

        if stripped.startswith("|") and idx + 1 < len(lines) and is_table_separator(lines[idx + 1]):
            flush_paragraph(paragraph, out)
            close_list()
            table_lines = [line, lines[idx + 1]]
            idx += 2
            while idx < len(lines) and lines[idx].strip().startswith("|"):
                table_lines.append(lines[idx])
                idx += 1
            out.append(render_table(table_lines))
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            flush_paragraph(paragraph, out)
            close_list()
            level = min(len(heading.group(1)) + 1, 4)
            out.append(f"<h{level}>{inline_markdown(heading.group(2))}</h{level}>")
            idx += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph(paragraph, out)
            close_list()
            quote = stripped.lstrip(">").strip()
            out.append(f"<blockquote>{inline_markdown(quote)}</blockquote>")
            idx += 1
            continue

        item = re.match(r"^[-*]\s+(.+)$", stripped)
        if item:
            flush_paragraph(paragraph, out)
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{inline_markdown(item.group(1))}</li>")
            idx += 1
            continue

        close_list()
        paragraph.append(stripped)
        idx += 1

    if in_code:
        out.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
    flush_paragraph(paragraph, out)
    close_list()
    return "\n".join(out)


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden_depth = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in {"script", "style"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)

    def text(self) -> str:
        return normalize_text(" ".join(self.parts))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", html.unescape(text or ""))


def markdown_fragments(markdown_text: str) -> Iterable[str]:
    in_code = False
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if not line or is_table_separator(line):
            continue
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            for cell in cells:
                cleaned = clean_markdown_text(cell)
                if cleaned:
                    yield cleaned
            continue
        cleaned = clean_markdown_text(line)
        if cleaned:
            yield cleaned


def clean_markdown_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned)
    cleaned = re.sub(r"^>\s*", "", cleaned)
    cleaned = re.sub(r"^[-*]\s+", "", cleaned)
    cleaned = cleaned.strip("= ")
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = cleaned.replace("**", "").replace("*", "").replace("`", "")
    return normalize_text(cleaned)


def validate_text_preserved(markdown_text: str, html_text: str) -> None:
    parser = VisibleTextParser()
    parser.feed(html_text)
    visible = parser.text()
    missing: List[str] = []
    for fragment in markdown_fragments(markdown_text):
        if len(fragment) < 2:
            continue
        if fragment not in visible:
            missing.append(fragment)
        if len(missing) >= 10:
            break
    if missing:
        preview = "\n".join(f"- {item[:120]}" for item in missing)
        raise RuntimeError(f"HTML text preservation check failed; missing fragments:\n{preview}")


def load_market_data(path: Path) -> dict:
    if not path.exists():
        return {
            "metadata": {"missing": True, "source": str(path)},
            "columns": [],
            "records": [],
            "series": {},
            "quality": {"records_available": 0, "has_120_records": False},
        }
    return json.loads(path.read_text(encoding="utf-8"))


def render_html(markdown_text: str, market_data: dict, title: str, source_path: Path) -> str:
    report_body = render_markdown(markdown_text)
    market_json = json.dumps(market_data, ensure_ascii=False)
    safe_market_json = (
        market_json
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source_label = html.escape(str(source_path))
    escaped_title = html.escape(title)
    quality = market_data.get("quality") or {}
    records_available = quality.get("records_available", 0)
    hint = "" if quality.get("has_120_records") else f"当前可用 {records_available} 条，未满 120 条。"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    :root {{
      --bg: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --line: #e5e7eb;
      --soft: #f6f8fb;
      --info: #e8f3ff;
      --info-text: #1d4f91;
      --red: #e74c3c;
      --green: #2dbf70;
      --blue: #2f80ed;
      --orange: #f2994a;
      --purple: #9b59b6;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .page {{
      width: min(1480px, calc(100vw - 48px));
      margin: 34px auto 72px;
    }}
    .topbar {{
      border-top: 4px solid #ff6b3a;
      margin: -34px calc((48px - 100vw) / 2) 32px;
      padding-top: 24px;
    }}
    .title-row {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 18px;
    }}
    h1 {{
      font-size: 28px;
      line-height: 1.2;
      margin: 0;
      letter-spacing: 0;
    }}
    .meta {{
      color: var(--muted);
      font-size: 12px;
      text-align: right;
      white-space: nowrap;
    }}
    .notice {{
      color: var(--info-text);
      background: var(--info);
      border-radius: 6px;
      padding: 10px 14px;
      margin: 16px 0 28px;
      font-weight: 600;
    }}
    .section {{
      border-top: 1px solid var(--line);
      padding-top: 28px;
      margin-top: 30px;
    }}
    .section h2 {{
      font-size: 24px;
      margin: 0 0 18px;
    }}
    .chart-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 28px 32px;
    }}
    .chart-card {{
      min-width: 0;
    }}
    .chart-title {{
      font-size: 14px;
      font-weight: 700;
      margin: 0 0 8px;
    }}
    .chart-subtitle {{
      color: var(--muted);
      font-size: 12px;
      min-height: 18px;
      margin-bottom: 4px;
    }}
    .chart-card svg {{
      display: block;
      width: 100%;
      height: 240px;
      overflow: visible;
    }}
    .axis, .grid-line {{
      stroke: #e5e7eb;
      stroke-width: 1;
    }}
    .series-line {{
      fill: none;
      stroke-width: 2.2;
      stroke-linejoin: round;
      stroke-linecap: round;
    }}
    .legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
    }}
    .legend span::before {{
      content: "";
      display: inline-block;
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--legend-color);
      margin-right: 5px;
    }}
    .report {{
      max-width: 1180px;
    }}
    .report h2, .report h3, .report h4 {{
      letter-spacing: 0;
      margin: 28px 0 12px;
      line-height: 1.35;
    }}
    .report h2 {{ font-size: 24px; }}
    .report h3 {{ font-size: 20px; }}
    .report h4 {{ font-size: 17px; }}
    .report p {{ margin: 10px 0; }}
    .report blockquote {{
      margin: 12px 0;
      padding: 10px 14px;
      border-left: 4px solid #c7ddff;
      background: #f7fbff;
      color: #374151;
    }}
    .callout {{
      color: var(--info-text);
      background: var(--info);
      border-radius: 6px;
      padding: 11px 14px;
      margin: 12px 0;
      font-weight: 600;
    }}
    .table-wrap {{
      overflow-x: auto;
      margin: 12px 0 18px;
      border: 1px solid var(--line);
      border-radius: 6px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
      background: #fff;
    }}
    th, td {{
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      word-break: keep-all;
    }}
    th {{
      background: var(--soft);
      font-weight: 700;
      color: #374151;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .align-right {{ text-align: right; }}
    .align-center {{ text-align: center; }}
    code {{
      background: #f3f4f6;
      border-radius: 4px;
      padding: 1px 5px;
      color: #374151;
    }}
    pre {{
      overflow-x: auto;
      background: #111827;
      color: #f9fafb;
      border-radius: 6px;
      padding: 14px;
    }}
    @media (max-width: 900px) {{
      .page {{ width: min(100vw - 28px, 760px); margin-top: 24px; }}
      .title-row {{ display: block; }}
      .meta {{ text-align: left; margin-top: 8px; white-space: normal; }}
      .chart-grid {{ grid-template-columns: 1fr; gap: 26px; }}
      .chart-card svg {{ height: 210px; }}
    }}
  </style>
</head>
<body>
  <div class="topbar"></div>
  <main class="page">
    <header class="title-row">
      <h1>{escaped_title}</h1>
      <div class="meta">生成时间：{generated_at}<br>源文件：{source_label}</div>
    </header>
    <section class="section" id="market-trends">
      <h2>Market Data 趋势</h2>
      <div class="notice" id="market-quality">{html.escape(hint) if hint else "已加载 Market Data 派生数据。"}</div>
      <div class="chart-grid" id="chart-grid"></div>
    </section>
    <section class="section report" id="report-body">
      {report_body}
    </section>
  </main>
  <script id="market-data" type="application/json">{safe_market_json}</script>
  <script>
  (function () {{
    const data = JSON.parse(document.getElementById("market-data").textContent || "{{}}");
    const records = Array.isArray(data.records) ? data.records.filter(r => r && r.trade_date).slice(-120) : [];
    const grid = document.getElementById("chart-grid");
    const quality = document.getElementById("market-quality");
    if (!records.length) {{
      quality.textContent = "暂无可用 Market Data JSON，正文仍可正常阅读。";
      return;
    }}
    const loadedText = data.quality && data.quality.has_120_records
      ? `已加载最近 ${{records.length}} 条 Market Data。`
      : `当前可用 ${{records.length}} 条，未满 120 条。`;
    quality.textContent = loadedText;

    const charts = [
      {{ title: "成交额趋势", fields: [{{ key: "成交额", color: "var(--blue)", scale: 1e9, unit: "万亿" }}] }},
      {{ title: "活跃度 / 情绪值", fields: [{{ key: "活跃度", color: "var(--orange)" }}, {{ key: "情绪值", color: "var(--purple)" }}] }},
      {{ title: "融资净买入", fields: [{{ key: "融资净买入", color: "var(--blue)", scale: 1e8, unit: "亿" }}] }},
      {{ title: "上涨 vs 下跌家数", fields: [{{ key: "上涨", color: "var(--red)" }}, {{ key: "下跌", color: "var(--green)" }}] }},
      {{ title: "涨停 vs 跌停家数", fields: [{{ key: "涨停", color: "var(--red)" }}, {{ key: "跌停", color: "var(--green)" }}] }},
      {{ title: "全市场换手率", fields: [{{ key: "全市场换手率", color: "var(--blue)", unit: "%" }}] }},
    ];

    charts.forEach(config => {{
      const card = document.createElement("article");
      card.className = "chart-card";
      const title = document.createElement("div");
      title.className = "chart-title";
      title.textContent = config.title;
      const subtitle = document.createElement("div");
      subtitle.className = "chart-subtitle";
      card.append(title, subtitle);
      const drawable = drawChart(records, config, subtitle);
      card.appendChild(drawable);
      const legend = document.createElement("div");
      legend.className = "legend";
      config.fields.forEach(field => {{
        const span = document.createElement("span");
        span.style.setProperty("--legend-color", field.color);
        span.textContent = field.key;
        legend.appendChild(span);
      }});
      card.appendChild(legend);
      grid.appendChild(card);
    }});

    function drawChart(rows, config, subtitle) {{
      const width = 480;
      const height = 240;
      const pad = {{ left: 42, right: 16, top: 16, bottom: 28 }};
      const usableW = width - pad.left - pad.right;
      const usableH = height - pad.top - pad.bottom;
      const svg = svgEl("svg", {{ viewBox: `0 0 ${{width}} ${{height}}`, role: "img" }});
      const series = config.fields.map(field => {{
        const points = rows.map((row, idx) => {{
          const raw = row[field.key];
          const value = typeof raw === "number" ? raw / (field.scale || 1) : null;
          return {{ idx, date: row.trade_date, value }};
        }}).filter(p => Number.isFinite(p.value));
        return {{ field, points }};
      }}).filter(item => item.points.length);
      if (!series.length) {{
        svg.appendChild(svgText(width / 2, height / 2, "暂无数据", "middle", "var(--muted)"));
        subtitle.textContent = "该列暂无可绘制数值";
        return svg;
      }}
      const allValues = series.flatMap(item => item.points.map(p => p.value));
      let min = Math.min(...allValues);
      let max = Math.max(...allValues);
      if (min === max) {{
        min -= 1;
        max += 1;
      }}
      const span = max - min;
      min -= span * 0.08;
      max += span * 0.08;
      const x = idx => pad.left + (rows.length <= 1 ? usableW / 2 : idx / (rows.length - 1) * usableW);
      const y = value => pad.top + (max - value) / (max - min) * usableH;
      for (let i = 0; i <= 4; i += 1) {{
        const gy = pad.top + usableH * i / 4;
        svg.appendChild(svgEl("line", {{ x1: pad.left, x2: width - pad.right, y1: gy, y2: gy, class: "grid-line" }}));
      }}
      svg.appendChild(svgEl("line", {{ x1: pad.left, x2: width - pad.right, y1: height - pad.bottom, y2: height - pad.bottom, class: "axis" }}));
      series.forEach(item => {{
        const d = item.points.map((p, i) => `${{i === 0 ? "M" : "L"}} ${{x(p.idx).toFixed(2)}} ${{y(p.value).toFixed(2)}}`).join(" ");
        svg.appendChild(svgEl("path", {{ d, class: "series-line", style: `stroke: ${{item.field.color}}` }}));
      }});
      svg.appendChild(svgText(pad.left, height - 6, formatDate(rows[0].trade_date), "start", "var(--muted)"));
      svg.appendChild(svgText(width - pad.right, height - 6, formatDate(rows[rows.length - 1].trade_date), "end", "var(--muted)"));
      svg.appendChild(svgText(4, pad.top + 4, formatValue(max, config.fields[0].unit), "start", "var(--muted)"));
      svg.appendChild(svgText(4, height - pad.bottom, formatValue(min, config.fields[0].unit), "start", "var(--muted)"));
      const last = series[0].points[series[0].points.length - 1];
      subtitle.textContent = last ? `最新：${{formatValue(last.value, series[0].field.unit)}}` : "";
      return svg;
    }}

    function svgEl(name, attrs) {{
      const el = document.createElementNS("http://www.w3.org/2000/svg", name);
      Object.entries(attrs || {{}}).forEach(([key, value]) => el.setAttribute(key, value));
      return el;
    }}
    function svgText(x, y, text, anchor, color) {{
      const el = svgEl("text", {{ x, y, "text-anchor": anchor, fill: color, "font-size": "11" }});
      el.textContent = text;
      return el;
    }}
    function formatDate(value) {{
      return String(value || "").replace(/^(\\d{{4}})(\\d{{2}})(\\d{{2}})$/, "$1-$2-$3");
    }}
    function formatValue(value, unit) {{
      if (!Number.isFinite(value)) return "—";
      const abs = Math.abs(value);
      const digits = abs >= 100 ? 0 : abs >= 10 ? 1 : 2;
      return `${{value.toFixed(digits)}}${{unit || ""}}`;
    }}
  }})();
  </script>
</body>
</html>
"""


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_suffix(".html")
    market_data_path = Path(args.market_data)
    markdown_text = input_path.read_text(encoding="utf-8")
    title = args.title or input_path.stem
    market_data = load_market_data(market_data_path)
    html_text = render_html(markdown_text, market_data, title, input_path)
    if not args.no_validate:
        validate_text_preserved(markdown_text, html_text)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_text, encoding="utf-8")
    print(json.dumps({
        "input": str(input_path),
        "output": str(output_path),
        "market_data": str(market_data_path),
        "records_available": (market_data.get("quality") or {}).get("records_available", 0),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
