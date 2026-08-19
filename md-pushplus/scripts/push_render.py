#!/usr/bin/env python3
"""把 Markdown 渲染成「适配 PushPlus 详情页」的自包含 HTML 片段。

为什么不是直接推一整页 HTML：PushPlus 把 content 以 innerHTML 注入自己的详情页
容器，实测（2026-08-18，pushplus.plus/shortMessage/…）有三个后果——

1. 页面里的 <script> 一律不执行。shared/html_report 的装饰脚本（数字红绿、
   h2 轮色、隐藏独立 ``---``）全部失效，正文里会裸露 ``---`` 这样的分隔符。
2. 主题里的 ``*`` / ``html`` / ``body`` 规则会作用到 PushPlus 自己的页面上，
   把宿主的字体、底色、间距一起改掉。
3. ``<!doctype>`` / ``<head>`` / ``<title>`` / ``<meta>`` 被当作正文解析，成为
   垃圾节点；而 ``.page`` 的 ``calc(100vw - 40px)`` 算的是视口宽而非容器宽，
   在窄容器里会溢出。

所以推送包走这条独立路径：同一套 shared Markdown 引擎 + 同一套主题 CSS，但
外壳换成一个带作用域的 <div>，装饰在 Python 侧静态做完，用不到的 CSS 规则
按片段里实际出现的 class 摇掉。主题仍然只维护一份，改 themes/*.css 推送跟着变。
"""
from __future__ import annotations

import re
from importlib import resources
from typing import Dict, List, Optional, Set, Tuple

from html_report import list_themes, render_markdown

# 片段根节点 id。短是有意的：CSS 作用域化要给每条选择器加前缀，一个字符都要省，
# content 有约 4 万字符上限。
ROOT_ID = "pp"
ROOT = f"#{ROOT_ID}"


# --------------------------------------------------------------------------- #
# 静态装饰：把 shared 的装饰 JS 在推送场景下做成构建期变换
#
# 规则与 shared/html_report/builder.py 的 _BASE_UI_JS 对齐（独立 ``---`` 段、
# h2 轮色下标、表格数字与星级染色）。那边改了规则，这里要跟着改——之所以复制
# 而不是复用，是因为推送渠道根本不执行 JS，同一套规则必须有一个构建期投影。
# --------------------------------------------------------------------------- #
_DASH_PARA_RE = re.compile(r"<p>\s*-{3,}\s*</p>")
_H2_RE = re.compile(r"<h2([^>]*)>")
_TD_RE = re.compile(r"<td([^>]*)>([^<]*)</td>")
_STARS_RE = re.compile(r"^[★☆]+$")
_SIGNED_CELL_RE = re.compile(r"^([+\-])(\d[\d,]*\.?\d*)\s*(%|pct|x|倍|亿|万亿|分位)?$")
_SIGNED_INLINE_RE = re.compile(r"([+\-])(\d+(?:[.,]\d+)?)(%|pct|倍|x)?")


def _decorate_headings(body_html: str) -> str:
    counter = [0]

    def repl(match: "re.Match[str]") -> str:
        idx = counter[0] % 6
        counter[0] += 1
        return f'<h2 data-idx="{idx}"{match.group(1)}>'

    return _H2_RE.sub(repl, body_html)


def _colorize_cell(text: str) -> Optional[str]:
    trimmed = text.strip()
    if not trimmed:
        return None
    if _STARS_RE.match(trimmed):
        filled = trimmed.count("★")
        total = max(filled, 3)
        return "".join(
            f'<span class="stars">★</span>' if i < filled else f'<span class="stars dim">★</span>'
            for i in range(total)
        )
    signed = _SIGNED_CELL_RE.match(trimmed)
    if signed:
        cls = "num-pos" if signed.group(1) == "+" else "num-neg"
        return f'<span class="{cls}">{trimmed}</span>'
    if re.search(r"[+\-]\d", trimmed):
        return _SIGNED_INLINE_RE.sub(
            lambda m: f'<span class="{"num-pos" if m.group(1) == "+" else "num-neg"}">'
                      f'{m.group(1)}{m.group(2)}{m.group(3) or ""}</span>',
            text,
        )
    return None


