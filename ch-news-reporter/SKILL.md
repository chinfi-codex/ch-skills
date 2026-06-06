---
name: ch-news-reporter
description: 当用户要求从金十、财联社、GitHub Trending、Product Hunt、Hacker News、RSS 等多信源采集财经、AI、宏观、地缘冲突或伊朗动态新闻，建立统一新闻数据表，按 report profile 生成 evidence packet，并输出 AI 日报、每日宏观日报、财经研究简报、热门产品观察、市场信号复盘、伊朗/中东局势动态或风险资产影响分析，或把这些日报导出为 HTML/网页/可视化单页时，必须使用此 skill。适用于“今天 AI 有什么重要新闻”“生成今天宏观日报”“把宏观日报导出成 HTML/网页版”“跟踪 CPI/PPI/社融/PMI/非农/美债/Brent/黄金信号”“结合 PH/HN/GitHub Trending 写日报”“抓取新闻库做研究”“生成伊朗动态播报”“分析霍尔木兹/油价/黄金受局势影响”等多步骤任务；不用于单条网页摘要、普通聊天式新闻问答、股票实时交易建议或不需要本地数据采集的简单搜索。
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
# 源仓库开发态用: python3 ../shared/db_ping.py --alpha-schema
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
- 用户要把金十、财联社、GitHub Trending、RSS 汇总成统一数据表。
- 用户要基于本地新闻库回答研究问题，如 AI Agent、算力、宏观风险、流动性、政策、产业链变化。
- 用户要输出研究简报、日报素材、主题观察、市场信号复盘。
- 用户要生成每日宏观日报，跟踪金十电报中的中国/美国经济数据、利率、汇率、大宗商品和风险资产价格信号。
- 用户要输出伊朗/中东局势动态、停火窗口跟踪、霍尔木兹航运、油气价格和风险资产影响分析。

不要使用本 skill 的场景：

- 只总结用户粘贴的一篇文章。
- 只查询一个公开事实，不需要多信源采集。
- 用户要求实时交易指令、买卖点或确定性预测。

## 工作流程

### 1. 刷新新闻库（DB-first）

进入 skill 目录后运行采集脚本。默认日期为 Asia/Shanghai 的当天。**默认走 DB-first:先看库里当天有没有数据,缺哪个源才补哪个源。**

```bash
python scripts/collect_news.py --date today --only-missing
```

`--only-missing` 会先查库里目标日期各源的行数,只对行数低于阈值(`--min-rows`,默认 1,即零行)的源发起采集,已有的源连网络请求都不发。今天首跑时库空→全采;重跑或补历史日→读库不重采,保证同一报告日的证据可复现(这也是 watchboard 跨天结算能成立的前提)。需要强制重抓时用 `--replace-date`(优先级高于 `--only-missing`)。

常用参数：

- `--date today` 或 `--date YYYY-MM-DD`：采集目标日期。
- `--only-missing`：DB-first,只补库里当天缺失的源;已有的源跳过,不发请求。
- `--min-rows N`:配合 `--only-missing`,行数达到 N 才算"已有"。默认 1(任意一条即视为已采)。RSS 这类多 feed 源容易"一条就算齐",想强制补全时把阈值调高。
- `--source all|cls|jin10|github|rss|product_hunt|hacker_news`：限制采集来源，默认 `all`。
- `--config config/sources.yaml`：RSS 配置文件。
- `--db data/news_research.sqlite`：仅 SQLite fallback 使用的本地文件路径；PostgreSQL 模式下忽略。
- `--limit N`：每个来源最多写入 N 条，适合调试。
- `--replace-date`：先删除目标日期内所选来源的旧数据，再重新写入；修正规则或重跑当天库时使用（覆盖 `--only-missing`）。

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

- `--profile ai_daily|iran_dynamic|macro_daily`：报告画像；信源配置在 `config/report_profiles.yaml`。
- `--include-enrichments`：把 `enrichments` 表里的二次加工结果拼回证据包。
- `--format json|markdown`：JSON 适合 Agent 继续处理，Markdown 适合人工核查。

evidence packet 现在还带两块新内容(DB-first 与活动状态):

- `coverage`：本 profile 各期望源在库里当天的行数与缺失源清单。某源缺数时,报告里相应判断要降级标注"今日该源无数据"。
- `prior_state`：`state_enabled` 的 profile(三个日报都已开启)会带上**最近一期 watchboard**(date < 今天)。markdown 输出里是 `## Prior Watchboard` 段,列出今天必须逐条结算的 open 跟踪项;冷启动(无上一期)时按各 profile methodology 的种子/默认构造第一份。机制见 `references/reports/watchboard.md`。

`macro_daily` 会先从当天金十、财联社和 RSS 中筛选宏观相关新闻；每日固定附加两类行情 evidence：

