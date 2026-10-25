---
name: ch-ai-eng-tracker
description: 跟踪本周 AI 工程动态并产出实战导向的周报。当用户说"看看本周 AI 工程有什么新东西/新模式"、"AI 工程周报"、"最近 agent 开发有什么值得学的"、"OpenAI 官方有什么新教程"、"GitHub 这周有什么火的项目"、"B站有什么好的 AI 开发教程"、"大厂技术公众号最近在写什么"、"HN 上有什么 AI 工程讨论"时使用此 skill。只收工程口径（Agent 工程、AI 编程、框架/工具链、评测、推理部署、工程实战复盘），不收模型发布营销稿、融资新闻、纯研究论文；不提供投资建议；不做实时快讯（那是新闻类 skill 的职责）。
---

# ch-ai-eng-tracker — AI 工程周报（实战导向）

## 目标

**做什么**：基于 6 路信源（OpenAI 官方博客、OpenAI Cookbook、GitHub 周榜、B站、大厂技术公众号、Hacker News），产出一份对 AI 软件工程与 Agent 开发有**实战指导作用**的周报（Markdown + HTML）。每条入选内容都要能回答"这对正在做 agent 开发的工程师有什么用"。

**不做什么**：不做模型发布与融资的资讯搬运；不做投资建议；不做实时快讯；不覆盖纯研究论文（但模型发布里的工程内容——推理优化、新 API 用法、cookbook 教程——照收）。

**给谁用**：正在做 AI 应用 / Agent 开发的工程师，想把每周有限的输入时间花在"能直接用到手上项目"的信息上。

## 适用场景与边界

| 适用 | 不适用 |
|---|---|
| "生成本周 AI 工程周报" | "今天 AI 圈有什么新闻"（实时快讯，用新闻类 skill） |
| "最近 agent 开发有什么新模式/最佳实践" | "GPT-x 发布了什么"（模型发布资讯，除非问的是其工程用法） |
| "OpenAI 官方这周有什么新教程" | "帮我调研某个具体技术选型"（深度调研任务，周报只做线索） |
| "GitHub/B站/公众号/HN 这周有什么值得看的" | 投资决策参考（本 skill 与投资无关） |
| "把上次周报的时间窗换成上周/近 14 天再出一期" | 分钟级监控告警 |

时间窗口径：默认**滚动近 7 天**；用户说"本周/上周"时先确认是滚动 7 天还是自然周。

## 核心方法论：实战价值优先

### 收录标准（四级优先，从高到低）

1. **可复用的工程模式**——agent loop 设计、context 工程、工具/MCP 设计、evals 方法、guardrails、可观测与追溯、成本/延迟优化、多 agent 协作范式。这是周报的皇冠。
2. **生产实战案例**——真实系统的架构复盘、踩坑记录、上线经验。大厂公众号长文与工程博客深度文主要落在这层。
3. **新工具/框架的上手教程**——cookbook 更新、B站实战教程、GitHub 新工具。收录条件：能写出"上手路径"（解决什么问题、从哪开始、值不值得试）。
4. **工程方法论讨论**——HN 上关于 agent 开发实践的高分讨论，能提炼正反方观点的。

**降级与剔除**：纯 benchmark 跑分（除非附带工程改进细节）、模型发布营销稿、无工程细节的新闻通稿、转述二手且无增量的内容。

### 三条纪律

- **排序原则**：实战可操作性 > 影响力 > 热度。播放量/stars/points 只做同层内部排序，永远不因热度把营销稿抬进精读。
- **去重规则**：同一事件多源报道只保留一手（官方博客 > 项目 README/发布页 > 深度复盘 > 媒体转述），合并成一条并注明其他信源看到了什么。
- **数据纪律**：所有数字（stars、播放量、points）原样引用脚本 JSON，不编造不四舍五入；脚本没返回的字段不写；时间窗与各信源抓取时间戳写进报告头。

### 公众号归属核实（本项目特有陷阱）

检索引擎按"账号名"匹配，返回的文章可能来自其他号。**真实归属以 content 模式抓回的 `account` 字段（页面 js_name）为准**——要署账号名的条目必须先用 content 模式核实，核实不符就按真实账号署名或弃用。

## 工作流程

### Step 1 定时间窗

确认 `--days`（默认 7）与用户说的"本周"口径一致。产出：窗口参数。

### Step 2 并行取数

六个脚本互相独立，尽量并行跑；产物 JSON 建议存档到 `reports/<日期>/raw/`（`--output`），便于回溯：

