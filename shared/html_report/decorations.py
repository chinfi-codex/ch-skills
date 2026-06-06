"""Data-driven UI decorations.

A *decoration* is a small JS snippet the builder runs after rendering the
report body, to upgrade plain DOM into richer UI. Two patterns recurred
verbatim (mechanism-identical) across skills, differing only in their word
lists / heading text — so they live here as parameterised classes instead of
hand-written JS in each skill:

- ``PillDecoration``  — colour table cells whose text matches a rule into pills.
- ``HeroDecoration``  — promote a summary heading + its following blocks into a
  hero ``<aside class="summary-card">``.

Each exposes ``.js`` (a complete ``(function(){...})();`` block). Pass them to
``builder.add_decoration(...)``. For anything these can't express, the raw
escape hatch ``builder.add_ui_decoration(js_string)`` still works.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple


@dataclass
class PillDecoration:
    """Colour table cells into pills by regex.

    ``rules`` is a list of ``(pattern, css_class)``; the first pattern that
    fully matches a cell's trimmed text wins. Patterns are JS regex source
    (e.g. ``r"^(成长股|成熟龙头)$"``). Cells that already contain child
    elements are skipped, matching the original per-skill behaviour.
    """

    rules: Sequence[Tuple[str, str]]

    @property
    def js(self) -> str:
        rules_js = ",\n    ".join(
            f"{{ re: new RegExp({json.dumps(pat, ensure_ascii=False)}), cls: {json.dumps(cls)} }}"
            for pat, cls in self.rules
        )
        return (
            "(function () {\n"
            '  const root = document.getElementById("report-body");\n'
            "  if (!root) return;\n"
            "  const pillRules = [\n"
            f"    {rules_js}\n"
            "  ];\n"
            '  root.querySelectorAll("td").forEach(td => {\n'
            "    const trimmed = td.textContent.trim();\n"
            "    if (!trimmed || td.children.length > 0) return;\n"
            "    for (const rule of pillRules) {\n"
            "      if (rule.re.test(trimmed)) {\n"
            '        td.innerHTML = `<span class="${rule.cls}">${trimmed}</span>`;\n'
            "        return;\n"
            "      }\n"
            "    }\n"
            "  });\n"
            "})();"
        )


_HERO_TEMPLATE = r"""(function () {
  const root = document.getElementById("report-body");
  if (!root) return;
  const summaryH = Array.from(root.querySelectorAll("h2, h3")).find(
    h => h.textContent.trim().startsWith(__PREFIX__));
  if (!summaryH) return;

  const card = document.createElement("aside");
  card.className = "summary-card";
  const label = document.createElement("div");
  label.className = "summary-label";
  label.textContent = summaryH.textContent.trim();
  card.appendChild(label);

  const collectTags = __COLLECT__;
  const maxBlocks = __MAX__;
  const stopLevel = __STOPLEVEL__;
  const collected = [];
  let cur = summaryH.nextElementSibling;
  while (cur) {
    const headingMatch = /^H([1-6])$/.exec(cur.tagName);
    if (headingMatch && Number(headingMatch[1]) <= stopLevel) break;
    const next = cur.nextElementSibling;
    const txt = cur.textContent.trim();
__NUMBERED_BLOCK__
    if (maxBlocks != null && collected.length >= maxBlocks) break;
    if (collectTags.includes(cur.tagName) && txt && !/^-{3,}$/.test(txt)) {
      cur.classList.add("summary-body");
      collected.push(cur);
    } else {
      cur.remove();
    }
    cur = next;
  }
  collected.forEach(node => card.appendChild(node));
  summaryH.replaceWith(card);

  collected.forEach(p => {
    let h = p.innerHTML.replace(/([+\-])(\d+(?:\.\d+)?)(__UNITS__)/g,
      (_, sign, num, unit) => `<span class="${sign === "+" ? "num-pos" : "num-neg"}">${sign}${num}${unit}</span>`);
__KEYWORD_BLOCK__
    p.innerHTML = h;
  });
})();"""


@dataclass
class HeroDecoration:
    """Promote a summary heading into a hero ``<aside class="summary-card">``.

    Finds the first h2/h3 whose text starts with ``heading_prefix``, pulls the
    following blocks (limited to ``collect_tags``) into a card up to the next
    heading, drops separators/other nodes, and highlights signed numbers.

    Parameters reproduce the per-skill variants:
    - ``collect_tags``      tags to keep, e.g. ``("P", "UL")`` or ``("P",)``.
    - ``max_blocks``        cap on collected blocks (``None`` = no cap).
    - ``stop_at_numbered``  stop when a numbered-list paragraph begins.
    - ``number_units``      regex alternation of units to colour after numbers.
    - ``keyword_pattern``   optional regex alternation wrapped in ``.kw`` spans.
    - ``stop_mode``         where the section ends:
        ``"section"``     — stop at a heading whose level is same-or-higher than
                            the summary heading (so a deeper subheading *inside*
                            the summary keeps collecting; the original analyzer
                            behaviour).
        ``"any_heading"`` — stop at the next heading of any level (the original
                            market-sense behaviour, and the default).
    """

    heading_prefix: str
    collect_tags: Sequence[str] = ("P", "UL")
    max_blocks: Optional[int] = None
    stop_at_numbered: bool = False
    number_units: str = "%|pct|倍|x|分位"
    keyword_pattern: Optional[str] = None
    stop_mode: str = "any_heading"

    @property
    def js(self) -> str:
        collect = "[" + ", ".join(json.dumps(t.upper()) for t in self.collect_tags) + "]"
        numbered = (
            r"    if (txt && /^[^\dA-Za-z一-鿿]*\d+\./.test(txt)) break;"
            if self.stop_at_numbered
            else ""
        )
        keyword = (
            f"    h = h.replace(/({self.keyword_pattern})/g, '<span class=\"kw\">$1</span>');"
            if self.keyword_pattern
            else ""
        )
        if self.stop_mode == "section":
            stoplevel = "Number(summaryH.tagName.slice(1))"
        elif self.stop_mode == "any_heading":
            stoplevel = "6"
        else:
            raise ValueError(f"unknown stop_mode {self.stop_mode!r}; use 'section' or 'any_heading'")
        return (
            _HERO_TEMPLATE.replace("__PREFIX__", json.dumps(self.heading_prefix, ensure_ascii=False))
            .replace("__COLLECT__", collect)
            .replace("__MAX__", "null" if self.max_blocks is None else str(int(self.max_blocks)))
            .replace("__STOPLEVEL__", stoplevel)
            .replace("__NUMBERED_BLOCK__", numbered)
            .replace("__UNITS__", self.number_units)
            .replace("__KEYWORD_BLOCK__", keyword)
        )
