"""三个渠道适配器的测试：argv 装配、目标解析、成功判定、去 markdown。

渠道适配是对外部 CLI（hermes / lark-cli）和 HTTP API（pushplus）的薄封装，
契约错误（参数顺序、目标 flag、成功信封字段）本机一跑就知道，但换台机器就
难复现——所以 subprocess 全部 mock，只验证我们自己装配的那一半。

跑法（本机没装 pytest 时用后者）：
    cd notify_push/scripts && python3 -m pytest test_channels.py -q
    cd notify_push/scripts && python3 -c "import test_channels as t; \
        [f() for n, f in vars(t).items() if n.startswith('test_')]; print('ok')"
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
_BUNDLED = SCRIPT_ROOT / "_shared"
_DEV = SCRIPT_ROOT.parents[1] / "shared"
sys.path.append(str(_BUNDLED if _BUNDLED.exists() else _DEV))
sys.path.append(str(SCRIPT_ROOT))

from channels import ConfigError  # noqa: E402
from channels import feishu, hermes, pushplus  # noqa: E402


class FakeProc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


MD = "# 标题行\n\n正文 **加粗** [链接](https://e.com)。\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"


# --------------------------------------------------------------------------- #
# hermes
# --------------------------------------------------------------------------- #
def test_hermes_argv_uses_stdin_and_subject():
    seen = {}

    def fake_run(argv, input=None, **kw):
        seen["argv"], seen["input"] = argv, input
        return FakeProc(0, json.dumps({"ok": True, "data": {}}))

    saved_bin, saved_run = hermes.locate_bin, hermes.subprocess.run
    hermes.locate_bin = lambda: "/fake/hermes"
    hermes.subprocess.run = fake_run
    try:
        r = hermes.send(MD, "我的标题")
    finally:
        hermes.locate_bin, hermes.subprocess.run = saved_bin, saved_run
    assert r["ok"] is True
    assert seen["argv"] == ["/fake/hermes", "send", "-s", "我的标题",
                            "-t", "weixin", "-f", "-", "--json"]
    assert "标题行" in seen["input"] and "**" not in seen["input"]


def test_hermes_target_override_and_failure_path():
    seen = {}

    def fake_run(argv, input=None, **kw):
        seen["argv"] = argv
        return FakeProc(1, "", "device offline")

    saved_bin, saved_run = hermes.locate_bin, hermes.subprocess.run
    hermes.locate_bin = lambda: "/fake/hermes"
    hermes.subprocess.run = fake_run
    try:
        r = hermes.send("hi", "", target="weixin:chat123")
    finally:
        hermes.locate_bin, hermes.subprocess.run = saved_bin, saved_run
    assert r["ok"] is False and "device offline" in r["error"]
    assert "-t" in seen["argv"] and "weixin:chat123" in seen["argv"]


def test_hermes_missing_bin_is_config_error():
    saved = hermes.locate_bin
    hermes.locate_bin = lambda: ""
    try:
        try:
            hermes.send("hi", "")
            raise AssertionError("missing bin must raise ConfigError")
        except ConfigError as e:
            assert "HERMES_BIN" in str(e)
    finally:
        hermes.locate_bin = saved


def test_demarkdown_transformations():
    md = (
        "# 大标题\n"
        "## 小标题\n"
        "段落 **粗** *斜* `码` [字](https://x.y) ![图](https://i.x/y.png)\n"
        "> 引用一句\n\n"
        "- 甲\n- 乙\n\n"
        "| k1 | k2 | k3 |\n|---|---|---|\n| v1 | v2 | v3 |\n"
        "---\n\n```\nkeep **raw**\n```\n"
    )
    out = hermes.demarkdown(md)
    assert "# 大标题" not in out and "大标题" in out
    assert "字（https://x.y）" in out and "图（图：https://i.x/y.png）" in out
    assert "引用一句" in out
    assert "- 甲" in out
    assert "v1 ｜ v2 ｜ v3" in out          # 三列表转短行
    assert "keep **raw**" in out            # 代码块内容原样保留
    assert "---" not in out                 # 水平线与表格分隔行都消失
    assert "k1 ｜ k2 ｜ k3" in out          # 表头同样转成一行
    outside_fence = "\n".join(l for l in out.splitlines() if "keep" not in l)
    assert "**" not in outside_fence and "*" not in outside_fence and "`" not in outside_fence


def test_demarkdown_two_col_table_becomes_kv_lines():
    out = hermes.demarkdown("| 品种 | 涨跌 |\n|---|---|\n| 美债 | +8bp |")
    assert "品种：涨跌" in out and "美债：+8bp" in out


# --------------------------------------------------------------------------- #
# feishu
# --------------------------------------------------------------------------- #
def test_feishu_target_routing_oc_ou_and_bad():
    assert feishu.resolve_target_flag("oc_abc") == ("--chat-id", "oc_abc")
    assert feishu.resolve_target_flag("ou_xyz") == ("--user-id", "ou_xyz")
    for bad in ("", "weixin", "chat:oc_a"):
        try:
            feishu.resolve_target_flag(bad)
            raise AssertionError(f"{bad!r} must raise")
        except ConfigError:
            pass


def test_feishu_default_target_from_config():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.json"
        cfg.write_text(json.dumps({"apps": [{"users": [
            {"userOpenId": "", "userName": "占位"},
            {"userOpenId": "ou_self", "userName": "我"}]}]}), encoding="utf-8")
        saved = feishu.LARK_CONFIG_PATH
        feishu.LARK_CONFIG_PATH = cfg
        try:
            assert feishu._default_target() == "ou_self"
            cfg.write_text("{}", encoding="utf-8")
            try:
                feishu._default_target()
                raise AssertionError("no user must raise")
            except ConfigError as e:
                assert "auth login" in str(e) or "userOpenId" in str(e)
        finally:
            feishu.LARK_CONFIG_PATH = saved


def test_feishu_compose_prepends_title_without_duplication():
    no_h1 = feishu._compose("正文", "T")
    assert no_h1 == "**T**\n\n正文"
    with_h1 = feishu._compose("# T\n\n正文", "T")
    assert with_h1 == "# T\n\n正文"       # 已有同文标题不重复拼
    assert feishu._compose("# 其它\n\n正文", "T").startswith("**T**")


def test_feishu_send_argv_and_ok_verdict():
    seen = {}

    def fake_run(argv, capture_output=None, text=None, timeout=None, env=None):
        seen.update(argv=argv, env=env)
        return FakeProc(0, json.dumps({"ok": True, "data": {"message_id": "om_1"}}))

    saved_which, saved_run = feishu.shutil.which, feishu.subprocess.run
    feishu.shutil.which = lambda name: "/fake/lark-cli"
    feishu.subprocess.run = fake_run
    try:
        r = feishu.send("正文", "标题", target="oc_grp")
    finally:
        feishu.shutil.which, feishu.subprocess.run = saved_which, saved_run
    assert r["ok"] is True and r["info"].startswith("sent to feishu chat")
    argv = seen["argv"]
    assert argv[:5] == ["/fake/lark-cli", "im", "+messages-send", "--chat-id", "oc_grp"]
    assert "--markdown" in argv and "**标题**\n\n正文" in argv
    key = argv[argv.index("--idempotency-key") + 1]
    assert key.startswith("np-") and len(key) <= 50
    assert seen["env"]["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] == "1"
    assert seen["env"]["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] == "1"


def test_feishu_rejects_non_ok_envelope_even_on_exit_zero():
    def fake_run(argv, **kw):
        return FakeProc(0, json.dumps({"ok": False, "error": {"code": 99991663}}))

    saved_which, saved_run = feishu.shutil.which, feishu.subprocess.run
    feishu.shutil.which = lambda name: "/fake/lark-cli"
    feishu.subprocess.run = fake_run
    try:
        r = feishu.send("正文", "", target="ou_me")
    finally:
        feishu.shutil.which, feishu.subprocess.run = saved_which, saved_run
    assert r["ok"] is False and "99991663" in r["error"]  # 成功信封判 ok，别判 exit code


def test_feishu_missing_cli_is_config_error():
    saved = feishu.shutil.which
    feishu.shutil.which = lambda name: None
    try:
        try:
            feishu.send("hi", "")
            raise AssertionError("missing cli must raise ConfigError")
        except ConfigError as e:
            assert "lark-cli" in str(e)
    finally:
        feishu.shutil.which = saved


# --------------------------------------------------------------------------- #
# pushplus
# --------------------------------------------------------------------------- #
def test_pushplus_dry_run_writes_preview_and_never_sends():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "p.html"

        def boom(*a, **kw):
            raise AssertionError("dry-run must not hit the API")

        saved = pushplus.send_pushplus
        pushplus.send_pushplus = boom
        try:
            r = pushplus.send("# T\n\n正文", "T", dry_run=True, save_html=str(out))
        finally:
            pushplus.send_pushplus = saved
        assert r["ok"] is True and out.exists()
        page = out.read_text(encoding="utf-8")
        assert page.startswith("<!doctype html>") and 'charset="utf-8"' in page
        assert 'id="report-body"' in page


def test_pushplus_no_token_is_config_error():
    try:
        pushplus.send("# T\n\n正文", "T", token="")
        raise AssertionError("missing token must raise ConfigError")
    except ConfigError as e:
        assert "PUSHPLUS_TOKEN" in str(e)


def test_pushplus_api_failure_maps_to_ok_false():
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("conn reset")

    saved = pushplus.urllib.request.urlopen
    pushplus.urllib.request.urlopen = fake_urlopen
    try:
        r = pushplus.send("# T\n\n正文", "T", token="t")
    finally:
        pushplus.urllib.request.urlopen = saved
    assert r["ok"] is False and "request failed" in r["error"]


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(vars(sys.modules[__name__]).items())
           if n.startswith("test_") and callable(f)]
    for n, f in fns:
        f()
        print(f"· {n}")
    print(f"ok: {len(fns)} tests")
