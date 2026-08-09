"""Execute a compiled gate plan against one staged or delivered artifact."""

from __future__ import annotations

import fnmatch
import hashlib
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional


_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_HTML_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_NUMBERING_RE = re.compile(r"^\s*\d+(?:\.\d+)*(?:\s*[.、]\s*|\s+)")
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)(?!\d)")


class GateExecutionError(RuntimeError):
    """The gate itself is misconfigured or could not execute."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _result(validator_id: str, ok: bool, message: str, **detail: Any) -> Dict[str, Any]:
    return {
        "id": validator_id,
        "status": "pass" if ok else "fail",
        "message": message,
        "detail": detail,
    }


def _strip_numbering(text: str) -> str:
    return _NUMBERING_RE.sub("", text).strip()


def _markdown_without_fences(text: str) -> str:
    out: list[str] = []
    fenced = False
    marker = ""
    for line in text.splitlines():
        match = re.match(r"^\s*(```+|~~~+)", line)
        if match:
            token = match.group(1)
            if not fenced:
                fenced = True
                marker = token[0]
            elif token[0] == marker:
                fenced = False
            continue
        if not fenced:
            out.append(line)
    return "\n".join(out)


def _headings(text: str, kind: str) -> list[Dict[str, Any]]:
    found: list[Dict[str, Any]] = []
    if kind == "markdown":
        for match in _MD_HEADING_RE.finditer(_markdown_without_fences(text)):
            title = re.sub(r"[*_`]+", "", match.group(2)).strip()
            found.append({"level": len(match.group(1)), "text": title})
    else:
        for match in _HTML_HEADING_RE.finditer(text):
            title = html.unescape(_TAG_RE.sub("", match.group(2))).strip()
            found.append({"level": int(match.group(1)), "text": title})
    for row in found:
        row["stripped"] = _strip_numbering(str(row["text"]))
    return found


def _match_sections(text: str, kind: str, config: Mapping[str, Any]) -> Dict[str, Any]:
    headings = _headings(text, kind)
    matched: list[tuple[str, int]] = []
    problems: list[str] = []
    sections = config.get("sections") or []
    for spec in sections:
        if not isinstance(spec, Mapping) or not spec.get("key"):
            problems.append(f"invalid section spec: {spec!r}")
            continue
        patterns = spec.get("patterns") or []
        hits: list[int] = []
        for pattern in patterns:
            compiled = re.compile(str(pattern))
            hits = [
                idx for idx, heading in enumerate(headings)
                if compiled.search(str(heading["stripped"])) or compiled.search(str(heading["text"]))
            ]
            if hits:
                break
        required = bool(spec.get("required", True))
        if not hits:
            if required:
                problems.append(f"[{spec['key']}] required heading missing")
            continue
        if len(hits) != 1:
            problems.append(f"[{spec['key']}] heading is ambiguous ({len(hits)} matches)")
            continue
        idx = hits[0]
        expected_level = spec.get("level")
        if expected_level is not None and int(expected_level) != int(headings[idx]["level"]):
            problems.append(
                f"[{spec['key']}] expected level {expected_level}, got {headings[idx]['level']}"
            )
            continue
        matched.append((str(spec["key"]), idx))
    if str(config.get("order") or "any") == "strict":
        actual = [key for key, _ in sorted(matched, key=lambda item: item[1])]
        expected = [str(spec["key"]) for spec in sections if any(key == str(spec["key"]) for key, _ in matched)]
        if actual != expected:
            problems.append(f"strict heading order mismatch: expected {expected}, got {actual}")
    return {
        "ok": not problems,
        "problems": problems,
        "matched": [key for key, _ in matched],
        "headings": [row["text"] for row in headings],
    }


def _extract_date(value: str) -> str:
    match = _DATE_RE.search(value)
    return "-".join(match.groups()) if match else ""


def _read_receipts(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _resolve_path(skill_root: Path, raw: str) -> Path:
    direct = (skill_root / raw).resolve()
    if direct.exists():
        return direct
    prefix = "scripts/_shared/"
    if raw.startswith(prefix):
        relative = raw[len(prefix):]
        for parent in (skill_root, *skill_root.parents):
            candidate = parent / "shared" / relative
            if candidate.exists():
                return candidate.resolve()
    return direct


def _artifact_exists(ctx: Mapping[str, Any], _config: Mapping[str, Any]) -> Dict[str, Any]:
    path = Path(ctx["artifact"])
    return _result("artifact-exists", path.is_file(), f"artifact {'exists' if path.is_file() else 'is missing'}", path=str(path))


def _path_policy(ctx: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
    final_path = Path(ctx["final_path"])
    skill_root = Path(ctx["skill_root"])
    try:
        relative = final_path.resolve().relative_to(skill_root.resolve()).as_posix()
    except ValueError:
        return _result("path-policy", False, "final path escapes the Skill root", path=str(final_path))
    pattern = str(config.get("path_glob") or "")
    ok = bool(pattern) and fnmatch.fnmatch(relative, pattern)
    return _result("path-policy", ok, "final path matches declared glob" if ok else "final path does not match declared glob", path=relative, glob=pattern)


def _artifact_size(ctx: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
    path = Path(ctx["artifact"])
    size = path.stat().st_size if path.is_file() else 0
    minimum = int(config.get("min_bytes") or 1)
    return _result("artifact-size", size >= minimum, f"artifact size is {size} bytes", min_bytes=minimum, size=size)


def _utf8(ctx: Mapping[str, Any], _config: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        Path(ctx["artifact"]).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return _result("utf8", False, f"artifact is not readable UTF-8: {exc}")
    return _result("utf8", True, "artifact is valid UTF-8")


def _heading_validator(kind: str) -> Callable[[Mapping[str, Any], Mapping[str, Any]], Dict[str, Any]]:
    validator_id = f"{kind}-headings"

    def validate(ctx: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
        text = Path(ctx["artifact"]).read_text(encoding="utf-8")
        detail = _match_sections(text, kind, config)
        return _result(
            validator_id,
            bool(detail.pop("ok")),
            "declared headings resolved" if not detail["problems"] else "; ".join(detail["problems"]),
            **detail,
        )

    return validate


def _regex_policy(ctx: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
    text = Path(ctx["artifact"]).read_text(encoding="utf-8")
    missing = [pattern for pattern in config.get("required_patterns") or [] if not re.search(str(pattern), text, re.MULTILINE)]
    forbidden = [pattern for pattern in config.get("forbidden_patterns") or [] if re.search(str(pattern), text, re.MULTILINE)]
    ok = not missing and not forbidden
    return _result("regex-policy", ok, "regex policy satisfied" if ok else "required/forbidden pattern policy failed", missing=missing, forbidden=forbidden)


def _html_document(ctx: Mapping[str, Any], _config: Mapping[str, Any]) -> Dict[str, Any]:
    text = Path(ctx["artifact"]).read_text(encoding="utf-8")
    checks = {
        "doctype": bool(re.search(r"<!doctype\s+html", text, re.IGNORECASE)),
        "report_body": 'id="report-body"' in text,
        "title": bool(re.search(r"<title>.+?</title>", text, re.IGNORECASE | re.DOTALL)),
    }
    return _result("html-document", all(checks.values()), "HTML shell is complete" if all(checks.values()) else "HTML shell is incomplete", checks=checks)


def _json_parse(ctx: Mapping[str, Any], _config: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        payload = json.loads(Path(ctx["artifact"]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _result("json-parse", False, f"invalid JSON: {exc}")
    return _result("json-parse", True, "artifact is valid JSON", root_type=type(payload).__name__)


def _evidence_required(ctx: Mapping[str, Any], _config: Mapping[str, Any]) -> Dict[str, Any]:
    evidence = [Path(item) for item in ctx.get("evidence") or []]
    missing = [str(path) for path in evidence if not path.is_file()]
    ok = bool(evidence) and not missing
    return _result("evidence-required", ok, "evidence files are present" if ok else "evidence is missing", evidence=[str(path) for path in evidence], missing=missing)


def _collect_urls(value: Any) -> set[str]:
    urls: set[str] = set()
    if isinstance(value, Mapping):
        for item in value.values():
            urls.update(_collect_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.update(_collect_urls(item))
    elif isinstance(value, str):
        urls.update(re.findall(r"https?://[^\s<>'\")\]]+", value))
    return {url.rstrip(".,;，。；") for url in urls}


def _evidence_citations(ctx: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
    artifact_text = Path(ctx["artifact"]).read_text(encoding="utf-8")
    evidence_urls: set[str] = set()
    parse_errors: list[str] = []
    for raw in ctx.get("evidence") or []:
        path = Path(raw)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parse_errors.append(f"{path}: {exc}")
            continue
        evidence_urls.update(_collect_urls(payload))
    cited = sorted(url for url in evidence_urls if url in artifact_text)
    source_markers = re.findall(r"\[[^\]\n]{2,40}\](?!\()", artifact_text)
    marker_count = len(source_markers) if bool(config.get("allow_source_markers")) else 0
    minimum = int(config.get("min_citations") or 1)
    reference_count = len(cited) + marker_count
    ok = not parse_errors and reference_count >= minimum
    return _result(
        "evidence-citations",
        ok,
        "artifact carries evidence references" if ok else "artifact lacks traceable evidence references",
        min_citations=minimum,
        evidence_url_count=len(evidence_urls),
        cited_count=len(cited),
        cited=cited[:50],
        source_marker_count=marker_count,
        source_markers=source_markers[:50],
        parse_errors=parse_errors,
    )


def _date_alignment(ctx: Mapping[str, Any], _config: Mapping[str, Any]) -> Dict[str, Any]:
    expected = _extract_date(str(ctx["final_path"]))
    evidence_dates = sorted({_extract_date(str(path)) for path in ctx.get("evidence") or []} - {""})
    ok = bool(expected) and expected in evidence_dates
    return _result("date-alignment", ok, "artifact and evidence dates align" if ok else "artifact date is not represented by evidence filenames", artifact_date=expected, evidence_dates=evidence_dates)


def _source_receipt(ctx: Mapping[str, Any], _config: Mapping[str, Any]) -> Dict[str, Any]:
    source_raw = str(ctx.get("source_artifact") or "")
    if not source_raw:
        return _result("source-receipt", False, "source artifact was not supplied")
    source = Path(source_raw)
    if not source.is_file():
        return _result("source-receipt", False, "source artifact is missing", path=str(source))
    receipts = _read_receipts(Path(ctx["receipts_path"]))
    source_hash = _sha256(source)
    hit = None
    for row in reversed(receipts):
        if row.get("status") != "success" or not row.get("terminal"):
            continue
        for output in row.get("outputs") or []:
            if Path(str(output.get("path") or "")).resolve() == source.resolve() and output.get("sha256") == source_hash:
                hit = row
                break
        if hit:
            break
    return _result("source-receipt", hit is not None, "source artifact has a matching successful receipt" if hit else "source artifact has no matching successful receipt", source=str(source), sha256=source_hash, source_run_id=(hit or {}).get("run_id"))


def _source_text_preserved(ctx: Mapping[str, Any], _config: Mapping[str, Any]) -> Dict[str, Any]:
    source_raw = str(ctx.get("source_artifact") or "")
    if not source_raw:
        return _result("source-text-preserved", False, "source artifact was not supplied")
    source = Path(source_raw)
    try:
        from html_report.text_validator import validate_text_preserved

        validate_text_preserved(
            source.read_text(encoding="utf-8"),
            Path(ctx["artifact"]).read_text(encoding="utf-8"),
        )
    except Exception as exc:  # noqa: BLE001 - validator boundary
        return _result("source-text-preserved", False, f"source text preservation failed: {exc}")
    return _result("source-text-preserved", True, "source Markdown text is preserved")


def _browser_attestation(ctx: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
    script = str(config.get("script") or "scripts/_shared/html_report/render_check.py")
    script_path = _resolve_path(Path(ctx["skill_root"]), script)
    audit = Path(ctx["audit_path"]).with_name(Path(ctx["audit_path"]).stem + ".render.json")
    command = [
        sys.executable,
        str(script_path),
        "--target",
        str(ctx["artifact"]),
        "--stage",
        "local",
        "--out",
        str(audit),
    ]
    expected = str(config.get("expect_contract") or "")
    if expected:
        command.extend(["--expect-contract", expected])
    result = subprocess.run(command, cwd=ctx["skill_root"], text=True, capture_output=True, check=False)
    audit_sha256 = _sha256(audit) if audit.is_file() else None
    return _result(
        "browser-attestation",
        result.returncode == 0,
        "browser attestation passed" if result.returncode == 0 else "browser attestation failed",
        exit_code=result.returncode,
        audit=str(audit),
        audit_sha256=audit_sha256,
        stdout=result.stdout[-4000:],
        stderr=result.stderr[-4000:],
    )


def _command_validator(ctx: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
    validator_id = str(config.get("name") or "command")
    raw_command = config.get("command") or []
    if not isinstance(raw_command, list) or not raw_command:
        return _result(validator_id, False, "command validator has no command")
    evidence = list(ctx.get("evidence") or [])
    tokens = {
        "artifact": str(ctx["artifact"]),
        "final_path": str(ctx["final_path"]),
        "source_artifact": str(ctx.get("source_artifact") or ""),
        "evidence": str(evidence[0]) if evidence else "",
        "audit": str(ctx["audit_path"]),
    }
    command: list[str] = []
    for index, raw in enumerate(raw_command):
        value = str(raw).format(**tokens)
        if index == 0 and value in {"python", "python3"}:
            value = sys.executable
        elif value.startswith("scripts/"):
            value = str(_resolve_path(Path(ctx["skill_root"]), value))
        command.append(value)
    if any(not value for value in command):
        return _result(validator_id, False, "command validator is missing a required substitution", command=command)
    result = subprocess.run(command, cwd=ctx["skill_root"], text=True, capture_output=True, check=False)
    return _result(validator_id, result.returncode == 0, "custom command passed" if result.returncode == 0 else "custom command failed", exit_code=result.returncode, command=command, stdout=result.stdout[-4000:], stderr=result.stderr[-4000:])


VALIDATORS: Dict[str, Callable[[Mapping[str, Any], Mapping[str, Any]], Dict[str, Any]]] = {
    "artifact-exists": _artifact_exists,
    "path-policy": _path_policy,
    "artifact-size": _artifact_size,
    "utf8": _utf8,
    "markdown-headings": _heading_validator("markdown"),
    "html-headings": _heading_validator("html"),
    "regex-policy": _regex_policy,
    "html-document": _html_document,
    "json-parse": _json_parse,
    "evidence-required": _evidence_required,
    "evidence-citations": _evidence_citations,
    "date-alignment": _date_alignment,
    "source-receipt": _source_receipt,
    "source-text-preserved": _source_text_preserved,
    "browser-attestation": _browser_attestation,
    "command": _command_validator,
}


def run_gate(
    *,
    skill_root: Path,
    gate_plan: Mapping[str, Any],
    output_id: str,
    artifact: Path,
    final_path: Path,
    receipts_path: Path,
    audit_path: Path,
    evidence: Iterable[Path] = (),
    source_artifact: Optional[Path] = None,
    run_id: str = "",
    capability_id: str = "",
) -> Dict[str, Any]:
    outputs = gate_plan.get("outputs") or {}
    output = outputs.get(output_id) if isinstance(outputs, Mapping) else None
    if not isinstance(output, Mapping):
        raise GateExecutionError(f"unknown output id {output_id!r}")
    context = {
        "skill_root": str(skill_root.resolve()),
        "artifact": str(artifact.resolve()),
        "final_path": str(final_path.resolve()),
        "receipts_path": str(receipts_path.resolve()),
        "audit_path": str(audit_path.resolve()),
        "evidence": [str(path.resolve()) for path in evidence],
        "source_artifact": str(source_artifact.resolve()) if source_artifact else "",
        "run_id": run_id,
        "capability_id": capability_id,
    }
    results: list[Dict[str, Any]] = []
    hard_failures = 0
    warnings = 0
    for spec in output.get("validators") or []:
        validator_id = str(spec.get("id") or "")
        validator = VALIDATORS.get(validator_id)
        if validator is None:
            row = _result(validator_id or "unknown", False, "validator is not registered")
        else:
            try:
                row = validator(context, spec.get("config") or {})
            except Exception as exc:  # noqa: BLE001 - gate must return an audit
                row = _result(validator_id, False, f"validator crashed: {type(exc).__name__}: {exc}")
        severity = str(spec.get("severity") or "hard")
        row["severity"] = severity
        results.append(row)
        if row["status"] == "fail":
            if severity == "warning":
                warnings += 1
            else:
                hard_failures += 1
    gate_pass = hard_failures == 0
    audit = {
        "schema_version": 1,
        "skill": gate_plan.get("skill"),
        "output_id": output_id,
        "contract_version": output.get("contract_version"),
        "run_id": run_id,
        "capability_id": capability_id,
        "checked_at": _now(),
        "artifact": str(artifact.resolve()),
        "final_path": str(final_path.resolve()),
        "artifact_sha256": _sha256(artifact) if artifact.is_file() else None,
        "gate_pass": gate_pass,
        "hard_failures": hard_failures,
        "warnings": warnings,
        "validators": results,
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    temp = audit_path.with_suffix(audit_path.suffix + ".tmp")
    temp.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, audit_path)
    return audit
