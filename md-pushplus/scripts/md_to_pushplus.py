#!/usr/bin/env python3
"""把一个 Markdown 文件渲染为 HTML，并通过 PushPlus 推送。

这是一个原子工具：只做「渲染 + 发送」两件确定性的事，不做任何内容判断。
标题、要推送哪个文件、推给哪个群组/渠道，都由调用方（模型/用户）决定后用参数传入。

渲染默认走仓库共享的 `shared/html_report` 主题模板（与各报告型 skill 出的 HTML
同源，默认 default 主题），推送 template 固定用 PushPlus 的 `html`。
`--renderer inline` 是降级路径：纯内联样式、体积小，用于对 <style> 支持差的
老邮件客户端；共享包导入不到时也会自动回落到它，并在 stderr 说明原因。

PushPlus 文档：https://www.pushplus.plus/doc/

用法示例：
    export PUSHPLUS_TOKEN=xxxx
    python md_to_pushplus.py report.md --title "今日宏观日报"
    python md_to_pushplus.py report.md --theme claude               # 换共享主题
    python md_to_pushplus.py report.md --dry-run --save-html out.html   # 只渲染、不发送
    python md_to_pushplus.py report.md --topic mygroup --channel mail   # 群组/邮件渠道
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import List, Optional

PUSHPLUS_ENDPOINT = "https://www.pushplus.plus/send"
# PushPlus 对 content 长度有上限（约 4 万字符），超出会被服务端拒绝。
CONTENT_LIMIT = 40000

# 共享渲染框架：装包后用 scripts/_shared，开发时回落仓库根的 shared/。
SCRIPT_ROOT = Path(__file__).resolve().parent
_BUNDLED_SHARED = SCRIPT_ROOT / "_shared"
_DEV_SHARED = SCRIPT_ROOT.parents[1] / "shared"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

try:  # 共享包缺失不该让推送整个失败——降级到内联渲染即可。
    from html_report import HtmlReportBuilder, list_themes  # noqa: E402

    SHARED_THEMES = list_themes()
    SHARED_IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # noqa: BLE001 — 任何导入期异常都只降级，不中断
    HtmlReportBuilder = None  # type: ignore[assignment]
    SHARED_THEMES = ["default", "claude", "print"]
    SHARED_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

DEFAULT_THEME = "default"


# --------------------------------------------------------------------------- #
# 降级渲染：Markdown → HTML（纯标准库，自包含；样式全部内联）
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
# 默认渲染：shared/html_report 主题模板
# --------------------------------------------------------------------------- #
_STYLE_BLOCK_RE = re.compile(r"(<style>)(.*?)(</style>)", re.DOTALL)


def slim_css(page_html: str) -> str:
    """把 <style> 块里的注释和缩进去掉。

    主题 CSS 有一万多字符的注释与缩进，而 PushPlus 的 content 有约 4 万字符上限；
    推送包里没人读这些注释，省下的额度留给正文。只删注释与行首尾空白、不合并行，
    选择器和声明本身一个字节不动。
    """

    def repl(m: "re.Match[str]") -> str:
        css = re.sub(r"/\*.*?\*/", "", m.group(2), flags=re.DOTALL)
        kept = [ln.strip() for ln in css.splitlines()]
        return m.group(1) + "\n".join(ln for ln in kept if ln) + m.group(3)

    return _STYLE_BLOCK_RE.sub(repl, page_html)


def render_with_theme(body_md: str, title: str, theme: str, *, meta_text: str) -> str:
    """用共享主题模板渲染成自包含单页 HTML。

    校验失败只警告不阻断（与 shared/html_report 的 CLI 同策略）：内容与排版解耦，
    文本保全对不上不该拦住一次推送。
    """
    assert HtmlReportBuilder is not None  # 调用方已确认共享包可用
    builder = HtmlReportBuilder(
        title=title,
        theme=theme,
        meta_text=meta_text,
        # 推送包要自包含：外链 Google Fonts 在微信/邮件里既加载不稳也没被主题字体族用到。
        font_links=False,
    )
    try:
        page = builder.render(body_md, validate=True)
    except RuntimeError as exc:
        print(f"WARNING: text preservation check failed: {exc}", file=sys.stderr)
        page = builder.render(body_md, validate=False)
    return slim_css(page)


# --------------------------------------------------------------------------- #
# PushPlus 发送
# --------------------------------------------------------------------------- #
def send_pushplus(token: str, title: str, content: str, *,
                  template: str = "html", topic: str = "", channel: str = "") -> dict:
    payload = {"token": token, "title": title, "content": content, "template": template}
    if topic:
        payload["topic"] = topic
    if channel:
        payload["channel"] = channel
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        PUSHPLUS_ENDPOINT, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a Markdown file to HTML and push via PushPlus.")
    ap.add_argument("md", help="Path to the Markdown file")
    ap.add_argument("--title", default=None, help="Push title (default: first H1, else filename)")
    ap.add_argument("--token", default=os.environ.get("PUSHPLUS_TOKEN", ""),
                    help="PushPlus token (default: $PUSHPLUS_TOKEN)")
    ap.add_argument("--template", default="html", help="PushPlus template (default: html)")
    ap.add_argument("--renderer", default="theme", choices=["theme", "inline"],
                    help="theme = shared/html_report theme template (default); "
                         "inline = self-contained inline-style fallback for clients that strip <style>")
    ap.add_argument("--theme", default=DEFAULT_THEME, choices=SHARED_THEMES,
                    help=f"Shared theme name for --renderer theme (default: {DEFAULT_THEME})")
    ap.add_argument("--topic", default="", help="Group code for one-to-many push (optional)")
    ap.add_argument("--channel", default="", help="Channel: wechat|mail|webhook|cp|sms (optional)")
    ap.add_argument("--save-html", default=None, help="Also write the rendered HTML to this path")
    ap.add_argument("--dry-run", action="store_true", help="Render only, do not send")
    args = ap.parse_args()

    md_path = Path(args.md)
    if not md_path.is_file():
        print(f"ERROR: file not found: {md_path}", file=sys.stderr)
        return 2
    md_text = md_path.read_text(encoding="utf-8")
    meta, body = strip_frontmatter(md_text)

    title = args.title or derive_title(body, meta, md_path.stem)

    renderer = args.renderer
    if renderer == "theme" and HtmlReportBuilder is None:
        print(f"WARNING: shared/html_report unavailable ({SHARED_IMPORT_ERROR}); "
              "falling back to --renderer inline.", file=sys.stderr)
        renderer = "inline"

    if renderer == "theme":
        content = render_with_theme(
            body, title, args.theme,
            meta_text=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        style_note = f"theme:{args.theme}"
    else:
        content = wrap_html_inline(render_markdown_inline(body))
        style_note = "inline"

    if args.save_html:
        Path(args.save_html).write_text(content, encoding="utf-8")
        print(f"Saved HTML → {args.save_html} ({len(content)} chars, {style_note})")

    if len(content) > CONTENT_LIMIT:
        print(f"WARNING: content is {len(content)} chars, over PushPlus limit ~{CONTENT_LIMIT}. "
              "PushPlus may reject it. Consider splitting the report.", file=sys.stderr)

    if args.dry_run:
        print(f"[dry-run] title={title!r}, renderer={style_note}, content={len(content)} chars, not sent.")
        return 0

    if not args.token:
        print("ERROR: no token. Set $PUSHPLUS_TOKEN or pass --token.", file=sys.stderr)
        return 2

    try:
        result = send_pushplus(args.token, title, content,
                               template=args.template, topic=args.topic, channel=args.channel)
    except urllib.error.URLError as e:
        print(f"ERROR: request failed: {e}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    if result.get("code") == 200:
        print(f"OK: pushed '{title}' ({style_note}, {len(content)} chars).")
        return 0
    print(f"FAILED: PushPlus returned code={result.get('code')} msg={result.get('msg')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
