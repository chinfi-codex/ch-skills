# AI 日报 Enrichment 子方法

## 目标

Enrichment 用来补足基础 evidence packet 无法提供的上下文，例如 GitHub README、repo 元数据、Product Hunt 官网定位、HN/RSS 外链正文片段。它不是分析结论，也不是日报本身。

Agent 先阅读基础 evidence packet，再用本文件判断哪些对象值得二次加工；脚本只执行 Agent 明确给出的 target。

## 什么时候需要 enrichment

优先 enrich 这些对象：

- GitHub Trending 中与 AI Agent、模型调用、开发者工具、推理基础设施、数据/评测/部署链路直接相关的项目。
- Product Hunt 中 tagline 或 topic 显示为 AI 原生产品，且 votes、comments、daily rank 或 maker 背景显示有足够关注度的产品。
- Hacker News 中 score/comments 较高，且外链不是纯观点短文，而是项目、产品、论文、技术文档或重要公司公告的条目。
- RSS 中来自 AI/技术信源、标题指向模型、算力、Agent、企业采用、政策监管、融资并购、开源生态变化的条目。
- 同一个对象跨 GitHub/Product Hunt/HN/RSS 多源出现，或与金十/财联社的产业快讯互相印证。

可以跳过这些对象：

- 只有泛科技、宏观、股价、营销软文信号，和 AI 日报主题关系弱。
- 已经在基础 item 中给出足够事实，不需要 README/官网/外链补充。
- HN 外链只是讨论页、社交媒体、无正文页面，或无法形成可核查上下文。
- Product Hunt 产品明显只是传统 SaaS 加一层 AI 文案，但没有目标用户、API、pricing、docs 或 enterprise 线索。
- GitHub repo 只是教程合集、榜单、个人配置、泛安全/运维资料，除非当天日报需要说明开发者生态外围信号。

## 选择 target 的方法

从 `prepare_report_data.py` 输出的 `enrichment_candidates` 中挑选。每个被选 target 至少要满足一个理由：

- `核心性`：它直接影响今天日报的一条主判断。
- `不确定性`：基础 item 说不清它是什么，需要 README/官网/正文验证。
- `交叉验证`：它能帮助确认另一个来源中的同一主题。
- `代表性`：它是当天某类趋势中最能说明问题的样本。

默认控制在 5-15 个 target。日报很短或证据很清楚时可以少于 5 个；深度复盘或重大事件日可以更多，但要避免机械抓全量。

## 执行契约

Agent 选择 target 后，把 target 对象交给 `scripts/enrich_targets.py`。每个 target 必须包含：

- `item_id`：来自基础 evidence packet 的 item id。
- `target_type`：`github_repo`、`product_website` 或 `article_url`。
- `target_url`：要抓取的绝对 URL。
- `source_type`、`title`、`reason` 可选，但推荐保留，方便审计。

脚本会把结果写入 `enrichments` 表。同一 `item_id + target_type + target_url` 会更新已有记录，不会制造重复行。

## 解读方法

GitHub 项目重点看：

- stars/forks/language/topics/license/pushed_at/open_issues_count。
- latest release、homepage、语言占比。
- README 中的目标、安装、使用、架构、示例、API、deploy 片段。

Product Hunt 产品重点看：

- 基础 item 的 votes、comments、daily_rank、tagline、description、topics、makers。
- 官网的 H1/H2、CTA、pricing/about/docs/API/enterprise 链接。
- 它是 AI 原生产品，还是传统 SaaS 加 AI 文案。

HN/RSS 外链重点看：

- 外链到底是项目、产品、论文、教程、公司公告还是观点文章。
- 讨论热度代表关注度，不等于采用度。
- 若外链指向 GitHub repo，应按 GitHub 项目解读。

## 使用原则

- Enrichment 缺失时不要补脑，只说明证据缺口。
- README 和官网文案可能有营销倾向，不能直接当作采用证据。
- 同一对象跨来源出现时可以提高关注权重，但仍需区分“关注度”“可用性”和“商业化”。
- 最终日报只引用对判断有贡献的 enrichment，不要把抓到的材料机械罗列。
