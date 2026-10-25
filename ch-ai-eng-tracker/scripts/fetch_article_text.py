#!/usr/bin/env python3
"""通用网页正文抓取：任意文章 URL → 标题 + 发布时间 + 正文文本。

"本周实战精读"的取全文入口（公众号文章请用 fetch_wechat_articles.py content，
它认微信页面结构）。提取策略：og:title / <title> 取标题；article:published_time
取时间；<article> 或 <main> 内的 <p> 拼正文，太短时回退整容器文本或 og:description。

返回 JSON：
{"script","generated_at","items":[{url,ok,title,published,text,chars,method,note}]}

用法：
    python scripts/fetch_article_text.py --url "https://openai.com/blog/xxx"
    python scripts/fetch_article_text.py --url URL1 --url URL2 --max-chars 12000

凭据：无。部分站点反爬（403/需 JS），失败的 URL 带 note 继续；全部失败 exit 1。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    HttpError,
    emit_error,
    http_get_text,
    iso_shanghai,
    make_session,
    now_shanghai,
    write_or_emit,
)

MIN_TEXT_CHARS = 200


def extract_article(html: str) -> dict:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: dict = {"title": None, "published": None, "text": None, "method": None}

    og_title = soup.find("meta", attrs={"property": "og:title"})
    out["title"] = (og_title.get("content") if og_title else None) or (
        soup.title.get_text(strip=True) if soup.title else None
    )
    published = (
        soup.find("meta", attrs={"property": "article:published_time"})
        or soup.find("meta", attrs={"name": "published_time"})
        or soup.find("time")
    )
    out["published"] = (published.get("content") if published.has_attr("content") else published.get_text(strip=True)) if published else None

    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "aside", "noscript", "form"]):
        tag.decompose()

    container = soup.find("article") or soup.find("main") or soup.body
    text = ""
    if container:
        paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
        text = "\n".join(p for p in paragraphs if p)
        method = "article_p" if soup.find("article") else ("main_p" if soup.find("main") else "body_p")
        if len(text) < MIN_TEXT_CHARS:
            full = container.get_text("\n", strip=True)
            if len(full) > len(text):
                text, method = full, f"{method}+full"
    if len(text) < MIN_TEXT_CHARS:
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        if og_desc and og_desc.get("content"):
            text, method = f"{text}\n[og:description] {og_desc['content']}".strip(), "og_description"
    out["text"], out["method"] = text or None, method
    return out


def extract_jina(text: str) -> dict:
    """r.jina.ai 返回格式：Title: … / URL Source: … / Markdown Content: …"""
    out: dict = {"title": None, "published": None, "text": None, "method": "jina_reader"}
    for line in text.splitlines():
        if line.startswith("Title: ") and not out["title"]:
            out["title"] = line[len("Title: "):].strip()
        if line.startswith("Markdown Content:"):
            body = text.split(line, 1)[1].strip()
            break
    else:
        body = text
    out["text"] = body or None
    return out


def fetch_via_jina(session, url: str, timeout: int) -> str:
    return http_get_text(session, f"https://r.jina.ai/{url}", timeout=timeout, headers={"Accept": "text/plain"})


def main() -> int:
    parser = argparse.ArgumentParser(description="通用网页正文抓取（精读用）")
    parser.add_argument("--url", action="append", required=True, help="文章链接，可重复")
    parser.add_argument("--max-chars", type=int, default=10000, help="正文截断长度")
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    session = make_session()
    items: list[dict] = []
    for url in args.url:
        item: dict = {"url": url, "ok": False, "title": None, "published": None, "text": None, "method": None, "note": None}
        try:
            try:
                html = http_get_text(session, url, timeout=args.timeout)
                item.update(extract_article(html))
            except HttpError as exc:
                # 直连被拒（Cloudflare 403 等）时走 r.jina.ai 阅读器降级
                try:
                    item.update(extract_jina(fetch_via_jina(session, url, args.timeout)))
                except Exception as jina_exc:  # noqa: BLE001
                    item["note"] = f"fetch failed: {exc}; jina fallback failed: {jina_exc}"
            text = item.get("text") or ""
            if args.max_chars and len(text) > args.max_chars:
                text = text[: args.max_chars] + "…[截断]"
                item["method"] = f"{item.get('method') or 'unknown'}+truncated"
            item["text"] = text or None
            item["chars"] = len(text)
            item["ok"] = bool(text and len(text) >= MIN_TEXT_CHARS) and not item.get("note")
            if not item["ok"] and not item.get("note"):
                item["note"] = "正文过短或为空，可能是 JS 渲染页或反爬页"
        except Exception as exc:  # noqa: BLE001
            item["note"] = f"fetch failed: {exc}"
        items.append(item)
        time.sleep(0.5)

    if not any(item["ok"] for item in items):
        emit_error("CONTENT_FETCH_FAILED", items)

    payload = {
        "script": "fetch_article_text",
        "generated_at": iso_shanghai(now_shanghai()),
        "items": items,
    }
    write_or_emit(args, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
