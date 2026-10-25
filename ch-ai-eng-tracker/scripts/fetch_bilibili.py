#!/usr/bin/env python3
"""B站 AI 工程视频抓取：官方搜索 API（自动 wbi 签名 + buvid）→ 搜索引擎降级。

主路径按关键词翻页取最新发布（order=pubdate），本地按窗口期过滤后按播放量排序。
官方接口 -412 风控或全关键词失败时，自动降级 multi_search（引擎序可配，
site 限定 bilibili.com）。无凭据可跑；BILIBILI_SESSDATA 可选（风控严重时提高通过率）。

返回 JSON：
{"script","window","generated_at","mode":"api|search_fallback","items":[...],"degraded":[...]}
api 条目：{title,url,author,pubdate,play,reviews,duration,keyword}
fallback 条目：{title,url,snippet,published_date,engine,source:"bilibili_fallback",keyword}

用法：
    python scripts/fetch_bilibili.py --days 7
    python scripts/fetch_bilibili.py --keyword "MCP 教程" --days 14
    python scripts/fetch_bilibili.py --no-fallback   # 只走官方 API，失败即报
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    add_common_args,
    emit_error,
    http_get_json,
    iso_shanghai,
    load_config,
    make_session,
    now_shanghai,
    search_via_multi_search,
    window_start,
    write_or_emit,
)

API = "https://api.bilibili.com"
EM_RE = re.compile(r"</?em[^>]*>")
# bilibili-API-collect 公开的 wbi 混淆表（社区逆向，长期稳定）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def wbi_mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def wbi_sign(params: dict, mixin_key: str) -> dict:
    import os

    params = dict(params, wts=int(time.time()))
    params = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in sorted(params.items())}
    query = urlencode(params, quote_via=quote)
    return {**params, "w_rid": hashlib.md5((query + mixin_key).encode()).hexdigest()}


def bootstrap(session) -> str | None:
    """拿 buvid3/buvid4 cookie + wbi keys；失败不致命（mixin_key=None 时退回未签名）。"""
    sessdata = os.getenv("BILIBILI_SESSDATA")
    if sessdata:
        session.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
    mixin_key = None
    try:
        spi = http_get_json(session, f"{API}/x/frontend/finger/spi", timeout=10)
        data = spi.get("data") or {}
        if data.get("b_3"):
            session.cookies.set("buvid3", data["b_3"], domain=".bilibili.com")
        if data.get("b_4"):
            session.cookies.set("buvid4", data["b_4"], domain=".bilibili.com")
    except Exception:  # noqa: BLE001 — spi 失败再试主页种 cookie
        try:
            session.get("https://www.bilibili.com/", timeout=10)
        except Exception:  # noqa: BLE001
            pass
    try:
        nav = http_get_json(session, f"{API}/x/web-interface/nav", timeout=10)
        wbi = (nav.get("data") or {}).get("wbi_img") or {}
        img_url, sub_url = wbi.get("img_url") or "", wbi.get("sub_url") or ""
        if img_url and sub_url:
            img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
            sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
            mixin_key = wbi_mixin_key(img_key, sub_key)
    except Exception:  # noqa: BLE001
        pass
    return mixin_key


def to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def search_keyword(session, keyword: str, pages: int, order: str, mixin_key: str | None, timeout: int) -> tuple[list[dict], str | None]:
    """单关键词翻页。返回 (videos, error)；error 为 None 表示成功。"""
    videos: list[dict] = []
    for page in range(1, pages + 1):
        params: dict = {"search_type": "video", "keyword": keyword, "page": page, "order": order}
        if mixin_key:
            params = wbi_sign(params, mixin_key)
        url = f"{API}/x/web-interface/search/type?{urlencode(params)}"
        headers = {"Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com"}
        try:
            payload = http_get_json(session, url, timeout=timeout, headers=headers)
        except Exception as exc:  # noqa: BLE001
            return videos, f"request failed: {exc}"
        code = payload.get("code")
        if code != 0:
            return videos, f"code={code} message={payload.get('message')}"
        result = (payload.get("data") or {}).get("result") or []
        for v in result:
            bvid = v.get("bvid")
            if not bvid:
                continue
            videos.append(
                {
                    "title": EM_RE.sub("", str(v.get("title") or "")).strip(),
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "author": v.get("author"),
                    "pubdate": v.get("pubdate"),
                    "play": to_int(v.get("play")),
                    "reviews": to_int(v.get("video_review")),
                    "duration": v.get("duration"),
                    "keyword": keyword,
                }
            )
        if len(result) < 20:  # 翻到底了
            break
        time.sleep(random.uniform(1.2, 2.2))  # 页间抖动，配合风控
    return videos, None


def fallback_search(keywords: list[str], days: int, engines: list[str], limit_per_keyword: int) -> tuple[list[dict], list[dict]]:
    items: list[dict] = []
    degraded: list[dict] = []
    for keyword in keywords:
        payload = search_via_multi_search(
            f"{keyword} 教程",
            engines=engines,
            max_results=limit_per_keyword,
            days=days,
            include_domains=["bilibili.com"],
        )
        if payload.get("error"):
            degraded.append({"source": f"fallback:{keyword}", "error": payload["error"]})
            continue
        for r in payload.get("results", []):
            url = r.get("url") or ""
            if "bilibili.com" not in url:
                continue
            items.append(
                {
                    "title": r.get("title"),
                    "url": url,
                    "snippet": r.get("snippet"),
                    "published_date": r.get("published_date"),
                    "engine": payload.get("engine"),
                    "source": "bilibili_fallback",
                    "keyword": keyword,
                }
            )
        time.sleep(0.8)
    return items, degraded


def main() -> int:
    parser = argparse.ArgumentParser(description="B站 AI 工程视频抓取（官方搜索 API + 搜索引擎降级）")
    add_common_args(parser, default_limit=40)
    parser.add_argument("--keyword", action="append", default=None, help="覆盖配置关键词，可重复")
    parser.add_argument("--pages", type=int, default=None, help="每关键词翻页数（覆盖配置）")
    parser.add_argument("--no-fallback", action="store_true", help="官方 API 失败时不降级搜索引擎")
    parser.add_argument("--fallback-engine", default=None, help="覆盖降级引擎序（如 tavily,deepseek）")
    args = parser.parse_args()

    try:
        cfg = load_config().get("bilibili") or {}
    except Exception as exc:  # noqa: BLE001
        emit_error("MISSING_CONFIG", str(exc), exit_code=2)

    keywords = args.keyword or [str(k) for k in (cfg.get("keywords") or [])]
    if not keywords:
        emit_error("NO_SOURCES", "bilibili.keywords 为空且未提供 --keyword", exit_code=2)
    pages = args.pages or int(cfg.get("pages_per_keyword") or 2)
    order = str(cfg.get("order") or "pubdate")

    session = make_session(referer="https://www.bilibili.com/")
    mixin_key = bootstrap(session)
    start = window_start(args.days)
    start_unix = int(start.timestamp())
    degraded: list[dict] = []

    api_items: list[dict] = []
    for keyword in keywords:
        videos, error = search_keyword(session, keyword, pages, order, mixin_key, args.timeout)
        if error:
            degraded.append({"source": f"api:{keyword}", "error": error})
        fresh = [v for v in videos if (v.get("pubdate") or 0) >= start_unix]
        fresh.sort(key=lambda v: v.get("play") or 0, reverse=True)
        api_items.extend(fresh[: args.limit])
        time.sleep(random.uniform(1.2, 2.2))

    # 关键词间可能重复命中同一条，按 url 去重后统一按播放量排序
    seen: set[str] = set()
    unique: list[dict] = []
    for v in api_items:
        if v["url"] in seen:
            continue
        seen.add(v["url"])
        v["pubdate"] = iso_shanghai(datetime.fromtimestamp(v["pubdate"], tz=timezone.utc))
        unique.append(v)
    api_items = sorted(unique, key=lambda v: v.get("play") or 0, reverse=True)[: args.limit]

    mode = "api"
    items = api_items
    if not items and not args.no_fallback:
        engines = [e.strip() for e in str(args.fallback_engine or cfg.get("fallback_engine") or "tavily").split(",") if e.strip()]
        fb_keywords = keywords[: int(cfg.get("fallback_keywords") or 3)]
        degraded.append(
            {"source": "bilibili_api", "error": "官方搜索无有效结果，降级搜索引擎", "engines": engines}
        )
        fb_items, fb_degraded = fallback_search(fb_keywords, args.days, engines, min(8, args.limit))
        degraded.extend(fb_degraded)
        if fb_items:
            mode = "search_fallback"
            items = fb_items[: args.limit]

    if not items:
        emit_error("BILIBILI_FETCH_FAILED", degraded or "官方 API 与降级路径均无结果")

    payload = {
        "script": "fetch_bilibili",
        "generated_at": iso_shanghai(now_shanghai()),
        "window": {"days": args.days, "start": iso_shanghai(start)},
        "mode": mode,
        "items": items,
        "degraded": degraded,
    }
    write_or_emit(args, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
