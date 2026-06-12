# AI 日报 Enrichment 子方法

## 目标

Enrichment 用来补足基础 evidence packet 无法提供的上下文，例如 GitHub README、repo 元数据、Product Hunt 官网定位、HN/RSS 外链正文片段。它不是分析结论，也不是日报本身。

Agent 先阅读基础 evidence packet，再用本文件判断哪些对象值得二次加工；脚本只执行 Agent 明确给出的 target。

## 什么时候需要 enrichment

按双引擎结构优先 enrich 这两类对象：

**引擎 A 优先（新项目发现）：**

- GitHub Trending 中与 AI Agent、模型调用、开发者工具、推理基础设施、数据/评测/部署链路直接相关的项目。
- Product Hunt 中 tagline 或 topic 显示为 AI 原生产品，且 votes、comments、daily rank 或 maker 背景显示有足够关注度的产品。
- Hacker News（尤其是 Show HN）中 score/comments 较高，且外链是项目、产品、论文、技术文档或重要公司公告而非纯观点短文。

**引擎 B 优先（重点公司动作）：**

- 重点公司官方 RSS（OpenAI / Anthropic / Google AI / DeepMind / Meta AI / Mistral / HuggingFace / NVIDIA / Microsoft AI 等）中含模型版本号、产品功能名、API 端点、定价数值、融资金额或合作主体的条目。
- 第三方媒体 RSS（The Decoder、Juya AI Daily、AI Hot 等）中转述重点公司动态的条目，enrich 以核对外链与原始公告。
- 金十快讯中提到 OpenAI / Anthropic / Google / Meta / Kimi / 智谱 / Minimax / 腾讯 / 阿里 / 字节 / DeepSeek / 百度 等重点厂商的条目 —— 这是中国厂商动态主要的可观察入口，enrich 外链页面以拿到原始信息。

**跨引擎交叉验证优先：**

- 同一对象 / 主题跨 GitHub / Product Hunt / HN / RSS / 快讯多源出现 → 提权。
- 某重点公司（引擎 B）的策略动作和当日开源 / Show HN（引擎 A）涌现的同方向项目互相印证 → 提权。

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

重点公司 RSS / 官方公告外链重点看（引擎 B 解读契约）：

- 模型发布：是否有版本号、参数规模、上下文长度、许可证类型（闭源 API / 开源权重 / 受限商用）、benchmark 数据。
- 产品更新：面向哪类用户（C 端 / 开发者 / 企业 / 政府）、功能是新增还是替换、是否有可访问的产品入口。
- API & 定价：旧价 vs 新价、起始日期、是否仅限特定地区或客户层级。
- 融资 & 政策：金额、轮次、领投方、关联估值、政策类型与生效时间。
- 第三方媒体转述 vs 厂商官方公告：标注证据等级。仅有第三方报道、未见官方页面时，必须降级表述（"据 The Decoder / 金十 报道，待官方公告确认"）。
- 中国厂商：如果原文是社交平台 / 媒体转述、缺乏官方页面，明确写"无可核实更新"，不要补脑。

## 使用原则

- Enrichment 缺失时不要补脑，只说明证据缺口。
- README 和官网文案可能有营销倾向，不能直接当作采用证据。
- 同一对象跨来源出现时可以提高关注权重，但仍需区分“关注度”“可用性”和“商业化”。
- 最终日报只引用对判断有贡献的 enrichment，不要把抓到的材料机械罗列。
