from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from output_gate.compiler import ContractConfigError, compile_gate_plan
from output_gate.gate import run_gate


class OutputGateTest(unittest.TestCase):
    def test_compiler_rejects_unregistered_validator(self) -> None:
        with self.assertRaises(ContractConfigError):
            compile_gate_plan(
                {
                    "schema_version": 1,
                    "skill": "demo",
                    "outputs": {
                        "report": {
                            "type": "markdown",
                            "terminal_capability": "finalize",
                            "path_glob": "reports/*.md",
                            "validators": [{"id": "model-decides-later"}],
                        }
                    },
                }
            )

    def test_compiler_shifts_markdown_heading_level_for_html(self) -> None:
        plan = compile_gate_plan(
            {
                "schema_version": 1,
                "skill": "demo",
                "outputs": {
                    "page": {
                        "type": "html",
                        "terminal_capability": "render",
                        "path_glob": "reports/*.html",
                        "features": [],
                        "contract": {
                            "sections": [
                                {"key": "hero", "patterns": ["^结论$"], "level": 2}
                            ]
                        },
                    }
                },
            }
        )
        validators = plan["outputs"]["page"]["validators"]
        headings = next(row for row in validators if row["id"] == "html-headings")
        self.assertEqual(headings["config"]["sections"][0]["level"], 3)

    def test_markdown_gate_checks_path_structure_and_evidence_date(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "demo"
            staged = root / "reports" / ".staging" / "report_2026-08-09.md"
            final = root / "reports" / "report_2026-08-09.md"
            evidence = root / "reports" / "evidence_2026-08-09.json"
            staged.parent.mkdir(parents=True)
            staged.write_text("# 结论\n\n有证据的正文。\n", encoding="utf-8")
            evidence.write_text("{}\n", encoding="utf-8")
            plan = compile_gate_plan(
                {
                    "schema_version": 1,
                    "skill": "demo",
                    "outputs": {
                        "report": {
                            "type": "markdown",
                            "terminal_capability": "finalize",
                            "path_glob": "reports/report_*.md",
                            "features": ["evidence-backed"],
                            "contract": {
                                "sections": [
                                    {"key": "hero", "patterns": ["^结论$"], "level": 1}
                                ]
                            },
                        }
                    },
                }
            )
            audit_path = root / "reports" / "audit.json"
            audit = run_gate(
                skill_root=root,
                gate_plan=plan,
                output_id="report",
                artifact=staged,
                final_path=final,
                receipts_path=root / "reports" / ".receipts.jsonl",
                audit_path=audit_path,
                evidence=[evidence],
            )
            self.assertTrue(audit["gate_pass"])
            self.assertTrue(json.loads(audit_path.read_text(encoding="utf-8"))["gate_pass"])

    def test_evidence_citation_validator_requires_parseable_evidence_and_reference(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "demo"
            artifact = root / "reports" / ".staging" / "news_2026-08-09.md"
            final = root / "reports" / "news_2026-08-09.md"
            evidence = root / "reports" / "evidence_2026-08-09.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("# 结论\n\n事实更新。[金十]\n", encoding="utf-8")
            evidence.write_text('{"items":[{"url":"https://example.com/a"}]}\n', encoding="utf-8")
            plan = compile_gate_plan(
                {
                    "schema_version": 1,
                    "skill": "demo",
                    "outputs": {
                        "news": {
                            "type": "markdown",
                            "terminal_capability": "finalize",
                            "path_glob": "reports/news_*.md",
                            "features": ["evidence-backed"],
                            "contract": {},
                            "validators": [
                                {
                                    "id": "evidence-citations",
                                    "config": {
                                        "min_citations": 1,
                                        "allow_source_markers": True,
                                    },
                                }
                            ],
                        }
                    },
                }
            )
            audit = run_gate(
                skill_root=root,
                gate_plan=plan,
                output_id="news",
                artifact=artifact,
                final_path=final,
                receipts_path=root / "reports" / ".receipts.jsonl",
                audit_path=root / "reports" / "audit.json",
                evidence=[evidence],
            )
            self.assertTrue(audit["gate_pass"])
            artifact.write_text("# 结论\n\n没有来源标记。\n", encoding="utf-8")
            audit = run_gate(
                skill_root=root,
                gate_plan=plan,
                output_id="news",
                artifact=artifact,
                final_path=final,
                receipts_path=root / "reports" / ".receipts.jsonl",
                audit_path=root / "reports" / "audit-fail.json",
                evidence=[evidence],
            )
            self.assertFalse(audit["gate_pass"])

    def test_trading_advice_policy_allows_factual_buying_language(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "demo"
            artifact = root / "reports" / ".staging" / "news_2026-08-09.md"
            final = root / "reports" / "news_2026-08-09.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("# 结论\n\n央行买入国债，资金净买入黄金。\n", encoding="utf-8")
            advice_patterns = [
                r"(?:建议读者|建议投资者|我们建议|操作建议|交易建议|可考虑|应当|应该|宜|请|务必)[^。；\n]{0,24}(?:买入|卖出|加仓|止损)",
                r"(?m)^(?:[-*]\s*)?(?:买入|卖出|加仓|止损)(?:[：:\s]|$)",
                r"(?m)^(?:[-*]\s*)?(?:目标价|止损价)\s*(?:为|设为|看到|上看|下看|[:：])?\s*(?:人民币|[¥￥$])?\s*\d",
            ]
            plan = compile_gate_plan(
                {
                    "schema_version": 1,
                    "skill": "demo",
                    "outputs": {
                        "news": {
                            "type": "markdown",
                            "terminal_capability": "finalize",
                            "path_glob": "reports/news_*.md",
                            "contract": {"forbidden_patterns": advice_patterns},
                        }
                    },
                }
            )
            audit = run_gate(
                skill_root=root,
                gate_plan=plan,
                output_id="news",
                artifact=artifact,
                final_path=final,
                receipts_path=root / "reports" / ".receipts.jsonl",
                audit_path=root / "reports" / "audit.json",
            )
            self.assertTrue(audit["gate_pass"])
            artifact.write_text("# 结论\n\n我们建议投资者立即买入。\n", encoding="utf-8")
            audit = run_gate(
                skill_root=root,
                gate_plan=plan,
                output_id="news",
                artifact=artifact,
                final_path=final,
                receipts_path=root / "reports" / ".receipts.jsonl",
                audit_path=root / "reports" / "audit-advice.json",
            )
            self.assertFalse(audit["gate_pass"])


if __name__ == "__main__":
    unittest.main()