- **基础行情**（金十/Stooq/AV）：Brent、WTI、黄金、天然气、USDCNH、纳指期货等。
- **位置富数据**（Yahoo）：美国 10Y / 5Y 国债收益率、DXY、VIX、BTC，以及核心估值锚——纳斯达克综合（^IXIC）与上证指数（000001.SS）。每个标的额外计算 52 周高低、距 52 周高 %、YTD、20/60 日均线、`pct_vs_ma20`，作为"市场相对位置"判断依据。

evidence packet 同时按 `groups` 字段把行情分组为 `liquidity_rates_fx` / `equity_position` / `risk_appetite` / `commodities`，对应方法论中的流动性三维度 + 商品维度。markdown 输出会先打印「Liquidity & Position Snapshot」表格，再附原始 JSON。

CPI、PPI、社融、PMI 等中国月度数据只在电报识别到当天发布/更新事件时触发 Tushare 细项补查，不做每日机械抓取。

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

- `references/reports/watchboard.md`：活动状态层通用机制（所有 `state_enabled` 的 profile 共用，先读这个）。
- `references/reports/ai_daily/methodology.md`
- `references/reports/ai_daily/enrichment.md`：AI 日报 enrichment 子方法，包含 target 选择契约和解读方法。
- `references/reports/macro_daily/methodology.md`
- `references/reports/iran_dynamic/methodology.md`（自包含，已并入原 frame 的稳定内容，含冷启动种子）
- `references/reports/iran_dynamic/economic_impact_framework.md`

根据问题选择财经、AI、产品观察、开源生态或地缘风险框架。`state_enabled` 的 profile 先读 `watchboard.md` 理解活动状态机制，再读各自 `methodology.md`，并逐条结算 packet 里 `prior_state` 的 open 跟踪项。`iran_dynamic` 的路径（A/B/C）、权重、信号清单现在都在 watchboard 里每日滚动，**不再有 frame 相位文件**；按 methodology"路径判定逻辑"用近 7-14 天证据现判，再按需要加载经济影响框架。

`macro_daily` 必须优先读取 `references/reports/macro_daily/methodology.md`，并以 `macro_data_events` 判断当天是否有中国 CPI/PPI/社融/PMI 月度数据更新；没有事件时不引用旧月度数据做“今日更新”。

分析时必须区分：

- **确认事实**：多源一致、原文明确、时间可追溯。
- **市场/产业信号**：价格、政策、融资、产品发布、开源热度、供需关系等可观察变化。
- **模型推断**：基于证据链的判断，必须说明不确定性和可能反例。

`iran_dynamic` 还必须区分：

- **合规层事实**：直接军事行动、代理人行动、核活动、航运/扫雷进展。
- **行为体压力变量**：美国、以色列、伊朗、代理人网络各自向打破僵局或续谈方向移动（权重：以色列 > 伊朗内部 > 美国）。
- **市场定价信号**：Brent、天然气、黄金、美元、航运保险、通胀预期等可观察反应。
- **路径判定与子分支**：当前处于 A 续期 / B 交战 / C 僵尸化 哪条路径，子分支为何，路径切换信号是否出现；路径与子分支概率调整必须回到当日证据。

### 6. 输出报告

读取通用 `references/report_template.md`，或按报告 profile 读取：

- `references/reports/ai_daily/template.md`
- `references/reports/macro_daily/template.md`
- `references/reports/iran_dynamic/template.md`

默认输出中文研究简报。关键证据使用：

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

`iran_dynamic` 报告必须把新闻证据写成 `时间 - 新闻 [来源]`，并包含路径判定（A 续期 / B 交战 / C 僵尸化）、当前子分支、战争烈度、今日边际变化、**框架演进与跟踪项结算（逐条结算上一期 open 项）**、各方行动、能源与市场反应、下一关键节点倒计时、路径子分支概率今日变动和 24-72h 观察清单。

### 7. 回写活动状态（watchboard）

`state_enabled` 的 profile（ai_daily / macro_daily / iran_dynamic）出完报告后,必须把今天的新 watchboard 存回去,供明天 carry-forward:

```bash
cat today_watchboard.json | python scripts/save_report_state.py \
    --profile iran_dynamic --date today --state-file -
```

watchboard 的 JSON 结构(regime / tracking_items / next_nodes / falsifiers / frame)与各 profile 的 frame 字段见 `references/reports/watchboard.md` 与各自 methodology。脚本做结构校验(必填字段、frame 字段齐全、概率求和、**上一期 open 项有没有被漏结算**),报错就按提示补全再存——它只查结构,不评判分析内容。`--check-only` 可只验证不写。

### 8. 按需生成 HTML（展示层）

当用户要 HTML、网页、可视化或截图风格的日报时，先写好并核对 `reports/<profile>_<date>.md`，再把它渲染成一份自包含单页 HTML（Claude UI 风格，图表不依赖外部 CDN，本地浏览器直接打开）。HTML 只是展示层：**不新增任何研报判断，也不删减 Markdown 正文**——共享渲染器会做文本保全校验，缺字报警告不阻断。

