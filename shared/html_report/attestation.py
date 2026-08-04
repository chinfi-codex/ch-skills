"""Render attestation: the page proves it rendered correctly, instead of the
reader hoping it did.

Why a *positive* signal rather than error reporting. The obvious design is
"catch exceptions, set ``data-render-status=error``". That has a blind spot
which matters exactly where it hurts most: when a page is externalised to a
Site or served online, a CSP or a bundler can strip the inline ``<script>``
blocks wholesale. Then no hook runs, no exception is thrown, nothing writes an
error — and a checker that only looks for errors passes a page whose charts are
all missing. The same blind spot swallows a hook that returns early because its
anchor was not found.

So the contract is inverted. The build embeds a manifest of what *should*
happen; every hook checks in when it is done; a finalizer compares check-ins
against the manifest and only then writes ``data-render-status="ok"``. Silence
is failure, not success.

What each hook attests to:

- ``rendered``  — elements it actually put in the DOM.
- ``matched``   — inputs it resolved to drawable data.
- ``expected``  — inputs it was asked to draw (e.g. rows in the source table).
- ``unmatched`` — ``[{name, reason}]`` for every expected input it could not
  draw. ``reason`` must come from a closed set. This is the deliberate
  relaxation of "expected must equal rendered": a suspended stock or a fresh
  listing legitimately has no K-line, and a gate that red-lights on those every
  day is a gate people learn to bypass. Gaps are allowed; *unexplained* gaps
  are not.

``rendered == matched`` stays a hard equality — resolving data and then failing
to draw it is a plain bug with no legitimate cause.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# Every gap between "expected" and "matched" must name one of these. Adding a
# reason is a deliberate act: it widens what the gate tolerates, so it belongs
# in review rather than in a hook's inline string.
DEFAULT_UNMATCHED_REASONS = (
    "no_kline_data",      # the symbol has no price series in the payload
    "no_records_in_window",  # series exists but is empty for the displayed window
    "suspended",          # halted for the session
    "not_in_universe",    # outside the skill's coverage (e.g. BJ board)
    "name_ambiguous",     # display name did not resolve to exactly one code
    "no_payload",         # upstream evidence file was absent for this part
)


@dataclass(frozen=True)
class HookExpectation:
    """What one chart bundle promises to do, declared at build time."""

    name: str
    target_sec: str
    placement: str = "section_end"
    expect_count: Optional[int] = None
    expect_min: Optional[int] = None
    expect_from: str = ""
    note: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value not in ("", None)}


@dataclass
class RenderManifest:
    """The page's own definition of "rendered correctly".

    The gate reads expectations from here rather than hardcoding them, so a
    changed chart set updates the check in the same commit that changes the
    charts. A constant like "exactly 5 trend charts" living in a checker rots
    within a quarter and then gets commented out.
    """

    contract_version: str
    build_id: str
    sections: Sequence[str] = field(default_factory=tuple)
    hooks: Sequence[HookExpectation] = field(default_factory=tuple)
    unmatched_reasons: Sequence[str] = DEFAULT_UNMATCHED_REASONS

    def to_json(self) -> Dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "build_id": self.build_id,
            "sections": list(self.sections),
            "hooks": [hook.to_json() for hook in self.hooks],
            "unmatched_reasons": list(self.unmatched_reasons),
        }

    def filtered_for(self, resolved_sections: Sequence[str]) -> "RenderManifest":
        """Drop expectations whose target section is not in this report.

        Optional sections (a feature group with no hits today) legitimately
        vanish; their charts should vanish with them rather than red-light.
        """
        available = set(resolved_sections)
        return RenderManifest(
            contract_version=self.contract_version,
            build_id=self.build_id,
            sections=[key for key in self.sections if key in available],
            hooks=[hook for hook in self.hooks if hook.target_sec in available],
            unmatched_reasons=self.unmatched_reasons,
        )


def manifest_script(manifest: RenderManifest) -> str:
    payload = json.dumps(manifest.to_json(), ensure_ascii=False).replace("</", "<\\/")
    return f'<script id="render-manifest" type="application/json">{payload}</script>'


# --------------------------------------------------------------------------- #
# Runtime, part 1: the attestation API. Injected before any hook runs.
# --------------------------------------------------------------------------- #
ATTEST_RUNTIME_JS = r"""
(function () {
  const manifestEl = document.getElementById("render-manifest");
  let manifest = null;
  try { manifest = JSON.parse(manifestEl ? manifestEl.textContent : "null"); }
  catch (e) { manifest = null; }

  const api = {
    manifest: manifest,
    attestations: {},
    elements: {},
    errors: [],
    finalized: false
  };

  /* Record a failure. Anything that reaches here forces render-status=error. */
  api.fail = function (scope, message, detail) {
    api.errors.push({
      scope: String(scope || "unknown"),
      message: String(message || ""),
      detail: detail === undefined ? null : detail
    });
  };

  /* A hook reports what it did. `el` (or `els`) is the DOM node it inserted;
     it is kept out of the JSON payload and used for the placement assertion. */
  api.attest = function (name, result) {
    const r = result || {};
    const els = r.els || (r.el ? [r.el] : []);
    api.elements[name] = els;
    api.attestations[name] = {
      rendered: Number(r.rendered != null ? r.rendered : els.length),
      matched: Number(r.matched != null ? r.matched : (r.rendered != null ? r.rendered : els.length)),
      expected: r.expected == null ? null : Number(r.expected),
      unmatched: Array.isArray(r.unmatched) ? r.unmatched : [],
      note: r.note || ""
    };
  };

  window.__render = api;
})();
"""


# --------------------------------------------------------------------------- #
# Runtime, part 2: the finalizer. Injected after every hook.
# --------------------------------------------------------------------------- #
FINALIZE_RUNTIME_JS = r"""
(function () {
  const api = window.__render;
  if (!api) return;

  function finalize() {
    const manifest = api.manifest;
    const problems = api.errors.slice();
    const detail = { hooks: {}, sections: {} };

    function fail(scope, message, extra) {
      problems.push({ scope: scope, message: message, detail: extra === undefined ? null : extra });
    }

    if (!manifest) {
      fail("manifest", "render-manifest missing or unparseable — the page cannot prove what it should contain");
    } else {
      const reasons = manifest.unmatched_reasons || [];

      (manifest.sections || []).forEach(function (key) {
        const present = Boolean(window.__sec && window.__sec.head(key));
        detail.sections[key] = present;
        if (!present) fail("section:" + key, "section anchor [data-sec=\"" + key + "\"] is not in the DOM");
      });

      (manifest.hooks || []).forEach(function (spec) {
        const att = api.attestations[spec.name];
        if (!att) {
          /* The blind spot this whole mechanism exists for: no check-in at all
             (script stripped, hook returned early, exception before attest). */
          fail("hook:" + spec.name, "hook did not check in — it never ran, returned early, or its script was stripped");
          detail.hooks[spec.name] = { checked_in: false };
          return;
        }
        const row = {
          checked_in: true,
          rendered: att.rendered,
          matched: att.matched,
          expected: att.expected,
          unmatched: att.unmatched,
          placed_in: spec.target_sec
        };
        detail.hooks[spec.name] = row;

        if (att.rendered !== att.matched) {
          fail("hook:" + spec.name, "rendered (" + att.rendered + ") != matched (" + att.matched + ") — data resolved but not drawn");
        }
        if (spec.expect_count != null && att.rendered !== spec.expect_count) {
          fail("hook:" + spec.name, "expected exactly " + spec.expect_count + " rendered, got " + att.rendered);
        }
        if (spec.expect_min != null && att.rendered < spec.expect_min) {
          fail("hook:" + spec.name, "expected at least " + spec.expect_min + " rendered, got " + att.rendered);
        }
        if (att.expected != null && att.expected !== att.matched + att.unmatched.length) {
          fail("hook:" + spec.name, "unmatched ledger does not close: expected " + att.expected +
               " != matched " + att.matched + " + unmatched " + att.unmatched.length);
        }
        att.unmatched.forEach(function (item) {
          const reason = item && item.reason;
          if (!reason) {
            fail("hook:" + spec.name, "unmatched entry without a reason", item);
          } else if (reasons.length && reasons.indexOf(reason) < 0) {
            fail("hook:" + spec.name, "unmatched reason \"" + reason + "\" is outside the declared set", item);
          }
        });

        /* Placement: the inserted node must live inside its declared section.
           A chart appended to the end of the document fails right here. */
        const els = api.elements[spec.name] || [];
        if (!els.length) {
          if (att.rendered > 0) fail("hook:" + spec.name, "attested " + att.rendered + " rendered but reported no element for placement");
        } else if (window.__sec) {
          els.forEach(function (el, idx) {
            if (!el || !el.isConnected) {
              fail("hook:" + spec.name, "rendered element #" + idx + " is not attached to the document");
            } else if (!window.__sec.contains(spec.target_sec, el)) {
              fail("hook:" + spec.name, "rendered element #" + idx + " is outside section [" + spec.target_sec + "]");
            }
          });
        }
      });
    }

    const status = problems.length ? "error" : "ok";
    document.documentElement.setAttribute("data-render-status", status);
    document.documentElement.setAttribute("data-render-contract", manifest ? manifest.contract_version : "unknown");
    const report = {
      status: status,
      contract_version: manifest ? manifest.contract_version : null,
      build_id: manifest ? manifest.build_id : null,
      checked_at: new Date().toISOString(),
      problems: problems,
      detail: detail
    };
    let node = document.getElementById("render-report");
    if (!node) {
      node = document.createElement("script");
      node.id = "render-report";
      node.type = "application/json";
      document.body.appendChild(node);
    }
    node.textContent = JSON.stringify(report);
    api.finalized = true;
    api.report = report;
    return report;
  }

  api.finalize = finalize;
  try {
    finalize();
  } catch (e) {
    document.documentElement.setAttribute("data-render-status", "error");
    const node = document.createElement("script");
    node.id = "render-report";
    node.type = "application/json";
    node.textContent = JSON.stringify({
      status: "error",
      problems: [{ scope: "finalizer", message: String((e && e.message) || e), detail: null }]
    });
    document.body.appendChild(node);
  }
})();
"""
