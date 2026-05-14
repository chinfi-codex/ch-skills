---
name: ch-news-reporter
description: 当用户要求从金十、财联社、GitHub Trending、Product Hunt、Hacker News、RSS 等多信源采集财经、AI、地缘冲突或伊朗动态新闻，建立统一新闻数据表，按 report profile 生成 evidence packet，并输出 AI 日报、财经研究简报、热门产品观察、市场信号复盘、伊朗/中东局势动态或风险资产影响分析时，必须使用此 skill。适用于“今天 AI 有什么重要新闻”“结合 PH/HN/GitHub Trending 写日报”“抓取新闻库做研究”“生成伊朗动态播报”“分析霍尔木兹/油价/黄金受局势影响”等多步骤任务；不用于单条网页摘要、普通聊天式新闻问答、股票实时交易建议或不需要本地数据采集的简单搜索。
---

# CH News Reporter

## 目标

从用户配置的财经、AI 与地缘风险信源采集当天信息，清洗为统一 SQLite 新闻库；在报告时间点按 profile 生成 evidence packet，并可对关键 GitHub 项目、Product Hunt 产品和外链做二次加工；最后由 Agent 基于方法论、模板和证据包输出中文报告。

本 skill 不提供买卖建议，不把单一快讯当作结论，不在采集阶段过早过滤“财经/AI/地缘”边界。它面向需要日常跟踪市场、AI 产业、开源生态、政策、地缘冲突和风险资产传导的主动研究者。

## 适用场景与边界

使用本 skill 的典型场景：

- 用户要抓取或刷新“今天”的财经与 AI 新闻库。
- 用户要把金十、财联社、GitHub Trending、RSS 汇总成统一数据表。
- 用户要基于本地新闻库回答研究问题，如 AI Agent、算力、宏观风险、流动性、政策、产业链变化。
- 用户要输出研究简报、日报素材、主题观察、市场信号复盘。
- 用户要输出伊朗/中东局势动态、停火窗口跟踪、霍尔木兹航运、油气价格和风险资产影响分析。

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

### 2. 按报告 profile 准备基础证据包

日报、动态跟踪等固定报告优先走 profile 流程。脚本只检索、排序、截断，并输出可二次加工的候选对象池；是否 enrichment 由 Agent 读取日报子方法后判断。

```bash
python scripts/prepare_report_data.py --profile ai_daily --date today --format json
```

常用参数：

- `--profile ai_daily|iran_dynamic`：报告画像；信源配置在 `config/report_profiles.yaml`。
- `--include-enrichments`：把 `enrichments` 表里的二次加工结果拼回证据包。
- `--format json|markdown`：JSON 适合 Agent 继续处理，Markdown 适合人工核查。

### 3. 执行二次加工

当日报需要 enrichment 时，先读取对应子方法，例如 AI 日报读取：

- `references/reports/ai_daily/enrichment.md`

Agent 从 evidence packet 的 `enrichment_candidates` 中挑选目标，再把明确 target 交给脚本执行。脚本只做抓取和写库，不负责判断哪些对象值得抓。

```powershell
$json = '{"item_id":"...","target_type":"github_repo","target_url":"https://github.com/owner/repo"}'
$json | python scripts/enrich_targets.py --targets-file -
```

批量执行时，把 target 列表写成 JSON 后传入：

```bash
python scripts/enrich_targets.py --targets-file selected_targets.json
```

二次加工层只做确定性补充：

- GitHub repo：README、repo metadata、语言占比、latest release、license、homepage 等。
- Product Hunt：官网或产品页 title/meta/H1/H2/CTA/pricing/about/docs/API 等链接与正文片段。
- Hacker News / RSS 外链：score/rank 等基础字段来自 `items.metadata_json`，脚本补充外链页面 title/meta/正文片段；若外链是 GitHub repo，Agent 应选择 `github_repo` target_type。

重复运行不会让数据膨胀；同一 `item_id + target_type + target_url` 会更新已有 enrichment。

完成 enrichment 后，再生成 enriched evidence packet：

```bash
python scripts/prepare_report_data.py --profile ai_daily --date today --include-enrichments --format markdown
```

### 4. 检索临时主题证据

根据用户问题先构建 2-5 组关键词，再查询新闻库。

```bash
python scripts/query_news.py --date today --q "AI Agent 算力 融资" --limit 50 --format markdown
```

常用参数：

- `--q`：FTS 检索词；为空时按时间返回。
- `--source-type cls|jin10|github_trending|rss|product_hunt|hacker_news`：限制来源类型。
- `--format markdown|json|csv`：输出格式。
- `--date today|YYYY-MM-DD`：限制日期。

这个入口用于临时研究问题；固定日报优先使用 `prepare_report_data.py`。

### 5. 研究分析

读取通用 `references/research_methodology.md`，或按报告 profile 读取：

