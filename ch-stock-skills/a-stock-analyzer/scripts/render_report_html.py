#!/usr/bin/env python3
"""Render an a-stock-analyzer Markdown report as a self-contained HTML page.

The Markdown→HTML engine, Claude-UI styling and table colorize logic mirror
``a-stock-daily-market-sense/scripts/render_report_html.py``. The embedded
charts are stock-specific: date-axis PE/PB/PS valuation bands (the valuation line
over time against its historical percentile band) and a revenue/profit/ROE
financial trend panel, all derived from ``evidence_*.json``.

The static HTML body contains the full report text; charts and the hero card
are injected client-side at view time, so text-preservation validation runs
against the pre-JS markup.
"""

from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCRIPT_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_ROOT.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a Markdown stock report to static HTML.")
    parser.add_argument("--input", "-i", required=True, help="Markdown report path, e.g. reports/report_600519.md.")
    parser.add_argument("--output", "-o", default=None, help="HTML output path. Defaults to input path with .html suffix.")
    parser.add_argument("--evidence", default=None, help="Evidence JSON path. Defaults to sibling evidence_<code>.json when input is report_<code>.md.")
    parser.add_argument("--title", default=None, help="HTML document title.")
    parser.add_argument("--no-validate", action="store_true", help="Skip Markdown text preservation validation entirely.")
    parser.add_argument("--strict", action="store_true", help="Abort on text-preservation mismatch instead of warning. Off by default so content changes never break HTML generation.")
    return parser


