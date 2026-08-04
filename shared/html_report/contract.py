"""Section contract: the one place where a report's *display titles* are
allowed to matter.

The problem this solves: report Markdown is rewritten by the model every day,
so heading text drifts — a section gets renumbered (``1.1 情绪趋势`` becomes
``1.2 情绪趋势`` the day a new card is inserted above it), reworded, or moved.
Renderers that locate their anchors by scanning heading text (``includes("1.1")
&& includes("情绪趋势")``) silently miss, and whatever they meant to insert
lands somewhere else — or at the end of the document.

The fix is not "match harder". It is to do the fragile text match exactly
**once**, in Python, at build time, against a declared and versioned table —
and to fail loudly when it misses. Everything downstream (chart hooks, static
figures, UI decorations, the deploy gate) then addresses sections by a stable
key via ``[data-sec="..."]`` and never looks at heading text again.

Matching rules, in order:

- Heading text is compared with its leading section numbering stripped, so
  ``1.2 情绪趋势`` and ``2.4 情绪趋势`` both match the pattern ``情绪趋势``.
  A contract may still pin numbering explicitly by writing it into a pattern.
- A spec lists patterns in priority order. The first pattern with at least one
  hit decides the candidate set; a later pattern is only consulted when the
  earlier ones miss entirely. This mirrors ``StaticFigure.anchor`` semantics.
- One hit → stamped. Zero hits on a required spec → ``ContractError``. Two or
  more hits → ``ContractError`` (an ambiguous anchor is a contract bug, not
  something to resolve by coin flip).
- Two specs claiming the same heading → ``ContractError``.
- ``level`` pins the rendered heading level (2–4 after Markdown's h-shift), so
  demoting a section from ``##`` to ``###`` is caught too.

Failures raise at build time, before any HTML is written. That is the point:
a missing anchor should stop the pipeline while the report is still on disk,
not produce a plausible-looking page that has to be caught by eye.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

_HEADING_RE = re.compile(r"<h([1-6])\b([^>]*)>(.*?)</h\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
# "1.2 ", "3.3、", "5. " — leading section numbering. The number must be followed
# by whitespace or a "."/"、" separator, which is what keeps a title that merely
# starts with digits intact: "10:30 前涨停明细" is a time, not section 10, and an
# earlier form of this pattern turned it into "30 前涨停明细".
_NUMBERING_RE = re.compile(r"^\s*\d+(?:\.\d+)*(?:\s*[.、]\s*|\s+)")


class ContractError(RuntimeError):
    """A required section could not be resolved to exactly one heading."""


@dataclass(frozen=True)
class SectionSpec:
    """One addressable section of a report.

    ``key`` is the stable name the rest of the pipeline uses. ``patterns`` are
    regexes searched (not fullmatched) against the number-stripped heading
    text, most specific first.
    """

    key: str
    patterns: Sequence[str]
    required: bool = True
    level: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("SectionSpec.key must be non-empty")
        if not self.patterns:
            raise ValueError(f"SectionSpec {self.key!r} needs at least one pattern")
        if self.level is not None and not 2 <= self.level <= 4:
            raise ValueError(
                f"SectionSpec {self.key!r}: level must be 2-4 (Markdown h1 renders as h2)"
            )


@dataclass(frozen=True)
class MatchedSection:
    key: str
    level: int
    text: str


@dataclass
class SectionContract:
    """A versioned set of section specs for one report kind.

    Bump ``version`` whenever the report template's section structure changes;
    the value is embedded in the page and reported by the deploy gate, so a
    contract that silently drifted out of sync with the template is visible in
    the audit file rather than inferred.
    """

    version: str
    sections: Sequence[SectionSpec]

    def __post_init__(self) -> None:
        keys = [spec.key for spec in self.sections]
        duplicates = {key for key in keys if keys.count(key) > 1}
        if duplicates:
            raise ValueError(f"duplicate section keys in contract: {sorted(duplicates)}")

    @property
    def required_keys(self) -> List[str]:
        return [spec.key for spec in self.sections if spec.required]

    def stamp(self, body_html: str) -> Tuple[str, Dict[str, MatchedSection]]:
        """Resolve every spec against *body_html* and stamp ``data-sec``.

        Returns the rewritten body and the resolved map. Raises
        ``ContractError`` listing every problem at once — reporting them one
        per run turns a five-minute fix into five runs.
        """
        headings = _scan_headings(body_html)
        resolved: Dict[str, MatchedSection] = {}
        claimed: Dict[int, str] = {}
        problems: List[str] = []

        for spec in self.sections:
            hits = _match_spec(spec, headings)
            if not hits:
                if spec.required:
                    problems.append(
                        f"[{spec.key}] no heading matched {list(spec.patterns)}; "
                        f"headings present: {_heading_digest(headings)}"
                    )
                continue
            if len(hits) > 1:
                problems.append(
                    f"[{spec.key}] ambiguous — {len(hits)} headings matched "
                    f"{list(spec.patterns)}: {[headings[i].text for i in hits]}"
                )
                continue
            index = hits[0]
            head = headings[index]
            if spec.level is not None and head.level != spec.level:
                problems.append(
                    f"[{spec.key}] expected h{spec.level} but {head.text!r} rendered as h{head.level}"
                )
                continue
            if index in claimed:
                problems.append(
                    f"[{spec.key}] heading {head.text!r} is already claimed by [{claimed[index]}]"
                )
                continue
            claimed[index] = spec.key
            resolved[spec.key] = MatchedSection(key=spec.key, level=head.level, text=head.text)

        if problems:
            raise ContractError(
                f"section contract {self.version} failed:\n"
                + "\n".join(f"  - {line}" for line in problems)
            )

        return _apply_stamps(body_html, headings, claimed), resolved


@dataclass
class _Heading:
    start: int
    end: int
    attr_end: int
    level: int
    text: str
    stripped: str


def _scan_headings(body_html: str) -> List[_Heading]:
    out: List[_Heading] = []
    for match in _HEADING_RE.finditer(body_html):
        text = html.unescape(_TAG_RE.sub("", match.group(3))).strip()
        out.append(
            _Heading(
                start=match.start(),
                end=match.end(),
                # end of "<hN" + existing attrs, i.e. just before the closing ">"
                attr_end=match.start(1) + 1 + len(match.group(2)),
                level=int(match.group(1)),
                text=text,
                stripped=strip_numbering(text),
            )
        )
    return out


def strip_numbering(text: str) -> str:
    """Drop a leading ``1.2`` / ``3.3、`` style section number."""
    return _NUMBERING_RE.sub("", text).strip()


def _match_spec(spec: SectionSpec, headings: Sequence[_Heading]) -> List[int]:
    for pattern in spec.patterns:
        compiled = re.compile(pattern)
        hits = [
            idx
            for idx, head in enumerate(headings)
            if compiled.search(head.stripped) or compiled.search(head.text)
        ]
        if hits:
            return hits
    return []


def _heading_digest(headings: Sequence[_Heading]) -> List[str]:
    return [f"h{head.level}:{head.text}" for head in headings]


def _apply_stamps(
    body_html: str, headings: Sequence[_Heading], claimed: Dict[int, str]
) -> str:
    """Insert ``data-sec`` into each claimed heading, rightmost first so the
    earlier offsets stay valid."""
    out = body_html
    for index in sorted(claimed, reverse=True):
        head = headings[index]
        attr = f' data-sec="{html.escape(claimed[index], quote=True)}" data-sec-level="{head.level}"'
        out = out[: head.attr_end] + attr + out[head.attr_end :]
    return out


# --------------------------------------------------------------------------- #
# Runtime helper. Injected once per page; every hook uses it instead of
# scanning heading text, and the gate uses it to assert placement.
# --------------------------------------------------------------------------- #
SECTION_RUNTIME_JS = r"""
(function () {
  const root = document.getElementById("report-body");
  const api = {};

  /* The heading that owns a section key, or null. */
  api.head = function (key) {
    return root ? root.querySelector('[data-sec="' + key + '"]') : null;
  };

  /* The first heading at the same or a higher level after the section head —
     i.e. where this section stops. Null means "runs to the end of the body".
     Sections are not wrapped in a container element on purpose: wrapping would
     change nextElementSibling traversal for every existing decoration. */
  api.end = function (key) {
    const head = api.head(key);
    if (!head) return null;
    const level = Number(head.dataset.secLevel || head.tagName.slice(1));
    let cur = head.nextElementSibling;
    while (cur) {
      const m = /^H([1-6])$/.exec(cur.tagName || "");
      if (m && Number(m[1]) <= level) return cur;
      cur = cur.nextElementSibling;
    }
    return null;
  };

  /* Every top-level block belonging to the section, in document order. */
  api.blocks = function (key) {
    const head = api.head(key);
    if (!head) return [];
    const stop = api.end(key);
    const out = [];
    let cur = head.nextElementSibling;
    while (cur && cur !== stop) { out.push(cur); cur = cur.nextElementSibling; }
    return out;
  };

  /* The last top-level block of the section — the insertion point for anything
     that belongs at the section's end. Falls back to the heading itself when
     the section has no body yet. */
  api.tail = function (key) {
    const blocks = api.blocks(key);
    return blocks.length ? blocks[blocks.length - 1] : api.head(key);
  };

  /* First matching descendant inside the section (e.g. its table). */
  api.find = function (key, selector) {
    for (const block of api.blocks(key)) {
      if (block.matches && block.matches(selector)) return block;
      const hit = block.querySelector ? block.querySelector(selector) : null;
      if (hit) return hit;
    }
    return null;
  };

  /* Placement predicate used by hooks and by the deploy gate: is `el` inside
     the document range owned by `key`? */
  api.contains = function (key, el) {
    const head = api.head(key);
    if (!head || !el) return false;
    const after = head.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING;
    if (!after) return false;
    const stop = api.end(key);
    if (!stop) return true;
    return Boolean(stop.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING);
  };

  window.__sec = api;
})();
"""