- `references/reports/ai_daily/methodology.md`
- `references/reports/ai_daily/enrichment.md`：AI 日报 enrichment 子方法，包含 target 选择契约和解读方法。
- `references/reports/iran_dynamic/methodology.md`
- `references/reports/iran_dynamic/frame_ceasefire.md`
- `references/reports/iran_dynamic/economic_impact_framework.md`

根据问题选择财经、AI、产品观察、开源生态或地缘风险框架。`iran_dynamic` 必须优先读取 `methodology.md`，再按需要加载停火框架和经济影响框架。

分析时必须区分：

- **确认事实**：多源一致、原文明确、时间可追溯。
- **市场/产业信号**：价格、政策、融资、产品发布、开源热度、供需关系等可观察变化。
- **模型推断**：基于证据链的判断，必须说明不确定性和可能反例。

`iran_dynamic` 还必须区分：

- **停火合规事实**：直接军事行动、代理人行动、核活动、航运/扫雷进展。
- **行为体压力变量**：美国、以色列、伊朗、代理人网络各自是否向打破僵局或续谈方向移动。
- **市场定价信号**：Brent、天然气、黄金、美元、航运保险、通胀预期等可观察反应。
- **情景推演**：停火续期/恢复交战/僵尸化延续等概率变化，概率调整必须回到当日证据。

### 6. 输出报告

读取通用 `references/report_template.md`，或按报告 profile 读取：

- `references/reports/ai_daily/template.md`
- `references/reports/iran_dynamic/template.md`

默认输出中文研究简报。关键证据使用：

```text
时间 - 标题 [来源]
```

报告不应只罗列新闻，要把证据转化为观点：发生了什么、为何重要、影响哪些资产/产业/公司类型、后续观察什么。

`iran_dynamic` 报告必须把新闻证据写成 `时间 - 新闻 [来源]`，并包含停火阶段、战争烈度、核心事实变化、以色列独立意志监测、航运/能源变量、到期推演和今日结论。

## 数据表说明

采集脚本写入 SQLite：

- `items`：统一新闻表。
- `items_fts`：FTS5 全文索引。
- `enrichments`：可选二次加工表，由 `enrich_targets.py` 写入，不改变原始 `items`。

核心字段：

- `date_key`：Asia/Shanghai 日期，格式 `YYYY-MM-DD`。
- `source_type`：`cls`、`jin10`、`github_trending`、`rss`、`product_hunt`、`hacker_news`。
- `source_name`：具体来源名称。
- `published_at` / `fetched_at`：发布时间与抓取时间。
- `title` / `content` / `url`：主要文本与链接。
- `tags_json` / `metadata_json` / `raw_json`：结构化补充信息。

采集使用稳定哈希去重。重复运行同一天采集不会重复插入同一条记录。

`enrichments` 核心字段：

- `item_id`：对应 `items.id`。
- `target_type`：`github_repo`、`product_website` 或 `article_url`。
- `target_url`：被二次抓取的 URL。
- `status` / `fetched_at`：抓取状态与更新时间。
- `title` / `text_excerpt`：页面标题与 README/网页正文片段。
- `metadata_json` / `raw_json`：结构化详情与原始补充数据。

二次加工层可以抓 README、官网 HTML、meta、标题和正文片段，可以做去重、排序、字段抽取和截断；不判断产品价值、不生成结论、不拼完整报告。

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
python scripts/prepare_report_data.py --profile ai_daily --date today --format json
# Agent 读取 references/reports/ai_daily/enrichment.md 后，从 enrichment_candidates 中挑选 targets。
python scripts/enrich_targets.py --targets-file selected_targets.json
python scripts/prepare_report_data.py --profile ai_daily --date today --include-enrichments --format markdown
```

输出应包含：

- 一句话结论。
- 关键新闻与 GitHub Trending 项目证据。
- 产品/开源/基础设施/资本市场四类信号。
- 哪些只是热度，哪些可能转化为产业趋势。
- 未来 24-72 小时继续观察的关键词。

### 伊朗动态播报

用户：

```text
生成今天的伊朗动态播报，重点看霍尔木兹、油价和以色列独立行动风险。
```

执行：

```bash
python scripts/collect_news.py --date today --source all --db data/news_research.sqlite
python scripts/prepare_report_data.py --profile iran_dynamic --date today --format markdown
```

加载：

- `references/reports/iran_dynamic/methodology.md`
- `references/reports/iran_dynamic/frame_ceasefire.md`
- `references/reports/iran_dynamic/economic_impact_framework.md`
- `references/reports/iran_dynamic/template.md`

输出应包含：

- 停火第 N 天 / 剩余 X 天 / 当前阶段。
- 战争烈度级别与趋势箭头。
- 违约/升级、合规/缓和、谈判动态三类核心事实变化。
- 美国、以色列、伊朗、代理人网络与世界行动。
- 霍尔木兹航运、油气与风险资产传导。
- 情景 A/B/C 概率和今日结论。
