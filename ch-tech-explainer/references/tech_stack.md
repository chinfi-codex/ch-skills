# 技术选型与交付

## 选型(规范决定需求，逐条满足)

- **2D 图解 + 伪 3D** → SVG(等距 / 2.5D) + CSS 3D transforms。SVG 是全仓通用语(chartkit / conceptkit)，零迁移。
- **动效编排** → GSAP 时间轴。要它不是为炫：命名时间轴、嵌套(scene→shot)、`seek(t)` 定点、可逆 / yoyo。**`seek` 定点是关键——它决定同一条动画能确定性逐帧导出成视频**，纯 CSS 动画做不到。轻量备选 anime.js。
- **真 3D** → **不进 scope**(已决定)。必须旋转观察才能讲清的概念，用伪 3D 近似，或拆成多个视角的静态镜。
- **依赖** → 经 CDN allowlist(cdnjs 等)按需加载，产物自包含 HTML。

## 两条交付链路

- **图集**：每镜渲成一个聚焦画面卡片(交互态直接用可视化组件 / `scripts/render_gallery.py` 导 HTML)。静态，是视频的合法降级。
- **视频**：scene HTML 暴露 `window.__tl`(有限可 seek)→ `scripts/capture_video.py` 用 Playwright 逐帧 seek + 截图 → ffmpeg 合 MP4；或把分镜表 + 画面交 `mmx-cli` 出 AI 视频并配音。

## capture_video.py 用法 + 依赖

scene HTML 契约：必须暴露 `window.__sceneReady === true` 与 `window.__tl`(GSAP timeline，有限时长)。

```bash
# 依赖（本仓库环境默认未装，需自行安装）
pip install playwright && python -m playwright install chromium
brew install ffmpeg            # 或 apt-get install ffmpeg

python scripts/capture_video.py references/examples/hbm_scene.html -o hbm.mp4 --fps 30
```

## 组件库位置

- `scripts/kit/conceptkit.js` —— 参数化知识图元。每个组件 `组件.svg(spec)` 出 SVG、`组件.beats(spec)` 给默认动效编排。已实现六个：concept_card / breakwall / stack_layers / compare_bars / supply_chain / timeline。
- `scripts/kit/motionkit.js` —— 把 beats 翻成 GSAP 补间(`MK.buildTimeline(gsap, root, beats)`)，并处理 reduced-motion。
- `assets/explainer.css` —— 皮肤(`.ck` 作用域 + `--ck-*` 变量，暗色自适应)。

`render_gallery.py` 把这三者内联进产物 HTML，所以组件库随 skill 目录整体同步、不依赖 shared bundle。等其它报告 skill 也要用这套动效图元时，再把 `scripts/kit/` 上提到 `shared/html_report` 并接进 `skill-sync.yaml` 的 html bundle。
