#!/usr/bin/env python3
"""pushplus 渠道：Markdown → 自包含 HTML 片段 → PushPlus HTTP API。

发送逻辑从旧 md_to_pushplus.py 原样迁出：template 固定 ``html``，默认走
shared/html_report 主题渲染（与各报告型 skill 的网页版同一套观感），共享包
缺失时自动降级 inline 并在 stderr 说明。content 上限按 UTF-8 字节保守判定
（中文一字符 3 字节，文档没写清上限是字符还是字节，按字节那一侧早警告）。

opts：token / theme / renderer / template / topic / pp_channel / save_html。
"""
from __future__ import annotations

import html
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import ConfigError

PUSHPLUS_ENDPOINT = "https://www.pushplus.plus/send"
CONTENT_LIMIT = 40000

# 共享渲染框架：装包后用 scripts/_shared，开发时回落仓库根的 shared/。
# 本模块自己定位，直接被测试导入时也不依赖入口脚本先摆 sys.path。
_SCRIPTS = Path(__file__).resolve().parents[1]
_BUNDLED_SHARED = _SCRIPTS / "_shared"
_DEV_SHARED = _SCRIPTS.parents[1] / "shared"
for _p in (str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.append(_p)

try:  # 共享包缺失不该让推送整个失败——降级到内联渲染即可。
    from push_render import fragment_stats, render_push_fragment, wrap_preview  # noqa: E402

    SHARED_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001 — 任何导入期异常都只降级，不中断
    render_push_fragment = None  # type: ignore[assignment]
    fragment_stats = None  # type: ignore[assignment]
    SHARED_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

    def wrap_preview(fragment: str, title: str) -> str:  # type: ignore[misc]
        """共享包缺失时的最小预览外壳。

        降级路径本就是给"共享包没同步过来"的机器用的，那台机器上也最难复现问题，
        所以三个符号都要有定义——预览文件缺 charset 会让整篇中文变乱码。
        """
        return (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title></head>"
            f'<body style="margin:0;background:#fff">{fragment}</body></html>'
        )


def send_pushplus(token: str, title: str, content: str, *,
                  template: str = "html", topic: str = "", pp_channel: str = "") -> dict:
    payload = {"token": token, "title": title, "content": content, "template": template}
    if topic:
        payload["topic"] = topic
    if pp_channel:
        payload["channel"] = pp_channel
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        PUSHPLUS_ENDPOINT, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send(markdown_body: str, title: str, *, dry_run: bool = False,
         token: str = "", theme: str = "default", renderer: str = "theme",
         template: str = "html", topic: str = "", pp_channel: str = "",
         save_html: str = "") -> dict:
    if renderer == "theme" and render_push_fragment is None:
        print(f"WARNING: shared/html_report unavailable ({SHARED_IMPORT_ERROR}); "
              "falling back to --renderer inline.", file=sys.stderr)
        renderer = "inline"

    if renderer == "theme":
        content = render_push_fragment(markdown_body, theme=theme)
        stats = fragment_stats(content)
        style_note = f"theme:{theme} (css {stats['css']} + body {stats['body']} + 外壳 {stats['shell']})"
        preview = wrap_preview(content, title)
    else:
        from inline_render import render_markdown_inline, wrap_html_inline

        content = wrap_html_inline(render_markdown_inline(markdown_body))
        style_note = "inline"
        preview = wrap_preview(content, title)

    if save_html:
        # 预览文件比推送包多一层 doctype/viewport 外壳，正文与样式逐字节相同，
        # 这样本地按手机视口打开看到的就是微信里的样子。
        Path(save_html).write_text(preview, encoding="utf-8")
        print(f"Saved preview HTML → {save_html} "
              f"(push payload {len(content)} chars, {style_note})")

    content_bytes = len(content.encode("utf-8"))
    if content_bytes > CONTENT_LIMIT:
        print(f"WARNING: content is {content_bytes} UTF-8 bytes ({len(content)} chars), over "
              f"PushPlus limit ~{CONTENT_LIMIT}. PushPlus may reject it. "
              "Consider splitting the report.", file=sys.stderr)

    if dry_run:
        info = (f"[dry-run] title={title!r}, renderer={style_note}, "
                f"content={len(content)} chars / {content_bytes} UTF-8 bytes, not sent.")
        return {"ok": True, "info": info, "dry_run": True}

    if not token:
        raise ConfigError("no token. Set $PUSHPLUS_TOKEN or pass --token.")

    try:
        result = send_pushplus(token, title, content,
                               template=template, topic=topic, pp_channel=pp_channel)
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"request failed: {e}"}

    if result.get("code") == 200:
        return {"ok": True, "info": f"pushed '{title}' ({style_note}, {content_bytes} UTF-8 bytes)",
                "response": result}
    return {"ok": False, "error": f"PushPlus returned code={result.get('code')} msg={result.get('msg')}",
            "response": result}
