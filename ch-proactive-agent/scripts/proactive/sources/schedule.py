"""固定复核窗口（盘前 / 盘后）：是复核机会，不是必须发消息。"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def due_slot(now: datetime, state: dict[str, Any]) -> str | None:
    """今天已到点、尚未处理的最近一个窗口；周末不触发。"""
    if now.weekday() >= 5:
        return None
    slots = [slot for slot in state["policy"]["scheduled_reviews"] if slot <= now.strftime("%H:%M")]
    if not slots:
        return None
    native_id = f"{now.date()}:{slots[-1]}"
    return None if native_id in (state.get("scheduled_windows") or {}) else native_id


def scheduled_candidate(now: datetime, state: dict[str, Any], trend: dict[str, Any] | None) -> dict[str, Any] | None:
    from . import candidate

    native_id = due_slot(now, state)
    if native_id is None:
        return None
    return candidate("scheduled_review", native_id, now.date().isoformat(), {
        "window": native_id[11:], "supporting_trend": trend["evidence"] if trend else None,
        "note": "固定复核窗口不是必须发消息；无新差距就静默，缺数据不启动长任务。",
    })
