#!/usr/bin/env python3
"""DeepSeek web-search helper —— 原子联网能力（脚本只取数，不下结论）。

从环境变量或就近的 .env 读 DEEPSEEK_API_KEY，调用 DeepSeek Search API
（POST https://api.deepseek.com/v1/tools/search，DeepSeek 自研搜索引擎），
把结构化结果以 JSON 打到 stdout。脚本不调 LLM、不做相关性判断——查什么、
怎么读结果由模型决定。

与 tavily_search.py 同一契约：成功 {"query", "results"}，失败
{"query", "error": {code, detail}}。Tavily 偏海外源，DeepSeek 中文源与
时效性强，两者互为备份与交叉验证（选型见同目录 README.md）。

用法：
    python deepseek_search.py "ALAB stock news" --count 8
    python deepseek_search.py "寒武纪 业绩预告" --need-content
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/v1/tools/search"
# 与官方文档一致的硬边界：超限直接本地报错，不打无效请求
QUERY_MAX_CHARS = 128
COUNT_MIN, COUNT_MAX = 1, 50


def _read_keys_from_env_file(path: Path, names: set[str]) -> dict[str, str]:
    """从单个 .env 文件里读指定 key（容错，读不到的名字不出现）。"""
    found: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                key = key.strip()
                if key in names and key not in found:
                    found[key] = value.strip().strip('"').strip("'")
    except OSError:
        return found
    return found


def _keys_from_env_files(names: set[str]) -> dict[str, str]:
    """就近找 .env：先 cwd，再脚本上溯最多 6 层；先找到的值优先。"""
    candidates = [Path.cwd() / ".env"]
    here = Path(__file__).resolve()
    candidates += [parent / ".env" for parent in list(here.parents)[:6]]

    merged: dict[str, str] = {}
    seen: set[Path] = set()
    for cand in candidates:
        if cand in seen or not cand.exists():
            continue
        seen.add(cand)
        for name, value in _read_keys_from_env_file(cand, names).items():
            merged.setdefault(name, value)
    return merged


def get_deepseek_key() -> str:
    """DEEPSEEK_API_KEY：优先环境变量，否则就近的 .env。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    return _keys_from_env_files({"DEEPSEEK_API_KEY"}).get("DEEPSEEK_API_KEY", "")


def deepseek_search(
    query: str,
    *,
    count: int = 10,
    need_content: bool = False,
    ori_question: str | None = None,
    localization: str | None = None,
    key: str | None = None,
) -> dict:
    """调用 DeepSeek Search，返回精简后的结构化 dict；任何失败都回结构化 error。"""
    if not query.strip():
        return {
            "query": query,
            "error": {"code": "invalid_parameters", "detail": "query cannot be blank"},
        }
    if len(query) > QUERY_MAX_CHARS:
        return {
            "query": query,
            "error": {
                "code": "invalid_parameters",
                "detail": f"query exceeds {QUERY_MAX_CHARS} unicode chars (got {len(query)})",
            },
        }
    if not COUNT_MIN <= count <= COUNT_MAX:
        return {
            "query": query,
            "error": {
                "code": "invalid_parameters",
                "detail": f"count must be within {COUNT_MIN}..{COUNT_MAX} (got {count})",
            },
        }
    if ori_question is not None and len(ori_question) > QUERY_MAX_CHARS:
        return {
            "query": query,
            "error": {
                "code": "invalid_parameters",
                "detail": f"ori_question exceeds {QUERY_MAX_CHARS} unicode chars",
            },
        }

    try:
        import requests  # 本地导入，缺依赖时给清晰报错
    except ImportError:
        return {
            "query": query,
            "error": {"code": "missing_dependency", "detail": "pip install requests"},
        }

    api_key = key or get_deepseek_key()
    if not api_key:
        return {
            "query": query,
            "error": {
                "code": "missing_api_key",
                "detail": "Set DEEPSEEK_API_KEY in the environment or repo-root .env.",
            },
        }

    payload: dict = {
        "query": query,
        "count": count,
        "need_content": need_content,
    }
    if ori_question is not None:
        payload["ori_question"] = ori_question
    if localization:
        payload["localization"] = localization

    try:
        resp = requests.post(
            DEEPSEEK_ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return {
            "query": query,
            "error": {"code": "request_failed", "detail": str(exc)},
        }

    if resp.status_code != 200:
        return {
            "query": query,
            "error": {
                "code": f"http_{resp.status_code}",
                "detail": resp.text[:500],
            },
        }

    try:
        data = resp.json()
    except ValueError:
        return {
            "query": query,
            "error": {"code": "invalid_response", "detail": resp.text[:500]},
        }

    results = [
        {
            "title": item.get("title"),
            "url": item.get("url"),
            "published_date": item.get("date"),
            "snippet": item.get("snippet"),
            "summary": item.get("summary"),
            "content": item.get("content") or "",
            "site_name": item.get("site_name"),
            "rerank_score": item.get("rerank_score"),
            "authority": item.get("authority"),
        }
        for item in data.get("results", [])
    ]
    return {
        "query": query,
        "results": results,
    }


def _int_in_range(value: str, lo: int, hi: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integer, got {value!r}") from exc
    if not lo <= parsed <= hi:
        raise argparse.ArgumentTypeError(f"must be within {lo}..{hi}, got {parsed}")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DeepSeek web search -> JSON（脚本只取数，模型判断）。"
    )
    parser.add_argument("query", help="检索词（≤128 字符，支持 site:/-site:/引号 语法）")
    parser.add_argument(
        "--count",
        type=lambda v: _int_in_range(v, COUNT_MIN, COUNT_MAX),
        default=10,
        help="返回条数 1..50，默认 10",
    )
    parser.add_argument(
        "--need-content",
        action="store_true",
        help="返回网页正文（默认只回 snippet/summary，正文更省 token 的做法是不带这个开关）",
    )
    parser.add_argument(
        "--ori-question",
        default=None,
        help="触发搜索的用户原始问题（≤128 字符），帮助引擎理解意图",
    )
    parser.add_argument(
        "--localization",
        default=None,
        help="本地化结果优先用的 IP 地址",
    )
    args = parser.parse_args(argv)

    result = deepseek_search(
        args.query,
        count=args.count,
        need_content=args.need_content,
        ori_question=args.ori_question,
        localization=args.localization,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
