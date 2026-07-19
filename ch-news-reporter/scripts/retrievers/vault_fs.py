#!/usr/bin/env python3
"""vault_fs 通道:AlphaVault 本地 Obsidian Vault 只读检索。

按主题配置的 paths(vault 内相对目录或 glob)收集 .md 笔记,用主题关键词
(支持引号短语)匹配正文,返回笔记路径 + 命中关键词 + 匹配段落片段。

铁律:
- 只读,绝不写入 vault;不调用任何 API,不需要 key。
- vault_root 或单个路径不存在时记 warning 并跳过,不崩溃。
- 笔记是"历史认知与既有判断",不是新证据;是否采信、是否过期由模型判断,
  本脚本只回传 mtime 供模型参考。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR.parent))

from retrievers.common import compact_text, keyword_hits  # noqa: E402

MAX_FILE_CHARS = 1_000_000       # 单文件超过 100 万字符跳过并记 warning
MAX_FILES_SCAN = 200             # 单主题最多扫描的文件数(按 mtime 新→旧)
MAX_NOTES = 20                   # 单主题最多返回的命中笔记数
MAX_SNIPPETS_PER_NOTE = 3        # 每篇笔记最多带回的匹配段落数
SNIPPET_LEN = 400
_GLOB_CHARS = set("*?[")


def _note_title(text: str, path: Path) -> str:
    """笔记标题:优先 frontmatter title,其次首个 ATX 标题,最后文件名。"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if line.strip().startswith("title:"):
                    value = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or path.stem
    return path.stem


def _matching_snippets(text: str, keywords: list[str]) -> list[str]:
    """按空行切段落,返回含关键词命中的段落片段(截断,最多 MAX_SNIPPETS_PER_NOTE 段)。"""
    snippets: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        if not keyword_hits(paragraph, keywords):
            continue
        snippet = compact_text(paragraph, SNIPPET_LEN)
        if snippet and snippet not in snippets:
            snippets.append(snippet)
        if len(snippets) >= MAX_SNIPPETS_PER_NOTE:
            break
    return snippets


def _collect_files(root: Path, paths: list[str], warnings: list[str]) -> list[Path]:
    """按配置 paths 收集 .md 文件;目录递归,glob 直接展开,不存在则跳过记 warning。"""
    root = root.resolve()
    files: list[Path] = []
    seen: set[str] = set()
    rejected: set[str] = set()

    def reject(entry: str) -> None:
        if entry not in rejected:
            warnings.append(f"vault 路径越界,已跳过: {entry}")
            rejected.add(entry)

    def resolve_inside(path: Path, entry: str) -> Path | None:
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            reject(entry)
            return None
        return resolved

    def add(path: Path, entry: str) -> None:
        path = resolve_inside(path, entry)
        if path is None:
            return
        key = str(path)
        if key in seen or not path.is_file() or path.suffix.lower() != ".md":
            return
        seen.add(key)
        files.append(path)

    for entry in paths:
        entry = str(entry).strip()
        if not entry:
            continue
        entry_path = Path(entry)
        if entry_path.is_absolute() or ".." in entry_path.parts:
            reject(entry)
            continue
        if _GLOB_CHARS & set(entry):
            for match in sorted(root.glob(entry)):
                add(match, entry)
            continue
        target = resolve_inside(root / entry, entry)
        if target is None:
            continue
        if target.is_dir():
            for match in sorted(target.rglob("*.md")):
                add(match, entry)
        elif target.is_file():
            add(target, entry)
        else:
            warnings.append(f"vault 路径不存在,已跳过: {entry}")
    return files


def retrieve(
    topic: dict[str, Any], settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    """按主题 channels.alpha_vault 配置检索 vault;返回统一通道结构 + notes 列表。"""
    raw = (topic.get("channels") or {}).get("alpha_vault")
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return {"status": "skipped", "count": 0, "warnings": ["alpha_vault 通道未启用"], "notes": []}

    keywords = [str(k) for k in (topic.get("keywords") or []) if str(k).strip()]
    if not keywords:
        return {
            "status": "ok",
            "count": 0,
            "warnings": ["主题未配置 keywords,alpha_vault 通道返回空"],
            "notes": [],
        }

    vault_root = str((settings or {}).get("vault_root") or "").strip()
    if not vault_root:
        return {
            "status": "degraded",
            "count": 0,
            "warnings": ["settings.vault_root 未配置,alpha_vault 通道降级"],
            "notes": [],
        }
    root = Path(os.path.expanduser(vault_root)).resolve()
    if not root.is_dir():
        return {
            "status": "degraded",
            "count": 0,
            "warnings": [f"vault_root 不存在或不是目录: {root},alpha_vault 通道降级"],
            "notes": [],
        }

    warnings: list[str] = []
    paths = raw.get("paths") or []
    if not paths:
        warnings.append("alpha_vault 未配置 paths,将检索整个 vault(建议配置子目录控制范围)")
        paths = ["."]

    files = _collect_files(root, paths, warnings)
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    files = files[:MAX_FILES_SCAN]

    notes: list[dict[str, Any]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"读取失败,已跳过 {path.relative_to(root)}: {exc}")
            continue
        if len(text) > MAX_FILE_CHARS:
            warnings.append(f"文件过大({len(text)} 字符),已跳过: {path.relative_to(root)}")
            continue
        hits = keyword_hits(text, keywords)
        if not hits:
            continue
        notes.append(
            {
                "path": str(path.relative_to(root)),
                "title": _note_title(text, path),
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "matched_keywords": hits,
                "snippets": _matching_snippets(text, keywords),
            }
        )
        if len(notes) >= MAX_NOTES:
            break

    if not notes:
        warnings.append("配置路径内无关键词命中的笔记")
    return {"status": "ok", "count": len(notes), "warnings": warnings, "notes": notes}
