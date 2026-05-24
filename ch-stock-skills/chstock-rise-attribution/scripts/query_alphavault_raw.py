#!/usr/bin/env python3
"""Thin wrapper around AlphaVault's `query_source_registry.py`.

Locates the AlphaVault root from --alphavault-root or the ALPHAVAULT_ROOT
environment variable, then forwards filters (page / keyword / source_type /
status / hash) to the registry CLI. Adds an optional date-window post-filter
on the registry's `最后摄取时间` field so the rise-attribution skill can pull
"sources updated near a given trade date" without bloating the main context.

Outputs JSON to stdout (or --output) shaped as::

    {
        "alphavault_root": "...",
        "queried_at": "...",
        "filters": {...},
        "registry_total": N,
        "returned": M,
        "truncated": bool,
        "skipped": "alphavault_root_missing" | null,
        "sources": [ { ...registry fields... } ]
    }

When AlphaVault root is missing, exits 0 with a `skipped` payload — the
orchestrator treats this as "library lookup deferred" rather than failure.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def resolve_alphavault_root(cli_value: str | None) -> Path | None:
    candidate = cli_value or os.environ.get("ALPHAVAULT_ROOT", "")
    if not candidate:
        return None
    path = Path(candidate).expanduser().resolve()
    if not (path / "system" / "tools" / "query_source_registry.py").exists():
        return None
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="定向查询 AlphaVault 来源登记表（薄包装），支持日期窗口后置过滤。",
    )
    parser.add_argument("--alphavault-root", default="", help="AlphaVault 根目录；缺省读 ALPHAVAULT_ROOT 环境变量")
    parser.add_argument("--page", default="", help="按更新页面路径或页面名子串过滤")
    parser.add_argument("--keyword", action="append", default=[], help="任意登记字段关键词（可多次）")
    parser.add_argument("--source-type", choices=["clippings", "crawlers", "raw", "pack"], default="")
    parser.add_argument("--status", choices=["ingested", "skipped_duplicate", "revised", "blocked", "failed"], default="")
    parser.add_argument("--date-from", default="", help="按 最后摄取时间 后置过滤的起始日期 YYYY-MM-DD")
    parser.add_argument("--date-to", default="", help="按 最后摄取时间 后置过滤的结束日期 YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=50, help="返回上限")
    parser.add_argument("--output", default="", help="输出 JSON 文件路径，缺省 stdout")
    return parser.parse_args()


def build_registry_cmd(args: argparse.Namespace, root: Path) -> list[str]:
    cmd: list[str] = [
        sys.executable,
        str(root / "system" / "tools" / "query_source_registry.py"),
        "--format",
        "json",
        "--limit",
        str(max(args.limit * 2, args.limit)),  # over-fetch so date filter still respects --limit
    ]
    if args.page:
        cmd += ["--page", args.page]
    for kw in args.keyword:
        cmd += ["--keyword", kw]
    if args.source_type:
        cmd += ["--source-type", args.source_type]
    if args.status:
        cmd += ["--status", args.status]
    return cmd


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[: len(fmt)], fmt)
        except ValueError:
            continue
    return None


def within_window(item: dict[str, Any], dt_from: datetime | None, dt_to: datetime | None) -> bool:
    if not dt_from and not dt_to:
        return True
    raw = item.get("最后摄取时间") or item.get("首次摄取时间")
    if not raw:
        return False
    parsed = parse_date(str(raw))
    if not parsed:
        return False
    if dt_from and parsed < dt_from:
        return False
    if dt_to and parsed > dt_to:
        return False
    return True


def emit(payload: dict[str, Any], output: str) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text)


def main() -> int:
    args = parse_args()
    has_filter = bool(args.page or args.keyword or args.source_type or args.status)

    root = resolve_alphavault_root(args.alphavault_root)
    if root is None:
        emit(
            {
                "alphavault_root": args.alphavault_root or os.environ.get("ALPHAVAULT_ROOT", ""),
                "queried_at": datetime.utcnow().isoformat() + "Z",
                "filters": vars(args),
                "registry_total": 0,
                "returned": 0,
                "truncated": False,
                "skipped": "alphavault_root_missing",
                "sources": [],
                "note": "未配置 ALPHAVAULT_ROOT 或 system/tools/query_source_registry.py 不存在；库内回溯已跳过。",
            },
            args.output,
        )
        return 0

    if not has_filter:
        print(
            "请至少提供 --page、--keyword、--source-type、--status 之一。",
            file=sys.stderr,
        )
        return 2

    cmd = build_registry_cmd(args, root)
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=str(root))
    except subprocess.CalledProcessError as exc:
        print(f"query_source_registry.py failed: {exc.stderr.strip() or exc}", file=sys.stderr)
        return 1

    try:
        registry_payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        print(f"无法解析 registry 输出: {exc}", file=sys.stderr)
        return 1

    sources = registry_payload.get("sources", [])
    dt_from = parse_date(args.date_from) if args.date_from else None
    dt_to = parse_date(args.date_to) if args.date_to else None
    if dt_to:
        # extend to end-of-day
        dt_to = datetime(dt_to.year, dt_to.month, dt_to.day, 23, 59, 59)
    filtered = [item for item in sources if within_window(item, dt_from, dt_to)]
    limited = filtered[: args.limit]

    emit(
        {
            "alphavault_root": str(root),
            "queried_at": datetime.utcnow().isoformat() + "Z",
            "filters": {
                "page": args.page,
                "keyword": args.keyword,
                "source_type": args.source_type,
                "status": args.status,
                "date_from": args.date_from,
                "date_to": args.date_to,
                "limit": args.limit,
            },
            "registry_total": registry_payload.get("total_sources"),
            "registry_matched": registry_payload.get("matched"),
            "date_filtered": len(filtered),
            "returned": len(limited),
            "truncated": len(filtered) > len(limited),
            "skipped": None,
            "sources": limited,
        },
        args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