```bash
RAW=reports/$(date +%F)/raw && mkdir -p $RAW
python scripts/fetch_official_blogs.py  --days 7 --output $RAW/blogs.json
python scripts/fetch_openai_cookbook.py --days 7 --output $RAW/cookbook.json
python scripts/fetch_github_trending.py --days 7 --output $RAW/github.json
python scripts/fetch_bilibili.py        --days 7 --output $RAW/bilibili.json
python scripts/fetch_wechat_articles.py search --days 7 --output $RAW/wechat.json
python scripts/fetch_hackernews.py      --days 7 --output $RAW/hn.json
```

产出：六份原始 JSON。每个脚本只返回原始条目，**做不做工程筛选是模型的活**；部分失败（`degraded` 数组）照常用其余数据，全败（`error`）则该信源缺席并在报告头注明。

### Step 3 精读选篇（本 skill 的核心动作）

从全部原始条目里挑 **3–5 篇**最可能改变手上做法的内容（优先级见方法论第一、二层），用脚本抓**全文**再判断，不要看标题猜：

```bash
# 通用网页（OpenAI 博客文章、项目博客、HN 链接的外文）
python scripts/fetch_article_text.py --url "https://..." --max-chars 12000
# 公众号文章（顺带核实真实归属）
python scripts/fetch_wechat_articles.py content --url "https://mp.weixin.qq.com/s/xxxx"
```

读完发现不够格进精读的，降级进对应板块或丢弃。产出：精读对象全文 + 每篇的"核心模式 / 如何借鉴"笔记。

### Step 4 去重、筛选、分板块

按去重规则合并多源；按四级优先落板块；每条自问"这条回答了'对我的 agent 开发有什么用'吗"，回答不了就删。产出：报告结构决策（哪些进精读、各板块条目与排序）。

### Step 5 写 Markdown 报告

按下方输出规范写，存 `reports/<日期>/ai-eng-weekly-<日期>.md`。

### Step 6 渲染 HTML 并交付

```bash
python scripts/render_report_html.py --input reports/<日期>/ai-eng-weekly-<日期>.md
```

交付时给出两份文件路径 + 3 句以内口头速览 + 信源健康说明（哪个信源降级/缺席及原因）。

## 数据获取（脚本抓手）

| 脚本 | 职责 | 关键参数 | 凭据 |
|---|---|---|---|
| `fetch_official_blogs.py` | OpenAI 官方博客 RSS | `--days` | 无 |
| `fetch_openai_cookbook.py` | Cookbook 合并 PR + commits | `--days --no-commits` | GITHUB_TOKEN 可选 |
| `fetch_github_trending.py` | 周榜 + 新建高星仓库 + enrich | `--days --enrich N` | GITHUB_TOKEN 可选 |
| `fetch_bilibili.py` | B站视频（官方搜索 API + 降级链） | `--keyword --pages --no-fallback` | BILIBILI_SESSDATA 可选 |
| `fetch_wechat_articles.py` | 公众号检索(search)/正文(content) | `--account --query / --url` | 元宝或 Tavily 或 DeepSeek |
| `fetch_hackernews.py` | HN 高分 AI 讨论 | `--min-points` | 无 |
| `fetch_article_text.py` | 任意 URL 抓全文（jina 降级） | `--url --max-chars` | 无 |
| `render_report_html.py` | Markdown → 自包含 HTML | `--theme` | 无 |

通用契约：JSON 到 stdout（或 `--output` 写文件）；整跑失败 `{"error":{code,detail}}` 非零退出；部分失败进 `degraded` 数组，**不静默丢数据**。各信源抓取细节、反爬机制、降级链见 `references/sources.md`（排障与调信源前必读）。

降级语义示例：

- 公众号/抖音类检索依赖元宝或 Tavily 凭据；全缺时 `ALL_ENGINES_FAILED`，明细指出缺哪个 key。
- B站官方 API 被 -412 风控时自动落搜索引擎降级（`mode: search_fallback`，条目带 `source: bilibili_fallback`，无播放量字段——报告里就不写播放量）。
- GitHub 无 token 时限 60 req/h，脚本已节流；403/429 时 enrich 提前停止并在 degraded 标注。

## 输出规范

### 结构（八块，顺序固定）

