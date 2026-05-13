---
name: market-ai-news-research
description: 当用户要求从金十、财联社、GitHub Trending、Product Hunt、Hacker News、RSS 等多信源采集财经或 AI 新闻，建立统一新闻数据表，按主题检索并生成研究简报、产业分析、热门产品观察、市场信号复盘、AI 方向观察或日报素材时，必须使用此 skill。适用于“今天 AI 有什么重要新闻”“抓取我的 RSS 并分析”“结合 PH/HN/GitHub Trending 写研究观点”“财经和 AI 新闻库检索”等多步骤任务；不用于单条网页摘要、普通聊天式新闻问答、股票实时交易建议或不需要本地数据采集的简单搜索。
---

# Market AI News Research

## 目标

从用户配置的财经与 AI 信源采集当天信息，清洗为统一 SQLite 新闻库，再基于用户问题检索证据、分析信号、输出中文研究简报。

本 skill 不提供买卖建议，不把单一快讯当作结论，不在采集阶段过早过滤“财经/AI”边界。它面向需要日常跟踪市场、AI 产业、开源生态、政策和风险偏好的主动研究者。

## 适用场景与边界

使用本 skill 的典型场景：

- 用户要抓取或刷新“今天”的财经与 AI 新闻库。
- 用户要把金十、财联社、GitHub Trending、RSS 汇总成统一数据表。
- 用户要基于本地新闻库回答研究问题，如 AI Agent、算力、宏观风险、流动性、政策、产业链变化。
- 用户要输出研究简报、日报素材、主题观察、市场信号复盘。

不要使用本 skill 的场景：

- 只总结用户粘贴的一篇文章。
- 只查询一个公开事实，不需要多信源采集。
- 用户要求实时交易指令、买卖点或确定性预测。

## 工作流程

### 1. 刷新新闻库

进入 skill 目录后运行采集脚本。默认日期为 Asia/Shanghai 的当天。

```bash
python scripts/collect_news.py --date today --db data/news_research.sqlite
```

常用参数：

- `--date today` 或 `--date YYYY-MM-DD`：采集目标日期。
- `--source all|cls|jin10|github|rss|product_hunt|hacker_news`：限制采集来源，默认 `all`。
- `--config config/sources.yaml`：RSS 配置文件。
- `--db data/news_research.sqlite`：SQLite 输出路径。
- `--limit N`：每个来源最多写入 N 条，适合调试。
- `--replace-date`：先删除目标日期内所选来源的旧数据，再重新写入；修正规则或重跑当天库时使用。

来源说明：

- 财联社电报当前接口单次最多返回约 50 条。
- 金十电报通过金十 MCP `list_flash` 接入，需要环境变量 `JIN10_AUTH_TOKEN`；脚本会尝试按 cursor 翻页，实际返回量取决于 MCP 服务。
- RSS 只写入目标日期内有明确发布时间的条目；没有发布时间的 RSS 条目默认跳过。
- GitHub Trending 会采集 daily 榜单，并补充 GitHub API 中的 repo 元数据。
- Product Hunt 使用 `PRODUCTHUNT_TOKEN` 调用官方 GraphQL API，只采集热门/高排名产品发布，保留 votes、comments、dailyRank、topics、makers 等结构化字段。
- Hacker News 只采集官方 Firebase API 的 `topstories` 和 `beststories`，不采集 `newstories`。

### 2. 检索证据

根据用户问题先构建 2-5 组关键词，再查询新闻库。

```bash
python scripts/query_news.py --date today --q "AI Agent 算力 融资" --limit 50 --format markdown
```

常用参数：

- `--q`：FTS 检索词；为空时按时间返回。
- `--source-type cls|jin10|github_trending|rss|product_hunt|hacker_news`：限制来源类型。
- `--format markdown|json|csv`：输出格式。
- `--date today|YYYY-MM-DD`：限制日期。

### 3. 研究分析

读取 `references/research_methodology.md`。根据问题选择财经、AI 或交叉研究框架。

分析时必须区分：

- **确认事实**：多源一致、原文明确、时间可追溯。
- **市场/产业信号**：价格、政策、融资、产品发布、开源热度、供需关系等可观察变化。
- **模型推断**：基于证据链的判断，必须说明不确定性和可能反例。

### 4. 输出报告

读取 `references/report_template.md`，默认输出中文研究简报。关键证据使用：

```text
时间 - 标题 [来源]
```

报告不应只罗列新闻，要把证据转化为观点：发生了什么、为何重要、影响哪些资产/产业/公司类型、后续观察什么。

## 数据表说明

采集脚本写入 SQLite：

- `items`：统一新闻表。
- `items_fts`：FTS5 全文索引。

核心字段：

- `date_key`：Asia/Shanghai 日期，格式 `YYYY-MM-DD`。
- `source_type`：`cls`、`jin10`、`github_trending`、`rss`、`product_hunt`、`hacker_news`。
- `source_name`：具体来源名称。
- `published_at` / `fetched_at`：发布时间与抓取时间。
- `title` / `content` / `url`：主要文本与链接。
- `tags_json` / `metadata_json` / `raw_json`：结构化补充信息。

采集使用稳定哈希去重。重复运行同一天采集不会重复插入同一条记录。

## 输出规范

- 默认中文，除非用户明确要求英文。
- 先给结论，再给证据，再给影响路径和观察清单。
- 每个关键判断都要能回到数据表中的新闻或项目。
- 对传闻、单源消息、未确认说法必须降级表述。
- 财经内容避免直接给交易指令；AI 内容避免把 GitHub star 变化直接等同于商业成功。
- 默认控制在 1500-2500 字；用户要求深度研究时可扩展。

## 示例

用户：

```text
根据今天的数据，帮我看 AI Agent 方向有什么新信号。
```

执行：

```bash
python scripts/collect_news.py --date today --db data/news_research.sqlite
python scripts/query_news.py --date today --q "AI Agent agents workflow browser computer use coding assistant" --limit 80 --format markdown
```

输出应包含：

- 一句话结论。
- 关键新闻与 GitHub Trending 项目证据。
- 产品/开源/基础设施/资本市场四类信号。
- 哪些只是热度，哪些可能转化为产业趋势。
- 未来 24-72 小时继续观察的关键词。
