"""候选采集。collect() 汇总各数据源；在 tick_cache() 内，昂贵的采集（PG 子进程、
全量事件表）每个进程只做一次，scan / 处理评估结果 / 投递前复核共用同一份结果。"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any, Callable

from ..context import CTX, FINGERPRINT_VERSION, digest

_CACHE: dict[Any, Any] | None = None


@contextlib.contextmanager
def tick_cache():
    global _CACHE
    outer = _CACHE
    _CACHE = {} if outer is None else outer
    try:
        yield
    finally:
        _CACHE = outer


def cached(key: Any, compute: Callable[[], Any]) -> Any:
    if _CACHE is None:
        return compute()
    if key not in _CACHE:
        _CACHE[key] = compute()
    return _CACHE[key]


def topic_for(kind: str, native_id: str, evidence: dict[str, Any]) -> str:
    """主题键由代码确定：同一主题的提醒共用冷却期，不依赖模型自觉。"""
    if kind in {"trend_state", "scheduled_review"}:
        return "trend:axis"
    if kind == "framework_review":
        return "framework_review:" + native_id.split(":", 1)[0]
    if kind == "framework_change":
        return "framework_change:" + native_id
    return "catalyst:" + native_id


def default_fingerprint(kind: str, native_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """指纹只覆盖语义字段：派生值（逾期天数、计划日）、注入的参考正文和固定说明文字变化不产生新版本。"""
    if kind == "trend_state":
        # plan_date 每个自然日都变，留在指纹里会让同一份收盘数据每天被重新评估一次
        evidence = {k: v for k, v in evidence.items() if k not in {"rule_texts", "note", "plan_date"}}
    elif kind == "framework_review":
        evidence = {"version": evidence.get("version"), "review_due": evidence.get("review_due")}
    return {"kind": kind, "native_id": native_id, "evidence": evidence}


def candidate(kind: str, native_id: str, observed_at: str, evidence: dict[str, Any],
              fingerprint_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    fingerprint = fingerprint_evidence or default_fingerprint(kind, native_id, evidence)
    evidence_hash = digest(fingerprint)
    return {"candidate_id": f"PG-{evidence_hash[:16]}", "kind": kind, "native_id": native_id,
            "observed_at": observed_at, "evidence_hash": evidence_hash,
            "fingerprint_version": FINGERPRINT_VERSION, "topic_key": topic_for(kind, native_id, evidence),
            "evidence": evidence}


def collect(state: dict[str, Any], now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    from . import board, catalyst, framework, schedule, trend

    framework_items, current_framework, errors = framework.framework_candidates(state)
    trend_item, trend_errors = cached(("trend", now.date().isoformat(), int(state["policy"]["max_trend_age_days"])),
                                      lambda: trend.trading_candidate(now, state["policy"]))
    errors = errors + list(trend_errors)
    fresh = list(framework_items)
    if trend_item:
        fresh.append(trend_item)
    fresh.extend(cached(("catalyst", now.date().isoformat()), lambda: catalyst.catalyst_candidates(now)))
    board_item = board.board_candidate(now)
    if board_item:
        fresh.append(board_item)
    scheduled = schedule.scheduled_candidate(now, state, trend_item)
    if scheduled:
        fresh.append(scheduled)
    fresh = [item for item in fresh if CTX.settings.source_enabled(item["kind"])]
    rank = CTX.settings.source_rank()
    # 先按观察日倒序，再按 rank 正序做稳定排序：rank 小的源排前，同源内新的在前。
    fresh.sort(key=lambda item: item["observed_at"], reverse=True)
    fresh.sort(key=lambda item: rank.get(item["kind"], 99))
    return fresh, {"framework_current": current_framework}, errors
