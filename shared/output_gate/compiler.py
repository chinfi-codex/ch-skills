"""Compile declarative output requirements into a deterministic gate plan.

The compiler runs while a Skill is built.  Runtime execution consumes the
compiled JSON plan and never asks the model to choose validators ad hoc.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

import yaml


SCHEMA_VERSION = 1

BASE_VALIDATORS = (
    "artifact-exists",
    "path-policy",
    "artifact-size",
)

TYPE_VALIDATORS = {
    "markdown": ("utf8", "markdown-headings", "regex-policy"),
    "html": ("utf8", "html-document", "html-headings", "regex-policy"),
    "json": ("utf8", "json-parse", "regex-policy"),
}

FEATURE_VALIDATORS = {
    "evidence-backed": ("evidence-required", "date-alignment"),
    "preserve-source-text": ("source-receipt", "source-text-preserved"),
    "browser-attestation": ("browser-attestation",),
}

CUSTOM_VALIDATORS = {"command", "evidence-citations"}


class ContractConfigError(ValueError):
    """The output declaration cannot be compiled safely."""


def load_yaml_document(path: Path) -> Dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractConfigError(f"missing declaration: {path}") from exc
    except yaml.YAMLError as exc:
        raise ContractConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractConfigError(f"{path} must contain a YAML mapping")
    return payload


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalise_validator(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str):
        return {"id": raw, "severity": "hard", "config": {}}
    if not isinstance(raw, Mapping) or not raw.get("id"):
        raise ContractConfigError(f"validator must be a name or mapping with id: {raw!r}")
    severity = str(raw.get("severity") or "hard")
    if severity not in {"hard", "warning"}:
        raise ContractConfigError(f"validator {raw['id']!r} has invalid severity {severity!r}")
    config = raw.get("config") or {}
    if not isinstance(config, Mapping):
        raise ContractConfigError(f"validator {raw['id']!r} config must be a mapping")
    return {"id": str(raw["id"]), "severity": severity, "config": dict(config)}


def _validator_ids(output_type: str, features: Iterable[str]) -> list[str]:
    if output_type not in TYPE_VALIDATORS:
        raise ContractConfigError(
            f"unsupported output type {output_type!r}; expected one of {sorted(TYPE_VALIDATORS)}"
        )
    ordered: list[str] = []
    for validator_id in (*BASE_VALIDATORS, *TYPE_VALIDATORS[output_type]):
        if validator_id not in ordered:
            ordered.append(validator_id)
    for feature in features:
        if feature not in FEATURE_VALIDATORS:
            raise ContractConfigError(f"unknown output feature {feature!r}")
        for validator_id in FEATURE_VALIDATORS[feature]:
            if validator_id not in ordered:
                ordered.append(validator_id)
    return ordered


def _base_config(validator_id: str, output: Mapping[str, Any]) -> Dict[str, Any]:
    contract = dict(output.get("contract") or {})
    if validator_id == "path-policy":
        return {"path_glob": str(output.get("path_glob") or "")}
    if validator_id == "artifact-size":
        return {"min_bytes": int(contract.get("min_bytes") or 1)}
    if validator_id in {"markdown-headings", "html-headings"}:
        sections = []
        for raw in contract.get("sections") or []:
            if not isinstance(raw, Mapping):
                sections.append(raw)
                continue
            section = dict(raw)
            # The declaration speaks in Markdown heading levels.  The shared
            # renderer intentionally shifts Markdown h1→HTML h2, so compile a
            # different deterministic level for the HTML gate instead of
            # making every Skill duplicate two copies of the same contract.
            if validator_id == "html-headings" and section.get("level") is not None:
                section["level"] = int(section["level"]) + 1
            sections.append(section)
        return {
            "sections": sections,
            "order": str(contract.get("order") or "any"),
        }
    if validator_id == "regex-policy":
        return {
            "required_patterns": list(contract.get("required_patterns") or []),
            "forbidden_patterns": list(contract.get("forbidden_patterns") or []),
        }
    if validator_id == "browser-attestation":
        browser = dict(contract.get("browser") or {})
        browser.setdefault("expect_contract", str(output.get("contract_version") or "1"))
        return browser
    if validator_id == "date-alignment":
        return dict(contract.get("date_alignment") or {})
    return {}


def compile_gate_plan(outputs_document: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a stable JSON-serialisable gate plan."""
    schema_version = int(outputs_document.get("schema_version") or 0)
    if schema_version != SCHEMA_VERSION:
        raise ContractConfigError(
            f"outputs schema_version must be {SCHEMA_VERSION}, got {schema_version}"
        )
    skill_name = str(outputs_document.get("skill") or "").strip()
    if not skill_name:
        raise ContractConfigError("outputs declaration needs a non-empty skill")
    raw_outputs = outputs_document.get("outputs")
    if not isinstance(raw_outputs, Mapping) or not raw_outputs:
        raise ContractConfigError("outputs declaration needs a non-empty outputs mapping")

    compiled: Dict[str, Any] = {}
    for output_id, raw in raw_outputs.items():
        if not isinstance(raw, Mapping):
            raise ContractConfigError(f"output {output_id!r} must be a mapping")
        output_type = str(raw.get("type") or "")
        terminal = str(raw.get("terminal_capability") or "").strip()
        path_glob = str(raw.get("path_glob") or "").strip()
        if not terminal or not path_glob:
            raise ContractConfigError(
                f"output {output_id!r} needs terminal_capability and path_glob"
            )
        features = [str(item) for item in (raw.get("features") or [])]
        validators = [
            {
                "id": validator_id,
                "severity": "hard",
                "config": _base_config(validator_id, raw),
            }
            for validator_id in _validator_ids(output_type, features)
        ]
        positions = {row["id"]: idx for idx, row in enumerate(validators)}
        for custom in raw.get("validators") or []:
            row = _normalise_validator(custom)
            if row["id"] not in CUSTOM_VALIDATORS and row["id"] not in positions:
                raise ContractConfigError(
                    f"output {output_id!r} references unregistered validator {row['id']!r}"
                )
            if row["id"] == "command" and not isinstance(row["config"].get("command"), list):
                raise ContractConfigError(
                    f"output {output_id!r} command validator needs config.command list"
                )
            if row["id"] in positions:
                previous = validators[positions[row["id"]]]
                row["config"] = {**previous["config"], **row["config"]}
                validators[positions[row["id"]]] = row
            else:
                positions[row["id"]] = len(validators)
                validators.append(row)

        policy = str(raw.get("gate_policy") or "hard")
        if policy not in {"hard", "advisory"}:
            raise ContractConfigError(
                f"output {output_id!r} gate_policy must be hard or advisory"
            )
        compiled[str(output_id)] = {
            "type": output_type,
            "terminal_capability": terminal,
            "path_glob": path_glob,
            "source_output": raw.get("source_output"),
            "contract_version": str(raw.get("contract_version") or "1"),
            "gate_policy": policy,
            "features": features,
            "contract": dict(raw.get("contract") or {}),
            "validators": validators,
        }

    canonical_source = json.dumps(outputs_document, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {
        "schema_version": SCHEMA_VERSION,
        "skill": skill_name,
        "source_sha256": _sha256_bytes(canonical_source),
        "outputs": compiled,
    }


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
