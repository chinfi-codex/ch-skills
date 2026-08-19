"""把 base-UI 装饰脚本的效果在构建期写进 HTML。

`builder.py` 的 `_BASE_UI_JS` 在浏览器里给报告加几处装饰：隐藏独立的 ``---``
段、给 h2 轮转 accent 下标、把表格里的涨跌数字和星级评级染色。有些投递渠道
根本不执行页面脚本——PushPlus 把 HTML 以 innerHTML 注入自己的详情页，邮件
正文同理——那边这些装饰全部失效。

同一套规则在这里有一个构建期投影：不依赖 JS，直接改 HTML。渲染到浏览器的
报告继续走 `_BASE_UI_JS`，投递到无脚本渠道的走这里，规则改动两边都从本文件
的常量出发，避免各家 skill 各抄一份。
"""

from __future__ import annotations

import re
from typing import Optional

# 独立成段的 ``---``（Markdown 分隔线在引擎里落成一个段落）。
DASH_PARAGRAPH_RE = re.compile(r"<p>\s*-{3,}\s*</p>")
H2_RE = re.compile(r"<h2([^>]*)>")
# 只匹配不含子元素的单元格（内容里没有 '<'），与装饰 JS 跳过
# ``td.children.length > 0`` 的判断等价。
TD_RE = re.compile(r"<td([^>]*)>([^<]*)</td>")
STARS_RE = re.compile(r"^[★☆]+$")
# 整格就是一个带符号的数字。
SIGNED_CELL_RE = re.compile(r"^([+\-])(\d[\d,]*\.?\d*)\s*(%|pct|x|倍|亿|万亿|分位)?$")
# 夹在文字里的带符号数字。符号必须出现在词首——句首、空白或左括号/顿号之后，
# 否则 ``2026-08-18``、``1-2 家``、``10Y-2Y`` 里的连字符会被当成跌幅染红。
SIGNED_INLINE_RE = re.compile(
    r"(?:^|(?<=[\s(（\[【，,、：:；;]))([+\-])(\d+(?:[.,]\d+)?)(%|pct|倍|x)?"
)
ACCENT_SLOTS = 6


def _decorate_headings(body_html: str) -> str:
    counter = [0]

    def repl(match: "re.Match[str]") -> str:
        idx = counter[0] % ACCENT_SLOTS
        counter[0] += 1
        return f'<h2 data-idx="{idx}"{match.group(1)}>'

    return H2_RE.sub(repl, body_html)


def colorize_cell(text: str) -> Optional[str]:
    """单元格文本 → 染过色的 HTML；没有可染的东西时返回 None。"""
    trimmed = text.strip()
    if not trimmed:
        return None
    if STARS_RE.match(trimmed):
        filled = trimmed.count("★")
        total = max(filled, 3)
        return "".join(
            '<span class="stars">★</span>' if i < filled else '<span class="stars dim">★</span>'
            for i in range(total)
        )
    signed = SIGNED_CELL_RE.match(trimmed)
    if signed:
        cls = "num-pos" if signed.group(1) == "+" else "num-neg"
        return f'<span class="{cls}">{trimmed}</span>'
    if SIGNED_INLINE_RE.search(text):
        return SIGNED_INLINE_RE.sub(
            lambda m: f'<span class="{"num-pos" if m.group(1) == "+" else "num-neg"}">'
                      f'{m.group(1)}{m.group(2)}{m.group(3) or ""}</span>',
            text,
        )
    return None


def _decorate_cells(body_html: str) -> str:
    def repl(match: "re.Match[str]") -> str:
        attrs, text = match.group(1), match.group(2)
        colored = colorize_cell(text)
        return f"<td{attrs}>{colored if colored is not None else text}</td>"

    return TD_RE.sub(repl, body_html)


def decorate_static(body_html: str, *, rule_class: str = "md-rule") -> str:
    """把装饰 JS 的效果直接写进 HTML（独立 ``---``、h2 下标、单元格染色）。"""
    out = DASH_PARAGRAPH_RE.sub(f'<hr class="{rule_class}">', body_html)
    out = _decorate_headings(out)
    out = _decorate_cells(out)
    return out
