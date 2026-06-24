# 技术选型与交付

## 选型(规范决定需求，逐条满足)

- **2D 图解 + 伪 3D** → SVG(等距 / 2.5D) + CSS 3D transforms。SVG 是全仓通用语(chartkit / conceptkit)，零迁移。
- **动效编排** → GSAP 时间轴。要它不是为炫：命名时间轴、嵌套(scene→shot)、`seek(t)` 定点、可逆 / yoyo。**`seek` 定点是关键——它决定同一条动画能确定性逐帧导出成视频**，纯 CSS 动画做不到。轻量备选 anime.js。
- **真 3D** → **不进 scope**(已决定)。必须旋转观察才能讲清的概念，用伪 3D 近似，或拆成多个视角的静态镜。
- **依赖** → 默认图文网页把 GSAP **内联**(`assets/vendor/gsap.min.js`)，每镜 SVG 在**构建时服务端渲染**进静态 HTML(conceptkit 经 Node 跑 `kit/conceptkit_ssr.cjs`)，所以**无 JS / GSAP 失效图仍可见**，浏览器端只内联 GSAP + motionkit 做动画。**构建期需 Node**(仅 custom 手绘镜可免)；查看端零外部 CDN(字体除外)。正文排版复用共享 `html_report`(经 `skill-sync.yaml` 的 html bundle 同步进 `scripts/_shared/html_report/`)。

## 两条交付链路

- **默认 · 图文动画网页**：`scripts/render_explainer.py` 把【文字稿(Markdown) + 分镜(JSON)】套成一份 **claude 主题**的单页 HTML——正文按理解顺序成文，每镜画面(conceptkit 组件或 custom 手绘 SVG)**构建时即渲成静态 SVG**、插到对应小节标题(`anchor`)之后；GSAP + motionkit 只做**渐进增强**——靠 IntersectionObserver 只在镜进入视口时建时间轴并播放，离屏 / 标签页隐藏自动暂停。这是默认交付。
  ```bash
  python3 scripts/render_explainer.py --article 文字稿.md --scenes 分镜.scene.json -o out.html
  ```
- **按需 · 视频**(仅用户明确要时)：scene HTML 暴露 `window.__tl`(有限可 seek)→ `scripts/capture_video.py` 用 Playwright 逐帧 seek + 截图 → ffmpeg 合 MP4；或 `scripts/build_storyboard.py` 出分镜表 + 画面交 `mmx-cli` 出片配音。
- **静态降级**：默认网页的每镜 SVG 是服务端渲染的，**无 JS 也直接可见**(静态末帧)；reduced-motion 不播放、只保留静态图。`scripts/render_gallery.py` 仍可单出纯图集 HTML(同样视口感知播放)。

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

- `scripts/kit/conceptkit_ssr.cjs` —— Node 侧服务端渲染器：执行同一份 conceptkit.js，把组件渲成静态 `{viewBox, svg, beats}`，供 `render_explainer.py` 构建期调用(默认网页因此不向浏览器发送 conceptkit，只发 motionkit + GSAP)。

`render_explainer.py`(服务端渲染图 + 内联 GSAP/motionkit) / `render_gallery.py`(浏览器端渲图 + GSAP) 把动效组件随 skill 目录同步、不依赖 shared bundle；正文排版另走 `html_report` 共享框架——已在 `skill-sync.yaml` 的 html bundle 里登记 `ch-tech-explainer`，同步进 `scripts/_shared/html_report/`，开发时回落仓库 `shared/`。
