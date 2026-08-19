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


def test_dates_ranges_and_spreads_are_not_painted_as_moves():
    out = pr.decorate_static(
        "<table><tr><td>2026-08-18</td><td>1-2 家</td><td>10Y-2Y</td></tr></table>"
    )
    assert "num-neg" not in out
    assert "<td>2026-08-18</td>" in out


def test_sign_after_punctuation_still_colorized():
    out = pr.decorate_static("<table><tr><td>环比 +6.1%</td></tr></table>")
    assert '<span class="num-pos">+6.1%</span>' in out


def test_star_cells_render_filled_and_dim():
    out = pr.decorate_static("<table><tr><td>★★☆</td></tr></table>")
    assert out.count('<span class="stars">★</span>') == 2
    assert out.count('<span class="stars dim">★</span>') == 1


def test_cells_with_child_elements_are_skipped():
    html = "<table><tr><td><strong>+8%</strong></td></tr></table>"
    assert pr.decorate_static(html) == html


def test_root_id_is_distinctive_enough_for_a_third_party_page():
    # 两个字符的 id 太容易和宿主页面撞车，撞上就会把清零规则泼到对方元素上
    assert len(pr.ROOT_ID) >= 4


# --- CSS 作用域化 --------------------------------------------------------- #
def test_root_and_body_collapse_onto_the_container():
    css = ":root{--a:1}\nbody{color:red}\nhtml{background:#fff}"
    out = pr.scope_and_shake(css, "<p>x</p>")
    assert out.count(f"{pr.ROOT}{{") == 3
    assert "body{" not in out and "html{" not in out


def test_universal_selector_also_covers_the_container():
    out = pr.scope_and_shake("*{margin:0}", "<p>x</p>")
    assert out == f"{pr.ROOT},{pr.ROOT} *{{margin:0}}"


def test_plain_selectors_get_prefixed():
    out = pr.scope_and_shake("td, th{padding:8px}", "<p>x</p>")
    assert out == f"{pr.ROOT} td,{pr.ROOT} th{{padding:8px}}"


def test_unused_class_rules_are_shaken_out():
    body = '<div class="table-wrap"><table></table></div>'
    css = ".table-wrap{overflow:auto}\n.kline-chart{height:300px}"
    out = pr.scope_and_shake(css, body)
    assert f"{pr.ROOT} .table-wrap{{overflow:auto}}" == out
    assert "kline" not in out


def test_descendant_selector_needs_every_class_present():
    body = '<section class="report"><p>x</p></section>'
    css = ".report .hero{font-size:20px}\n.report p{margin:0}"
    out = pr.scope_and_shake(css, body)
    assert "hero" not in out
    assert f"{pr.ROOT} .report p{{margin:0}}" in out


def test_negation_pseudo_class_does_not_shake_the_rule_out():
    body = '<section class="report"><h2>x</h2></section>'
    out = pr.scope_and_shake(".report h2:not(.plain){color:red}", body)
    assert "color:red" in out


def test_media_queries_are_recursed_and_keyframes_kept_verbatim():
    css = "@media (max-width:520px){.report{padding:4px}.gone{color:red}}\n@keyframes spin{from{opacity:0}}"
    out = pr.scope_and_shake(css, '<section class="report"></section>')
    assert f"@media (max-width:520px){{{pr.ROOT} .report{{padding:4px}}}}" in out
    assert "@keyframes spin{from{opacity:0}}" in out


def test_empty_media_block_is_dropped_entirely():
    out = pr.scope_and_shake("@media print{.nope{color:red}}", "<p>x</p>")
    assert out == ""


# --- 片段装配 ------------------------------------------------------------- #
def test_fragment_is_a_scoped_div_without_document_chrome():
    frag = pr.render_push_fragment("# 标题\n\n正文一句话。\n", theme="default")
    assert frag.startswith("<style>")
    assert f'<div id="{pr.ROOT_ID}">' in frag
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
        assert f'<div id="{pr.ROOT_ID}">' in frag


def _stderr_of(fn) -> str:
    import io
    import sys as _sys

    buf, saved = io.StringIO(), _sys.stderr
    _sys.stderr = buf
    try:
        fn()
    finally:
        _sys.stderr = saved
    return buf.getvalue()


def test_engine_dropping_text_is_warned_about():
    dropped = _stderr_of(lambda: pr.render_push_fragment("# 标题\n\n正文。\n"))
    assert "WARNING" not in dropped  # 正常报告不该有噪音
    try:
        pr.validate_text_preserved("这句话不在里面", "<p>别的</p>")
    except RuntimeError:
        return  # 校验器确实会在丢字时抛，render_push_fragment 把它转成 WARNING
    raise AssertionError("validate_text_preserved 没有对丢失的正文报错")


def test_star_ratings_do_not_trigger_a_false_warning():
    # 装饰把 ★★☆ 染成三个 ★ span，若校验放在装饰之后，每份带星级的报告都会误报
    noise = _stderr_of(
        lambda: pr.render_push_fragment("# T\n\n| 主线 | 评级 |\n|---|---|\n| A | ★★☆ |\n")
    )
    assert noise == ""


def test_fragment_stats_parts_add_up_to_total():
    frag = pr.render_push_fragment("# 标题\n\n正文一句话。\n", theme="default")
    stats = pr.fragment_stats(frag)
    assert stats["css"] + stats["body"] + stats["shell"] == stats["total"]
    assert stats["body"] < stats["total"] - stats["css"]  # 外壳不再算进正文


def test_preview_wrapper_embeds_the_exact_fragment():
    frag = pr.render_push_fragment("# 标题\n\n正文。\n", theme="default")
    page = pr.wrap_preview(frag, "标题 & <b>")
    assert frag in page
    assert "&amp; &lt;b&gt;" in page
    assert page.startswith("<!doctype html>")
