#!/usr/bin/env python3
"""把「文字稿(Markdown) + 分镜脚本(JSON)」渲成一份自包含的图文 HTML —— 本 skill 的【默认】交付物。

文章正文用仓库共享的 html_report 框架 + claude 主题排版；每一镜的画面在【构建时】就
服务端渲染成静态 SVG，直接写进对应小节标题之后(conceptkit 组件经 Node 跑 conceptkit_ssr.cjs，
custom 手绘镜用 scene.visual.svg)。GSAP + motionkit 内联，只负责【动画】——把已经在页面上的
静态 SVG 动起来。所以：无 JS / GSAP 失效 → 图仍在(静态末帧)，只是不动；动画是渐进增强。

动画播放是【视口感知】的：用 IntersectionObserver 只在镜进入视口时才建时间轴并播放，
离开视口暂停，标签页隐藏(visibilitychange)时全部暂停——不在后台空转，省 CPU / 电量。

视频是【按需】的次选链路(build_storyboard.py + capture_video.py / mmx-cli)，默认不出视频。

脑 / 手边界：脚本只做确定性装配，不产生讲解内容、不选组件、不改 beats、不下结论。

用法：
    python3 scripts/render_explainer.py --article 文字稿.md --scenes 分镜.scene.json -o out.html
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
KIT_DIR = SCRIPT_ROOT / "kit"
ASSETS = SCRIPT_ROOT.parent / "assets"

_BUNDLED = SCRIPT_ROOT / "_shared"
_DEV = SCRIPT_ROOT.parents[1] / "shared"  # ~/.../ch-skills/shared (dev fallback)
sys.path.insert(0, str(_BUNDLED if _BUNDLED.exists() else _DEV))

from html_report import HtmlReportBuilder  # noqa: E402


# conceptkit 的 --ck-* 皮肤变量桥接到 claude 主题(暖色/黏土，亮色)，补齐 custom 手绘镜常用类，
# 外加每镜图注(scene-fig)的版式。换主题即自适应。
CK_BRIDGE = """
.ck{--ck-bg:var(--surface);--ck-bg2:var(--surface-2);--ck-line:var(--line-1);
  --ck-tx:var(--ink-1);--ck-tx2:var(--ink-3);--ck-info-bg:var(--clay-soft);--ck-info-tx:var(--clay-ink)}
