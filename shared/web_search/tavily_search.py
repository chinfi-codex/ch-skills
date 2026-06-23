#!/usr/bin/env python3
"""Tavily web-search helper —— 原子联网能力（脚本只取数，不下结论）。

从环境变量或就近的 .env 读 TAVILY_API_KEY，调用 Tavily Search API，把结构化
结果以 JSON 打到 stdout。脚本不调 LLM、不做相关性判断——查什么、怎么读结果
由模型决定。

为什么需要它：在部分网络下，Claude 的 WebFetch 会先做域名安全预检（连
claude.ai），WebSearch 也可能直接返回 0 结果——请求根本没碰到目标站，所以这
不是目标站反爬，而是 claude.ai 安全预检 / WebSearch 服务不可达。Tavily 是直连
第三方检索 API，绕开这一层，因此作为需要催化 / 新闻核查的 skill 的联网主路径
（见 CLAUDE.md 的 TAVILY_API_KEY 一行）。

用法：
    python tavily_search.py "ALAB stock news 2026-06-18" --topic news --days 7
    python tavily_search.py "Credo CRDO catalyst" --max-results 8 --search-depth advanced
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TAVILY_ENDPOINT = "https://api.tavily.com/search"


def _read_key_from_env_file(path: Path) -> str:
    """从单个 .env 文件里读 TAVILY_API_KEY（容错，读不到返回空串）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                if key.strip() == "TAVILY_API_KEY":
                    return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def get_tavily_key() -> str:
    """TAVILY_API_KEY：优先环境变量，否则就近的 .env（cwd 或脚本上溯的仓库根）。"""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if key:
        return key

    candidates = [Path.cwd() / ".env"]
    here = Path(__file__).resolve()
    candidates += [parent / ".env" for parent in list(here.parents)[:6]]

    seen: set[Path] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if cand.exists():
            found = _read_key_from_env_file(cand)
            if found:
                return found
    return ""


def tavily_search(
    query: str,
    *,
    max_results: int = 5,
    topic: str = "general",
    days: int | None = None,
    search_depth: str = "basic",
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    key: str | None = None,
) -> dict:
    """调用 Tavily Search，返回精简后的结构化 dict；任何失败都回结构化 error。"""
    try:
        import requests  # 本地导入，缺依赖时给清晰报错
    except ImportError:
        return {
            "query": query,
            "error": "missing_dependency",
            "detail": "pip install requests",
        }

    api_key = key or get_tavily_key()
    if not api_key:
        return {
            "query": query,
            "error": "missing_api_key",
            "detail": "Set TAVILY_API_KEY in the environment or repo-root .env.",
        }

    payload: dict = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "topic": topic,
    }
    if days is not None:
        payload["days"] = days
    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains

    try:
        resp = requests.post(TAVILY_ENDPOINT, json=payload, timeout=30)
    except requests.RequestException as exc:
        return {"query": query, "error": "request_failed", "detail": str(exc)}

    if resp.status_code != 200:
        return {
            "query": query,
            "error": f"http_{resp.status_code}",
            "detail": resp.text[:500],
        }

    data = resp.json()
    results = [
        {
            "title": item.get("title"),
            "url": item.get("url"),
            "published_date": item.get("published_date"),
            "score": item.get("score"),
            "content": item.get("content"),
        }
        for item in data.get("results", [])
    ]
    return {
        "query": query,
        "topic": topic,
        "count": len(results),
        "results": results,
    }


def _split_domains(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [d.strip() for d in raw.split(",") if d.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tavily web search -> JSON（脚本只取数，模型判断）。"
    )
    parser.add_argument("query", help="检索词")
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument(
        "--topic",
        choices=["general", "news"],
        default="general",
        help="催化/异动核查用 'news'，可配 --days 控制时效窗口",
    )
    parser.add_argument(
        "--days", type=int, default=None, help="时效窗口（天，仅 news topic 生效）"
    )
    parser.add_argument(
        "--search-depth", choices=["basic", "advanced"], default="basic"
    )
    parser.add_argument(
        "--include-domains", default=None, help="逗号分隔，限定来源域名"
    )
    parser.add_argument(
        "--exclude-domains", default=None, help="逗号分隔，排除来源域名"
    )
    args = parser.parse_args(argv)

    result = tavily_search(
        args.query,
        max_results=args.max_results,
        topic=args.topic,
        days=args.days,
        search_depth=args.search_depth,
        include_domains=_split_domains(args.include_domains),
        exclude_domains=_split_domains(args.exclude_domains),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
