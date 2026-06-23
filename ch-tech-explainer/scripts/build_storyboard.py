#!/usr/bin/env python3
"""把一份 scene script（JSON）摊成视频分镜表 + 交给 mmx-cli 的手卡。

产出两个文件：
  <name>.storyboard.md  —— 人读的分镜表（镜号 / 角色 / 画面 / 口播 / 字幕 / 时长）。
  <name>.mmx.json       —— 机器手卡：每镜的口播、时长、字幕、应配的画面文件名，
                           供 mmx-cli 做 TTS 配音与合成时逐镜对齐。

脑 / 手边界：本脚本只做确定性的"重排格式"，不改写口播、不生成画面、不调用任何
模型或视频接口。真正出片(配音 / 合成)交给 mmx-cli。

用法：
    python scripts/build_storyboard.py references/examples/hbm.scene.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def spec_digest(visual: dict) -> str:
    """把 visual.spec 压成一句话画面摘要，给人读的分镜表用。"""
    comp = visual.get("component", "")
    spec = visual.get("spec", {}) or {}
    bits = []
    for key in ("title", "topLabel", "channelLabel"):
        if spec.get(key):
            bits.append(str(spec[key]))
    for key in ("layers", "bars", "nodes", "milestones", "signals"):
        val = spec.get(key)
        if isinstance(val, list) and val:
            bits.append(f"{key}×{len(val)}")
    return f"{comp}（{' · '.join(bits)}）" if bits else comp


def captions_text(captions) -> str:
    if not isinstance(captions, list):
        return ""
    return " / ".join(str(c.get("text", "")) for c in captions if isinstance(c, dict))


def build_markdown(data: dict, asset_stem: str) -> str:
    rows = ["# 分镜表 · " + str(data.get("concept", "")), ""]
    rows.append("> " + str(data.get("elevator", "")))
    rows.append("")
    rows.append("| 镜 | 角色 | 画面 | 口播 | 字幕 | 时长(s) | 画面文件 |")
    rows.append("|---|---|---|---|---|---|---|")
    for idx, sc in enumerate(data.get("scenes", [])):
        img = f"{asset_stem}_s{idx:02d}.png"
        rows.append("| {i} | {role} | {vis} | {vo} | {cap} | {dur} | `{img}` |".format(
            i=idx,
            role=str(sc.get("role", "")),
            vis=spec_digest(sc.get("visual", {}) or {}).replace("|", "／"),
            vo=str(sc.get("voiceover", "")).replace("|", "／"),
            cap=captions_text(sc.get("captions")).replace("|", "／"),
            dur=sc.get("duration", ""),
            img=img,
        ))
    rows += ["", "出片：把每镜画面导成 PNG（capture_video.py 或截图），连同本表交 mmx-cli 配音合成。"]
    return "\n".join(rows) + "\n"


def build_mmx(data: dict, asset_stem: str) -> dict:
    shots = []
    for idx, sc in enumerate(data.get("scenes", [])):
        shots.append({
            "index": idx,
            "role": sc.get("role", ""),
            "voiceover": sc.get("voiceover", ""),
            "captions": sc.get("captions", []),
            "duration": sc.get("duration"),
            "image": f"{asset_stem}_s{idx:02d}.png",
        })
    return {
        "concept": data.get("concept", ""),
        "elevator": data.get("elevator", ""),
        "shots": shots,
        "note": "交给 mmx-cli：按 shots 顺序为每镜 voiceover 生成 TTS，与 image 对齐、按 duration 排时长后合成。",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="scene script JSON → 分镜表 + mmx 手卡。")
    ap.add_argument("scene", help="scene script JSON 路径")
    ap.add_argument("--outdir", default=None, help="输出目录（默认与输入同目录）")
    args = ap.parse_args()

    scene_path = Path(args.scene).resolve()
    if not scene_path.exists():
        sys.exit(f"✗ scene JSON 不存在：{scene_path}")
    data = json.loads(scene_path.read_text(encoding="utf-8"))
    outdir = Path(args.outdir).resolve() if args.outdir else scene_path.parent
    stem = scene_path.stem.replace(".scene", "")

    md_path = outdir / f"{stem}.storyboard.md"
    mmx_path = outdir / f"{stem}.mmx.json"
    md_path.write_text(build_markdown(data, stem), encoding="utf-8")
    mmx_path.write_text(json.dumps(build_mmx(data, stem), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 分镜表 {md_path}")
    print(f"✓ mmx 手卡 {mmx_path}  （{len(data.get('scenes', []))} 镜）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
