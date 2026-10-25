#!/usr/bin/env python3
"""稳定入口：联网检索（默认 DeepSeek 搜索 API）。

默认目标 shared/web_search/multi_search.py，engine=deepseek；可用命令行
--engine 或环境变量 WEB_SEARCH_ENGINE 切换到 tavily / yuanbao / all 或逗号
分隔的 failover 列表。旧的 Tavily 专属参数 --topic / --search-depth 会被
剥离（DeepSeek 无对应参数）；--max-results / --days / --include-domains 等
统一参数原样传递，表达不了的过滤条件由 multi_search 以 notes/skipped 明示。

共享实现只调用搜索 API 并返回结构化结果；事件归因、产业推演和强弱判断均由
模型完成。本入口同时兼容 canonical 仓库与 Skill Sync 后的独立安装目录。
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
_CANDIDATES = [
    SCRIPT_ROOT.parents[2] / "shared" / "web_search" / "multi_search.py",
    SCRIPT_ROOT / "_shared" / "web_search" / "multi_search.py",
]
_LEGACY_FLAGS = ("--topic", "--search-depth")


def _resolve_target() -> Path:
    for candidate in _CANDIDATES:
        if candidate.exists():
            return candidate
    looked = "\n  ".join(str(candidate) for candidate in _CANDIDATES)
    sys.stderr.write("multi_search.py not found. Looked in:\n  " + looked + "\n")
    raise SystemExit(2)


def _strip_legacy(args: list[str]) -> list[str]:
    """剥离 Tavily 专属参数，保留统一入口支持的其余参数。"""
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _LEGACY_FLAGS:
            i += 2  # 跳过 flag 与其值
            continue
        if any(arg.startswith(flag + "=") for flag in _LEGACY_FLAGS):
            i += 1
            continue
        out.append(arg)
        i += 1
    return out


if __name__ == "__main__":
    target = _resolve_target()
    args = _strip_legacy(sys.argv[1:])
    if not any(arg == "--engine" or arg.startswith("--engine=") for arg in args):
        engine = (os.environ.get("WEB_SEARCH_ENGINE") or "deepseek").strip() or "deepseek"
        args = ["--engine", engine, *args]
    sys.argv = [str(target), *args]
    runpy.run_path(str(target), run_name="__main__")
