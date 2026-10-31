"""运行上下文：配置快照、路径、时间与哈希工具。

所有模块通过 CTX 取路径，测试用 CTX.configure() 指向临时目录。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from proactive_config import SKILL_DIR, Settings, load_settings

SCHEMA_VERSION = "proactive-goal/v3"
LOCAL_TZ = timezone(timedelta(hours=8))
FINGERPRINT_VERSION = 3
FINAL_ACTIONS = {"silent", "notify", "needs_user", "baseline", "acknowledged", "superseded", "dead", "backfill"}
NOTIFY_ACTIONS = {"notify", "needs_user"}
PRIORITY_RANK = {"P1": 1, "P2": 2, "P3": 3}
KINDS = ("trend_state", "catalyst", "framework_review", "framework_change", "scheduled_review")


class Context:
    def __init__(self) -> None:
        self.configure()

    def configure(self, *, vault_root: Path | None = None, runtime_home: Path | None = None,
                  settings: Settings | None = None) -> "Context":
        s = settings or load_settings()
        self.settings = s
        self.root = Path(vault_root) if vault_root else s.vault_root
        self.runtime = Path(runtime_home) if runtime_home else s.runtime_home
        self.goal_id = s.goal_id
        self.policy_defaults = dict(s.policy)
        self.framework_files = list(s.framework_files)
        self.rules_dir = s.rules_dir
        self.inputs = dict(s.inputs)
        self.sources = dict(s.sources)
        self.schema: Path | None = SKILL_DIR / "config" / "schemas" / "state.schema.json"
        return self

    # 运行态
    @property
    def state_path(self) -> Path:
        return self.runtime / "state.json"

    @property
    def index_path(self) -> Path:
        return self.runtime / "index.json"

    @property
    def audit_path(self) -> Path:
        return self.runtime / "decisions.jsonl"

    @property
    def packets(self) -> Path:
        return self.runtime / "packets"

    @property
    def signals_path(self) -> Path:
        return self.runtime / "signals.jsonl"

    @property
    def ready_batch_path(self) -> Path:
        return self.runtime / "ready_batch.json"

    @property
    def archive(self) -> Path:
        return self.runtime / "archive"

    # 金库输入（只读）
    @property
    def events(self) -> Path:
        return self.root / self.inputs["catalyst_events"]

    @property
    def board(self) -> Path:
        return self.root / self.inputs["industry_board"]

    @property
    def board_history(self) -> Path:
        return self.root / self.inputs["industry_board_history"]

    @property
    def board_framework(self) -> str:
        return self.inputs["board_review_framework"]

    @property
    def trend_tool(self) -> Path:
        return self.root / self.inputs["trend_tool"]

    def source(self, kind: str) -> dict[str, Any]:
        return self.sources.get(kind) or {}


CTX = Context()


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


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
