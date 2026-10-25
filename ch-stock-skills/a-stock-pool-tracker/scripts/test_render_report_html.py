from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import render_report_html as renderer  # noqa: E402


def make_records(count: int = 200) -> list[dict]:
    start = date(2026, 1, 1)
    records = []
    for idx in range(count):
        close = 10 + idx * 0.03
        records.append({
            "trade_date": (start + timedelta(days=idx)).isoformat(),
            "open": round(close - 0.05, 4),
            "high": round(close + 0.2, 4),
            "low": round(close - 0.25, 4),
            "close": round(close, 4),
            "pct_chg": 0.3,
            "amount": 100000 + idx * 100,
        })
    return records


def make_track(member_count: int = 5) -> dict:
    records = make_records()
    return {
        "asof": records[-1]["trade_date"],
        "members": [
            {
                "ts_code": f"60000{idx}.SH",
                "name": f"测试{idx}",
                "entry_date": records[120]["trade_date"],
                "kline_days_requested": 200,
                "kline_records": records,
            }
            for idx in range(member_count)
        ],
    }


def make_markdown(member_count: int = 5, with_legacy_sections: bool = False) -> str:
    rows = "\n".join(f"| 60000{idx} | 测试{idx} | 0 | +1.0% | 健康 |" for idx in range(member_count))
    legacy = ""
    if with_legacy_sections:
        legacy = """## 目录
- 不应保留的目录项
## 历史记录
- 不应保留的历史内容
## 7. 数据说明
"""
    return f"""# 涨停强势池日报 · 2026-07-19

==测试结论。==

## 介入提醒（形态参考）
今日无信号。
{legacy}
## 池内全景

| 代码 | 名称 | 入池天数 | 入池以来 | 定性 |
|---|---|---:|---:|---|
{rows}

## 消息面
今日池内无重要公告或消息。
"""


class LegacySectionTests(unittest.TestCase):
    def test_removes_only_legacy_standalone_sections(self) -> None:
        cleaned, removed = renderer.strip_legacy_sections(make_markdown(with_legacy_sections=True))
        self.assertEqual(removed, ["目录", "历史记录", "数据说明"])
        self.assertNotIn("不应保留", cleaned)
        self.assertIn("## 池内全景", cleaned)
        self.assertIn("今日池内无重要公告或消息", cleaned)

    def test_ignores_heading_like_text_inside_fences(self) -> None:
        source = "# 报告\n\n```markdown\n## 数据说明\n这里是代码示例，不是实际章节。\n```\n\n## 消息面\n保留正文。\n"
        cleaned, removed = renderer.strip_legacy_sections(source)
        self.assertEqual(removed, [])
        self.assertEqual(cleaned, source)

    def test_nonempty_legacy_data_notes_must_be_inlined_first(self) -> None:
        source = "# 报告\n\n## 数据说明\n成交额是估算值，不能当成精确口径。\n\n## 消息面\n保留正文。\n"
        with self.assertRaisesRegex(ValueError, "内联"):
            renderer.strip_legacy_sections(source)


class BuildJobTests(unittest.TestCase):
    def test_missing_track_json_fails_instead_of_dropping_all_klines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "pool_report.md"
            md_path.write_text(make_markdown(), encoding="utf-8")
            args = Namespace(input=str(md_path), output=None, title=None, theme="default", track=None)
            with self.assertRaisesRegex(FileNotFoundError, "pool_track.py"):
                renderer.build_job(args)

    def test_uses_shared_four_column_theme_and_full_kline_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            md_path = root / "pool_report.md"
            md_path.write_text(make_markdown(with_legacy_sections=True), encoding="utf-8")
            (root / "track.json").write_text(json.dumps(make_track(), ensure_ascii=False), encoding="utf-8")
            args = Namespace(input=str(md_path), output=None, title=None, theme="default", track=None)
            job = renderer.build_job(args)
            html = job.builder.render(job.markdown_text)

        self.assertEqual(job.builder.theme, "default")
        self.assertIn(".stock-kline-grid", html)
        self.assertIn("repeat(4, minmax(0, 1fr))", html)
        self.assertIn("pool-member-klines", html)
        self.assertEqual(job.summary["kline_members"], 5)
        self.assertEqual(job.summary["kline_records"], 1000)
        self.assertEqual(job.summary["removed_sections"], ["目录", "历史记录", "数据说明"])
        self.assertNotIn("不应保留", html)


class BrowserRenderTests(unittest.TestCase):
    def test_draws_200_candles_four_columns_and_exact_entry_markers(self) -> None:
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError as exc:
            self.skipTest(f"playwright 未安装：{exc}")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            md_path = root / "pool_report.md"
            html_path = root / "pool_report.html"
            track = make_track()
            md_path.write_text(make_markdown(with_legacy_sections=True), encoding="utf-8")
            (root / "track.json").write_text(json.dumps(track, ensure_ascii=False), encoding="utf-8")
            args = Namespace(input=str(md_path), output=str(html_path), title=None,
                             theme="default", track=None)
            job = renderer.build_job(args)
            html_path.write_text(job.builder.render(job.markdown_text), encoding="utf-8")

            with sync_playwright() as playwright:
                chrome_path = next((path for path in (
                    os.environ.get("CHROME_PATH"),
                    shutil.which("chromium"),
                    shutil.which("google-chrome"),
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                ) if path and Path(path).exists()), None)
                try:
                    browser = playwright.chromium.launch(
                        headless=True,
                        **({"executable_path": chrome_path} if chrome_path else {}),
                    )
                except Error as exc:
                    self.skipTest(f"Chromium / Chrome 不可用：{exc}")
                try:
                    page = browser.new_page(viewport={"width": 1600, "height": 1000})
                    page_errors: list[str] = []
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.goto(html_path.as_uri(), wait_until="domcontentloaded")
                    page.wait_for_selector(".pool-kline-grid .kline-card")

                    self.assertEqual(page.locator(".pool-kline-grid .kline-card").count(), 5)
                    columns = page.eval_on_selector(
                        ".pool-kline-grid",
                        "el => getComputedStyle(el).gridTemplateColumns.split(' ').filter(Boolean).length",
                    )
                    self.assertEqual(columns, 4)
                    self.assertEqual(page.locator(".kline-card").first.locator(".kline-wick").count(), 200)
                    self.assertEqual(page.locator(".pool-entry-marker").count(), 5)
                    first_marker = page.locator(".pool-entry-marker").first
                    self.assertEqual(first_marker.get_attribute("data-entry-index"), "120")
                    self.assertEqual(
                        first_marker.get_attribute("data-entry-date"),
                        track["members"][0]["entry_date"],
                    )
                    headings = page.locator("#report-body h2, #report-body h3").all_inner_texts()
                    self.assertFalse({"目录", "历史记录", "数据说明"}.intersection(headings))
                    self.assertEqual(page_errors, [])
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main()
