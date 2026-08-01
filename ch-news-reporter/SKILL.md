---
name: ch-news-reporter
description: 当用户要求从金十、GitHub Trending、Product Hunt、Hacker News、RSS 等多信源采集财经、AI、宏观或全球地缘风险新闻，建立统一新闻数据表，按 report profile 生成 evidence packet，并输出三类固定主题日报——AI 日报、每日宏观日报、地缘日报（覆盖中东、俄乌、台海、朝鲜半岛、红海/航运、能源通道、制裁、联盟与大国博弈，含风险资产影响），或把这些日报导出为 HTML/网页/可视化单页时，必须使用此 skill。也用于用户在 config/custom_topics.yaml 注册的自定义关注主题日报：当用户要求跟踪特定公司/产品/产业事项（如英伟达 Rubin 出货、Kimi 算力部署、华为升腾出货）、生成每天的自定义主题日报（全部关注事项合并为一份）、新增/暂停/归档关注主题、或对关注主题做多通道（新闻库/实时搜索/本地 Vault 笔记/结构化数据）证据汇总时，走自定义主题流程。适用于“今天 AI 有什么重要新闻”“生成今天宏观日报”“生成今天地缘日报”“把宏观日报导出成 HTML/网页版”“跟踪 CPI/PPI/社融/PMI/非农/美债/Brent/黄金信号”“结合 PH/HN/GitHub Trending 写 AI 日报”“分析红海/霍尔木兹/俄乌/台海/制裁对油价、航运和风险资产的影响”“帮我每天跟踪 XX 的进展并出日报”“把 XX 加为关注主题”“今天 XX 主题有什么增量”等多步骤任务；本 skill 不采集财联社/CLS 数据，只产出上述固定日报与已注册关注主题日报，不用于未注册主题的临时/零碎新闻检索与自由主题研究、单条网页摘要、普通聊天式新闻问答、股票实时交易建议或不需要本地数据采集的简单搜索。
---

# CH News Reporter

## 目标

从用户配置的财经、AI、宏观与地缘风险信源采集当天信息，默认写入统一 PostgreSQL 新闻库；在报告时间点按 profile 生成 evidence packet，并可对关键 GitHub 项目、Product Hunt 产品和外链做二次加工；最后由 Agent 基于方法论、模板和证据包输出中文报告。

本 skill 不提供买卖建议，不把单一快讯当作结论，不在采集阶段过早过滤“财经/AI/地缘”边界。它面向需要日常跟踪市场、AI 产业、开源生态、政策、地缘冲突和风险资产传导的主动研究者。

## 环境变量设置

默认使用共享 PostgreSQL 数据库。进入 skill 目录后，先在当前 shell 设置：

```bash
export ALPHA_DB_BACKEND=postgresql
export ALPHA_PG_URL="${ALPHA_PG_URL:-postgresql://alpha_user:alpha_pass@/alpha_data?host=/tmp}"
python3 scripts/_shared/db_ping.py --alpha-schema   # 同步后的 skill 包
# 源仓库开发态用: python3 ../shared/data/db_ping.py --alpha-schema
python scripts/collect_news.py --date today
```

如果安装包中没有 `scripts/_shared/db_ping.py`，说明 shared bundle 没同步，回到源仓库运行 `python scripts/skill_sync.py`。如果当前环境不能使用 Unix socket，把 `ALPHA_PG_URL` 改成 `postgresql://alpha_user:alpha_pass@localhost:5432/alpha_data`。

只有在需要离线或本机临时 fallback 时才使用 SQLite：

```bash
export ALPHA_DB_BACKEND=sqlite
python scripts/collect_news.py --date today --db data/news_research.sqlite
```

SQLite fallback 下的 `--db` 参数仅指定本地 SQLite 文件；PostgreSQL 模式下连接信息完全由 `ALPHA_PG_URL` 决定。

DB-first 的取数/回写脚本(`collect_news.py` / `prepare_report_data.py` / `save_report_state.py`)在 PostgreSQL 不可达时不会抛裸栈,而是先做一次连接预检,失败就打印可执行的恢复提示并以非零码退出:要么把库拉起来 / 修 `ALPHA_PG_URL` 后重跑,要么按上面的方式切到 `ALPHA_DB_BACKEND=sqlite` 离线跑。**不会**静默回退到可能为空的本地库,以免发出"看起来正常其实没数据"的报告。

## 适用场景与边界

使用本 skill 的典型场景：

