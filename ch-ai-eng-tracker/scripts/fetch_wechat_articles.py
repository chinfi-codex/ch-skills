#!/usr/bin/env python3
"""大厂技术公众号文章检索与正文抓取（两个模式，共用一个入口）。

search 模式：按"账号名 × 检索词"走共享 multi_search（site 限定 mp.weixin.qq.com，
failover 引擎序可配）。公众号无官方 API，按账号名检索是当前唯一可落地方案，
检索结果里同名文章可能混入其他号，模型按 site_name/正文核对归属。

content 模式：给 mp.weixin.qq.com 文章 URL，抓正文（div#js_content 优先，
og:description 兜底），供"本周实战精读"读全文用。

返回 JSON：
- search：{"script","window","generated_at","mode":"search","items":[{title,url,snippet,published_date,account,query,engine}],"degraded":[...]}
- content：{"script","generated_at","mode":"content","items":[{url,ok,title,account,publish_time,text,chars,note}]}

用法：
    python scripts/fetch_wechat_articles.py search --days 7
    python scripts/fetch_wechat_articles.py search --account 腾讯技术工程 --days 7
    python scripts/fetch_wechat_articles.py content --url "https://mp.weixin.qq.com/s/xxxx"

凭据：TENCENTCLOUD_SECRET_ID/KEY（元宝）优先；缺则按 failover 落到 TAVILY_API_KEY、
DEEPSEEK_API_KEY；全缺时报 MISSING_CREDENTIALS 语义的 all_engines_failed。
"""
from __future__ import annotations

import argparse
import sys
import time
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
    search_via_multi_search,
    write_or_emit,
)

DOMAIN = "mp.weixin.qq.com"
VERIFY_WALL_MARKS = ("环境异常", "完成验证后即可进行访问", "当前环境异常")


def run_search(args) -> None:
    try:
        cfg = load_config().get("wechat") or {}
    except Exception as exc:  # noqa: BLE001
        emit_error("MISSING_CONFIG", str(exc), exit_code=2)

    accounts = cfg.get("accounts") or []
    if args.account:
        wanted = set(args.account)
        accounts = [a for a in accounts if a.get("name") in wanted]
        wanted_missing = wanted - {a.get("name") for a in accounts}
        if wanted_missing:
            print(f"warn: 配置中不存在的账号 {sorted(wanted_missing)}，已忽略", file=sys.stderr)
    if not accounts:
        emit_error("NO_SOURCES", "wechat.accounts 为空且未提供 --account", exit_code=2)

    engines = [e.strip() for e in str(args.fallback_engine or cfg.get("fallback_engine") or "tavily").split(",") if e.strip()]
    session = make_session()
    items: list[dict] = []
    degraded: list[dict] = []
    seen_urls: set[str] = set()

    for account_cfg in accounts:
        account = str(account_cfg.get("name"))
        queries = args.query or [str(q) for q in (account_cfg.get("queries") or ["大模型"])]
        for q in queries:
            payload = search_via_multi_search(
                f"{account} {q}",
                engines=engines,
                max_results=args.max_results,
                days=args.days,
                include_domains=[DOMAIN],
            )
            if payload.get("error"):
                degraded.append({"source": f"{account}:{q}", "error": payload["error"]})
            else:
                added = 0
                for r in payload.get("results", []):
                    url = str(r.get("url") or "")
                    if DOMAIN not in url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    items.append(
                        {
                            "title": r.get("title"),
                            "url": url,
                            "snippet": r.get("snippet"),
                            "published_date": r.get("published_date"),
                            "site_name": r.get("site_name"),
                            "account": account,
                            "query": q,
                            "engine": payload.get("engine"),
                        }
                    )
                    added += 1
                if added == 0:
                    degraded.append({"source": f"{account}:{q}", "error": "0 results", "engine": payload.get("engine")})
            time.sleep(0.8)

    if not items and degraded:
        emit_error("ALL_ENGINES_FAILED", degraded)
    if not items:
        emit_error("NO_ITEMS", "窗口期内未检索到公众号文章（可放宽 --days 或换 --query）")

    payload = {
        "script": "fetch_wechat_articles",
        "generated_at": iso_shanghai(now_shanghai()),
        "window": {"days": args.days},
        "mode": "search",
        "items": items,
        "degraded": degraded,
    }
    write_or_emit(args, payload)


