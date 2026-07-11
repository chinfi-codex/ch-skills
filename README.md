# ch-skills — A 股研究与情报日报的 Agent Skills 矩阵

> 一套可跨 Agent 复用的 **Agent Skills 集合**，把「多源采集 → 统一数据层 → 方法论分析 → 结构化日报 / 研报 → HTML 渲染与推送」这条链路，拆成一个个职责单一、**Prompt 优先**的技能。面向每天要认真跟踪市场、AI 产业、宏观与地缘风险，并研究 A 股 / 美股的主动研究者。

<p align="center">
  <img alt="Skills" src="https://img.shields.io/badge/Agent_Skills-10-4c8bf5">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10+-3776ab?logo=python&logoColor=white">
  <img alt="PostgreSQL" src="https://img.shields.io/badge/台账-PostgreSQL-336791?logo=postgresql&logoColor=white">
  <img alt="Agents" src="https://img.shields.io/badge/多_Agent-Kimi_·_Claude_·_Codex_·_Hermes-8a63d2">
</p>

本仓库是这些 Skills 的 **canonical source(唯一可信源)**。各 Agent(Kimi CLI / Claude Code / Codex / Hermes / OpenClaw)在自己品牌目录里读到的副本，都是从这里单向同步过去的——**写一次，处处运行**。

---

## 1. News Reporter:统一采集 + 方法论分析 + 快慢思考

`ch-news-reporter` 不是「三个写死的日报」,而是一台**可配置的主题日报引擎**:你配好「吃哪些源、用什么框架组织、套什么模板」,它就长出一个带记忆、能跨天认账的日报主题。仓库自带 **AI 日报、宏观日报、地缘日报** 三个现成主题,只是同一台引擎的三个样例——照着搭,你能做出自己的。下面三小节,就是你分别能配的东西。

### 1.1 采集与信源:配「这个主题吃什么数据」

多个异构信源被收敛进**同一张 PostgreSQL 新闻表**,后续所有分析都从库里取数、而不是每次现抓。可配的是「这个主题吃什么数据」:

- **信源与过滤**:金十电报、GitHub Trending、Product Hunt、Hacker News、RSS 已经接好;RSS 订阅在 `config/sources.yaml` 里增删,每个主题吃哪些源、RSS 归哪些类、用什么关键词筛,写在 `config/report_profiles.yaml` 的 profile 里。
- **DB-first,证据可复现**:采集默认 `--only-missing`,先查库里当天缺哪个源、只补缺失的,已有的连请求都不发;同一报告日重跑拿到同一份证据、跨天结算才站得住脚,去重靠稳定哈希、同一天跑多次不插重。
- **二次加工(enrichment)按需触发**:模型判断哪些 repo / 产品 / 外链值得深挖,脚本才去抓 README、官网正文、release,结果单独入 `enrichments` 表、不污染原始新闻。

### 1.2 快思考 / 慢思考:让日报既稳定又不僵化

任何主题搭出来都自动获得这套照搬 Kahneman 双系统的运转方式,不用自己写:

| | 快思考层(System 1) | 慢思考层(System 2) |
|---|---|---|
| **频率** | 每天 | 低频、证据触发 |
| **前提** | 框架当常量,不质疑 | 框架本身是否还成立 |
| **动作** | 采集 → 证据包 → 分析 → 写报告 → 结算 watchboard | 增 / 删矢量、改档位定义、换 regime |
| **纪律** | 快、可复现、不漂移 | 慢、留痕、**人在环拍板** |
| **载体** | `watchboard`(每日活动状态台账) | `framework.md`(带 `framework_version` / `supersedes` 的版本化框架) |

快思考层每天在框架内可复现地跑,慢思考层低频、人在环地演进框架本身;两层之间的框架挑战与每日结算全部由 `save_report_state.py` 硬校验。换来的是用户最看重的三件事:**可复现、不僵化、可追溯**。


### 1.3 配一个属于你自己的主题

一个日报主题 = 下面四块拼起来,配好就是一个新主题、不用改引擎:

- **分析框架(`framework.md`,核心)**:机器区一段 YAML 定义 frame schema——这个主题有哪些维度、每个维度分几档;模型区用大白话写领域判定逻辑。维度完全由你定义,三个现成主题各用一套互不相同的 frame(能力 × 渗透两轴 / 流动性四维度 / A-B-C-D 路径),正说明这里能长出什么全看你怎么写。
- **信源与过滤(profile)**:见 1.1,决定这个主题盯哪些源、哪些关键词。
- **输出模板(`template.md`)**:报告有哪些板块、长什么样。
- **状态与治理(白拿)**:watchboard 跨天认账、快慢思考协议、HTML 渲染,配好上面三块就自动接上,不用重写。

所以「做自己的主题日报」= 加一个 profile + 写一份 `framework.md` 和 `template.md`,引擎其余部分原样复用。

---

## 2. Stock Skills:A 股 / 美股研究矩阵

**研究分析类(模型做判断,台账攒战绩):**

