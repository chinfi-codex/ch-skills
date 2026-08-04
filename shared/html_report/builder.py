"""Compose a self-contained HTML report.

Architecture:
- One shared HTML shell (head + page wrapper + doc-head + report section).
- One CSS theme picked by name (``default`` / ``print``); skills may layer
  additional CSS via ``extra_css``.
- One built-in UI-decoration IIFE: hides standalone ``---`` paragraphs,
  rotates h2 accent hue, colorizes numeric / labelled table cells.
- Pluggable per-skill ChartHooks: each contributes a JSON payload (merged
  under its ``name`` into a single ``<script id="chart-data">``) plus a JS
  body that runs inside an IIFE; the JS reads its slice via
  ``window.__chartData[name]``.
- Optional skill-supplied UI decoration JS (for hero cards that vary per
  report kind).
- The final HTML is run through ``validate_text_preserved``; failure raises
  ``RuntimeError`` (callers convert to warning if they want a non-strict
  CLI).
- Optionally, a ``SectionContract``: headings are resolved to stable
  ``data-sec`` keys at build time (missing anchor → build fails), a render
  manifest of declared hook expectations is embedded, and the page attests at
  view time to what it actually drew. See ``contract.py`` / ``attestation.py``.
  Pages built without a contract behave exactly as before.
"""

from __future__ import annotations

import hashlib
import html
from datetime import datetime
from importlib import resources
from typing import Dict, List, Optional, Sequence

from .attestation import (
    ATTEST_RUNTIME_JS,
    FINALIZE_RUNTIME_JS,
    HookExpectation,
    RenderManifest,
    manifest_script,
)
from .chart_hook import ChartHook
from .chartkit import chartkit_js
from .contract import SECTION_RUNTIME_JS, MatchedSection, SectionContract
from .figures import StaticFigure, collect_css, insert_figures
from .markdown_engine import render_markdown
from .safe_json import safe_json_for_script
from .text_validator import validate_text_preserved


_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700'
    '&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">'
)


_BASE_UI_JS = """\
(function () {
  const root = document.getElementById("report-body");
  if (!root) return;

  /* hide standalone "---" separator paragraphs */
  root.querySelectorAll("p").forEach(p => {
    const txt = p.textContent.trim();
    if (/^-{3,}$/.test(txt)) { p.style.display = "none"; p.setAttribute("aria-hidden", "true"); }
  });

  /* rotate h2 accent hue across the six theme colors */
  let h2Idx = 0;
  root.querySelectorAll("h2").forEach(h => { h.setAttribute("data-idx", String(h2Idx % 6)); h2Idx += 1; });

  /* colorize numeric and star-rating table cells (domain-specific pill rules
     live in each skill's add_ui_decoration block) */
  root.querySelectorAll("td").forEach(td => {
    const trimmed = td.textContent.trim();
    if (!trimmed || td.children.length > 0) return;
    if (/^[★☆]+$/.test(trimmed)) {
      const filled = (trimmed.match(/★/g) || []).length;
      const total = Math.max(filled, 3);
      let h = "";
      for (let i = 0; i < total; i++) {
        h += i < filled ? '<span class="stars">★</span>' : '<span class="stars dim">★</span>';
      }
      td.innerHTML = h;
      return;
    }
    const signed = trimmed.match(/^([+\\-])(\\d[\\d,]*\\.?\\d*)\\s*(%|pct|x|倍|亿|万亿|分位)?$/);
    if (signed) {
      td.innerHTML = `<span class="${signed[1] === "+" ? "num-pos" : "num-neg"}">${trimmed}</span>`;
      return;
    }
    if (/[+\\-]\\d/.test(trimmed)) {
      td.innerHTML = td.innerHTML.replace(/([+\\-])(\\d+(?:[\\.,]\\d+)?)(%|pct|倍|x)?/g,
        (_, sign, num, unit) => `<span class="${sign === "+" ? "num-pos" : "num-neg"}">${sign}${num}${unit || ""}</span>`);
    }
  });

  /* expose the chart-data envelope under window.__chartData for ChartHook JS.
     A parse failure here blanks *every* chart on the page while leaving it
     looking perfectly normal, so it is reported rather than swallowed. */
  const dataEl = document.getElementById("chart-data");
  if (!dataEl) {
    if (window.__render) window.__render.fail("chart-data", "chart-data script block is missing from the page");
    window.__chartData = {};
    return;
  }
  try { window.__chartData = JSON.parse(dataEl.textContent || "{}"); }
  catch (e) {
    window.__chartData = {};
    if (window.__render) {
      window.__render.fail("chart-data", "chart-data payload is not valid JSON — every hook will draw nothing", String(e));
    }
  }
})();
"""


