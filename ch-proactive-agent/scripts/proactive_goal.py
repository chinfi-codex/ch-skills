#!/usr/bin/env python3
"""AlphaVault 主动目标运行时入口。

Hermes 任务：
- pulse（no-agent，每 2 分钟）：回执对账 → 处理评估输出 → 信号检测与扫描 → 投递；stdout 即唯一可投递内容
- gate（评估任务的预检）：把 pulse 备好的一批候选交给零工具评估模型，没有就 wakeAgent=false
交互与调试：init / scan / dispatch / record / status / control / ack / signal / compact。
AlphaVault 只作为只读数据源；状态、候选包、审计、锁都在运行态目录。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proactive import evaluate, outbox, pulse, runs, sources, validate  # noqa: E402
from proactive.context import CTX, iso, parse_time, short  # noqa: E402
from proactive.lock import locked  # noqa: E402
from proactive.store import (add_health, append_audit, compact, default_state, load_state,  # noqa: E402
                             save_state)

COMPACT_EVERY_HOURS = 6


def emit(payload: dict[str, Any], wake: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps({"wakeAgent": wake}))


def _latest_for(state: dict[str, Any], now: datetime) -> Any:
    return lambda: evaluate.latest_native(sources.collect(state, now)[0], state)


def _maybe_compact(state: dict[str, Any], now: datetime) -> None:
    last = state.get("last_compact_at")
    if not last or parse_time(last) <= now - timedelta(hours=COMPACT_EVERY_HOURS):
        compact(state, now)


def cmd_init(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    with locked(CTX.state_path):
        if CTX.state_path.exists():
            raise ValueError("主动目标已存在；不会覆盖游标")
        state = default_state(now)
        with sources.tick_cache():
            fresh, extra, errors = sources.collect(state, now)
        for item in fresh:
            if item["kind"] == "framework_review":
                continue
            state["candidates"][item["candidate_id"]] = {
                "evidence_hash": item["evidence_hash"], "kind": item["kind"], "native_id": item["native_id"],
                "first_seen_at": iso(now), "last_seen_at": iso(now), "observed_at": item["observed_at"],
                "attempts": 0, "last_action": "baseline", "review_after": None,
                "fingerprint_version": item["fingerprint_version"], "topic_key": item.get("topic_key"),
            }
            if item["kind"] == "scheduled_review":
                state["scheduled_windows"][item["native_id"]] = {"action": "baseline", "at": iso(now)}
        state["framework_versions"] = extra["framework_current"]
        state["initialized"] = True
        state["last_scan_at"] = iso(now)
        state["signals"]["last_full_scan_at"] = iso(now)
        state["source_errors"] = errors
        save_state(state)
        print(json.dumps({"initialized": True, "baseline_candidates": len(state["candidates"]),
                          "source_errors": errors}, ensure_ascii=False, indent=2))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """手动 / 调试用的全量扫描；线上由 pulse 调度。异常一律 fail-closed 为静默。"""
    now = parse_time(args.now)
    try:
        with locked(CTX.state_path, timeout=0.2):
            state = load_state()
            if state["status"] != "active" or not state.get("initialized") or not outbox.is_active_time(state["policy"], now):
                emit({"status": "outside_active_hours", "candidates": []}, False)
                return 0
            with sources.tick_cache():
                items, _extra, errors = evaluate.eligible(state, now)
                evaluate.update_presentation(state, items, now)
                payload = evaluate.wake_payload(items, errors, now, state)
            state["last_scan_at"] = iso(now)
            state["signals"]["last_full_scan_at"] = iso(now)
            state["source_errors"] = errors
            if not args.dry_run:
                save_state(state)
            if errors and not items:
                emit(payload, False)
                print("proactive_goal source errors: " + "；".join(errors), file=sys.stderr)
                return 0
            emit(payload, bool(items))
    except TimeoutError:
        emit({"status": "another_tick_active", "candidates": []}, False)
    except Exception as exc:
        emit({"source_errors": [short(exc, 500)], "candidates": []}, False)
        print(f"proactive_goal source error: {exc}", file=sys.stderr)
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    """评估任务预检：只交出 pulse 已备好的一批，绝不自己扫描。"""
    now = parse_time(args.now)
    try:
        with locked(CTX.state_path):
            state = load_state()
            payload = None
            if state["status"] == "active" and outbox.is_active_time(state["policy"], now):
                payload = pulse.take_batch(state, now)
                save_state(state)
    except Exception as exc:
        emit({"source_errors": [short(exc, 500)], "candidates": []}, False)
        print(f"proactive_goal gate error: {exc}", file=sys.stderr)
        return 0
    if payload is None:
        emit({"status": "no_ready_batch", "candidates": []}, False)
    else:
        emit(payload, True)
    return 0


def _intake(state: dict[str, Any], now: datetime, args: argparse.Namespace) -> None:
    """回执对账，再把已完成的评估输出入账。"""
    outbox.reconcile_receipts(state, now, getattr(args, "delivery_job_id", None))
    runs.process_runs(state, args.evaluation_job_id, now, _latest_for(state, now))


def cmd_dispatch(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    message = None
    with locked(CTX.state_path):
        state = load_state()
        if state["status"] != "active" or not outbox.is_active_time(state["policy"], now):
            return 0
        with sources.tick_cache():
            _intake(state, now, args)
            message = outbox.deliver_due(state, now, args.channel, lambda: sources.collect(state, now))
        state["updated_at"] = iso(now)
        save_state(state)
    if message:
        print(message)
    return 0


def cmd_pulse(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    message = None
    trigger = False
    with locked(CTX.state_path):
        state = load_state()
        if state["status"] != "active" or not state.get("initialized") or not outbox.is_active_time(state["policy"], now):
            return 0
        with sources.tick_cache():
            _intake(state, now, args)
            runs.stale_inflight(state, now, int(state["policy"]["evaluation_lease_minutes"]))
            pulse.detect(state, now)
            keys = pulse.ready(state, now)
            if keys and not state["signals"].get("inflight"):
                if pulse.rate_ok(state, now):
                    items, _extra, errors = evaluate.eligible(state, now)
                    evaluate.update_presentation(state, items, now)
                    state["last_scan_at"] = state["signals"]["last_full_scan_at"] = iso(now)
                    state["source_errors"] = errors
                    for key in keys:
                        state["signals"]["pending"].pop(key, None)
                    if items:
                        pulse.stage_batch(state, evaluate.wake_payload(items, errors, now, state), now)
                        trigger = True
                    if errors:
                        add_health(state, "source_errors", "；".join(errors), now)
                else:
                    add_health(state, "evaluation_rate_limited", ",".join(keys), now)
            _maybe_compact(state, now)
            message = outbox.deliver_due(state, now, args.channel, lambda: sources.collect(state, now))
        state["updated_at"] = iso(now)
        save_state(state)
    if trigger:
        ok, detail = pulse.trigger_evaluation(args.evaluation_job_id)
        if not ok:
            with locked(CTX.state_path):
                state = load_state()
                add_health(state, "evaluation_trigger_failed", detail, now)
                save_state(state)
    if message:
        print(message)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    raw = Path(args.decision_file).read_text(encoding="utf-8") if args.decision_file != "-" else sys.stdin.read()
    with locked(CTX.state_path):
        state = load_state()
        decisions = validate.parse_decisions(raw)
        with sources.tick_cache():
            fresh, _extra, _errors = sources.collect(state, now)
            outcomes = validate.record_batch(state, decisions, now, "manual-" + uuid.uuid4().hex,
                                             evaluate.latest_native(fresh, state))
        print(json.dumps({"outcomes": outcomes, "note": "仅记录意图；由 pulse 决定是否产生可投递内容"}, ensure_ascii=False))
    return 0


def _counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        result[value] = result.get(value, 0) + 1
    return result


def cmd_status(args: argparse.Namespace) -> int:
    with locked(CTX.state_path, exclusive=False):
        state = load_state()
    pending = [{key: intent.get(key) for key in ("candidate_id", "native_id", "kind", "priority", "topic_key",
                                                  "not_before", "valid_until", "defer_reason")}
               for intent in state["outbox"].values() if intent.get("status") == "pending"]
    print(json.dumps({
        "goal_id": CTX.goal_id, "workspace": str(CTX.root), "runtime": str(CTX.runtime), "status": state["status"],
        "initialized": state["initialized"], "last_scan_at": state.get("last_scan_at"),
        "pending_notifications": pending,
        "candidate_counts": _counts(list(state["candidates"].values()), "last_action"),
        "indexed_candidates": len(state.get("_index") or {}),
        "outbox_counts": _counts(list(state["outbox"].values()), "status"),
        "processed_run_counts": _counts(list(state["processed_runs"].values()), "status"),
        "topics": state.get("topics"), "signals": {k: state["signals"].get(k) for k in ("pending", "inflight", "last_full_scan_at")},
        "health": (state.get("health") or [])[-10:],
        "policy": state["policy"], "last_notification_request_at": state.get("last_notification_request_at"),
        "notification_requests_today": state["notification_requests_today"], "delivery": state["delivery"],
        "source_errors": state["source_errors"]}, ensure_ascii=False, indent=2))
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    with locked(CTX.state_path):
        state = load_state()
        if args.action == "pause":
            state["status"] = "paused"
        elif args.action == "resume":
            state["status"] = "active"
            state["mute_until"] = None
        elif args.action == "schedule":
            if args.heartbeat_minutes < 5:
                raise ValueError("heartbeat_minutes 不得小于 5 分钟")
            for value in (args.active_start, args.active_end):
                datetime.strptime(value, "%H:%M")
            if args.active_start == args.active_end:
                raise ValueError("active_start 与 active_end 不能相同")
            state["policy"].update(heartbeat_minutes=args.heartbeat_minutes, active_start=args.active_start,
                                   active_end=args.active_end, quiet_start=args.active_end, quiet_end=args.active_start)
        elif args.action == "freshness":
            if args.trend_max_age_days < 1 or args.pending_ttl_hours < 1:
                raise ValueError("趋势数据期限和待发有效期必须为正数")
            state["policy"].update(max_trend_age_days=args.trend_max_age_days,
                                   pending_notification_ttl_hours=args.pending_ttl_hours)
        else:
            if not 0 < args.mute_hours <= 168:
                raise ValueError("mute_hours 必须在 (0, 168] 小时内")
            state["mute_until"] = iso(now + timedelta(hours=args.mute_hours))
        state["updated_at"] = iso(now)
        save_state(state)
        print(json.dumps({"status": state["status"], "mute_until": state["mute_until"]}, ensure_ascii=False))
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    with locked(CTX.state_path):
        state = load_state()
        entry = state["candidates"].get(args.candidate_id)
        if not entry:
            raise ValueError("找不到活跃候选（可能已归档为终态）")
        entry.update(last_action="acknowledged", review_after=None, lease_until=None)
        state["updated_at"] = iso(now)
        append_audit([{"at": iso(now), "goal_id": CTX.goal_id, "candidate_id": args.candidate_id,
                       "action": "acknowledged", "note": short(args.note, 500)}])
        save_state(state)
        print(json.dumps({"acknowledged": args.candidate_id}, ensure_ascii=False))
    return 0


def cmd_signal(args: argparse.Namespace) -> int:
    """长任务 Skill 收尾或手动调用：只追加一条信号，不扫描、不唤醒模型。"""
    row = pulse.append_signal(args.source, args.ref, args.reason, parse_time(args.now))
    print(json.dumps({"queued": row}, ensure_ascii=False))
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    with locked(CTX.state_path):
        state = load_state()
        stats = compact(state, now)
        save_state(state)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {"init": cmd_init, "scan": cmd_scan, "gate": cmd_gate, "pulse": cmd_pulse, "dispatch": cmd_dispatch,
                "record": cmd_record, "status": cmd_status, "control": cmd_control, "ack": cmd_ack,
                "signal": cmd_signal, "compact": cmd_compact}
    for name, func in commands.items():
        p = sub.add_parser(name)
        p.add_argument("--now", help="测试用 ISO 时间")
        p.set_defaults(func=func)
        if name == "scan":
            p.add_argument("--dry-run", action="store_true")
        if name in {"dispatch", "pulse"}:
            p.add_argument("--evaluation-job-id", required=True)
            p.add_argument("--delivery-job-id")
            p.add_argument("--channel", choices=["local", "weixin"], required=True)
        if name == "record":
            p.add_argument("--decision-file", default="-")
        if name == "control":
            p.add_argument("action", choices=["pause", "resume", "mute", "schedule", "freshness"])
            p.add_argument("--mute-hours", type=float, default=12)
            p.add_argument("--heartbeat-minutes", type=int, default=30)
            p.add_argument("--active-start", default="06:00")
            p.add_argument("--active-end", default="23:00")
            p.add_argument("--trend-max-age-days", type=int, default=3)
            p.add_argument("--pending-ttl-hours", type=int, default=24)
        if name == "ack":
            p.add_argument("candidate_id")
            p.add_argument("--note", required=True)
        if name == "signal":
            p.add_argument("--source", required=True, help="skill:<名称> / manual / sources.yaml 中的源名")
            p.add_argument("--ref")
            p.add_argument("--reason")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except (ValueError, OSError, json.JSONDecodeError, subprocess.TimeoutExpired, TimeoutError) as exc:
        print(f"proactive_goal: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