- **a-stock-analyzer** — 单只 A 股的基本面投研:先做**双轴分型**(生命周期 × 商业模式)选对主估值锚,而不是无脑套 PE,再叠加 A 股特有的主线归属修正和 VC 式「成长成功率」判断成长能否兑现。带 Deep 模式做命题先行的主动调研——工单全覆盖、子代理隔离、每条发现都带原文链接可追溯。
- **a-stock-daily-market-sense** — 成交额优先的盘后复盘:讲清今天谁在赚钱、主线是否延续,识别爆量下跌、全市场月线平台突破等特征分组。它还有一条慢循环——对特征分组做因子回测挖出「叠加条件最优解」,沉淀成策略画像喂给每日的策略选股,选股落台账、事后回填真实前向收益攒样本外战绩。
- **a-stock-earnings-forecast** — 全市场扫业绩预告:净利中值折算后拆出累计同比、单季同比、环比三口径,并用 cninfo 公告原文补出 Tushare 没有的扣非净利来判含金量。同时观察净利润断层的股价反应、把预告股归属到上涨主线,按主线强弱表现交叉验证出行业趋势与产业结构综述,渲染成一期一页、随披露增量更新的 HTML。
- **chstock-usmarket-report** — 昨夜美股纳斯达克科技板块复盘:以纳指100(QQQ)为唯一大盘锚,报告分观察池逐股表现、按成交额定的赚钱效应主线、池外新方向挖掘三层。异动票和新方向票都先联网核查催化(Tavily 优先)再写入,不靠训练记忆瞎猜。

**数据取证类(脚本抓原料,供下游复用):**

- **chstock-cninfo-announcement** — 按日期、类型、关键词查巨潮资讯网公告,脚本抓元数据并按标题规则打标签、过滤(问询回复 / 监管函 / 增减持 / 回购 / 股权激励等)。模型判断公告重要性、组织摘要,结果可导出 JSON 作为下游分析的证据。
- **chstock-interactive-qa** — 查上市公司互动易投资者问答,支持按公司或关键词(如 AI、算力、订单)检索公开回应,脚本自动过滤低信息密度的提问。模型负责判断回复可信度、提炼事实边界,结果可导出结构化 JSON。
- **chstock-macro-monitor** — 取宏观市场原始数据(汇率、美债收益率、BTC、黄金 / 原油 / 天然气、纳指期货、中国 CPI/PPI/社融/PMI),金十 MCP 优先、失败再用 Alpha Vantage / Tushare / Stooq 兜底。只输出 JSON 证据包,数据含义与风险判断留给模型,为地缘 / A 股 / 美股研究备料。
- **chstock-market-telegraphs** — 抓财联社 CLS 电报与金十数据的**全量原始内容**,不做主题筛选、去重、摘要或截断。用户要「全量原文、自己加工」时用它,直接返回完整数据或写入 JSON。

---

## 3. Shared 共享层:数据、渲染与分发

跨技能复用的通用能力集中在 `shared/`,通过 bundle 机制在同步时打包进每个用得上的 skill,让安装后的技能自包含。这里只放**通用能力**,不放任何领域判断。

- **`shared/html_report` — 通用 HTML 报告渲染框架**
  把任何研报 Markdown 渲染成自包含、可离线打开的单页 HTML,图表用内联 SVG + 轻量 JS(`chartkit.js`),零外部 CDN 依赖、本地双击即开。展示层严格不产生新判断——渲染前后跑文本保全校验,确保 Markdown 里的关键结论一句不丢;换肤只改 CSS 变量,自带 default / claude / print 三套主题。
- **`shared/data` — 数据库连接层**
  所有数据库型 skill 统一走 `db_core`,默认 PostgreSQL(优先读 `ALPHA_PG_URL`),带自检脚本 `db_ping`、建表 SQL 与连接契约文档。谁都不自建私有连接方案,同步后各 Agent 目录里就有一份 `scripts/_shared/db_core.py`,`from db_core import` 一行不用改。
- **`shared/web_search` — Tavily 联网检索**
  当 WebFetch / WebSearch 在本机网络下不可达时,Tavily HTTP API 是最稳的联网主路径。所有需要联网核查催化、找第三方评测的技能复用这一个入口,只要配好 `TAVILY_API_KEY` 就能用。
- **`md-pushplus` — Markdown 渲染并推送(独立 skill)**
  把上游任何技能产出的 Markdown 报告渲染成带样式的 HTML,经 PushPlus 通道推送到微信 / 邮箱。它只做「渲染 + 送达」这一件事,不生成报告内容,让日报 / 研报写完能一键落到手机。

---

## 4. Skill 属性:一次编写,多处运行

仓库里**任何含 `SKILL.md` 的目录都是一个独立 skill**,无论嵌套多深;`skill_sync.py` 会把它单向同步到 Kimi / Claude Code / Codex / Hermes / OpenClaw 等各 Agent 的品牌目录,并按 `shared.bundles` 把数据、渲染、检索等通用能力打包进去,让每个 skill 装到哪都自包含、可运行。每个技能靠 `SKILL.md` 的 metadata `description` 决定何时被触发,靠三层加载(始终在场的元信息 → 触发时进入的正文 → 按需读取的 `references` / `scripts` / `assets`)把 token 花在刀刃上——**改一处 canonical source,所有 Agent 同步生效。**

