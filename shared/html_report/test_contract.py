from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from html_report.attestation import HookExpectation, RenderManifest  # noqa: E402
from html_report.builder import HtmlReportBuilder  # noqa: E402
from html_report.chart_hook import ChartHook  # noqa: E402
from html_report.contract import (  # noqa: E402
    ContractError,
    SectionContract,
    SectionSpec,
    strip_numbering,
)


BODY = (
    "<h2>报告</h2>"
    "<h3>1.1 市场状态定位</h3><p>状态</p>"
    "<h3>1.2 情绪趋势</h3><table></table>"
    "<h3>1.3 指数趋势</h3><p>指数</p>"
)


class StripNumberingTest(unittest.TestCase):
    def test_drops_leading_section_numbers(self) -> None:
        self.assertEqual(strip_numbering("1.2 情绪趋势"), "情绪趋势")
        self.assertEqual(strip_numbering("3.3、主线领导股"), "主线领导股")
        self.assertEqual(strip_numbering("5. 特征分组分析"), "特征分组分析")

    def test_leaves_numbers_inside_the_title_alone(self) -> None:
        self.assertEqual(strip_numbering("10:30 前涨停明细"), "10:30 前涨停明细")
        self.assertEqual(strip_numbering("涨停 vs 跌停家数"), "涨停 vs 跌停家数")


class StampTest(unittest.TestCase):
    def test_stamps_matched_headings(self) -> None:
        contract = SectionContract("t/1", [SectionSpec("sentiment", [r"情绪趋势"], level=3)])
        out, resolved = contract.stamp(BODY)
        self.assertIn('<h3 data-sec="sentiment" data-sec-level="3">1.2 情绪趋势</h3>', out)
        self.assertEqual(resolved["sentiment"].text, "1.2 情绪趋势")

    def test_renumbering_is_a_non_event(self) -> None:
        contract = SectionContract("t/1", [SectionSpec("sentiment", [r"情绪趋势"], level=3)])
        for numbering in ("1.1", "1.2", "4.9"):
            out, _ = contract.stamp(BODY.replace("1.2 情绪趋势", f"{numbering} 情绪趋势"))
            self.assertIn('data-sec="sentiment"', out)

    def test_ambiguous_pattern_fails(self) -> None:
        contract = SectionContract("t/1", [SectionSpec("trend", [r"趋势"])])
        with self.assertRaises(ContractError) as ctx:
            contract.stamp(BODY)
        self.assertIn("ambiguous", str(ctx.exception))

    def test_optional_section_may_be_absent(self) -> None:
        contract = SectionContract("t/1", [SectionSpec("gone", [r"不存在"], required=False)])
        out, resolved = contract.stamp(BODY)
        self.assertEqual(resolved, {})
        self.assertNotIn("data-sec", out)

    def test_every_problem_is_reported_at_once(self) -> None:
        contract = SectionContract("t/1", [
            SectionSpec("a", [r"没有这一节"]),
            SectionSpec("b", [r"也没有这一节"]),
        ])
        with self.assertRaises(ContractError) as ctx:
            contract.stamp(BODY)
        self.assertIn("[a]", str(ctx.exception))
        self.assertIn("[b]", str(ctx.exception))

    def test_two_specs_cannot_claim_one_heading(self) -> None:
        contract = SectionContract("t/1", [
            SectionSpec("first", [r"^情绪趋势$"]),
            SectionSpec("second", [r"情绪趋势"]),
        ])
        with self.assertRaises(ContractError) as ctx:
            contract.stamp(BODY)
        self.assertIn("already claimed", str(ctx.exception))

    def test_duplicate_keys_are_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            SectionContract("t/1", [SectionSpec("x", [r"a"]), SectionSpec("x", [r"b"])])

    def test_strict_order_rejects_reordered_sections(self) -> None:
        contract = SectionContract("t/1", [
            SectionSpec("state", [r"市场状态定位"]),
            SectionSpec("sentiment", [r"情绪趋势"]),
        ], order="strict")
        reordered = BODY.replace(
            '<h3>1.1 市场状态定位</h3><p>状态</p><h3>1.2 情绪趋势</h3><table></table>',
            '<h3>1.2 情绪趋势</h3><table></table><h3>1.1 市场状态定位</h3><p>状态</p>',
        )
        with self.assertRaises(ContractError) as ctx:
            contract.stamp(reordered)
        self.assertIn("strict section order mismatch", str(ctx.exception))


class ManifestTest(unittest.TestCase):
    def test_expectations_for_absent_sections_are_dropped(self) -> None:
        manifest = RenderManifest(
            contract_version="t/1", build_id="b",
            sections=["present", "absent"],
            hooks=[HookExpectation("h1", "present"), HookExpectation("h2", "absent")],
        ).filtered_for(["present"])
        self.assertEqual([h.name for h in manifest.hooks], ["h1"])
        self.assertEqual(list(manifest.sections), ["present"])


MD = "# 报告\n\n## 1.2 情绪趋势\n\n正文。\n"
CONTRACT = SectionContract("t/1", [SectionSpec("sentiment", [r"情绪趋势"], level=3)])


class BuilderIntegrationTest(unittest.TestCase):
    def test_contract_free_pages_are_unchanged(self) -> None:
        html = HtmlReportBuilder(title="x").render(MD)
        self.assertNotIn("render-manifest", html)
        self.assertNotIn("data-sec=", html)

    def test_manifest_is_embedded_and_parseable(self) -> None:
        builder = HtmlReportBuilder(title="x", contract=CONTRACT, build_id="fixed")
        builder.add_chart_hook(
            ChartHook(name="demo", payload={}, js="/* noop */"),
            expects=[HookExpectation(name="demo", target_sec="sentiment", expect_count=1)],
        )
        html = builder.render(MD)
        match = re.search(r'<script id="render-manifest" type="application/json">(.*?)</script>', html, re.DOTALL)
        self.assertIsNotNone(match)
        manifest = json.loads(match.group(1))
        self.assertEqual(manifest["contract_version"], "t/1")
        self.assertEqual(manifest["hooks"][0]["expect_count"], 1)
        self.assertTrue(manifest["build_id"].startswith("fixed-"))

    def test_content_contract_audit_is_embedded(self) -> None:
        builder = HtmlReportBuilder(
            title="x", contract=CONTRACT, contract_audit={"status": "ok", "degraded": []}
        )
        html = builder.render(MD)
        match = re.search(r'<script id="render-manifest" type="application/json">(.*?)</script>', html, re.DOTALL)
        manifest = json.loads(match.group(1))
        self.assertEqual(manifest["content_contract"]["status"], "ok")

    def test_expectation_on_unknown_section_is_a_programming_error(self) -> None:
        builder = HtmlReportBuilder(title="x", contract=CONTRACT)
        with self.assertRaises(ValueError):
            builder.declare(HookExpectation(name="demo", target_sec="typo"))

    def test_same_markdown_yields_the_same_build_id(self) -> None:
        first = HtmlReportBuilder(title="x", contract=CONTRACT, build_id="fixed").render(MD)
        second = HtmlReportBuilder(title="x", contract=CONTRACT, build_id="fixed").render(MD)
        pattern = r'"build_id": ?"([^"]+)"'
        self.assertEqual(re.search(pattern, first).group(1), re.search(pattern, second).group(1))


if __name__ == "__main__":
    unittest.main()
