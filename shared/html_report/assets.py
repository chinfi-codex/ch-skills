"""Content-versioned JSON assets for HTML reports.

Report pages are often rebuilt in place while their lazy-loaded JSON assets keep
the same pathname.  A browser or CDN may then combine a new HTML shell with an
old JSON response.  This module makes the asset bundle content-addressable at
the URL level without forcing callers to rename every shard: callers put the
returned ``asset_version`` in the query string and fetch with revalidation.

The manifest is written last, so it is also a compact integrity record for the
exact files belonging to the current render.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def write_json_asset_bundle(
    asset_dir: Path,
    payloads: Mapping[str, Any],
    *,
    schema_version: str,
    generated_at: Optional[str] = None,
    manifest_extra: Optional[Mapping[str, Any]] = None,
    manifest_name: str = "_manifest.json",
    prune_glob: Optional[str] = "*.json",
) -> Dict[str, Any]:
    """Write deterministic JSON assets and return their content version.

    ``payloads`` keys are file names relative to ``asset_dir``.  The bundle
    version is derived only from schema + file names + file contents, so a
    changed shard always produces a changed request URL.  ``generated_at`` is
    recorded for audit but intentionally does not churn the version when the
    underlying assets are byte-identical.
    """
    asset_dir = Path(asset_dir)
    asset_dir.mkdir(parents=True, exist_ok=True)
    if not schema_version:
        raise ValueError("schema_version must not be empty")
    if not manifest_name or Path(manifest_name).name != manifest_name:
        raise ValueError("manifest_name must be a plain file name")

    encoded: Dict[str, bytes] = {}
    files: Dict[str, Dict[str, Any]] = {}
    for name in sorted(payloads):
        if not name or Path(name).name != name or name == manifest_name:
            raise ValueError(f"asset name must be a plain non-manifest file name: {name!r}")
        data = _json_bytes(payloads[name])
        encoded[name] = data
        files[name] = {"sha256": _sha256(data), "bytes": len(data)}

    version_input = _json_bytes({"schema_version": schema_version, "files": files})
    asset_version = _sha256(version_input)[:16]
    manifest: Dict[str, Any] = {
        "schema_version": schema_version,
        "generated_at": generated_at,
        "asset_version": asset_version,
        "file_count": len(files),
        "files": files,
    }
    if manifest_extra:
        overlap = set(manifest).intersection(manifest_extra)
        if overlap:
            raise ValueError(f"manifest_extra cannot replace reserved keys: {sorted(overlap)}")
        manifest.update(manifest_extra)

    for name, data in encoded.items():
        _atomic_write(asset_dir / name, data)
    _atomic_write(asset_dir / manifest_name, _json_bytes(manifest))

    managed = set(encoded) | {manifest_name}
    if prune_glob:
        for old in asset_dir.glob(prune_glob):
            if old.is_file() and old.name not in managed:
                old.unlink()

    return {
        "asset_dir": str(asset_dir),
        "asset_version": asset_version,
        "manifest": manifest,
        "files": files,
    }


def verify_json_asset_bundle(asset_dir: Path, manifest_name: str = "_manifest.json") -> Dict[str, Any]:
    """Verify every file declared by a bundle manifest."""
    asset_dir = Path(asset_dir)
    manifest = json.loads((asset_dir / manifest_name).read_text(encoding="utf-8"))
    problems = []
    files = manifest.get("files") or {}
    for name, expected in files.items():
        path = asset_dir / name
        if not path.is_file():
            problems.append(f"missing:{name}")
            continue
        data = path.read_bytes()
        if _sha256(data) != expected.get("sha256"):
            problems.append(f"sha256:{name}")
        if len(data) != expected.get("bytes"):
            problems.append(f"bytes:{name}")
    expected_version = _sha256(_json_bytes({
        "schema_version": manifest.get("schema_version"),
        "files": files,
    }))[:16]
    if expected_version != manifest.get("asset_version"):
        problems.append("asset_version")
    return {
        "ok": not problems,
        "asset_version": manifest.get("asset_version"),
        "file_count": len(files),
        "problems": problems,
    }