| # | 板块 | 条数 | 每条要求 |
|---|---|---|---|
| 1 | 报告头 | — | 时间窗、生成时间戳、信源健康表（成功/降级/缺席+原因） |
| 2 | 本周实战精读 | 3–5 篇 | 全报告重心。每篇两段式：**核心模式/做法**（提出或验证了什么可复用做法）+ **如何借鉴**（落到 agent 开发的具体动作）。必须基于全文 |
| 3 | 本周速览 | 3–5 条 | 一句话一条，各 ≤60 字，说清"什么 + 为什么值得注意" |
| 4 | 官方教程与工程博客 | ≤5 条 | 是什么 + 实战启示（能怎么用） |
| 5 | 开源速递 | ≤6 条 | 项目一句话 + 为什么关注 + 上手建议 + stars/周增 |
| 6 | B站精选 | ≤4 条 | 标题/UP主/播放量 + 能学到什么。只收实战教程类，宣传向视频不收 |
| 7 | 大厂技术公众号 | ≤5 条 | 真实账号/标题 + 要点。优先工程复盘长文，署名前按方法论核实归属 |
| 8 | HN 热谈 | ≤4 条 | 话题 + 分数/评论数 + 正反方要点（各一句） |

某信源降级/缺席时照出其余板块，缺席原因写在报告头信源健康表里；对应板块整块省略而非用空话填充。

### 长度与文风

- 全报告正文 1500–2500 字；精读每篇 150–300 字；其余每条 1–3 句。
- 文风讲人话：像跟懂行的同事当面讲清楚，句子通顺、因果说明白；同项罗列用 list，每条完整成句；不用横杠拼接的"字段A - 字段B"。
- 每条必须带链接；数字与信源一致（播放量/stars 原样引用）。
- 精读的"如何借鉴"要具体到动作（"把 evals 拆成 per-turn 断言"），不写"值得关注""值得学习"这类空话。

## 示例

输入："生成本周 AI 工程周报。"

命令序列：Step 2 六连跑 → 选 4 篇精读（2 篇 OpenAI 博客 + 1 篇公众号长文 + 1 个 GitHub 项目 README）→ content 核实公众号归属 → 写 md → 渲染。

输出骨架（节选）：

```markdown
# AI 工程周报 · 2026-10-18 ~ 10-24

> 窗口：滚动 7 天 ｜ 生成：2026-10-24 12:00 CST
> 信源健康：OpenAI 博客 ✅ ｜ Cookbook ✅ ｜ GitHub ✅ ｜ B站 ✅ ｜ 公众号 ⚠️ Tavily 降级（缺腾讯云凭据）｜ HN ✅

## 一、本周实战精读

### 1. Rapidly scaling online storage to serve over 1 billion ChatGPT users (Part II)
[OpenAI Blog](https://openai.com/index/scaling-storage-one-billion-users-part-two) · 10-24

**核心模式**：OpenAI 讲 Habitat 存储层怎么做到 12× 读提速——多租户隔离
（租户级配额+慢查询熔断）、全局缓存分层、Azure Cosmos DB 的分区键设计。
值得注意的不是组件本身，而是"先隔离再优化"的顺序。

**如何借鉴**：我们做多 agent 平台时同样面临共享存储的多租户争抢。可直接借鉴
两点：按租户维度做配额与熔断（而非全局限流）；读路径加一层带 TTL 的全局缓存，
TTL 取业务可容忍 staleness 而非追求强一致。

## 二、本周速览

- OpenAI 发布 Decisions API：低延迟小决策专用通道，agent 工作流里 route/classify 类调用有了更便宜的落点。
- openai-cookbook 合并 Databricks Sandbox 示例：Agents API 沙箱供给模式又多一家，代码解释器场景可参考。
- ...

## 五、开源速递

- **morluto/rea**（373k★，本周 +88k）：用 agent 做逆向工程，从应用行为到原生二进制。上手：CLI 直接跑单文件，插件机制（dsh）适合把内部反编译流程封装进去。[GitHub](https://github.com/morluto/rea)
```

边界示例：B站官方 API 全被风控 → 自动落降级链，`bilibili.json` 里 `mode=search_fallback`、条目无 `play` 字段 → 报告"六、B站精选"只写标题/UP主/看点，不出现播放量数字，信源健康表标"B站 ⚠️ 搜索引擎降级"。

## 参考索引

| 主题 | 文件 | 何时读 |
|---|---|---|
| 各信源抓取机制、反爬细节、降级链、加信源方法 | `references/sources.md` | 排障、调信源、加新源之前 |
