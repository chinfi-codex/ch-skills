#!/usr/bin/env python3
"""hermes 渠道：纯文本短消息，经本机 hermes CLI（``hermes send``）投递。

hermes-agent 是独立的宿主守护进程，``hermes send`` 定位就是"从 shell 把文本
送到任一已配置平台"——no LLM、无 agent loop，凭据复用 ``~/.hermes``（.env +
config.yaml），本模块不需要任何 token。默认目标 ``weixin`` 是 home 频道，
``target`` 参数可换成 ``weixin:chat_id`` 等任意 ``hermes send --list`` 里列出的目标。

weixin 不渲染 markdown，``**`` ``##`` 这类符号会原样出现在聊天里，所以发送前
做一次确定性的轻量去 markdown（去强调、标题转普通行、链接留文字+地址、表格
转短行）。这只影响排版，不改内容——摘要该写什么仍由上游的模型负责。

正文走 stdin（``-f -``），避开 argv 长度与转义问题；标题走 ``-s SUBJECT``。
二进制定位顺序与 ch-proactive-agent 的 pulse 一致：
$HERMES_BIN → which hermes → ~/.local/bin/hermes。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import ConfigError

DEFAULT_TARGET = "weixin"
SUBPROCESS_TIMEOUT = 120


def locate_bin() -> str:
    """$HERMES_BIN → which → ~/.local/bin/hermes，找不到返回空串。"""
    for candidate in (os.environ.get("HERMES_BIN", ""),
                      shutil.which("hermes") or "",
                      str(Path.home() / ".local" / "bin" / "hermes")):
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def demarkdown(md: str) -> str:
    """轻量去 markdown：只动排版符号，不动文字内容。"""
    out: list[str] = []
    in_fence = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(raw)
            continue
        s = line.strip()
        if not s:
            out.append("")
            continue
        if set(s) <= {"-", "=", "*", "_"} and len(s) >= 3:
            continue  # 水平线，纯文本里只会是噪音
        s = re.sub(r"^#{1,6}\s+", "", s)  # 标题降为普通行
        s = re.sub(r"^>\s?", "", s)  # 引用去标记
        s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1（图：\2）", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
        s = re.sub(r"__([^_]+)__", r"\1", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", s)
        s = re.sub(r"`([^`]+)`", r"\1", s)
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and all(set(c) <= {"-", ":", " "} and c for c in cells):
                continue  # 表格分隔行
            cells = [c for c in cells if c]
            if len(cells) == 2:
                s = f"{cells[0]}：{cells[1]}"
            else:
                s = " ｜ ".join(cells)
        out.append(s)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def send(markdown: str, title: str, *, dry_run: bool = False,
         target: str = "") -> dict:
    bin_path = locate_bin()
    if not bin_path:
        raise ConfigError(
            "hermes CLI not found (tried $HERMES_BIN, which hermes, ~/.local/bin/hermes). "
            "Install hermes-agent or set HERMES_BIN.")
    tgt = target or DEFAULT_TARGET
    body = demarkdown(markdown)
    argv = [bin_path, "send"]
    if title:
        argv += ["-s", title]
    argv += ["-t", tgt, "-f", "-", "--json"]

    if dry_run:
        # info 直接从真实 argv 拼接，保证"打印出来的"就是"会执行的"。
        pretty = " ".join(argv[1:])
        info = (f"[dry-run] would run: {Path(bin_path).name} {pretty}; "
                f"body={len(body)} chars")
        return {"ok": True, "info": info, "dry_run": True, "target": tgt, "body": body}

    try:
        proc = subprocess.run(argv, input=body, capture_output=True, text=True,
                              timeout=SUBPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"hermes send timed out after {SUBPROCESS_TIMEOUT}s"}
    except OSError as e:
        return {"ok": False, "error": f"failed to run hermes: {e}"}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    payload: dict = {"target": tgt}
    if stdout:
        try:
            payload["response"] = json.loads(stdout)
        except ValueError:
            payload["response_raw"] = stdout[:2000]

    if proc.returncode == 0:
        return {"ok": True, "info": f"sent to {tgt} via hermes ({len(body)} chars)",
                **payload}
    payload["error"] = f"hermes send failed (exit {proc.returncode}): {stderr or stdout or 'no output'}"
    return {"ok": False, **payload}
