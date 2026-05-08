#!/usr/bin/env python3
"""
Skill Sync Hub —— 跨 Agent 技能同步工具

参考 neuDrive Bundle Sync 思路：本仓库作为 canonical source，
通过声明式配置 + Git Hook，单向同步到 Kimi CLI / Claude Code / Codex / .agents 安装目录。

用法:
    python scripts/skill_sync.py              # 一次性复制同步
    python scripts/skill_sync.py --link       # 建立 Junction/Symlink（开发推荐）
    python scripts/skill_sync.py --dry-run    # 预览变更，不实际执行
    python scripts/skill_sync.py --watch      # 监控文件变更并自动同步
    python scripts/skill_sync.py --install-hook   # 安装 post-commit Git Hook
"""

from __future__ import annotations

import argparse
import os
import platform
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
    expanded = path_str
    # PowerShell $env:NAME -> %NAME%
    expanded = __import__("re").sub(r"\$env:([A-Za-z_][A-Za-z0-9_]*)", r"%\1%", expanded)
    expanded = os.path.expandvars(os.path.expanduser(expanded))
    return Path(expanded).resolve()


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
    """Windows: 使用 robocopy /MIR 做镜像同步。"""
    if dry_run:
        print(f"  [DRY-RUN] 将同步: {src.name} -> {dst}")
        return

    dst.mkdir(parents=True, exist_ok=True)
    cmd = [
        "robocopy",
        str(src),
        str(dst),
        "/MIR",
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
    """Unix-like: 使用 rsync 做镜像同步。"""
    if dry_run:
        print(f"  [DRY-RUN] 将同步: {src.name} -> {dst}")
        return

    dst.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-av", "--delete"]
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
    """跨平台镜像同步。"""
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
        print(f"\n{'=' * 50}")
        print(f"Target: {target_name} -> {target_path}")
        print(f"{'=' * 50}")
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


def install_git_hook(repo_root: Path) -> None:
    """在 .git/hooks/post-commit 写入自动同步调用。"""
    hooks_dir = repo_root / ".git" / "hooks"
    if not hooks_dir.exists():
        print("错误: 找不到 .git/hooks 目录。请确保在 Git 仓库内运行。")
        raise SystemExit(1)

    hook_path = hooks_dir / "post-commit"
    script_path = repo_root / "scripts" / "skill_sync.py"
    config_path = repo_root / "skill-sync.yaml"

    # 使用绝对路径，避免 MSYS/Cygwin 路径转换问题
    content = f'''#!/bin/sh
# =============================================================================
# Skill Sync Hub —— post-commit hook
# 由 skill_sync.py --install-hook 自动生成
# =============================================================================
python "{script_path}" --config "{config_path}"
'''

    hook_path.write_text(content, encoding="utf-8")
    # Git for Windows 的 hooks 由 MSYS sh 执行，需要可执行权限
    os.chmod(hook_path, 0o755)
    print(f"Git Hook 已安装: {hook_path}")
    print("此后每次 git commit 将自动同步技能到各 Agent 目录。")


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
        help="安装 post-commit Git Hook",
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
