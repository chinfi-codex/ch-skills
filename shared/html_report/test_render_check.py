from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from . import render_check
from .cli import render_report


class CliGateSafetyTest(unittest.TestCase):
    def test_gate_rejects_no_validate_before_building(self) -> None:
        called = False

        def build_job(_args):
            nonlocal called
            called = True
            raise AssertionError("build_job must not run for conflicting flags")

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                render_report(
                    description="test",
                    build_job=build_job,
                    argv=["--input", "ignored.md", "--gate", "--no-validate"],
                )
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse(called)


class ExpectedBuildTest(unittest.TestCase):
    @staticmethod
    def _passing_result(build_id: str = "old-build") -> dict:
        return {
            "mode": "browser",
            "status": "ok",
            "contract_version": "dms/1.0.0",
            "build_id": build_id,
            "problems": [],
            "detail": {},
            "console_errors": [],
            "http_errors": [],
        }

    def test_site_and_online_require_expected_build(self) -> None:
        for stage in ("site", "online"):
            with self.subTest(stage=stage), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    render_check.main(["--target", "https://example.invalid/report.html", "--stage", stage])
                self.assertEqual(ctx.exception.code, 2)

    def test_stale_build_fails_even_when_contract_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            render_check,
            "check_browser",
            return_value=self._passing_result(),
        ), redirect_stderr(io.StringIO()):
            audit = Path(tmp) / "audit.json"
            code = render_check.main([
                "--target", "https://example.invalid/report.html",
                "--stage", "online",
                "--expect-contract", "dms/1.0.0",
                "--expect-build", "new-build",
                "--out", str(audit),
            ])
            self.assertEqual(code, 1)
            self.assertIn("expected build new-build", audit.read_text(encoding="utf-8"))

    def test_matching_build_passes(self) -> None:
        with patch.object(
            render_check,
            "check_browser",
            return_value=self._passing_result("wanted-build"),
        ), redirect_stderr(io.StringIO()):
            code = render_check.main([
                "--target", "https://example.invalid/report.html",
                "--stage", "online",
                "--expect-build", "wanted-build",
            ])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
