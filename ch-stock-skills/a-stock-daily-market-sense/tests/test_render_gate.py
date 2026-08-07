"""Regression cover for the render contract and the deploy gate.

The failure this locks down: on 2026-08-03 the trend-chart panel was anchored
by ``includes("1.1") && includes("情绪趋势")``. Adding 「1.1 市场状态定位」
pushed 情绪趋势 to 1.2, the anchor stopped matching, and the hook's fallback
appended five charts to the end of the document. Nothing threw, nothing logged,
the page looked normal.

So the tests here are not "does it render" — they are "does a *wrong* render get
caught". Each browser test deliberately breaks the page in one of the ways the
pipeline actually breaks, and asserts the gate goes red with a message that
names the cause.

Fixtures are synthetic on purpose: the real evidence JSON is cleaned up after
each daily run, so a test that needed it would rot within a day (and would need
a Tushare token and a network). The Markdown is the genuine 2026-08-03 report,
because heading drift is exactly what the contract has to survive.

Run:  python3 tests/test_render_gate.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = SKILL_ROOT / "tests" / "fixtures"
SCRIPTS = SKILL_ROOT / "scripts"
SHARED = SKILL_ROOT.parents[1] / "shared"

sys.path.insert(0, str(SHARED))
sys.path.insert(0, str(SCRIPTS))


def _render(out_dir: Path) -> Path:
    """Run the real renderer entrypoint over the fixture report."""
    report = out_dir / "report_20260803.md"
    shutil.copy(FIXTURES / "report_20260803.md", report)
    shutil.copy(FIXTURES / "evidence_20260803_utf8.json", out_dir / "evidence_20260803_utf8.json")
    shutil.copy(FIXTURES / "kline_20260803.json", out_dir / "kline_20260803.json")
    html = out_dir / "report_20260803.html"
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_report_html.py"),
         "--input", str(report), "--output", str(html),
         "--market-data", str(FIXTURES / "market_data.json"),
         "--no-lifecycle"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"renderer failed: {result.stderr.strip()}")
    return html


def _gate(html: Path):
    from html_report.render_check import check_browser

    return check_browser(str(html), 30000)


def _problem_text(result) -> str:
    return "\n".join(f"{p.get('scope')}: {p.get('message')}" for p in result["problems"])


def _eval(html: Path, expression: str):
    """Load the page in chromium and read a value back out of the live DOM.

    Decorations and charts only exist after the page's own scripts run, so
    asserting on the HTML file text would test nothing. The viewport is sized
    explicitly because ``getBBox`` on a zero-width viewport returns zeros and
    would make every layout assertion pass vacuously.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(html.resolve().as_uri(), wait_until="load")
            page.wait_for_function("() => document.documentElement.dataset.renderStatus", timeout=15000)
            return page.evaluate(expression)
        finally:
            browser.close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *args) -> None:
        pass


