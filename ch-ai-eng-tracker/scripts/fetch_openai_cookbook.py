#!/usr/bin/env python3
"""OpenAI Cookbook 教程更新跟踪（GitHub API：窗口期内合并的 PR + 提交）。

Cookbook 的 PR 合并即"新增/更新一篇官方教程"，PR 标题天然带教程语义，是主信号；
commits 作补充（可选 path=examples 过滤，配置在 sources.yaml openai_cookbook）。

返回 JSON：
{"script","window","generated_at","merged_prs":[...],"commits":[...],"degraded":[...]}

用法：
    python scripts/fetch_openai_cookbook.py --days 7

凭据：GITHUB_TOKEN 可选（无 token 60 req/h，本脚本仅 2 个请求）。
降级：PR / commits 一路失败另一路照常；两路全败 exit 1。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    HttpError,
    RateLimiter,
    add_common_args,
    emit_error,
    http_get_json,
    iso_shanghai,
    load_config,
    make_session,
    now_shanghai,
    window_start_utc_iso,
    write_or_emit,
)

API = "https://api.github.com"


def gh_headers() -> dict:
    import os

    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_merged_prs(session, repo: str, since_iso: str, limit: int) -> list[dict]:
    url = f"{API}/repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=50"
    data = http_get_json(session, url, timeout=30, headers=gh_headers())
    prs: list[dict] = []
    for pr in data:
        merged_at = pr.get("merged_at")
        if not merged_at or merged_at < since_iso:
            continue
        prs.append(
            {
                "title": str(pr.get("title") or "").strip(),
                "url": pr.get("html_url"),
                "merged_at": iso_shanghai(datetime.fromisoformat(merged_at.replace("Z", "+00:00"))),
                "author": (pr.get("user") or {}).get("login"),
                "labels": [lb.get("name") for lb in pr.get("labels", []) if lb.get("name")],
                "body_excerpt": (pr.get("body") or "")[:400] or None,
            }
        )
        if len(prs) >= limit:
            break
    return prs


def fetch_commits(session, repo: str, since_iso: str, path_filter: bool, limit: int) -> list[dict]:
    url = f"{API}/repos/{repo}/commits?since={since_iso}&per_page=50"
    if path_filter:
        url += "&path=examples"
    data = http_get_json(session, url, timeout=30, headers=gh_headers())
    commits: list[dict] = []
    for c in data:
        commit = c.get("commit") or {}
        message = (commit.get("message") or "").splitlines()
        commits.append(
            {
                "sha": (c.get("sha") or "")[:10],
                "title": message[0].strip() if message else "",
                "url": c.get("html_url"),
                "date": iso_shanghai(datetime.fromisoformat(commit["author"]["date"].replace("Z", "+00:00")))
                if commit.get("author", {}).get("date")
                else None,
                "author": (commit.get("author") or {}).get("name"),
            }
        )
        if len(commits) >= limit:
            break
    return commits


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI Cookbook 教程更新跟踪（GitHub API）")
    add_common_args(parser, default_limit=30)
    parser.add_argument("--no-commits", action="store_true", help="只看合并 PR，跳过 commits")
    args = parser.parse_args()

    try:
        repo_cfg = (load_config().get("openai_cookbook") or {})
    except Exception as exc:  # noqa: BLE001
        emit_error("MISSING_CONFIG", str(exc), exit_code=2)
    repo = repo_cfg.get("repo") or "openai/openai-cookbook"

    since_iso = window_start_utc_iso(args.days)
    session = make_session()
    limiter = RateLimiter(1.1)  # 无 token 时对 api.github.com 保守节流
    degraded: list[dict] = []
    prs: list[dict] = []
    commits: list[dict] = []
    limit = getattr(args, "limit", None) or 30

    try:
        limiter.wait()
        prs = fetch_merged_prs(session, repo, since_iso, limit)
    except Exception as exc:  # noqa: BLE001
        degraded.append({"source": f"{repo}:merged_prs", "error": str(exc)})

    if not args.no_commits:
        try:
            limiter.wait()
            commits = fetch_commits(
                session, repo, since_iso, bool(repo_cfg.get("commits_path_filter")), limit
            )
        except Exception as exc:  # noqa: BLE001
            degraded.append({"source": f"{repo}:commits", "error": str(exc)})

    if not prs and not commits:
        if degraded:
            emit_error("GITHUB_API_FAILED", degraded)
        emit_error("NO_ITEMS", f"{repo} 窗口期内无教程更新（PR 与 commits 均为空）")

    payload = {
        "script": "fetch_openai_cookbook",
        "generated_at": iso_shanghai(now_shanghai()),
        "window": {"days": args.days, "since_utc": since_iso},
        "repo": repo,
        "merged_prs": prs,
        "commits": commits,
        "degraded": degraded,
    }
    write_or_emit(args, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
