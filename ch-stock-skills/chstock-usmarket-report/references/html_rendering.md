---
name: chstock-usmarket-report-html-rendering
description: 仅供 chstock-usmarket-report skill 内部按需读取。说明美股日报 HTML 派生层的渲染规则与图表约定。
---

# HTML 渲染规则

## 触发条件

只在用户要求 HTML / 可浏览页面 / Site 派生层 / 本地归档时生成。普通日报只交 Markdown。

## 基本原则

- HTML 必须从已定稿 Markdown 渲染，不让 renderer 代写正文，也不作为 Wiki 真相源。
- 带 YAML frontmatter 默认剥离后渲染。
- `==...==` 渲染为浅蓝提示块。
- 图表只读 evidence 已有的价格 / 成交额 / 位置字段；催化仍以正文联网检索（Tavily/WebSearch）来源为准。

## 热力墙

观察池在 HTML 里以一块**分组热力墙**呈现：

- 整池一格、按分组分带。
- 每格 = 代码 + 当日涨幅，绿涨红跌、色深 = 幅度。
- `＋` 标跑赢 QQQ，`★` 标 ±7% 异动。
- 悬停 tooltip 给：收盘 / vs-QQQ / 5日 / 52周位置 / 量比 + 信号箭头。

墙已承载逐股明细，故渲染器**在 HTML 里自动删掉墙下面的各组明细表**（每组的文字点评保留）。**Markdown 正文的明细表不动**——它仍是真相源，HTML 只清浏览层冗余。

## 分组 ETF 指数对比图

热力墙正下方接一张**分组 ETF 指数对比图**：

- 把各非空分组当成等权 ETF 指数（成员等权、日频再平衡、全部 rebase 到 100、与 QQQ 同窗）。
- 7 组左右 + QQQ 基准（虚线）叠在一张多线走势图里看相对强弱。
- 右端按区间收益高低排标签，悬停看某日各组指数值与涨幅。
- 数据全取自 evidence 的 `group_indices`（确定性派生，模型不参与）。
- 此图**取代**了旧版「观察池 vs QQQ（当日超额）」条形图。

## 大盘 K 线图

「大盘」小节标题下接一行两列的 120 日 K 线（OHLC + MA20/MA60 + 成交金额柱）：

- 左 QQQ、右 SOXX，两张同窗同口径，用来对照纳指整体与半导体这条先行链的位置差。
- 卡片标题是 `代码 · 简称`，副标题给区间、交易日数与当日涨跌幅；悬停看单日 OHLC / 涨跌 / 成交额。
- 只有一条指数有 K 线数据时（例如旧的 evidence 只存了 QQQ），自动退回单张宽图，不留半幅空位。
- 数据全取自 evidence `indices[].kline_records`；SOXX 只入图，不参与 vs-QQQ、分组指数基准等相对强弱计算。

## 渲染命令

```bash
python scripts/render_report_html.py --input reports/us-2026-06-18.md --evidence outputs/us-2026-06-18.json
python scripts/render_report_html.py --input reports/us-2026-06-18.md --theme print
```
