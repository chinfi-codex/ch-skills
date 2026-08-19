"""push_render 的确定性变换测试。

跑法（本机没装 pytest 时用后者，断言都是纯 assert，两种都能跑）：
    cd md-pushplus/scripts && python3 -m pytest test_push_render.py -q
    cd md-pushplus/scripts && python3 -c "import test_push_render as t; \
        [f() for n, f in vars(t).items() if n.startswith('test_')]; print('ok')"
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
_BUNDLED = SCRIPT_ROOT / "_shared"
_DEV = SCRIPT_ROOT.parents[1] / "shared"
sys.path.insert(0, str(_BUNDLED if _BUNDLED.exists() else _DEV))
sys.path.insert(0, str(SCRIPT_ROOT))

import push_render as pr  # noqa: E402


# --- 静态装饰 ------------------------------------------------------------- #
def test_standalone_dash_paragraph_becomes_rule():
    assert pr.decorate_static("<p>---</p>") == '<hr class="md-rule">'
    assert pr.decorate_static("<p>-----</p>") == '<hr class="md-rule">'


def test_dash_inside_text_is_left_alone():
    html = "<p>营收 --- 同比转正</p>"
    assert pr.decorate_static(html) == html


def test_h2_indices_rotate_across_six():
    html = "".join(f"<h2>第{i}节</h2>" for i in range(7))
    out = pr.decorate_static(html)
    assert 'data-idx="0"' in out and 'data-idx="5"' in out
    assert out.count('data-idx="0"') == 2  # 第 7 个绕回 0


def test_signed_cells_get_sign_classes():
    out = pr.decorate_static("<table><tr><td>+8bp</td><td>-1.2%</td><td>2410</td></tr></table>")
    assert '<span class="num-pos">+8</span>' in out
    assert '<span class="num-neg">-1.2%</span>' in out
    assert "<td>2410</td>" in out  # 无符号数字不染色


def test_star_cells_render_filled_and_dim():
    out = pr.decorate_static("<table><tr><td>★★☆</td></tr></table>")
    assert out.count('<span class="stars">★</span>') == 2
    assert out.count('<span class="stars dim">★</span>') == 1


def test_cells_with_child_elements_are_skipped():
    html = "<table><tr><td><strong>+8%</strong></td></tr></table>"
    assert pr.decorate_static(html) == html


# --- CSS 作用域化 --------------------------------------------------------- #
def test_root_and_body_collapse_onto_the_container():
    css = ":root{--a:1}\nbody{color:red}\nhtml{background:#fff}"
    out = pr.scope_and_shake(css, "<p>x</p>")
    assert out.count("#pp{") == 3
    assert "body{" not in out and "html{" not in out


def test_universal_selector_also_covers_the_container():
    out = pr.scope_and_shake("*{margin:0}", "<p>x</p>")
    assert out == "#pp,#pp *{margin:0}"


def test_plain_selectors_get_prefixed():
    out = pr.scope_and_shake("td, th{padding:8px}", "<p>x</p>")
    assert out == "#pp td,#pp th{padding:8px}"


def test_unused_class_rules_are_shaken_out():
    body = '<div class="table-wrap"><table></table></div>'
    css = ".table-wrap{overflow:auto}\n.kline-chart{height:300px}"
    out = pr.scope_and_shake(css, body)
    assert "#pp .table-wrap{overflow:auto}" == out
    assert "kline" not in out


def test_descendant_selector_needs_every_class_present():
    body = '<section class="report"><p>x</p></section>'
    css = ".report .hero{font-size:20px}\n.report p{margin:0}"
    out = pr.scope_and_shake(css, body)
    assert "hero" not in out
    assert "#pp .report p{margin:0}" in out


def test_media_queries_are_recursed_and_keyframes_kept_verbatim():
    css = "@media (max-width:520px){.report{padding:4px}.gone{color:red}}\n@keyframes spin{from{opacity:0}}"
    out = pr.scope_and_shake(css, '<section class="report"></section>')
    assert "@media (max-width:520px){#pp .report{padding:4px}}" in out
    assert "@keyframes spin{from{opacity:0}}" in out


def test_empty_media_block_is_dropped_entirely():
    out = pr.scope_and_shake("@media print{.nope{color:red}}", "<p>x</p>")
    assert out == ""


# --- 片段装配 ------------------------------------------------------------- #
def test_fragment_is_a_scoped_div_without_document_chrome():
    frag = pr.render_push_fragment("# 标题\n\n正文一句话。\n", theme="default")
    assert frag.startswith("<style>")
    assert '<div id="pp">' in frag
    for banned in ("<!doctype", "<html", "<head", "<script", "<title>"):
        assert banned not in frag.lower()


def test_fragment_keeps_theme_variables_and_shrinks_css():
    frag = pr.render_push_fragment("# 标题\n\n正文。\n", theme="default")
    stats = pr.fragment_stats(frag)
    assert "--g-blue" in frag
    assert stats["css"] < len(pr.load_theme_css("default"))


def test_every_theme_renders():
    from html_report import list_themes

    for theme in list_themes():
        frag = pr.render_push_fragment("# T\n\n| a | b |\n|---|---|\n| +1% | ★★☆ |\n", theme=theme)
        assert '<div id="pp">' in frag


def test_preview_wrapper_embeds_the_exact_fragment():
    frag = pr.render_push_fragment("# 标题\n\n正文。\n", theme="default")
    page = pr.wrap_preview(frag, "标题 & <b>")
    assert frag in page
    assert "&amp; &lt;b&gt;" in page
    assert page.startswith("<!doctype html>")
