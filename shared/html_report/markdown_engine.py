"""Markdown → HTML engine shared across stock-skill renderers.

Pure standard library. Supports the subset of Markdown that research reports
need: headings, paragraphs, bullet lists, blockquotes, tables (with
alignment), inline code/bold/italic/links, fenced code blocks, and the
``==highlighted text==`` callout sugar. Analyzer Deep-mode finding
cards use a structured ``==深度调研发现｜...`` block.
"""

from __future__ import annotations

import html
import re
from typing import List


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


def _strip_callout_markers(lines: List[str]) -> List[str]:
    cleaned = [line.strip() for line in lines]
    if cleaned:
        cleaned[0] = cleaned[0].removeprefix("==").strip()
        cleaned[0] = cleaned[0].removesuffix("==").strip()
    if cleaned:
        cleaned[-1] = cleaned[-1].removesuffix("==").strip()
    return [line for line in cleaned if line]


def _render_deep_finding(lines: List[str]) -> str:
    cleaned = _strip_callout_markers(lines)
    header = cleaned[0] if cleaned else "深度调研发现"
    parts = [part.strip() for part in re.split(r"[|｜]", header) if part.strip()]
    badges = parts[1:] if parts and parts[0] == "深度调研发现" else parts

    rows = {}
    notes: List[str] = []
    for line in cleaned[1:]:
        match = re.match(r"^(来源|原文|判断|影响)[：:]\s*(.+)$", line)
        if match:
            rows[match.group(1)] = match.group(2).strip()
        else:
            notes.append(line)

    out = [
        '<aside class="deep-finding-card">',
        '<div class="deep-finding-head"><span class="deep-finding-title">深度调研发现</span>',
    ]
    for badge in badges:
        out.append('<span class="deep-finding-sep">｜</span>')
        out.append(f'<span class="deep-finding-badge">{inline_markdown(badge)}</span>')
    out.append("</div>")
    out.append('<div class="deep-finding-body">')
    for label in ("来源", "原文", "判断", "影响"):
        value = rows.get(label)
        if value:
            out.append(
                f'<div class="deep-finding-row deep-finding-row-{label}">'
                f'<span class="deep-finding-label">{label}：</span>'
                f'<span class="deep-finding-value">{inline_markdown(value)}</span>'
                "</div>"
            )
    for note in notes:
        out.append(f'<p class="deep-finding-note">{inline_markdown(note)}</p>')
    out.append("</div></aside>")
    return "".join(out)


def render_callout(lines: List[str]) -> str:
    cleaned = _strip_callout_markers(lines)
    if cleaned and cleaned[0].startswith("深度调研发现"):
        return _render_deep_finding(lines)
    callout = " ".join(cleaned).strip()
    return f"<div class=\"callout\">{inline_markdown(callout)}</div>"


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
            if stripped == "==深度调研发现==":
                while idx + 1 < len(lines):
                    next_line = lines[idx + 1].strip()
                    if (
                        not next_line
                        or next_line.startswith("#")
                        or next_line.startswith("|")
                        or re.match(r"^[-*]\s+", next_line)
                    ):
                        break
                    idx += 1
                    callout_parts.append(next_line)
                    if next_line == "==" or next_line.endswith("=="):
                        break
            else:
                while not callout_parts[-1].endswith("==") and idx + 1 < len(lines):
                    idx += 1
                    callout_parts.append(lines[idx].strip())
            out.append(render_callout(callout_parts))
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
