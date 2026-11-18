#!/usr/bin/env python3
"""飞书渠道：markdown 消息，经本机 lark-cli（``lark-cli im +messages-send``）投递。

lark-cli（@larksuite/cli）已登录后 token 在加密文件与 keychain 里，本模块不需要
任何环境变量传密钥；加 LARKSUITE_CLI_NO_*_NOTIFIER 只是为了压掉更新提示、保证
stdout 是干净 JSON。目标解析：``oc_`` 前缀按群（--chat-id）、``ou_`` 前缀按个人
（--user-id）；都不传时回落到 ``~/.lark-cli/config.json`` 的 ``userOpenId``——
发给自己，最稳的默认。bot 身份发群消息要求 bot 已在群里。

判定成功用 退出码 0 且 stdout JSON ``ok==true``（成功信封没有顶层 code 字段）。
默认带幂等键（title+正文 hash 派生，1 小时窗口），同一内容重试不会重发——
这是预期行为，不是丢了消息。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import ConfigError

LARK_CONFIG_PATH = Path.home() / ".lark-cli" / "config.json"
SUBPROCESS_TIMEOUT = 60


def _default_target() -> str:
    """回落目标：lark-cli 登录用户的 open_id，即"发给自己"。
    config 结构是 {apps:[{users:[{userOpenId}]}]}，取第一个登录用户的。"""
    try:
        cfg = json.loads(LARK_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConfigError(
            f"no --target given and {LARK_CONFIG_PATH} is unreadable. "
            "Run `lark-cli auth login` or pass --target oc_xxx/ou_xxx.")
    for app in cfg.get("apps", []):
        for user in app.get("users", []):
            if user.get("userOpenId"):
                return user["userOpenId"]
    raise ConfigError(
        f"{LARK_CONFIG_PATH} has no logged-in userOpenId. "
        "Run `lark-cli auth login` or pass --target oc_xxx/ou_xxx.")


def resolve_target_flag(target: str) -> "tuple[str, str]":
    """目标字符串 → (flag, value)。oc_ 群 / ou_ 个人，其余格式报错。"""
    t = (target or "").strip()
    if t.startswith("oc_"):
        return "--chat-id", t
    if t.startswith("ou_"):
        return "--user-id", t
    raise ConfigError(f"bad feishu target {target!r}: expect chat_id (oc_…) or open_id (ou_…)")


def idempotency_key(title: str, body: str) -> str:
    digest = hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()
    return f"np-{digest[:38]}"  # ≤50 字符上限，留足余量


def _compose(markdown: str, title: str) -> str:
    """--markdown 没有独立标题字段：标题以加粗首行拼进正文。
    正文已含同文的一级标题时不重复拼，避免消息开头出现两遍标题。"""
    body = markdown.strip()
    if not title:
        return body
    if re.search(rf"^#\s+{re.escape(title)}\s*$", body, re.MULTILINE):
        return body
    return f"**{title}**\n\n{body}"


def send(markdown: str, title: str, *, dry_run: bool = False,
         target: str = "", key: str = "") -> dict:
    cli = shutil.which("lark-cli")
    if not cli:
        raise ConfigError("lark-cli not found on PATH. Install @larksuite/cli and run `lark-cli auth login`.")
    resolved = target or _default_target()
    flag, value = resolve_target_flag(resolved)
    body = _compose(markdown, title)
    idem = key or idempotency_key(title, body)

    env = dict(os.environ,
               LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1",
               LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
    argv = [cli, "im", "+messages-send", flag, value,
            "--markdown", body, "--idempotency-key", idem, "--as", "bot"]

    if dry_run:
        info = (f"[dry-run] would run: lark-cli im +messages-send {flag} {value} "
                f"--markdown <{len(body)} chars> --idempotency-key {idem} --as bot")
        return {"ok": True, "info": info, "dry_run": True, "target": value,
                "idempotency_key": idem, "body": body}

    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=SUBPROCESS_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"lark-cli timed out after {SUBPROCESS_TIMEOUT}s"}
    except OSError as e:
        return {"ok": False, "error": f"failed to run lark-cli: {e}"}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    payload: dict = {"target": value, "idempotency_key": idem}
    parsed: dict = {}
    if stdout:
        try:
            parsed = json.loads(stdout)
            payload["response"] = parsed
        except ValueError:
            payload["response_raw"] = stdout[:2000]

    # 注意：成功信封没有顶层 code 字段，别按 code==0 判。
    if proc.returncode == 0 and parsed.get("ok") is True:
        message_id = (parsed.get("data") or {}).get("message_id", "")
        where = "chat" if flag == "--chat-id" else "user"
        return {"ok": True, "info": f"sent to feishu {where} {value} (message_id={message_id})",
                **payload}
    payload["error"] = (f"lark-cli send failed (exit {proc.returncode}): "
                        f"{stderr or stdout or 'no output'}")
    return {"ok": False, **payload}
