#!/usr/bin/env python3
"""把一份 scene script（JSON）渲染成自包含的 HTML 图集。

每个 scene 用 conceptkit 组件渲成一张聚焦画面，并由 motionkit 按 beats 建一条
GSAP 时间轴自动播放；旁白(voiceover)作为画面下的串场文字。这是"图集"交付链路。

脑 / 手边界：本脚本只做确定性的装配——把模型写好的 scene JSON、组件库 JS、皮肤
CSS 拼成一个 HTML 文件。不产生任何讲解内容，不替模型选组件、不改 beats。

用法：
    python scripts/render_gallery.py references/examples/hbm.scene.json -o hbm_gallery.html
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
KIT_DIR = SCRIPT_ROOT / "kit"
ASSETS_DIR = SCRIPT_ROOT.parent / "assets"
GSAP_CDN = "https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"

GALLERY_CSS = """
body{margin:0;background:var(--ck-bg);color:var(--ck-tx);
  font-family:-apple-system,system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
.page{max-width:760px;margin:0 auto;padding:28px 20px 60px}
.hd h1{font-size:22px;font-weight:500;margin:0 0 6px}
.hd .elev{color:var(--ck-tx2);font-size:15px;line-height:1.6;margin:0 0 8px}
.scene-card{border:.5px solid var(--ck-line);border-radius:14px;padding:16px 18px;margin:18px 0;background:var(--ck-bg)}
.scene-hd{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.scene-hd .role{font-size:12px;color:var(--ck-info-tx);background:var(--ck-info-bg);padding:2px 10px;border-radius:8px}
.scene-hd h3{font-size:16px;font-weight:500;margin:0}
.vis{margin:6px 0}
.vo{color:var(--ck-tx2);font-size:14px;line-height:1.7;margin:8px 0 0}
.foot{color:var(--ck-tx2);font-size:12px;margin-top:24px;opacity:.85}
"""

BOOTSTRAP_JS = """
function renderAll(){
  if(!window.CKIT||!window.MK||!window.gsap){return;}
  (DATA.scenes||[]).forEach(function(sc,idx){
    var comp=CKIT[(sc.visual||{}).component];
    var host=document.getElementById('vis-'+idx);
    if(!comp||!host){return;}
    var spec=(sc.visual||{}).spec||{};
    var vb=typeof comp.viewBox==='function'?comp.viewBox(spec):comp.viewBox;
    host.innerHTML='<svg class="ck" viewBox="'+vb+'" width="100%" role="img"><title>'+(sc.title||'')+'</title>'+comp.svg(spec)+'</svg>';
    var beats=sc.beats||comp.beats(spec);
    var tl=MK.buildTimeline(window.gsap,host.querySelector('svg'),beats);
    if(!MK.prefersReduce()){tl.repeat(-1).repeatDelay(1.2).play();}
  });
}
if(window.gsap)renderAll();else window.addEventListener('load',renderAll);
"""


def read_text(path: Path) -> str:
    if not path.exists():
        sys.exit(f"✗ 缺少必需文件：{path}")
    return path.read_text(encoding="utf-8")


def safe_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def build_html(data: dict, css: str, motionkit: str, conceptkit: str) -> str:
    concept = html.escape(str(data.get("concept", "")))
    elevator = html.escape(str(data.get("elevator", "")))
    cards = []
    for idx, sc in enumerate(data.get("scenes", [])):
        role = html.escape(str(sc.get("role", "")))
        title = html.escape(str(sc.get("title", "")))
        vo = html.escape(str(sc.get("voiceover", "")))
        cards.append(
            f'<section class="scene-card"><div class="scene-hd">'
            f'<span class="role">{role}</span><h3>{title}</h3></div>'
            f'<div class="vis" id="vis-{idx}"></div>'
            f'<p class="vo">{vo}</p></section>'
        )
    foot = "科普图解，只讲产业逻辑，不构成买卖建议或个股估值。"
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{concept} · 一图看懂</title>
<style>{css}{GALLERY_CSS}</style></head>
<body><div class="page">
<div class="hd"><h1>{concept} · 一图看懂</h1><p class="elev">{elevator}</p></div>
{''.join(cards)}
<p class="foot">{foot}</p>
</div>
<script>var DATA={safe_json(data)};</script>
<script src="{GSAP_CDN}"></script>
<script>{motionkit}</script>
<script>{conceptkit}</script>
<script>{BOOTSTRAP_JS}</script>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description="scene script JSON → 自包含 HTML 图集。")
    ap.add_argument("scene", help="scene script JSON 路径")
    ap.add_argument("-o", "--out", default=None, help="输出 HTML（默认与输入同名 .html）")
    args = ap.parse_args()

    scene_path = Path(args.scene).resolve()
    if not scene_path.exists():
        sys.exit(f"✗ scene JSON 不存在：{scene_path}")
    data = json.loads(scene_path.read_text(encoding="utf-8"))
    out = Path(args.out).resolve() if args.out else scene_path.with_suffix(".html")

    css = read_text(ASSETS_DIR / "explainer.css")
    motionkit = read_text(KIT_DIR / "motionkit.js")
    conceptkit = read_text(KIT_DIR / "conceptkit.js")

    out.write_text(build_html(data, css, motionkit, conceptkit), encoding="utf-8")
    print(f"✓ 图集 {out}  （{len(data.get('scenes', []))} 镜）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
