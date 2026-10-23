#!/usr/bin/env python3
"""Build a WorkBuddy expert zip from experts/<name>/.

    python scripts/package_expert.py experts/a-stock-earnings-season
    python scripts/package_expert.py experts/a-stock-earnings-season --check-only
    python scripts/package_expert.py experts/a-stock-earnings-season --author-email you@example.com

Layout of the zip (one top-level folder, as the WorkBuddy open platform expects):

    <name>/
    ├── .codebuddy-plugin/plugin.json
    ├── agents/<agentName>.md
    ├── avatars/...
    ├── README.md
    └── skills/<package_name>/   (SKILL.md + scripts/ references/ assets/ ...)

Each skill is copied from its canonical source in this repo, receives the shared
bundles skill-sync.yaml assigns to it (so the package is self-contained), loses
everything that is not a runtime asset (tests, evals, reports, caches — see the
publish boundary in CLAUDE.md), and gets the WorkBuddy skill frontmatter fields
(description_zh / description_en / author / version) added.

Repo-only content: Markdown wrapped in ``<!-- repo-only -->…<!-- /repo-only -->``
(maintainer environment, pointers to skills this package does not ship) is
removed. expert.yaml ``bundle_files`` narrows a shared bundle to the files the
packaged scripts import. The build then fails if anything shipped still names a
repository skill outside the package or a repository-only path.

The author email is never stored in the repo: plugin.json carries the
__AUTHOR_EMAIL__ placeholder, filled from --author-email, $WORKBUDDY_AUTHOR_EMAIL
or `git config user.email`.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import os

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_sync  # noqa: E402  (reuses config loading and bundle matching)
from skill_sync import REPO_ROOT, yaml  # noqa: E402

EMAIL_PLACEHOLDER = "__AUTHOR_EMAIL__"
CATEGORY_IDS = {
    "01-ProductDesign", "02-Engineering", "03-GameSpatial", "04-DataAI",
    "05-MarketingGrowth", "06-ContentCreative", "07-SalesCommerce",
    "08-FinanceInvestment", "09-OperationsHR", "10-ProjectQuality",
    "11-SecurityCompliance", "12-IndustryConsultant", "13-TencentZone",
    "14-WorldWise", "15-Education",
}
REQUIRED_PLUGIN_FIELDS = (
    "name", "expertType", "version", "description", "author", "agents", "agentName",
    "displayName", "profession", "displayDescription", "avatar", "categoryId",
    "defaultInitPrompt", "plugin", "tags", "quickPrompts",
)
# Runtime assets allowed at a skill root (CLAUDE.md publish boundary).
SKILL_RUNTIME_ENTRIES = {
    "SKILL.md", "scripts", "references", "assets", "examples", "templates", "requirements.txt",
}
# Never ship these, whatever skill-sync.yaml says.
ALWAYS_EXCLUDE_DIRS = ["reports", "evals", "eval", "benchmarks", "tests", "__pycache__",
                       ".pytest_cache", "*-workspace", ".staging"]
ALWAYS_EXCLUDE_FILES = ["test_*.py", "*_test.py", "conftest.py", ".env", ".env.*", "*.pyc",
                        "*.db", "*.sqlite", "*.log", "gate-plan.json", ".receipts.jsonl"]
REPO_ONLY_BLOCK = re.compile(r"<!--\s*repo-only\s*-->.*?<!--\s*/repo-only\s*-->", re.S)
# Paths and files that exist only in the source repository or the maintainer's machine.
REPO_ONLY_TERMS = ["~/.zshrc", "shared/data", "shared/html_report", "crawler/", "AGENTS.md",
                   "eval.md", "skill-sync.yaml", "skill_sync.py", "ch-stock-skills/"]
SECRET_PATTERNS = [
    re.compile(r"TUSHARE_TOKEN\s*=\s*['\"]?[0-9a-f]{40,}"),
    re.compile(r"(?i)(api[_-]?key|secret|password)\s*=\s*['\"][^'\"\s]{16,}['\"]"),
]


class PackageError(Exception):
    pass


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _bilingual(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("en")) and bool(value.get("zh"))


def _image_size(path: Path) -> tuple[int, int] | None:
    data = path.read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":  # JPEG: walk segments to the SOF marker
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + seg_len
    return None


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.S)
    if not m:
        raise PackageError("missing YAML frontmatter")
    return yaml.safe_load(m.group(1)) or {}, text[m.end():]


def validate_plugin(plugin: dict[str, Any], expert_dir: Path, manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_PLUGIN_FIELDS:
        if field not in plugin:
            errors.append(f"plugin.json missing field: {field}")
    if errors:
        return errors
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", plugin["name"]):
        errors.append("name must be lowercase letters/digits joined by hyphens")
    if plugin["plugin"] != plugin["name"]:
        errors.append("plugin must equal name")
    if plugin["expertType"] not in ("agent", "team"):
        errors.append("expertType must be 'agent' or 'team'")
    if plugin["expertType"] == "team" and not plugin.get("teamInfo"):
        errors.append("expertType=team requires teamInfo")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(plugin["version"])):
        errors.append("version must be semantic (x.y.z)")
    author = plugin["author"]
    if not (isinstance(author, dict) and author.get("name") and author.get("email")):
        errors.append("author must be {name, email}")
    for field in ("displayName", "profession", "displayDescription", "defaultInitPrompt"):
        if not _bilingual(plugin[field]):
            errors.append(f"{field} must be {{en, zh}}")
    zh_desc = (plugin["displayDescription"] or {}).get("zh", "")
    if not 40 <= len(zh_desc) <= 50:
        errors.append(f"displayDescription.zh must be 40-50 chars, got {len(zh_desc)}")
    for field in ("tags", "quickPrompts"):
        items = plugin[field]
        if not (isinstance(items, list) and len(items) == 3 and all(_bilingual(i) for i in items)):
            errors.append(f"{field} must be exactly 3 {{en, zh}} items")
    if not errors and plugin["defaultInitPrompt"] != {
        "en": plugin["quickPrompts"][0]["en"], "zh": plugin["quickPrompts"][0]["zh"],
    }:
        errors.append("defaultInitPrompt must equal quickPrompts[0]")
    if plugin["categoryId"] not in CATEGORY_IDS:
        errors.append(f"categoryId {plugin['categoryId']!r} is not one of the 15 platform categories")

    avatar = expert_dir / plugin["avatar"]
    if not avatar.is_file():
        errors.append(f"avatar not found: {plugin['avatar']}")
    else:
        size = _image_size(avatar)
        if size is None:
            errors.append("avatar must be PNG or JPG")
        elif size != (512, 512):
            errors.append(f"avatar must be 512x512, got {size[0]}x{size[1]}")
        if avatar.stat().st_size > 500 * 1024:
            errors.append("avatar must be <= 500KB")

    agent_paths = [expert_dir / p for p in plugin["agents"]]
    for path in agent_paths:
        if not path.is_file():
            errors.append(f"agent file not found: {path.relative_to(expert_dir)}")
    if plugin["agentName"] not in {p.stem for p in agent_paths}:
        errors.append("agentName must match an agents/*.md file stem")

    package_names = [s["package_name"] for s in manifest.get("skills", [])]
    declared = plugin.get("skills", [])
    if sorted(declared) != sorted(f"./skills/{n}" for n in package_names):
        errors.append("plugin.json skills must list ./skills/<package_name> for every expert.yaml skill")

    for path in agent_paths:
        if not path.is_file():
            continue
        try:
            fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
        except PackageError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        rel = path.relative_to(expert_dir)
        if fm.get("name") != path.stem:
            errors.append(f"{rel}: frontmatter name must equal the file name")
        if not fm.get("description"):
            errors.append(f"{rel}: frontmatter description required")
        for field in ("displayName", "profession"):
            if not _bilingual(fm.get(field)):
                errors.append(f"{rel}: frontmatter {field} must be {{en, zh}}")
        unknown = set(fm.get("skills") or []) - set(package_names)
        if unknown:
            errors.append(f"{rel}: skills not packaged: {sorted(unknown)}")
        if not body.strip():
            errors.append(f"{rel}: system prompt body is empty")
    return errors


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #
def _excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def copy_tree(src: Path, dst: Path, exclude_dirs: list[str], exclude_files: list[str]) -> None:
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not _excluded(d, exclude_dirs))
        rel = Path(root).relative_to(src)
        for name in sorted(files):
            if _excluded(name, exclude_files):
                continue
            target = dst / rel / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(root) / name, target)


def inject_skill_frontmatter(skill_md: Path, spec: dict[str, Any], author: str, version: str) -> None:
    text = skill_md.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    fm["name"] = spec["package_name"]
    for key in ("display_name", "display_name_en", "description_zh", "description_en"):
        if spec.get(key):
            fm[key] = spec[key]
    fm.setdefault("version", version)
    fm["author"] = author
    head = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=10_000).strip()
    skill_md.write_text(f"---\n{head}\n---\n{body}", encoding="utf-8")


def strip_repo_only(stage: Path) -> int:
    count = 0
    for path in stage.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        new, n = REPO_ONLY_BLOCK.subn("", text)
        if n:
            path.write_text(new, encoding="utf-8")
            count += n
    return count


def copy_bundle(bundle_src: Path, bundle_dst: Path, files: list[str] | None,
                exclude_dirs: list[str], exclude_files: list[str]) -> None:
    if files is None:
        copy_tree(bundle_src, bundle_dst, exclude_dirs, exclude_files)
        return
    for rel in files:
        src = bundle_src / rel
        if not src.is_file():
            raise PackageError(f"bundle_files entry not found: {src.relative_to(REPO_ROOT)}")
        target = bundle_dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def assemble(expert_dir: Path, plugin: dict[str, Any], manifest: dict[str, Any],
             stage: Path) -> list[str]:
    config = skill_sync.load_config(REPO_ROOT / "skill-sync.yaml")
    exclude = config.get("exclude", {}) or {}
    exclude_dirs = list(exclude.get("dirs", [])) + ALWAYS_EXCLUDE_DIRS
    exclude_files = list(exclude.get("files", [])) + ALWAYS_EXCLUDE_FILES
    bundles = (config.get("shared", {}) or {}).get("bundles", []) or []
    warnings: list[str] = []

    (stage / ".codebuddy-plugin").mkdir(parents=True)
    (stage / ".codebuddy-plugin" / "plugin.json").write_text(
        json.dumps(plugin, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for rel in plugin["agents"]:
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(expert_dir / rel, target)
    avatar_dir = (expert_dir / plugin["avatar"]).parent
    copy_tree(avatar_dir, stage / avatar_dir.relative_to(expert_dir), [], [])
    if (expert_dir / "README.md").is_file():
        shutil.copy2(expert_dir / "README.md", stage / "README.md")
    else:
        warnings.append("expert README.md missing")

    for spec in manifest["skills"]:
        src = skill_sync._resolve_repo_relative_path(spec["source"])
        if not (src / "SKILL.md").is_file():
            raise PackageError(f"not a skill (no SKILL.md): {spec['source']}")
        dst = stage / "skills" / spec["package_name"]
        for entry in sorted(src.iterdir()):
            if entry.name not in SKILL_RUNTIME_ENTRIES:
                continue
            if entry.is_dir():
                # A local scripts/_shared is a stale sync copy; bundles below are the source.
                copy_tree(entry, dst / entry.name, exclude_dirs + ["_shared"], exclude_files)
            else:
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, dst / entry.name)
        # skill-sync bundles name skills by their source directory name.
        for bundle in bundles:
            if not skill_sync._skill_matches_bundle(src.name, src, bundle):
                continue
            bundle_src = skill_sync._resolve_repo_relative_path(bundle["source"])
            bundle_dst = skill_sync._resolve_skill_relative_path(dst, bundle["dest"])
            copy_bundle(bundle_src, bundle_dst, (manifest.get("bundle_files") or {}).get(bundle["source"]),
                        exclude_dirs, exclude_files)
        inject_skill_frontmatter(dst / "SKILL.md", spec, plugin["author"]["name"], plugin["version"])
    strip_repo_only(stage)
    return warnings


def outside_skill_names(manifest: dict[str, Any]) -> list[str]:
    """Names of repository skills (directory and frontmatter names) this package does not ship."""
    shipped = {s["package_name"] for s in manifest["skills"]}
    names: set[str] = set()
    for dir_name, path in skill_sync.discover_skills(REPO_ROOT, {}).items():
        names.add(dir_name)
        try:
            fm, _ = split_frontmatter((path / "SKILL.md").read_text(encoding="utf-8"))
            if fm.get("name"):
                names.add(str(fm["name"]))
        except (PackageError, OSError, yaml.YAMLError):
            pass
    return sorted(n for n in names - shipped if len(n) > 3)


def _mentions(text: str, term: str, python: bool) -> list[int]:
    """Line numbers mentioning term; in Python only user-visible string literals count."""
    hits = []
    for no, line in enumerate(text.splitlines(), 1):
        if term not in line:
            continue
        if python:
            code = line.split("#", 1)[0] if not line.lstrip().startswith(("'", '"')) else line
            if not re.search(r"""["'][^"']*""" + re.escape(term), code):
                continue
        hits.append(no)
    return hits


FILE_REF = re.compile(r"`([^`\n]+)`|\]\(([^)\s]+)\)")
FILE_EXT = re.compile(r"\.(py|md|json|ya?ml|txt|css|js|html|sql|csv)$")
# Runtime artifacts and placeholders are not package files.
NOT_A_PACKAGE_FILE = re.compile(r"[<>*{}$]|^~|^/|^https?:|^reports/|^\.\.")


def audit_file_refs(stage: Path) -> list[str]:
    """Every file a shipped Markdown names in backticks or a link must exist in the package.

    Inside a skill, a reference resolves against the file's own folder or the skill
    root, and a bare file name may live anywhere in that skill. Anything in another
    skill, and anything named from agents/ or the package root, must be written
    as a package path `skills/<skill>/...`. This mirrors the platform review, which
    rejects a package whose documents name files it cannot find.
    """
    problems = []
    for md in sorted(stage.rglob("*.md")):
        rel = md.relative_to(stage)
        in_skill = rel.parts[0] == "skills" and len(rel.parts) > 2
        skill_root = stage / rel.parts[0] / rel.parts[1] if in_skill else None
        skill_names = ({p.name for p in skill_root.rglob("*") if p.is_file()} if skill_root else set())
        text = md.read_text(encoding="utf-8")
        for m in FILE_REF.finditer(text):
            token = (m.group(1) or m.group(2)).strip()
            ref = token.split()[0]
            if not FILE_EXT.search(ref) or NOT_A_PACKAGE_FILE.search(ref):
                continue
            if ref.startswith("skills/") and (stage / ref).is_file():
                continue
            if (md.parent / ref).is_file() or (skill_root and (skill_root / ref).is_file()):
                continue
            if in_skill and "/" not in ref and ref in skill_names:
                continue
            line = text[:m.start()].count("\n") + 1
            problems.append(f"{rel}:{line} names missing file '{ref}'")
    return problems


def audit_references(stage: Path, manifest: dict[str, Any]) -> list[str]:
    """Shipped text must not send the model to skills or paths that are not in the package."""
    terms = outside_skill_names(manifest) + REPO_ONLY_TERMS
    problems = []
    for path in sorted(stage.rglob("*")):
        if not path.is_file() or path.suffix not in {".md", ".json", ".yaml", ".yml", ".txt", ".py"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for term in terms:
            # Word-ish boundary so "a-stock-analyzer" does not match inside a longer name.
            if not re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", text):
                continue
            lines = _mentions(text, term, path.suffix == ".py")
            if lines:
                problems.append(f"{path.relative_to(stage)}:{lines[0]} references outside-package '{term}'")
    return problems


def audit_stage(stage: Path) -> list[str]:
    """Last line of defence: nothing dev-only or secret-looking ships."""
    problems: list[str] = []
    for path in stage.rglob("*"):
        rel = path.relative_to(stage)
        if path.is_dir():
            if _excluded(path.name, ALWAYS_EXCLUDE_DIRS):
                problems.append(f"forbidden dir shipped: {rel}")
            continue
        if _excluded(path.name, ALWAYS_EXCLUDE_FILES):
            problems.append(f"forbidden file shipped: {rel}")
        if path.suffix in {".py", ".md", ".json", ".yaml", ".yml", ".txt", ".env", ".js", ".css"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if EMAIL_PLACEHOLDER in text:
                problems.append(f"author email placeholder left in {rel}")
            for pat in SECRET_PATTERNS:
                if pat.search(text):
                    problems.append(f"secret-looking value in {rel}")
    return problems


def resolve_author_email(cli_value: str | None) -> str:
    value = cli_value or os.environ.get("WORKBUDDY_AUTHOR_EMAIL", "")
    if not value:
        try:
            value = subprocess.run(["git", "config", "user.email"], capture_output=True,
                                   text=True, cwd=REPO_ROOT, check=False).stdout.strip()
        except OSError:
            value = ""
    return value


def build(expert_dir: Path, out_dir: Path, author_email: str | None, check_only: bool) -> dict[str, Any]:
    plugin = json.loads((expert_dir / ".codebuddy-plugin" / "plugin.json").read_text(encoding="utf-8"))
    manifest = yaml.safe_load((expert_dir / "expert.yaml").read_text(encoding="utf-8")) or {}
    errors = validate_plugin(plugin, expert_dir, manifest)
    if errors:
        raise PackageError("; ".join(errors))
    if check_only:
        return {"ok": True, "checked": str(expert_dir.relative_to(REPO_ROOT)), "skills": len(manifest["skills"])}

    if plugin["author"].get("email") == EMAIL_PLACEHOLDER:
        email = resolve_author_email(author_email)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""):
            raise PackageError("author email unknown: pass --author-email or set WORKBUDDY_AUTHOR_EMAIL")
        plugin["author"]["email"] = email

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / plugin["name"]
        warnings = assemble(expert_dir, plugin, manifest, stage)
        problems = audit_stage(stage) + audit_references(stage, manifest) + audit_file_refs(stage)
        if problems:
            raise PackageError("; ".join(problems))
        out_dir.mkdir(parents=True, exist_ok=True)
        zip_path = out_dir / f"{plugin['name']}-{plugin['version']}.zip"
        files = sorted(p for p in stage.rglob("*") if p.is_file())
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                zf.write(path, path.relative_to(stage.parent).as_posix())
        return {
            "ok": True,
            "zip": str(zip_path),
            "bytes": zip_path.stat().st_size,
            "files": len(files),
            "skills": [s["package_name"] for s in manifest["skills"]],
            "warnings": warnings,
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build a WorkBuddy expert zip")
    ap.add_argument("expert_dir", help="e.g. experts/a-stock-earnings-season")
    ap.add_argument("--out", default="dist/workbuddy", help="output directory (default dist/workbuddy)")
    ap.add_argument("--author-email", help="fills plugin.json author.email (never committed)")
    ap.add_argument("--check-only", action="store_true", help="validate without building")
    args = ap.parse_args(argv)
    expert_dir = (REPO_ROOT / args.expert_dir).resolve()
    out_dir = (REPO_ROOT / args.out).resolve()
    try:
        result = build(expert_dir, out_dir, args.author_email, args.check_only)
    except PackageError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
