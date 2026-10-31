"""评估输出校验：逐项处理，格式与边界字段由代码确定性修正，模型的判断不改写。

- 单项不合格只作废该项并计入该候选的 attempts，同批其他项照常入账；
- 修正（夹紧 defer、忽略多余 review_after、多条 notify 只留最高优先级）都写进审计 repairs；
- 涉及外发安全的问题（附件、控制标记）不修正，直接作废该项。
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta
from typing import Any

from proactive_config import read_json

from . import outbox, render
from .context import CTX, FINAL_ACTIONS, NOTIFY_ACTIONS, PRIORITY_RANK, iso, parse_time, short
from .store import append_audit, lookup, save_state

ACTIONS = {"silent", "defer", *NOTIFY_ACTIONS}
RETRY_MINUTES = 60


class ItemError(ValueError):
    pass


def parse_decisions(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    payload = json.loads(text)
    rows = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("最终响应必须是含非空 decisions 数组的 JSON 对象")
    return rows


def validate_message(message: str) -> None:
    if not message or len(message) > 2000:
        raise ItemError("通知必须是非空且不超过 2000 字的纯文本")
    if re.search(r"(?im)^\s*(?:MEDIA|ATTACHMENT|FILE)\s*:", message) or "file://" in message.lower():
        raise ItemError("通知禁止本地文件或附件引用，避免文件外发")
    if "[SILENT]" in message or "[CRON_FAILURE]" in message:
        raise ItemError("通知不能包含 Hermes 控制标记")
    last = next((line.strip() for line in reversed(message.splitlines()) if line.strip()), "")
    try:
        gate = json.loads(last)
    except json.JSONDecodeError:
        gate = None
    if isinstance(gate, dict) and gate.get("wakeAgent") is False:
        raise ItemError("通知末行不能伪装成 wakeAgent 控制信号")


def _demote_extra_notifications(rows: list[Any]) -> dict[int, str]:
    """一批只放行一条提醒：保留优先级最高（同级取先出现）的一条，其余改为延期复核。"""
    notify = [(PRIORITY_RANK.get(str(row.get("priority") or "P2"), 2), i) for i, row in enumerate(rows)
              if isinstance(row, dict) and row.get("action") in NOTIFY_ACTIONS]
    if len(notify) <= 1:
        return {}
    keep = min(notify)[1]
    return {i: "multi_notify_demoted" for _, i in notify if i != keep}


def _bounded_retry(now: datetime, valid_until: str | None, minutes: int = RETRY_MINUTES) -> str | None:
    target = now + timedelta(minutes=minutes)
    if valid_until and parse_time(valid_until) < target:
        return None
    return iso(target)


def _apply(state: dict[str, Any], row: Any, now: datetime, run_key: str, latest: dict[str, str],
           demoted: bool) -> tuple[str, dict[str, Any] | None, list[str]]:
    if not isinstance(row, dict):
        raise ItemError("决策必须是对象")
    repairs: list[str] = []
    cid = str(row.get("candidate_id") or "")
    entry = state["candidates"].get(cid)
    if entry is None:
        known = lookup(state, cid)
        if known is not None:
            return "skipped_final", None, ["candidate_already_final"]
        raise ItemError(f"未登记的候选：{cid}")
    if entry.get("last_action") in FINAL_ACTIONS:
        return "skipped_final", None, ["candidate_already_final"]
    if row.get("evidence_hash") != entry.get("evidence_hash"):
        raise ItemError(f"候选证据版本不匹配：{cid}")
    if latest.get(entry["native_id"]) != entry["evidence_hash"]:
        return "skipped_superseded", None, ["candidate_superseded"]
    action = row.get("action")
    if action not in ACTIONS:
        raise ItemError("未知 action")
    priority = row.get("priority") if row.get("priority") in PRIORITY_RANK else None
    valid_until = entry.get("valid_until")
    review_after = row.get("review_after")

    if demoted:
        repairs.append("multi_notify_demoted")
        review_after = _bounded_retry(now, valid_until, 30)
        action = "defer" if review_after else "silent"
    if action in NOTIFY_ACTIONS:
        blocked = None
        if entry.get("freshness_class") == "backfill":
            blocked = "backfill_notify_blocked"
        elif valid_until and parse_time(valid_until) < now:
            blocked = "expired_notify_blocked"
        if blocked:
            repairs.append(blocked)
            action, review_after = "silent", None
        elif entry["kind"] == "catalyst" and not entry.get("raw_available", True):
            repairs.append("raw_missing_notify_deferred")
            review_after = _bounded_retry(now, valid_until)
            action = "defer" if review_after else "silent"
    if action != "defer" and review_after and "multi_notify_demoted" not in repairs \
            and "raw_missing_notify_deferred" not in repairs:
        repairs.append("review_after_ignored")
        review_after = None
    if action == "defer":
        if not review_after or parse_time(review_after) <= now:
            repairs.append("review_after_defaulted")
            review_after = iso(now + timedelta(minutes=RETRY_MINUTES))
        if entry["kind"] == "catalyst" and not entry.get("raw_available", True):
            review_after = iso(max(parse_time(review_after), now + timedelta(minutes=RETRY_MINUTES)))
        if valid_until and parse_time(review_after) > parse_time(valid_until):
            if parse_time(valid_until) > now + timedelta(minutes=5):
                repairs.append("defer_clamped_to_valid_until")
                review_after = valid_until
            else:
                repairs.append("defer_past_validity_silenced")
                action, review_after = "silent", None

    message = None
    if action in NOTIFY_ACTIONS:
        packet = read_json(CTX.runtime / entry["packet_path"]) if entry.get("packet_path") else {}
        if row.get("brief") is not None:
            try:
                message = render.render_brief(entry["kind"], action, priority, row["brief"],
                                              packet.get("observed_at") or entry.get("observed_at"))
            except render.BriefError as exc:
                if not row.get("message"):
                    raise ItemError(str(exc)) from exc
                repairs.append("brief_invalid_used_message")
        if message is None:
            message = short(row.get("message"), 2000)
        validate_message(message)
        message, notes = render.scrub(message)
        repairs.extend(notes)
        validate_message(message)

    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    entry.update(lease_until=None, updated_at=iso(now), review_after=review_after if action == "defer" else None,
                 last_action=action)
    if priority:
        entry["priority"] = priority
    outcome = action
    if entry["kind"] == "framework_change" and action != "defer":
        packet = read_json(CTX.runtime / entry["packet_path"])
        new_hash = packet["evidence"].get("current_sha256")
        if new_hash:
            state["framework_versions"][entry["native_id"]] = new_hash
        else:
            state["framework_versions"].pop(entry["native_id"], None)
    if entry["kind"] == "scheduled_review" and action != "defer":
        state["scheduled_windows"][entry["native_id"]] = {"action": action, "at": iso(now)}
    if action in NOTIFY_ACTIONS:
        source_version = entry["evidence_hash"]
        if entry["kind"] == "framework_change":
            source_version = read_json(CTX.runtime / entry["packet_path"])["evidence"].get("current_sha256")
        outcome, _intent_id = outbox.prepare_intent(state, entry, cid, message, priority, now, run_key, source_version)
    audit = {"at": iso(now), "goal_id": CTX.goal_id, "candidate_id": cid, "action": action, "outcome": outcome,
             "priority": priority, "topic_key": entry.get("topic_key"), "evidence_hash": entry["evidence_hash"],
             "run_key": run_key, "reason": short(row.get("reason"), 500), "review_after": review_after,
             "missing_inputs": row.get("missing_inputs") or [], "falsifier": short(row.get("falsifier"), 500),
             "repairs": repairs}
    return outcome, audit, repairs


def record_batch(input_state: dict[str, Any], decisions: list[Any], now: datetime,
                 run_key: str, latest: dict[str, str]) -> list[dict[str, Any]]:
    limit = int(input_state["policy"]["max_candidates_per_wake"])
    if not decisions or len(decisions) > limit * 2:
        raise ValueError("决策数量异常，整批拒绝")
    state = copy.deepcopy(input_state)
    demoted = _demote_extra_notifications(decisions)
    audits: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for i, row in enumerate(decisions):
        cid = str(row.get("candidate_id") or "") if isinstance(row, dict) else ""
        try:
            outcome, audit, repairs = _apply(state, row, now, run_key, latest, i in demoted)
        except ItemError as exc:
            entry = state["candidates"].get(cid)
            if entry is not None and entry.get("last_action") not in FINAL_ACTIONS:
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
                entry["lease_until"] = None
            audits.append({"at": iso(now), "goal_id": CTX.goal_id, "candidate_id": cid or None,
                           "status": "invalid_item", "run_key": run_key, "error": short(exc, 500)})
            outcomes.append({"candidate_id": cid or None, "outcome": "invalid", "error": short(exc, 200)})
            continue
        if audit:
            audits.append(audit)
        outcomes.append({"candidate_id": cid, "outcome": outcome, **({"repairs": repairs} if repairs else {})})
    state["updated_at"] = iso(now)
    save_state(state)
    input_state.clear()
    input_state.update(state)
    append_audit(audits)
    return outcomes


def charge_presented(state: dict[str, Any], candidate_ids: list[str], now: datetime, reason: str) -> None:
    """整份响应无法解析时，给这一批呈现过的候选各计一次失败，避免无限重试。"""
    for cid in candidate_ids:
        entry = state["candidates"].get(cid)
        if entry is None or entry.get("last_action") in FINAL_ACTIONS:
            continue
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["lease_until"] = None
    append_audit([{"at": iso(now), "goal_id": CTX.goal_id, "status": "batch_charged", "reason": reason,
                   "candidate_ids": candidate_ids}])