- 用户要抓取或刷新“今天”的财经与 AI 新闻库。
- 用户要把金十、GitHub Trending、Product Hunt、Hacker News、RSS 汇总成统一数据表。
- 用户要生成 AI 日报，跟踪模型能力、Agent、开源生态、端侧运行时、AI 资本与产品发布。
- 用户要生成每日宏观日报，跟踪金十电报中的中国/美国经济数据、利率、汇率、大宗商品和风险资产价格信号。
- 用户要输出地缘日报，覆盖全球主要地缘冲突、能源与航运通道、制裁、联盟行动和风险资产传导。
- 用户要跟踪已在 `config/custom_topics.yaml` 注册的自定义关注事项，生成「自定义主题日报」（全部关注事项合并为一份）。
- 用户要新增、暂停或归档一个关注主题（onboarding 流程见「8. 自定义主题日报」）。

不要使用本 skill 的场景：

- 做固定日报与已注册关注主题之外的临时、自由主题新闻检索或研究（先把主题注册进 `config/custom_topics.yaml`，再走自定义主题流程）。
- 只总结用户粘贴的一篇文章。
- 只查询一个公开事实，不需要多信源采集。
- 用户要求实时交易指令、买卖点或确定性预测。

## 工作流程

### 1. 刷新新闻库（DB-first）

进入 skill 目录后运行采集脚本。默认日期为 Asia/Shanghai 的当天。**默认走 DB-first:先看库里当天有没有数据,缺哪个源才补哪个源。**

```bash
python scripts/collect_news.py --date today --only-missing
```

`--only-missing` 会先查库里目标日期各源的行数,只对行数低于阈值的源发起采集,已有的源连网络请求都不发。阈值按源取 `config/sources.yaml` 的 `collect_min_rows`(金十 300 / rss 30 / github_trending 20 / product_hunt 10 / hacker_news 20,约为各源典型日产量),`--min-rows N` 可全源统一覆盖;配置缺失时回退默认 1。今天首跑时库空→全采;重跑或补历史日→读库不重采,保证同一报告日的证据可复现(这也是 watchboard 跨天结算能成立的前提)。需要强制重抓时用 `--replace-date`(优先级高于 `--only-missing`)。

常用参数：

- `--date today` 或 `--date YYYY-MM-DD`：采集目标日期。
- `--only-missing`：DB-first,只补库里当天缺失的源;已有的源跳过,不发请求。
- `--min-rows N`:配合 `--only-missing`,全源统一覆盖 `collect_min_rows` 的每源阈值;不传则按配置逐源判定。
- `--no-watermark`：禁用金十翻页水位线(默认从第 2 页起,某页过半条目已入库即停止翻页),强制翻满 10 页上限。
- `--source all|jin10|github|rss|product_hunt|hacker_news`：限制采集来源，默认 `all`。
- `--config config/sources.yaml`：RSS 配置文件。
- `--db data/news_research.sqlite`：仅 SQLite fallback 使用的本地文件路径；PostgreSQL 模式下忽略。
- `--limit N`：每个来源最多写入 N 条，适合调试。
- `--replace-date`：先删除目标日期内所选来源的旧数据，再重新写入；修正规则或重跑当天库时使用（覆盖 `--only-missing`）。

来源说明：

- 金十电报通过金十 MCP `list_flash` 接入，需要环境变量 `JIN10_AUTH_TOKEN`；脚本会尝试按 cursor 翻页，实际返回量取决于 MCP 服务。
- RSS 只写入目标日期内有明确发布时间的条目；没有发布时间的 RSS 条目默认跳过。feed 抓取成功但当日 0 条时会写一条 `error_type=feed_empty` 的诊断行，与抓取异常区分，coverage 里可见。
- GitHub Trending 会采集 daily 榜单，并补充 GitHub API 中的 repo 元数据。
- Product Hunt 使用 `PRODUCTHUNT_TOKEN` 调用官方 GraphQL API，只采集热门/高排名产品发布，保留 votes、comments、dailyRank、topics、makers 等结构化字段。
- Hacker News 只采集官方 Firebase API 的 `topstories` 和 `beststories`，不采集 `newstories`。

### 2. 按报告 profile 准备基础证据包

日报、动态跟踪等固定报告优先走 profile 流程。脚本只检索、排序、截断，并输出可二次加工的候选对象池；是否 enrichment 由 Agent 读取日报子方法后判断。

```bash
python scripts/prepare_report_data.py --profile ai_daily --date today --format json
```

常用参数：

- `--profile ai_daily|geopolitical_daily|macro_daily`：报告画像；信源、RSS 类别、关键词过滤、状态预算、reference 目录和渲染配置在 `config/report_profiles.yaml`。
- `--include-enrichments`：把 `enrichments` 表里的二次加工结果拼回证据包。
- `--format json|markdown`：JSON 适合 Agent 继续处理，Markdown 适合人工核查。

evidence packet 现在还带两块新内容(DB-first 与活动状态):

