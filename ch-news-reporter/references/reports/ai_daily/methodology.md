# AI 日报方法论

## 目标

把当日多信源中的 AI 新闻、开源项目、产品发布与社区讨论转化为可追溯的判断。日报不是新闻列表,而是要并行回答两个问题:

1. **开源 / 独立开发者侧**:今天 GitHub Trending、Product Hunt、Show HN 涌现了哪些新项目?它们的特征、热度、解决的 AI 问题是什么?多个项目同向共振反映出 Agent / 模型生态正在向哪里走?
2. **重点公司侧**:OpenAI、Anthropic、Google(含 DeepMind)、Meta、Mistral、NVIDIA、HuggingFace 等国际厂商,以及 Kimi(月之暗面)、智谱、Minimax、腾讯、阿里、字节、DeepSeek、百度 等中国厂商,今天有哪些**模型发布 / 产品更新 / API & 定价 / 融资 & 政策**类动作?

两条线先各自分析,再横向贯通,最后形成对当日 AI 行业边际变化的判断。完整公司清单与别名见 `config/report_profiles.yaml` 的 `company_groups`。

## 第一性原理:为什么用"双引擎"

AI 行业的边际信息在两个源头交替出现,任何一个单看都不够:

- **公司只看不开源**会丢掉早期信号:Agent / MCP / Coding Agent 这些方向的真正前沿往往先在 Show HN 和 GH Trending 上跑出来,再被大厂收编。
- **只看开源不看公司**会丢掉规模化判断:权重再热,真正决定行业能不能用、能不能赚钱的还是大厂的 API、定价、产品入口。

所以**先各自看 4 步,再横向印证**才能得到一个能落地的判断。

## 引擎 A:新项目发现(GitHub Trending + Product Hunt + Show HN)

### 适用对象

- GitHub Trending daily / weekly 榜单上的项目(尤其是 stars/day 高、近期 commit 活跃、带 release 的)
- Product Hunt 今日有显著 votes、comments、daily rank 或 maker 背景的产品
- Hacker News 上的 `Show HN` 项目发布、自托管工具、AI 原生产品(对 Show HN 的关注度要高于普通 topstories)

### 四步流程

1. **项目特征定性** —— 把每个对象归入一个特征标签:
   - 基础模型与权重(开源模型本体、量化版本、微调变体)
   - Agent 框架与编排(MCP server、Agent 平台、多智能体协作)
   - 开发者工具链(CLI、SDK、IDE 插件、调试、Observability)
   - 推理与部署(推理引擎、量化、本地化运行、KV cache)
   - 数据与 RAG(向量库、文档处理、检索增强)
   - 应用产品(C 端工具、垂直 SaaS、内容生成)
   - 评测与基准(benchmark、leaderboard、agent-eval)
   - 开源基础设施(数据集、训练代码、生态周边)
2. **解决的 AI 问题画像** —— 用一句话回答"这个对象的真实需求场景是什么、给谁用":
   - 是为开发者解决工程问题,还是为 C 端用户解决任务效率?
   - 是补足某个特定能力(长上下文、工具调用、推理),还是封装现有能力?
   - 是垂直行业(法律、医疗、Coding、Customer Support)还是通用平台?
3. **热度真伪辨别** —— star / votes / comments 只代表注意力,不代表采用:
   - 看 GitHub:`stars/day` 增速、latest release、license、homepage、pricing、open issues、push 活跃度、贡献者数
   - 看 Product Hunt:官网是否有 pricing / docs / API / enterprise 入口、是 AI 原生产品还是套层 AI 文案
   - 看 Show HN:回帖反馈是积极采用、技术质疑还是冷淡观望;maker 是否在认真回复
4. **趋势聚类** —— 同类项目当天出现 ≥ 2 个时单独列一条方向信号;**单点不写趋势**:
   - 例:今天 Show HN 出现 3 个 MCP server、GitHub Trending 又有 2 个 MCP 工具 → 写"MCP 工具链向前推进"信号
   - 例:今天 PH 出现 4 个 AI Coding Agent → 写"AI Coding 代理产品化"信号

## 引擎 B:重点公司动作

### 重点公司清单

- **国际**:OpenAI / Anthropic / Google(含 DeepMind) / Meta / Mistral / NVIDIA / HuggingFace
- **中国**:Kimi(月之暗面)/ 智谱 / Minimax / 腾讯 / 阿里 / 字节 / DeepSeek / 百度

