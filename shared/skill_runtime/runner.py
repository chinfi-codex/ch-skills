#!/usr/bin/env python3
"""Execute registered Skill capabilities and gate every terminal artifact."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

_THIS_DIR = Path(__file__).resolve().parent
_SHARED_ROOT = _THIS_DIR.parent
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from output_gate.gate import GateExecutionError, run_gate  # noqa: E402
from skill_runtime.receipts import (  # noqa: E402
    append_receipt,
    artifact_record,
    read_receipts,
    sha256_file,
)
from skill_runtime.registry import RegistryError, load_registry, resolve_entry  # noqa: E402


_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)(?!\d)")


class RuntimeFailure(RuntimeError):
    """A capability failed preflight, execution or its delivery gate."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_key(values: Iterable[str]) -> str:
    for value in values:
        match = _DATE_RE.search(value)
        if match:
            return "-".join(match.groups())
    return ""


def _inside(root: Path, raw: Path) -> Path:
    path = raw if raw.is_absolute() else root / raw
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeFailure(f"path escapes Skill root: {raw}") from exc
    return resolved


def _staging_path(final_path: Path, run_id: str) -> Path:
    return final_path.parent / ".staging" / run_id / final_path.name


def _audit_path(final_path: Path, run_id: str) -> Path:
    return final_path.parent / ".audits" / run_id / (final_path.name + ".gate-audit.json")


def _staged_audit_path(staged_path: Path, run_id: str) -> Path:
    return staged_path.with_name(staged_path.name + f".{run_id}.gate-audit.json")


def _check_required_env(capability: Mapping[str, Any]) -> None:
    missing = [name for name in capability.get("required_env") or [] if not os.environ.get(str(name))]
    if missing:
        raise RuntimeFailure(f"required environment variables are missing: {', '.join(missing)}")


def _receipt_output_matches(receipt: Mapping[str, Any], path: Path, digest: str) -> bool:
    return any(
        Path(str(item.get("path") or "")).resolve() == path.resolve()
        and item.get("sha256") == digest
        for item in receipt.get("outputs") or []
    )


def _check_dependencies(
    capability: Mapping[str, Any],
    receipts_path: Path,
    date_key: str,
    evidence: Sequence[Path],
) -> None:
    required = [str(item) for item in (capability.get("requires_receipts") or [])]
    if not required:
        return
    if not date_key:
        raise RuntimeFailure("cannot verify required receipts because no YYYY-MM-DD date was found")
    rows = read_receipts(receipts_path)
    matching = {
        item: [
            row
            for row in rows
            if row.get("status") == "success"
            and row.get("capability_id") == item
            and row.get("date_key") == date_key
        ]
        for item in required
    }
    missing = [item for item, receipts in matching.items() if not receipts]
    if missing:
        raise RuntimeFailure(
            f"missing successful same-date prerequisite receipts: {', '.join(missing)}"
        )
    prerequisite_receipts = [receipt for receipts in matching.values() for receipt in receipts]
    unbound: list[str] = []
    for path in evidence:
        if not path.is_file():
            unbound.append(str(path))
            continue
        digest = sha256_file(path)
        if not any(
            _receipt_output_matches(receipt, path, digest)
            for receipt in prerequisite_receipts
        ):
            unbound.append(str(path))
    if unbound:
        raise RuntimeFailure(
            "evidence is not an exact output of a successful same-date prerequisite receipt: "
            + ", ".join(unbound)
        )


def _normalise_user_args(args: Sequence[str]) -> list[str]:
    values = list(args)
    return values[1:] if values[:1] == ["--"] else values


def _argument_values(args: Sequence[str]) -> Iterable[str]:
    for raw in args:
        value = str(raw)
        if value.startswith("--") and "=" in value:
            yield value.split("=", 1)[1]
        elif not value.startswith("-"):
            yield value


def _reject_terminal_targets(
    *, skill_root: Path, args: Sequence[str], gate_plan: Mapping[str, Any]
) -> None:
    patterns = [str(output.get("path_glob") or "") for output in (gate_plan.get("outputs") or {}).values()]
    for raw in _argument_values(args):
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                relative = candidate.resolve().relative_to(skill_root).as_posix()
            except ValueError:
                continue
        else:
            relative = candidate.as_posix()
        if any(pattern and fnmatch.fnmatch(relative, pattern) for pattern in patterns):
            raise RuntimeFailure(
                f"non-terminal capability arguments may not target declared final output: {raw}"
            )


