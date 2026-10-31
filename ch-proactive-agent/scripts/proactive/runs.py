"""读取评估任务的 Hermes 运行记录，交给 validate 入账；格式异常写健康告警而不是静默跳过。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import validate
from .context import hermes_home, iso, short
from .store import add_health, append_audit, save_state

RUN_LIMIT_BYTES = 2_000_000
RESPONSE_HEADER = re.compile(r"\*\*Response Characters:\*\* (\d+)\n## Response\n\n")
SCAN_FILES = 30
ROTATION_WARN = 45   # Hermes 每个任务只保留约 50 份运行记录


def response_from_run(path: Path) -> str | None:
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        text = handle.read(RUN_LIMIT_BYTES + 1)
    if len(text) > RUN_LIMIT_BYTES:
        raise ValueError("运行记录过大，拒绝无界读取")
    match = RESPONSE_HEADER.search(text)
    if not match:
        if "## Prompt" in text:
            raise ValueError("模型已运行但找不到可解析的 Response 段，Hermes 运行记录格式可能已变化")
        return None
    size = int(match.group(1))
    body = text[match.end():match.end() + size]
    if len(body) != size or text[match.end() + size:match.end() + size + 1] != "\n":
        raise ValueError("运行记录的 Response 长度边界无效")
    return body


def _clear_inflight(state: dict[str, Any]) -> list[str]:
    inflight = (state.get("signals") or {}).get("inflight") or {}
    state["signals"]["inflight"] = None
    return list(inflight.get("candidate_ids") or [])


def process_runs(state: dict[str, Any], evaluation_job_id: str, now: datetime,
                 latest_for: Callable[[], dict[str, str]]) -> int:
    if not re.fullmatch(r"[0-9a-f]{12}", evaluation_job_id):
        raise ValueError("evaluation_job_id 非法")
    output_dir = (hermes_home() / "cron" / "output" / evaluation_job_id).resolve()
    if not output_dir.is_dir():
        raise ValueError(f"评估输出目录不存在：{output_dir}")
    all_paths = sorted(output_dir.glob("*.md"), key=lambda path: path.stat().st_mtime)
    if len(all_paths) >= ROTATION_WARN:
        oldest = all_paths[0]
        try:
            pending_old = response_from_run(oldest) is not None and \
                hashlib.sha256(oldest.read_bytes()).hexdigest() not in state["processed_runs"]
        except ValueError:
            pending_old = False
        if pending_old:
            add_health(state, "eval_output_rotation_risk", f"最旧的评估输出尚未处理，即将被 Hermes 轮转：{oldest.name}", now)
    processed = 0
    for path in all_paths[-SCAN_FILES:]:
        if path.resolve().parent != output_dir:
            continue
        with path.open("rb") as handle:
            raw_run = handle.read(RUN_LIMIT_BYTES + 1)
        run_key = hashlib.sha256(raw_run).hexdigest()
        if run_key in state["processed_runs"]:
            continue
        if len(raw_run) > RUN_LIMIT_BYTES:
            state["processed_runs"][run_key] = {"status": "invalid", "at": iso(now), "error": "运行记录超过 2MB"}
            add_health(state, "eval_run_oversize", path.name, now)
            save_state(state)
            continue
        try:
            response = response_from_run(path)
        except ValueError as exc:
            state["processed_runs"][run_key] = {"status": "invalid", "at": iso(now), "error": short(exc, 500)}
            add_health(state, "eval_run_unparseable", f"{path.name}：{short(exc, 200)}", now)
            validate.charge_presented(state, _clear_inflight(state), now, "unparseable_run")
            save_state(state)
            continue
        if response is None:
            continue
        try:
            decisions = validate.parse_decisions(response)
        except (ValueError, json.JSONDecodeError) as exc:
            state["processed_runs"][run_key] = {"status": "invalid", "at": iso(now), "error": short(exc, 500)}
            append_audit([{"at": iso(now), "status": "invalid_decision", "run_key": run_key, "error": short(exc, 500)}])
            validate.charge_presented(state, _clear_inflight(state), now, "invalid_response")
            save_state(state)
            continue
        _clear_inflight(state)
        outcomes = validate.record_batch(state, decisions, now, run_key, latest_for())
        # 逐项结果已写入审计，状态里只留计数
        summary: dict[str, int] = {}
        for row in outcomes:
            summary[row["outcome"]] = summary.get(row["outcome"], 0) + 1
        state["processed_runs"][run_key] = {"status": "recorded", "at": iso(now), "outcomes": summary}
        save_state(state)
        processed += 1
    return processed


def stale_inflight(state: dict[str, Any], now: datetime, minutes: int) -> bool:
    """评估已触发但久未返回：清掉在途标记并计一次失败，让候选可以重新评估。"""
    inflight = (state.get("signals") or {}).get("inflight")
    if not inflight:
        return False
    from .context import parse_time

    if parse_time(inflight["created_at"]) > now - timedelta(minutes=minutes):
        return False
    ids = _clear_inflight(state)
    add_health(state, "evaluation_timeout", f"批次 {inflight.get('batch_id')} 超过 {minutes} 分钟未返回", now)
    validate.charge_presented(state, ids, now, "evaluation_timeout")
    return True
