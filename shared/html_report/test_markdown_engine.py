from __future__ import annotations

import unittest

from markdown_engine import render_markdown


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


if __name__ == "__main__":
    unittest.main()