- `coverage`：本 profile 各期望源在库里当天的行数、缺失源、profile 相关 feed 失败，以及最终 packet 的分源选择摘要。某源缺数或具体 feed 失败时,报告里相应判断要降级标注；不能用 RSS 总行数掩盖关键官方源抓取失败。
- `prior_state`：`state_enabled` 的 profile(三个日报都已开启)会带上**最近一期 watchboard**(date < 今天)。markdown 输出里是 `## Prior Watchboard` 段,列出今天必须逐条结算的 open 跟踪项;冷启动(无上一期)时按各 profile methodology 的种子/默认构造第一份。机制见 `references/reports/watchboard.md`。

`prepare_report_data.py` 会按 profile 的 `rss_categories` 过滤 RSS，避免地缘 RSS 污染 AI 日报。`macro_daily` 会先从当天金十和 RSS 中筛选宏观相关新闻；每日固定附加两类行情 evidence：

- **基础行情**（金十/Stooq/AV）：Brent、WTI、黄金、天然气、USDCNH、纳指期货等。
- **位置富数据**（Yahoo）：美国 10Y / 5Y 国债收益率、DXY、VIX、BTC，以及核心估值锚——纳斯达克综合（^IXIC）与上证指数（000001.SS）。每个标的额外计算 52 周高低、距 52 周高 %、YTD、20/60 日均线、`pct_vs_ma20`，作为"市场相对位置"判断依据。

evidence packet 同时按 `groups` 字段把行情分组为 `liquidity_rates_fx` / `equity_position` / `risk_appetite` / `commodities`，对应方法论中的流动性三维度 + 商品维度。markdown 输出会先打印「Liquidity & Position Snapshot」表格，再附原始 JSON。

CPI、PPI、社融、PMI 等中国月度数据只在电报识别到当天发布/更新事件时触发 Tushare 细项补查，不做每日机械抓取。

### 3. 执行二次加工

当日报需要 enrichment 时，先读取对应子方法，例如 AI 日报读取：

- `references/reports/ai_daily/enrichment.md`

Agent 从 evidence packet 的 `enrichment_candidates` 中挑选目标，再把明确 target 交给脚本执行。脚本只做抓取和写库，不负责判断哪些对象值得抓。

AI 日报走**两轮 enrichment**：**广度 pass**（默认，5-15 个对象、每个抓一个 URL，核实对象是什么）跑完并完成实体 / 事件聚类后，先按 `framework.md` 判 **S-candidate / A+**；候选一成立就进入**深挖 pass**，不能等到“已确认 S”才补证。候选与 S-confirmed 共用每天 0-2 个对象的硬预算，每个对象目标 3-6 个 URL——用 `scripts/_shared/web_search/tavily_search.py` 搜第三方评测 / 竞品回应 / benchmark 复核，把命中 URL 连同官方公告页、docs、pricing 一起喂给 `enrich_targets.py`。深挖后再定为 S-confirmed 或保留 S-candidate；证据客观不可得可提前停止，但必须记录阻断项与缺口。判级标准见 `references/reports/ai_daily/framework.md`（价值定级），深挖清单见 `references/reports/ai_daily/enrichment.md`（深挖 pass）。

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

**截断 RSS 全文回填（硬规则）**：`prepare_report_data.py` 会对 `content` 被源站截断（以「…」/「...」结尾）的 RSS 条目，在 `enrichment_candidates` 里给其自身 URL 的 `article_url` 候选打上 `truncated: true` 标记，`reason` 标为「RSS content truncated — fetch full article from source URL」。这类候选**必须全部纳入本轮 enrichment**——抓取条目原文 URL，抓回的 `text_excerpt` 即为该条目的权威正文，报告引用以全文为准，不得只依据截断摘要下结论。全文回填是补全动作，**不计入 5-15 个广度 pass 预算**。Juya AI Daily 等每日聚合类 feed（feed summary 被源站截断在「…」）必然触发此规则；长文（如 27 条聚合早报）抓取时显式传 `--website-max-chars 16000` 避免正文被二次截断。

完成 enrichment 后，再生成 enriched evidence packet：

```bash
python scripts/prepare_report_data.py --profile ai_daily --date today --include-enrichments --format markdown
```

### 4. 研究分析

按报告 profile 读取对应方法论：

- `references/reports/<profile>/framework.md`：**当前分析框架（唯一可信源）**——frame schema、path/维度定义、watchlist、输出板块、写作要点、冷启动种子。**每个 `state_enabled` profile 先读这个。** 改框架/换 regime 也只改这个文件。
- `references/reports/watchboard.md`：活动状态层通用机制（所有 `state_enabled` 的 profile 共用）。
- `references/reports/ai_daily/methodology.md`（方法论常量：数据分层 / 输出边界 / 反例）
- `references/reports/ai_daily/enrichment.md`：AI 日报 enrichment 子方法，包含 target 选择契约和解读方法。
- `references/reports/macro_daily/methodology.md`（方法论常量：月度数据规则 / 结论约束）
- `references/reports/geopolitical_daily/methodology.md`（方法论常量：边际判定 / 证据处理 / 可信度 / 降级策略）
- `references/reports/geopolitical_daily/cross_asset_impact_framework.md`

