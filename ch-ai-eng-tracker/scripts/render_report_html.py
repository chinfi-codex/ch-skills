#!/usr/bin/env python3
"""把周报 Markdown 渲染成自包含 HTML（shared/html_report 薄清单，无图表）。

用法：
    python scripts/render_report_html.py --input reports/2026-10-24/ai-eng-weekly.md
    python scripts/render_report_html.py --input report.md --output report.html --theme claude

主题：default（AlphaVault 风格）/ claude / print。校验失败默认转 warning 不阻断。
"""
from __future__ import annotations

import sys
from pathlib import Path

_BUNDLED = Path(__file__).resolve().parent / "_shared"
_DEV = Path(__file__).resolve().parents[2] / "shared"
sys.path.insert(0, str(_BUNDLED if (_BUNDLED / "html_report").exists() else _DEV))

from html_report import HtmlReportBuilder, RenderJob, render_report  # noqa: E402

SKILL = "ch-ai-eng-tracker"


def build_job(args) -> RenderJob:
    markdown_path = Path(args.input)
    markdown_text = markdown_path.read_text(encoding="utf-8")
    title = args.title or markdown_path.stem.replace("-", " ")
    builder = HtmlReportBuilder(title=title, theme=args.theme, lang="zh-CN")
    output = Path(args.output) if args.output else markdown_path.with_suffix(".html")
    return RenderJob(
        markdown_text=markdown_text,
        builder=builder,
        output_path=output,
        summary={"skill": SKILL, "theme": args.theme},
    )


if __name__ == "__main__":
    raise SystemExit(
        render_report(
            description=f"Render {SKILL} weekly markdown to self-contained HTML.",
            build_job=build_job,
        )
    )
