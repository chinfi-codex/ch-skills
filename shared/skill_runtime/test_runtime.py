from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from output_gate.compiler import canonical_json, compile_gate_plan
from skill_runtime.runner import RuntimeFailure, execute_capability, parse_cli, verify_delivery


class RuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "demo-skill"
        self.root.mkdir()
        scripts = self.root / "scripts"
        scripts.mkdir()
        (scripts / "copy.py").write_text(
            "import argparse, shutil\n"
            "p=argparse.ArgumentParser(); p.add_argument('--input', required=True); "
            "p.add_argument('--output', required=True); a=p.parse_args()\n"
            "shutil.copyfile(a.input, a.output)\n",
            encoding="utf-8",
        )
        capabilities = {
            "schema_version": 1,
            "skill": "demo-skill",
            "capabilities": {
                "report.finalize": {
                    "kind": "finalize",
                    "terminal": True,
                    "judgment": "forbidden",
                    "outputs": ["report"],
                },
                "report.render": {
                    "kind": "command",
                    "entry": "scripts/copy.py",
                    "terminal": True,
                    "judgment": "forbidden",
                    "outputs": ["rendered"],
                    "artifact_arg": "--output",
                    "source_arg": "--input",
                },
                "utility.write": {
                    "kind": "command",
                    "entry": "scripts/copy.py",
                    "terminal": False,
                    "judgment": "forbidden",
                },
            },
        }
        outputs = {
            "schema_version": 1,
            "skill": "demo-skill",
            "outputs": {
                "report": {
                    "type": "markdown",
                    "terminal_capability": "report.finalize",
                    "path_glob": "reports/report_*.md",
                    "contract_version": "demo/1",
                    "contract": {
                        "min_bytes": 5,
                        "sections": [
                            {"key": "hero", "patterns": ["^结论$"], "level": 1}
                        ],
                    },
                },
                "rendered": {
                    "type": "markdown",
                    "terminal_capability": "report.render",
                    "path_glob": "reports/rendered_*.md",
                    "contract_version": "demo/1",
                    "contract": {
                        "min_bytes": 5,
                        "sections": [
                            {"key": "hero", "patterns": ["^结论$"], "level": 1}
                        ],
                    },
                },
            },
        }
        (self.root / "capabilities.yaml").write_text(
            yaml.safe_dump(capabilities, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        (self.root / "outputs.yaml").write_text(
            yaml.safe_dump(outputs, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        (self.root / "gate-plan.json").write_text(
            canonical_json(compile_gate_plan(outputs)), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_finalize_promotes_and_verify_matches_receipt_hash(self) -> None:
        staged = self.root / "reports" / ".staging" / "report_2026-08-09.md"
        staged.parent.mkdir(parents=True)
        staged.write_text("# 结论\n\n生产正文。\n", encoding="utf-8")
        final = self.root / "reports" / "report_2026-08-09.md"
        receipt = execute_capability(
            skill_root=self.root,
            capability_id="report.finalize",
            output_id="report",
            staged_path=staged,
            final_path=final,
        )
        self.assertEqual(receipt["status"], "success")
        self.assertTrue(receipt["gate_pass"])
        self.assertFalse(staged.exists())
        self.assertTrue(final.is_file())
        verified = verify_delivery(
            skill_root=self.root, output_id="report", artifact=final
        )
        self.assertTrue(verified["verified"])
        audit = Path(receipt["audit"])
        original_audit = audit.read_text(encoding="utf-8")
        audit.write_text(original_audit + " ", encoding="utf-8")
        with self.assertRaises(RuntimeFailure):
            verify_delivery(skill_root=self.root, output_id="report", artifact=final)
        audit.write_text(original_audit, encoding="utf-8")
        final.write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(RuntimeFailure):
            verify_delivery(skill_root=self.root, output_id="report", artifact=final)

    def test_failed_gate_keeps_staging_and_records_failure(self) -> None:
        staged = self.root / "reports" / ".staging" / "report_2026-08-10.md"
        staged.parent.mkdir(parents=True)
        staged.write_text("# 错误标题\n\n正文。\n", encoding="utf-8")
        final = self.root / "reports" / "report_2026-08-10.md"
        with self.assertRaises(RuntimeFailure):
            execute_capability(
                skill_root=self.root,
                capability_id="report.finalize",
                output_id="report",
                staged_path=staged,
                final_path=final,
            )
        self.assertTrue(staged.is_file())
        self.assertFalse(final.exists())
        receipts = [
            json.loads(line)
            for line in (self.root / "reports" / ".receipts.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(receipts[-1]["status"], "failed")
        self.assertFalse(receipts[-1]["gate_pass"])
        self.assertTrue(Path(receipts[-1]["audit"]).is_file())

    def test_runtime_bound_output_cannot_be_overridden(self) -> None:
        source = self.root / "reports" / "source_2026-08-11.md"
        source.parent.mkdir(parents=True)
        source.write_text("# 结论\n\n正文。\n", encoding="utf-8")
        final = self.root / "reports" / "rendered_2026-08-11.md"
        with self.assertRaises(RuntimeFailure):
            execute_capability(
                skill_root=self.root,
                capability_id="report.render",
                output_id="rendered",
                final_path=final,
                source_artifact=source,
                user_args=["--output", str(final)],
            )
        self.assertFalse(final.exists())

    def test_nonterminal_capability_cannot_target_final_output_glob(self) -> None:
        source = self.root / "source.md"
        source.write_text("# 结论\n", encoding="utf-8")
        final = self.root / "reports" / "report_2026-08-12.md"
        with self.assertRaises(RuntimeFailure):
            execute_capability(
                skill_root=self.root,
                capability_id="utility.write",
                user_args=["--input", str(source), "--output", str(final)],
            )
        self.assertFalse(final.exists())

    def test_cli_keeps_runtime_options_separate_from_passthrough(self) -> None:
        args, passthrough = parse_cli(
            [
                "--skill-root",
                ".",
                "run",
                "report.render",
                "--output-id",
                "rendered",
                "--final-path",
                "reports/rendered_2026-08-13.md",
                "--source-artifact",
                "reports/source_2026-08-13.md",
                "--evidence",
                "reports/evidence_2026-08-13.json",
                "--",
                "--theme",
                "default",
            ]
        )
        self.assertEqual(args.output_id, "rendered")
        self.assertEqual(args.final_path, Path("reports/rendered_2026-08-13.md"))
        self.assertEqual(len(args.evidence), 1)
        self.assertEqual(passthrough, ["--theme", "default"])


if __name__ == "__main__":
    unittest.main()
