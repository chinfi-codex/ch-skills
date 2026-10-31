"""通知意图：主题冷却、频控、投递前复核与回执对账。

意图状态：pending → attempted → delivered | failed | not_sent | unverified；
另有 cancelled（证据被取代）、expired（过期）、suppressed_cooldown（同主题冷却期内）。
attempted 不自动重发；回执只从 Hermes 执行记录读取，未知时标 unverified，不猜测已送达。
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from .context import CTX, PRIORITY_RANK, digest, hermes_home, iso, parse_time
from .sources import catalyst
from .store import add_health, append_audit

RECEIPT_GRACE = timedelta(hours=2)


def is_active_time(policy: dict[str, Any], now: datetime) -> bool:
    current = now.strftime("%H:%M")
    return str(policy["active_start"]) <= current < str(policy["active_end"])


def next_notification_time(state: dict[str, Any], now: datetime) -> datetime:
    policy = state["policy"]
    start_h, start_m = map(int, policy["quiet_start"].split(":"))
    end_h, end_m = map(int, policy["quiet_end"].split(":"))

    def outside(value: datetime) -> datetime:
        start = value.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end = value.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        quiet = (value >= start or value < end) if start > end else (start <= value < end)
        return (end if value < end else end + timedelta(days=1)) if quiet else value

    result = outside(now)
    if state.get("last_notification_request_at"):
        result = max(result, parse_time(state["last_notification_request_at"])
                     + timedelta(minutes=int(policy["min_notification_interval_minutes"])))
    today = state.get("notification_requests_today") or {}
    if today.get("date") == now.date().isoformat() and int(today.get("count", 0)) >= int(policy["max_notifications_per_day"]):
        result = max(result, (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
    if state.get("mute_until"):
        result = max(result, parse_time(state["mute_until"]))
    return outside(result)


def topic_until(state: dict[str, Any], topic: str | None, kind: str) -> datetime | None:
    """同主题上次发出后的冷却截止时间；冷却时长按候选类型取 sources.yaml 的 cooldown_hours。"""
    last = (state.get("topics") or {}).get(topic or "")
    hours = float(CTX.source(kind).get("cooldown_hours") or 0)
    if not last or hours <= 0:
        return None
    return parse_time(last["last_at"]) + timedelta(hours=hours)


def prepare_intent(state: dict[str, Any], entry: dict[str, Any], cid: str, message: str, priority: str | None,
                   now: datetime, run_key: str, source_version: str) -> tuple[str, str]:
    intent_id = "OUT-" + digest({"candidate_id": cid, "message": message})[:16]
    if intent_id in state["outbox"]:
        return "duplicate_intent", intent_id
    topic = entry.get("topic_key")
    ttl_expiry = now + timedelta(hours=int(state["policy"]["pending_notification_ttl_hours"]))
    valid_until = min(parse_time(entry["valid_until"]), ttl_expiry) if entry.get("valid_until") else ttl_expiry
    not_before = next_notification_time(state, now)
    cooldown = topic_until(state, topic, entry["kind"])
    if cooldown and cooldown > not_before:
        not_before = cooldown
    status = "suppressed_cooldown" if not_before > valid_until else "pending"
    state["outbox"][intent_id] = {
        "candidate_id": cid, "native_id": entry["native_id"], "kind": entry["kind"],
        "evidence_hash": entry["evidence_hash"], "source_version": source_version,
        "raw_available": entry.get("raw_available", True), "topic_key": topic, "priority": priority,
        "message": message, "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "status": status, "not_before": iso(not_before), "created_at": iso(now), "valid_until": iso(valid_until),
        "attempted_at": None, "channel": None, "run_key": run_key,
    }
    if status == "pending":
        state["delivery"] = {"status": "pending", "last_candidate_id": cid,
                             "last_message_sha256": state["outbox"][intent_id]["message_sha256"]}
        return "prepared", intent_id
    return "suppressed_cooldown", intent_id


def intent_raw_ok(intent: dict[str, Any], fresh: list[dict[str, Any]]) -> bool:
    """投递前只为这一条通知定向读原始证据，避免全量预读。"""
    item = next((row for row in fresh if row["native_id"] == intent["native_id"]), None)
    if item is None:
        return True
    refs = (item.get("evidence") or {}).get("source_refs") or []
    if not refs:
        return False
    checks = [catalyst.raw_excerpt(str(ref.get("raw_path") or ""), limit=800) for ref in refs[:2]]
    return all("error" not in row for row in checks)


def expire_pending(state: dict[str, Any], now: datetime) -> None:
    for intent in state["outbox"].values():
        if intent.get("status") != "pending":
            continue
        expiry = intent.get("valid_until") or iso(
            parse_time(intent["created_at"]) + timedelta(hours=int(state["policy"]["pending_notification_ttl_hours"])))
        if parse_time(expiry) < now:
            intent.update(status="expired", expired_at=iso(now), cancel_reason="notification_valid_until_elapsed")
            append_audit([{"at": iso(now), "goal_id": CTX.goal_id, "candidate_id": intent["candidate_id"],
                           "status": "notification_expired", "valid_until": expiry}])
            if state["delivery"].get("last_candidate_id") == intent["candidate_id"]:
                state["delivery"]["status"] = "expired"


def deliver_due(state: dict[str, Any], now: datetime, channel: str,
                collect: Callable[[], tuple[list[dict[str, Any]], dict[str, Any], list[str]]]) -> str | None:
    """最多放行一条：优先级高的先发，同级按创建时间。返回的文本即本次唯一可投递内容。"""
    expire_pending(state, now)
    pending = [row for row in state["outbox"].values()
               if row.get("status") == "pending" and parse_time(row["not_before"]) <= now]
    if not pending:
        return None
    pending.sort(key=lambda row: (PRIORITY_RANK.get(row.get("priority") or "P2", 2), row["created_at"]))
    intent = pending[0]
    fresh, extra, errors = collect()
    state["source_errors"] = errors
    if errors:
        return None
    latest = {item["native_id"]: item["evidence_hash"] for item in fresh}
    latest.update(extra["framework_current"])
    expected = intent.get("source_version", intent["evidence_hash"])
    if intent.get("kind") == "scheduled_review":
        latest.setdefault(intent["native_id"], expected)
    if latest.get(intent["native_id"]) != expected:
        intent.update(status="cancelled", cancel_reason="superseded_before_delivery")
        return None
    freshness = next((item.get("freshness") or {} for item in fresh if item["native_id"] == intent["native_id"]), {})
    if intent["kind"] == "catalyst" and freshness.get("class") == "backfill":
        intent.update(status="expired", cancel_reason="backfill_not_immediate")
        if state["delivery"].get("last_candidate_id") == intent["candidate_id"]:
            state["delivery"]["status"] = "expired"
        return None
    if intent["kind"] == "catalyst" and not intent_raw_ok(intent, fresh):
        intent.update(not_before=iso(now + timedelta(minutes=60)), defer_reason="raw_unavailable")
        return None
    allowed = next_notification_time(state, now)
    cooldown = topic_until(state, intent.get("topic_key"), intent["kind"])
    if cooldown and cooldown > allowed:
        allowed = cooldown
    if allowed > now:
        intent["not_before"] = iso(allowed)
        return None
    intent.update(status="attempted", attempted_at=iso(now), channel=channel, attempt_id=uuid.uuid4().hex)
    state["last_notification_request_at"] = iso(now)
    today = state["notification_requests_today"] or {}
    same_day = today.get("date") == now.date().isoformat()
    state["notification_requests_today"] = {"date": now.date().isoformat(),
                                            "count": (int(today.get("count", 0)) + 1) if same_day else 1}
    state["delivery"] = {"status": "attempted", "last_candidate_id": intent["candidate_id"],
                         "last_message_sha256": intent["message_sha256"], "attempt_id": intent["attempt_id"]}
    if intent.get("topic_key"):
        state.setdefault("topics", {})[intent["topic_key"]] = {
            "last_at": iso(now), "priority": intent.get("priority"), "candidate_id": intent["candidate_id"],
            "headline": intent["message"].splitlines()[0][:80]}
    return intent["message"]


def _executions(job_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    path = hermes_home() / "cron" / "executions.db"
    if not path.is_file():
        return []
    # Hermes 的库是 WAL 模式，mode=ro 仍需写共享内存文件，在部分环境会打不开；改为普通连接 + query_only。
    conn = sqlite3.connect(str(path), timeout=2)
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT status, delivery_outcome, error, started_at, finished_at FROM executions "
            "WHERE job_id = ? AND started_at IS NOT NULL", (job_id,)).fetchall()
    finally:
        conn.close()
    result = []
    for status, outcome, error, started, finished in rows:
        try:
            started_at = parse_time(started)
        except ValueError:
            continue
        if start <= started_at <= end:
            result.append({"status": status, "outcome": outcome, "error": error,
                           "started_at": started_at, "finished_at": finished})
    return result


def reconcile_receipts(state: dict[str, Any], now: datetime, delivery_job_id: str | None) -> int:
    """把 attempted 推进到 delivered / failed / not_sent / unverified；只读 Hermes 执行记录。"""
    if not delivery_job_id:
        return 0
    updated = 0
    for intent in state["outbox"].values():
        if intent.get("status") != "attempted" or not intent.get("attempted_at"):
            continue
        attempted = parse_time(intent["attempted_at"])
        try:
            rows = _executions(delivery_job_id, attempted - timedelta(minutes=10), attempted + timedelta(seconds=1))
        except sqlite3.Error as exc:
            add_health(state, "receipt_lookup_failed", str(exc), now)
            return updated
        row = max(rows, key=lambda r: r["started_at"]) if rows else None
        if row is None or row["status"] in {"claimed", "running"}:
            if now - attempted < RECEIPT_GRACE:
                continue
            status, detail = "unverified", "no_execution_record"
        elif row["outcome"] == "delivered":
            status, detail = "delivered", "delivered"
        elif row["status"] == "failed" or row["outcome"] in {"failed", "error"}:
            status, detail = "failed", row["error"] or row["outcome"] or "failed"
        elif row["outcome"] == "suppressed":
            status, detail = "not_sent", "suppressed"
        else:
            status, detail = "unverified", str(row["outcome"])
        intent.update(status=status, receipt_at=iso(now), receipt=detail)
        if state["delivery"].get("last_candidate_id") == intent["candidate_id"]:
            state["delivery"]["status"] = status
        append_audit([{"at": iso(now), "goal_id": CTX.goal_id, "candidate_id": intent["candidate_id"],
                       "status": "receipt", "receipt": status, "detail": detail}])
        updated += 1
    return updated
