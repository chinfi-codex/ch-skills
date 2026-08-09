"""Load and validate a Skill's compiled capability registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

import yaml


class RegistryError(ValueError):
    """A capability declaration is absent, stale or unsafe."""


def _read_mapping(path: Path) -> Dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"missing runtime declaration: {path}") from exc
    except yaml.YAMLError as exc:
        raise RegistryError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RegistryError(f"{path} must contain a YAML mapping")
    return payload


def resolve_entry(skill_root: Path, raw: str) -> Path:
    """Resolve a package-local entry, with a source-tree shared fallback."""
    if not raw or Path(raw).is_absolute() or ".." in Path(raw).parts:
        raise RegistryError(f"capability entry must be a safe relative path: {raw!r}")
    direct = (skill_root / raw).resolve()
    if direct.is_file():
        return direct
    prefix = "scripts/_shared/"
    if raw.startswith(prefix):
        relative = raw[len(prefix) :]
        for parent in (skill_root, *skill_root.parents):
            candidate = (parent / "shared" / relative).resolve()
            if candidate.is_file():
                return candidate
    raise RegistryError(f"capability entry does not exist: {raw}")


def load_registry(skill_root: Path) -> Dict[str, Any]:
    skill_root = skill_root.resolve()
    capabilities = _read_mapping(skill_root / "capabilities.yaml")
    if int(capabilities.get("schema_version") or 0) != 1:
        raise RegistryError("capabilities schema_version must be 1")
    skill_name = str(capabilities.get("skill") or "").strip()
    if not skill_name or skill_name != skill_root.name:
        raise RegistryError(
            f"capabilities skill {skill_name!r} must equal directory {skill_root.name!r}"
        )
    raw_capabilities = capabilities.get("capabilities")
    if not isinstance(raw_capabilities, Mapping) or not raw_capabilities:
        raise RegistryError("capabilities declaration needs a non-empty capabilities mapping")

    normalised: Dict[str, Any] = {}
    for capability_id, raw in raw_capabilities.items():
        if not isinstance(raw, Mapping):
            raise RegistryError(f"capability {capability_id!r} must be a mapping")
        kind = str(raw.get("kind") or "command")
        if kind not in {"command", "finalize"}:
            raise RegistryError(f"capability {capability_id!r} has unsupported kind {kind!r}")
        terminal = bool(raw.get("terminal", False))
        output_ids = [str(item) for item in (raw.get("outputs") or [])]
        if terminal and not output_ids:
            raise RegistryError(f"terminal capability {capability_id!r} must declare outputs")
        if kind == "finalize" and not terminal:
            raise RegistryError(f"finalize capability {capability_id!r} must be terminal")
        entry = str(raw.get("entry") or "")
        if kind == "command":
            resolve_entry(skill_root, entry)
        normalised[str(capability_id)] = {
            **dict(raw),
            "kind": kind,
            "terminal": terminal,
            "outputs": output_ids,
            "entry": entry,
        }

    try:
        import json

        gate_plan = json.loads((skill_root / "gate-plan.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError("missing compiled gate-plan.json; run the Skill Factory") from exc
    except ValueError as exc:
        raise RegistryError(f"invalid gate-plan.json: {exc}") from exc
    if gate_plan.get("skill") != skill_name:
        raise RegistryError("gate-plan skill does not match capabilities skill")
    # Fail closed when someone edits outputs.yaml but forgets to rebuild.  The
    # factory hash is canonical YAML data, so comments and key order do not
    # create false staleness.
    try:
        from output_gate.compiler import compile_gate_plan, load_yaml_document

        current_plan = compile_gate_plan(load_yaml_document(skill_root / "outputs.yaml"))
    except Exception as exc:  # noqa: BLE001 - convert compiler errors at registry boundary
        raise RegistryError(f"cannot compile current outputs.yaml: {exc}") from exc
    if gate_plan.get("source_sha256") != current_plan.get("source_sha256"):
        raise RegistryError("stale gate-plan.json; run the Skill Factory")
    for output_id, output in (gate_plan.get("outputs") or {}).items():
        terminal_id = str(output.get("terminal_capability") or "")
        capability = normalised.get(terminal_id)
        if not capability or not capability["terminal"] or output_id not in capability["outputs"]:
            raise RegistryError(
                f"output {output_id!r} is not owned by terminal capability {terminal_id!r}"
            )
    return {
        "schema_version": 1,
        "skill": skill_name,
        "skill_root": skill_root,
        "capabilities": normalised,
        "gate_plan": gate_plan,
    }