根据问题选择财经、AI、产品观察、开源生态或地缘风险框架。`state_enabled` 的 profile **先读各自 `framework.md`（当前分析框架，唯一可信源）** 与 `watchboard.md`（活动状态机制），再读 `methodology.md`（方法论常量），并逐条结算 packet 里 `prior_state` 的 open 跟踪项。`geopolitical_daily` 的路径（A/B/C/D）、区域主线、行为体权重、传导渠道和信号清单都在 `framework.md` 定义、在 watchboard 里每日滚动，**没有独立 frame 相位文件**；按 `framework.md` 的 path 判定逻辑用近 7-14 天证据现判，再按需要加载跨资产传导框架。框架本身的换代（regime 质变时改 `framework.md`）是慢思考层 + 人在环的动作，不在日报流程里。

`macro_daily` 必须优先读取 `references/reports/macro_daily/framework.md`（当前框架）与 `methodology.md`（方法论常量），并以 `macro_data_events` 判断当天是否有中国 CPI/PPI/社融/PMI 月度数据更新；没有事件时不引用旧月度数据做“今日更新”。

分析时必须区分：

- **确认事实**：多源一致、原文明确、时间可追溯。
- **市场/产业信号**：价格、政策、融资、产品发布、开源热度、供需关系等可观察变化。
- **模型推断**：基于证据链的判断，必须说明不确定性和可能反例。

`geopolitical_daily` 还必须区分：

- **事实层**：军事行动、外交/调解、制裁、军援、核活动、航运/能源设施、联盟声明。
- **行为体压力变量**：主要国家、联盟、国际组织、代理人网络各自的能力、动机和约束是否改变。
- **市场定价信号**：Brent、天然气、黄金、美元、美债、VIX、航运保险、通胀预期等可观察反应。
- **路径判定与子分支**：当前处于 A 局部缓和 / B 可控摩擦 / C 扩散升级 / D 系统冲击哪条路径，子分支为何，路径切换信号是否出现；概率调整必须回到当日证据。

### 5. 输出报告

按报告 profile 读取对应模板：

- `references/reports/ai_daily/template.md`
- `references/reports/macro_daily/template.md`
- `references/reports/geopolitical_daily/template.md`

默认输出对应主题的中文研究报告。关键证据使用：

```text
时间 - 标题 [来源]
```

报告不应只罗列新闻，要把证据转化为观点：发生了什么、为何重要、影响哪些资产/产业/公司类型、后续观察什么。

`macro_daily` 报告采用"流动性优先"范式：流动性 → 折现率/风险溢价 → 权益估值 → 市场相对位置 → 边际方向。报告必须覆盖：

1. 一句话结论（必含"流动性边际方向 + 纳指/上证位置档位 + 对权益估值的影响方向"三要素）
2. 流动性总图表（利率/美元/风险偏好/人民币/商品的当日值与边际变化）
3. 市场相对位置表（纳指、上证、纳指期货的收盘、距 52 周高 %、YTD、20 日趋势、位置档位"高/中/低"）
4. 今日 3 个关键边际信号（每条走"信号属性 → 市场定价校验 → 位置约束"三步法）
5. 流动性传导推断（美元体系 / 人民币体系 / 跨市场验证）
6. 宏观新闻与数据事件、中国政策与流动性、美国政策与流动性
7. 反向场景与风险（流动性方向 vs 市场位置是否矛盾、何种证据会推翻判断）
8. 后续观察（24-72h 关键数据/会议/价格位）

`geopolitical_daily` 报告必须把新闻证据写成 `时间 - 新闻 [来源]`，并包含路径判定（A 局部缓和 / B 可控摩擦 / C 扩散升级 / D 系统冲击）、当前子分支、烈度、今日边际变化、**框架演进与跟踪项结算（逐条结算上一期 open 项）**、区域主线、能源/航运/市场反应、下一关键节点倒计时、路径概率今日变动和 24-72h 观察清单。

### 5b. 简版输出（≤600 字，可选）

当用户要“简版 / 精简 / 速览 / 短版 / 600 字以内”的日报时改读简版模板，**仅 `geopolitical_daily` 与 `macro_daily` 提供简版**；`ai_daily` 不做简版，仍走完整版。

- `references/reports/geopolitical_daily/template_brief.md`
- `references/reports/macro_daily/template_brief.md`