def _decorate_cells(body_html: str) -> str:
    def repl(match: "re.Match[str]") -> str:
        attrs, text = match.group(1), match.group(2)
        colored = _colorize_cell(text)
        return f"<td{attrs}>{colored if colored is not None else text}</td>"

    # 正则只匹配不含子元素的 <td>（内容里没有 '<'），与装饰 JS 跳过
    # ``td.children.length > 0`` 的判断等价。
    return _TD_RE.sub(repl, body_html)


def decorate_static(body_html: str) -> str:
    """把装饰 JS 的效果直接写进 HTML。"""
    out = _DASH_PARA_RE.sub('<hr class="md-rule">', body_html)
    out = _decorate_headings(out)
    out = _decorate_cells(out)
    return out


# --------------------------------------------------------------------------- #
# CSS：解析 → 按实际用到的 class 摇树 → 作用域化 → 压缩空白
# --------------------------------------------------------------------------- #
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CLASS_RE = re.compile(r"\.(-?[A-Za-z_][A-Za-z0-9_-]*)")
_ID_RE = re.compile(r"#(-?[A-Za-z_][A-Za-z0-9_-]*)")
_VERBATIM_AT_RE = re.compile(r"^@(-\w+-)?(keyframes|font-face|page|counter-style|property)\b", re.I)
_NESTED_AT_RE = re.compile(r"^@(media|supports|container|layer)\b", re.I)


def load_theme_css(theme: str) -> str:
    if theme not in list_themes():
        raise ValueError(f"unknown theme {theme!r}; available: {', '.join(list_themes())}")
    return (resources.files("html_report") / "themes" / f"{theme}.css").read_text(encoding="utf-8")


def _split_rules(css: str) -> List[Tuple[str, Optional[str]]]:
    """切成顶层 (prelude, body) 序列。body 为 None 表示 ``@import …;`` 这类语句。"""
    rules: List[Tuple[str, Optional[str]]] = []
    depth = 0
    start = 0
    body_start = 0
    prelude = ""
    i = 0
    n = len(css)
    while i < n:
        ch = css[i]
        if ch in "\"'":
            quote = ch
            i += 1
            while i < n and css[i] != quote:
                i += 2 if css[i] == "\\" else 1
        elif ch == "{":
            if depth == 0:
                prelude = css[start:i]
                body_start = i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                rules.append((prelude.strip(), css[body_start:i]))
                start = i + 1
        elif ch == ";" and depth == 0:
            statement = css[start:i + 1].strip()
            if statement:
                rules.append((statement, None))
            start = i + 1
        i += 1
    return rules


def _split_selectors(prelude: str) -> List[str]:
    """按顶层逗号切选择器，括号内（``:is(a, b)``）的逗号不算。"""
    parts: List[str] = []
    depth = 0
    buf: List[str] = []
    for ch in prelude:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _selector_is_used(selector: str, used_classes: Set[str], used_ids: Set[str]) -> bool:
    """选择器涉及的 class / id 是否都在片段里出现过。

    只按标签或伪类写的规则（``td, th``、``:root``）一律保留——判断不了就不删。
    descendant 选择器要求每一段都命中，所以是 AND 不是 OR。
    """
    classes = set(_CLASS_RE.findall(selector))
    ids = {i for i in _ID_RE.findall(selector) if i != ROOT_ID}
    if not classes and not ids:
        return True
    return classes <= used_classes and ids <= used_ids


def _scope_selector(selector: str) -> str:
    s = selector.strip()
    if s == "*":
        # 主题用 ``*`` 做 box-sizing/清零，根节点自己也要吃到这条。
        return f"{ROOT},{ROOT} *"
    if s in ("html", "body", "html body", ":root"):
        return ROOT
    for prefix in (":root", "html body ", "html ", "body "):
        if s.startswith(prefix):
            return (ROOT + s[len(prefix):]).rstrip()
    return f"{ROOT} {s}"