完整别名(如 Kimi/Moonshot/月之暗面、Qwen/通义/通义千问)见 `config/report_profiles.yaml` 的 `company_groups`,Agent 在归类前应先读取该字典。

### 四类动作矩阵(模板里就按这四类组织)

| 动作类型 | 涵盖范围 |
|---|---|
| **模型发布** | 基础模型、推理模型、多模态、专用模型(coding/math/voice)、开源权重、新版本号 |
| **产品更新** | C 端 App、Agent 平台、IDE 插件、Workspace 集成、企业版功能 |
| **API & 定价** | 新 API 端点、企业入口、token 定价变动、批量优惠、免费额度变化 |
| **融资 & 政策** | 融资轮次、并购、战略合作、监管表态、安全政策、政府合作、人事变动 |

### 三个分析维度

- **模型 vs 产品要分开**:模型能力升级和产品功能迭代有时是两件事
  - 反例:不要把 ChatGPT 新加一个按钮当作模型升级;也不要把 GPT-5 发布等同于 ChatGPT 改版
- **中美阵营对照**:同一时间窗内,中外厂商之间是呼应还是错位?
  - 例:中国厂商密集放开源权重,而 OpenAI 在推闭源 API → 当日格局信号
  - 例:Anthropic 推 Claude Code,阿里同期推通义灵码 → 同方向竞争
- **商业化层级**:从"研究 → 权重 → API → 产品 → 企业 → 生态"链条上,今天的动作处在哪一层?
  - 单一公司在一天里跨多层(如 Anthropic 同时发模型 + 推 API + 改定价)= 信号密度高

## 横向贯通(两引擎写完后必做)

主动追问下面三个问题,把答案写进"趋势聚类"或"今日结论":

- **引擎 A 是否呼应 / 挑战引擎 B?**
  - 例:Show HN 涌现 MCP 工具 ↔ Anthropic / OpenAI 都在推 MCP / GPT Store,呼应
  - 例:PH 上挤了一堆 AI 笔记应用,而 Notion / 飞书没有相应更新,反例信号:商业入口未跟上
- **重点公司今日没动作的板块,是否被开源社区填补了?**
  - 例:今天大厂没有发新视频模型,但 GH Trending 上出现 2 个开源视频生成项目 → 视频赛道压力向开源转移
- **中国厂商和国际厂商在同一方向上是补位、竞争还是平行?**
  - 例:中国厂商集中在 Coding 和 Agent 平台,而国际厂商集中在通用模型 → 注意中国侧"应用先行"路径

## 数据信息分层

每个对象的证据按以下层级标注,**不要把这些层级混成同一个判断**:

- **确认事实**:多源一致、原文明确、时间可追溯
- **社区热度**:star、votes、score、comments 等关注度数据
- **采用与商业化线索**:release 活跃度、官网、文档、pricing、API、企业入口
- **产业风险与争议**:负面回帖、争议、合规风险、潜在抄袭 / 合规问题

## 输出判断边界

- 可以说"值得进入后续观察""可能代表某类需求升温""与某公司的策略层动作呼应"
- 不直接说"必然成功""会替代某产品""确定商业化"
- 涉及上市公司或资产价格时,只写影响路径与观察点,**不给交易指令**
- 中国厂商如果今天没有官方信源覆盖的动作,**不要用未确认信息硬填**,直接写"无可核实更新"
- GH star 增量、PH votes、HN score 都是**注意力指标**,不是商业成功指标
- 重点公司动态若仅来自第三方转述(The Decoder、媒体快讯),需注明"待官方公告确认"

## 反例(常见错误,看到要改)

- ❌ 把所有 GH Trending 上带 AI 标签的项目都列入新项目雷达 → 没经过特征定性和热度真伪
- ❌ 把 Kimi、智谱等中国厂商动态完全归类到中国新闻里,不和国际厂商横向对照
- ❌ 把 OpenAI 发了一条 tweet 等同于产品发布
- ❌ 把单个项目当作"趋势信号"(必须 ≥ 2 个证据支撑)
- ❌ 罗列今天所有新闻,没有按"为什么值得看"排序
- ❌ 把模型发布(权重/API)和产品发布(应用功能)混为一谈
- ❌ 用"全球 AI 大爆发""国产 AI 弯道超车"等空泛叙事代替具体证据
