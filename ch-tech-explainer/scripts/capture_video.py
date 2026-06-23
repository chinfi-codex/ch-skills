#!/usr/bin/env python3
"""把一个 GSAP 分镜 HTML 逐帧导出成 MP4。

脑 / 手边界：本脚本只做确定性的"逐帧 seek + 截图 + 合成"，不产生任何讲解
内容。画面与动效编排都写在 scene HTML 里（由模型按 motion_spec 产出），这里
只负责把 ``window.__tl`` 这条时间轴在 ``[0, duration]`` 上等间隔 seek、截图，
再交给 ffmpeg 拼成视频。

scene HTML 契约：
    - 加载后 ``window.__sceneReady === true``；
    - ``window.__tl`` 是一条 GSAP timeline，``duration()`` 返回有限秒数，
      且 ``seek(t)`` 会同步把画面定到第 t 秒。

依赖（本仓库环境默认未装，需自行安装）：
    pip install playwright && python -m playwright install chromium
    brew install ffmpeg          # 或 apt-get install ffmpeg

用法：
    python scripts/capture_video.py references/examples/hbm_scene.html -o hbm.mp4 --fps 30
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="GSAP 分镜 HTML → MP4（逐帧 seek 截图）。")
    ap.add_argument("scene", help="scene HTML 路径（需暴露 window.__tl / window.__sceneReady）")
    ap.add_argument("-o", "--out", default=None, help="输出 MP4（默认与 scene 同名）")
    ap.add_argument("--fps", type=int, default=30, help="帧率（默认 30）")
    ap.add_argument("--selector", default="#scene", help="截图元素选择器（默认 #scene）")
    ap.add_argument("--duration", type=float, default=None, help="覆盖时间轴时长（秒）")
    ap.add_argument("--scale", type=float, default=2.0, help="设备像素比，越大越清晰（默认 2）")
    args = ap.parse_args()

    scene = Path(args.scene).resolve()
    if not scene.exists():
        print(f"✗ scene 不存在：{scene}", file=sys.stderr)
        return 2
    out = Path(args.out).resolve() if args.out else scene.with_suffix(".mp4")

    if not shutil.which("ffmpeg"):
        print("✗ 缺少 ffmpeg。安装：brew install ffmpeg（或 apt-get install ffmpeg）", file=sys.stderr)
        return 3
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("✗ 缺少 playwright。安装：pip install playwright && python -m playwright install chromium", file=sys.stderr)
        return 3

    frames = Path(tempfile.mkdtemp(prefix="scene_frames_"))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(device_scale_factor=args.scale)
            page.goto(scene.as_uri())
            page.wait_for_function("window.__sceneReady === true", timeout=10000)
            dur = args.duration if args.duration else page.evaluate("window.__tl.duration()")
            if not dur or dur <= 0:
                print("✗ 时间轴时长为 0 或无限，无法逐帧导出（检查 scene 是否用了无限 repeat）。", file=sys.stderr)
                browser.close()
                return 4
            page.evaluate("window.__tl.pause()")
            el = page.query_selector(args.selector)
            if el is None:
                print(f"✗ 找不到截图元素：{args.selector}", file=sys.stderr)
                browser.close()
                return 4

            n = max(1, int(round(dur * args.fps)))
            for i in range(n + 1):
                t = dur * i / n
                page.evaluate("(t) => window.__tl.seek(t, false)", t)
                el.screenshot(path=str(frames / f"f{i:05d}.png"))
            browser.close()

        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(args.fps),
            "-i", str(frames / "f%05d.png"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            str(out),
        ]
        subprocess.run(cmd, check=True)
        print(f"✓ 导出 {out}  （{n + 1} 帧 @ {args.fps}fps，时长 {dur:.2f}s）")
        return 0
    finally:
        shutil.rmtree(frames, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
