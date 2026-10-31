"""候选生命周期：筛出本轮该呈现的候选、写候选包、组装评估包。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from proactive_config import atomic_write_json, read_json

from . import render, sources
from .context import CTX, FINAL_ACTIONS, FINGERPRINT_VERSION, digest, iso, parse_time
from .sources import catalyst
from .store import append_audit

INHERITABLE_KINDS = {"trend_state", "framework_review"}


def packet_path(cid: str) -> Path:
    if not re.fullmatch(r"PG-[0-9a-f]{16}", cid):
        raise ValueError("非法 candidate_id")
    return CTX.packets / f"{cid}.json"


def _entries(state: dict[str, Any]):
    yield from state["candidates"].items()
    yield from (state.get("_index") or {}).items()


def latest_native(fresh: list[dict[str, Any]], state: dict[str, Any] | None = None) -> dict[str, str]:
    latest = {item["native_id"]: item["evidence_hash"] for item in fresh}
    if state:
        for entry in state["candidates"].values():
            if entry.get("last_action") in {"pending", "defer"}:
                latest.setdefault(entry["native_id"], entry["evidence_hash"])
    return latest


def rehydrate(state: dict[str, Any], now: datetime, fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """已呈现候选可越过文件 diff、跨日窗口和进程重启恢复。"""
    result = list(fresh)
    present = {item["candidate_id"] for item in fresh}
    for cid, entry in state["candidates"].items():
        if cid in present or entry.get("last_action") not in {"pending", "defer"}:
            continue
        review_after = entry.get("review_after")
        if review_after and parse_time(review_after) > now:
            continue
        if entry.get("last_action") == "pending" and entry.get("lease_until") and parse_time(entry["lease_until"]) > now:
            continue
        rel = entry.get("packet_path")
        if not rel:
            raise ValueError(f"延期候选缺少落盘包：{cid}")
        path = (CTX.runtime / rel).resolve()
        try:
            path.relative_to(CTX.runtime.resolve())
            item = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"延期候选包缺失、损坏或越界：{cid} ({rel})") from exc
        if item.get("candidate_id") != cid or item.get("evidence_hash") != entry.get("evidence_hash"):
            raise ValueError(f"延期候选包版本不匹配：{cid}")
        result.append(item)
    return result


def supersede_old_versions(state: dict[str, Any], latest: dict[str, str], now: datetime) -> None:
    by_id = state["candidates"]
    for entry in by_id.values():
        new_hash = latest.get(entry.get("native_id"))
        if new_hash and new_hash != entry.get("evidence_hash") and entry.get("last_action") not in FINAL_ACTIONS:
            entry["last_action"] = "superseded"
            entry["superseded_at"] = iso(now)
    for intent in state["outbox"].values():
        if intent.get("status") != "pending":
            continue
        new_hash = latest.get(intent.get("native_id"))
        if new_hash and new_hash != intent.get("evidence_hash"):
            intent["status"] = "cancelled"
            intent["cancel_reason"] = "superseded_by_new_evidence"


def inherit_fingerprints(state: dict[str, Any], fresh: list[dict[str, Any]], now: datetime) -> int:
    """指纹算法升级后，用旧候选包按新算法重算；语义相同则继承终态，不因算法变化重新唤醒。"""
    inherited = 0
    for item in fresh:
        cid = item["candidate_id"]
        if item["kind"] not in INHERITABLE_KINDS or cid in state["candidates"] or cid in (state.get("_index") or {}):
            continue
        for _old_id, entry in list(_entries(state)):
            if (entry.get("native_id") != item["native_id"] or entry.get("last_action") not in FINAL_ACTIONS
                    or int(entry.get("fingerprint_version") or 0) >= FINGERPRINT_VERSION or not entry.get("packet_path")):
                continue
            try:
                old = read_json(CTX.runtime / entry["packet_path"])
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if digest(sources.default_fingerprint(item["kind"], item["native_id"], old.get("evidence") or {})) != item["evidence_hash"]:
                continue
            state["candidates"][cid] = {
                "evidence_hash": item["evidence_hash"], "kind": item["kind"], "native_id": item["native_id"],
                "fingerprint_version": FINGERPRINT_VERSION, "first_seen_at": iso(now), "last_seen_at": iso(now),
                "attempts": 0, "last_action": entry["last_action"], "review_after": None,
                "inherited_from": _old_id, "topic_key": item.get("topic_key"),
            }
            inherited += 1
            break
    return inherited


def attach_prior_version(state: dict[str, Any], item: dict[str, Any]) -> None:
    """给已呈现候选附上一版差异；只读该候选自己的旧包，不重读全量。"""
    priors = [row for cid, row in _entries(state)
              if row.get("native_id") == item["native_id"] and cid != item["candidate_id"] and row.get("packet_path")]
    if not priors:
        return
    prior = max(priors, key=lambda row: row.get("last_seen_at") or row.get("at") or "")
    try:
        old = read_json(CTX.runtime / prior["packet_path"])
    except (OSError, ValueError, json.JSONDecodeError):
        return
    changes = catalyst.semantic_diff(old.get("evidence") or {}, item["evidence"])
    if changes:
        item["prior_version"] = {"candidate_id": str(old.get("candidate_id") or "")[3:],
                                 "last_seen_at": prior.get("last_seen_at") or prior.get("at"), "changes": changes}


def update_presentation(state: dict[str, Any], items: list[dict[str, Any]], now: datetime) -> None:
    lease = int(state["policy"]["evaluation_lease_minutes"])
    for item in items:
        cid = item["candidate_id"]
        entry = state["candidates"].setdefault(cid, {
            "evidence_hash": item["evidence_hash"], "kind": item["kind"], "native_id": item["native_id"],
            "first_seen_at": iso(now), "attempts": 0, "last_action": "pending", "review_after": None,
        })
        freshness = item.get("freshness") or {}
        if entry.get("valid_until") is None:
            entry["valid_until"] = item.get("valid_until") or iso(
                now + timedelta(hours=int(state["policy"]["pending_notification_ttl_hours"])))
        entry.update(evidence_hash=item["evidence_hash"], last_seen_at=iso(now),
                     fingerprint_version=item.get("fingerprint_version", FINGERPRINT_VERSION),
                     freshness_class=freshness.get("class", "current"),
                     raw_available=freshness.get("raw_available", True), topic_key=item.get("topic_key"),
                     observed_at=item["observed_at"], packet_path=packet_path(cid).relative_to(CTX.runtime).as_posix())
        if entry.get("last_action") == "pending":
            entry["lease_until"] = iso(now + timedelta(minutes=lease))
        if item["kind"] == "catalyst":
            attach_prior_version(state, item)
        atomic_write_json(packet_path(cid), item)


def eligible(state: dict[str, Any], now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    fresh, extra, errors = sources.collect(state, now)
    inherit_fingerprints(state, fresh, now)
    if not errors:
        supersede_old_versions(state, latest_native(fresh), now)
    index = state.get("_index") or {}
    items = []
    for item in rehydrate(state, now, fresh):
        cid = item["candidate_id"]
        if cid in index:
            continue
        entry = state["candidates"].get(cid)
        if (item.get("freshness") or {}).get("class") == "backfill":
            if not entry or entry.get("last_action") not in FINAL_ACTIONS:
                state["candidates"][cid] = {
                    "evidence_hash": item["evidence_hash"], "kind": item["kind"], "native_id": item["native_id"],
                    "fingerprint_version": FINGERPRINT_VERSION, "first_seen_at": iso(now),
                    "attempts": 0, "last_action": "backfill", "review_after": None,
                    "valid_until": item.get("valid_until"),
                }
            continue
        if entry:
            if entry.get("evidence_hash") != item["evidence_hash"]:
                raise ValueError("candidate_id 与证据哈希不一致")
            if entry.get("last_action") in FINAL_ACTIONS:
                continue
            if int(entry.get("attempts", 0)) >= int(state["policy"]["max_decision_attempts"]):
                entry["last_action"] = "dead"
                entry["dead_reason"] = "decision_attempts_exhausted"
                errors.append(f"候选决策次数耗尽，已转为 dead：{entry['native_id']}")
                append_audit([{"at": iso(now), "goal_id": CTX.goal_id, "candidate_id": cid,
                               "status": "dead", "reason": "decision_attempts_exhausted"}])
                continue
            if entry.get("last_action") == "defer" and entry.get("review_after") and parse_time(entry["review_after"]) > now:
                continue
            if entry.get("last_action") == "pending" and entry.get("lease_until") and parse_time(entry["lease_until"]) > now:
                continue
        items.append(item)
    items = items[:int(state["policy"]["max_candidates_per_wake"])]
    return items, extra, errors


def recent_notifications(state: dict[str, Any], now: datetime, days: int = 14) -> list[dict[str, Any]]:
    rows = []
    for intent in state["outbox"].values():
        if intent.get("status") in {"pending", "cancelled", "expired", "suppressed_cooldown"} or not intent.get("attempted_at"):
            continue
        if parse_time(intent["attempted_at"]) < now - timedelta(days=days):
            continue
        rows.append({"topic_key": intent.get("topic_key"), "kind": intent.get("kind"),
                     "priority": intent.get("priority"), "sent_at": intent["attempted_at"],
                     "headline": (intent.get("message") or "").splitlines()[0][:80]})
    rows.sort(key=lambda row: row["sent_at"], reverse=True)
    return rows[:10]


def wake_payload(items: list[dict[str, Any]], errors: list[str], now: datetime,
                 state: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"goal_id": CTX.goal_id, "protocol": "ch-proactive-agent/references/protocol.md",
               "evaluated_at": iso(now), "data_scope": list(CTX.settings.data_scope),
               "long_task_skills": "output_only_never_invoke", "tools_available_to_agent": [],
               "candidates": items, "source_errors": errors,
               "instruction": "按已加载 SKILL.md 的五道闸与输出契约逐项决策，只输出一个 JSON 对象。"}
    if state is not None:
        payload["recent_notifications"] = recent_notifications(state, now)
        payload["glossary"] = render.glossary_for(json.dumps(items, ensure_ascii=False))
    return payload
