"""md_to_pushplus CLI 装配的测试：渲染器选择、降级回落、save-html 分支。

这三条路径原来只有手工验证过——降级只在"共享包没同步过来"的机器上才走到，
那台机器上最难复现，所以必须有测试盯着。全程 --dry-run，不发任何请求。

跑法（本机没装 pytest 时用后者）：
    cd md-pushplus/scripts && python3 -m pytest test_md_to_pushplus.py -q
    cd md-pushplus/scripts && python3 -c "import test_md_to_pushplus as t; \
        [f() for n, f in vars(t).items() if n.startswith('test_')]; print('ok')"
"""
from __future__ import annotations

import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
_BUNDLED = SCRIPT_ROOT / "_shared"
_DEV = SCRIPT_ROOT.parents[1] / "shared"
sys.path.append(str(_BUNDLED if _BUNDLED.exists() else _DEV))
sys.path.append(str(SCRIPT_ROOT))

import md_to_pushplus as cli  # noqa: E402

SAMPLE = """# 8/18 宏观日报

10Y 美债 +8bp，黄金 -1.2%。

| 品种 | 涨跌 |
|---|---|
| 美债 | +8bp |
"""


def _run(*argv: str) -> str:
    """跑一次 main()，返回 stdout。"""
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp) / "报告.md"
        md.write_text(SAMPLE, encoding="utf-8")
        saved, sys.argv = sys.argv, ["md_to_pushplus.py", str(md), *argv]
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = cli.main()
        finally:
            sys.argv = saved
        assert code == 0, out.getvalue()
        text = out.getvalue()
        for line in text.splitlines():
            if line.startswith("Saved preview HTML"):
                path = Path(line.split("→", 1)[1].split("(")[0].strip())
                text += "\n<<<PREVIEW>>>" + path.read_text(encoding="utf-8")
        return text


def test_default_run_uses_the_shared_theme_fragment():
    out = _run("--dry-run")
    assert "renderer=theme:default" in out
    assert "css " in out and "外壳 " in out


def test_dry_run_reports_both_chars_and_utf8_bytes():
    out = _run("--dry-run")
    # 中文报告的字节数必然大于字符数；上限按字节判定才是保守的那一侧
    assert "chars /" in out and "UTF-8 bytes" in out


def test_explicit_inline_renderer_is_honored():
    out = _run("--dry-run", "--renderer", "inline")
    assert "renderer=inline" in out


def test_theme_falls_back_to_inline_when_shared_is_missing():
    saved = cli.render_push_fragment
    cli.render_push_fragment = None
    try:
        out = _run("--dry-run")
    finally:
        cli.render_push_fragment = saved
    assert "renderer=inline" in out


def test_saved_preview_is_a_standalone_page_in_both_renderers():
    with tempfile.TemporaryDirectory() as tmp:
        for renderer in ("theme", "inline"):
            out_path = Path(tmp) / f"{renderer}.html"
            out = _run("--dry-run", "--renderer", renderer, "--save-html", str(out_path))
            page = out.split("<<<PREVIEW>>>", 1)[1]
            # 缺 charset 的预览文件在本地打开会把整篇中文变乱码
            assert page.startswith("<!doctype html>")
            assert 'charset="utf-8"' in page
            assert "width=device-width" in page


def test_preview_file_carries_the_push_payload_verbatim():
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "p.html"
        out = _run("--dry-run", "--save-html", str(out_path))
        page = out.split("<<<PREVIEW>>>", 1)[1]
        assert "<style>" in page and 'id="report-body"' in page
