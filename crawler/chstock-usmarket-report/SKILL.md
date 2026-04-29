---
name: chstock-usmarket-report
description: 生成美股观察池日报，输出大盘概览、分组股票表现、异动扫描和板块观察。适用于“生成美股日报”“复盘昨晚美股”“看我的美股池表现”“美股观察池报告”等请求；默认日期取刚结束的前一个交易日，也支持显式指定 YYYY-MM-DD 日期。
---

# 美股观察池日报

使用 `stock_pool.yaml` 里的观察池配置，调用 Yahoo Finance chart 接口拉取日线数据，生成 Markdown 格式的美股日报。

## 适用方式

当用户希望：

- 生成默认日期的美股日报
- 指定某个交易日回看报告
- 调整观察池分组后重新出报告
- 以 JSON 方式拿到报告正文和元数据

优先使用这个 skill。

## 目录结构

```text
chstock-usmarket-report/
├── SKILL.md
├── stock_pool.yaml
├── generate_report.py
└── scripts/
    └── generate_report.py
```

其中根目录的 `generate_report.py` 只是兼容入口，真实逻辑在 `scripts/generate_report.py`。

## 使用命令

默认读取当前目录下的 `stock_pool.yaml`，并自动使用最近一个已结束的美股交易日：

```bash
python generate_report.py
```

指定日期：

```bash
python generate_report.py --date 2026-03-30
```

输出到 Markdown 文件：

```bash
python generate_report.py --output reports/us-market-report-2026-03-30.md
```

输出 JSON：

```bash
python generate_report.py --json
```

指定其它配置文件：

```bash
python generate_report.py --config custom_pool.yaml --output reports/custom-report.md
```

## 输出内容

报告默认包含：

- 大盘概览
- 目标股票池表现
- 异动扫描
- 板块观察
- 一句话总结

如果 `stock_pool.yaml` 的 `output.show_volume` 为 `true`，还会追加成交量补充。

## 配置说明

`stock_pool.yaml` 支持以下结构：

- `indices`: 大盘指数或 ETF 列表
- `groups`: 自定义观察池分组
- `thresholds`: 大涨大跌和预警阈值
- `output`: 是否展示 5 日趋势、成交量

## 依赖

```bash
pip install requests pyyaml
```

## 注意事项

- 默认日期来自最近一个可取得日线数据的交易日，而不是本地自然日。
- 该 skill 只生成基于收盘数据的结构化日报，不获取盘后涨跌幅，也不自动做深度新闻归因。