def _reject_bound_overrides(args: Sequence[str], blocked: Iterable[str]) -> None:
    blocked_set = {item for item in blocked if item}
    for value in args:
        if value in blocked_set or any(value.startswith(item + "=") for item in blocked_set):
            raise RuntimeFailure(f"runtime-bound argument may not be overridden: {value}")


def _discover_outputs(skill_root: Path, patterns: Iterable[str], started_ns: int) -> list[Path]:
    found: list[Path] = []
    for pattern in patterns:
        if Path(str(pattern)).is_absolute() or ".." in Path(str(pattern)).parts:
            raise RuntimeFailure(f"unsafe receipt output pattern: {pattern}")
        for path in skill_root.glob(str(pattern)):
            if path.is_file() and path.stat().st_mtime_ns >= started_ns:
                found.append(path.resolve())
    return sorted(set(found))


def _write_capture(path: Path, stdout: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(stdout, encoding="utf-8")
    os.replace(temp, path)


def _receipt_base(
    *,
    run_id: str,
    registry: Mapping[str, Any],
    capability_id: str,
    capability: Mapping[str, Any],
    started_at: str,
    started: float,
    date_key: str,
    argv: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "skill": registry["skill"],
        "capability_id": capability_id,
        "kind": capability["kind"],
        "terminal": bool(capability["terminal"]),
        "started_at": started_at,
        "finished_at": _now(),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "date_key": date_key,
        "argv": list(argv),
    }


def execute_capability(
    *,
    skill_root: Path,
    capability_id: str,
    user_args: Sequence[str] = (),
    output_id: str = "",
    final_path: Optional[Path] = None,
    staged_path: Optional[Path] = None,
    source_artifact: Optional[Path] = None,
    evidence: Sequence[Path] = (),
    capture_stdout: Optional[Path] = None,
) -> dict[str, Any]:
    registry = load_registry(skill_root)
    root = Path(registry["skill_root"])
    capability = registry["capabilities"].get(capability_id)
    if not isinstance(capability, Mapping):
        raise RuntimeFailure(f"unknown capability id: {capability_id}")
    args = _normalise_user_args(user_args)
    run_id = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:10]}"
    receipts_path = root / "reports" / ".receipts.jsonl"
    resolved_final = _inside(root, final_path) if final_path else None
    resolved_source = _inside(root, source_artifact) if source_artifact else None
    resolved_evidence = [_inside(root, path) for path in evidence]
    resolved_capture = _inside(root, capture_stdout) if capture_stdout else None
    date_key = _date_key(
        [str(item) for item in (resolved_final, resolved_source, resolved_capture) if item]
        + [str(item) for item in resolved_evidence]
        + args
    )
    started_at = _now()
    started = time.monotonic()
    started_ns = time.time_ns()
    command: list[str] = []
    subprocess_result: Optional[subprocess.CompletedProcess[str]] = None
    audit: Optional[dict[str, Any]] = None
    audit_path: Optional[Path] = None
    output_paths: list[Path] = []
    status = "failed"
    error = ""
    resolved_staging: Optional[Path] = None

    try:
        _check_required_env(capability)
        if capability["terminal"]:
            if not output_id or output_id not in capability["outputs"]:
                raise RuntimeFailure(
                    f"terminal capability requires --output-id in {capability['outputs']}"
                )
            if resolved_final is None:
                raise RuntimeFailure("terminal capability requires --final-path")
            declared_terminal = registry["gate_plan"]["outputs"][output_id]["terminal_capability"]
            if declared_terminal != capability_id:
                raise RuntimeFailure(
                    f"output {output_id!r} belongs to {declared_terminal!r}, not {capability_id!r}"
                )
            if resolved_capture is not None:
                raise RuntimeFailure("terminal capability does not support --capture-stdout")
        _check_dependencies(capability, receipts_path, date_key, resolved_evidence)

        if capability["kind"] == "finalize":
            if args:
                raise RuntimeFailure("finalize capability does not accept passthrough arguments")
            if staged_path is None:
                raise RuntimeFailure("finalize capability requires --staged-path")
            resolved_staging = _inside(root, staged_path)
            if ".staging" not in resolved_staging.relative_to(root).parts:
                raise RuntimeFailure("model-authored artifact must be inside a .staging directory")
            if not resolved_staging.is_file():
                raise RuntimeFailure(f"staged artifact is missing: {resolved_staging}")
        else:
            command = [sys.executable, str(resolve_entry(root, str(capability["entry"])))]
            command.extend(str(item) for item in (capability.get("fixed_args") or []))
            if capability["terminal"]:
                resolved_staging = _staging_path(resolved_final, run_id)  # type: ignore[arg-type]
                resolved_staging.parent.mkdir(parents=True, exist_ok=True)
                artifact_arg = str(capability.get("artifact_arg") or "")
                if not artifact_arg:
                    raise RuntimeFailure("terminal command capability needs artifact_arg")
                source_arg = str(capability.get("source_arg") or "")
                evidence_arg = str(capability.get("evidence_arg") or "")
                blocked_args = [artifact_arg, source_arg, evidence_arg]
                blocked_args.extend(str(item) for item in (capability.get("bound_arg_aliases") or []))
                _reject_bound_overrides(args, blocked_args)
                command.extend([artifact_arg, str(resolved_staging)])
                if source_arg:
                    if resolved_source is None:
                        raise RuntimeFailure(f"terminal capability requires --source-artifact for {source_arg}")
                    command.extend([source_arg, str(resolved_source)])
                if evidence_arg:
                    if not resolved_evidence:
                        raise RuntimeFailure(f"terminal capability requires --evidence for {evidence_arg}")
                    command.extend([evidence_arg, str(resolved_evidence[0])])
                per_output = (capability.get("per_output") or {}).get(output_id) or {}
                command.extend(str(item) for item in (per_output.get("args") or []))
            else:
                _reject_terminal_targets(
                    skill_root=root, args=args, gate_plan=registry["gate_plan"]
                )
                if resolved_capture is not None:
                    _reject_terminal_targets(
                        skill_root=root,
                        args=[str(resolved_capture)],
                        gate_plan=registry["gate_plan"],
                    )
            command.extend(args)
            subprocess_result = subprocess.run(
                command,
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            if subprocess_result.returncode != 0:
                raise RuntimeFailure(
                    f"capability process exited {subprocess_result.returncode}: "
                    f"{subprocess_result.stderr[-2000:].strip()}"
                )
            if resolved_capture is not None:
                _write_capture(resolved_capture, subprocess_result.stdout)
                output_paths.append(resolved_capture)

        if capability["terminal"]:
            assert resolved_staging is not None and resolved_final is not None
            if not resolved_staging.is_file():
                raise RuntimeFailure(f"terminal capability did not create {resolved_staging}")
            audit_path = _staged_audit_path(resolved_staging, run_id)
            audit = run_gate(
                skill_root=root,
                gate_plan=registry["gate_plan"],
                output_id=output_id,
                artifact=resolved_staging,
                final_path=resolved_final,
                receipts_path=receipts_path,
                audit_path=audit_path,
                evidence=resolved_evidence,
                source_artifact=resolved_source,
                run_id=run_id,
                capability_id=capability_id,
            )
            if not audit["gate_pass"]:
                failures = [row["message"] for row in audit["validators"] if row["status"] == "fail"]
                raise RuntimeFailure("output gate failed: " + "; ".join(failures[:5]))
            delivered_audit = _audit_path(resolved_final, run_id)
            delivered_audit.parent.mkdir(parents=True, exist_ok=True)
            os.replace(audit_path, delivered_audit)
            audit_path = delivered_audit
            resolved_final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(resolved_staging, resolved_final)
            output_paths = [resolved_final]
        else:
            output_paths.extend(
                _discover_outputs(root, capability.get("receipt_outputs") or [], started_ns)
            )

        if not date_key:
            date_key = _date_key(str(path) for path in output_paths)

        status = "success"
    except Exception as exc:  # noqa: BLE001 - receipt every failure boundary
        error = f"{type(exc).__name__}: {exc}"

    receipt = _receipt_base(
        run_id=run_id,
        registry=registry,
        capability_id=capability_id,
        capability=capability,
        started_at=started_at,
        started=started,
        date_key=date_key,
        argv=command or args,
    )
    receipt.update(
        {
            "status": status,
            "exit_code": subprocess_result.returncode if subprocess_result else (0 if status == "success" else 1),
            "stdout_tail": subprocess_result.stdout[-4000:] if subprocess_result else "",
            "stderr_tail": subprocess_result.stderr[-4000:] if subprocess_result else "",
            "outputs": [artifact_record(path) for path in output_paths if path.is_file()],
            "evidence": [artifact_record(path) for path in resolved_evidence if path.is_file()],
            "source_artifact": artifact_record(resolved_source) if resolved_source and resolved_source.is_file() else None,
            "audit": str(audit_path) if audit and audit_path else None,
            "audit_sha256": (
                sha256_file(audit_path)
                if audit and audit_path and audit_path.is_file()
                else None
            ),
            "gate_pass": audit.get("gate_pass") if audit else None,
            "staging_path": str(resolved_staging) if resolved_staging else None,
            "error": error or None,
        }
    )
    append_receipt(receipts_path, receipt)
    if status != "success":
        raise RuntimeFailure(f"run_id={run_id}; {error}; receipt={receipts_path}")
    return receipt


def verify_delivery(
    *, skill_root: Path, output_id: str, artifact: Path, receipt_run_id: str = ""
) -> dict[str, Any]:
    registry = load_registry(skill_root)
    root = Path(registry["skill_root"])
    delivered = _inside(root, artifact)
    receipts_path = root / "reports" / ".receipts.jsonl"
    rows = read_receipts(receipts_path)
    matching = None
    digest = sha256_file(delivered) if delivered.is_file() else ""
    for row in reversed(rows):
        if row.get("status") != "success" or not row.get("terminal"):
            continue
        if receipt_run_id and row.get("run_id") != receipt_run_id:
            continue
        if any(
            Path(str(item.get("path") or "")).resolve() == delivered.resolve()
            and item.get("sha256") == digest
            for item in row.get("outputs") or []
        ):
            matching = row
            break
    if matching is None:
        raise RuntimeFailure("delivered artifact has no matching successful terminal receipt")
    audit_raw = str(matching.get("audit") or "")
    if not audit_raw:
        raise RuntimeFailure("matching receipt has no gate audit")
    audit_path = _inside(root, Path(audit_raw))
    if not audit_path.is_file():
        raise RuntimeFailure("matching receipt has no gate audit")
    if matching.get("audit_sha256") != sha256_file(audit_path):
        raise RuntimeFailure("gate audit hash does not match the delivery receipt")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if not audit.get("gate_pass") or audit.get("output_id") != output_id:
        raise RuntimeFailure("matching gate audit is not a passing audit for this output")
    if audit.get("run_id") != matching.get("run_id"):
        raise RuntimeFailure("gate audit run_id does not match the delivery receipt")
    if audit.get("artifact_sha256") != digest:
        raise RuntimeFailure("gate audit artifact hash does not match the delivered artifact")
    if Path(str(audit.get("final_path") or "")).resolve() != delivered.resolve():
        raise RuntimeFailure("gate audit final path does not match the delivered artifact")
    return {
        "verified": True,
        "artifact": str(delivered),
        "sha256": digest,
        "run_id": matching["run_id"],
        "audit": str(audit_path),
        "gate_pass": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run registered Skill capabilities with receipts and output gates.")
    parser.add_argument("--skill-root", default=".", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Execute one registered capability.")
    run.add_argument("capability_id")
    run.add_argument("--output-id", default="")
    run.add_argument("--final-path", type=Path)
    run.add_argument("--staged-path", type=Path)
    run.add_argument("--source-artifact", type=Path)
    run.add_argument("--evidence", action="append", default=[], type=Path)
    run.add_argument("--capture-stdout", type=Path)

    verify = subparsers.add_parser("verify", help="Verify a delivered artifact against receipt and audit.")
    verify.add_argument("--output-id", required=True)
    verify.add_argument("--artifact", required=True, type=Path)
    verify.add_argument("--run-id", default="")
    return parser


def parse_cli(argv: Optional[Sequence[str]] = None) -> tuple[argparse.Namespace, list[str]]:
    raw = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in raw:
        index = raw.index("--")
        passthrough = raw[index + 1 :]
        raw = raw[:index]
    args = build_parser().parse_args(raw)
    if passthrough and args.command != "run":
        raise RuntimeFailure("passthrough arguments are only valid for the run command")
    return args, passthrough


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args, passthrough = parse_cli(argv)
        if args.command == "run":
            result = execute_capability(
                skill_root=args.skill_root,
                capability_id=args.capability_id,
                user_args=passthrough,
                output_id=args.output_id,
                final_path=args.final_path,
                staged_path=args.staged_path,
                source_artifact=args.source_artifact,
                evidence=args.evidence,
                capture_stdout=args.capture_stdout,
            )
        else:
            result = verify_delivery(
                skill_root=args.skill_root,
                output_id=args.output_id,
                artifact=args.artifact,
                receipt_run_id=args.run_id,
            )
    except (RegistryError, GateExecutionError, RuntimeFailure) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
