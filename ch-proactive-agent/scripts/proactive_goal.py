#!/usr/bin/env python3
"""AlphaVault 主动目标的无模型运行时。

评估 Agent 没有工具，只输出结构化决策。dispatch 由 Hermes no-agent 任务运行，
读取它的本地运行记录、校验并保存决策，最后只把合格的通知意图交给 Hermes。
本文件不调用长任务 Skill，不生成投研结论，不直接调用任何消息 API。

AlphaVault 只作为只读数据源（config/goals.yaml 的 vault_root）；状态、候选包、
决策审计与锁都在运行态目录（runtime_home，默认 ~/AlphaData/proactive）。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from proactive_config import SKILL_DIR, atomic_write_json, file_hash, load_settings, read_json, read_jsonl

SETTINGS = load_settings()
ROOT = SETTINGS.vault_root
RUNTIME = SETTINGS.runtime_home
STATE = RUNTIME / "state.json"
SCHEMA = SKILL_DIR / "config" / "schemas" / "state.schema.json"
AUDIT = RUNTIME / "decisions.jsonl"
PACKETS = RUNTIME / "packets"
EVENTS = ROOT / SETTINGS.inputs["catalyst_events"]
BOARD = ROOT / SETTINGS.inputs["industry_board"]
BOARD_HISTORY = ROOT / SETTINGS.inputs["industry_board_history"]
BOARD_FRAMEWORK = SETTINGS.inputs["board_review_framework"]
TREND_TOOL = ROOT / SETTINGS.inputs["trend_tool"]
RULES_DIR = SETTINGS.rules_dir
GOAL_ID = SETTINGS.goal_id
SCHEMA_VERSION = "proactive-goal/v2"
LOCAL_TZ = timezone(timedelta(hours=8))
SILENT = "[SILENT]"
FINAL_ACTIONS = {"silent", "notify", "needs_user", "baseline", "acknowledged", "superseded", "dead", "backfill"}
NOTIFY_ACTIONS = {"notify", "needs_user"}
FINGERPRINT_VERSION = 2
CATALYST_MAX_AGE_DAYS = int(SETTINGS.policy.get("catalyst_max_age_days", 7))
CATALYST_SEMANTIC_FIELDS = (
    "event_id", "event_date", "ingest_date", "rating", "industry", "title", "description",
    "delta_vs_baseline", "hard_fields", "rating_reason", "negative_chain", "source_refs",
    "related_pages", "framework_impacts",
)
FRAMEWORK_FILES = list(SETTINGS.framework_files)
DEFAULT_POLICY = dict(SETTINGS.policy)


@contextlib.contextmanager
def locked(path: Path, *, timeout: float = 20.0, exclusive: bool = True):
    """在本机运行态目录持锁，进程退出由 OS 释放，避免云盘 stale 清理竞态。"""
    lock_root = RUNTIME / "locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / (hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest() + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"另一主动目标进程仍持有状态锁：{lock_path}")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def parse_time(value: str | None = None) -> datetime:
    if not value:
        return datetime.now(LOCAL_TZ)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    result = datetime.fromisoformat(text)
    if result.tzinfo is None:
        result = result.replace(tzinfo=LOCAL_TZ)
    return result.astimezone(LOCAL_TZ)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short(value: Any, limit: int = 600) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def default_state(now: datetime) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "goal_id": GOAL_ID, "workspace": ".",
        "status": "active", "created_at": iso(now), "updated_at": iso(now),
        "initialized": False, "policy": copy.deepcopy(DEFAULT_POLICY),
        "framework_versions": {}, "candidates": {}, "scheduled_windows": {},
        "outbox": {}, "processed_runs": {}, "last_scan_at": None,
        "last_notification_request_at": None,
        "notification_requests_today": {"date": now.date().isoformat(), "count": 0},
        "mute_until": None,
        "delivery": {"status": "idle", "last_candidate_id": None, "last_message_sha256": None},
        "source_errors": [],
    }


def load_state() -> dict[str, Any]:
    state = read_json(STATE)
    if not isinstance(state, dict) or state.get("goal_id") != GOAL_ID:
        raise ValueError("主动目标状态缺失或 goal_id 不匹配；先运行 init")
    if state.get("schema_version") == "proactive-goal/v1":
        # v1 只在 local shadow 运行过；保留游标，不把 prepared 当成可重投的意图。
        state["schema_version"] = SCHEMA_VERSION
        state["outbox"] = {}
        state["processed_runs"] = {}
        state["delivery"] = {"status": "idle", "last_candidate_id": None, "last_message_sha256": None}
        state["last_notification_request_at"] = None
        state["notification_requests_today"] = {"date": datetime.now(LOCAL_TZ).date().isoformat(), "count": 0}
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("不支持的主动目标状态版本")
    for key in ("policy", "candidates", "outbox", "processed_runs", "framework_versions"):
        if not isinstance(state.get(key), dict):
            raise ValueError(f"主动目标状态缺少 {key}")
    state["policy"] = {**DEFAULT_POLICY, **state["policy"]}
    if state.get("status") not in {"active", "paused"}:
        raise ValueError("status 必须是 active 或 paused")
    if SCHEMA.is_file():
        import jsonschema

        jsonschema.validate(state, json.loads(SCHEMA.read_text(encoding="utf-8")))
    return state


def candidate(kind: str, native_id: str, observed_at: str, evidence: dict[str, Any],
              fingerprint_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    fingerprint = fingerprint_evidence or {"kind": kind, "native_id": native_id, "evidence": evidence}
    evidence_hash = digest(fingerprint)
    return {"candidate_id": f"PG-{evidence_hash[:16]}", "kind": kind, "native_id": native_id,
            "observed_at": observed_at, "evidence_hash": evidence_hash,
            "fingerprint_version": FINGERPRINT_VERSION, "evidence": evidence}


def framework_candidates(state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    previous = state.get("framework_versions") or {}
    paths = [ROOT / rel for rel in FRAMEWORK_FILES]
    paths.extend(sorted((ROOT / RULES_DIR).glob("R-*.md")))
    current = {path.relative_to(ROOT).as_posix(): file_hash(path, length=None) for path in paths}
    missing = sorted(set(previous) - set(current))
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for rel in FRAMEWORK_FILES:
        if rel not in current:
            errors.append(f"必需方法论文件缺失：{rel}")
    for rel in sorted(set(current) | set(missing)):
        old_hash = previous.get(rel)
        new_hash = current.get(rel)
        if not state.get("initialized") or old_hash == new_hash:
            continue
        change_type = "added" if old_hash is None else "deleted" if new_hash is None else "modified"
        excerpt = ""
        if new_hash:
            excerpt = (ROOT / rel).read_text(encoding="utf-8-sig")[:3500]
        results.append(candidate("framework_change", rel, datetime.now(LOCAL_TZ).date().isoformat(), {
            "path": rel, "change_type": change_type, "previous_sha256": old_hash,
            "current_sha256": new_hash, "current_excerpt": excerpt,
            "note": "比较目标和规则含义；排版变化或无法判断语义时保持静默，不把变更本身当成市场事件。",
        }))
    return results, current, errors


def trading_candidate(now: datetime, policy: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    proc = subprocess.run(
        [sys.executable, str(TREND_TOOL), "--date", now.date().isoformat(), "--json"],
        cwd=ROOT, text=True, capture_output=True, timeout=60, check=False,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    if proc.returncode:
        return None, [f"trend_state 读取失败：{short(proc.stderr, 400)}"]
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, [f"trend_state 返回非法 JSON：{exc}"]
    ref_date = datetime.strptime(data["ref_date"], "%Y-%m-%d").date()
    age = (now.date() - ref_date).days
    if age < 0 or age > int(policy["max_trend_age_days"]):
        return None, [f"trend_state 数据过期：ref_date={ref_date}"]
    rule_texts: dict[str, str] = {}
    for row in data.get("arming") or []:
        for rule_id in re.findall(r"R-\d{3}", str(row.get("rule") or "")):
            paths = list((ROOT / RULES_DIR).glob(f"{rule_id}-*.md"))
            if paths:
                rule_texts[rule_id] = paths[0].read_text(encoding="utf-8-sig")[:5000]
    previous = (data.get("history") or [])[-2:-1]
    evidence = {
        "plan_date": data["plan_date"], "ref_date": data["ref_date"], "state": data["state"],
        "phase": data.get("phase"), "position": data.get("position"), "counters": data.get("counters"),
        "ref_day": data.get("ref_day"), "phase_score": data.get("phase_score"),
        "state_note": data.get("state_note"), "exit_progress": data.get("exit_progress"),
        "arming": data.get("arming"), "previous_state": previous[0] if previous else None,
        "rule_texts": rule_texts,
        "note": "既定 trend_state 和 R-XXX 原文；集中度轴、领导股、盘中承接和实际持仓另行核验。",
    }
    return candidate("trend_state", ref_date.isoformat(), ref_date.isoformat(), evidence), []


def raw_status(raw_path: str) -> dict[str, Any]:
    """只确认来源是否在库内可定位，不读正文；正文留到投递前定向复核。"""
    path = Path(raw_path)
    resolved = path if path.is_absolute() else ROOT / path
    try:
        resolved = resolved.resolve()
        resolved.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        return {"path": raw_path, "state": "outside_scope"}
    if not resolved.is_file():
        return {"path": raw_path, "state": "missing"}
    if resolved.suffix.lower() == ".pdf":
        extracted = next((candidate for candidate in (
            resolved.with_suffix(".extracted.md"),
            resolved.with_name(resolved.name + ".extracted.md"),
        ) if candidate.is_file()), None)
        if extracted is None:
            return {"path": raw_path, "state": "no_text_extract"}
        return {"path": raw_path, "state": "ok", "bytes": extracted.stat().st_size, "via": "pdf_text"}
    return {"path": raw_path, "state": "ok", "bytes": resolved.stat().st_size}


def raw_excerpt(raw_path: str, limit: int = 3000) -> dict[str, Any]:
    """只在投递前定向复核时读取正文；路径、PDF 文本和大小都受限。"""
    path = Path(raw_path)
    resolved = path if path.is_absolute() else ROOT / path
    try:
        resolved = resolved.resolve()
        resolved.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        return {"path": raw_path, "error": "路径不在 AlphaVault 数据范围内"}
    if not resolved.is_file():
        return {"path": raw_path, "error": "Raw 文件不存在"}
    if resolved.suffix.lower() == ".pdf":
        extracted = next((candidate for candidate in (
            resolved.with_suffix(".extracted.md"),
            resolved.with_name(resolved.name + ".extracted.md"),
        ) if candidate.is_file()), None)
        if extracted is None:
            return {"path": raw_path, "error": "PDF 尚无可用文本提取，证据降级"}
        resolved = extracted.resolve()
        try:
            resolved.relative_to(ROOT.resolve())
        except ValueError:
            return {"path": raw_path, "error": "PDF 提取文本不在 AlphaVault 数据范围内"}
    try:
        with resolved.open("r", encoding="utf-8-sig", errors="replace") as handle:
            return {"path": raw_path, "excerpt": handle.read(limit)}
    except OSError:
        return {"path": raw_path, "error": "Raw 暂时无法读取，证据降级"}


def semantic_diff(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    changes = []
    for key in CATALYST_SEMANTIC_FIELDS:
        old, new = previous.get(key), current.get(key)
        if old == new:
            continue
        changes.append({"field": key, "previous": short(json.dumps(old, ensure_ascii=False, sort_keys=True), 240),
                        "current": short(json.dumps(new, ensure_ascii=False, sort_keys=True), 240)})
    return changes


def attach_prior_version(state: dict[str, Any], item: dict[str, Any]) -> None:
    """给已呈现候选附上一版差异；只读该候选自己的旧包，不重读全量。"""
    priors = [row for cid, row in state["candidates"].items()
              if row.get("native_id") == item["native_id"] and cid != item["candidate_id"]
              and row.get("evidence_hash") != item["evidence_hash"] and row.get("packet_path")]
    if not priors:
        return
    prior = max(priors, key=lambda row: row.get("last_seen_at") or "")
    try:
        old = read_json(RUNTIME / prior["packet_path"])
    except (OSError, ValueError, json.JSONDecodeError):
        return
    changes = semantic_diff(old.get("evidence") or {}, item["evidence"])
    if changes:
        item["prior_version"] = {"candidate_id": prior["evidence_hash"][:16],
                                 "last_seen_at": prior.get("last_seen_at"), "changes": changes}


def catalyst_freshness(event_date: str, updated_at: Any, raw_checks: list[dict[str, Any]],
                       has_sources: bool, now: datetime) -> dict[str, Any]:
    observed = datetime.strptime(event_date, "%Y-%m-%d").date()
    age = (now.date() - observed).days
    valid_until = datetime.combine(observed + timedelta(days=CATALYST_MAX_AGE_DAYS),
                                   datetime.min.time(), tzinfo=LOCAL_TZ) + timedelta(hours=23)
    return {
        "class": "current" if 0 <= age <= CATALYST_MAX_AGE_DAYS else "backfill",
        "age_days": age, "event_date": event_date, "evidence_updated_at": updated_at,
        "valid_until": iso(valid_until),
        "raw_available": has_sources and all(row.get("state") == "ok" for row in raw_checks),
        "raw_states": [row.get("state") for row in raw_checks],
    }


def catalyst_candidates(now: datetime) -> list[dict[str, Any]]:
    rows = [row for row in read_jsonl(EVENTS, strict=True) if row.get("status") in {None, "active"}]
    by_industry: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_industry.setdefault(str(row.get("industry") or ""), []).append(row)
    for group in by_industry.values():
        group.sort(key=lambda row: str(row.get("event_date") or ""), reverse=True)
    result = []
    for row in rows:
        event_id = str(row.get("event_id") or "")
        if not event_id:
            raise ValueError("催化事件缺少 event_id")
        refs = row.get("source_refs") or []
        raw_checks = [raw_status(str(ref.get("raw_path") or "")) for ref in refs[:2]]
        related = [
            {"event_id": peer.get("event_id"), "event_date": peer.get("event_date"),
             "rating": peer.get("rating"), "title": short(peer.get("event_title"), 110),
             "delta_vs_baseline": short(peer.get("delta_vs_baseline"), 160)}
            for peer in by_industry.get(str(row.get("industry") or ""), [])
            if peer.get("event_id") != event_id
        ][:3]
        evidence = {
            "event_id": event_id, "event_date": row.get("event_date"), "ingest_date": row.get("ingest_date"),
            "rating": row.get("rating"), "industry": row.get("industry"),
            "title": short(row.get("event_title"), 350), "description": short(row.get("event_description"), 800),
            "delta_vs_baseline": short(row.get("delta_vs_baseline"), 700),
            "hard_fields": row.get("hard_fields"), "rating_reason": short(row.get("rating_reason"), 500),
            "negative_chain": row.get("negative_chain"), "source_refs": refs,
            "related_pages": row.get("related_pages"), "framework_impacts": row.get("framework_impacts"),
            "updated_at": row.get("updated_at"), "update_history": row.get("update_history"),
            "raw_status": raw_checks, "industry_baseline": related,
            "note": "脚本只给本条增量和精简存量索引；是否需要对比、对照哪一版由评估者决定。",
        }
        observed_at = str(row.get("event_date") or now.date())
        semantic = {key: evidence.get(key) for key in CATALYST_SEMANTIC_FIELDS}
        item = candidate("catalyst", event_id, observed_at, evidence,
                         {"kind": "catalyst", "native_id": event_id, "evidence": semantic})
        item["freshness"] = catalyst_freshness(observed_at, row.get("updated_at"), raw_checks, bool(refs), now)
        item["valid_until"] = item["freshness"]["valid_until"]
        result.append(item)
    return result


def board_candidate(now: datetime) -> dict[str, Any] | None:
    board = read_json(BOARD)
    version = datetime.strptime(board["version"], "%Y-%m-%d").date()
    due = version + timedelta(days=30)
    if now.date() <= due:
        return None
    reviews = [row for row in read_jsonl(BOARD_HISTORY, strict=False)
               if row.get("type") == "framework_review" and str(row.get("version") or "") == board["version"]]
    if reviews:
        return None
    excerpt = (ROOT / BOARD_FRAMEWORK).read_text(encoding="utf-8-sig")
    return candidate("framework_review", f"industry-board:{board['version']}", board["version"], {
        "path": "ledgers/industry-board/board.json", "version": board["version"],
        "review_due": due.isoformat(), "overdue_days": (now.date() - due).days,
        "framework": BOARD_FRAMEWORK, "review_excerpt": excerpt[-5000:],
        "note": "这是框架体检到期，不是产业多空结论；不得把旧 stance 当成最新事实。",
    })


def scheduled_candidate(now: datetime, state: dict[str, Any], trend: dict[str, Any] | None) -> dict[str, Any] | None:
    if now.weekday() >= 5:
        return None
    slots = [slot for slot in state["policy"]["scheduled_reviews"] if slot <= now.strftime("%H:%M")]
    if not slots:
        return None
    slot = slots[-1]
    native_id = f"{now.date()}:{slot}"
    if native_id in (state.get("scheduled_windows") or {}):
        return None
    return candidate("scheduled_review", native_id, now.date().isoformat(), {
        "window": slot, "supporting_trend": trend["evidence"] if trend else None,
        "note": "固定复核窗口不是必须发消息；无新差距就静默，缺数据不启动长任务。",
    })


def collect(state: dict[str, Any], now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    framework, current_framework, errors = framework_candidates(state)
    trend, trend_errors = trading_candidate(now, state["policy"])
    errors.extend(trend_errors)
    fresh = list(framework)
    if trend:
        fresh.append(trend)
    fresh.extend(catalyst_candidates(now))
    board = board_candidate(now)
    if board:
        fresh.append(board)
    scheduled = scheduled_candidate(now, state, trend)
    if scheduled:
        fresh.append(scheduled)
    fresh = [item for item in fresh if SETTINGS.source_enabled(item["kind"])]
    rank = SETTINGS.source_rank()
    fresh.sort(key=lambda item: (rank.get(item["kind"], 99), item["observed_at"]), reverse=True)
    return fresh, {"framework_current": current_framework}, errors


def packet_path(cid: str) -> Path:
    if not re.fullmatch(r"PG-[0-9a-f]{16}", cid):
        raise ValueError("非法 candidate_id")
    return PACKETS / f"{cid}.json"


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
        path = (RUNTIME / rel).resolve()
        try:
            path.relative_to(RUNTIME.resolve())
            item = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"延期候选包缺失、损坏或越界：{cid} ({rel})") from exc
        if item.get("candidate_id") != cid or item.get("evidence_hash") != entry.get("evidence_hash"):
            raise ValueError(f"延期候选包版本不匹配：{cid}")
        result.append(item)
    return result


def supersede_old_versions(state: dict[str, Any], latest: dict[str, str], now: datetime) -> None:
    by_id = state["candidates"]
    for cid, entry in by_id.items():
        new_hash = latest.get(entry.get("native_id"))
        if new_hash and new_hash != entry.get("evidence_hash") and entry.get("last_action") not in FINAL_ACTIONS:
            entry["last_action"] = "superseded"
            entry["superseded_at"] = iso(now)
    for intent in state["outbox"].values():
        if intent.get("status") != "pending":
            continue
        entry = by_id.get(intent["candidate_id"], {})
        new_hash = latest.get(intent.get("native_id"))
        if new_hash and new_hash != intent.get("evidence_hash"):
            intent["status"] = "cancelled"
            intent["cancel_reason"] = "superseded_by_new_evidence"


def migrate_fingerprint_versions(state: dict[str, Any], fresh: list[dict[str, Any]]) -> None:
    """一次性迁移旧的全包哈希；历史基线不因算法升级而重新报警。"""
    for item in fresh:
        entry = state["candidates"].get(item["candidate_id"])
        if not entry or entry.get("fingerprint_version") == FINGERPRINT_VERSION:
            continue
        if entry.get("last_action") in FINAL_ACTIONS or item["native_id"] in {
            row["native_id"] for row in fresh
        }:
            entry["evidence_hash"] = item["evidence_hash"]
            entry["fingerprint_version"] = FINGERPRINT_VERSION


def update_presentation(state: dict[str, Any], items: list[dict[str, Any]], now: datetime) -> None:
    for item in items:
        cid = item["candidate_id"]
        entry = state["candidates"].setdefault(cid, {
            "evidence_hash": item["evidence_hash"], "kind": item["kind"], "native_id": item["native_id"],
            "first_seen_at": iso(now), "attempts": 0, "last_action": "pending", "review_after": None,
        })
        freshness = item.get("freshness") or {}
        if "valid_until" not in entry or entry["valid_until"] is None:
            entry["valid_until"] = item.get("valid_until") or iso(now + timedelta(hours=int(state["policy"]["pending_notification_ttl_hours"])))
        entry.update(evidence_hash=item["evidence_hash"], last_seen_at=iso(now),
                     fingerprint_version=item.get("fingerprint_version", FINGERPRINT_VERSION),
                     freshness_class=freshness.get("class", "current"),
                     raw_available=freshness.get("raw_available", True),
                     observed_at=item["observed_at"], packet_path=packet_path(cid).relative_to(RUNTIME).as_posix())
        if entry.get("last_action") == "pending":
            entry["lease_until"] = iso(now + timedelta(minutes=int(state["policy"]["evaluation_lease_minutes"])))
        if item["kind"] == "catalyst":
            attach_prior_version(state, item)
        atomic_write_json(packet_path(cid), item)


def eligible(state: dict[str, Any], now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    fresh, extra, errors = collect(state, now)
    migrate_fingerprint_versions(state, fresh)
    if not errors:
        supersede_old_versions(state, latest_native(fresh), now)
    items = []
    for item in rehydrate(state, now, fresh):
        entry = state["candidates"].get(item["candidate_id"])
        if (item.get("freshness") or {}).get("class") == "backfill":
            if not entry or entry.get("last_action") not in FINAL_ACTIONS:
                state["candidates"][item["candidate_id"]] = {
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
                append_audit([{"at": iso(now), "goal_id": GOAL_ID, "candidate_id": item["candidate_id"],
                               "status": "dead", "reason": "decision_attempts_exhausted"}])
                continue
            if entry.get("last_action") == "defer" and entry.get("review_after") and parse_time(entry["review_after"]) > now:
                continue
            if entry.get("last_action") == "pending" and entry.get("lease_until") and parse_time(entry["lease_until"]) > now:
                continue
        items.append(item)
    items = items[:int(state["policy"]["max_candidates_per_wake"])]
    return items, extra, errors


def wake_payload(items: list[dict[str, Any]], errors: list[str], now: datetime) -> dict[str, Any]:
    return {"goal_id": GOAL_ID, "protocol": "ch-proactive-agent/references/protocol.md",
            "evaluated_at": iso(now),
            "data_scope": ["AlphaVault files", "AlphaVault PostgreSQL"],
            "long_task_skills": "output_only_never_invoke", "tools_available_to_agent": [],
            "candidates": items, "source_errors": errors,
            "response_contract": {"decisions": [{"candidate_id": "…", "evidence_hash": "…",
                "action": "silent|defer|notify|needs_user", "reason": "…",
                "review_after": "defer 必填", "message": "notify/needs_user 必填",
                "missing_inputs": [], "falsifier": "…"}]},
            "instruction": "只输出一个 JSON 对象，不要 Markdown。每个候选一项，最多一项 notify/needs_user；没有通知价值全选 silent。候选已带本条增量、上一版字段差异和同产业精简索引；是否对比、对照哪一版、看哪个维度由你判断，不要假设脚本已替你比对。backfill 历史补评不通知，raw_available=false 只能延期复核，valid_until 之后不提醒；K线验证完成不代表预测仍成立。"}


def emit(payload: dict[str, Any], wake: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps({"wakeAgent": wake}))


def cmd_init(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    with locked(STATE):
        if STATE.exists():
            raise ValueError("主动目标已存在；不会覆盖游标")
        state = default_state(now)
        fresh, extra, errors = collect(state, now)
        for item in fresh:
            if item["kind"] == "framework_review":
                continue
            state["candidates"][item["candidate_id"]] = {
                "evidence_hash": item["evidence_hash"], "kind": item["kind"], "native_id": item["native_id"],
                "first_seen_at": iso(now), "last_seen_at": iso(now), "observed_at": item["observed_at"],
                "attempts": 0, "last_action": "baseline", "review_after": None,
            }
            if item["kind"] == "scheduled_review":
                state["scheduled_windows"][item["native_id"]] = {"action": "baseline", "at": iso(now)}
        state["framework_versions"] = extra["framework_current"]
        state["initialized"] = True
        state["last_scan_at"] = iso(now)
        state["source_errors"] = errors
        atomic_write_json(STATE, state)
        print(json.dumps({"initialized": True, "baseline_candidates": len(state["candidates"]),
                          "source_errors": errors}, ensure_ascii=False, indent=2))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    try:
        with locked(STATE, timeout=0.2):
            state = load_state()
            if state["status"] != "active" or not state.get("initialized") or not is_active_time(state["policy"], now):
                emit({"status": "outside_active_hours", "candidates": []}, False)
                return 0
            items, _extra, errors = eligible(state, now)
            update_presentation(state, items, now)
            state["last_scan_at"] = iso(now)
            state["source_errors"] = errors
            if errors and not items:
                emit(wake_payload([], errors, now), False)
                if not args.dry_run:
                    atomic_write_json(STATE, state)
                print("proactive_goal source errors: " + "；".join(errors), file=sys.stderr)
                return 0
            if not args.dry_run:
                atomic_write_json(STATE, state)
            emit(wake_payload(items, errors, now), bool(items))
    except TimeoutError:
        emit({"status": "another_tick_active", "candidates": []}, False)
    except Exception as exc:
        emit({"source_errors": [short(exc, 500)], "candidates": []}, False)
        print(f"proactive_goal source error: {exc}", file=sys.stderr)
        return 0
    return 0


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


def append_audit(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def validate_message(message: str) -> None:
    if not message or len(message) > 2000:
        raise ValueError("通知必须是非空且不超过 2000 字的纯文本")
    if re.search(r"(?im)^\s*(?:MEDIA|ATTACHMENT|FILE)\s*:", message) or "file://" in message.lower():
        raise ValueError("通知禁止本地文件或附件引用，避免文件外发")
    if "[SILENT]" in message or "[CRON_FAILURE]" in message:
        raise ValueError("通知不能包含 Hermes 控制标记")
    last = next((line.strip() for line in reversed(message.splitlines()) if line.strip()), "")
    try:
        gate = json.loads(last)
    except json.JSONDecodeError:
        gate = None
    if isinstance(gate, dict) and gate.get("wakeAgent") is False:
        raise ValueError("通知末行不能伪装成 wakeAgent 控制信号")


def record_batch(input_state: dict[str, Any], decisions: list[dict[str, Any]], now: datetime,
                 run_key: str, latest: dict[str, str]) -> list[dict[str, Any]]:
    state = copy.deepcopy(input_state)
    if not decisions or len(decisions) > int(state["policy"]["max_candidates_per_wake"]):
        raise ValueError("必须逐项提供本批全部候选的决策")
    if sum(row.get("action") in NOTIFY_ACTIONS for row in decisions if isinstance(row, dict)) > 1:
        raise ValueError("每批最多一项 notify/needs_user，避免多候选互相覆盖")
    audits: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for row in decisions:
        if not isinstance(row, dict):
            raise ValueError("决策必须是对象")
        cid = str(row.get("candidate_id") or "")
        entry = state["candidates"].get(cid)
        if not entry:
            raise ValueError(f"未登记的候选：{cid}")
        if entry.get("last_action") in FINAL_ACTIONS:
            raise ValueError(f"候选已完成或被取代：{cid}")
        if row.get("evidence_hash") != entry.get("evidence_hash"):
            raise ValueError(f"候选证据版本不匹配：{cid}")
        if latest.get(entry["native_id"]) != entry["evidence_hash"]:
            raise ValueError(f"候选已被新版本取代，拒绝旧证据通知：{cid}")
        action = row.get("action")
        if action not in {"silent", "defer", *NOTIFY_ACTIONS}:
            raise ValueError("未知 action")
        review_after = row.get("review_after")
        if action == "defer" and (not review_after or parse_time(review_after) <= now):
            raise ValueError("defer 需要晚于当前时间的 review_after")
        if action != "defer" and review_after:
            raise ValueError("只有 defer 可以设置 review_after")
        valid_until = entry.get("valid_until")
        if action == "defer" and valid_until and parse_time(review_after) > parse_time(valid_until):
            raise ValueError("defer 不能超过候选 valid_until")
        if action == "defer" and entry["kind"] == "catalyst" and not entry.get("raw_available", True):
            review_after = max(parse_time(review_after), now + timedelta(minutes=60)).isoformat()
            if valid_until and parse_time(review_after) > parse_time(valid_until):
                raise ValueError("Raw 缺失的延期复核不能超过候选有效期")
        if action in NOTIFY_ACTIONS:
            if entry.get("freshness_class") == "backfill":
                raise ValueError("历史补评不允许转为即时提醒；需要新的当前证据")
            if valid_until and parse_time(valid_until) < now:
                raise ValueError("候选已过有效期，拒绝即时提醒")
            if entry["kind"] == "catalyst" and not entry.get("raw_available", True):
                raise ValueError("原始证据缺失，不能生成高优先级通知；请 defer 后复核")
        message = short(row.get("message"), 2000)
        if action in NOTIFY_ACTIONS:
            validate_message(message)
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        if entry["attempts"] > int(state["policy"]["max_decision_attempts"]):
            entry["last_action"] = "dead"
            entry["dead_reason"] = "decision_attempts_exhausted"
            outcomes.append({"candidate_id": cid, "outcome": "dead"})
            continue
        entry["lease_until"] = None
        entry["updated_at"] = iso(now)
        entry["review_after"] = review_after if action == "defer" else None
        entry["last_action"] = action
        outcome = action
        if entry["kind"] == "framework_change" and action != "defer":
            path = entry["native_id"]
            packet = read_json(RUNTIME / entry["packet_path"])
            new_hash = packet["evidence"].get("current_sha256")
            if new_hash:
                state["framework_versions"][path] = new_hash
            else:
                state["framework_versions"].pop(path, None)
        if entry["kind"] == "scheduled_review" and action != "defer":
            state["scheduled_windows"][entry["native_id"]] = {"action": action, "at": iso(now)}
        if action in NOTIFY_ACTIONS:
            intent_id = "OUT-" + digest({"candidate_id": cid, "message": message})[:16]
            source_version = entry["evidence_hash"]
            if entry["kind"] == "framework_change":
                packet = read_json(RUNTIME / entry["packet_path"])
                source_version = packet["evidence"].get("current_sha256")
            intent = state["outbox"].get(intent_id)
            if not intent:
                not_before = next_notification_time(state, now)
                ttl_expiry = now + timedelta(hours=int(state["policy"]["pending_notification_ttl_hours"]))
                valid_until = min(parse_time(valid_until), ttl_expiry) if valid_until else ttl_expiry
                state["outbox"][intent_id] = {
                    "candidate_id": cid, "native_id": entry["native_id"], "kind": entry["kind"],
                    "evidence_hash": entry["evidence_hash"], "source_version": source_version,
                    "raw_available": entry.get("raw_available", True),
                    "message": message, "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                    "status": "pending", "not_before": iso(not_before), "created_at": iso(now),
                    "valid_until": iso(valid_until),
                    "attempted_at": None, "channel": None, "run_key": run_key,
                }
                outcome = "prepared"
            else:
                outcome = "duplicate_intent"
            if outcome == "prepared":
                state["delivery"] = {"status": "pending", "last_candidate_id": cid,
                                     "last_message_sha256": state["outbox"][intent_id]["message_sha256"]}
        audits.append({"at": iso(now), "goal_id": GOAL_ID, "candidate_id": cid, "action": action,
                       "outcome": outcome, "evidence_hash": entry["evidence_hash"], "run_key": run_key,
                       "reason": short(row.get("reason"), 500), "review_after": review_after,
                       "missing_inputs": row.get("missing_inputs") or [],
                       "falsifier": short(row.get("falsifier"), 500)})
        outcomes.append({"candidate_id": cid, "outcome": outcome})
    state["updated_at"] = iso(now)
    atomic_write_json(STATE, state)
    input_state.clear()
    input_state.update(state)
    append_audit(audits)
    return outcomes


def parse_decisions(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    payload = json.loads(text)
    rows = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("最终响应必须是含 decisions 数组的 JSON 对象")
    return rows


def response_from_run(path: Path) -> str | None:
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        text = handle.read(2_000_001)
    if len(text) > 2_000_000:
        raise ValueError("运行记录过大，拒绝无界读取")
    match = re.search(r"\*\*Response Characters:\*\* (\d+)\n## Response\n\n", text)
    if not match:
        return None
    size = int(match.group(1))
    body = text[match.end():match.end() + size]
    if len(body) != size or text[match.end() + size:match.end() + size + 1] != "\n":
        raise ValueError("运行记录的 Response 长度边界无效")
    return body


def process_runs(state: dict[str, Any], evaluation_job_id: str, now: datetime) -> None:
    if not re.fullmatch(r"[0-9a-f]{12}", evaluation_job_id):
        raise ValueError("evaluation_job_id 非法")
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).resolve()
    output_dir = home / "cron" / "output" / evaluation_job_id
    if not output_dir.is_dir():
        raise ValueError(f"评估输出目录不存在：{output_dir}")
    paths = sorted(output_dir.glob("*.md"), key=lambda path: path.stat().st_mtime)[-30:]
    for path in paths:
        if path.resolve().parent != output_dir.resolve():
            continue
        with path.open("rb") as handle:
            raw_run = handle.read(2_000_001)
        if len(raw_run) > 2_000_000:
            state["processed_runs"][path.name] = {"status": "invalid", "at": iso(now), "error": "运行记录超过 2MB"}
            atomic_write_json(STATE, state)
            continue
        run_key = hashlib.sha256(raw_run).hexdigest()
        if run_key in state["processed_runs"]:
            continue
        response = response_from_run(path)
        if response is None:
            continue
        try:
            decisions = parse_decisions(response)
            fresh, _extra, errors = collect(state, now)
            if errors:
                raise ValueError("数据源异常，暂缓本批决策：" + "；".join(errors))
            outcomes = record_batch(state, decisions, now, run_key, latest_native(fresh, state))
            state["processed_runs"][run_key] = {"status": "recorded", "at": iso(now), "outcomes": outcomes}
        except (ValueError, json.JSONDecodeError) as exc:
            state["processed_runs"][run_key] = {"status": "invalid", "at": iso(now), "error": short(exc, 500)}
            append_audit([{"at": iso(now), "goal_id": GOAL_ID, "run_key": run_key,
                           "status": "invalid_decision", "error": short(exc, 500)}])
            atomic_write_json(STATE, state)
    if len(state["processed_runs"]) > 1000:
        state["processed_runs"] = dict(list(state["processed_runs"].items())[-1000:])


def intent_raw_ok(intent: dict[str, Any], fresh: list[dict[str, Any]]) -> bool:
    """投递前只为这一条通知定向读原始证据，避免全量预读。"""
    item = next((row for row in fresh if row["native_id"] == intent["native_id"]), None)
    if item is None:
        return True
    refs = (item.get("evidence") or {}).get("source_refs") or []
    if not refs:
        return False
    checks = [raw_excerpt(str(ref.get("raw_path") or ""), limit=800) for ref in refs[:2]]
    return all("error" not in row for row in checks)


def cmd_dispatch(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    with locked(STATE):
        state = load_state()
        if state["status"] != "active" or not is_active_time(state["policy"], now):
            return 0
        process_runs(state, args.evaluation_job_id, now)
        for intent in state["outbox"].values():
            if intent.get("status") != "pending":
                continue
            expiry = intent.get("valid_until") or iso(
                parse_time(intent["created_at"]) + timedelta(hours=int(state["policy"]["pending_notification_ttl_hours"])))
            if parse_time(expiry) < now:
                intent["status"] = "expired"
                intent["expired_at"] = iso(now)
                intent["cancel_reason"] = "notification_valid_until_elapsed"
                append_audit([{"at": iso(now), "goal_id": GOAL_ID, "candidate_id": intent["candidate_id"],
                               "status": "notification_expired", "valid_until": expiry}])
                if state["delivery"].get("last_candidate_id") == intent["candidate_id"]:
                    state["delivery"]["status"] = "expired"
        pending = [row for row in state["outbox"].values()
                   if row.get("status") == "pending" and parse_time(row["not_before"]) <= now]
        pending.sort(key=lambda row: row["created_at"])
        message = None
        if pending:
            intent = pending[0]
            fresh, extra, errors = collect(state, now)
            state["source_errors"] = errors
            if errors:
                pending = []
            else:
                latest = latest_native(fresh)
                latest.update(extra["framework_current"])
                expected_version = intent.get("source_version", intent["evidence_hash"])
                if intent.get("kind") == "scheduled_review":
                    latest.setdefault(intent["native_id"], expected_version)
                if latest.get(intent["native_id"]) != expected_version:
                    intent["status"] = "cancelled"
                    intent["cancel_reason"] = "superseded_before_delivery"
                else:
                    freshness = next((item.get("freshness") or {} for item in fresh
                                      if item["native_id"] == intent["native_id"]), {})
                    if intent["kind"] == "catalyst" and freshness.get("class") == "backfill":
                        intent["status"] = "expired"
                        intent["cancel_reason"] = "backfill_not_immediate"
                        if state["delivery"].get("last_candidate_id") == intent["candidate_id"]:
                            state["delivery"]["status"] = "expired"
                    elif intent["kind"] == "catalyst" and not intent_raw_ok(intent, fresh):
                        intent["not_before"] = iso(now + timedelta(minutes=60))
                        intent["defer_reason"] = "raw_unavailable"
                    else:
                        allowed = next_notification_time(state, now)
                        if allowed > now:
                            intent["not_before"] = iso(allowed)
                        else:
                            intent["status"] = "attempted"
                            intent["attempted_at"] = iso(now)
                            intent["channel"] = args.channel
                            intent["attempt_id"] = uuid.uuid4().hex
                            state["last_notification_request_at"] = iso(now)
                            today = state["notification_requests_today"] or {}
                            state["notification_requests_today"] = {
                                "date": now.date().isoformat(),
                                "count": (int(today.get("count", 0)) + 1) if today.get("date") == now.date().isoformat() else 1,
                            }
                            state["delivery"] = {"status": "attempted", "last_candidate_id": intent["candidate_id"],
                                                 "last_message_sha256": intent["message_sha256"], "attempt_id": intent["attempt_id"]}
                            message = intent["message"]
        state["updated_at"] = iso(now)
        atomic_write_json(STATE, state)
    if message:
        # stdout 是 Hermes no-agent 任务唯一可投递的内容；此前已通过全部确定性门禁。
        print(message)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    raw = Path(args.decision_file).read_text(encoding="utf-8") if args.decision_file != "-" else sys.stdin.read()
    with locked(STATE):
        state = load_state()
        decisions = parse_decisions(raw)
        fresh, _extra, _errors = collect(state, now)
        outcomes = record_batch(state, decisions, now, "manual-" + uuid.uuid4().hex, latest_native(fresh, state))
        atomic_write_json(STATE, state)
        print(json.dumps({"outcomes": outcomes, "note": "仅记录意图；由 dispatch 决定是否产生可投递内容"}, ensure_ascii=False))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with locked(STATE, exclusive=False):
        state = load_state()
    def counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in rows:
            value = str(row.get(key) or "unknown")
            result[value] = result.get(value, 0) + 1
        return result
    pending_notifications = []
    for intent in state["outbox"].values():
        if intent.get("status") == "pending":
            pending_notifications.append({key: intent.get(key) for key in (
                "candidate_id", "native_id", "kind", "not_before", "valid_until", "defer_reason", "raw_available",
            )})
            if not pending_notifications[-1]["valid_until"]:
                pending_notifications[-1]["valid_until"] = iso(
                    parse_time(intent["created_at"]) + timedelta(hours=int(state["policy"]["pending_notification_ttl_hours"])))
    print(json.dumps({"goal_id": GOAL_ID, "workspace": str(ROOT), "runtime": str(RUNTIME), "status": state["status"],
        "initialized": state["initialized"], "last_scan_at": state.get("last_scan_at"),
        "pending_notifications": pending_notifications,
        "candidate_counts": counts(list(state["candidates"].values()), "last_action"),
        "outbox_counts": counts(list(state["outbox"].values()), "status"),
        "processed_run_counts": counts(list(state["processed_runs"].values()), "status"),
        "policy": state["policy"], "last_notification_request_at": state.get("last_notification_request_at"),
        "notification_requests_today": state["notification_requests_today"], "delivery": state["delivery"],
        "source_errors": state["source_errors"]}, ensure_ascii=False, indent=2))
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    with locked(STATE):
        state = load_state()
        if args.action == "pause":
            state["status"] = "paused"
        elif args.action == "resume":
            state["status"] = "active"
            state["mute_until"] = None
        else:
            if args.action == "schedule":
                if args.heartbeat_minutes < 5:
                    raise ValueError("heartbeat_minutes 不得小于 5 分钟")
                for value in (args.active_start, args.active_end):
                    datetime.strptime(value, "%H:%M")
                if args.active_start == args.active_end:
                    raise ValueError("active_start 与 active_end 不能相同")
                state["policy"].update(
                    heartbeat_minutes=args.heartbeat_minutes,
                    active_start=args.active_start, active_end=args.active_end,
                    quiet_start=args.active_end, quiet_end=args.active_start,
                )
            elif args.action == "freshness":
                if args.trend_max_age_days < 1 or args.pending_ttl_hours < 1:
                    raise ValueError("趋势数据期限和待发有效期必须为正数")
                state["policy"].update(
                    max_trend_age_days=args.trend_max_age_days,
                    pending_notification_ttl_hours=args.pending_ttl_hours,
                )
            else:
                if not 0 < args.mute_hours <= 168:
                    raise ValueError("mute_hours 必须在 (0, 168] 小时内")
                state["mute_until"] = iso(now + timedelta(hours=args.mute_hours))
        state["updated_at"] = iso(now)
        atomic_write_json(STATE, state)
        print(json.dumps({"status": state["status"], "mute_until": state["mute_until"]}, ensure_ascii=False))
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    now = parse_time(args.now)
    with locked(STATE):
        state = load_state()
        entry = state["candidates"].get(args.candidate_id)
        if not entry:
            raise ValueError("找不到候选")
        entry["last_action"] = "acknowledged"
        entry["review_after"] = None
        entry["lease_until"] = None
        state["updated_at"] = iso(now)
        append_audit([{"at": iso(now), "goal_id": GOAL_ID, "candidate_id": args.candidate_id,
                       "action": "acknowledged", "note": short(args.note, 500)}])
        atomic_write_json(STATE, state)
        print(json.dumps({"acknowledged": args.candidate_id}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("init", cmd_init), ("scan", cmd_scan), ("dispatch", cmd_dispatch),
                       ("record", cmd_record), ("status", cmd_status), ("control", cmd_control), ("ack", cmd_ack)):
        p = sub.add_parser(name)
        p.add_argument("--now", help="测试用 ISO 时间")
        p.set_defaults(func=func)
        if name == "scan":
            p.add_argument("--dry-run", action="store_true")
        if name == "dispatch":
            p.add_argument("--evaluation-job-id", required=True)
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
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except (ValueError, OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"proactive_goal: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