def extract_wechat_content(html: str) -> dict:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    result: dict = {}

    title = soup.find("meta", attrs={"property": "og:title"})
    result["title"] = (title.get("content") if title else None) or (
        soup.find(id="activity-name").get_text(strip=True) if soup.find(id="activity-name") else None
    )
    account = soup.find(id="js_name") or soup.find("strong", class_="profile_nickname")
    result["account"] = account.get_text(strip=True) if account else None
    publish_time = soup.find(id="publish_time")
    result["publish_time"] = publish_time.get_text(strip=True) if publish_time else None

    content_node = soup.find(id="js_content") or soup.find(id="js_article")
    if content_node:
        for tag in content_node.find_all(["script", "style"]):
            tag.decompose()
        text = content_node.get_text("\n", strip=True)
    else:
        text = ""
    if len(text) < 200:
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        fallback = og_desc.get("content") if og_desc else ""
        text = f"{text}\n[og:description] {fallback}".strip() if fallback else text
    result["text"] = text
    for mark in VERIFY_WALL_MARKS:
        if mark in html[:4000]:
            result["note"] = "verify_wall：命中微信验证墙，正文不可用"
            break
    return result


def run_content(args) -> None:
    session = make_session(referer="https://mp.weixin.qq.com/")
    items: list[dict] = []
    for url in args.url:
        item: dict = {"url": url, "ok": False, "title": None, "account": None, "publish_time": None, "text": None, "note": None}
        if DOMAIN not in url:
            item["note"] = "非 mp.weixin.qq.com 链接，请用 fetch_article_text.py"
            items.append(item)
            continue
        try:
            html = http_get_text(session, url, timeout=args.timeout)
            extracted = extract_wechat_content(html)
            item.update(extracted)
            text = item.get("text") or ""
            if args.max_chars and len(text) > args.max_chars:
                text = text[: args.max_chars] + "…[截断]"
            item["text"] = text or None
            item["chars"] = len(text)
            item["ok"] = bool(text) and not item.get("note")
        except Exception as exc:  # noqa: BLE001
            item["note"] = f"fetch failed: {exc}"
        items.append(item)
        time.sleep(0.8)

    if not any(item["ok"] for item in items):
        emit_error("CONTENT_FETCH_FAILED", items)

    payload = {
        "script": "fetch_wechat_articles",
        "generated_at": iso_shanghai(now_shanghai()),
        "mode": "content",
        "items": items,
    }
    write_or_emit(args, payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="大厂技术公众号：search 检索 / content 正文抓取")
    sub = parser.add_subparsers(dest="mode")

    p_search = sub.add_parser("search", help="按账号名 × 检索词搜公众号文章")
    add_common_args(p_search, default_days=7)
    p_search.add_argument("--account", action="append", default=None, help="只搜这些账号（覆盖配置全集），可重复")
    p_search.add_argument("--query", action="append", default=None, help="覆盖各账号默认检索词，可重复")
    p_search.add_argument("--max-results", type=int, default=8, help="每组合取回条数")
    p_search.add_argument("--fallback-engine", default=None, help="覆盖 failover 引擎序")

    p_content = sub.add_parser("content", help="抓公众号文章正文（供精读）")
    p_content.add_argument("--url", action="append", required=True, help="mp.weixin.qq.com 文章链接，可重复")
    p_content.add_argument("--max-chars", type=int, default=8000, help="正文截断长度")
    p_content.add_argument("--timeout", type=int, default=20)
    p_content.add_argument("--output", default=None)

    args = parser.parse_args()
    if args.mode == "content":
        run_content(args)
    elif args.mode == "search":
        run_search(args)
    else:
        parser.print_help(sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