@contextmanager
def _serve(directory: Path):
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(_QuietHandler, directory=str(directory)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class SectionContractTest(unittest.TestCase):
    """Build-time resolution — no browser needed."""

    def setUp(self) -> None:
        from html_report.markdown_engine import render_markdown
        from render_report_html import DMS_CONTRACT

        self.contract = DMS_CONTRACT
        self.body = render_markdown((FIXTURES / "report_20260803.md").read_text(encoding="utf-8"))

    def test_resolves_every_section_of_the_current_template(self) -> None:
        _, resolved = self.contract.stamp(self.body)
        self.assertEqual(sorted(resolved), sorted(spec.key for spec in self.contract.sections))

    def test_survives_renumbering(self) -> None:
        """The 1.1 → 1.2 shift that broke the old anchor must be a non-event."""
        shifted = self.body.replace("1.2 情绪趋势", "2.7 情绪趋势")
        _, resolved = self.contract.stamp(shifted)
        self.assertIn("sentiment_trend", resolved)
        self.assertIn('data-sec="sentiment_trend"', self.contract.stamp(shifted)[0])

    def test_missing_section_fails_the_build(self) -> None:
        from html_report.contract import ContractError

        dropped = self.body.replace("情绪趋势", "情绪面观察")
        with self.assertRaises(ContractError) as ctx:
            self.contract.stamp(dropped)
        self.assertIn("sentiment_trend", str(ctx.exception))

    def test_demoted_heading_fails_the_build(self) -> None:
        from html_report.contract import ContractError

        demoted = re.sub(
            r"<h3>(1\.2 情绪趋势)</h3>", r"<h4>\1</h4>", self.body
        )
        with self.assertRaises(ContractError) as ctx:
            self.contract.stamp(demoted)
        self.assertIn("h3", str(ctx.exception))


class RenderGateTest(unittest.TestCase):
    """View-time attestation — these load the page in chromium."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import playwright  # noqa: F401
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest(
                "playwright not installed; run `python3 -m pip install playwright` then "
                "`python3 -m playwright install chromium`"
            )
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out_dir = Path(cls._tmp.name)
        cls.html = _render(cls.out_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_wellformed_render_passes(self) -> None:
        result = _gate(self.html)
        self.assertEqual(result["status"], "ok", _problem_text(result))
        hooks = result["detail"]["hooks"]
        self.assertEqual(hooks["market-trends"]["rendered"], 5)
        self.assertEqual(hooks["klines.index"]["rendered"], 3)
        self.assertEqual(hooks["market-state.ladder"]["rendered"], 1)
        self.assertEqual(hooks["market-state.ladder"]["placed_in"], "market_state")

    def test_unmatched_rows_are_allowed_but_must_carry_a_reason(self) -> None:
        """Two fixture stocks deliberately have no K-line data."""
        result = _gate(self.html)
        gaps = [
            item
            for row in result["detail"]["hooks"].values()
            for item in (row.get("unmatched") or [])
        ]
        self.assertTrue(gaps, "fixture should exercise the unmatched path")
        for item in gaps:
            self.assertIn(item["reason"], ("no_kline_data", "no_records_in_window"), item)

    def test_market_state_card_is_built_from_the_11_readings(self) -> None:
        """1.1 must reach the reader as a card: tier badges, a ✓/✗ checklist and
        a hit counter — all of it lifted from text the Markdown already wrote."""
        card = _eval(self.html, """() => {
          const el = document.querySelector('.market-state-card');
          if (!el) return {card: false};
          return {
            card: true,
            badges: [...el.querySelectorAll('.msc-badge')].map(b => b.textContent),
            badgeClasses: [...el.querySelectorAll('.msc-badge')].map(b => b.className),
            score: (el.querySelector('.msc-score') || {}).textContent || '',
            marks: [...el.querySelectorAll('.msc-check')].map(c => c.className.replace('msc-check ', '')),
            warnRow: !!el.querySelector('.msc-warn'),
            /* the nested <ul> must be gone: it became the checklist */
            leftoverNestedUl: !!el.querySelector('li ul')
          };
        }""")
        self.assertTrue(card["card"], "1.1 did not become a state card")
        self.assertEqual(card["badges"], ["权重深度调整", "成长小盘接近技术性熊市"])
        self.assertEqual(card["badgeClasses"], ["msc-badge t-deep", "msc-badge t-bear"])
        self.assertEqual(card["score"], "确认三要素 1/3")
        self.assertEqual(card["marks"], ["k-hit", "k-miss", "k-miss", "k-open"])
        self.assertTrue(card["warnRow"], "数据提示 row should be called out")
        self.assertFalse(card["leftoverNestedUl"])

    def test_drawdown_ladder_fits_its_canvas(self) -> None:
        """SVG does not clip, so an over-wide label spills outside the card
        instead of being cut. Assert the columns stay inside the viewBox and
        clear of each other — this caught a real 24px overflow."""
        geo = _eval(self.html, """() => {
          const svg = document.querySelector('.msc-ladder svg');
          if (!svg) return {svg: false};
          const box = el => { const b = el.getBBox(); return [b.x, b.x + b.width]; };
          const viewW = +svg.getAttribute('viewBox').split(' ')[2];
          const vals = [...svg.querySelectorAll('.msc-val')].map(box);
          const deltas = [...svg.querySelectorAll('.msc-delta')].map(box);
          const names = [...svg.querySelectorAll('.msc-name')].map(box);
          const bars = [...svg.querySelectorAll('rect')].map(r =>
            [+r.getAttribute('x'), +r.getAttribute('x') + +r.getAttribute('width')]);
          return {
            svg: true, viewW,
            rightMost: Math.max(...vals.map(v => v[1]), ...deltas.map(v => v[1])),
            valRight: Math.max(...vals.map(v => v[1])),
            deltaLeft: Math.min(...deltas.map(v => v[0])),
            nameRight: Math.max(...names.map(v => v[1])),
            barLeft: Math.min(...bars.map(b => b[0])),
            barCount: bars.length,
            barClasses: [...svg.querySelectorAll('rect')].map(r => r.getAttribute('class')),
            widths: bars.map(b => Math.round(b[1] - b[0])),
            groups: [...svg.querySelectorAll('.msc-group')].map(t => t.textContent)
          };
        }""")
        self.assertTrue(geo["svg"], "ladder svg missing")
        self.assertEqual(geo["barCount"], 6)
        self.assertLessEqual(geo["rightMost"], geo["viewW"], "value labels spill past the viewBox")
        self.assertLess(geo["valRight"], geo["deltaLeft"], "回撤 and 环比 columns overlap")
        self.assertLessEqual(geo["nameRight"], geo["barLeft"], "index names run into the bars")
        # 权重 first, then 成长小盘; within a group shallowest drawdown first
        self.assertEqual(geo["groups"], ["权重", "成长小盘"])
        self.assertEqual(geo["widths"], sorted(geo["widths"]))
        self.assertEqual(geo["barClasses"][-1], "msc-bar t-bear")

    def test_ladder_is_not_promised_when_evidence_lacks_market_state(self) -> None:
        """A missing evidence block is a data gap, not a render failure: the
        hook must not be declared at all, so the gate stays green and the skip
        is reported on stderr instead."""
        stripped = self.out_dir / "no_state"
        stripped.mkdir(exist_ok=True)
        for name in ("report_20260803.md", "kline_20260803.json"):
            shutil.copy(FIXTURES / name, stripped / name)
        shutil.copy(FIXTURES / "market_data.json", stripped / "market_data.json")
        evidence = json.loads((FIXTURES / "evidence_20260803_utf8.json").read_text(encoding="utf-8"))
        evidence.pop("market_state", None)
        (stripped / "evidence_20260803_utf8.json").write_text(
            json.dumps(evidence, ensure_ascii=False), encoding="utf-8")

        html = stripped / "report_20260803.html"
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "render_report_html.py"),
             "--input", str(stripped / "report_20260803.md"), "--output", str(html),
             "--market-data", str(stripped / "market_data.json"), "--no-lifecycle"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("跳过宽基回撤阶梯", result.stderr)
        self.assertNotIn("market-state.ladder", html.read_text(encoding="utf-8"))
        gate = _gate(html)
        self.assertEqual(gate["status"], "ok", _problem_text(gate))

    def test_chart_appended_to_document_end_is_caught(self) -> None:
        """Reproduce the 2026-08-03 failure: the hook still believes it worked,
        but the panel lands outside its section."""
        broken = self.out_dir / "broken_placement.html"
        text = self.html.read_text(encoding="utf-8")
        patched = text.replace(
            'const insertAfter = window.__sec.tail("sentiment_trend");',
            'const insertAfter = document.getElementById("report-body").lastElementChild;',
        )
        self.assertNotEqual(text, patched, "patch target not found — test needs updating")
        broken.write_text(patched, encoding="utf-8")

        result = _gate(broken)
        self.assertEqual(result["status"], "error")
        self.assertIn("outside section [sentiment_trend]", _problem_text(result))

    def test_missing_anchor_does_not_relocate_the_panel(self) -> None:
        """With the anchor gone, the hook must refuse rather than fall back."""
        broken = self.out_dir / "no_anchor.html"
        text = self.html.read_text(encoding="utf-8")
        broken.write_text(text.replace('data-sec="sentiment_trend"', 'data-sec="sentiment_trend_x"'), encoding="utf-8")

        result = _gate(broken)
        self.assertEqual(result["status"], "error")
        self.assertIn("refusing to relocate", _problem_text(result))

    def test_stripped_scripts_are_caught(self) -> None:
        """What Site externalisation or a CSP does: no script runs, so nothing
        reports an error. Absence of attestation is the failure."""
        broken = self.out_dir / "stripped.html"
        text = self.html.read_text(encoding="utf-8")
        broken.write_text(
            re.sub(r'<script(?! id="render-manifest").*?</script>', "", text, flags=re.DOTALL),
            encoding="utf-8",
        )

        result = _gate(broken)
        self.assertEqual(result["status"], "error")
        self.assertIn("data-render-status", _problem_text(result))

    def test_corrupt_chart_payload_is_not_swallowed(self) -> None:
        """A bad payload used to blank every chart while the page looked fine."""
        broken = self.out_dir / "bad_payload.html"
        text = self.html.read_text(encoding="utf-8")
        patched = re.sub(
            r'(<script id="chart-data" type="application/json">).*?(</script>)',
            r"\1{not json,,}\2", text, count=1, flags=re.DOTALL,
        )
        broken.write_text(patched, encoding="utf-8")

        result = _gate(broken)
        self.assertEqual(result["status"], "error")
        self.assertIn("chart-data", _problem_text(result))

    def test_http_404_asset_is_caught(self) -> None:
        """HTTP errors are responses, not Playwright requestfailed events."""
        broken = self.out_dir / "missing_asset.html"
        text = self.html.read_text(encoding="utf-8")
        broken.write_text(
            text.replace("</head>", '<link rel="stylesheet" href="missing.css">\n</head>'),
            encoding="utf-8",
        )

        with _serve(self.out_dir) as base_url:
            from html_report.render_check import check_browser

            result = check_browser(f"{base_url}/{broken.name}", 30000)
        self.assertEqual(result["status"], "error")
        self.assertIn("response: HTTP 404", _problem_text(result))

    def test_audit_file_records_the_verdict(self) -> None:
        from html_report.render_check import main

        out = self.out_dir / "audit.json"
        code = main(["--target", str(self.html), "--stage", "local", "--out", str(out)])
        self.assertEqual(code, 0)
        audit = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(audit["gate_pass"])
        self.assertEqual(audit["stage"], "local")
        self.assertEqual(audit["mode"], "browser")


if __name__ == "__main__":
    unittest.main(verbosity=2)
