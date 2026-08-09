from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]


class NewsRenderContractTest(unittest.TestCase):
    def test_macro_renderer_embeds_compiled_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "macro.html"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_report_html.py",
                    "--input",
                    "reports/macro_daily_2026-06-13.md",
                    "--output",
                    str(output),
                    "--strict",
                ],
                cwd=SKILL_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            html = output.read_text(encoding="utf-8")
            self.assertIn('data-sec="hero"', html)
            self.assertIn('data-sec="changes"', html)
            marker = '<script id="render-manifest" type="application/json">'
            payload = html.split(marker, 1)[1].split("</script>", 1)[0]
            manifest = json.loads(payload)
            self.assertEqual(manifest["contract_version"], "news/macro-daily/1.0.0")
            self.assertEqual(manifest["sections"][0], "hero")


if __name__ == "__main__":
    unittest.main()
