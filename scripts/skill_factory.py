#!/usr/bin/env python3
"""Build and lint production contracts for the explicitly managed Skills."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "shared"))

from output_gate.compiler import (  # noqa: E402
    ContractConfigError,
    canonical_json,
    compile_gate_plan,
    load_yaml_document,
)


class FactoryError(ValueError):
    """A managed Skill violates the generation-time contract."""


def _load_config(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FactoryError(f"missing factory config: {path}") from exc
    except yaml.YAMLError as exc:
        raise FactoryError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
        raise FactoryError("skill-framework.yaml must be a schema_version 1 mapping")
    return payload


def managed_roots(config: Mapping[str, Any]) -> list[Path]:
    raw = config.get("managed_skills")
    if not isinstance(raw, list) or not raw:
        raise FactoryError("skill-framework.yaml needs managed_skills")
    roots: list[Path] = []
    for item in raw:
        rel = Path(str(item))
        if rel.is_absolute() or ".." in rel.parts:
            raise FactoryError(f"managed Skill path must be repository-relative: {item}")
        root = (REPO_ROOT / rel).resolve()
        if REPO_ROOT not in root.parents:
            raise FactoryError(f"managed Skill escapes repository: {item}")
        roots.append(root)
    return roots


def _frontmatter_name(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        raise FactoryError(f"{path} needs YAML frontmatter")
    payload = yaml.safe_load(match.group(1))
    if not isinstance(payload, dict):
        raise FactoryError(f"{path} frontmatter must be a mapping")
    return str(payload.get("name") or "")


def _safe_entry(skill_root: Path, raw: str) -> Path:
    rel = Path(raw)
    if not raw or rel.is_absolute() or ".." in rel.parts:
        raise FactoryError(f"unsafe capability entry {raw!r} in {skill_root.name}")
    path = (skill_root / rel).resolve()
    if skill_root not in path.parents or not path.is_file():
        raise FactoryError(f"capability entry does not exist: {path}")
    return path


def compile_skill(skill_root: Path) -> tuple[dict[str, Any], list[str]]:
    skill_root = skill_root.resolve()
    if not skill_root.is_dir():
        raise FactoryError(f"managed Skill directory is missing: {skill_root}")
    name = _frontmatter_name(skill_root / "SKILL.md")
    if name != skill_root.name:
        raise FactoryError(f"SKILL.md name {name!r} must equal directory {skill_root.name!r}")
    if not (skill_root / "agents" / "openai.yaml").is_file():
        raise FactoryError(f"managed Skill needs agents/openai.yaml: {skill_root}")

    capabilities_doc = load_yaml_document(skill_root / "capabilities.yaml")
    outputs_doc = load_yaml_document(skill_root / "outputs.yaml")
    for label, document in (("capabilities", capabilities_doc), ("outputs", outputs_doc)):
        if int(document.get("schema_version") or 0) != 1:
            raise FactoryError(f"{label}.yaml schema_version must be 1 in {name}")
        if document.get("skill") != name:
            raise FactoryError(f"{label}.yaml skill must be {name!r}")

    capabilities = capabilities_doc.get("capabilities")
    if not isinstance(capabilities, Mapping) or not capabilities:
        raise FactoryError(f"{name} needs a non-empty capabilities mapping")
    problems: list[str] = []
    for capability_id, raw in capabilities.items():
        if not isinstance(raw, Mapping):
            problems.append(f"capability {capability_id!r} must be a mapping")
            continue
        kind = str(raw.get("kind") or "command")
        terminal = bool(raw.get("terminal", False))
        if kind not in {"command", "finalize"}:
            problems.append(f"capability {capability_id!r}: unsupported kind {kind!r}")
        if kind == "command":
            try:
                _safe_entry(skill_root, str(raw.get("entry") or ""))
            except FactoryError as exc:
                problems.append(str(exc))
        if kind == "finalize" and not terminal:
            problems.append(f"capability {capability_id!r}: finalize must be terminal")
        judgment = str(raw.get("judgment") or "")
        if judgment not in {"forbidden", "allowed"}:
            problems.append(f"capability {capability_id!r}: judgment must be forbidden or allowed")
        effects = raw.get("effects")
        if not isinstance(effects, list) or not effects:
            problems.append(f"capability {capability_id!r}: effects must be a non-empty list")

    registered_entries = {
        str(raw.get("entry"))
        for raw in capabilities.values()
        if isinstance(raw, Mapping) and raw.get("entry")
    }
    skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    referenced_entries = {
        match
        for match in re.findall(r"(?<!_shared/)(scripts/[A-Za-z0-9_./-]+\.py)", skill_text)
        if (skill_root / match).is_file()
    }
    for missing in sorted(referenced_entries - registered_entries):
        problems.append(f"SKILL.md references unregistered executable: {missing}")

    gate_plan = compile_gate_plan(outputs_doc)
    for output_id, output in gate_plan["outputs"].items():
        terminal_id = output["terminal_capability"]
        capability = capabilities.get(terminal_id)
        if not isinstance(capability, Mapping):
            problems.append(f"output {output_id!r}: unknown terminal capability {terminal_id!r}")
            continue
        if not capability.get("terminal"):
            problems.append(f"output {output_id!r}: {terminal_id!r} is not terminal")
        if output_id not in [str(item) for item in (capability.get("outputs") or [])]:
            problems.append(f"output {output_id!r}: not listed in {terminal_id!r}.outputs")
    return gate_plan, problems


def process_skill(skill_root: Path, *, write: bool) -> list[str]:
    try:
        plan, problems = compile_skill(skill_root)
    except (FactoryError, ContractConfigError, OSError, ValueError) as exc:
        return [str(exc)]
    if problems:
        return problems
    expected = canonical_json(plan)
    target = skill_root / "gate-plan.json"
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".json.tmp")
        temp.write_text(expected, encoding="utf-8")
        os.replace(temp, target)
        return []
    if not target.is_file():
        return [f"missing generated gate plan: {target}"]
    actual = target.read_text(encoding="utf-8")
    return [] if actual == expected else [f"stale generated gate plan: {target}"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile and lint managed Skill production contracts.")
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "skill-framework.yaml"
    )
    parser.add_argument("command", choices=("build-managed", "check-managed"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _load_config(args.config)
        roots = managed_roots(config)
    except FactoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    failed = False
    for root in roots:
        problems = process_skill(root, write=args.command == "build-managed")
        if problems:
            failed = True
            print(f"FAIL {root.relative_to(REPO_ROOT)}", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
        else:
            verb = "built" if args.command == "build-managed" else "checked"
            print(f"OK {verb}: {root.relative_to(REPO_ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