简版只换展示密度，不换分析动作：采集、证据包、读 framework / methodology、读 `Prior Watchboard` 并逐条结算 open 项、第 6 步回写今天的 watchboard——全部照常跑。简版省掉的是正文的逐条铺开（不按国家板块罗列每条快讯、不画流动性总图与位置大表、不渲染完整「本期变更」段），但一句话结论 / 路径判定、今日边际变化、月度数据无更新明示、反向风险、不给交易建议这些方法论底线必须保留；正文压到 600 字内，跟踪项结算用一句话带过、明细留在 watchboard。文件名用 `reports/<profile>_<date>_brief.md`，与完整版并存不冲突，需要网页版时照常用 `render_report_html.py`（按前缀识别 profile）。

### 6. 回写活动状态（watchboard）

`state_enabled` 的 profile（ai_daily / macro_daily / geopolitical_daily）出完报告后,必须把今天的新 watchboard 存回去,供明天 carry-forward:

```bash
cat today_watchboard.json | python scripts/save_report_state.py \
    --profile geopolitical_daily --date today --state-file -
```

watchboard 的 JSON 结构(regime / tracking_items / next_nodes / falsifiers / frame)通用骨架见 `references/reports/watchboard.md`;各 profile 的 frame schema 见各自 `framework.md` 机器区(脚本即从这里读取校验规则,读不到才回退 `report_profiles.yaml`)。`ai_daily` 还必须回写顶层 `grading_audit`：当天无候选写 `[]`，有候选则记录触发条件、候选排除检查、证据类型、确认阻断项和深挖 URL / 状态。脚本做结构校验(必填字段、frame 字段齐全、概率求和、定级审计、**上一期 open 项有没有被漏结算**),报错就按提示补全再存——它只查结构,不评判分析内容。`--check-only` 可只验证不写。

若 profile 重命名，需要先迁移历史状态，避免 watchboard 冷启动断链。迁移脚本会检查同日期冲突，冲突时停止且不覆盖：

```bash
python scripts/migrate_profile_state.py --from-profile iran_dynamic --to-profile geopolitical_daily --check-only
python scripts/migrate_profile_state.py --from-profile iran_dynamic --to-profile geopolitical_daily
```

### 7. 按需生成 HTML（展示层）

当用户要 HTML、网页、可视化或截图风格的日报时，先写好并核对 `reports/<profile>_<date>.md`，再把它渲染成一份自包含单页 HTML（AlphaVault 站点风格，图表不依赖外部 CDN，本地浏览器直接打开；另有 `--theme claude` 暖色风格）。HTML 只是展示层：**不新增任何研报判断，也不删减 Markdown 正文**——共享渲染器会做文本保全校验，缺字报警告不阻断。

```bash
python scripts/render_report_html.py -i reports/macro_daily_2026-05-19.md
# 默认输出同名 .html；profile 从文件名前缀自动识别（ai_daily / macro_daily / geopolitical_daily）
```

三个 profile 通用的处理：

- `一句话结论` 段自动升格成醒目的 hero 摘要卡。
- 表格里的 +/- 数值、涨跌方向自动染色；非加粗的分类格（如宏观日报「性质」列的"数据事件/政策表态"）渲染成彩色 pill。
- `--theme print` 出黑白衬线、A4 友好版，适合导出 PDF 或邮件附件；文件名非标准前缀时用 `--profile` 手动指定。

`geopolitical_daily` 的渲染配置启用**路径概率图**（A/B/C/D 的概率条），把当天 watchboard 传进来即可：

```bash
python scripts/render_report_html.py -i reports/geopolitical_daily_2026-06-04.md --watchboard today_watchboard.json
```

渲染框架来自仓库通用 `shared/html_report`（随 `shared` bundle 同步到 `scripts/_shared/html_report/`），与 A 股各 skill 共用同一套主题与图表工具；新增样式主题只需在该目录 `themes/` 下放一个 CSS 文件。

### 8. 自定义主题日报（custom topics）

固定三日报之外，用户可以在 `config/custom_topics.yaml` 注册自己的窄关注事项（5-10 个，如英伟达 Rubin 出货、Kimi 算力部署、华为升腾出货）。「自定义主题」在 News Reporter 里是**一个**与固定三日报平级的主题：所有 active 关注事项每天合并产出**一份**日报 `reports/custom_daily_<date>.md`，事项作为报告内板块，不按事项各出一份。单个事项当天有重大进展时，可应用户要求额外出一份该事项的单主题深挖报告。设计全文见 `docs/custom-topics-design.md`。

**onboarding（新增主题）**：用户给出关注点 → Agent 按 `config/custom_topics.yaml` 的 schema 起草主题配置（slug、focus、keywords、web_search queries、通道挂钩）→ 用户确认后落盘 → 首次跑检索建立冷启动 watchboard。slug 禁止与固定 profile 冲突（加载时校验、冲突即报错）；`status: active / paused / archived` 管生命周期，`open_budget: 0` 或 `state_enabled: false` 关闭跨天状态。

