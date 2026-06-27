#!/usr/bin/env python3
"""Load ch-news-reporter report profile metadata from config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_PROFILE_CONFIG = Path("config/report_profiles.yaml")
SOURCE_ALIASES = {
    "github": "github_trending",
    "ph": "product_hunt",
    "hn": "hacker_news",
}


def load_profiles(config_path: Path | str = DEFAULT_PROFILE_CONFIG) -> dict[str, dict[str, Any]]:
    path = Path(config_path)
    if not path.exists():
        raise SystemExit(f"Profile config does not exist: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profiles = payload.get("profiles") or {}
    if not isinstance(profiles, dict):
        raise SystemExit(f"Profile config {path} has no valid 'profiles' map.")
    return {
        str(name): profile
        for name, profile in profiles.items()
        if isinstance(profile, dict)
    }


def load_profile(
    profile_name: str, config_path: Path | str = DEFAULT_PROFILE_CONFIG
) -> dict[str, Any]:
    profiles = load_profiles(config_path)
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        available = ", ".join(sorted(profiles)) or "(none)"
        raise SystemExit(f"Unknown profile '{profile_name}'. Available: {available}")
    return {**profile, "_name": profile_name}


def normalize_source(source: str) -> str:
    return SOURCE_ALIASES.get(source, source)


def profile_sources(profile: dict[str, Any]) -> list[str]:
    sources = profile.get("sources") or []
    return [normalize_source(str(source)) for source in sources]


def reference_dir(profile_name: str, profile: dict[str, Any] | None = None) -> str:
    if profile and profile.get("reference_dir"):
        return str(profile["reference_dir"])
    return profile_name


def open_budget(profile: dict[str, Any]) -> int | None:
    value = profile.get("open_budget")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SystemExit(f"profile {profile.get('_name') or '?'} open_budget must be an integer")


def rss_categories(profile: dict[str, Any]) -> set[str]:
    configured = profile.get("rss_categories") or []
    if not isinstance(configured, list):
        raise SystemExit(f"profile {profile.get('_name') or '?'} rss_categories must be a list")
    return {str(category) for category in configured if str(category).strip()}


def render_config(profile: dict[str, Any]) -> dict[str, Any]:
    render = profile.get("render") or {}
    if not isinstance(render, dict):
        raise SystemExit(f"profile {profile.get('_name') or '?'} render must be a map")
    return render
