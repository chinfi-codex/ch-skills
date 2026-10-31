"""状态读写、压缩索引、审计与健康记录。

state.json 只放活跃候选、待办通知与游标；终态候选压缩进 index.json（去重只需要它），
过期的通知意图与运行记录滚动到 archive/。运行时 state["_index"] 挂着索引，保存时拆开写。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from typing import Any

from proactive_config import atomic_write_json, read_json

from .context import CTX, FINAL_ACTIONS, SCHEMA_VERSION, iso, parse_time, short

LEGACY_SCHEMA_VERSIONS = {"proactive-goal/v2"}
TERMINAL_INTENTS = {"delivered", "failed", "not_sent", "unverified", "cancelled", "expired", "suppressed_cooldown"}
INDEX_KEYS = ("native_id", "kind", "last_action", "packet_path", "fingerprint_version")
HEALTH_LIMIT = 50


def _defaults() -> dict[str, Any]:
    return {
        "health": [],
        "topics": {},
        "signals": {"watermarks": {}, "pending": {}, "offset": 0, "last_full_scan_at": None,
                    "evaluations": [], "inflight": None},
        "last_compact_at": None,
    }


def default_state(now: datetime) -> dict[str, Any]:
    state = {
        "schema_version": SCHEMA_VERSION, "goal_id": CTX.goal_id, "workspace": ".",
        "status": "active", "created_at": iso(now), "updated_at": iso(now),
        "initialized": False, "policy": copy.deepcopy(CTX.policy_defaults),
        "framework_versions": {}, "candidates": {}, "scheduled_windows": {},
        "outbox": {}, "processed_runs": {}, "last_scan_at": None,
        "last_notification_request_at": None,
        "notification_requests_today": {"date": now.date().isoformat(), "count": 0},
        "mute_until": None,
        "delivery": {"status": "idle", "last_candidate_id": None, "last_message_sha256": None},
        "source_errors": [],
    }
    state.update(_defaults())
    state["_index"] = {}
    return state


def _upgrade(state: dict[str, Any]) -> None:
    if state.get("schema_version") in LEGACY_SCHEMA_VERSIONS:
        state["schema_version"] = SCHEMA_VERSION
        # v2 的 10 分钟租约短于“备批 → 评估返回”的周期，升级时抬到新默认值
        policy = state.setdefault("policy", {})
        policy["evaluation_lease_minutes"] = max(int(policy.get("evaluation_lease_minutes") or 0),
                                                 int(CTX.policy_defaults.get("evaluation_lease_minutes", 30)))
    for key, value in _defaults().items():
        state.setdefault(key, value)
    for key, value in _defaults()["signals"].items():
        state["signals"].setdefault(key, copy.deepcopy(value))


def _validate(state: dict[str, Any]) -> None:
    if CTX.schema is None or not CTX.schema.is_file():
        return
    import jsonschema

    jsonschema.validate({k: v for k, v in state.items() if not k.startswith("_")},
                        json.loads(CTX.schema.read_text(encoding="utf-8")))


def load_state() -> dict[str, Any]:
    if not CTX.state_path.is_file():
        raise ValueError("主动目标状态缺失；先运行 init 或迁移")
    state = read_json(CTX.state_path)
    if not isinstance(state, dict) or state.get("goal_id") != CTX.goal_id:
        raise ValueError("主动目标状态缺失或 goal_id 不匹配；先运行 init")
    _upgrade(state)
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("不支持的主动目标状态版本")
    for key in ("policy", "candidates", "outbox", "processed_runs", "framework_versions"):
        if not isinstance(state.get(key), dict):
            raise ValueError(f"主动目标状态缺少 {key}")
    state["policy"] = {**CTX.policy_defaults, **state["policy"]}
    if state.get("status") not in {"active", "paused"}:
        raise ValueError("status 必须是 active 或 paused")
    _validate(state)
    index = read_json(CTX.index_path) if CTX.index_path.is_file() else {}
    state["_index"] = index if isinstance(index, dict) else {}
    return state


def save_state(state: dict[str, Any]) -> None:
    index = state.get("_index")
    persisted = {k: v for k, v in state.items() if not k.startswith("_")}
    if index is not None:
        atomic_write_json(CTX.index_path, index)
    atomic_write_json(CTX.state_path, persisted)


def lookup(state: dict[str, Any], cid: str) -> dict[str, Any] | None:
    return state["candidates"].get(cid) or (state.get("_index") or {}).get(cid)


def append_audit(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    CTX.audit_path.parent.mkdir(parents=True, exist_ok=True)
    with CTX.audit_path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def add_health(state: dict[str, Any], code: str, detail: Any, now: datetime) -> None:
    """记录本地可见的健康问题；同一 code 连续出现只更新计数，不刷屏。"""
    rows = state.setdefault("health", [])
    if rows and rows[-1].get("code") == code and rows[-1].get("detail") == short(detail, 300):
        rows[-1]["count"] = int(rows[-1].get("count", 1)) + 1
        rows[-1]["last_at"] = iso(now)
    else:
        rows.append({"code": code, "detail": short(detail, 300), "at": iso(now), "last_at": iso(now), "count": 1})
        append_audit([{"at": iso(now), "goal_id": CTX.goal_id, "status": "health", "code": code,
                       "detail": short(detail, 300)}])
    del rows[:-HEALTH_LIMIT]


def _append_archive(name: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    CTX.archive.mkdir(parents=True, exist_ok=True)
    with (CTX.archive / name).open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def compact(state: dict[str, Any], now: datetime) -> dict[str, int]:
    """终态候选进索引；过期意图、运行记录与固定窗口滚动归档。只移动，不改语义。"""
    policy = state["policy"]
    keep_days = int(policy.get("compact_after_days", 3))
    cutoff = now - timedelta(days=keep_days)
    index = state.setdefault("_index", {})
    referenced = {row.get("candidate_id") for row in state["outbox"].values() if row.get("status") in {"pending", "attempted"}}
    moved = 0
    for cid, entry in list(state["candidates"].items()):
        if entry.get("last_action") not in FINAL_ACTIONS or cid in referenced:
            continue
        stamp = entry.get("updated_at") or entry.get("last_seen_at") or entry.get("first_seen_at")
        if entry.get("last_action") not in {"baseline", "backfill"} and stamp and parse_time(stamp) > cutoff:
            continue
        index[cid] = {key: entry.get(key) for key in INDEX_KEYS if entry.get(key) is not None}
        index[cid]["at"] = stamp
        del state["candidates"][cid]
        moved += 1

    month = now.strftime("%Y-%m")
    retired = []
    for intent_id, intent in list(state["outbox"].items()):
        stamp = intent.get("receipt_at") or intent.get("expired_at") or intent.get("created_at")
        if intent.get("status") in TERMINAL_INTENTS and stamp and parse_time(stamp) < now - timedelta(days=14):
            retired.append({"intent_id": intent_id, **intent})
            del state["outbox"][intent_id]
    _append_archive(f"outbox-{month}.jsonl", retired)

    for row in state["processed_runs"].values():
        if isinstance(row.get("outcomes"), list):   # v2 存的是逐项明细，审计里已有，压成计数
            summary: dict[str, int] = {}
            for item in row["outcomes"]:
                key = str(item.get("outcome"))
                summary[key] = summary.get(key, 0) + 1
            row["outcomes"] = summary
    runs = sorted(state["processed_runs"].items(), key=lambda item: item[1].get("at") or "")
    stale = [key for key, row in runs[:-200]] + [
        key for key, row in runs[-200:] if row.get("at") and parse_time(row["at"]) < now - timedelta(days=14)]
    for key in stale:
        state["processed_runs"].pop(key, None)

    window_cutoff = (now - timedelta(days=14)).date().isoformat()
    for key in [k for k in state.get("scheduled_windows", {}) if k[:10] < window_cutoff]:
        del state["scheduled_windows"][key]
    state["last_compact_at"] = iso(now)
    return {"candidates_indexed": moved, "intents_archived": len(retired), "runs_pruned": len(stale)}