```bash
python scripts/render_report_html.py -i reports/macro_daily_2026-05-19.md
# 默认输出同名 .html；profile 从文件名前缀自动识别（ai_daily / macro_daily / iran_dynamic）
```

三个 profile 通用的处理：

- `一句话结论` 段自动升格成醒目的 hero 摘要卡。
- 表格里的 +/- 数值、涨跌方向自动染色；非加粗的分类格（如宏观日报「性质」列的"数据事件/政策表态"）渲染成彩色 pill。
- `--theme print` 出黑白衬线、A4 友好版，适合导出 PDF 或邮件附件；文件名非标准前缀时用 `--profile` 手动指定。

`iran_dynamic` 额外能画**路径概率图**（A 续期 / B 交战 / C 僵尸化 的概率条），把当天 watchboard 传进来即可：

```bash
python scripts/render_report_html.py -i reports/iran_dynamic_2026-06-04.md --watchboard today_watchboard.json
```

渲染框架来自仓库通用 `shared/html_report`（随 `shared` bundle 同步到 `scripts/_shared/html_report/`），与 A 股各 skill 共用同一套主题与图表工具；新增样式主题只需在该目录 `themes/` 下放一个 CSS 文件。

## 数据表说明

采集脚本默认写入 PostgreSQL；显式设置 `ALPHA_DB_BACKEND=sqlite` 时写入本地 SQLite fallback：

- `items`：统一新闻表。
- `items_fts`：FTS5 全文索引。
- `enrichments`：可选二次加工表，由 `enrich_targets.py` 写入，不改变原始 `items`。
- `report_state`：活动状态层(watchboard),按 `(profile, date_key)` 存每日分析状态;由 `save_report_state.py` 写入、`prepare_report_data.py` 读回,与原始 `items` 解耦。属运行时数据,不进发布包。

核心字段：

- `date_key`：Asia/Shanghai 日期，格式 `YYYY-MM-DD`。
- `source_type`：`cls`、`jin10`、`github_trending`、`rss`、`product_hunt`、`hacker_news`。
- `source_name`：具体来源名称。
- `published_at` / `fetched_at`：发布时间与抓取时间。
- `title` / `content` / `url`：主要文本与链接。
- `tags_json` / `metadata_json` / `raw_json`：结构化补充信息。

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

- `macro_news_items`：按宏观关键词筛选后的金十、财联社、RSS 新闻。
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
- 默认控制在 1500-2500 字；用户要求深度研究时可扩展。

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

- 一句话结论（轴一工程演进往哪走 + 轴二有无新形态/渗透变化 + 后续最该跟踪的对象）。
- 轴一证据：前沿实验室的 agent 能力进展 + 开源在解的工程痛点；同一矢量 ≥2 证据才判收敛。
- 轴二证据：有无新产品形态扩大渗透（载体/用户段/自主度/真实使用信号）；没有就直说无新形态。
- 证据层：新项目雷达（引擎 A）与重点公司动态矩阵（引擎 B）。
- 哪些只是注意力热度，哪些可能转成趋势或真实采用。
- 未来 24-72 小时三栏观察（待印证矢量 / 待观察形态 / 待发布动作）。

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

### 伊朗动态播报

用户：

```text
生成今天的伊朗动态播报，重点看霍尔木兹、油价和以色列独立行动风险。
```

执行：

```bash
export ALPHA_DB_BACKEND=postgresql
export ALPHA_PG_URL="postgresql://alpha_user:alpha_pass@localhost:5432/alpha_data"
python scripts/collect_news.py --date today --source all --only-missing
python scripts/prepare_report_data.py --profile iran_dynamic --date today --format markdown
```

宏观行情(Brent / 黄金 / 天然气 / USD-CNH / 美债 / BTC / 纳指期货)可通过本 skill 的 `macro_monitor.py` 取数,作为风险资产证据补充:

```bash
python scripts/macro_monitor.py market
```

加载：

- `references/reports/watchboard.md`（活动状态机制）
- `references/reports/iran_dynamic/methodology.md`（含冷启动种子）
- `references/reports/iran_dynamic/economic_impact_framework.md`
- `references/reports/iran_dynamic/template.md`

输出应包含：

- 当前阶段路径判定（A 续期 / B 交战 / C 僵尸化）与子分支。
- 战争烈度级别与趋势箭头。
- 今日边际变化（1-3 条；按 methodology"边际变化判定"三条线筛选）。
- 框架演进与跟踪项结算：逐条结算上一期 open 跟踪项，写清新开 / 关闭与框架微调。
- 美国、以色列、伊朗的核心行动（emoji 内嵌升级/缓和性质，不再独立板块）。
- 能源节点状态 + 价格变动 + 传导评估（统一为"能源与市场反应"）。
- 路径子分支概率今日变动、下一关键节点倒计时、24-72h 观察清单。

出完报告后用 `save_report_state.py` 回写今天的 watchboard，供明天 carry-forward。
