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
import sys
from importlib import resources
from typing import Dict, List, Optional, Set, Tuple

from html_report import (
    decorate_static,
    list_themes,
    render_markdown,
    validate_text_preserved,
)

# 片段根节点 id。要注入的是第三方页面，所以带一个不太可能撞车的后缀：宿主自己
# 有个 id="pp" 的容器时，我们的 ``#pp,#pp *`` 清零规则就会泼到对方元素上，正是
# 这套片段方案要根除的污染。多出的四个字符对 4 万字符上限可以忽略。
ROOT_ID = "pp-doc"
ROOT = f"#{ROOT_ID}"
_BODY_OPEN = '<section class="section report" id="report-body">'
_BODY_CLOSE = "</section>"


# --------------------------------------------------------------------------- #
# CSS：解析 → 按实际用到的 class 摇树 → 作用域化 → 压缩空白
# --------------------------------------------------------------------------- #
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CLASS_RE = re.compile(r"\.(-?[A-Za-z_][A-Za-z0-9_-]*)")
# ``:not(.x)`` / ``:is(…)`` 里的 class 是否定或备选条件，不能当作"这条规则要求
# 片段里有 .x"——否则 ``.report h2:not(.plain)`` 会因为片段里没有 .plain 被整条
# 摇掉，而它本该对所有不带 .plain 的 h2 生效。
_PSEUDO_ARGS_RE = re.compile(r":(?:not|is|where|has)\([^)]*\)", re.I)
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
    descendant 选择器要求每一段都命中，所以是 AND 不是 OR；``:not()`` 这类函数
    伪类的参数先剥掉，它们描述的是"不要匹配什么"，不是本规则的前提。
    """
    plain = _PSEUDO_ARGS_RE.sub("", selector)
    classes = set(_CLASS_RE.findall(plain))
    ids = {i for i in _ID_RE.findall(plain) if i != ROOT_ID}
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


# 推送专用覆盖：宿主容器宽度未知,一切按容器走;另外补一个主题里没有的类
# （静态化后的 ``---`` 分隔线）。手写前缀,不进摇树。
PUSH_OVERRIDE_CSS = f"""
{ROOT}{{max-width:100%;padding:10px 0 14px;-webkit-text-size-adjust:100%;text-size-adjust:100%}}
/* 主题的列宽上限挂在 .page 上,而片段没有 .page（摇树时已被丢掉）。这里把上限
   补回来:宿主容器有多宽正文就有多宽的话,宽屏邮件里一行会拉到上千像素。 */
{ROOT} .report{{max-width:min(100%,860px);margin:0 auto}}
{ROOT} hr.md-rule{{border:0;border-top:1px solid var(--line-2,#e8eaed);margin:24px 0}}
@media (max-width:520px){{
{ROOT} .report{{padding:16px 14px;border-radius:10px}}
/* 主题给桌面报告的表设了 min-width:520px + nowrap，手机上会让两三列的小表也被
   强制横滑。窄屏放开这两条：表按内容收进屏幕，真正超宽的表仍由 .table-wrap 横滑
   兜底。keep-all 是必须的——只放开 nowrap 的话，中文短词会被逐字拆成一列一个字
   （实测一行 55px 的表被撑到 136px 高），keep-all 让词保持完整，宁可横滑。 */
{ROOT} .report table{{min-width:0;font-size:13px}}
{ROOT} .report th,{ROOT} .report td{{white-space:normal;word-break:keep-all;padding:7px 9px}}
}}
"""


def render_push_fragment(markdown_body: str, *, theme: str = "default",
                         validate: bool = True) -> str:
    """Markdown → 可直接作为 PushPlus content 的自包含片段。

    默认跑一遍文本保全校验:引擎吞掉正文时（嵌套表格、未闭合的 callout 块之类）
    要有人看见,否则一份缺了半章的日报会静默推出去,而字符数看着完全正常。与
    shared 的 CLI 同策略——只警告不阻断,内容与排版解耦。
    """
    raw_html = render_markdown(markdown_body)
    if validate:
        # 校验放在装饰之前：星级 ``★★☆`` 会被染色成三个 ★ span，装饰后的 HTML 拿去
        # 比对必然报"少了 ★★☆"。要抓的是引擎吞正文，不是我们自己的染色。
        try:
            validate_text_preserved(markdown_body, raw_html)
        except RuntimeError as exc:
            print(f"WARNING: text preservation check failed: {exc}", file=sys.stderr)
    body_html = decorate_static(raw_html)
    css = slim_css(scope_and_shake(load_theme_css(theme), body_html)) + slim_css(PUSH_OVERRIDE_CSS)
    fragment = (
        f"<style>{css}</style>"
        f'<div id="{ROOT_ID}">{_BODY_OPEN}'
        f"{body_html}"
        f"{_BODY_CLOSE}</div>"
    )
    return fragment


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
    """拆出 css / 正文 / 外壳三块字符数,三者与 total 严格相加。

    正文要准:接近 4 万上限时,判断该砍正文还是砍 CSS 全看这两个数。
    """
    style = re.search(r"<style>(.*?)</style>", fragment, re.DOTALL)
    css_chars = len(style.group(1)) if style else 0
    body = re.search(re.escape(_BODY_OPEN) + "(.*)" + re.escape(_BODY_CLOSE), fragment, re.DOTALL)
    body_chars = len(body.group(1)) if body else 0
    total = len(fragment)
    return {"total": total, "css": css_chars, "body": body_chars,
            "shell": total - css_chars - body_chars}
