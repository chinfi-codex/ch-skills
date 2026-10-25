#!/usr/bin/env python3
"""官方工程博客 RSS 抓取（主线：OpenAI Blog，源清单在 config/sources.yaml）。

只取窗口期内的原始条目，做不做工程筛选由模型决定。返回 JSON：
{"script","window","generated_at","items":[{title,url,published,source,summary}],"degraded":[...]}

用法：
    python scripts/fetch_official_blogs.py --days 7
    python scripts/fetch_official_blogs.py --days 14 --output reports/2026-10-24/raw/blogs.json

凭据：无。降级：单 feed 失败进 degraded 继续跑；全部失败 exit 1。
"""
from __future__ import annotations

import argparse
import calendar
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    add_common_args,
    emit_error,
    http_get_text,
    iso_shanghai,
    load_config,
    make_session,
    now_shanghai,
    strip_html,
    window_start,
    write_or_emit,
)


def parse_feed_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="官方工程博客 RSS 抓取（只返回原始条目）")
    add_common_args(parser)
    args = parser.parse_args()

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        emit_error("MISSING_CONFIG", str(exc), exit_code=2)
    feeds = [f for f in (config.get("official_blogs") or []) if f.get("enabled", True)]
    if not feeds:
        emit_error("NO_SOURCES", "config.sources.yaml official_blogs 为空", exit_code=2)

    try:
        import feedparser
    except ImportError:
        emit_error("MISSING_DEPENDENCY", "pip install feedparser", exit_code=2)

    start = window_start(args.days)
    session = make_session()
    items: list[dict] = []
    degraded: list[dict] = []

    for feed_cfg in feeds:
        name, url = str(feed_cfg.get("name") or feed_cfg["url"]), str(feed_cfg["url"])
        try:
            feed = feedparser.parse(http_get_text(session, url, timeout=args.timeout))
        except Exception as exc:  # noqa: BLE001
            degraded.append({"source": name, "error": str(exc)})
            continue
        count = 0
        for entry in feed.entries:
            published = parse_feed_time(entry)
            if not published or published < start:
                continue
            items.append(
                {
                    "title": str(entry.get("title") or "").strip(),
                    "url": str(entry.get("link") or ""),
                    "published": iso_shanghai(published),
                    "source": name,
                    "summary": strip_html(
                        str(entry.get("summary") or entry.get("description") or ""), cap=600
                    ),
                }
            )
            count += 1
        if count == 0:
            degraded.append({"source": name, "error": "feed_empty", "detail": f"窗口期({args.days}天)内 0 条"})

    if not items and not degraded:
        emit_error("NO_ITEMS", "所有 feed 窗口期内均为空")
    if not items and degraded:
        emit_error("ALL_FEEDS_FAILED", degraded)

    payload = {
        "script": "fetch_official_blogs",
        "generated_at": iso_shanghai(now_shanghai()),
        "window": {"days": args.days, "start": iso_shanghai(start)},
        "items": sorted(items, key=lambda x: x["published"], reverse=True),
        "degraded": degraded,
    }
    write_or_emit(args, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
