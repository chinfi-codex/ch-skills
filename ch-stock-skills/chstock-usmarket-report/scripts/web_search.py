#!/usr/bin/env python3
"""稳定入口：联网检索（Tavily）。

把共享实现 `shared/web_search/tavily_search.py` 的路径解析封装掉，让 SKILL.md
里始终用同一条命令 `python scripts/web_search.py "query" [opts]`——无论当前是
canonical 仓库（用 ../../../shared）还是同步副本（用 scripts/_shared）。

解析顺序与 render_report_html.py 一致：同步副本的 _shared 优先，dev 仓库 shared
兜底。用 runpy 直接以 __main__ 执行目标脚本，避免 web_search 包名冲突。
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
_CANDIDATES = [
    SCRIPT_ROOT.parents[2] / "shared" / "web_search" / "tavily_search.py",  # canonical 仓库
    SCRIPT_ROOT / "_shared" / "web_search" / "tavily_search.py",      # 同步副本（bundle）
]


def _resolve_target() -> Path:
    for cand in _CANDIDATES:
        if cand.exists():
            return cand
    looked = "\n  ".join(str(c) for c in _CANDIDATES)
    sys.stderr.write(
        "tavily_search.py not found. Looked in:\n  " + looked + "\n"
    )
    raise SystemExit(2)


if __name__ == "__main__":
    target = _resolve_target()
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name="__main__")
