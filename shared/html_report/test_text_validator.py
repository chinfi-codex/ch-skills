from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from html_report.markdown_engine import render_markdown  # noqa: E402
from html_report.text_validator import validate_text_preserved  # noqa: E402


class FrontMatterValidationTests(unittest.TestCase):
    def test_front_matter_is_metadata_not_visible_report_text(self) -> None:
        markdown = """---
title: 2026-07-13趋势复盘
type: 日周报
sources: 1
---

# A 股盘后市场复盘报告

正文内容。
"""
        html = render_markdown(markdown)
        validate_text_preserved(markdown, html)


if __name__ == "__main__":
    unittest.main()
