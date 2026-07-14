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


if __name__ == "__main__":
    unittest.main()
