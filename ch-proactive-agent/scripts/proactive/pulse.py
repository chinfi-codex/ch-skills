"""事件唤醒：触发器只产生“信号”，是否叫醒模型仍由确定性扫描决定。

信号来源：
- 水位：sources.yaml 登记的金库文件（mtime+size）与 PG 水位查询，每次 pulse 比较；
- 收件箱：signals.jsonl，由 `proactive_goal.py signal` 追加（长任务 Skill 收尾、手动）；
- 时间：固定复核窗口到点、延期复核到期、心跳兜底。
就绪的信号经过防抖与限流后才做一次全量扫描；有候选时写 ready_batch.json 并触发评估任务，
评估任务的 gate 只负责把这一批交给模型。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta
from typing import Any

from proactive_config import atomic_write_json, read_json

from .context import CTX, digest, hermes_home, iso, parse_time, short
from .sources import schedule
from .store import add_health

IMMEDIATE = {"manual", "schedule", "defer", "heartbeat"}
PG_TIMEOUT_SECONDS = 5


def _file_watermark(patterns: list[str]) -> str | None:
    stamps = []
    for pattern in patterns:
        for path in sorted(CTX.root.glob(pattern)):
            try:
                stat = path.stat()
            except OSError:
                continue
            stamps.append([path.relative_to(CTX.root).as_posix(), stat.st_mtime_ns, stat.st_size])
    return digest(stamps) if stamps else None


def _database_url() -> str:
    return (os.environ.get("DATABASE_URL", "").strip() or os.environ.get("ALPHA_PG_URL", "").strip()
            or "postgresql://alpha_user:alpha_pass@/alpha_data?host=/tmp")


def _pg_watermark(queries: list[str]) -> str | None:
    import psycopg2

    conn = psycopg2.connect(_database_url(), connect_timeout=PG_TIMEOUT_SECONDS)
    try:
        conn.set_session(readonly=True, autocommit=True)
        values = []
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {PG_TIMEOUT_SECONDS * 1000}")
            for sql in queries:
                cur.execute(sql)
                values.append([str(v) for v in (cur.fetchone() or [])])
        return digest(values)
    finally:
        conn.close()


def watermarks(state: dict[str, Any], now: datetime) -> dict[str, str]:
    result: dict[str, str] = {}
    for kind, cfg in CTX.sources.items():
        if not cfg.get("enabled", True):
            continue
        watch = cfg.get("watch") or {}
        parts = []
        if watch.get("files"):
            parts.append(_file_watermark(list(watch["files"])))
        if watch.get("pg_watermarks"):
            try:
                parts.append(_pg_watermark(list(watch["pg_watermarks"])))
            except Exception as exc:  # 连接失败不是市场事件：记健康告警，保持旧水位
                add_health(state, "pg_watermark_failed", f"{kind}：{short(exc, 160)}", now)
                previous = (state["signals"]["watermarks"] or {}).get(kind)
                if previous:
                    result[kind] = previous
                continue
        if parts:
            result[kind] = digest(parts)
    return result


def append_signal(source: str, ref: str | None, reason: str | None, now: datetime) -> dict[str, Any]:
    row = {"at": iso(now), "source": source, "ref": short(ref, 300) if ref else None,
           "reason": short(reason, 300) if reason else None}
    CTX.signals_path.parent.mkdir(parents=True, exist_ok=True)
    with CTX.signals_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _read_inbox(state: dict[str, Any]) -> list[dict[str, Any]]:
    path = CTX.signals_path
    if not path.is_file():
        return []
    offset = int(state["signals"].get("offset") or 0)
    size = path.stat().st_size
    if offset > size:
        offset = 0  # 收件箱被轮转
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    complete = data[:data.rfind(b"\n") + 1] if b"\n" in data else b""
    state["signals"]["offset"] = offset + len(complete)
    rows = []
    for line in complete.decode("utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("source"):
            rows.append(row)
    return rows


def _sources_for_skill(skill: str) -> list[str]:
    return [kind for kind, cfg in CTX.sources.items() if skill in ((cfg.get("watch") or {}).get("skills") or [])]


def _mark(pending: dict[str, Any], key: str, now: datetime, why: str) -> None:
    row = pending.setdefault(key, {"first_at": iso(now), "reasons": []})
    row["last_at"] = iso(now)
    if why not in row["reasons"]:
        row["reasons"] = (row["reasons"] + [why])[-5:]


def detect(state: dict[str, Any], now: datetime) -> None:
    signals = state["signals"]
    pending = signals.setdefault("pending", {})
    current = watermarks(state, now)
    previous = signals.get("watermarks") or {}
    for kind, value in current.items():
        if kind in previous and previous[kind] != value:
            _mark(pending, kind, now, "watermark")
    signals["watermarks"] = {**previous, **current}
    for row in _read_inbox(state):
        source = str(row["source"])
        if source.startswith("skill:"):
            for kind in _sources_for_skill(source[6:]) or ["manual"]:
                _mark(pending, kind, now, source)
        elif source in CTX.sources:
            _mark(pending, source, now, "signal")
        else:
            _mark(pending, "manual", now, source)
    if schedule.due_slot(now, state):
        _mark(pending, "schedule", now, "window")
    if any(entry.get("last_action") == "defer" and entry.get("review_after") and parse_time(entry["review_after"]) <= now
           for entry in state["candidates"].values()):
        _mark(pending, "defer", now, "review_due")
    last_full = signals.get("last_full_scan_at")
    if not last_full or parse_time(last_full) <= now - timedelta(minutes=int(state["policy"]["heartbeat_minutes"])):
        _mark(pending, "heartbeat", now, "heartbeat")


def ready(state: dict[str, Any], now: datetime) -> list[str]:
    result = []
    for key, row in (state["signals"].get("pending") or {}).items():
        if key in IMMEDIATE:
            result.append(key)
            continue
        debounce = CTX.source(key).get("debounce") or {}
        quiet = int(debounce.get("quiet_seconds", 0))
        max_wait = int(debounce.get("max_wait_seconds", quiet))
        if now - parse_time(row["last_at"]) >= timedelta(seconds=quiet) or \
                now - parse_time(row["first_at"]) >= timedelta(seconds=max_wait):
            result.append(key)
    return sorted(result)


def rate_ok(state: dict[str, Any], now: datetime) -> bool:
    policy = state["policy"]
    stamps = [parse_time(s) for s in state["signals"].get("evaluations") or []]
    stamps = [s for s in stamps if s > now - timedelta(days=1)]
    state["signals"]["evaluations"] = [iso(s) for s in stamps]
    hour = sum(1 for s in stamps if s > now - timedelta(hours=1))
    return hour < int(policy.get("max_evaluations_per_hour", 4)) and len(stamps) < int(policy.get("max_evaluations_per_day", 30))


def stage_batch(state: dict[str, Any], payload: dict[str, Any], now: datetime) -> str:
    batch_id = uuid.uuid4().hex[:12]
    ids = [item["candidate_id"] for item in payload["candidates"]]
    atomic_write_json(CTX.ready_batch_path, {"batch_id": batch_id, "created_at": iso(now), "payload": payload})
    state["signals"]["inflight"] = {"batch_id": batch_id, "created_at": iso(now), "candidate_ids": ids, "status": "ready"}
    state["signals"]["evaluations"] = (state["signals"].get("evaluations") or []) + [iso(now)]
    return batch_id


def take_batch(state: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """评估任务的 gate：一批只交出一次；没有就绪批次就不唤醒模型。"""
    inflight = state["signals"].get("inflight")
    if not inflight or inflight.get("status") != "ready" or not CTX.ready_batch_path.is_file():
        return None
    batch = read_json(CTX.ready_batch_path)
    if batch.get("batch_id") != inflight["batch_id"]:
        return None
    inflight.update(status="taken", taken_at=iso(now))
    CTX.ready_batch_path.unlink()
    return batch["payload"]


def trigger_evaluation(job_id: str) -> tuple[bool, str]:
    """请 Hermes 在下一个调度 tick 运行评估任务；失败由评估任务的定时兜底接手。"""
    binary = os.environ.get("HERMES_BIN") or shutil.which("hermes") or str(hermes_home().parent / ".local" / "bin" / "hermes")
    try:
        proc = subprocess.run([binary, "cron", "run", job_id], text=True, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, short(exc, 200)
    return proc.returncode == 0, short(proc.stderr or proc.stdout, 200)
