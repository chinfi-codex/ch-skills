#!/usr/bin/env python3
"""notify_push 统一入口：把一份 Markdown 按一个或多个渠道推送出去。

原子工具，只做「渠道适配 + 投递」这类确定性的事，不做任何内容判断——
推原文还是推总结、总结怎么写，由调用方（模型/用户）准备好再传进来。

三个渠道：
    pushplus  Markdown → 自包含 HTML 片段 → PushPlus API（微信/邮箱，需 $PUSHPLUS_TOKEN）
    hermes    纯文本短消息 → hermes send（默认 weixin home 频道，凭据在 ~/.hermes）
    feishu    markdown → lark-cli im +messages-send（默认发给自己，需 lark-cli 已登录）

多渠道一次调用：--channel pushplus,feishu 或重复 --channel，逐渠道发送，
某渠道失败不影响其它渠道；stdout 每个渠道打一行机器可解析的结果 JSON。
退出码：0 全部成功或 dry-run；1 任一发送失败；2 输入/配置缺失（一个渠道
都没能尝试）。发送类失败经 stderr 汇总。

用法示例：
    export PUSHPLUS_TOKEN=xxxx
    python notify_send.py 报告.md                                  # 默认 pushplus
    python notify_send.py 摘要.md --channel hermes                 # 微信纯文本
    python notify_send.py 摘要.md --channel feishu                 # 飞书发给自己
    python notify_send.py 摘要.md --channel pushplus,feishu        # 一次两渠道
    python notify_send.py 报告.md --dry-run --save-html /tmp/p.html  # pushplus 只渲染不发送
    cat 摘要.md | python notify_send.py - --channel hermes --title "盘后速览"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
# channels 包自己定位 _shared；这里再补一次，保证 inline_render 等兄弟模块可直接导入。
sys.path.append(str(SCRIPT_ROOT))

from channels import ConfigError  # noqa: E402
from channels import feishu, hermes, pushplus  # noqa: E402
from inline_render import derive_title, strip_frontmatter  # noqa: E402

CHANNELS = ("pushplus", "hermes", "feishu")


def parse_channels(raw: "list[str] | None") -> "list[str]":
    """--channel 支持逗号分隔与重复传参，去重且保持首次出现的顺序。"""
    if not raw:
        return ["pushplus"]
    seen: "list[str]" = []
    for chunk in raw:
        for name in chunk.split(","):
            name = name.strip().lower()
            if name and name not in seen:
                seen.append(name)
    bad = [n for n in seen if n not in CHANNELS]
    if bad:
        raise SystemExit(f"ERROR: unknown channel(s): {', '.join(bad)}; choose from {', '.join(CHANNELS)}")
    return seen


def read_input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"input file not found: {p}")
    return p.read_text(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Push a Markdown file (original or a summary you prepared) via pushplus/hermes/feishu.")
    ap.add_argument("input", help="Markdown file path, or '-' for stdin")
    ap.add_argument("--channel", action="append", default=None, metavar="NAME",
                    help="pushplus|hermes|feishu; comma list or repeatable (default: pushplus)")
    ap.add_argument("--title", default=None,
                    help="Push title (default: first H1, else frontmatter title:, else filename)")
    ap.add_argument("--target", default="",
                    help="Channel target: hermes destination (default weixin) or "
                         "feishu chat_id oc_…/open_id ou_… (feishu default: yourself)")
    # pushplus 专属
    ap.add_argument("--token", default=os.environ.get("PUSHPLUS_TOKEN", ""),
                    help="PushPlus token (default: $PUSHPLUS_TOKEN)")
    ap.add_argument("--template", default="html", help="PushPlus template (default: html)")
    ap.add_argument("--renderer", default="theme", choices=["theme", "inline"],
                    help="pushplus renderer: theme = shared/html_report (default); "
                         "inline = self-contained fallback for clients that strip <style>")
    ap.add_argument("--theme", default="default",
                    help="Shared theme for --renderer theme (default: default)")
    ap.add_argument("--topic", default="", help="PushPlus group code for one-to-many push")
    ap.add_argument("--pushplus-channel", dest="pp_channel", default="",
                    help="PushPlus delivery channel: wechat|mail|webhook|cp|sms")
    ap.add_argument("--save-html", default="",
                    help="pushplus only: also write the rendered HTML preview to this path")
    ap.add_argument("--idempotency-key", dest="idem_key", default="",
                    help="feishu only: override the derived idempotency key (<=50 chars)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Prepare payloads per channel but do not send anything")
    return ap


def dispatch(name: str, markdown: str, title: str, args: argparse.Namespace) -> dict:
    """按渠道名装配 opts 并调用适配器。渠道内部不互相感知。"""
    if name == "pushplus":
        return pushplus.send(markdown, title, dry_run=args.dry_run, token=args.token,
                             theme=args.theme, renderer=args.renderer,
                             template=args.template, topic=args.topic,
                             pp_channel=args.pp_channel, save_html=args.save_html)
    if name == "hermes":
        return hermes.send(markdown, title, dry_run=args.dry_run, target=args.target)
    if name == "feishu":
        return feishu.send(markdown, title, dry_run=args.dry_run,
                           target=args.target, key=args.idem_key)
    raise AssertionError(f"unreachable channel {name}")  # parse_channels 已把关


def main() -> int:
    args = build_parser().parse_args()

    try:
        md_text = read_input(args.input)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    meta, body = strip_frontmatter(md_text)
    fallback = "stdin" if args.input == "-" else Path(args.input).stem
    title = args.title or derive_title(body, meta, fallback)
    channels = parse_channels(args.channel)

    results: "list[dict]" = []
    send_failed: "list[str]" = []
    config_failed: "list[str]" = []
    for name in channels:
        try:
            outcome = dispatch(name, body, title, args)
        except ConfigError as e:
            outcome = {"ok": False, "error": str(e), "fatal": "config"}
        outcome = {"channel": name, **outcome}
        print(json.dumps(outcome, ensure_ascii=False))
        results.append(outcome)
        if not outcome.get("ok"):
            (config_failed if outcome.get("fatal") == "config" else send_failed).append(name)

    if send_failed:
        exit_code = 1  # 有渠道真的尝试发送且失败，比"没到能发那一步"更严重
    elif config_failed:
        exit_code = 2
    else:
        exit_code = 0

    sent = [r["channel"] for r in results if r.get("ok") and not r.get("dry_run")]
    failed = send_failed + config_failed
    if failed:
        print(f"FAILED: {', '.join(failed)} — details in the result lines above.", file=sys.stderr)
    elif sent:
        print(f"OK: pushed '{title}' via {', '.join(sent)}.", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
