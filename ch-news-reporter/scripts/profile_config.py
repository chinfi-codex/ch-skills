#!/usr/bin/env python3
"""Load ch-news-reporter report profile metadata from config.

除固定 profile(config/report_profiles.yaml)外,还加载自定义关注主题
(config/custom_topics.yaml):主题 slug 映射为 profile 名 custom_<slug>,
字段对齐固定 profile 结构,使 prepare_report_data / save_report_state
可以无感复用(见 docs/custom-topics-design.md D4)。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PROFILE_CONFIG = Path("config/report_profiles.yaml")
CUSTOM_TOPICS_CONFIG = Path("config/custom_topics.yaml")
CUSTOM_PROFILE_PREFIX = "custom_"
CUSTOM_REFERENCE_DIR = "custom_topic"
# 固定 profile 名单:自定义主题 slug 与之冲突即报错(D4)
FIXED_PROFILE_NAMES = {"ai_daily", "macro_daily", "geopolitical_daily"}
TOPIC_STATUS = {"active", "paused", "archived"}
TOPIC_FREQUENCY = {"daily", "weekly"}
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
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
    profile_name: str,
    config_path: Path | str = DEFAULT_PROFILE_CONFIG,
    custom_config_path: Path | str = CUSTOM_TOPICS_CONFIG,
) -> dict[str, Any]:
    if is_custom_profile(profile_name):
        return load_custom_profile(
            custom_profile_slug(profile_name), custom_config_path
        )
    profiles = load_profiles(config_path)
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        available = ", ".join(sorted(profiles)) or "(none)"
        raise SystemExit(f"Unknown profile '{profile_name}'. Available: {available}")
    return {**profile, "_name": profile_name}


# ---------------------------------------------------------------------------
# 自定义关注主题(config/custom_topics.yaml)
# ---------------------------------------------------------------------------
def is_custom_profile(profile_name: str) -> bool:
    return str(profile_name).startswith(CUSTOM_PROFILE_PREFIX)


def custom_profile_slug(profile_name: str) -> str:
    return str(profile_name)[len(CUSTOM_PROFILE_PREFIX):]


def custom_profile_name(slug: str) -> str:
    return f"{CUSTOM_PROFILE_PREFIX}{slug}"


def _validate_slug(slug: str) -> None:
    """slug 形态与冲突校验(D4):冲突即报错退出。"""
    if not _SLUG_RE.match(slug):
        raise SystemExit(
            f"自定义主题 slug {slug!r} 非法:须为小写字母开头的 snake_case"
        )
    if slug in FIXED_PROFILE_NAMES:
        raise SystemExit(
            f"自定义主题 slug {slug!r} 与固定 profile 冲突,请改名"
            f"(固定 profile: {sorted(FIXED_PROFILE_NAMES)})"
        )
    if slug.startswith(CUSTOM_PROFILE_PREFIX):
        raise SystemExit(
            f"自定义主题 slug {slug!r} 不应以 {CUSTOM_PROFILE_PREFIX!r} 开头"
            "(profile 名会自动加此前缀)"
        )


def _validate_topic(slug: str, topic: Any) -> dict[str, Any]:
    """主题配置结构校验;返回原 dict(校验失败直接报错退出)。"""
    if not isinstance(topic, dict):
        raise SystemExit(f"custom_topics.yaml 主题 {slug!r} 必须是 map")
    status = str(topic.get("status") or "active")
    if status not in TOPIC_STATUS:
        raise SystemExit(
            f"主题 {slug!r} status={status!r} 非法(允许: {sorted(TOPIC_STATUS)})"
        )
    frequency = str(topic.get("frequency") or "daily")
    if frequency not in TOPIC_FREQUENCY:
        raise SystemExit(
            f"主题 {slug!r} frequency={frequency!r} 非法(允许: {sorted(TOPIC_FREQUENCY)})"
        )
    try:
        days = int(topic.get("time_window_days", 3))
    except (TypeError, ValueError):
        raise SystemExit(f"主题 {slug!r} time_window_days 必须是整数") from None
    if days < 1:
        raise SystemExit(f"主题 {slug!r} time_window_days 必须 >= 1")
    try:
        budget = int(topic.get("open_budget", 5))
    except (TypeError, ValueError):
        raise SystemExit(f"主题 {slug!r} open_budget 必须是整数") from None
    if budget < 0:
        raise SystemExit(f"主题 {slug!r} open_budget 必须 >= 0")
    for field in ("keywords", "exclude_keywords"):
        value = topic.get(field)
        if value is not None and not isinstance(value, list):
            raise SystemExit(f"主题 {slug!r} {field} 必须是 list")
    channels = topic.get("channels")
    if channels is not None and not isinstance(channels, dict):
        raise SystemExit(f"主题 {slug!r} channels 必须是 map")
    return topic


def load_custom_topics(
    config_path: Path | str = CUSTOM_TOPICS_CONFIG,
) -> dict[str, Any]:
    """加载 custom_topics.yaml,返回 {"settings": ..., "topics": {slug: cfg}}。

    文件不存在或结构非法时 SystemExit;所有 slug 先做冲突校验(D4)。
    """
    path = Path(config_path)
    if not path.exists():
        raise SystemExit(f"Custom topics config does not exist: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise SystemExit(f"Custom topics config {path} 必须是 map")
    settings = payload.get("settings") or {}
    if not isinstance(settings, dict):
        raise SystemExit(f"Custom topics config {path} 的 settings 必须是 map")
    topics = payload.get("topics") or {}
    if not isinstance(topics, dict):
        raise SystemExit(f"Custom topics config {path} 的 topics 必须是 map")
    normalized: dict[str, dict[str, Any]] = {}
    for slug, topic in topics.items():
        slug = str(slug)
        _validate_slug(slug)
        normalized[slug] = _validate_topic(slug, topic)
    return {"settings": settings, "topics": normalized}


def topic_open_budget(topic: dict[str, Any]) -> int:
    """主题 open 跟踪项预算;0 = 显式关闭跨天状态(O3)。"""
    try:
        value = int(topic.get("open_budget", 5))
    except (TypeError, ValueError):
        raise SystemExit(f"主题 open_budget 必须是整数: {topic.get('open_budget')!r}")
    if value < 0:
        raise SystemExit(f"主题 open_budget 必须 >= 0: {value}")
    return value


def topic_state_enabled(topic: dict[str, Any]) -> bool:
    """有效 state_enabled:state_enabled false 或 open_budget 0 都视为关闭。"""
    return bool(topic.get("state_enabled", True)) and topic_open_budget(topic) != 0


def topic_to_profile(
    slug: str, topic: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    """把主题配置映射成固定 profile 同构的 dict,供管线无感复用。

    custom 主题没有固定 sources 池与 state_schema(证据来自多通道检索、
    frame 自由结构),这些字段给空默认值;主题原始配置挂在 topic_config
    与 custom_settings 上供检索层使用。
    """
    return {
        "title": topic.get("title") or slug,
        "reference_dir": CUSTOM_REFERENCE_DIR,
        "timezone": topic.get("timezone") or "Asia/Shanghai",
        "sources": [],
        "state_enabled": topic_state_enabled(topic),
        "open_budget": topic_open_budget(topic),
        "custom_topic": True,
        "topic_slug": slug,
        "topic_config": topic,
        "custom_settings": settings,
    }


def load_custom_profile(
    slug: str, config_path: Path | str = CUSTOM_TOPICS_CONFIG
) -> dict[str, Any]:
    """按 slug 加载单个自定义主题,返回 profile 同构 dict(_name=custom_<slug>)。"""
    payload = load_custom_topics(config_path)
    topics = payload["topics"]
    topic = topics.get(slug)
    if topic is None:
        available = ", ".join(sorted(topics)) or "(none)"
        raise SystemExit(
            f"Unknown custom topic '{slug}'. Available: {available} "
            f"(config: {Path(config_path)})"
        )
    profile = topic_to_profile(slug, topic, payload["settings"])
    profile["_name"] = custom_profile_name(slug)
    return profile


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
