#!/usr/bin/env python3
"""把主动目标运行态从 AlphaVault 迁到运行态目录；默认 dry-run。

只复制，不删除金库原件：state.json、packets/、decisions.jsonl。
迁移时持有旧运行时同一把状态锁，避免与正在运行的 tick 交错；目标目录已有状态则拒绝。
迁移后改写 packet_path 为相对运行态目录，并移出已搬离金库的协议文件版本记录，
否则新运行时会把它误报为“方法论被删除”。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from proactive_config import atomic_write_json, file_hash, load_settings, read_json

SETTINGS = load_settings()
LEGACY_STATE_REL = "system/frameworks/主动目标状态.json"
LEGACY_RUNS_REL = "ledgers/runs/proactive"
LEGACY_PACKET_PREFIX = LEGACY_RUNS_REL + "/packets/"
# 已迁入 ch-proactive-agent/references/protocol.md，不再是金库方法论。
RETIRED_FRAMEWORK_FILES = ("system/frameworks/27-主动目标协议.md",)
LOCAL_TZ = timezone(timedelta(hours=8))


@contextlib.contextmanager
def legacy_lock(state_path: Path, timeout: float = 20.0):
    """与迁移前运行时相同的锁位置：$HERMES_HOME/proactive-locks/<sha256(状态路径)>.lock。"""
    lock_root = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "proactive-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / (hashlib.sha256(str(state_path.resolve()).encode("utf-8")).hexdigest() + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"旧运行时仍持有状态锁：{lock_path}")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    result = copy.deepcopy(state)
    rewritten = 0
    for entry in result.get("candidates", {}).values():
        rel = entry.get("packet_path")
        if rel and rel.startswith(LEGACY_PACKET_PREFIX):
            entry["packet_path"] = "packets/" + rel[len(LEGACY_PACKET_PREFIX):]
            rewritten += 1
        elif rel:
            raise ValueError(f"无法识别的 packet_path：{rel}")
    retired = 0
    for rel in RETIRED_FRAMEWORK_FILES:
        if result.get("framework_versions", {}).pop(rel, None) is not None:
            retired += 1
    return result, {"packet_paths_rewritten": rewritten, "framework_versions_retired": retired}


def plan(vault: Path, runtime: Path) -> dict[str, Any]:
    state_path = vault / LEGACY_STATE_REL
    runs = vault / LEGACY_RUNS_REL
    if not state_path.is_file():
        raise ValueError(f"金库中没有旧状态：{state_path}")
    state = read_json(state_path)
    if state.get("goal_id") != SETTINGS.goal_id:
        raise ValueError("旧状态 goal_id 与 config/goals.yaml 不一致")
    packets = sorted((runs / "packets").glob("PG-*.json"))
    referenced = {entry["packet_path"] for entry in state["candidates"].values() if entry.get("packet_path")}
    missing = sorted(rel for rel in referenced if not (vault / rel).is_file())
    audit = runs / "decisions.jsonl"
    audit_rows = len(audit.read_text(encoding="utf-8").splitlines()) if audit.is_file() else 0
    return {
        "vault_state": str(state_path), "runtime_home": str(runtime),
        "candidates": len(state["candidates"]), "outbox": len(state["outbox"]),
        "packets": len(packets), "packets_referenced": len(referenced), "packets_missing": missing,
        "audit_rows": audit_rows,
        "runtime_exists": (runtime / "state.json").exists(),
    }


def apply(vault: Path, runtime: Path) -> dict[str, Any]:
    state_path = vault / LEGACY_STATE_REL
    runs = vault / LEGACY_RUNS_REL
    if (runtime / "state.json").exists():
        raise ValueError(f"运行态已存在，拒绝覆盖：{runtime / 'state.json'}")
    with legacy_lock(state_path):
        source_hash = file_hash(state_path, length=None)
        state, changes = transform(read_json(state_path))
        (runtime / "packets").mkdir(parents=True, exist_ok=True)
        copied = 0
        for path in sorted((runs / "packets").glob("PG-*.json")):
            shutil.copy2(path, runtime / "packets" / path.name)
            copied += 1
        if (runs / "decisions.jsonl").is_file():
            shutil.copy2(runs / "decisions.jsonl", runtime / "decisions.jsonl")
        for entry in state["candidates"].values():
            rel = entry.get("packet_path")
            if rel and entry.get("last_action") in {"pending", "defer"} and not (runtime / rel).is_file():
                raise ValueError(f"活跃候选的候选包未能迁移：{rel}")
        atomic_write_json(runtime / "state.json", state)
    receipt = {"migrated_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
               "source_state": str(state_path), "source_sha256": source_hash,
               "runtime_home": str(runtime), "packets_copied": copied,
               "candidates": len(state["candidates"]), "outbox": len(state["outbox"]), **changes,
               "note": "金库原件未删除；切换 Hermes 前请先暂停两个任务"}
    atomic_write_json(runtime / "migration.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    vault, runtime = SETTINGS.vault_root, SETTINGS.runtime_home
    if not args.apply:
        print(json.dumps({"dry_run": True, **plan(vault, runtime)}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(apply(vault, runtime), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, TimeoutError) as exc:
        print(f"migrate_from_vault: {exc}", file=sys.stderr)
        raise SystemExit(2)
