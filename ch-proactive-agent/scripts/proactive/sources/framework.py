"""方法论文件变化：新增、修改、删除都产生 framework_change 候选，用于重新对齐目标。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from proactive_config import file_hash

from ..context import CTX, LOCAL_TZ

EXCERPT_LIMIT = 2000


def framework_candidates(state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    from . import candidate

    previous = state.get("framework_versions") or {}
    paths = [CTX.root / rel for rel in CTX.framework_files]
    paths.extend(sorted((CTX.root / CTX.rules_dir).glob("R-*.md")))
    current = {path.relative_to(CTX.root).as_posix(): file_hash(path, length=None) for path in paths if path.is_file()}
    errors = [f"必需方法论文件缺失：{rel}" for rel in CTX.framework_files if rel not in current]
    results: list[dict[str, Any]] = []
    for rel in sorted(set(current) | set(previous)):
        old_hash, new_hash = previous.get(rel), current.get(rel)
        if not state.get("initialized") or old_hash == new_hash:
            continue
        change_type = "added" if old_hash is None else "deleted" if new_hash is None else "modified"
        excerpt = (CTX.root / rel).read_text(encoding="utf-8-sig")[:EXCERPT_LIMIT] if new_hash else ""
        results.append(candidate("framework_change", rel, datetime.now(LOCAL_TZ).date().isoformat(), {
            "path": rel, "change_type": change_type, "previous_sha256": old_hash,
            "current_sha256": new_hash, "current_excerpt": excerpt,
            "note": "比较目标和规则含义；排版变化或无法判断语义时保持静默，不把变更本身当成市场事件。",
        }))
    return results, current, errors
