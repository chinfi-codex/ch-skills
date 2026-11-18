#!/usr/bin/env python3
"""纯标准库的 Markdown → 内联样式 HTML 渲染，pushplus 渠道的降级路径。

从旧 md_to_pushplus.py 原样迁出，职责不变：

- 对 <style> 支持差的老邮件客户端（如 Outlook 桌面版）点名要 inline 时用它；
- 共享渲染包（shared/html_report）导入不到时也自动落到它。
注意它不是"更省字符"的选项：每个元素都挂 style，正文越长越亏，md 超过约
10KB 后整包反而比主题版更大。

同时提供两个与渲染无关的小工具，标题推导和 frontmatter 剥离是三个渠道共用的：

- strip_frontmatter：剥 Obsidian / Jekyll 的 YAML 元数据块（只做轻量 key: value）；
- derive_title：第一个 ``# 一级标题`` 优先，其次 frontmatter 的 ``title:``，最后文件名。
"""
from __future__ import annotations

import html
import re
from typing import List


# --------------------------------------------------------------------------- #
# Markdown → HTML（纯标准库，样式全部内联）
# --------------------------------------------------------------------------- #
def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) <= {"|", "-", ":", " "}


def _inline(text: str) -> str:
    escaped = html.escape(text)
    codes: List[str] = []

    def keep_code(m: "re.Match[str]") -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"@@C{len(codes) - 1}@@"

    escaped = re.sub(r"`([^`]+)`", keep_code, escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)
    for i, v in enumerate(codes):
        escaped = escaped.replace(f"@@C{i}@@", v)
    return escaped


def _flush(parts: List[str], out: List[str]) -> None:
    if not parts:
        return
    text = " ".join(p.strip() for p in parts if p.strip())
    if text:
        out.append(f"<p>{_inline(text)}</p>")
    parts.clear()


def _render_table(lines: List[str]) -> str:
    rows: List[List[str]] = []
    aligns: List[str] = []
    for idx, line in enumerate(lines):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if idx == 1 and _is_table_sep(line):
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
    header, body = rows[0], rows[1:]
    h = ['<table style="border-collapse:collapse;width:100%;margin:14px 0;font-size:14px;">']
    h.append("<thead><tr>")
    for i, cell in enumerate(header):
        a = aligns[i] if i < len(aligns) else "left"
        h.append(f'<th style="border:1px solid #d9dde3;padding:7px 10px;background:#f4f6f8;text-align:{a};">{_inline(cell)}</th>')
    h.append("</tr></thead><tbody>")
    for row in body:
        h.append("<tr>")
        for i, cell in enumerate(row):
            a = aligns[i] if i < len(aligns) else "left"
            h.append(f'<td style="border:1px solid #d9dde3;padding:7px 10px;text-align:{a};">{_inline(cell)}</td>')
        h.append("</tr>")
    h.append("</tbody></table>")
    return "".join(h)


def render_markdown_inline(md: str) -> str:
    lines = md.splitlines()
    out: List[str] = []
    para: List[str] = []
    idx = 0
    in_code = False
    code: List[str] = []
    list_open = False

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    while idx < len(lines):
        line = lines[idx]
        s = line.strip()

        if s.startswith("```"):
            _flush(para, out)
            close_list()
            if in_code:
                out.append(
                    '<pre style="background:#f6f8fa;border-radius:6px;padding:12px;'
                    'overflow:auto;font-size:13px;"><code>'
                    + html.escape("\n".join(code))
                    + "</code></pre>"
                )
                code, in_code = [], False
            else:
                in_code = True
            idx += 1
            continue
        if in_code:
            code.append(line)
            idx += 1
            continue

        if not s:
            _flush(para, out)
            close_list()
            idx += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.+)$", s)
        if m:
            _flush(para, out)
            close_list()
            level = len(m.group(1))
            sizes = {1: "24px", 2: "20px", 3: "17px", 4: "15px", 5: "14px", 6: "13px"}
            border = "border-bottom:1px solid #eaecef;padding-bottom:6px;" if level <= 2 else ""
            out.append(
                f'<h{level} style="font-size:{sizes[level]};margin:20px 0 10px;'
                f'line-height:1.35;{border}">{_inline(m.group(2))}</h{level}>'
            )
            idx += 1
            continue

        if s.startswith("|") and idx + 1 < len(lines) and _is_table_sep(lines[idx + 1]):
            _flush(para, out)
            close_list()
            tbl = [line, lines[idx + 1]]
            idx += 2
            while idx < len(lines) and lines[idx].strip().startswith("|"):
                tbl.append(lines[idx])
                idx += 1
            out.append(_render_table(tbl))
            continue

        if s.startswith(">"):
            _flush(para, out)
            close_list()
            quote = s.lstrip(">").strip()
            out.append(
                '<blockquote style="margin:12px 0;padding:6px 14px;color:#586069;'
                f'border-left:4px solid #d0d7de;background:#f8f9fb;">{_inline(quote)}</blockquote>'
            )
            idx += 1
            continue

        item = re.match(r"^[-*]\s+(.+)$", s)
        if item:
            _flush(para, out)
            if not list_open:
                out.append('<ul style="margin:10px 0;padding-left:22px;line-height:1.7;">')
                list_open = True
            out.append(f"<li>{_inline(item.group(1))}</li>")
            idx += 1
            continue

        ol = re.match(r"^\d+\.\s+(.+)$", s)
        if ol:
            _flush(para, out)
            if not list_open:
                out.append('<ul style="margin:10px 0;padding-left:22px;line-height:1.7;">')
                list_open = True
            out.append(f"<li>{_inline(ol.group(1))}</li>")
            idx += 1
            continue

        close_list()
        para.append(s)
        idx += 1

    if in_code:
        out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    _flush(para, out)
    close_list()
    return "\n".join(out)


def wrap_html_inline(body: str) -> str:
    """套一层有节制的内联样式容器。降级路径专用：不依赖 <style>，
    在过滤样式块的老客户端里也能保住基本排版。"""
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        'PingFang SC,Helvetica,Arial,sans-serif;font-size:15px;color:#24292e;'
        'line-height:1.7;max-width:720px;margin:0 auto;">'
        + body
        + "</div>"
    )


# --------------------------------------------------------------------------- #
# frontmatter 剥离与标题推导（三个渠道共用）
# --------------------------------------------------------------------------- #
def strip_frontmatter(md: str) -> "tuple[dict, str]":
    """剥离 Obsidian / Jekyll 风格的 YAML frontmatter（文件开头 --- ... ---）。
    返回 (元数据字典, 去掉 frontmatter 的正文)。只做轻量 key: value 解析，
    不引第三方 YAML 库——元数据本就不进推送正文，够用即可。"""
    if not md.startswith("---"):
        return {}, md
    lines = md.splitlines()
    if lines[0].strip() != "---":
        return {}, md
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            meta: dict = {}
            for raw in lines[1:i]:
                m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)$", raw)
                if m:
                    meta[m.group(1)] = m.group(2).strip().strip("'\"")
            body = "\n".join(lines[i + 1:]).lstrip("\n")
            return meta, body
    return {}, md


def derive_title(md: str, meta: dict, fallback: str) -> str:
    for line in md.splitlines():
        m = re.match(r"^#\s+(.+)$", line.strip())
        if m:
            return m.group(1).strip()[:100]
    if meta.get("title"):
        return meta["title"][:100]
    return fallback
