#!/usr/bin/env python3
"""Load a profile's analysis framework from its single-source framework.md.

The file ``references/reports/<profile>/framework.md`` is the single source of
truth for a profile's analysis framework.  It has two regions:

* a **machine region** — exactly one fenced ```yaml block — carrying the frame
  schema, the regime ``invalidation_triggers`` and the ``output_sections``
  skeleton.  Scripts parse ONLY this block.
* a **model region** — free-form prose (path judgement logic, sub-branches,
  watchlist, writing guidance) that only the model reads.

YAML frontmatter carries ``framework_version`` and ``regime_assumption``.

When a profile has no framework.md yet (mid-migration), :func:`load_framework`
returns ``None`` so callers fall back to the legacy ``report_profiles.yaml``
``state_schema`` — nothing breaks before every profile is migrated.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from profile_config import DEFAULT_PROFILE_CONFIG, load_profile, reference_dir

_SCRIPT_DIR = Path(__file__).resolve().parent
_REFERENCES = _SCRIPT_DIR.parent / "references" / "reports"

_YAML_BLOCK = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


def framework_path(
    profile: str,
    base: Optional[Path] = None,
    config_path: Path | str = DEFAULT_PROFILE_CONFIG,
) -> Path:
    """Path to a profile's framework.md (references/reports/<profile>/framework.md)."""
    root = base or _REFERENCES
    try:
        profile_config = load_profile(profile, config_path)
        ref_dir = reference_dir(profile, profile_config)
    except SystemExit:
        ref_dir = profile
    return root / ref_dir / "framework.md"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a leading ``--- ... ---`` block; return (frontmatter, body)."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                fm = yaml.safe_load(text[3:end]) or {}
            except yaml.YAMLError:
                fm = {}
            return (fm if isinstance(fm, dict) else {}), text[end + 4 :]
    return {}, text


def _first_yaml_block(body: str) -> dict[str, Any]:
    """Parse the first ```yaml fenced block (the machine region)."""
    match = _YAML_BLOCK.search(body)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def load_framework(
    profile: str,
    base: Optional[Path] = None,
    config_path: Path | str = DEFAULT_PROFILE_CONFIG,
) -> Optional[dict[str, Any]]:
    """Load a profile's framework, or ``None`` when framework.md is absent/unusable.

    Returns a dict with ``framework_version``, ``regime_assumption``, ``profile``,
    ``frame_schema`` (the machine region's ``frame``), ``invalidation_triggers``
    and ``output_sections``.  ``None`` tells callers to fall back to the legacy
    ``report_profiles.yaml`` ``state_schema``.
    """
    path = framework_path(profile, base, config_path)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    frontmatter, body = _split_frontmatter(text)
    machine = _first_yaml_block(body)
    frame_schema = machine.get("frame")
    if not isinstance(frame_schema, dict) or not frame_schema:
        # No usable machine region — treat as not-a-framework so caller falls back.
        return None
    return {
        "framework_version": frontmatter.get("framework_version"),
        "regime_assumption": frontmatter.get("regime_assumption"),
        "profile": frontmatter.get("profile") or profile,
        "frame_schema": frame_schema,
        "invalidation_triggers": machine.get("invalidation_triggers") or [],
        "output_sections": machine.get("output_sections") or [],
    }