# --------------------------------------------------------------------------- #
# Markdown → HTML (shared engine)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Text-preservation validation
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Evidence loading + chart payload extraction
# --------------------------------------------------------------------------- #
def safe_json_for_script(payload: Any) -> str:
    return (
        json.dumps(payload, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


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
    # Dedup income by end_date (Tushare may return restated/duplicate rows); last occurrence wins.
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
# HTML rendering
# --------------------------------------------------------------------------- #
def render_html(markdown_text: str, charts: dict, title: str, source_path: Path) -> str:
    report_body = render_markdown(markdown_text)
    safe_charts_json = safe_json_for_script(charts)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    escaped_title = html.escape(title)
    meta = charts.get("metadata") or {}
    sub_bits = [bit for bit in [meta.get("name"), meta.get("ts_code")] if bit]
    header_sub = html.escape(" · ".join(sub_bits)) if sub_bits else ""
    latest = html.escape(str(meta.get("latest_trade_date") or ""))

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
      --accent: #1a73e8;
      --accent-ink: #0b57d0;
      --accent-soft: #e8f0fe;
      --accent-hair: #c2d7f7;
      --pos: #137333;
      --pos-soft: #e6f4ea;
      --neg: #c5221f;
      --neg-soft: #fce8e6;
      --warn: #b06000;
      --warn-soft: #feefc3;
      --violet: #6f4ee0;
      --orange: #e8710a;
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
    td, th, code, pre {{ font-feature-settings: "tnum"; }}

    .page {{
      width: min(1180px, calc(100vw - 48px));
      margin: 0 auto 96px;
      padding-top: 28px;
    }}
    .report {{ max-width: 1180px; margin: 0 auto; }}

    /* doc header */
    .doc-head {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
      padding-bottom: 18px;
      margin-bottom: 8px;
      border-bottom: 1px solid var(--line-1);
    }}
    .doc-head .dh-title {{ font-size: 13px; font-weight: 600; color: var(--ink-2); letter-spacing: 0.04em; }}
    .doc-head .dh-meta {{
      font-family: "JetBrains Mono", ui-monospace, monospace;
      font-size: 11.5px; color: var(--ink-4); letter-spacing: 0.02em;
    }}

    .report h1 {{ font-size: 26px; font-weight: 700; margin: 12px 0 6px; letter-spacing: -0.02em; }}
    .report h2 {{
      font-size: 22px; font-weight: 600; margin: 56px 0 18px; line-height: 1.3;
      color: var(--ink-1); letter-spacing: -0.01em; padding-bottom: 12px;
      border-bottom: 1px solid var(--line-1); position: relative;
    }}
    .report h2::before {{
      content: ""; position: absolute; left: 0; bottom: -1px;
      width: 32px; height: 2px; background: var(--accent); border-radius: 2px;
    }}
    .report h2[data-idx="0"]::before {{ background: var(--accent); }}
    .report h2[data-idx="1"]::before {{ background: var(--violet); }}
    .report h2[data-idx="2"]::before {{ background: var(--orange); }}
    .report h2[data-idx="3"]::before {{ background: var(--neg); }}
    .report h2[data-idx="4"]::before {{ background: var(--pos); }}
    .report h2[data-idx="5"]::before {{ background: var(--cyan); }}
    .report > h1:first-child, .report > h2:first-child {{ margin-top: 0; }}

    .report h3 {{ font-size: 15.5px; font-weight: 600; margin: 32px 0 12px; line-height: 1.4; color: var(--ink-1); letter-spacing: -0.005em; }}
    .report h4 {{ font-size: 14px; font-weight: 600; margin: 20px 0 10px; line-height: 1.4; color: var(--ink-2); }}
    .report p {{ margin: 12px 0; color: var(--ink-2); font-size: 14px; line-height: 1.85; text-wrap: pretty; }}
    .report ul {{ margin: 12px 0 12px 4px; padding-left: 20px; color: var(--ink-2); }}
    .report li {{ margin: 5px 0; font-size: 14px; line-height: 1.8; }}

    .report blockquote {{
      margin: 18px 0; padding: 12px 18px; border-left: 2px solid var(--accent);
      background: transparent; color: var(--ink-2); font-size: 13.5px; font-weight: 500;
    }}

    .callout {{
      position: relative; color: #3d2c00;
      background: linear-gradient(180deg, #fff8d8 0%, #fff4c2 100%);
      border: 1px solid #f7d774; border-left: 4px solid #f9ab00; border-radius: var(--r-md);
      padding: 14px 18px 14px 44px; margin: 20px 0; font-weight: 500; font-size: 13.5px; line-height: 1.8;
    }}
    .callout::before {{
      content: ""; position: absolute; left: 16px; top: 17px; width: 16px; height: 16px;
      border-radius: 50%; background: #f9ab00; box-shadow: 0 0 0 4px rgba(249, 171, 0, 0.18);
    }}

    .table-wrap {{
      overflow-x: auto; margin: 14px 0 28px; border-radius: var(--r-md);
      background: var(--surface); border: 1px solid var(--line-1);
    }}
    table {{ width: 100%; border-collapse: separate; border-spacing: 0; min-width: 640px; background: var(--surface); font-size: 12.5px; }}
    th, td {{ padding: 7px 12px; border-bottom: 1px solid var(--line-2); vertical-align: middle; word-break: keep-all; color: var(--ink-1); line-height: 1.55; }}
    th {{
      padding: 9px 12px; background: var(--tint); font-weight: 500; color: var(--ink-3);
      font-size: 10.5px; letter-spacing: 0.06em; text-transform: uppercase;
      position: sticky; top: 0; z-index: 1; text-align: left; border-bottom: 1px solid var(--line-1); white-space: nowrap;
    }}
    th.align-right, td.align-right {{ text-align: right; }}
    th.align-center, td.align-center {{ text-align: center; }}
    td.align-right {{ font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 12px; color: var(--ink-1); letter-spacing: -0.01em; font-variant-numeric: tabular-nums; }}
    td .num-pos {{ color: var(--neg); font-weight: 500; }}
    td .num-neg {{ color: var(--pos); font-weight: 500; }}
    td .pill {{
      display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 500;
      line-height: 1.7; background: var(--accent-soft); color: var(--accent-ink); border: 1px solid var(--accent-hair);
    }}
    td .pill.pos {{ background: var(--pos-soft); color: var(--pos); border-color: #b7dfc4; }}
    td .pill.neg {{ background: var(--neg-soft); color: var(--neg); border-color: #f5c2c0; }}
    td .pill.warn {{ background: var(--warn-soft); color: var(--warn); border-color: #f7d774; }}
    td .pill.violet {{ background: #ece4fb; color: var(--violet); border-color: #d4c2f5; }}
    tbody td {{ color: var(--ink-2); }}
    tbody td:first-child {{ color: var(--ink-1); font-weight: 500; }}
    tbody tr:nth-child(even) td {{ background: #fafbfd; }}
    tr:last-child td {{ border-bottom: none; }}
    tbody tr:hover td {{ background: #eef4fe; }}

    code {{ background: var(--tint); border-radius: var(--r-xs); padding: 1px 6px; color: var(--accent-ink); font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 12.5px; font-weight: 500; border: 1px solid var(--line-1); }}
    pre {{ overflow-x: auto; background: #1f2329; color: #e6e9ef; border-radius: var(--r-md); padding: 18px 20px; margin: 16px 0; font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 12.5px; line-height: 1.65; border: 1px solid #2a2f37; }}

    /* chart cards */
    .chart-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin: 14px 0 8px; }}
    .chart-card {{ background: var(--surface); border-radius: var(--r-md); padding: 14px 16px 12px; border: 1px solid var(--line-1); transition: border-color 0.15s ease; }}
    .chart-card:hover {{ border-color: var(--accent-hair); }}
    .chart-title {{ font-size: 12px; font-weight: 600; margin: 0 0 4px; color: var(--ink-3); letter-spacing: 0.05em; text-transform: uppercase; }}
    .chart-subtitle {{ color: var(--ink-4); font-size: 11.5px; margin-bottom: 6px; font-family: "JetBrains Mono", ui-monospace, monospace; }}
    .chart-card svg {{ display: block; width: 100%; height: auto; overflow: visible; }}
    .kline-card {{ background: var(--surface); border: 1px solid var(--line-1); border-radius: var(--r-md); padding: 14px 16px 12px; margin: 12px 0 20px; overflow: hidden; position: relative; }}
    .kline-card svg {{ display: block; width: 100%; height: auto; overflow: visible; }}
    .kline-candle-up {{ fill: var(--neg); stroke: var(--neg); }}
    .kline-candle-down {{ fill: var(--pos); stroke: var(--pos); }}
    .amount-bar-up {{ fill: rgba(220, 38, 38, 0.42); }}
    .amount-bar-down {{ fill: rgba(5, 150, 105, 0.42); }}
    .kline-wick {{ stroke-width: 1; }}
    .ma-line {{ fill: none; stroke-width: 1.4; stroke-linejoin: round; stroke-linecap: round; }}
    .axis, .grid-line {{ stroke: var(--line-2); stroke-width: 1; }}
    .series-line {{ fill: none; stroke-width: 1.8; stroke-linejoin: round; stroke-linecap: round; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 6px 12px; color: var(--ink-3); font-size: 11.5px; margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--line-2); }}
    .legend span {{ display: inline-flex; align-items: center; gap: 6px; font-family: "JetBrains Mono", ui-monospace, monospace; }}
    .legend span::before {{ content: ""; display: inline-block; width: 8px; height: 2px; border-radius: 1px; background: var(--legend-color); }}

    /* valuation band */
    .band-row-label {{ fill: var(--ink-3); font-size: 11px; font-family: "JetBrains Mono", ui-monospace, monospace; }}
    .band-track {{ stroke: var(--line-1); stroke-width: 2; stroke-linecap: round; }}
    .band-box {{ fill: var(--accent-soft); }}
    .band-median {{ stroke: var(--ink-3); stroke-width: 1.5; }}
    .band-current {{ fill: var(--orange); stroke: #fff; stroke-width: 1.5; }}
    .band-pct {{ font-size: 10.5px; font-family: "JetBrains Mono", ui-monospace, monospace; }}

    /* hero summary card (核心判断) */
    .summary-card {{
      position: relative;
      background:
        radial-gradient(120% 140% at 100% 0%, rgba(111, 78, 224, 0.10) 0%, transparent 55%),
        radial-gradient(110% 130% at 0% 100%, rgba(26, 115, 232, 0.10) 0%, transparent 55%),
        linear-gradient(180deg, #f4f7fc 0%, #eaf0f8 100%);
      color: var(--ink-1); border: 1px solid #d8e2f0; border-radius: var(--r-lg);
      padding: 24px 28px 22px; margin: 4px 0 36px; overflow: hidden;
      box-shadow: 0 1px 2px rgba(15, 23, 32, 0.04), 0 8px 24px rgba(26, 115, 232, 0.06);
    }}
    .summary-card::before {{
      content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px;
      background: linear-gradient(90deg, var(--accent) 0%, var(--violet) 38%, var(--orange) 72%, var(--neg) 100%);
    }}
    .summary-card .summary-label {{
      font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--accent-ink);
      font-weight: 600; margin: 0 0 14px; display: inline-flex; align-items: center; gap: 10px;
      font-family: "JetBrains Mono", "Inter", ui-monospace, monospace;
    }}
    .summary-card .summary-label::before {{
      content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--accent);
      box-shadow: 0 0 0 4px rgba(26, 115, 232, 0.18);
    }}
    .summary-card .summary-body {{ font-size: 15px; line-height: 1.9; color: var(--ink-1); margin: 0 0 8px; text-wrap: pretty; }}
    .summary-card .summary-body:last-child {{ margin-bottom: 0; }}
    .summary-card .summary-body .num-pos {{ color: var(--neg); font-weight: 600; }}
    .summary-card .summary-body .num-neg {{ color: var(--pos); font-weight: 600; }}
    .summary-card .summary-body .kw {{
      color: var(--ink-1); font-weight: 600;
      background: linear-gradient(180deg, transparent 62%, rgba(249, 171, 0, 0.28) 62%, rgba(249, 171, 0, 0.28) 92%, transparent 92%);
      padding: 0 1px;
    }}

    @media (max-width: 1024px) {{ .chart-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
    @media (max-width: 900px) {{
      .page {{ width: calc(100vw - 24px); padding-top: 20px; margin-bottom: 64px; }}
      .chart-grid {{ grid-template-columns: 1fr; gap: 14px; }}
      .report h2 {{ font-size: 19px; margin-top: 40px; }}
      table {{ min-width: 520px; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <div class="doc-head">
      <span class="dh-title">{escaped_title}</span>
      <span class="dh-meta">{header_sub}{(" · " + latest) if (latest and header_sub) else latest}</span>
    </div>
    <section class="section report" id="report-body">
      {report_body}
    </section>
  </main>
  <script id="chart-data" type="application/json">{safe_charts_json}</script>
  <script>
  (function () {{
    /* ===== UI decoration — never edits text content ===== */
    const root = document.getElementById("report-body");
    if (!root) return;

    /* hide standalone "---" separators */
    root.querySelectorAll("p").forEach(p => {{
      const txt = p.textContent.trim();
      if (/^-{{3,}}$/.test(txt)) {{ p.style.display = "none"; p.setAttribute("aria-hidden", "true"); }}
    }});

    /* promote the leading "核心判断" section into a hero card */
    const summaryH = Array.from(root.querySelectorAll("h2, h3")).find(h => h.textContent.trim().startsWith("核心判断"));
    if (summaryH) {{
      const card = document.createElement("aside");
      card.className = "summary-card";
      const label = document.createElement("div");
      label.className = "summary-label";
      label.textContent = summaryH.textContent.trim();
      card.appendChild(label);
      const stopTags = summaryH.tagName === "H2" ? /^H[12]$/ : /^H[123]$/;
      const collected = [];
      let cur = summaryH.nextElementSibling;
      while (cur && !stopTags.test(cur.tagName)) {{
        const next = cur.nextElementSibling;
        const txt = cur.textContent.trim();
        if ((cur.tagName === "P" || cur.tagName === "UL") && txt && !/^-{{3,}}$/.test(txt)) {{
          cur.classList.add("summary-body");
          collected.push(cur);
        }} else {{
          cur.remove();
        }}
        cur = next;
      }}
      collected.forEach(node => card.appendChild(node));
      summaryH.replaceWith(card);
      collected.forEach(p => {{
        p.innerHTML = p.innerHTML.replace(/([+\\-])(\\d+(?:\\.\\d+)?)(%|pct|倍|x|分位)/g,
          (_, sign, num, unit) => `<span class="${{sign === "+" ? "num-pos" : "num-neg"}}">${{sign}}${{num}}${{unit}}</span>`);
      }});
    }}

    /* rotate h2 accent hue */
    let h2Idx = 0;
    root.querySelectorAll("h2").forEach(h => {{ h.setAttribute("data-idx", String(h2Idx % 6)); h2Idx += 1; }});

    /* colorize numeric / labelled table cells */
    const pillRules = [
      {{ re: /^(成长股|成熟龙头|红利价值)$/, cls: "pill" }},
      {{ re: /^(强连接|核心标的)$/, cls: "pill neg" }},
      {{ re: /^(弱连接|边缘受益)$/, cls: "pill warn" }},
      {{ re: /^(无连接|脱离主线)$/, cls: "pill" }},
      {{ re: /^(深度低估|合理偏低)$/, cls: "pill pos" }},
      {{ re: /^(偏高|透支|极度乐观)$/, cls: "pill neg" }},
      {{ re: /^(合理|合理定价)$/, cls: "pill" }},
      {{ re: /^(强|高)$/, cls: "pill neg" }},
      {{ re: /^(中)$/, cls: "pill warn" }},
      {{ re: /^(弱|低)$/, cls: "pill pos" }},
      {{ re: /^(基准|乐观|压力|Bull|Bear|Base)$/, cls: "pill violet" }}
    ];
    root.querySelectorAll("td").forEach(td => {{
      const trimmed = td.textContent.trim();
      if (!trimmed || td.children.length > 0) return;
      const signed = trimmed.match(/^([+\\-])(\\d[\\d,]*\\.?\\d*)\\s*(%|pct|x|倍|亿|万亿|分位)?$/);
      if (signed) {{ td.innerHTML = `<span class="${{signed[1] === "+" ? "num-pos" : "num-neg"}}">${{trimmed}}</span>`; return; }}
      for (const rule of pillRules) {{ if (rule.re.test(trimmed)) {{ td.innerHTML = `<span class="${{rule.cls}}">${{trimmed}}</span>`; return; }} }}
      if (/[+\\-]\\d/.test(trimmed)) {{
        td.innerHTML = td.innerHTML.replace(/([+\\-])(\\d+(?:[\\.,]\\d+)?)(%|pct|倍|x)?/g,
          (_, sign, num, unit) => `<span class="${{sign === "+" ? "num-pos" : "num-neg"}}">${{sign}}${{num}}${{unit || ""}}</span>`);
      }}
    }});
  }})();
  </script>
  <script>
  (function () {{
    const charts = JSON.parse(document.getElementById("chart-data").textContent || "{{}}");
    const root = document.getElementById("report-body");
    if (!root) return;
    const NS = "http://www.w3.org/2000/svg";

    /* ---------- shared helpers ---------- */
    const svgEl = (name, attrs) => {{
      const el = document.createElementNS(NS, name);
      Object.entries(attrs || {{}}).forEach(([k, v]) => el.setAttribute(k, v));
      return el;
    }};
    const svgText = (x, y, text, anchor, color, size) => {{
      const el = svgEl("text", {{ x, y, "text-anchor": anchor || "start", fill: color || "var(--ink-4)", "font-size": size || 11 }});
      el.textContent = text;
      return el;
    }};
    const fmtDate = v => String(v || "").replace(/^(\\d{{4}})(\\d{{2}})(\\d{{2}})$/, "$1-$2-$3");
    const fmtNum = (v, d) => Number.isFinite(v) ? v.toFixed(d == null ? (Math.abs(v) >= 100 ? 1 : Math.abs(v) >= 10 ? 2 : 2) : d) : "—";
    const fmtAmt = v => Number.isFinite(v) ? (v / 100000).toFixed(2) + "亿" : "—";  // daily.amount 单位千元 → 亿元
    const fmtYi = v => Number.isFinite(v) ? (v / 1e8).toFixed(2) + "亿" : "—";       // 元 → 亿元
    const findHeading = (texts) => {{
      const list = Array.from(root.querySelectorAll("h2, h3, h4"));
      for (const t of texts) {{ const h = list.find(e => e.textContent.includes(t)); if (h) return h; }}
      return null;
    }};
    const insertAfterBlock = (heading, node) => {{
      // place node right after heading (charts read best directly under the section title)
      heading.after(node);
    }};
    const mkCard = (cls, title, subtitle) => {{
      const card = document.createElement("article");
      card.className = cls;
      if (title) {{ const t = document.createElement("div"); t.className = "chart-title"; t.textContent = title; card.appendChild(t); }}
      if (subtitle) {{ const s = document.createElement("div"); s.className = "chart-subtitle"; s.textContent = subtitle; card.appendChild(s); }}
      return card;
    }};
    const mkLegend = (items) => {{
      const legend = document.createElement("div");
      legend.className = "legend";
      items.forEach(([label, color]) => {{
        const span = document.createElement("span");
        span.style.setProperty("--legend-color", color);
        span.textContent = label;
        legend.appendChild(span);
      }});
      return legend;
    }};
    const mkTooltip = (card) => {{
      const tip = document.createElement("div");
      tip.style.cssText = "position:absolute;background:rgba(15,23,42,0.92);color:#f1f5f9;padding:8px 12px;border-radius:8px;font-size:12px;line-height:1.5;pointer-events:none;opacity:0;transition:opacity .15s ease;z-index:100;white-space:nowrap;box-shadow:0 4px 12px rgba(0,0,0,0.2);";
      card.style.position = "relative";
      card.appendChild(tip);
      return tip;
    }};
    const moveTip = (tip, card, e) => {{
      const r = card.getBoundingClientRect();
      tip.style.left = Math.min(e.clientX - r.left + 12, r.width - tip.offsetWidth - 8) + "px";
      tip.style.top = Math.max(8, e.clientY - r.top - tip.offsetHeight - 12) + "px";
    }};

    drawValuationBands();
    drawFinancialTrends();

    /* ---------- valuation bands: PE / PB / PS over time (date x-axis) ---------- */
    function drawValuationBands() {{
      const series = charts.valuation_series || [];
      const bands = charts.valuation_bands || {{}};
      if (series.length < 2) return;
      const heading = findHeading(["估值快照与历史分位", "估值快照", "历史分位", "估值模式"]);
      if (!heading) return;

      const specs = [
        {{ key: "pe_ttm", title: "PE-TTM 估值带" }},
        {{ key: "pb", title: "PB 估值带" }},
        {{ key: "ps_ttm", title: "PS-TTM 估值带" }}
      ];
      const grid = document.createElement("div");
      grid.className = "chart-grid";
      specs.forEach(spec => {{
        const card = buildBandTimeCard(spec.key, spec.title);
        if (card) grid.appendChild(card);
      }});
      if (grid.children.length) insertAfterBlock(heading, grid);

      function buildBandTimeCard(key, title) {{
        // positive-only points (a negative PE/PB/PS is meaningless on a band)
        const pts = series
          .map((r, i) => ({{ i, date: String(r.trade_date || ""), v: Number(r[key]) }}))
          .filter(p => Number.isFinite(p.v) && p.v > 0);
        if (pts.length < 2) return null;
        const band = bands[key] || {{}};
        const windows = band.windows || {{}};
        const wkey = ["5y", "3y", "1y"].find(k => windows[k] && windows[k].sample_size);
        const win = wkey ? windows[wkey] : null;
        const current = Number.isFinite(Number(band.current)) ? Number(band.current) : pts[pts.length - 1].v;
        const pctText = win && Number.isFinite(win.current_percentile) ? ` · 分位 ${{win.current_percentile.toFixed(0)}}%` : "";
        const metricName = title.split(" ")[0];
        const card = mkCard("chart-card", title, `当前 ${{fmtNum(current)}}${{pctText}}`);
        const tip = mkTooltip(card);

        const W = 480, H = 220, pad = {{ l: 38, r: 40, t: 12, b: 28 }};
        const usableW = W - pad.l - pad.r, usableH = H - pad.t - pad.b;
        const svg = svgEl("svg", {{ viewBox: `0 0 ${{W}} ${{H}}`, role: "img" }});

        const levels = win ? [win.min, win.p25, win.median, win.p75, win.max].filter(Number.isFinite) : [];
        const vals = pts.map(p => p.v).concat(levels);
        let lo = Math.min(...vals), hi = Math.max(...vals);
        if (lo === hi) {{ lo -= 1; hi += 1; }}
        const sp = hi - lo; lo -= sp * 0.06; hi += sp * 0.06;
        const n = pts.length;
        const x = i => pad.l + (n <= 1 ? usableW / 2 : i / (n - 1) * usableW);
        const y = v => pad.t + (hi - v) / (hi - lo) * usableH;

        // shaded P25–P75 zone
        if (win && Number.isFinite(win.p25) && Number.isFinite(win.p75)) {{
          svg.appendChild(svgEl("rect", {{ x: pad.l, y: y(win.p75).toFixed(2), width: usableW, height: Math.max(1, y(win.p25) - y(win.p75)).toFixed(2), class: "band-box" }}));
        }}
        // horizontal percentile lines + right-edge labels
        const bandLines = win ? [
          ["max", win.max, "var(--neg)"],
          ["P75", win.p75, "var(--ink-4)"],
          ["中位", win.median, "var(--ink-3)"],
          ["P25", win.p25, "var(--ink-4)"],
          ["min", win.min, "var(--pos)"]
        ].filter(l => Number.isFinite(l[1])) : [];
        bandLines.forEach(item => {{
          const v = item[1], color = item[2];
          svg.appendChild(svgEl("line", {{ x1: pad.l, x2: W - pad.r, y1: y(v).toFixed(2), y2: y(v).toFixed(2), stroke: color, "stroke-width": 1, "stroke-dasharray": "3 3", opacity: 0.65 }}));
          svg.appendChild(svgText(W - pad.r + 3, y(v) + 3, fmtNum(v), "start", color, 9));
        }});

        // valuation line over time
        const d = pts.map((p, i) => `${{i === 0 ? "M" : "L"}} ${{x(i).toFixed(2)}} ${{y(p.v).toFixed(2)}}`).join(" ");
        svg.appendChild(svgEl("path", {{ d, class: "series-line", style: "stroke: var(--accent)" }}));
        const last = pts[pts.length - 1];
        svg.appendChild(svgEl("circle", {{ cx: x(n - 1).toFixed(2), cy: y(last.v).toFixed(2), r: 3.5, fill: "var(--orange)", stroke: "#fff", "stroke-width": 1.5 }}));

        // x-axis date ticks (~4) + baseline
        const ticks = 4;
        for (let t = 0; t <= ticks; t++) {{
          const idx = Math.round(t / ticks * (n - 1));
          svg.appendChild(svgText(x(idx), H - pad.b + 14, fmtDate(pts[idx].date).slice(0, 7), "middle", "var(--ink-4)", 9));
        }}
        svg.appendChild(svgEl("line", {{ x1: pad.l, x2: W - pad.r, y1: pad.t + usableH, y2: pad.t + usableH, class: "axis" }}));

        // hover crosshair
        const cursor = svgEl("line", {{ x1: 0, x2: 0, y1: pad.t, y2: pad.t + usableH, stroke: "var(--ink-4)", "stroke-width": 1, opacity: 0 }});
        svg.appendChild(cursor);
        const hit = svgEl("rect", {{ x: pad.l, y: pad.t, width: usableW, height: usableH, fill: "transparent", style: "cursor:crosshair" }});
        hit.addEventListener("mousemove", e => {{
          const r = svg.getBoundingClientRect();
          const px = (e.clientX - r.left) / r.width * W;
          let idx = Math.round((px - pad.l) / usableW * (n - 1));
          idx = Math.max(0, Math.min(n - 1, idx));
          const p = pts[idx];
          cursor.setAttribute("x1", x(idx)); cursor.setAttribute("x2", x(idx)); cursor.setAttribute("opacity", "1");
          tip.innerHTML = `<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${{fmtDate(p.date)}}</div><div>${{metricName}}: ${{fmtNum(p.v)}}</div>`;
          tip.style.opacity = "1";
          moveTip(tip, card, e);
        }});
        hit.addEventListener("mouseleave", () => {{ cursor.setAttribute("opacity", "0"); tip.style.opacity = "0"; }});
        svg.appendChild(hit);

        card.appendChild(svg);
        card.appendChild(mkLegend([[(wkey ? wkey.toUpperCase() : "") + "分位带", "var(--accent-soft)"], ["估值", "var(--accent)"], ["当前", "var(--orange)"]]));
        return card;
      }}
    }}

    /* ---------- 3. financial trends ---------- */
    function drawFinancialTrends() {{
      const rows = (charts.financial_trends || []).filter(r => r.end_date);
      if (rows.length < 2) return;
      const heading = findHeading(["成长性与财务质量诊断", "成长性与财务质量", "财务质量诊断", "成长性诊断"]);
      if (!heading) return;

      const labels = rows.map(r => String(r.end_date).replace(/^(\\d{{4}})(\\d{{2}})(\\d{{2}})$/, "$1/$2"));
      const grid = document.createElement("div");
      grid.className = "chart-grid";
      const configs = [
        {{ title: "营收 / 归母净利 (亿元)", type: "bars", fmt: fmtYi, fields: [
          {{ key: "revenue", label: "营收", color: "var(--blue)", div: 1e8 }},
          {{ key: "n_income", label: "归母净利", color: "var(--orange)", div: 1e8 }} ]}},
        {{ title: "同比增速 (%)", type: "line", fmt: v => fmtNum(v) + "%", fields: [
          {{ key: "tr_yoy", label: "营收YoY", color: "var(--blue)" }},
          {{ key: "netprofit_yoy", label: "归母YoY", color: "var(--orange)" }} ]}},
        {{ title: "盈利能力 (%)", type: "line", fmt: v => fmtNum(v) + "%", fields: [
          {{ key: "roe", label: "ROE", color: "var(--violet)" }},
          {{ key: "grossprofit_margin", label: "毛利率", color: "var(--pos)" }},
          {{ key: "netprofit_margin", label: "净利率", color: "var(--cyan)" }} ]}}
      ];
      configs.forEach(cfg => {{
        const hasData = cfg.fields.some(f => rows.some(r => Number.isFinite(r[f.key])));
        if (hasData) grid.appendChild(buildTrendCard(cfg, rows, labels));
      }});
      if (grid.children.length) insertAfterBlock(heading, grid);

      function buildTrendCard(cfg, rows, labels) {{
        const card = mkCard("chart-card", cfg.title, `${{labels[0]}} – ${{labels[labels.length - 1]}}`);
        const tip = mkTooltip(card);
        const W = 480, H = 210, pad = {{ l: 40, r: 14, t: 14, b: 30 }};
        const usableW = W - pad.l - pad.r, usableH = H - pad.t - pad.b;
        const svg = svgEl("svg", {{ viewBox: `0 0 ${{W}} ${{H}}`, role: "img" }});
        const series = cfg.fields.map(f => ({{
          field: f,
          pts: rows.map((r, i) => ({{ i, date: labels[i], v: Number.isFinite(r[f.key]) ? r[f.key] / (f.div || 1) : null }})).filter(p => Number.isFinite(p.v))
        }})).filter(s => s.pts.length);
        if (!series.length) {{ svg.appendChild(svgText(W / 2, H / 2, "暂无数据", "middle")); card.appendChild(svg); return card; }}
        const vals = series.flatMap(s => s.pts.map(p => p.v));
        let lo = Math.min(...vals, 0), hi = Math.max(...vals, 0);
        if (lo === hi) {{ lo -= 1; hi += 1; }}
        const sp = hi - lo; lo -= sp * 0.08; hi += sp * 0.08;
        const n = rows.length;
        const x = i => pad.l + (n <= 1 ? usableW / 2 : (i + 0.5) / n * usableW);
        const y = v => pad.t + (hi - v) / (hi - lo) * usableH;
        for (let i = 0; i <= 4; i++) {{
          const gy = pad.t + usableH * i / 4;
          svg.appendChild(svgEl("line", {{ x1: pad.l, x2: W - pad.r, y1: gy, y2: gy, class: "grid-line" }}));
        }}
        const y0 = y(0);
        if (y0 >= pad.t && y0 <= pad.t + usableH) svg.appendChild(svgEl("line", {{ x1: pad.l, x2: W - pad.r, y1: y0, y2: y0, class: "axis" }}));

        if (cfg.type === "bars") {{
          const groupW = usableW / n, bw = Math.min(18, groupW / (series.length + 0.6));
          series.forEach((s, si) => {{
            s.pts.forEach(p => {{
              const cx = pad.l + (p.i + 0.5) / n * usableW + (si - (series.length - 1) / 2) * bw;
              const py = y(p.v), barY = p.v >= 0 ? py : y0;
              svg.appendChild(svgEl("rect", {{ x: (cx - bw / 2).toFixed(2), y: barY.toFixed(2), width: bw.toFixed(2), height: Math.max(1, Math.abs(py - y0)).toFixed(2), fill: s.field.color, rx: 2, opacity: 0.85 }}));
            }});
          }});
        }} else {{
          series.forEach(s => {{
            const d = s.pts.map((p, i) => `${{i === 0 ? "M" : "L"}} ${{x(p.i).toFixed(2)}} ${{y(p.v).toFixed(2)}}`).join(" ");
            svg.appendChild(svgEl("path", {{ d, class: "series-line", style: `stroke:${{s.field.color}}` }}));
            s.pts.forEach(p => svg.appendChild(svgEl("circle", {{ cx: x(p.i).toFixed(2), cy: y(p.v).toFixed(2), r: 3, fill: s.field.color, stroke: "#fff", "stroke-width": 1.5 }})));
          }});
        }}

        // x labels + hover columns
        rows.forEach((r, i) => {{
          svg.appendChild(svgText(x(i), H - pad.b + 14, labels[i], "middle", "var(--ink-4)", 9.5));
          const hit = svgEl("rect", {{ x: (pad.l + i / n * usableW).toFixed(2), y: pad.t, width: (usableW / n).toFixed(2), height: usableH, fill: "transparent", style: "cursor:pointer" }});
          hit.addEventListener("mouseenter", () => {{
            tip.innerHTML = `<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${{labels[i]}}</div>` +
              cfg.fields.map(f => {{
                const raw = r[f.key];
                if (!Number.isFinite(raw)) return "";
                return `<div>${{f.label}}: ${{cfg.fmt(raw)}}</div>`;
              }}).join("");
            tip.style.opacity = "1";
          }});
          hit.addEventListener("mousemove", e => moveTip(tip, card, e));
          hit.addEventListener("mouseleave", () => {{ tip.style.opacity = "0"; }});
          svg.appendChild(hit);
        }});
        svg.appendChild(svgText(4, pad.t + 4, cfg.fmt(hi)));
        svg.appendChild(svgText(4, pad.t + usableH, cfg.fmt(lo)));
        card.appendChild(svg);
        card.appendChild(mkLegend(cfg.fields.map(f => [f.label, f.color])));
        return card;
      }}
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
    evidence_path = Path(args.evidence) if args.evidence else default_evidence_path(input_path)
    markdown_text = input_path.read_text(encoding="utf-8")
    title = args.title or input_path.stem
    evidence = load_evidence(evidence_path)
    charts = extract_chart_payload(evidence, evidence_path)
    html_text = render_html(markdown_text, charts, title, input_path)
    validation_warning: Optional[str] = None
    if not args.no_validate:
        try:
            validate_text_preserved(markdown_text, html_text)
        except RuntimeError as exc:
            if args.strict:
                raise
            # Content/format are decoupled: a preservation mismatch is reported as a
            # warning but never blocks HTML output (run with --strict to hard-fail).
            validation_warning = str(exc)
            print(f"warning: {exc}", file=sys.stderr)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_text, encoding="utf-8")
    print(json.dumps({
        "input": str(input_path),
        "output": str(output_path),
        "evidence": str(evidence_path) if evidence_path is not None else None,
        "valuation_series_points": len(charts.get("valuation_series") or []),
        "valuation_bands": sorted((charts.get("valuation_bands") or {}).keys()),
        "financial_periods": len(charts.get("financial_trends") or []),
        "validation_warning": validation_warning,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