**每日流程（全部关注事项 → 一份合并日报）**：

```bash
python scripts/collect_news.py --date today --only-missing        # 照旧刷新新闻库
python scripts/topic_retrieve.py --topic all --date today         # 遍历 active 事项:多通道检索 → 去重/落库 → 各事项证据包
python scripts/prepare_report_data.py --topic <slug> --date today --format markdown   # 需要单看某事项证据包时
```

- 证据来自四个通道（脚本只取数/去重/落库/打包）：`news_db`（库内新闻关键词+时间窗）、`pg_data`（alpha_data 结构化表白名单查询，仅 PG）、`web_search`（Tavily 实时检索，结果落库 `items`，需 `TAVILY_API_KEY`，无 key 或网络失败时优雅降级并记 coverage；历史日期回放仅读取已落库 Web 证据，禁止调用以当前时刻为锚的实时检索）、`alpha_vault`（本地 Obsidian Vault 只读检索，只接受 `vault_root` 内相对路径或 glob；不存在或越界路径跳过并记警告）。新通道 = 在 `scripts/retrievers/` 加一个原子脚本 + 配置项。
- `--topic all` 遍历所有 `active` 且 `frequency: daily` 的主题；`weekly` 主题不进每日批量，`paused` 主题只能显式点名手动跑，`archived` 不再检索。
- web 检索预算：每主题 `max_queries_per_day` + 全局 `global_max_queries_per_day`，当日已执行 query 以库内收据计量，同日重跑不重复扣预算；落库行按 `retention_days` 自动清理（也可 `python scripts/topic_retrieve.py --groom`）。
- 分析时读 `references/reports/custom_topic/methodology.md`（通用底线：证据分层 / 边际判定 / 传闻降级 / 无增量不硬凑）+ 各事项 `focus` + 各事项证据包与 `Prior Watchboard`，按 `references/reports/custom_topic/template.md` 写**一份合并日报**：每个关注事项一个板块（边际变化 / 新证据 / 判断更新 / 跟踪项一句话结算），当天无增量的事项一句话带过，不硬凑篇幅。
- 回写仍按事项逐个进行：`save_report_state.py --profile custom_<slug> --date <报告日>`，watchboard 通用骨架（regime/tracking_items/next_nodes/falsifiers）照常校验，frame 自由结构。跨天状态按事项各自滚动，合并日报只是展示层。

## 数据表说明

采集脚本默认写入 PostgreSQL；显式设置 `ALPHA_DB_BACKEND=sqlite` 时写入本地 SQLite fallback：

- `items`：统一新闻表。
- `items_fts`：FTS5 全文索引（SQLite 下由 items 表上的 AFTER INSERT/UPDATE/DELETE 触发器自动同步，老库首次打开时补建触发器并一次性 rebuild）。
- `enrichments`：可选二次加工表，由 `enrich_targets.py` 写入，不改变原始 `items`。
- `report_state`：活动状态层(watchboard),按 `(profile, date_key)` 存每日分析状态;由 `save_report_state.py` 写入、`prepare_report_data.py` 读回,与原始 `items` 解耦。属运行时数据,不进发布包。

核心字段：

- `date_key`：Asia/Shanghai 日期，格式 `YYYY-MM-DD`。
- `source_type`：`jin10`、`github_trending`、`rss`、`product_hunt`、`hacker_news`，以及自定义主题 web 检索落库的 `web_search`（自由 TEXT 值，无需 DDL）。
- `source_name`：具体来源名称；web 检索行形如 `tavily:<slug>`。
- `published_at` / `fetched_at`：发布时间与抓取时间。
- `title` / `content` / `url`：主要文本与链接。
- `tags_json` / `metadata_json` / `raw_json`：结构化补充信息；web 检索行的 `metadata_json` 必带 `query` 与 `retrieved_at`，保证证据可回指到具体检索动作。

采集使用稳定哈希去重。重复运行同一天采集不会重复插入同一条记录。

`enrichments` 表的真实列（`init_alpha_data.sql` / `db_adapter.py` 一致）：

- `item_id`：对应 `items.id`。
- `enrichment_type`：二次加工类型，对应逻辑视图里的 `target_type`（`github_repo`、`product_website`、`article_url`）。
- `source`：被二次抓取的 URL，对应逻辑视图里的 `target_url`。
- `model` / `prompt_hash`：若该 enrichment 经过模型加工，记录所用模型与 prompt 指纹；纯抓取则留空。
- `result_json`：抓取/加工结果的 JSON 串，是这张表的有效载荷。
- `created_at`：写入时间。

