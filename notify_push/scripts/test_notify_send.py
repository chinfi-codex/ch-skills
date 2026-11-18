"""notify_send CLI 装配的测试：渠道解析、标题推导、dry-run 隔离、失败折算。

多渠道分发是新的装配层，出错的形态很分散（配置缺失 / 发送失败 / 某渠道拖死
其它渠道），且只在真实调用时才暴露——所以全程 mock 渠道 send、--dry-run，
不发任何真实请求。

跑法（本机没装 pytest 时用后者）：
    cd notify_push/scripts && python3 -m pytest test_notify_send.py -q
    cd notify_push/scripts && python3 -c "import test_notify_send as t; \
        [f() for n, f in vars(t).items() if n.startswith('test_')]; print('ok')"
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
_BUNDLED = SCRIPT_ROOT / "_shared"
_DEV = SCRIPT_ROOT.parents[1] / "shared"
sys.path.append(str(_BUNDLED if _BUNDLED.exists() else _DEV))
sys.path.append(str(SCRIPT_ROOT))

import notify_send as cli  # noqa: E402

SAMPLE = """---
title: frontmatter 兜底标题
tags: [x]
---

10Y 美债 +8bp，黄金 -1.2%。
"""

H1_SAMPLE = "# 8/18 宏观日报\n\n10Y 美债 +8bp。\n"


@contextmanager
def patched_send(module, fn):
    saved = module.send
    module.send = fn
    try:
        yield
    finally:
        module.send = saved


def _run(*argv: str, md_text: str = H1_SAMPLE) -> "tuple[int, list[dict], str]":
    """跑一次 main()。argv 里的 @MD@ 会替换成临时 md 路径。
    返回 (退出码, stdout 里的结果 JSON 行列表, stderr 文本)。"""
    out, err = io.StringIO(), io.StringIO()
    saved_argv = sys.argv
    try:
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "报告.md"
            md.write_text(md_text, encoding="utf-8")
            sys.argv = ["notify_send.py"] + [a.replace("@MD@", str(md)) for a in argv]
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main()
    finally:
        sys.argv = saved_argv
    lines = [json.loads(l) for l in out.getvalue().splitlines() if l.startswith("{")]
    return code, lines, err.getvalue()


def test_parse_channels_defaults_dedupes_and_rejects():
    assert cli.parse_channels(None) == ["pushplus"]
    assert cli.parse_channels(["hermes,feishu", "pushplus", "HERMES"]) == \
        ["hermes", "feishu", "pushplus"]
    try:
        cli.parse_channels(["smser"])
        raise AssertionError("unknown channel must SystemExit")
    except SystemExit as e:
        assert "smser" in str(e)


def test_dry_run_never_touches_the_network():
    """--dry-run 会进各渠道 send 做载荷装配，但网络层一个都不能被碰到。"""
    def boom(*a, **kw):
        raise AssertionError("dry-run must not hit the network")

    saved = (cli.pushplus.send_pushplus, cli.hermes.subprocess.run,
             cli.feishu.subprocess.run, cli.hermes.locate_bin, cli.feishu.shutil.which)
    cli.pushplus.send_pushplus = boom
    cli.hermes.subprocess.run = boom
    cli.feishu.subprocess.run = boom
    cli.hermes.locate_bin = lambda: "/fake/hermes"       # dry-run 只拼 argv，不校验存在
    cli.feishu.shutil.which = lambda name: "/fake/lark-cli"
    try:
        code, lines, _ = _run("@MD@", "--channel", "pushplus,hermes,feishu", "--dry-run")
    finally:
        (cli.pushplus.send_pushplus, cli.hermes.subprocess.run,
         cli.feishu.subprocess.run, cli.hermes.locate_bin, cli.feishu.shutil.which) = saved
    assert code == 0
    assert [l["channel"] for l in lines] == ["pushplus", "hermes", "feishu"]
    assert all(l["ok"] is True and l.get("dry_run") for l in lines)
    assert "not sent" in lines[0]["info"]


def test_multi_channel_csv_keeps_order_and_reports_each():
    code, lines, _ = _run("@MD@", "--channel", "hermes,feishu,pushplus",
                          "--channel", "hermes", "--dry-run")
    assert code == 0
    assert [l["channel"] for l in lines] == ["hermes", "feishu", "pushplus"]


def test_title_from_h1_frontmatter_and_flag():
    code, lines, _ = _run("@MD@", "--dry-run")
    assert "8/18 宏观日报" in lines[0]["info"]
    code, lines, _ = _run("@MD@", "--title", "手拟标题", "--dry-run")
    assert "手拟标题" in lines[0]["info"]
    code, lines, _ = _run("@MD@", "--dry-run", md_text=SAMPLE)
    assert "frontmatter 兜底标题" in lines[0]["info"]


def test_frontmatter_is_stripped_from_body():
    # hermes dry-run 的结果 JSON 带最终正文，能直接断言元数据块没漏进去
    code, lines, _ = _run("@MD@", "--channel", "hermes", "--dry-run", md_text=SAMPLE)
    body = lines[0]["body"]
    assert "tags:" not in body and "title:" not in body
    assert "10Y 美债 +8bp" in body


def test_missing_input_file_is_exit_2():
    out, err = io.StringIO(), io.StringIO()
    saved, sys.argv = sys.argv, ["n", "/nonexistent/no.md"]
    try:
        with redirect_stderr(err):
            assert cli.main() == 2
    finally:
        sys.argv = saved


def test_send_failure_is_isolated_per_channel_exit_1():
    def fail_hermes(*a, **kw):
        return {"ok": False, "error": "boom"}

    with patched_send(cli.hermes, fail_hermes):
        code, lines, err = _run("@MD@", "--channel", "pushplus,hermes", "--dry-run")
    assert code == 1
    by_ch = {l["channel"]: l for l in lines}
    assert by_ch["pushplus"]["ok"] is True      # 一个渠道失败不拖死另一个
    assert by_ch["hermes"]["ok"] is False
    assert "FAILED: hermes" in err


def test_all_config_failures_exit_2():
    saved = cli.hermes.locate_bin
    cli.hermes.locate_bin = lambda: ""
    try:
        code, lines, _ = _run("@MD@", "--channel", "hermes")
    finally:
        cli.hermes.locate_bin = saved
    assert code == 2
    assert lines[0]["fatal"] == "config" and lines[0]["ok"] is False


def test_stdin_input_works():
    saved_stdin, saved_argv = sys.stdin, sys.argv
    sys.stdin = io.StringIO("# stdin 标题\n\n正文\n")
    sys.argv = ["n", "-", "--channel", "pushplus", "--dry-run"]
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            assert cli.main() == 0
    finally:
        sys.stdin, sys.argv = saved_stdin, saved_argv
    assert "stdin 标题" in out.getvalue()


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(vars(sys.modules[__name__]).items())
           if n.startswith("test_") and callable(f)]
    for n, f in fns:
        f()
        print(f"· {n}")
    print(f"ok: {len(fns)} tests")
