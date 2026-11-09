"""主动目标的配置与基础 I/O。

读取 config/goals.yaml、policy.yaml、sources.yaml，解析金库与运行态目录。
读写 helper 与 AlphaVault system/tools/_common.py 的同名函数行为等价，随包内置，
项目因此不依赖金库内的 Python 模块。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = SKILL_DIR / "config"
# 安装器切换到 Hermes 自带解释器（无 PyYAML）时，用它传递已解析的配置快照。
SETTINGS_ENV = "PROACTIVE_SETTINGS_JSON"


def _load_yaml(name: str) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式无效：config/{name}")
    return data


def _load_config() -> dict[str, Any]:
    snapshot = os.environ.get(SETTINGS_ENV)
    if snapshot:
        return json.loads(snapshot)
    return {"goals": _load_yaml("goals.yaml"), "policy": _load_yaml("policy.yaml"),
            "sources": _load_yaml("sources.yaml")}


def settings_snapshot() -> str:
    """序列化当前配置，供子进程在没有 PyYAML 的解释器里复用。"""
    return json.dumps(_load_config(), ensure_ascii=False)


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(value))


class Settings:
    """一次性解析的配置快照；路径不做存在性检查，缺失由调用方按场景报错。"""

    def __init__(self) -> None:
        config = _load_config()
        goals = config["goals"]
        self.goals = goals
        self.policy = config["policy"]
        self.sources = (config["sources"].get("sources") or {})
        self.upstream = (config["sources"].get("upstream") or {})
        self.goal_id = str(goals["goal_id"])
        paths = goals.get("paths") or {}
        self.vault_root = _expand(os.environ.get("ALPHAVAULT_ROOT") or paths["vault_root"])
        self.runtime_home = _expand(os.environ.get("PROACTIVE_HOME") or paths["runtime_home"])
        self.inputs = goals.get("inputs") or {}
        methodology = goals.get("methodology") or {}
        self.framework_files = list(methodology.get("files") or [])
        self.rules_dir = str(methodology.get("rules_dir") or "")
        self.data_scope = list(goals.get("data_scope") or [])

    def source_enabled(self, kind: str) -> bool:
        return bool((self.sources.get(kind) or {}).get("enabled", True))

    def source_rank(self) -> dict[str, int]:
        return {kind: int(row.get("rank", 99)) for kind, row in self.sources.items()}


def load_settings() -> Settings:
    return Settings()


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path, *, strict: bool = True) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if strict:
                raise ValueError(f"{path.name} 第 {lineno} 行 JSON 无效: {exc}") from exc
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def file_hash(path: Path, length: int | None = 16) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    hexed = digest.hexdigest()
    return hexed[:length] if length else hexed


def atomic_write_json(path: Path, data: Any) -> None:
    """同目录临时文件 + os.replace 原子替换；运行态目录按需创建。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