`prepare_report_data.py` 读这张表时，会把 `result_json` 展平成给模型看的 enrichment 对象（带 `target_type` / `target_url` / `status` / `fetched_at` / `title` / `text_excerpt` / `metadata` 等字段）；这些是逻辑视图，不是表里的物理列。直接查库排错时以上面的真实列为准。

二次加工层可以抓 README、官网 HTML、meta、标题和正文片段，可以做去重、排序、字段抽取和截断；不判断产品价值、不生成结论、不拼完整报告。

`macro_daily` 的 evidence packet 会额外包含：

- `macro_news_items`：按宏观关键词筛选后的金十、RSS 新闻。
- `macro_data_events`：从新闻中识别的 CPI/PPI/PCE/PMI/社融/非农/库存等数据事件。
- `macro_market_signals`：价格证据。`data` 平铺所有标的；`groups` 按方法论分组：
  - `liquidity_rates_fx`：US10Y、US5Y、DXY、USDCNH
  - `equity_position`：纳指、上证、纳指期货（含 52 周高低 / YTD / MA20 / pct_off_52w_high）
  - `risk_appetite`：VIX、BTC、黄金
  - `commodities`：Brent、WTI、天然气
- `conditional_data_fetches`：事件触发式补查结果；中国 CPI/PPI/社融/PMI 只有在当天电报出现发布/更新信号时才抓取。

## 输出规范

- 默认中文，除非用户明确要求英文。
- 先给结论，再给证据，再给影响路径和观察清单。
- **文风讲人话，减少机械与僵硬**：像跟懂行的人当面把一件事讲清楚那样写，句子通顺、有逻辑衔接，该解释因果和给判断时把话说透。避免模板腔、翻译腔和套话——别成段堆砌"综上所述""值得注意的是""总体来看"，别把每条都写成生硬的"主语+动词+宾语"公式句，也别为了凑结构把话说断、只丢关键词。
- **同类信息优先用列表，但每条要说人话**：同一维度的多个条目（多条新闻、多个项目、多个信号、多方行动、多个观察点）拆成 bullet 或编号，一条一项，别塞进一个长段落；但每条用完整通顺的话写，不要退化成"字段A - 字段B - 字段C"式的横杠拼接。结构化对照（厂商 × 动作、指标 × 数值、节点 × 日期）才用表格。
- 每个关键判断都要能回到数据表中的新闻或项目。
- 对传闻、单源消息、未确认说法必须降级表述。
- 财经内容避免直接给交易指令；AI 内容避免把 GitHub star 变化直接等同于商业成功。
- 默认控制在 1500-2500 字；`ai_daily` 当天有 S-confirmed 今日重点、带深度拆解板块时正文可到 3000+ 字（深拆本身就是深度研究，不必压字数）；用户要求深度研究时也可扩展。当用户要“简版 / 精简 / 速览 / 短版”时改走简版输出（见工作流程「5b. 简版输出」）：**仅 `geopolitical_daily` / `macro_daily`，正文 ≤600 字**；`ai_daily` 不提供简版。

## 示例

用户：

```text
根据今天的数据，帮我看 AI Agent 方向有什么新信号。
```

执行：

```bash
export ALPHA_DB_BACKEND=postgresql
export ALPHA_PG_URL="postgresql://alpha_user:alpha_pass@localhost:5432/alpha_data"
python scripts/collect_news.py --date today --only-missing
python scripts/prepare_report_data.py --profile ai_daily --date today --format json
# Agent 读取 references/reports/ai_daily/enrichment.md 后，从 enrichment_candidates 中挑选 targets。
python scripts/enrich_targets.py --targets-file selected_targets.json
python scripts/prepare_report_data.py --profile ai_daily --date today --include-enrichments --format markdown
```

输出应包含：

- 一句话结论（唯一速读层）：第一句给框架移动——哪条矢量 / 渗透进档、新开或被证伪，以及这对两轴大图意味着什么的关系级判断（地图没动就直说是证据积累日）；再点 S-confirmed 或 S-candidate 的当前状态与 24-72h 最该盯的对象。
- 今日重点 · 深度拆解：只放通过结构影响、一手证据、真实可用、独立复核四道门槛的 S-confirmed；对 0-2 个重点做 300-500 字拆解，没有就省略整节。
- 重点候选 · 待验证：深挖后仍缺官方证据、真实可用性或独立复核的 S-candidate / A+ 在这里短写，明确阻断项和 24-72h 验证点；不得静默降成普通 A，也不得冒充已确认重点。
- 纵轴 · 能力与技术演进：逐条矢量，实验室证据 + 开源证据并在一处，写档位（实验 / 收敛中 / 事实标准）+ 卡点；同一矢量 ≥2 证据才判收敛。
- 横轴 · 渗透率：有没有新形态 / 新入口让更多人真用上（载体 / 用户段 / 自主度 / 真实使用信号 + 萌芽 / 早期采用 / 主流化档位）；没有就直说"今日无新渗透信号"。
- 证据随正文走：不再单列「原始证据附录」。每个对象只在它唯一展开的位置——今日重点、纵轴、横轴或资本与政策——附上来源、热度 / 体量和可点击的原文链接；不要为了补附录再重复一次。
- 横向对照与降噪仍作为 Agent 的后台分析步骤：用来校验供需、开源 vs 公司、中美错位，以及识别只有注意力热度的对象；不再单独展示成读者可见章节。最关键的关系级判断可收进「一句话结论」，被降级但仍值得登记的对象在对应正文板块留一句即可；与框架无关的低价值背景项只在后台保留。
- 待跟踪 & 本期变更：未来 24-72h 三栏观察（待印证矢量 / 待观察渗透 / 待发布动作）+ 本期 watchboard 变更（只用 T 编号引用）。

