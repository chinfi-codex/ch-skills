#!/usr/bin/env python3
"""
Skill Sync Hub —— 跨 Agent 技能同步工具

参考 neuDrive Bundle Sync 思路：本仓库作为 canonical source，
通过声明式配置 + Git Hook，单向同步到 Kimi CLI / Claude Code / Codex / .agents 等安装目录。

用法:
    python scripts/skill_sync.py              # 一次性复制同步
    python scripts/skill_sync.py --link       # 建立 Junction/Symlink（开发推荐）
    python scripts/skill_sync.py --dry-run    # 预览变更，不实际执行
    python scripts/skill_sync.py --watch      # 监控文件变更并自动同步
    python scripts/skill_sync.py --install-hook   # 安装 post-commit / post-merge / post-rewrite Git Hooks
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    print("错误: 需要 PyYAML。请执行: pip install pyyaml")
    raise SystemExit(1) from exc

REPO_ROOT = Path(__file__).parent.parent.resolve()
MANAGED_HOOK_BEGIN = "# >>> Skill Sync Hub >>>"
MANAGED_HOOK_END = "# <<< Skill Sync Hub <<<"

# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def load_config(config_path: Path) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def expand_path(path_str: str) -> Path:
    """展开环境变量和 ~，返回绝对路径。

    支持三种风格的环境变量:
    - $env:NAME  (PowerShell)
    - %NAME%     (Windows CMD)
    - $NAME      (Unix / Git Bash)
    """
    def env_value(name: str) -> str | None:
        if name in os.environ:
            return os.environ[name]
        if name in {"HOME", "USERPROFILE"}:
            return str(Path.home())
        return None

    def replace_env(match: re.Match[str]) -> str:
        name = match.group(1)
        return env_value(name) or match.group(0)

    expanded = os.path.expanduser(path_str)
    expanded = re.sub(r"\$env:([A-Za-z_][A-Za-z0-9_]*)", replace_env, expanded)
    expanded = re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", replace_env, expanded)
    expanded = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace_env, expanded)
    expanded = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", replace_env, expanded)
    expanded = os.path.expandvars(expanded)
    return Path(expanded).resolve()


def validate_target_path(target_name: str, target_path: Path) -> None:
    """阻止把镜像同步打到根目录、用户目录或非 skills 目录。"""
    home = Path.home().resolve()
    root = Path(target_path.anchor).resolve()
    resolved = target_path.resolve()

    if resolved in {root, home, REPO_ROOT}:
        raise ValueError(f"危险目标路径: {target_name} -> {resolved}")

    if resolved.name != "skills":
        raise ValueError(f"目标路径必须以 skills 结尾: {target_name} -> {resolved}")

    if REPO_ROOT in resolved.parents:
        raise ValueError(f"目标路径不能位于当前仓库内: {target_name} -> {resolved}")


# ---------------------------------------------------------------------------
# Skill 发现（扁平化扫描）
# ---------------------------------------------------------------------------


def _should_skip_dir(parts: tuple[str, ...]) -> bool:
    """跳过隐藏目录和常见非代码目录。"""
    skip = {
        ".git",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        "venv",
        ".venv",
        "evals",
        "benchmarks",
    }
    return any(p in skip or p.startswith(".") for p in parts)


def discover_skills(repo_root: Path, rename_map: dict[str, str]) -> dict[str, Path]:
    """
    递归扫描仓库，发现所有包含 SKILL.md 的目录。

    规则:
    - 任何包含 SKILL.md 的目录视为一个独立 skill。
    - 找到 SKILL.md 后停止向该目录内部继续递归（防止误扫子目录）。
    - 通过 rename_map 可覆盖目标 skill 名。

    返回: {skill_name: source_directory_path}
    """
    skills: dict[str, Path] = {}
    for root, dirs, files in os.walk(repo_root):
        root_path = Path(root)
        rel_parts = root_path.relative_to(repo_root).parts

        if _should_skip_dir(rel_parts):
            dirs.clear()
            continue

        if "SKILL.md" in files:
            rel_str = str(root_path.relative_to(repo_root)).replace("\\", "/")
            dir_name = root_path.name
            skill_name = rename_map.get(rel_str, dir_name)
            skills[skill_name] = root_path
            dirs.clear()  # 不再深入该 skill 内部

    return skills


# ---------------------------------------------------------------------------
# 同步原语
# ---------------------------------------------------------------------------


def _mirror_robocopy(
    src: Path,
    dst: Path,
    exclude_dirs: list[str],
    exclude_files: list[str],
    dry_run: bool = False,
) -> None:
    """Windows: 使用 robocopy 做增量覆盖同步（保留目标端原有文件）。"""
    if dry_run:
        print(f"  [DRY-RUN] 将同步: {src.name} -> {dst}")
        return

    dst.mkdir(parents=True, exist_ok=True)
    cmd = [
        "robocopy",
        str(src),
        str(dst),
        "/E",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP",
    ]
    for d in exclude_dirs:
        cmd.extend(["/XD", d])
    for f in exclude_files:
        cmd.extend(["/XF", f])
    subprocess.run(cmd, check=False)


def _mirror_rsync(
    src: Path,
    dst: Path,
    exclude_dirs: list[str],
    exclude_files: list[str],
    dry_run: bool = False,
) -> None:
    """Unix-like: 使用 rsync 做增量覆盖同步（保留目标端原有文件）。"""
    if dry_run:
        print(f"  [DRY-RUN] 将同步: {src.name} -> {dst}")
        return

    dst.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-av"]
    for e in exclude_dirs + exclude_files:
        cmd.append(f"--exclude={e}")
    cmd.extend([str(src) + "/", str(dst) + "/"])
    subprocess.run(cmd, check=True)


def mirror(
    src: Path,
    dst: Path,
    exclude_dirs: list[str],
    exclude_files: list[str],
    dry_run: bool = False,
) -> None:
    """跨平台增量覆盖同步（保留目标端原有文件）。"""
    if platform.system() == "Windows":
        _mirror_robocopy(src, dst, exclude_dirs, exclude_files, dry_run)
    else:
        _mirror_rsync(src, dst, exclude_dirs, exclude_files, dry_run)


def _create_junction(src: Path, dst: Path, dry_run: bool = False) -> None:
    """Windows Junction: 同卷目录硬链接，无需管理员权限。"""
    if dry_run:
        print(f"  [DRY-RUN] 将创建 Junction: {dst} -> {src}")
        return

    if dst.exists() or dst.is_symlink():
        if dst.is_junction():
            dst.unlink()
        elif dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()

    subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], check=True)
    print(f"  [LINK] {dst} -> {src}")


def _create_symlink(src: Path, dst: Path, dry_run: bool = False) -> None:
    """Unix-like / Windows(开发者模式) 符号链接。"""
    if dry_run:
        print(f"  [DRY-RUN] 将创建 Symlink: {dst} -> {src}")
        return

    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()

    dst.symlink_to(src, target_is_directory=True)
    print(f"  [LINK] {dst} -> {src}")


def link(src: Path, dst: Path, dry_run: bool = False) -> None:
    """建立目录链接（Windows Junction / Unix Symlink）。"""
    if platform.system() == "Windows":
        _create_junction(src, dst, dry_run)
    else:
        _create_symlink(src, dst, dry_run)


# ---------------------------------------------------------------------------
# 同步引擎
# ---------------------------------------------------------------------------


def run_sync(config: dict, mode: str = "copy", dry_run: bool = False) -> None:
    """执行全量同步。"""
    targets = config.get("targets", {})
    rename = config.get("rename", {})
    exclude = config.get("exclude", {})
    exclude_dirs = exclude.get("dirs", [])
    exclude_files = exclude.get("files", [])

    skills = discover_skills(REPO_ROOT, rename)

    print(f"发现 {len(skills)} 个 skill:")
    for name in sorted(skills):
        rel = skills[name].relative_to(REPO_ROOT)
        print(f"  - {name}  ({rel})")

    for target_name, target_path_tpl in targets.items():
        target_path = expand_path(target_path_tpl)
        validate_target_path(target_name, target_path)
        print(f"\n{'=' * 50}")
        print(f"Target: {target_name} -> {target_path}")
        print(f"{'=' * 50}")
        if dry_run:
            print(f"  [DRY-RUN] 将确保目标目录存在: {target_path}")
        else:
            target_path.mkdir(parents=True, exist_ok=True)

        for skill_name, skill_src in sorted(skills.items()):
            skill_dst = target_path / skill_name
            if mode == "link":
                link(skill_src, skill_dst, dry_run)
            else:
                mirror(skill_src, skill_dst, exclude_dirs, exclude_files, dry_run)


# ---------------------------------------------------------------------------
# Watch 模式（简单轮询）
# ---------------------------------------------------------------------------


def watch_and_sync(
    config: dict,
    mode: str = "copy",
    interval: int = 5,
) -> None:
    """轮询文件 mtime，变更后自动同步。"""
    print(f"Watch 模式启动，每 {interval} 秒检查一次...")
    last_state: dict[str, float] = {}

    while True:
        current_state: dict[str, float] = {}
        for root, _dirs, files in os.walk(REPO_ROOT):
            root_path = Path(root)
            rel_parts = root_path.relative_to(REPO_ROOT).parts
            if _should_skip_dir(rel_parts):
                continue
            for f in files:
                fp = root_path / f
                current_state[str(fp)] = fp.stat().st_mtime

        if last_state and current_state != last_state:
            print("检测到文件变更，执行同步...")
            run_sync(config, mode=mode)
            print()

        last_state = current_state
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Git Hook 安装
# ---------------------------------------------------------------------------


def _hook_sync_block(hook_name: str, script_path: Path, config_path: Path) -> str:
    """生成可嵌入现有 Git hook 的受管同步片段。"""
    return f'''{MANAGED_HOOK_BEGIN}
# Skill Sync Hub - {hook_name}
# Generated by skill_sync.py --install-hook; failures should not block Git.
repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -n "$repo_root" ]; then
    if command -v python3 >/dev/null 2>&1; then
        sync_python=python3
    else
        sync_python=python
    fi
    (cd "$repo_root" && "$sync_python" "scripts/skill_sync.py" --config "skill-sync.yaml") || true
fi
{MANAGED_HOOK_END}
'''


def _upsert_git_hook(hook_path: Path, hook_name: str, script_path: Path, config_path: Path) -> None:
    """创建或更新 Git hook，尽量保留用户已有自定义内容。"""
    block = _hook_sync_block(hook_name, script_path, config_path)

    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8")
    else:
        existing = "#!/bin/sh\n"

    if MANAGED_HOOK_BEGIN in existing and MANAGED_HOOK_END in existing:
        before, rest = existing.split(MANAGED_HOOK_BEGIN, 1)
        _old, after = rest.split(MANAGED_HOOK_END, 1)
        content = before.rstrip() + "\n" + block + after.lstrip("\n")
    elif "Skill Sync Hub" in existing and "skill_sync.py --install-hook" in existing:
        content = "#!/bin/sh\n" + block
    else:
        content = existing.rstrip() + "\n\n" + block
        if not content.startswith("#!"):
            content = "#!/bin/sh\n" + content

    hook_path.write_text(content, encoding="utf-8", newline="\n")
    os.chmod(hook_path, 0o755)


def install_git_hook(repo_root: Path) -> None:
    """安装 commit 与 pull 后自动同步所需的 Git hooks。"""
    hooks_dir = repo_root / ".git" / "hooks"
    if not hooks_dir.exists():
        print("错误: 找不到 .git/hooks 目录。请确保在 Git 仓库内运行。")
        raise SystemExit(1)

    script_path = repo_root / "scripts" / "skill_sync.py"
    config_path = repo_root / "skill-sync.yaml"

    hooks = {
        "post-commit": "post-commit hook",
        "post-merge": "post-merge hook (git pull / merge)",
        "post-rewrite": "post-rewrite hook (git pull --rebase / rebase)",
    }

    for hook_file, hook_name in hooks.items():
        hook_path = hooks_dir / hook_file
        _upsert_git_hook(hook_path, hook_name, script_path, config_path)
        print(f"Git Hook 已安装: {hook_path}")

    print("此后 git commit、git pull、git pull --rebase 后都会自动同步技能到各 Agent 目录。")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Skill Sync Hub —— 跨 Agent 技能同步工具",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "skill-sync.yaml"),
        help="同步配置文件路径 (默认: skill-sync.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览变更，不实际执行",
    )
    parser.add_argument(
        "--link",
        action="store_true",
        help="使用 Junction/Symlink 而非复制",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="监控文件变更并自动同步",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Watch 模式轮询间隔（秒）(默认: 5)",
    )
    parser.add_argument(
        "--install-hook",
        action="store_true",
        help="安装 post-commit / post-merge / post-rewrite Git Hooks",
    )
    args = parser.parse_args()

    if args.install_hook:
        install_git_hook(REPO_ROOT)
        return

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"错误: 配置文件不存在: {config_path}")
        raise SystemExit(1)

    config = load_config(config_path)
    mode = "link" if args.link else "copy"

    if args.watch:
        watch_and_sync(config, mode=mode, interval=args.interval)
    else:
        run_sync(config, mode=mode, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
