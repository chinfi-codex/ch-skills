#!/usr/bin/env python3
"""Hacker News 高分 AI/工程讨论抓取（Algolia API，免费无凭据）。

按窗口期 + 最低分数拉 story，本地按 AI 工程关键词过滤（词表在
config/sources.yaml hackernews.keywords）。反映海外工程社区本周关注点。

返回 JSON：
{"script","window","generated_at","items":[{title,url,points,num_comments,author,created_at,hn_url,matched_keywords}],"degraded":[]}

用法：
    python scripts/fetch_hackernews.py --days 7
    python scripts/fetch_hackernews.py --days 7 --min-points 100

凭据：无。降级：单页失败进 degraded；全失败 exit 1。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    add_common_args,
    emit_error,
    http_get_json,
    iso_shanghai,
    keyword_match,
    load_config,
    make_session,
    now_shanghai,
    window_start,
    write_or_emit,
)

ALGOLIA = "https://hn.algolia.com/api/v1/search_by_date"


def main() -> int:
    parser = argparse.ArgumentParser(description="HN 高分 AI/工程讨论（Algolia API）")
    add_common_args(parser, default_limit=60)
    parser.add_argument("--min-points", type=int, default=None, help="最低分数（覆盖配置）")
    args = parser.parse_args()

    try:
        cfg = load_config().get("hackernews") or {}
    except Exception as exc:  # noqa: BLE001
        emit_error("MISSING_CONFIG", str(exc), exit_code=2)

    min_points = args.min_points if args.min_points is not None else int(cfg.get("min_points") or 50)
    hits_per_page = int(cfg.get("hits_per_page") or 200)
    pages = int(cfg.get("pages") or 2)
    keywords = [str(k) for k in (cfg.get("keywords") or [])]
    start_unix = int(window_start(args.days).timestamp())

    session = make_session()
    items: list[dict] = []
    degraded: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(pages):
        query = urlencode(
            {
                "tags": "story",
                "hitsPerPage": hits_per_page,
                "page": page,
                "numericFilters": f"points>{min_points},created_at_i>{start_unix}",
            }
        )
        try:
            data = http_get_json(session, f"{ALGOLIA}?{query}", timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            degraded.append({"source": f"algolia:page{page}", "error": str(exc)})
            break
        hits = data.get("hits") or []
        for hit in hits:
            object_id = hit.get("objectID")
            if not object_id or object_id in seen_ids:
                continue
            seen_ids.add(object_id)
            title = str(hit.get("title") or hit.get("story_title") or "").strip()
            url = hit.get("url") or hit.get("story_url") or ""
            matched = keyword_match(f"{title} {url}", keywords) if keywords else []
            if not matched:
                continue
            items.append(
                {
                    "title": title,
                    "url": url or None,
                    "points": hit.get("points"),
                    "num_comments": hit.get("num_comments"),
                    "author": hit.get("author"),
                    "created_at": hit.get("created_at"),
                    "hn_url": f"https://news.ycombinator.com/item?id={object_id}",
                    "matched_keywords": matched,
                }
            )
        if len(hits) < hits_per_page:
            break

    if not items and degraded:
        emit_error("ALGOLIA_FAILED", degraded)
    if not items:
        emit_error("NO_ITEMS", f"窗口期内无 ≥{min_points} 分的 AI/工程相关 story（可降 --min-points 或扩 --days）")

    items.sort(key=lambda x: x.get("points") or 0, reverse=True)
    items = items[: args.limit]
    payload = {
        "script": "fetch_hackernews",
        "generated_at": iso_shanghai(now_shanghai()),
        "window": {"days": args.days, "min_points": min_points},
        "items": items,
        "degraded": degraded,
    }
    write_or_emit(args, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
