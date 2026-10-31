"""催化事件台账：只呈现本条增量与精简存量索引；扫描阶段不读 Raw 正文。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from proactive_config import read_jsonl

from ..context import CTX, LOCAL_TZ, iso, short

SEMANTIC_FIELDS = (
    "event_id", "event_date", "ingest_date", "rating", "industry", "title", "description",
    "delta_vs_baseline", "hard_fields", "rating_reason", "negative_chain", "source_refs",
    "related_pages", "framework_impacts",
)
UPDATE_HISTORY_KEEP = 2


def max_age_days() -> int:
    return int(CTX.policy_defaults.get("catalyst_max_age_days", 7))


def _resolve(raw_path: str) -> Path | None:
    path = Path(raw_path)
    resolved = path if path.is_absolute() else CTX.root / path
    try:
        resolved = resolved.resolve()
        resolved.relative_to(CTX.root.resolve())
    except (ValueError, OSError):
        return None
    return resolved


def _pdf_text(resolved: Path) -> Path | None:
    return next((p for p in (resolved.with_suffix(".extracted.md"),
                             resolved.with_name(resolved.name + ".extracted.md")) if p.is_file()), None)


def raw_status(raw_path: str) -> dict[str, Any]:
    """只确认来源是否在库内可定位，不读正文；正文留到投递前定向复核。"""
    resolved = _resolve(raw_path)
    if resolved is None:
        return {"path": raw_path, "state": "outside_scope"}
    if not resolved.is_file():
        return {"path": raw_path, "state": "missing"}
    if resolved.suffix.lower() == ".pdf":
        extracted = _pdf_text(resolved)
        if extracted is None:
            return {"path": raw_path, "state": "no_text_extract"}
        return {"path": raw_path, "state": "ok", "bytes": extracted.stat().st_size, "via": "pdf_text"}
    return {"path": raw_path, "state": "ok", "bytes": resolved.stat().st_size}


def raw_excerpt(raw_path: str, limit: int = 3000) -> dict[str, Any]:
    """只在投递前定向复核时读取正文；路径、PDF 文本和大小都受限。"""
    resolved = _resolve(raw_path)
    if resolved is None:
        return {"path": raw_path, "error": "路径不在 AlphaVault 数据范围内"}
    if not resolved.is_file():
        return {"path": raw_path, "error": "Raw 文件不存在"}
    if resolved.suffix.lower() == ".pdf":
        extracted = _pdf_text(resolved)
        if extracted is None:
            return {"path": raw_path, "error": "PDF 尚无可用文本提取，证据降级"}
        resolved = extracted.resolve()
        try:
            resolved.relative_to(CTX.root.resolve())
        except ValueError:
            return {"path": raw_path, "error": "PDF 提取文本不在 AlphaVault 数据范围内"}
    try:
        with resolved.open("r", encoding="utf-8-sig", errors="replace") as handle:
            return {"path": raw_path, "excerpt": handle.read(limit)}
    except OSError:
        return {"path": raw_path, "error": "Raw 暂时无法读取，证据降级"}


def semantic_diff(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    changes = []
    for key in SEMANTIC_FIELDS:
        old, new = previous.get(key), current.get(key)
        if old == new:
            continue
        changes.append({"field": key, "previous": short(json.dumps(old, ensure_ascii=False, sort_keys=True), 240),
                        "current": short(json.dumps(new, ensure_ascii=False, sort_keys=True), 240)})
    return changes


def catalyst_freshness(event_date: str, updated_at: Any, raw_checks: list[dict[str, Any]],
                       has_sources: bool, now: datetime) -> dict[str, Any]:
    limit = max_age_days()
    observed = datetime.strptime(event_date, "%Y-%m-%d").date()
    age = (now.date() - observed).days
    valid_until = datetime.combine(observed + timedelta(days=limit), datetime.min.time(),
                                   tzinfo=LOCAL_TZ) + timedelta(hours=23)
    return {
        "class": "current" if 0 <= age <= limit else "backfill",
        "age_days": age, "event_date": event_date, "evidence_updated_at": updated_at,
        "valid_until": iso(valid_until),
        "raw_available": has_sources and all(row.get("state") == "ok" for row in raw_checks),
        "raw_states": [row.get("state") for row in raw_checks],
    }


def catalyst_candidates(now: datetime) -> list[dict[str, Any]]:
    from . import candidate

    rows = [row for row in read_jsonl(CTX.events, strict=True) if row.get("status") in {None, "active"}]
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
            "updated_at": row.get("updated_at"),
            "update_history": (row.get("update_history") or [])[-UPDATE_HISTORY_KEEP:],
            "raw_status": raw_checks, "industry_baseline": related,
            "note": "脚本只给本条增量和精简存量索引；是否需要对比、对照哪一版由评估者决定。",
        }
        observed_at = str(row.get("event_date") or now.date())
        semantic = {key: evidence.get(key) for key in SEMANTIC_FIELDS}
        item = candidate("catalyst", event_id, observed_at, evidence,
                         {"kind": "catalyst", "native_id": event_id, "evidence": semantic})
        item["freshness"] = catalyst_freshness(observed_at, row.get("updated_at"), raw_checks, bool(refs), now)
        item["valid_until"] = item["freshness"]["valid_until"]
        result.append(item)
    return result
