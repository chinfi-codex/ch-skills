---
name: 互动问答
version: 0.1.0
description: 查询上市公司互动问答（P5W/互动易）内容，支持按关键词、公司名或股票代码检索，并输出结构化问答结果。用户提到“互动问答”“互动易”“按关键词查公司回复”“查某家公司投资者问答”“P5W 问答”“看公司怎么回复某个问题”等场景时，应优先使用此 skill。默认过滤“股东人数/股东户数”类低信息密度问答，除非用户明确要求保留。
trigger_patterns:
  - "查互动问答"
  - "按关键词查互动易"
  - "查 {company} 的互动问答"
  - "查 {company} 对 {keyword} 的回复"
  - "P5W 问答检索"
  - "互动易关键词搜索"
compatibility:
  tools: ["shell"]
  os: ["Windows", "macOS", "Linux"]
---

# 互动问答

用于检索上市公司在 P5W 互动平台上的投资者问答，支持：

- 按关键词搜索问答
- 按公司名或股票代码过滤
- 按日期区间过滤
- 默认排除“股东人数/户数”类问答噪声

## 适用场景

- 用户要查某家公司最近回复过哪些问题
- 用户要看某个主题词在互动平台上的问答记录
- 用户想同时限定“公司 + 关键词”
- 用户明确说“不要股东人数这类问题”

## 命令语法

按关键词搜索：

```bash
cd interactive-qa
python scripts/interactive_qa_search.py --keyword 算力
```

按公司搜索：

```bash
cd interactive-qa
python scripts/interactive_qa_search.py --company 中际旭创
```

按公司 + 关键词搜索：

```bash
cd interactive-qa
python scripts/interactive_qa_search.py --company 中际旭创 --keyword cpo
```

限定日期区间：

```bash
cd interactive-qa
python scripts/interactive_qa_search.py --company 中际旭创 --date-from 2026-03-01 --date-to 2026-03-28
```

保留股东人数/户数类问题：

```bash
cd interactive-qa
python scripts/interactive_qa_search.py --company 中际旭创 --include-shareholder-count
```

写入 JSON 文件：

```bash
cd interactive-qa
python scripts/interactive_qa_search.py --company 中际旭创 --keyword cpo --output data/zjxc_cpo.json
```

## 参数说明

- `--keyword`: 问题关键词，可选
- `--company`: 公司名或 6 位股票代码，可选
- `--date-from`: 起始日期，格式 `YYYY-MM-DD`
- `--date-to`: 结束日期，格式 `YYYY-MM-DD`
- `--limit`: 返回条数上限，默认 `20`
- `--max-pages`: 最多抓取页数，默认 `30`
- `--include-shareholder-count`: 保留“股东人数/户数”类问题
- `--output`: 输出 JSON 文件路径

至少提供 `--keyword` 或 `--company` 其中之一。

## 输出格式

脚本输出 JSON，对象包含：

- `query`: 本次查询参数
- `total`: 过滤后结果总数
- `items`: 问答列表

每条问答包含：

- `pid`: 问答唯一 ID
- `company_code`: 股票代码
- `company_name`: 公司简称
- `question`: 投资者问题
- `reply`: 公司回复
- `event_time`: 回复时间
- `event_date`: 回复日期
- `url`: 平台入口地址
- `excluded_reason`: 若被规则标记则说明原因

## 默认过滤规则

默认排除以下低信息密度问法：

- 股东人数
- 股东户数
- 最新股东数
- 股东总户数

如果用户明确要查这类内容，再添加 `--include-shareholder-count`。

## 注意事项

- P5W 接口本身偏向近期记录；仅按公司名检索时，本质上是在近期抓取结果中做公司过滤。
- 如果需要更高召回率，优先同时提供 `--company` 和 `--keyword`，并适当增大 `--max-pages`。
- 脚本只访问公开接口，不需要额外 API key。

## 依赖

- Python 3.9+
- `requests`

## 环境变量说明

本 skill 运行不依赖额外环境变量。
发布到 ClawHub 时如需发布权限，仍需仓库规范中的 `CLAWHUB_TOKEN`。
