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
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {{
      /* neutral palette — cool, paper-like */
      --bg: #f6f8fb;
      --surface: #ffffff;
      --card: #ffffff;
      --ink-1: #1f2329;
      --ink-2: #4b5563;
      --ink-3: #7a8290;
      --ink-4: #a8b0bb;
      --line-1: #e6e9ef;
      --line-2: #eef1f5;
      --tint: #f3f6fb;
      /* single accent — a restrained tech blue */
      --accent: #1a73e8;
      --accent-ink: #0b57d0;
      --accent-soft: #e8f0fe;
      --accent-hair: #c2d7f7;
      /* data signals */
      --pos: #137333;
      --pos-soft: #e6f4ea;
      --neg: #c5221f;
      --neg-soft: #fce8e6;
      --warn: #b06000;
      --warn-soft: #feefc3;
      --violet: #6f4ee0;
      --orange: #e8710a;

      /* aliases consumed by chart script */
      --blue: var(--accent);
      --green: var(--pos);
      --red: var(--neg);
      --purple: var(--violet);
      --yellow: var(--warn);
      --cyan: #0aa5b8;

      --r-xs: 4px;
      --r-sm: 6px;
      --r-md: 8px;
      --r-lg: 12px;

      --shadow-flat: 0 0 0 1px var(--line-1);
      --shadow-card: 0 1px 2px rgba(15, 23, 32, 0.04), 0 0 0 1px var(--line-1);
      --shadow-hover: 0 1px 2px rgba(15, 23, 32, 0.04), 0 0 0 1px var(--accent-hair);
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}

    html {{ background: var(--bg); }}

    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink-1);
      font-family: "Inter", "Helvetica Neue", -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
      font-size: 14.5px;
      line-height: 1.75;
      font-feature-settings: "cv02", "cv03", "cv04", "cv11", "tnum" 0;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }}

    /* numeric cells use tabular figures for clean column alignment */
    td, th, .meta-bar code, code, pre {{ font-feature-settings: "tnum"; }}

    .page {{
      width: min(1180px, calc(100vw - 48px));
      margin: 0 auto 96px;
      padding-top: 28px;
    }}

    .report {{ max-width: 1180px; margin: 0 auto; }}

    .report h2 {{
      font-size: 22px;
      font-weight: 600;
      margin: 56px 0 18px;
      line-height: 1.3;
      color: var(--ink-1);
      letter-spacing: -0.01em;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--line-1);
      position: relative;
    }}
    .report h2::before {{
      content: "";
      position: absolute;
      left: 0; bottom: -1px;
      width: 32px; height: 2px;
      background: var(--accent);
      border-radius: 2px;
      transition: width 0.2s ease;
    }}
    /* rotate accent hues across major sections */
    .report h2[data-idx="0"]::before {{ background: var(--accent); }}
    .report h2[data-idx="1"]::before {{ background: var(--violet); }}
    .report h2[data-idx="2"]::before {{ background: var(--orange); }}
    .report h2[data-idx="3"]::before {{ background: var(--neg); }}
    .report h2[data-idx="4"]::before {{ background: var(--pos); }}
    .report h2[data-idx="5"]::before {{ background: var(--cyan); }}
    .report h2 .sec-num {{
      display: inline-block;
      font-family: "JetBrains Mono", ui-monospace, monospace;
      color: var(--ink-4);
      font-weight: 500;
      font-size: 13px;
      margin-right: 12px;
      letter-spacing: 0.02em;
      vertical-align: 2px;
    }}
    .report > h2:first-child {{ margin-top: 0; }}

    .report h3 {{
      font-size: 15.5px;
      font-weight: 600;
      margin: 32px 0 12px;
      line-height: 1.4;
      color: var(--ink-1);
      letter-spacing: -0.005em;
    }}
    .report h4 {{
      font-size: 14px;
      font-weight: 600;
      margin: 20px 0 10px;
      line-height: 1.4;
      color: var(--ink-2);
    }}
    .report p {{
      margin: 12px 0;
      color: var(--ink-2);
      font-size: 14px;
      line-height: 1.85;
      text-wrap: pretty;
    }}

    .report blockquote {{
      margin: 18px 0;
      padding: 12px 18px;
      border-left: 2px solid var(--accent);
      background: transparent;
      color: var(--ink-2);
      font-size: 13.5px;
      font-weight: 500;
    }}

    /* TRUE highlight — warm amber tint, like a marker pen */
    .callout {{
      position: relative;
      color: #3d2c00;
      background: linear-gradient(180deg, #fff8d8 0%, #fff4c2 100%);
      border: 1px solid #f7d774;
      border-left: 4px solid #f9ab00;
      border-radius: var(--r-md);
      padding: 14px 18px 14px 44px;
      margin: 20px 0;
      font-weight: 500;
      font-size: 13.5px;
      line-height: 1.8;
      box-shadow: 0 1px 0 rgba(249, 171, 0, 0.08);
    }}
    .callout::before {{
      content: "";
      position: absolute;
      left: 16px; top: 17px;
      width: 16px; height: 16px;
      border-radius: 50%;
      background: #f9ab00;
      box-shadow: 0 0 0 4px rgba(249, 171, 0, 0.18);
    }}
    .callout::after {{
      content: "";
      position: absolute;
      left: 22.5px; top: 22px;
      width: 3px; height: 3px;
      border-radius: 50%;
      background: #fff;
      box-shadow: 0 4px 0 -0.5px #fff;
    }}
    /* secondary insight variant — softer blue tint */
    .callout.insight {{
      color: var(--ink-1);
      background: linear-gradient(180deg, #eef4fe 0%, #e3edfd 100%);
      border: 1px solid var(--accent-hair);
      border-left: 4px solid var(--accent);
    }}
    .callout.insight::before {{ background: var(--accent); box-shadow: 0 0 0 4px rgba(26, 115, 232, 0.16); }}

    .table-wrap {{
      overflow-x: auto;
      margin: 14px 0 28px;
      border-radius: var(--r-md);
      background: var(--surface);
      border: 1px solid var(--line-1);
      box-shadow: none;
    }}
    table {{
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      min-width: 720px;
      background: var(--surface);
      font-size: 12.5px;
    }}
    th, td {{
      padding: 7px 12px;
      border-bottom: 1px solid var(--line-2);
      vertical-align: middle;
      word-break: keep-all;
      color: var(--ink-1);
      line-height: 1.55;
    }}
    th {{
      padding: 9px 12px;
      background: var(--tint);
      font-weight: 500;
      color: var(--ink-3);
      font-size: 10.5px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      position: sticky;
      top: 0;
      z-index: 1;
      text-align: left;
      border-bottom: 1px solid var(--line-1);
      white-space: nowrap;
    }}
    th.align-right, td.align-right {{ text-align: right; }}
    th.align-center, td.align-center {{ text-align: center; }}
    td.align-right, td[class*="align-right"] {{
      font-variant-numeric: tabular-nums;
      font-feature-settings: "tnum";
    }}
    /* numeric columns get mono treatment */
    td.align-right {{
      font-family: "JetBrains Mono", ui-monospace, monospace;
      font-size: 12px;
      color: var(--ink-1);
      letter-spacing: -0.01em;
    }}
    /* auto-colored data — CN stock convention: 涨/正 = 红, 跌/负 = 绿 */
    td .num-pos {{ color: var(--neg); font-weight: 500; }}
    td .num-neg {{ color: var(--pos); font-weight: 500; }}
    td .stars {{
      color: #f9ab00;
      letter-spacing: 2px;
      font-size: 13px;
      text-shadow: 0 0 1px rgba(249, 171, 0, 0.4);
    }}
    td .stars.dim {{ color: #d8dde5; text-shadow: none; }}
    /* small pill for trend / classification labels in tables */
    td .pill {{
      display: inline-block;
      padding: 1px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 500;
      line-height: 1.7;
      letter-spacing: 0.01em;
      background: var(--accent-soft);
      color: var(--accent-ink);
      border: 1px solid var(--accent-hair);
    }}
    td .pill.pos {{ background: var(--pos-soft); color: var(--pos); border-color: #b7dfc4; }}
    td .pill.neg {{ background: var(--neg-soft); color: var(--neg); border-color: #f5c2c0; }}
    td .pill.warn {{ background: var(--warn-soft); color: var(--warn); border-color: #f7d774; }}
    td .pill.violet {{ background: #ece4fb; color: var(--violet); border-color: #d4c2f5; }}
    tbody td {{ color: var(--ink-2); }}
    tbody td:first-child {{ color: var(--ink-1); font-weight: 500; }}
    /* subtle zebra for legibility in dense tables */
    tbody tr:nth-child(even) td {{ background: #fafbfd; }}
    tr:last-child td {{ border-bottom: none; }}
    tbody tr:hover td {{ background: #eef4fe; }}

    code {{
      background: var(--tint);
      border-radius: var(--r-xs);
      padding: 1px 6px;
      color: var(--accent-ink);
      font-family: "JetBrains Mono", ui-monospace, "SF Mono", Consolas, monospace;
      font-size: 12.5px;
      font-weight: 500;
      border: 1px solid var(--line-1);
    }}
    pre {{
      overflow-x: auto;
      background: #1f2329;
      color: #e6e9ef;
      border-radius: var(--r-md);
      padding: 18px 20px;
      margin: 16px 0;
      font-family: "JetBrains Mono", ui-monospace, monospace;
      font-size: 12.5px;
      line-height: 1.65;
      border: 1px solid #2a2f37;
    }}

    /* chart grid — clean tech cards */
    .chart-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
      margin-top: 20px;
      margin-bottom: 8px;
    }}
    .chart-card {{
      background: var(--surface);
      border-radius: var(--r-md);
      padding: 18px 18px 14px;
      border: 1px solid var(--line-1);
      box-shadow: none;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }}
    .chart-card:hover {{
      border-color: var(--accent-hair);
      box-shadow: 0 1px 2px rgba(15, 23, 32, 0.04);
      transform: none;
    }}
    .chart-title {{
      font-size: 12px;
      font-weight: 600;
      margin: 0 0 4px;
      color: var(--ink-3);
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }}
    .chart-subtitle {{
      color: var(--ink-4);
      font-size: 11.5px;
      min-height: 16px;
      margin-bottom: 10px;
      font-family: "JetBrains Mono", ui-monospace, monospace;
    }}
    .chart-card svg {{
      display: block;
      width: 100%;
      height: 190px;
      overflow: visible;
    }}
    .axis, .grid-line {{
      stroke: var(--line-2);
      stroke-width: 1;
    }}
    .series-line {{
      fill: none;
      stroke-width: 1.8;
      stroke-linejoin: round;
      stroke-linecap: round;
    }}
    .legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      color: var(--ink-3);
      font-size: 11.5px;
      margin-top: 12px;
      padding-top: 10px;
      border-top: 1px solid var(--line-2);
    }}
    .legend span {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-family: "JetBrains Mono", ui-monospace, monospace;
    }}
    .legend span::before {{
      content: "";
      display: inline-block;
      width: 8px;
      height: 2px;
      border-radius: 1px;
      background: var(--legend-color);
      box-shadow: none;
    }}

    /* ===== Summary hero card ("一句话盘面判断") ===== */
    .summary-card {{
      position: relative;
      background:
        radial-gradient(120% 140% at 100% 0%, rgba(111, 78, 224, 0.10) 0%, transparent 55%),
        radial-gradient(110% 130% at 0% 100%, rgba(26, 115, 232, 0.10) 0%, transparent 55%),
        linear-gradient(180deg, #f4f7fc 0%, #eaf0f8 100%);
      color: var(--ink-1);
      border: 1px solid #d8e2f0;
      border-radius: var(--r-lg);
      padding: 24px 28px 26px;
      margin: 4px 0 36px;
      overflow: hidden;
      box-shadow: 0 1px 2px rgba(15, 23, 32, 0.04), 0 8px 24px rgba(26, 115, 232, 0.06);
    }}
    .summary-card::before {{
      content: "";
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 3px;
      background: linear-gradient(90deg, var(--accent) 0%, var(--violet) 38%, var(--orange) 72%, var(--neg) 100%);
    }}
    .summary-card::after {{
      content: "";
      position: absolute;
      right: -40px; top: -40px;
      width: 180px; height: 180px;
      background: radial-gradient(circle at center, rgba(232, 113, 10, 0.10) 0%, transparent 60%);
      pointer-events: none;
    }}
    .summary-card .summary-label {{
      font-size: 11px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--accent-ink);
      font-weight: 600;
      margin: 0 0 14px;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      font-family: "JetBrains Mono", "Inter", ui-monospace, monospace;
    }}
    .summary-card .summary-label::before {{
      content: "";
      width: 6px; height: 6px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 0 4px rgba(26, 115, 232, 0.18);
    }}
    .summary-card .summary-body {{
      font-size: 15.5px;
      line-height: 1.9;
      color: var(--ink-1);
      font-weight: 400;
      margin: 0;
      letter-spacing: 0.005em;
      position: relative;
      z-index: 1;
      text-wrap: pretty;
    }}
    /* CN convention inside summary card: 正数红 / 负数绿 */
    .summary-card .summary-body .num-pos {{ color: var(--neg); font-weight: 600; }}
    .summary-card .summary-body .num-neg {{ color: var(--pos); font-weight: 600; }}
    .summary-card .summary-body .kw {{
      color: var(--ink-1);
      font-weight: 600;
      background: linear-gradient(180deg, transparent 62%, rgba(249, 171, 0, 0.28) 62%, rgba(249, 171, 0, 0.28) 92%, transparent 92%);
      padding: 0 1px;
    }}

    /* responsive */
    @media (max-width: 1024px) {{
      .chart-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 900px) {{
      .page {{ width: calc(100vw - 24px); padding-top: 20px; margin-bottom: 64px; }}
      .chart-grid {{ grid-template-columns: 1fr; gap: 14px; }}
      .chart-card svg {{ height: 170px; }}
      .report h2 {{ font-size: 19px; margin-top: 40px; }}
      .report h3 {{ font-size: 14.5px; }}
      table {{ min-width: 600px; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="section report" id="report-body">
      {report_body}
    </section>
    <div id="chart-quality" style="display:none">{html.escape(hint) if hint else "已加载 Market Data 派生数据。"}</div>
  </main>
  <script id="market-data" type="application/json">{safe_market_json}</script>
  <script>
  (function () {{
    /* ===== UI post-processing — decorates existing markup; never edits text content ===== */
    const root = document.getElementById("report-body");
    if (!root) return;

    /* 1. Hide standalone MD separator paragraphs ("---") */
    root.querySelectorAll("p").forEach(p => {{
      const txt = p.textContent.trim();
      if (txt === "---" || /^-{{3,}}$/.test(txt)) {{
        p.style.display = "none";
        p.setAttribute("aria-hidden", "true");
      }}
    }});

    /* 1b. Promote the leading "一句话盘面判断" block into a hero summary card */
    const summaryH3 = Array.from(root.querySelectorAll("h3")).find(h =>
      h.textContent.trim().startsWith("一句话盘面判断")
    );
    if (summaryH3) {{
      const card = document.createElement("aside");
      card.className = "summary-card";
      const label = document.createElement("div");
      label.className = "summary-label";
      label.textContent = summaryH3.textContent.trim();
      card.appendChild(label);

      const collected = [];
      let cur = summaryH3.nextElementSibling;
      while (cur && !/^H[1-6]$/.test(cur.tagName)) {{
        const next = cur.nextElementSibling;
        const txt = cur.textContent.trim();
        if (cur.tagName === "P" && txt && !/^-{{3,}}$/.test(txt)) {{
          cur.classList.add("summary-body");
          collected.push(cur);
        }} else {{
          cur.remove();
        }}
        cur = next;
      }}
      collected.forEach(node => card.appendChild(node));
      summaryH3.replaceWith(card);

      /* highlight signed numbers and key tickers inside the summary body */
      collected.forEach(p => {{
        let html = p.innerHTML.replace(
          /([+\-])(\d+(?:\.\d+)?)(%|pct|倍)/g,
          (_, sign, num, unit) => {{
            const cls = sign === "+" ? "num-pos" : "num-neg";
            return `<span class="${{cls}}">${{sign}}${{num}}${{unit}}</span>`;
          }}
        );
        html = html.replace(/(上证|创业板|半导体设备与材料|电力能源)/g, '<span class="kw">$1</span>');
        p.innerHTML = html;
      }});
    }}

    /* 2. Tag major h2 sections with a rotating data-idx for color variety */
    let h2Idx = 0;
    root.querySelectorAll("h2").forEach(h => {{
      h.setAttribute("data-idx", String(h2Idx % 6));
      h2Idx += 1;
    }});

    /* 3. Colorize numeric cells, star ratings, and tag chips inside tables */
    const colorize = (td) => {{
      const trimmed = td.textContent.trim();
      if (!trimmed || td.children.length > 0) return;

      // Star ratings: pad to 3 with dim stars
      if (/^[★☆]+$/.test(trimmed)) {{
        const filled = (trimmed.match(/★/g) || []).length;
        const total = Math.max(filled, 3);
        let html = "";
        for (let i = 0; i < total; i++) {{
          html += i < filled
            ? '<span class="stars">★</span>'
            : '<span class="stars dim">★</span>';
        }}
        td.innerHTML = html;
        return;
      }}

      // Signed numbers (CN convention: + = red, - = green)
      const signedMatch = trimmed.match(/^([+\-])(\d[\d,]*\.?\d*)\s*(%|pct|x|倍|亿|万亿)?$/);
      if (signedMatch) {{
        const sign = signedMatch[1];
        const cls = sign === "+" ? "num-pos" : "num-neg";
        td.innerHTML = `<span class="${{cls}}">${{trimmed}}</span>`;
        return;
      }}

      // Tagged labels — wrap select keywords in pills
      const pillRules = [
        {{ re: /^高位强势股退潮$/, cls: "pill neg" }},
        {{ re: /^流动性杀跌$/, cls: "pill warn" }},
        {{ re: /^主线内部分歧$/, cls: "pill warn" }},
        {{ re: /^高位趋势$/, cls: "pill" }},
        {{ re: /^高$/, cls: "pill neg" }},
        {{ re: /^中$/, cls: "pill warn" }},
        {{ re: /^低$/, cls: "pill pos" }},
        {{ re: /^领导股$/, cls: "pill" }},
        {{ re: /^弹性股$/, cls: "pill violet" }},
        {{ re: /^启动型$|^持续换手型$|^分歧型$/, cls: "pill" }},
        {{ re: /^[ABC]$/, cls: "pill" }}
      ];
      for (const rule of pillRules) {{
        if (rule.re.test(trimmed)) {{
          td.innerHTML = `<span class="${{rule.cls}}">${{trimmed}}</span>`;
          return;
        }}
      }}

      // Mixed-text cell — color any inline signed numbers found within
      if (/[+\-]\d/.test(trimmed)) {{
        td.innerHTML = td.innerHTML.replace(
          /([+\-])(\d+(?:[\.,]\d+)?)(%|pct|倍|x)?/g,
          (_, sign, num, unit) => {{
            const cls = sign === "+" ? "num-pos" : "num-neg";
            return `<span class="${{cls}}">${{sign}}${{num}}${{unit || ""}}</span>`;
          }}
        );
      }}
    }};

    root.querySelectorAll("td").forEach(colorize);
  }})();
  </script>
  <script>
  (function () {{
    const data = JSON.parse(document.getElementById("market-data").textContent || "{{}}");
    const records = Array.isArray(data.records) ? data.records.filter(r => r && r.trade_date).slice(-90) : [];
    if (!records.length) return;

    const reportBody = document.getElementById("report-body");
    const headings = reportBody.querySelectorAll("h3");
    let targetHeading = null;
    for (const h of headings) {{
      if (h.textContent.includes("1.1") && h.textContent.includes("情绪趋势")) {{
        targetHeading = h;
        break;
      }}
    }}

    const chartSection = document.createElement("div");
    chartSection.className = "chart-grid";
    chartSection.style.marginTop = "28px";

    const charts = [
      {{ title: "成交额趋势", fields: [{{ key: "成交额", color: "var(--blue)", scale: 1e9, unit: "万亿" }}] }},
      {{ title: "活跃度 / 情绪值", fields: [{{ key: "活跃度", color: "var(--orange)" }}, {{ key: "情绪值", color: "var(--purple)" }}] }},
      {{ title: "融资净买入", type: "bar", fields: [{{ key: "融资净买入", color: "var(--green)", scale: 1e8, unit: "亿" }}] }},
      {{ title: "上涨 vs 下跌家数", fields: [{{ key: "上涨", color: "var(--red)" }}, {{ key: "下跌", color: "var(--green)" }}] }},
      {{ title: "涨停 vs 跌停家数", fields: [{{ key: "涨停", color: "var(--red)" }}, {{ key: "跌停", color: "var(--green)" }}] }},
    ];

    charts.forEach(config => {{
      const card = document.createElement("article");
      card.className = "chart-card";
      const title = document.createElement("div");
      title.className = "chart-title";
      title.textContent = config.title;
      const subtitle = document.createElement("div");
      subtitle.className = "chart-subtitle";
      card.appendChild(title);
      const drawable = drawChart(records, config, card);
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
      chartSection.appendChild(card);
    }});

    if (targetHeading) {{
      let insertAfter = targetHeading;
      let sibling = targetHeading.nextElementSibling;
      while (sibling) {{
        if (sibling.tagName === "H3" || sibling.tagName === "H2") break;
        insertAfter = sibling;
        sibling = sibling.nextElementSibling;
      }}
      insertAfter.after(chartSection);
    }} else {{
      reportBody.appendChild(chartSection);
    }}

    function drawChart(rows, config, card) {{
      const width = 480;
      const height = 200;
      const pad = {{ left: 42, right: 16, top: 14, bottom: 28 }};
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
        svg.appendChild(svgText(width / 2, height / 2, "暂无数据", "middle", "var(--text-tertiary)"));
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
      // Create tooltip element for this chart card
      const tooltip = document.createElement("div");
      tooltip.style.cssText = "position:absolute;background:rgba(15,23,42,0.92);color:#f1f5f9;padding:8px 12px;border-radius:8px;font-size:12px;line-height:1.5;pointer-events:none;opacity:0;transition:opacity 0.15s ease;z-index:100;white-space:nowrap;box-shadow:0 4px 12px rgba(0,0,0,0.2);";
      card.style.position = "relative";
      card.appendChild(tooltip);

      const isBar = config.type === "bar";
      const barWidth = isBar ? Math.max(2, usableW / rows.length * 0.55) : 0;
      const y0 = y(0);

      series.forEach(item => {{
        if (isBar) {{
          item.points.forEach(p => {{
            const px = x(p.idx);
            const py = y(p.value);
            const barH = Math.abs(py - y0);
            const barY = p.value >= 0 ? py : y0;
            const barColor = p.value >= 0 ? "var(--red)" : "var(--green)";
            const rect = svgEl("rect", {{
              x: (px - barWidth / 2).toFixed(2),
              y: barY.toFixed(2),
              width: barWidth.toFixed(2),
              height: Math.max(1, barH).toFixed(2),
              fill: barColor,
              rx: 2,
              opacity: 0.85
            }});
            svg.appendChild(rect);
            const hit = svgEl("rect", {{
              x: (px - barWidth / 2).toFixed(2),
              y: Math.min(py, y0).toFixed(2),
              width: barWidth.toFixed(2),
              height: Math.max(1, barH).toFixed(2),
              fill: "transparent",
              stroke: "none",
              style: "cursor:pointer"
            }});
            hit.addEventListener("mouseenter", () => {{
              rect.setAttribute("opacity", "1");
              const dateStr = formatDate(p.date);
              const valStr = formatValue(p.value, item.field.unit);
              tooltip.innerHTML = `<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${{dateStr}}</div><div style="font-weight:600;">${{item.field.key}}: ${{valStr}}</div>`;
              tooltip.style.opacity = "1";
            }});
            hit.addEventListener("mousemove", (e) => {{
              const r = card.getBoundingClientRect();
              tooltip.style.left = `${{Math.min(e.clientX - r.left + 12, r.width - tooltip.offsetWidth - 8)}}px`;
              tooltip.style.top = `${{Math.max(8, e.clientY - r.top - tooltip.offsetHeight - 12)}}px`;
            }});
            hit.addEventListener("mouseleave", () => {{
              rect.setAttribute("opacity", "0.85");
              tooltip.style.opacity = "0";
            }});
            svg.appendChild(hit);
          }});
        }} else {{
          const d = item.points.map((p, i) => `${{i === 0 ? "M" : "L"}} ${{x(p.idx).toFixed(2)}} ${{y(p.value).toFixed(2)}}`).join(" ");
          svg.appendChild(svgEl("path", {{ d, class: "series-line", style: `stroke: ${{item.field.color}}` }}));
          item.points.forEach(p => {{
            svg.appendChild(svgEl("circle", {{
              cx: x(p.idx).toFixed(2),
              cy: y(p.value).toFixed(2),
              r: 3.5,
              fill: item.field.color,
              stroke: "#ffffff",
              "stroke-width": 1.5
            }}));
            const hit = svgEl("circle", {{
              cx: x(p.idx).toFixed(2),
              cy: y(p.value).toFixed(2),
              r: 10,
              fill: "transparent",
              stroke: "none",
              style: "cursor:pointer"
            }});
            hit.addEventListener("mouseenter", () => {{
              const dateStr = formatDate(p.date);
              const valStr = formatValue(p.value, item.field.unit);
              tooltip.innerHTML = `<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${{dateStr}}</div><div style="font-weight:600;">${{item.field.key}}: ${{valStr}}</div>`;
              tooltip.style.opacity = "1";
            }});
            hit.addEventListener("mousemove", (e) => {{
              const r = card.getBoundingClientRect();
              tooltip.style.left = `${{Math.min(e.clientX - r.left + 12, r.width - tooltip.offsetWidth - 8)}}px`;
              tooltip.style.top = `${{Math.max(8, e.clientY - r.top - tooltip.offsetHeight - 12)}}px`;
            }});
            hit.addEventListener("mouseleave", () => {{
              tooltip.style.opacity = "0";
            }});
            svg.appendChild(hit);
          }});
        }}
      }});
      svg.appendChild(svgText(4, pad.top + 4, formatValue(max, config.fields[0].unit), "start", "var(--text-tertiary)"));
      svg.appendChild(svgText(4, height - pad.bottom, formatValue(min, config.fields[0].unit), "start", "var(--text-tertiary)"));
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
