# 范例：HBM（「架构 / 性能跃迁」型）

HBM 的"新"在**结构**(把 DRAM 叠起来、垂直打通)，不是材料——所以诊断为「架构 / 性能跃迁」型，骨架是"先立性能墙 → 结构创新破墙 → 量化对比 → 产业链"。

- 机器稿：[`hbm.scene.json`](hbm.scene.json)
- 可运行单镜(手绘 GSAP，含 `window.__tl` 导出契约)：[`hbm_scene.html`](hbm_scene.html)
- 图集渲染：`python scripts/render_gallery.py references/examples/hbm.scene.json -o hbm.html`

## 六镜怎么对应"诊断三步 + 五条投研锚点"

| 镜 | role | 组件 | 它在做什么 |
|---|---|---|---|
| 1 | 是什么 | concept_card | 铺认知(类比)，不算锚点 |
| 2 | 为什么是现在 | breakwall | 锚点①痛点：内存墙 |
| 3 | 怎么做到的 | stack_layers | 讲机理，为后面的锚点铺垫 |
| 4 | 凭什么更好 | compare_bars | 锚点②增量：带宽 vs GDDR |
| 5 | 谁吃到肉 | supply_chain | 锚点④谁受益 + 弹性排序 |
| 6 | 怎么追踪 | timeline | 锚点⑤节奏与证伪信号 |

> 注意锚点不是和镜一一对应：**锚点③卡点 / 壁垒**在 HBM 这例里并进了机理与产业链口播(先进封装是瓶颈)；到了玻璃基板那例，卡点重到要单独开一镜「难在玻璃上钻孔不裂」——同一套锚点，按概念决定要不要给独立镜。这就是"具体问题具体分析"。

## 一镜动效拆解（第 3 镜 stack_layers）

beats 由模型写、对应 `motion_spec` 六类语法，GSAP 只负责插值：

- `die[]` **introduce**（stagger 0.4）——逐层落上基片，演示"叠"这个结构。
- `tsv` **reveal**——两根直梯画通，演示"垂直打通"。
- `chan` **introduce**——GPU 旁的数据通道点亮。
- `flow` **flow**（循环）——粒子沿通道流动，演示"喂数据"。
- `bar-fill` **contrast**——带宽条拉满，量化"凭什么更好"。

## 出图集 / 分镜 / 视频

```bash
python scripts/render_gallery.py   references/examples/hbm.scene.json -o hbm.html
python scripts/build_storyboard.py references/examples/hbm.scene.json
python scripts/capture_video.py    references/examples/hbm_scene.html -o hbm.mp4   # 需 playwright + ffmpeg
```