.ck .th{fill:var(--ck-tx);font-size:15px;font-weight:500}
.ck .scr{fill:var(--ck-bg2);opacity:.5;stroke:var(--ck-line);stroke-width:.5}
.ck .stroke-foc{stroke:var(--ck-info-tx);fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.ck .rail{stroke:var(--ck-line);fill:none;stroke-width:1}
.ck .box{fill:var(--ck-bg2);stroke:var(--ck-line);stroke-width:.5}
.ck .hotbox{fill:var(--clay-soft);stroke:var(--clay);stroke-width:.6}
.ck .hl{fill:var(--clay-ink);font-size:12px}
.ck .bar-foc{fill:var(--ck-info-tx)}
.scene-fig{margin:20px 0 28px}
.scene-fig .scene-hd{display:flex;align-items:center;gap:10px;margin:0 0 8px}
.scene-fig .role{font-size:12px;color:var(--clay-ink);background:var(--clay-soft);
  border:1px solid var(--clay-hair);padding:2px 10px;border-radius:999px;white-space:nowrap}
.scene-fig .ck-host{border:1px solid var(--line-1);border-radius:14px;background:var(--surface);
  box-shadow:var(--shadow-1);padding:14px 16px}
.scene-fig .scene-vo{color:var(--ink-3);font-size:14px;line-height:1.75;margin:9px 2px 0}
"""

# 渐进增强 hydration：图已在页面上(静态末帧)。只有 GSAP+motionkit 在、且非 reduced-motion 时，
# 才【按视口】懒构建时间轴并播放；离开视口暂停，标签页隐藏全部暂停。一个坏镜不连累其它(try/catch)。
HYDRATE = r"""
(function () {
  function run() {
    if (!window.MK || !window.gsap) return;          // 无动画引擎 → 静态 SVG 已可见
    if (window.MK.prefersReduce && window.MK.prefersReduce()) return; // 尊重 reduced-motion：保持静态末帧
    var svgs = Array.prototype.slice.call(document.querySelectorAll('#report-body svg.ck[data-scene]'));
    if (!svgs.length || !('IntersectionObserver' in window)) return;
    var beatsAll = window.__SCENE_BEATS || [];
    var states = svgs.map(function (svg) {
      return { svg: svg, beats: (beatsAll[+svg.getAttribute('data-scene')] || []), tl: null, vis: false };
    });
    function ensure(st) {
      if (st.tl) return st.tl;
      try { st.tl = window.MK.buildTimeline(window.gsap, st.svg, st.beats); }
      catch (e) { st.tl = null; }
      return st.tl;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        var st = e.target.__st; if (!st) return;
        st.vis = e.isIntersecting;
        if (e.isIntersecting) {
          var tl = ensure(st);
          if (tl && !document.hidden) tl.repeat(-1).repeatDelay(1.4).play();
        } else if (st.tl) { st.tl.pause(); }
      });
    }, { threshold: 0.25 });
    states.forEach(function (st) { st.svg.__st = st; io.observe(st.svg); });
    document.addEventListener('visibilitychange', function () {
      states.forEach(function (st) {
        if (!st.tl) return;
        if (document.hidden) st.tl.pause();
        else if (st.vis) st.tl.play();
      });
    });
  }
  if (window.gsap) run(); else window.addEventListener('load', run);
})();
"""

_VB_RE = re.compile(r"^[\d.\s\-]+$")
_HEADING_RE = re.compile(r"<h[234][^>]*>(.*?)</h[234]>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _read(path: Path) -> str:
    if not path.exists():
        sys.exit(f"✗ 缺少必需文件：{path}")
    return path.read_text(encoding="utf-8")


def safe_js_json(data) -> str:
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", _TAG_RE.sub("", s or "")).lower()


def ssr_conceptkit(payload: list) -> list:
    """跑 Node 把 conceptkit 组件渲成 {viewBox, svg, beats}。Node 是默认路径的构建期依赖。"""
    node = shutil.which("node")
    if not node:
        sys.exit("✗ 默认图文网页需要 Node 在构建时服务端渲染 conceptkit 图元——请安装 node，"
                 "或把这些镜改成 custom 手绘 SVG(visual.svg)。")
    helper = KIT_DIR / "conceptkit_ssr.cjs"
    proc = subprocess.run([node, str(helper)], input=json.dumps({"scenes": payload}),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"✗ conceptkit 服务端渲染失败：{proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout)


def resolve_scenes(scenes: list) -> list:
    """每镜解析成 {role,title,voiceover,anchor,viewBox,svg,beats}；conceptkit 走 Node，custom 直用。"""
    resolved = [None] * len(scenes)
    node_idx, node_payload = [], []
    for i, sc in enumerate(scenes):
        v = sc.get("visual", {}) or {}
        comp = v.get("component", "")
        if comp and comp != "custom":
            node_idx.append(i)
            node_payload.append({"component": comp, "spec": v.get("spec", {}), "beats": sc.get("beats")})
        else:
            resolved[i] = {"viewBox": v.get("viewBox", "0 0 680 300"),
                           "svg": v.get("svg", ""), "beats": sc.get("beats", [])}
            if not v.get("svg"):
                print(f"warning: 第 {i} 镜是 custom 但缺 visual.svg，将渲成空画面。", file=sys.stderr)
    if node_payload:
        for k, res in enumerate(ssr_conceptkit(node_payload)):
            i = node_idx[k]
            if res.get("error"):
                print(f"warning: 第 {i} 镜 conceptkit 渲染失败：{res['error']}", file=sys.stderr)
                resolved[i] = {"viewBox": "0 0 680 60", "svg": "", "beats": []}
            else:
                resolved[i] = {"viewBox": res["viewBox"], "svg": res["svg"], "beats": res["beats"]}
    for i, sc in enumerate(scenes):
        resolved[i].update({"role": sc.get("role", ""), "title": sc.get("title", ""),
                            "voiceover": sc.get("voiceover", ""), "anchor": sc.get("anchor", "")})
    return resolved


def figure_html(idx: int, r: dict) -> str:
    vb = r["viewBox"] if _VB_RE.match(str(r.get("viewBox") or "")) else "0 0 680 300"
    title = html.escape(r.get("title", ""))
    role = (f'<div class="scene-hd"><span class="role">{html.escape(r["role"])}</span></div>'
            if r.get("role") else "")
    vo = (f'<figcaption class="scene-vo">{html.escape(r["voiceover"])}</figcaption>'
          if r.get("voiceover") else "")
    return (f'<figure class="scene-fig">{role}'
            f'<div class="ck-host"><svg class="ck" data-scene="{idx}" viewBox="{vb}" width="100%" '
            f'role="img" aria-label="{title}"><title>{title}</title>{r["svg"]}</svg></div>'
            f'{vo}</figure>')


def insert_figures(html_text: str, resolved: list) -> str:
    """把每镜 figure 插到锚点小节标题之后(命中不到的追到正文末，并 warn)。从右往左插，索引不漂移。"""
    heads = [(m.end(), _norm(m.group(1))) for m in _HEADING_RE.finditer(html_text)]
    by_pos: dict = {}
    unmatched: list = []
    for idx, r in enumerate(resolved):
        fig = figure_html(idx, r)
        key = _norm(r.get("anchor") or r.get("title") or "")
        pos = next((end for end, txt in heads if key and key in txt), None)
        if pos is None:
            unmatched.append((idx, fig))
            print(f"warning: 第 {idx} 镜 anchor「{r.get('anchor') or r.get('title')}」未命中任何小节标题，"
                  f"已追加到正文末尾。", file=sys.stderr)
        else:
            by_pos.setdefault(pos, []).append(fig)
    for pos in sorted(by_pos, reverse=True):
        html_text = html_text[:pos] + "".join(by_pos[pos]) + html_text[pos:]
    if unmatched:
        close = html_text.find("</section>", html_text.find('id="report-body"'))
        block = "".join(fig for _, fig in unmatched)
        if close != -1:
            html_text = html_text[:close] + block + html_text[close:]
        else:
            html_text += block
    return html_text


def main() -> int:
    ap = argparse.ArgumentParser(description="文字稿(MD) + 分镜(JSON) → claude 主题图文 HTML（图静态渲染 + 动画渐进增强）。")
    ap.add_argument("--article", "-i", required=True, help="文字稿 Markdown 路径")
    ap.add_argument("--scenes", "-s", required=True, help="分镜脚本 JSON 路径")
    ap.add_argument("--output", "-o", default=None, help="HTML 输出路径（默认与文字稿同名 .html）")
    ap.add_argument("--title", default=None, help="文档标题（默认取 scene.concept 或文件名）")
    ap.add_argument("--meta", default="", help="文档头部副标题（如日期 / 主线）")
    ap.add_argument("--theme", default="claude", help="主题（默认 claude）")
    ap.add_argument("--strict", action="store_true", help="文本保全校验失败即中止（默认转 warning 不阻断）")
    args = ap.parse_args()

    article_path = Path(args.article)
    scenes_path = Path(args.scenes)
    md = _read(article_path)
    data = json.loads(_read(scenes_path))
    scenes = data.get("scenes", [])
    title = args.title or str(data.get("concept") or article_path.stem)
    out = Path(args.output) if args.output else article_path.with_suffix(".html")

    resolved = resolve_scenes(scenes)
    beats_all = [r.get("beats", []) for r in resolved]

    gsap = _read(ASSETS / "vendor" / "gsap.min.js")
    motionkit = _read(KIT_DIR / "motionkit.js")
    explainer_css = _read(ASSETS / "explainer.css")

    builder = HtmlReportBuilder(
        title=title, theme=args.theme, meta_text=args.meta,
        extra_css=explainer_css + "\n" + CK_BRIDGE,
    )
    # 只内联动画所需：GSAP + motionkit + 每镜 beats + hydration（conceptkit 不再进浏览器，图已服务端渲染）。
    builder.add_ui_decoration(gsap)
    builder.add_ui_decoration(motionkit)
    builder.add_ui_decoration("window.__SCENE_BEATS = " + safe_js_json(beats_all) + ";")
    builder.add_ui_decoration(HYDRATE)

    try:
        html_text = builder.render(md, validate=True)
    except RuntimeError as exc:
        if args.strict:
            raise
        print(f"warning: {exc}", file=sys.stderr)
        html_text = builder.render(md, validate=False)

    html_text = insert_figures(html_text, resolved)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    print(json.dumps({"output": str(out), "theme": args.theme, "scenes": len(scenes)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