def _load_theme_css(theme: str) -> str:
    safe = theme.replace("/", "_").replace("\\", "_")
    pkg = resources.files(__package__) / "themes" / f"{safe}.css"
    if not pkg.is_file():
        available = ", ".join(list_themes())
        raise ValueError(f"unknown theme {theme!r}; available: {available}")
    return pkg.read_text(encoding="utf-8")


def list_themes() -> List[str]:
    themes_dir = resources.files(__package__) / "themes"
    return sorted(
        p.name[:-4]
        for p in themes_dir.iterdir()
        if p.is_file() and p.name.endswith(".css") and not p.name.startswith("_")
    )


class HtmlReportBuilder:
    def __init__(
        self,
        title: str,
        theme: str = "default",
        meta_text: str = "",
        extra_css: str = "",
        extra_head: str = "",
        lang: str = "zh-CN",
        font_links: bool = True,
        contract: Optional[SectionContract] = None,
        build_id: str = "",
    ) -> None:
        self.title = title
        self.theme = theme
        self.meta_text = meta_text
        self.extra_css = extra_css
        self.extra_head = extra_head
        self.lang = lang
        # Themes that use system fonts (claude=system+Georgia, print=serif) can drop the
        # remote Google Fonts <link>s for a truly offline, self-contained page.
        self.font_links = font_links
        self.contract = contract
        self.build_id = build_id or datetime.now().strftime("%Y%m%dT%H%M%S")
        self._theme_css = _load_theme_css(theme)
        self._hooks: List[ChartHook] = []
        self._figures: List[StaticFigure] = []
        self._ui_decorations: List[str] = []
        self._decoration_css: List[str] = []
        self._expectations: List[HookExpectation] = []
        self._sections: Dict[str, MatchedSection] = {}

    def add_chart_hook(
        self, hook: ChartHook, *, expects: Sequence[HookExpectation] = ()
    ) -> None:
        """Attach a chart bundle, optionally declaring what it must produce.

        A hook that draws into several sections (one grid per feature group,
        say) declares one expectation per insertion point and attests under the
        matching name; see :meth:`declare`.
        """
        self._hooks.append(hook)
        if expects:
            self.declare(*expects)

    def declare(self, *expectations: HookExpectation) -> None:
        """Register render expectations without adding a hook.

        Every declared expectation must name a section the contract knows
        about — a typo here would otherwise turn into a permanently red gate
        that no report can satisfy.
        """
        if self.contract is None:
            raise ValueError("declare() needs a SectionContract; pass contract= to the builder")
        known = {spec.key for spec in self.contract.sections}
        for item in expectations:
            if item.target_sec not in known:
                raise ValueError(
                    f"expectation {item.name!r} targets unknown section {item.target_sec!r}; "
                    f"contract {self.contract.version} defines {sorted(known)}"
                )
            self._expectations.append(item)

    def add_figure(self, figure: StaticFigure) -> None:
        """Splice a pre-rendered figure into the body at build time.

        Unlike :meth:`add_chart_hook`, the markup lands in the file itself, so
        the chart still shows in viewers that block inline scripts.
        """
        self._figures.append(figure)

    def add_ui_decoration(self, js: str) -> None:
        """Append a skill-specific UI decoration IIFE that runs after the
        built-in one (e.g. promote a particular heading into a hero card).
        The supplied snippet should be a complete ``(function () { ... })();``
        block or equivalent; it is inserted verbatim inside ``<script>`` tags.
        """
        self._ui_decorations.append(js)

    def add_decoration(self, decoration: object) -> None:
        """Append a typed decoration (e.g. ``PillDecoration`` / ``HeroDecoration``)
        or a raw JS string. Typed decorations expose a ``.js`` property; this is
        sugar over :meth:`add_ui_decoration`. Decorations that also expose a
        ``.css`` property (e.g. ``TimelineDecoration``) get their styles merged
        into the page <style> block.
        """
        js = getattr(decoration, "js", decoration)
        self._ui_decorations.append(js)
        css = getattr(decoration, "css", "")
        if isinstance(css, str) and css.strip():
            self._decoration_css.append(css)

    def render(self, markdown_text: str, *, validate: bool = True) -> str:
        body_html = render_markdown(markdown_text)
        manifest_block = ""
        runtime_block = ""
        finalize_block = ""
        if self.contract is not None:
            # Raises ContractError when a required section is missing, ambiguous
            # or at the wrong level — before a single byte of HTML is written.
            body_html, self._sections = self.contract.stamp(body_html)
            manifest = RenderManifest(
                contract_version=self.contract.version,
                build_id=self._build_fingerprint(markdown_text),
                sections=[spec.key for spec in self.contract.sections],
                hooks=self._expectations,
            ).filtered_for(list(self._sections))
            manifest_block = manifest_script(manifest) + "\n"
            runtime_block = (
                f"<script>{ATTEST_RUNTIME_JS}</script>\n"
                f"<script>{SECTION_RUNTIME_JS}</script>\n"
            )
            finalize_block = f"<script>{FINALIZE_RUNTIME_JS}</script>\n"
        if self._figures:
            body_html = insert_figures(body_html, self._figures)
        chart_envelope = {hook.name: hook.payload for hook in self._hooks}
        chart_json = safe_json_for_script(chart_envelope)
        hook_scripts = "\n".join(self._hook_script(hook) for hook in self._hooks)
        ui_extras = "\n".join(
            f"<script>\n{self._guarded(js, f'decoration#{idx}')}\n</script>"
            for idx, js in enumerate(self._ui_decorations)
        )
        # The chart kit (window.CK) is only needed when hooks draw charts; emit it
        # once, after the base UI script and before any hook IIFE runs.
        chartkit_block = f"<script>\n{chartkit_js()}</script>\n" if self._hooks else ""

        escaped_title = html.escape(self.title)
        doc_head_html = ""
        if self.meta_text:
            meta_html = html.escape(self.meta_text)
            doc_head_html = (
                '<div class="doc-head">\n'
                f'      <span class="dh-title">{escaped_title}</span>\n'
                f'      <span class="dh-meta">{meta_html}</span>\n'
                '    </div>\n    '
            )

        font_block = _FONT_LINKS if self.font_links else ""
        out = f"""<!doctype html>
<html lang="{self.lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  {font_block}
  <style>
{self._theme_css}
{self.extra_css}
{chr(10).join(self._decoration_css + collect_css(self._figures))}
  </style>
  {self.extra_head}
</head>
<body>
  <main class="page">
    {doc_head_html}<section class="section report" id="report-body">
      {body_html}
    </section>
  </main>
  <script id="chart-data" type="application/json">{chart_json}</script>
  {manifest_block}{runtime_block}<script>
{_BASE_UI_JS}  </script>
{chartkit_block}{ui_extras}
{hook_scripts}
{finalize_block}</body>
</html>
"""
        if validate:
            validate_text_preserved(markdown_text, out)
        return out

    @property
    def generated_at(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @property
    def sections(self) -> Dict[str, MatchedSection]:
        """Sections resolved by the last :meth:`render` call."""
        return dict(self._sections)

    def _build_fingerprint(self, markdown_text: str) -> str:
        digest = hashlib.sha256(markdown_text.encode("utf-8")).hexdigest()[:8]
        return f"{self.build_id}-{digest}"

    def _hook_script(self, hook: ChartHook) -> str:
        body = (
            f'  const __payload = (window.__chartData || {{}})[{hook.name!r}] || {{}};\n'
            f"{hook.js}\n"
        )
        return f"<script>\n{self._guarded(body, f'hook:{hook.name}', wrap_fn=True)}\n</script>"

    @staticmethod
    def _guarded(js: str, scope: str, *, wrap_fn: bool = False) -> str:
        """Run *js* so a throw is reported instead of silently ending the block.

        Without this, one bad hook leaves an empty space on an otherwise
        normal-looking page and nothing anywhere says so. ``wrap_fn`` keeps the
        IIFE that hook bodies rely on (they use bare ``return`` to bail out).
        """
        scope_literal = repr(scope)
        handler = (
            "  } catch (__err) {\n"
            f"    if (window.__render) window.__render.fail({scope_literal}, "
            'String((__err && __err.message) || __err), '
            "__err && __err.stack ? String(__err.stack).slice(0, 800) : null);\n"
            f"    else console.error('[render]', {scope_literal}, __err);\n"
            "  }\n"
        )
        if wrap_fn:
            return "(function () {\n  try {\n" + js + handler + "})();"
        return "try {\n" + js + "\n" + handler
