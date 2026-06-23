# examples —— 三个范例 = 三种概念形状

故意各取一种"形状"，证明拆解是概念驱动、不是一个模板套到底：

- **hbm**（「架构 / 性能跃迁」型）——走完全部六个组件，是组件库的完整跑通。详见 [`hbm.md`](hbm.md)。
- **glass_substrate**（「材料 / 工艺替代」型）——**不用** stack_layers，还把 concept_card 复用来单讲"卡点"，体现按概念裁剪镜序。
- **industry_increment**（「价值流 / 产业链增量」型）——只四镜，"是什么 / 机理"压成一句背景，把锚点(增量来源 + 弹性排序)放大为主体。

## 渲染

```bash
# 图集（自包含 HTML，内联 conceptkit/motionkit + GSAP）
python scripts/render_gallery.py references/examples/<name>.scene.json -o <name>.html

# 视频分镜表 + 交给 mmx-cli 的手卡
python scripts/build_storyboard.py references/examples/<name>.scene.json

# 把单镜动画导成 MP4（需 playwright + ffmpeg；scene HTML 须暴露 window.__tl）
python scripts/capture_video.py references/examples/hbm_scene.html -o hbm.mp4
```

> 例稿里的数字取公认量级 / 示意，仅用于演示框架与组件；正式产出必须联网核验事实并标注出处(见 `../archetypes.md` 第三步取证纪律)。
