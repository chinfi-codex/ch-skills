"""Build-time figures: pre-rendered HTML (typically inline SVG) spliced into the
report body before the page is written.

``ChartHook`` covers the other half of the chart story — charts that *draw
themselves at view time* via JS. That is the right tool when a chart needs the
DOM (tooltips, filtering, reading the rendered text). It is the wrong tool when
the chart must simply be visible: readers that block inline scripts (some
in-app viewers, WeChat/pushplus relays, print-to-PDF pipelines, plain email)
render the page with every hook silently skipped, leaving empty containers.

A ``StaticFigure`` has no such dependency: the markup is already in the file.
Use it for charts that carry the report's argument, and layer a ChartHook on
top only for optional interactivity.

Anchoring is by heading text, since that is the one stable landmark a Markdown
report offers:

- ``placement="before"``      insert immediately before the matched heading
- ``placement="after"``       insert immediately after the matched heading
- ``placement="section_end"`` insert at the end of the matched heading's
  section (just before the next heading of the same or higher level)

``match="last"`` picks the last heading matching an anchor instead of the
first. Anchor text is matched as a substring, so a section heading often
contains its own subsection's name ("待跟踪 & 本期变更" contains "本期变更");
``last`` is how a figure targets the inner one.

Figures are wrapped in ``<section class="report-figure">``. The tag matters:
``HeroDecoration`` walks forward from its summary heading and *removes* nodes
it does not collect, but stops at SECTION/ASIDE/BLOCKQUOTE — so a figure placed
inside a hero-managed section survives as a ``<section>`` and would be deleted
as a ``<div>``.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import List, Sequence

_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

PLACEMENTS = ("before", "after", "section_end")

FIGURE_CSS = """
.report-figure { margin: 26px 0; }
.report-figure .rf-card {
  background: var(--card);
  border: 1px solid var(--line-1);
  border-radius: 14px;
  padding: 16px 18px 12px;
}
.report-figure .rf-title { font-size: 15px; font-weight: 650; color: var(--ink-1); }
.report-figure .rf-sub { font-size: 12px; color: var(--ink-4); margin-top: 2px; }
.report-figure svg { display: block; width: 100%; height: auto; margin-top: 10px; }
.report-figure .rf-cap { font-size: 12px; color: var(--ink-3); margin-top: 8px; line-height: 1.7; }
.report-figure .rf-cap b { color: var(--neg); }
.report-figure .rf-legend {
  display: flex; flex-wrap: wrap; gap: 6px 16px;
  font-size: 11.5px; color: var(--ink-3);
  margin-top: 10px; padding-top: 9px; border-top: 1px solid var(--line-2);
}
.report-figure .rf-legend span { display: inline-flex; align-items: center; gap: 6px; }
.report-figure .rf-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.report-figure .rf-keys {
  display: grid; grid-template-columns: 1fr 1fr; gap: 0 22px;
  margin-top: 10px; padding-top: 9px; border-top: 1px solid var(--line-2); font-size: 12.5px;
}
.report-figure .rf-keys > div {
  display: flex; gap: 8px; align-items: baseline;
  padding: 3px 0; border-bottom: 1px dotted var(--line-2);
}
.report-figure .rf-no {
  flex: 0 0 19px; height: 19px; border-radius: 50%; color: #fff;
  font-size: 10.5px; font-weight: 650;
  display: inline-flex; align-items: center; justify-content: center;
}
.report-figure .rf-nm { flex: 1; color: var(--ink-1); }
.report-figure .rf-mt { color: var(--ink-4); font-size: 11px; white-space: nowrap; }
@media (max-width: 720px) { .report-figure .rf-keys { grid-template-columns: 1fr; } }
"""


@dataclass
class StaticFigure:
    """One block of pre-rendered HTML and where it belongs in the report."""

    html: str
    anchor: Sequence[str] = field(default_factory=tuple)
    placement: str = "section_end"
    match: str = "first"
    title: str = ""
    subtitle: str = ""
    caption: str = ""
    css: str = ""

    def __post_init__(self) -> None:
        if self.placement not in PLACEMENTS:
            raise ValueError(f"unknown placement {self.placement!r}; use one of {PLACEMENTS}")
        if self.match not in ("first", "last"):
            raise ValueError(f"unknown match {self.match!r}; use 'first' or 'last'")

    def block_html(self) -> str:
        head = ""
        if self.title:
            head += f'<div class="rf-title">{html.escape(self.title)}</div>'
        if self.subtitle:
            head += f'<div class="rf-sub">{html.escape(self.subtitle)}</div>'
        cap = f'<div class="rf-cap">{self.caption}</div>' if self.caption else ""
        return (
            '<section class="report-figure">'
            f'<div class="rf-card">{head}{self.html}{cap}</div>'
            "</section>"
        )


def _heading_text(inner: str) -> str:
    return html.unescape(_TAG_RE.sub("", inner)).strip()


def _insert_at(body: str, index: int, markup: str) -> str:
    return body[:index] + markup + body[index:]


def insert_figures(body_html: str, figures: Sequence[StaticFigure]) -> str:
    """Splice each figure into *body_html* at its anchor.

    Offsets shift as figures are inserted, so headings are re-scanned per
    figure. A figure whose anchor is not found is appended at the end rather
    than dropped — a missing heading should not silently lose a chart.
    """
    out = body_html
    for fig in figures:
        markup = fig.block_html()
        headings = [
            (m.start(), m.end(), int(m.group(1)), _heading_text(m.group(2)))
            for m in _HEADING_RE.finditer(out)
        ]
        target = None
        for wanted in fig.anchor:
            hits = [i for i, (_s, _e, _lvl, txt) in enumerate(headings) if wanted in txt]
            if hits:
                target = hits[-1] if fig.match == "last" else hits[0]
                break
        if target is None:
            out += markup
            continue
        start, end, level, _txt = headings[target]
        if fig.placement == "before":
            out = _insert_at(out, start, markup)
        elif fig.placement == "after":
            out = _insert_at(out, end, markup)
        else:  # section_end
            nxt = next(
                (s for s, _e, lvl, _t in headings[target + 1:] if lvl <= level),
                None,
            )
            out = _insert_at(out, nxt, markup) if nxt is not None else out + markup
    return out


def collect_css(figures: Sequence[StaticFigure]) -> List[str]:
    """Base figure CSS plus any per-figure additions, de-duplicated in order."""
    if not figures:
        return []
    blocks = [FIGURE_CSS]
    for fig in figures:
        if fig.css.strip() and fig.css not in blocks:
            blocks.append(fig.css)
    return blocks
