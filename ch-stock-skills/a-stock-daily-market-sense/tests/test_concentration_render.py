"""Browser regression for the compact concentration panel; no network or DB."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "shared"))
sys.path.insert(0, str(ROOT / "scripts"))

from html_report import ChartHook, HookExpectation, HtmlReportBuilder, SectionContract, SectionSpec
from render_report_html import CONCENTRATION_TIMELINE_JS, MARKET_SENSE_EXTRA_CSS, TREND_STATE_CARD_JS


READING = "成交额集中度：CR1% 17.92%、CR5% 43.09%，均为常态集中；缩量桶样本不足，分位回退无条件口径。"


def render_panel(rows, *, with_card=True, reading=READING):
    builder = HtmlReportBuilder(
        title="集中度展示回归样例（合成数据）", font_links=False,
        extra_css=MARKET_SENSE_EXTRA_CSS,
        contract=SectionContract(version="concentration-test/1", sections=[
            SectionSpec("pos_gate", ["环境与仓位总闸门"], level=2),
            SectionSpec("m4_decline", ["亏钱效应（爆量下跌）"], level=2),
        ]),
    )
    if with_card:
        builder.add_ui_decoration(TREND_STATE_CARD_JS)
    builder.add_chart_hook(
        ChartHook(name="concentration-timeline", payload={
            "recent": rows, "data_through": rows[-1]["date"],
        }, js=CONCENTRATION_TIMELINE_JS),
        expects=[HookExpectation(name="concentration-timeline", target_sec="pos_gate", expect_count=1)],
    )
    return builder.render(
        "# 1. 环境与仓位总闸门\n\n"
        "- 趋势状态：退潮（第 26 日）\n"
        "- 前瞻轴：未触发。\n"
        f"- {reading}\n\n"
        "# 5. 亏钱效应（爆量下跌）\n\n- 成交额集中度：结合前文，只解释主线拥挤背景。\n"
    )


def rows_for(states):
    return [{"date": f"2026-09-{i + 1:02d}", "cr_1pct": 17.92 + i / 10,
             "cr_5pct": 43.09 + i / 10, "env_bucket": "shrink", "state": state}
            for i, state in enumerate(states)]


class ConcentrationRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.tmp.cleanup()

    def open_panel(self, rows, **kwargs):
        path = Path(self.tmp.name) / "panel.html"
        path.write_text(render_panel(rows, **kwargs), encoding="utf-8")
        page = self.browser.new_page(viewport={"width": 390, "height": 844})
        self.addCleanup(page.close)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(path.as_uri())
        page.wait_for_selector(".concentration-timeline")
        self.assertEqual(errors, [])
        self.assertEqual(page.locator("html").get_attribute("data-render-status"), "ok")
        return page

    def test_reading_moves_once_and_keeps_caveat_visible(self):
        page = self.open_panel(rows_for(["normal"] * 30))
        self.assertEqual(page.locator(".concentration-reading li").inner_text(), READING)
        self.assertEqual(page.locator("#report-body").inner_text().count(READING), 1)
        self.assertTrue(page.locator(".concentration-reading").is_visible())
        self.assertIn("结合前文", page.locator("#report-body").inner_text())
        self.assertEqual(page.locator(".concentration-timeline svg polyline").count(), 2)
        self.assertTrue(page.evaluate("window.__sec.contains('pos_gate', document.querySelector('.concentration-timeline'))"))

    def test_normal_window_has_no_unused_driver_legend(self):
        page = self.open_panel(rows_for(["normal"] * 30))
        legend = page.locator(".concentration-timeline .stl-legend").inner_text()
        self.assertIn("CR1%", legend)
        self.assertIn("CR5%", legend)
        self.assertIn("量能：缩量", legend)
        self.assertNotIn("驱动：", legend)
        self.assertNotIn("放量", legend)
        hits = page.locator(".concentration-timeline svg rect[tabindex]")
        self.assertEqual(hits.count(), 30)
        self.assertIn("43.09%", hits.first.get_attribute("aria-label"))

    def test_only_drawn_states_enter_legend_and_missing_values_are_explicit(self):
        rows = rows_for(["normal", "passive", "passive", "overheat", "rotation"])
        rows[-1]["cr_1pct"] = None  # No driver dot can be placed on this date.
        page = self.open_panel(rows)
        legend = page.locator(".concentration-timeline .stl-legend").inner_text()
        self.assertEqual(legend.count("驱动：被动集中"), 1)
        self.assertEqual(legend.count("驱动：主动过热"), 1)
        self.assertNotIn("驱动：存量搬家", legend)
        self.assertIn("CR1% 缺失", page.locator(".concentration-timeline svg rect[tabindex]").last.get_attribute("aria-label"))

    def test_fallback_placement_and_mobile_page_do_not_overflow(self):
        page = self.open_panel(rows_for(["normal"] * 30), with_card=False)
        self.assertEqual(page.locator(".concentration-reading li").inner_text(), READING)
        self.assertTrue(page.evaluate("window.__sec.contains('pos_gate', document.querySelector('.concentration-timeline'))"))
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))


if __name__ == "__main__":
    unittest.main()
