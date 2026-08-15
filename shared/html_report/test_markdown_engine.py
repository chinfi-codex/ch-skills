from __future__ import annotations

import unittest

from markdown_engine import render_markdown, strip_front_matter


class FrontMatterTests(unittest.TestCase):
    def test_front_matter_is_removed_from_rendered_body(self) -> None:
        markdown = """---
title: 2026-07-13趋势复盘
type: 日周报
sources: 1
---

# A 股盘后市场复盘报告
"""
        self.assertEqual(
            render_markdown(markdown),
            "<h2>A 股盘后市场复盘报告</h2>",
        )

    def test_utf8_bom_before_front_matter_is_supported(self) -> None:
        markdown = "\ufeff---\ntitle: 报告\n---\n\n正文"
        self.assertEqual(strip_front_matter(markdown), "\n正文")

    def test_unclosed_front_matter_is_preserved(self) -> None:
        markdown = "---\ntitle: 报告\n正文"
        self.assertEqual(strip_front_matter(markdown), markdown)

    def test_leading_thematic_break_is_not_treated_as_front_matter(self) -> None:
        markdown = "---\n\n正文\n\n---\n\n结尾"
        self.assertEqual(strip_front_matter(markdown), markdown)


class NestedListTests(unittest.TestCase):
    def test_flat_list_keeps_existing_shape(self) -> None:
        rendered = render_markdown("- 甲\n- 乙")
        self.assertEqual(rendered, "<ul><li>甲</li><li>乙</li></ul>")

    def test_four_and_eight_space_indents_create_nested_lists(self) -> None:
        markdown = """- 主题分支：
    - **细分线路**
        - 原因：产业传导
        - 盘面映射股：甲、乙
    - **第二线路**
- 结论"""
        rendered = render_markdown(markdown)
        self.assertEqual(
            rendered,
            "<ul><li>主题分支：<ul><li><strong>细分线路</strong>"
            "<ul><li>原因：产业传导</li><li>盘面映射股：甲、乙</li></ul>"
            "</li><li><strong>第二线路</strong></li></ul></li><li>结论</li></ul>",
        )

    def test_two_space_indent_remains_supported(self) -> None:
        rendered = render_markdown("- 一级\n  - 二级")
        self.assertEqual(rendered, "<ul><li>一级<ul><li>二级</li></ul></li></ul>")


class HighlightAndCalloutTests(unittest.TestCase):
    def test_line_start_inline_highlight_does_not_swallow_following_sections(self) -> None:
        markdown = """==深度调研发现==【W3】正文判断。

### 下一节

后续正文。"""
        rendered = render_markdown(markdown)
        self.assertIn("<p><mark>深度调研发现</mark>【W3】正文判断。</p>", rendered)
        self.assertIn("<h4>下一节</h4>", rendered)
        self.assertIn("<p>后续正文。</p>", rendered)
        self.assertNotIn("deep-finding-card", rendered)

    def test_multiline_structured_tracking_block_still_renders_as_card(self) -> None:
        markdown = """==跟踪事项｜T1｜观察中
事项：验证订单兑现
变量：季度收入
=="""
        rendered = render_markdown(markdown)
        self.assertIn('<aside class="todo-card">', rendered)
        self.assertIn("验证订单兑现", rendered)
        self.assertIn("季度收入", rendered)


class ReadabilityTests(unittest.TestCase):
    def test_numbered_summary_renders_as_ordered_list(self) -> None:
        markdown = "## 摘要\n\n1. 第一条判断。\n2. 第二条判断。\n"
        rendered = render_markdown(markdown)
        self.assertIn("<ol><li>第一条判断。</li><li>第二条判断。</li></ol>", rendered)

    def test_bullets_and_numbers_do_not_absorb_each_other(self) -> None:
        markdown = "- 甲\n- 乙\n\n1. 一\n2. 二\n"
        rendered = render_markdown(markdown)
        self.assertIn("<ul><li>甲</li><li>乙</li></ul>", rendered)
        self.assertIn("<ol><li>一</li><li>二</li></ol>", rendered)

    def test_nested_numbers_under_a_bullet_keep_their_own_tag(self) -> None:
        markdown = "- 顶层\n  1. 子项一\n  2. 子项二\n"
        rendered = render_markdown(markdown)
        self.assertIn("<ol><li>子项一</li><li>子项二</li></ol>", rendered)
        self.assertTrue(rendered.startswith("<ul><li>顶层"))

    def test_author_line_breaks_survive_inside_a_paragraph(self) -> None:
        # 事实一行、`→` 推断一行，是作者有意的两行，不能被软合并成一大段。
        markdown = "设备订单强劲，交期拉长至 32 周。\n→ 超级周期从建模进入业绩实证。\n"
        rendered = render_markdown(markdown)
        self.assertIn(
            "<p>设备订单强劲，交期拉长至 32 周。<br>→ 超级周期从建模进入业绩实证。</p>",
            rendered,
        )

    def test_blank_line_still_starts_a_new_paragraph(self) -> None:
        rendered = render_markdown("第一段。\n\n第二段。\n")
        self.assertIn("<p>第一段。</p>", rendered)
        self.assertIn("<p>第二段。</p>", rendered)
        self.assertNotIn("<br>", rendered)


if __name__ == "__main__":
    unittest.main()
