"""供需框架体检到期：只是体检提醒，不是产业多空结论。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from proactive_config import read_json, read_jsonl

from ..context import CTX

REVIEW_DAYS = 30
EXCERPT_LIMIT = 1500


def review_excerpt(text: str) -> str:
    """优先取框架里讲“体检”的那一节，取不到再退回文末。"""
    for block in re.split(r"(?m)^(?=#{2,3} )", text):
        if block.startswith("#") and "体检" in block.splitlines()[0]:
            return block.strip()[:EXCERPT_LIMIT]
    return text[-EXCERPT_LIMIT:]


def board_candidate(now: datetime) -> dict[str, Any] | None:
    from . import candidate

    board = read_json(CTX.board)
    version = datetime.strptime(board["version"], "%Y-%m-%d").date()
    due = version + timedelta(days=REVIEW_DAYS)
    if now.date() <= due:
        return None
    reviews = [row for row in read_jsonl(CTX.board_history, strict=False)
               if row.get("type") == "framework_review" and str(row.get("version") or "") == board["version"]]
    if reviews:
        return None
    excerpt = review_excerpt((CTX.root / CTX.board_framework).read_text(encoding="utf-8-sig"))
    return candidate("framework_review", f"industry-board:{board['version']}", board["version"], {
        "path": CTX.inputs["industry_board"], "version": board["version"],
        "review_due": due.isoformat(), "overdue_days": (now.date() - due).days,
        "framework": CTX.board_framework, "review_excerpt": excerpt,
        "note": "这是框架体检到期，不是产业多空结论；不得把旧 stance 当成最新事实。",
    })