### 每日宏观日报

用户：

```text
生成今天的宏观日报，重点看美国10年期国债、Brent、黄金和中美经济数据。
```

执行：

```bash
export ALPHA_DB_BACKEND=postgresql
export ALPHA_PG_URL="postgresql://alpha_user:alpha_pass@localhost:5432/alpha_data"
python scripts/collect_news.py --date today --source all --only-missing
python scripts/prepare_report_data.py --profile macro_daily --date today --format markdown
```

加载：

- `references/reports/macro_daily/methodology.md`
- `references/reports/macro_daily/template.md`

输出应包含：

- 一句话结论（含流动性边际方向 + 纳指/上证位置档位 + 对权益估值的影响方向）。
- 流动性总图表（US10Y/US5Y/DXY/VIX/USDCNH/Brent/黄金/BTC 的当日值与边际变化）。
- 市场相对位置表（纳指、上证、纳指期货：收盘 / 距 52 周高 / YTD / 位置档位）。
- 今日 3 个关键边际信号（每条三步法：信号属性 → 市场定价校验 → 位置约束）。
- 流动性传导推断（美元体系 / 人民币体系 / 跨市场验证）。
- 金十优先的宏观新闻与数据事件；中国 CPI/PPI/社融/PMI 无更新时明确写"无新增月度数据事件"。
- 反向场景与 24-72h 后续观察清单。

### 地缘日报

用户：

```text
生成今天的地缘日报，重点看红海/霍尔木兹、俄乌、台海、制裁和油价/航运传导。
```

执行：

```bash
export ALPHA_DB_BACKEND=postgresql
export ALPHA_PG_URL="postgresql://alpha_user:alpha_pass@localhost:5432/alpha_data"
python scripts/collect_news.py --date today --source all --only-missing
python scripts/prepare_report_data.py --profile geopolitical_daily --date today --format markdown
```

宏观行情(Brent / 黄金 / 天然气 / USD-CNH / 美债 / BTC / 纳指期货)可通过本 skill 的 `macro_monitor.py` 取数,作为风险资产证据补充:

```bash
python scripts/macro_monitor.py market
```

取数后先看 `sources.yahoo_finance`：`OK` 是七个位置锚点全回来了，`PARTIAL(5/7)` 说明有品种没取到，`ERROR` 是整条线都被拒。只要不是 `OK`，`source_failures.yahoo_finance` 里会逐条列出哪个品种、什么原因（`BLOCKED_BY_EDGE` 是 Yahoo 边缘把请求判成机器人，重试即可恢复；`NO_DATA` 是该品种当天确实没有数据），以及 `recovered_by` —— 有值表示后面被 Stooq / Alpha Vantage 补上了、可以正常引用，为 `null` 则这个数字今天真的缺席。**缺席的品种不要用上一交易日的数值顶替，也不要略过不提**，在证据说明里写清是哪一个、为什么缺。

加载：

- `references/reports/watchboard.md`（活动状态机制）
- `references/reports/geopolitical_daily/methodology.md`（含冷启动种子）
- `references/reports/geopolitical_daily/cross_asset_impact_framework.md`
- `references/reports/geopolitical_daily/template.md`

输出应包含：

- 当前路径判定（A 局部缓和 / B 可控摩擦 / C 扩散升级 / D 系统冲击）与子分支。
- 全球地缘烈度级别与趋势。
- 今日边际变化（1-3 条；按 methodology"边际变化判定"三条线筛选）。
- 框架演进与跟踪项结算：逐条结算上一期 open 跟踪项，写清新开 / 关闭与框架微调。
- 中东、俄乌、台海、朝鲜半岛、制裁/联盟等区域主线中真正有边际变化的部分。
- 能源、航运、制裁、避险资产与风险资产的传导评估。
- 路径概率今日变动、下一关键节点倒计时、24-72h 观察清单。

出完报告后用 `save_report_state.py` 回写今天的 watchboard，供明天 carry-forward。
