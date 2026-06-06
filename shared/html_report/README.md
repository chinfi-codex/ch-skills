# shared/html_report

仓库级 HTML 报告渲染框架。各报告型 skill 把 Markdown 研报 + JSON 证据包套成一份自包含的单页 HTML(图表不依赖外部 CDN)。

设计目标:**新 skill 出 HTML 只写一张"薄清单"**——怎么读自己的数据、画哪几张图——其余(命令行、Markdown→HTML、主题、校验、图表工具、染色/hero 装饰)全部由本包提供。

## 一张薄清单长什么样

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "_shared"))

from html_report import (
    HtmlReportBuilder, ChartHook, RenderJob,
    render_report, PillDecoration, HeroDecoration,
)

def add_arguments(parser):                 # 可选:本 skill 额外的命令行参数
    parser.add_argument("--evidence", default=None)

def build_job(args) -> RenderJob:          # 唯一要写的:读数据 + 选图表
    md = Path(args.input).read_text(encoding="utf-8")
    evidence = load_my_evidence(args.evidence)        # 域:自己的 evidence schema
    charts = extract_my_payload(evidence)             # 域:整形成 JS 好用的 payload
    builder = HtmlReportBuilder(title=args.title or Path(args.input).stem,
                                theme=args.theme, meta_text="…")
    builder.add_decoration(PillDecoration(MY_PILL_RULES))   # 词表是数据
    builder.add_decoration(HeroDecoration(heading_prefix="核心判断"))
    builder.add_chart_hook(ChartHook(name="my-charts", payload=charts, js=MY_CHARTS_JS))
    out = Path(args.output) if args.output else Path(args.input).with_suffix(".html")
    return RenderJob(markdown_text=md, builder=builder, output_path=out,
                     summary={"…": "打印到 stdout 的诊断字段"})

if __name__ == "__main__":
    raise SystemExit(render_report(
        description="Render … to static HTML.",
        build_job=build_job, add_arguments=add_arguments))
```

`render_report` 统一负责:解析 `--input/--output/--title/--theme/--no-validate/--strict`、读 Markdown、跑 `build_job`、以"校验失败转 warning 不阻断"的策略渲染(`--strict` 才硬失败)、写文件、打印 JSON 摘要、把异常收成 `error: …` + 退出码 1。

## 四层结构

| 层 | 模块 | 职责 |
|---|---|---|
| CLI | `cli.py` | `render_report` / `RenderJob`,吃掉每个 skill 重复的 argparse + main 编排 |
| 装饰 | `decorations.py` | `PillDecoration` / `HeroDecoration`,数据驱动,机制只一份 |
| 图表 | `chartkit.js` (`window.CK`) + `ChartHook` | 共享 SVG/DOM 工具 + 各 skill 自带的画图 JS |
| 外壳 | `builder.py` `markdown_engine.py` `text_validator.py` `themes/` | HTML 骨架、Markdown→HTML、文本保全校验、CSS 主题 |

## 装饰:换词表不换代码

```python
PillDecoration(rules=[(r"^(成长股|成熟龙头)$", "pill"),
                      (r"^(强|高)$", "pill neg")])          # 表格单元格按正则染成 pill
HeroDecoration(heading_prefix="一句话盘面判断",
               collect_tags=("P",), max_blocks=3,
               stop_at_numbered=True, number_units="%|pct|倍",
               keyword_pattern="上证|创业板|科创50")          # 把摘要标题升格成 hero 卡
```

裸 JS 逃生舱仍在:`builder.add_ui_decoration(js_string)`。

## 图表工具 `window.CK`

builder 在有 ChartHook 时,自动在 base-UI 脚本之后、各 hook IIFE 之前注入一次 `chartkit.js`,暴露 `window.CK`。hook JS 不再各自重定义 `svgEl`,直接用:

- `CK.svgEl(name, attrs)` / `CK.svgText(x, y, text, anchor, color, size)`
- `CK.fmt.date(v)` / `CK.fmt.num(v, d)` / `CK.fmt.signedPct(v)`
- `CK.tooltip(card)` / `CK.moveTip(tip, card, e)`
- `CK.card(cls, title, sub)` / `CK.legend([[label, color], …])` / `CK.grid(cls)`
- `CK.findHeading(root, texts[, sel])` / `CK.findNextTable(heading)`

skill 独有的格式化(价格精度、亿/万亿 除数)仍留在各自 hook 里,因为单位本就不同。

### ChartHook JS 协议

每个 hook 的 JS 在专属 IIFE 中执行,渲染器自动注入:

- `__payload` — 当前 hook 的 payload(已经 JSON.parse)
- `window.__chartData` — 整个 envelope `{ hookName: payload, … }`,供跨 hook 取数

## 主题

- `default`:AlphaVault 站点风格(Google Material)——冷白底 + Google 蓝 + 红绿涨跌,默认值。CSS 与 AlphaVault Site 的 `siteCss()` 同源(Google Sans、g-palette、Material 卡片/阴影、4 色 logo 点)
- `claude`:Claude.ai 暖色风格——奶油底 + 黏土橙(clay)强调 + 衬线(serif)标题,编辑器质感
- `print`:黑白衬线、A4 友好,适合导出 PDF 或邮件附件

新增主题:在 `themes/` 下放 `<name>.css`,`list_themes()` 自动发现。

## 同步路径

源码在 `shared/html_report/`。通过 `skill-sync.yaml` 的 `shared.bundles` 同步到目标 skill 的 `scripts/_shared/html_report/`(纯渲染型 skill),或随整个 `shared/` 进 `scripts/_shared/`(同时需要 `db_core` 的 skill)。各 skill 用文件头的 `sys.path` 片段引导导入:

```python
_BUNDLED = Path(__file__).resolve().parent / "_shared"
_DEV = Path(__file__).resolve().parents[2] / "shared"
sys.path.insert(0, str(_BUNDLED if _BUNDLED.exists() else _DEV))   # 装包后用 _shared,开发时回落仓库 shared/
```
