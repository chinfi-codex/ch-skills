#!/usr/bin/env python3
"""安装主动目标的评估与确定性投递 Hermes 任务；默认 dry-run，不发送消息。

wrapper 从 Hermes 已同步的技能副本（$HERMES_HOME/skills/ch-proactive-agent）运行，
未提交、未同步的开发改动不会上线。金库与运行态路径来自 config/goals.yaml。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from proactive_config import SETTINGS_ENV, load_settings, settings_snapshot

SETTINGS = load_settings()
ROOT = SETTINGS.vault_root
RUNTIME = SETTINGS.runtime_home
GOAL_ID = SETTINGS.goal_id
SKILL_NAME = "ch-proactive-agent"
EVALUATION_NAME = "AlphaVault 主动评估"
DELIVERY_NAME = "AlphaVault 主动提醒"
LEGACY_NAME = "AlphaVault 主动投研提醒"
EVALUATION_WRAPPER = "alphavault-proactive-evaluate.py"
DELIVERY_WRAPPER = "alphavault-proactive-delivery.py"
REQUIRED_MODULES = ("psycopg2", "yaml", "jsonschema")
# 规则只保留在 SKILL.md 一处；这里只标识目标并指向已加载的技能。
PROMPT = f"""[{GOAL_ID}]
按已加载的 ch-proactive-agent 完成本轮无工具评估。下方是预检注入的候选包；只输出 SKILL 规定的 JSON 对象。
"""


def _argv(kind: str, evaluation_job_id: str, channel: str) -> str:
    if kind == "evaluation":
        return '["scan"]'
    if not evaluation_job_id:
        raise ValueError("投递 wrapper 需要 evaluation_job_id")
    return f'["dispatch", "--evaluation-job-id", {evaluation_job_id!r}, "--channel", {channel!r}]'


def wrapper_source(kind: str, home: Path, evaluation_job_id: str = "", channel: str = "local") -> str:
    argv = _argv(kind, evaluation_job_id, channel)
    skill = home / "skills" / SKILL_NAME
    return f'''#!/usr/bin/env python3
import os
import runpy
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    raise SystemExit("ch-proactive-agent 需要 Python 3.9+")
SKILL = Path({str(skill)!r})
os.environ.setdefault("HERMES_HOME", {str(home)!r})
os.environ["ALPHAVAULT_ROOT"] = {str(ROOT)!r}
os.environ["PROACTIVE_HOME"] = {str(RUNTIME)!r}
os.chdir(os.environ["PROACTIVE_HOME"])
sys.path.insert(0, str(SKILL / "scripts"))
sys.argv = [str(SKILL / "scripts" / "proactive_goal.py"), *{argv}]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def legacy_wrapper_source(kind: str, home: Path, evaluation_job_id: str = "", channel: str = "local") -> str:
    """迁移前由 AlphaVault system/tools/install_proactive_hermes.py 生成的版本，允许被本安装器升级。"""
    argv = _argv(kind, evaluation_job_id, channel)
    return f'''#!/usr/bin/env python3
import os
import runpy
import sys
from pathlib import Path

ROOT = Path({str(ROOT)!r})
os.chdir(ROOT)
os.environ.setdefault("HERMES_HOME", {str(home)!r})
sys.path.insert(0, str(ROOT / "system" / "tools"))
sys.argv = [str(ROOT / "system" / "tools" / "proactive_goal.py"), *{argv}]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def evaluation_config(home: Path, interpreter: Path, paused: bool) -> dict[str, Any]:
    return {
        "name": EVALUATION_NAME, "prompt": PROMPT, "schedule": "*/30 * * * *",
        "deliver": "local", "failure_deliver": "local", "skills": [SKILL_NAME],
        "enabled_toolsets": [], "script": str(home / "scripts" / EVALUATION_WRAPPER),
        "context_from": [], "workdir": str(RUNTIME), "no_agent": False,
        "attach_to_session": False, "interpreter": str(interpreter),
        "paused": paused, "paused_reason": "等待双段影子验证" if paused else None,
    }


def delivery_config(home: Path, interpreter: Path, live: bool, paused: bool) -> dict[str, Any]:
    return {
        "name": DELIVERY_NAME, "prompt": None, "schedule": "*/30 * * * *",
        "deliver": "weixin" if live else "local", "failure_deliver": "local",
        "skills": [], "enabled_toolsets": [], "script": str(home / "scripts" / DELIVERY_WRAPPER),
        "context_from": [], "workdir": str(RUNTIME), "no_agent": True,
        "attach_to_session": live, "interpreter": str(interpreter),
        "paused": paused, "paused_reason": "等待双段影子验证" if paused else None,
    }


def ensure_hermes_python(home: Path) -> None:
    if sys.version_info >= (3, 11):
        return
    candidates = sorted((home / "installs").glob("*/environments/*/venv/bin/python"), reverse=True)
    candidates.extend(sorted((home / "tools").glob("python-*/bin/python3"), reverse=True))
    candidates.extend([home / "hermes-agent/venv/bin/python", home / "hermes-agent/.hermes/bin/python"])
    for candidate in candidates:
        if not candidate.is_file() or candidate.resolve() == Path(sys.executable).resolve():
            continue
        os.environ[SETTINGS_ENV] = settings_snapshot()
        os.execv(str(candidate), [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise ValueError("未找到 Hermes 自带的 Python 3.11+ 解释器")


def write_wrapper(path: Path, content: str, *, managed_variants: tuple[str, ...] = ()) -> None:
    if path.exists() and path.read_text(encoding="utf-8") not in {content, *managed_variants}:
        raise ValueError(f"wrapper 已存在且不是本安装器生成的版本，未覆盖：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def managed_variants(kind: str, home: Path, evaluation_job_id: str = "") -> tuple[str, ...]:
    channels = ("local", "weixin") if kind == "delivery" else ("local",)
    return tuple(builder(kind, home, evaluation_job_id, channel)
                 for builder in (wrapper_source, legacy_wrapper_source) for channel in channels)


def preflight(interpreter: Path, home: Path) -> None:
    probe = subprocess.run(
        [str(interpreter), "-c", f"import sys; assert sys.version_info >= (3, 9); import {', '.join(REQUIRED_MODULES)}"],
        text=True, capture_output=True, timeout=10, check=False)
    if probe.returncode:
        raise ValueError(f"主动目标 Python 需为 3.9+ 且具备 {', '.join(REQUIRED_MODULES)}：{interpreter}")
    skill = home / "skills" / SKILL_NAME
    for rel in ("SKILL.md", "scripts/proactive_goal.py", "scripts/proactive_config.py", "config/goals.yaml"):
        if not (skill / rel).is_file():
            raise ValueError(f"{SKILL_NAME} 尚未完整同步到 Hermes skills（缺 {rel}）；先提交 ch-skills 触发同步")
    if not (RUNTIME / "state.json").is_file():
        raise ValueError(f"运行态尚未初始化：{RUNTIME / 'state.json'}；先运行 migrate_from_vault.py --apply")


def upsert(job: dict[str, Any], existing: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    if existing is None:
        from cron.jobs import create_job

        return "created", create_job(**job)
    from cron.jobs import update_job

    changed = {key: value for key, value in job.items() if key not in {"paused", "paused_reason"}}
    changed["schedule_display"] = job["schedule"]
    if job["paused"]:
        changed.update(enabled=False, state="paused", paused_reason=job["paused_reason"])
    return "updated", update_job(existing["id"], changed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--enable-live", action="store_true", help="仅让确定性投递任务使用微信；评估永远 local")
    parser.add_argument("--paused", action="store_true")
    args = parser.parse_args()
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser().resolve()
    interpreter = Path(os.environ.get("ALPHAVAULT_PYTHON") or shutil.which("python3") or "").resolve()
    if not interpreter.is_file():
        raise ValueError("找不到用于读取 PG 的 python3，可设置 ALPHAVAULT_PYTHON")
    evaluation = evaluation_config(home, interpreter, args.paused)
    delivery = delivery_config(home, interpreter, args.enable_live, args.paused)
    if not args.apply:
        print(json.dumps({"dry_run": True, "vault_root": str(ROOT), "runtime_home": str(RUNTIME),
                          "evaluation": evaluation, "delivery": delivery,
                          "note": "不写文件、不创建任务、不发送消息"}, ensure_ascii=False, indent=2))
        return 0

    source = Path(os.environ.get("HERMES_SOURCE", str(home / "hermes-agent"))).resolve()
    if not (source / "cron" / "jobs.py").is_file():
        raise ValueError(f"找不到 Hermes cron.jobs：{source}")
    ensure_hermes_python(home)
    preflight(interpreter, home)
    sys.path.insert(0, str(source))
    from cron.jobs import list_jobs  # noqa: E402

    jobs = list_jobs(include_disabled=True)
    eval_matches = [job for job in jobs if job.get("name") in {EVALUATION_NAME, LEGACY_NAME}]
    delivery_matches = [job for job in jobs if job.get("name") == DELIVERY_NAME]
    if len(eval_matches) > 1 or len(delivery_matches) > 1:
        raise ValueError("存在重复主动目标任务，未猜测修改对象")
    if eval_matches and GOAL_ID not in str(eval_matches[0].get("prompt") or ""):
        raise ValueError("同名评估任务不是本目标创建")
    if delivery_matches and not delivery_matches[0].get("no_agent"):
        raise ValueError("同名提醒任务不是无模型投递器")

    eval_action, eval_job = upsert(evaluation, eval_matches[0] if eval_matches else None)
    evaluation_id = eval_job["id"]
    write_wrapper(home / "scripts" / EVALUATION_WRAPPER, wrapper_source("evaluation", home),
                  managed_variants=managed_variants("evaluation", home))
    write_wrapper(home / "scripts" / DELIVERY_WRAPPER,
                  wrapper_source("delivery", home, evaluation_id, "weixin" if args.enable_live else "local"),
                  managed_variants=managed_variants("delivery", home, evaluation_id))
    delivery_action, delivery_job = upsert(delivery, delivery_matches[0] if delivery_matches else None)
    print(json.dumps({
        "evaluation": {"action": eval_action, "job_id": evaluation_id, "deliver": eval_job["deliver"],
                       "enabled_toolsets": eval_job.get("enabled_toolsets"), "workdir": eval_job.get("workdir")},
        "delivery": {"action": delivery_action, "job_id": delivery_job["id"], "deliver": delivery_job["deliver"],
                     "no_agent": delivery_job.get("no_agent"), "attach_to_session": delivery_job.get("attach_to_session")},
        "note": "仅注册双段任务；没有发送测试消息",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"install_hermes: {exc}", file=sys.stderr)
        raise SystemExit(2)