def _transform_rules(rules: List[Tuple[str, Optional[str]]],
                     used_classes: Set[str], used_ids: Set[str]) -> List[str]:
    out: List[str] = []
    for prelude, body in rules:
        if body is None:
            out.append(prelude)
            continue
        if _NESTED_AT_RE.match(prelude):
            inner = _transform_rules(_split_rules(body), used_classes, used_ids)
            if inner:
                out.append(f"{prelude}{{{''.join(inner)}}}")
            continue
        if _VERBATIM_AT_RE.match(prelude) or prelude.startswith("@"):
            out.append(f"{prelude}{{{body}}}")
            continue
        kept = [s for s in _split_selectors(prelude) if _selector_is_used(s, used_classes, used_ids)]
        if not kept:
            continue
        out.append(f"{','.join(_scope_selector(s) for s in kept)}{{{body}}}")
    return out


def _collect_used(body_html: str) -> Tuple[Set[str], Set[str]]:
    classes: Set[str] = set()
    for attr in re.findall(r'class="([^"]*)"', body_html):
        classes.update(attr.split())
    ids = set(re.findall(r'id="([^"]*)"', body_html))
    return classes, ids


def scope_and_shake(css: str, body_html: str) -> str:
    """摇掉片段用不到的规则，再把剩下的全部限制在 ``#pp`` 子树内。"""
    stripped = _COMMENT_RE.sub("", css)
    used_classes, used_ids = _collect_used(body_html)
    used_classes.update({"section", "report"})  # 外壳自带
    used_ids.add("report-body")
    return "".join(_transform_rules(_split_rules(stripped), used_classes, used_ids))


def slim_css(css: str) -> str:
    """去掉注释与行首尾空白。不合并行、不动选择器与声明本身。"""
    stripped = _COMMENT_RE.sub("", css)
    return "\n".join(line for line in (ln.strip() for ln in stripped.splitlines()) if line)


# 推送专用覆盖：宿主容器宽度未知，一切按 100% 走；另外补两个主题里没有的类
# （静态化后的 ``---`` 分隔线、宽表横滑提示）。手写前缀，不进摇树。
PUSH_OVERRIDE_CSS = f"""
{ROOT}{{max-width:100%;padding:10px 0 14px;-webkit-text-size-adjust:100%;text-size-adjust:100%}}
{ROOT} .report{{max-width:100%;margin:0}}
{ROOT} hr.md-rule{{border:0;border-top:1px solid var(--line-2,#e8eaed);margin:24px 0}}
@media (max-width:520px){{
{ROOT} .report{{padding:16px 14px;border-radius:10px}}
/* 主题给桌面报告的表设了 min-width:520px + nowrap，手机上会让两三列的小表也被
   强制横滑。窄屏放开这两条：表按内容收进屏幕，真正超宽的表仍由 .table-wrap 横滑
   兜底。keep-all 是必须的——只放开 nowrap 的话，中文短词会被逐字拆成一列一个字
   （实测一行 55px 的表被撑到 136px 高），keep-all 让词保持完整，宁可横滑。 */
{ROOT} .report table{{min-width:0;font-size:13px}}
{ROOT} .report th,{ROOT} .report td{{white-space:normal;word-break:keep-all}}
{ROOT} .report th,{ROOT} .report td{{padding:7px 9px}}
}}
"""


def render_push_fragment(markdown_body: str, *, theme: str = "default") -> str:
    """Markdown → 可直接作为 PushPlus content 的自包含片段。"""
    body_html = decorate_static(render_markdown(markdown_body))
    css = slim_css(scope_and_shake(load_theme_css(theme), body_html)) + slim_css(PUSH_OVERRIDE_CSS)
    return (
        f"<style>{css}</style>"
        f'<div id="{ROOT_ID}"><section class="section report" id="report-body">'
        f"{body_html}"
        "</section></div>"
    )


def wrap_preview(fragment: str, title: str) -> str:
    """给片段套一层最小外壳，让本地浏览器能按手机视口预览。

    只加 doctype / charset / viewport / title——正文与样式跟推送出去的那份逐字节
    相同，所以预览所见即推送所得。
    """
    import html as _html

    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_html.escape(title)}</title></head>"
        '<body style="margin:0;background:#fff">'
        f"{fragment}</body></html>"
    )


def fragment_stats(fragment: str) -> Dict[str, int]:
    match = re.search(r"<style>(.*?)</style>", fragment, re.DOTALL)
    css_chars = len(match.group(1)) if match else 0
    return {"total": len(fragment), "css": css_chars, "body": len(fragment) - css_chars}
