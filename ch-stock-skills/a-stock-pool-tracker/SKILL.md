---
name: a-stock-pool-tracker
description: A 股涨停强势股票池的入池、跟踪、移除与介入提醒。规则入池条件为收盘涨停、非一字板、成交额 ≥ 3 亿、换手率 < 15%、10:30 前首次封板，也支持手动加股；每日跟踪池内个股走势（相对涨停价/涨停日最低价/均线的位置、回撤、量能）与消息面（公告、互动易、快讯、联网催化），按硬规则把走势走坏的票移出池，并只在"放量突破平台"或"缩量回踩支撑"两种形态出现时给出介入提醒参考，同时挂上 daily-market-sense 的主线生命周期标签。当用户说"扫一下今天的涨停入池""更新/跟踪我的股票池""池子里哪些票走坏了要移除""把 XX 加进池子/移出池子""池里有没有突破或缩量回踩的票""池内股票最近有什么消息""涨停股池日报"时使用。不用于单只股票的基本面深度研究（用 a-stock-analyzer）、全市场盘面复盘与主线判定（用 a-stock-daily-market-sense）、美股观察池（用 chstock-usmarket-report）。脚本只做取数、规则筛选与指标计算；题材归属、走势定性、消息定性和提醒措辞由模型完成。不构成买卖建议。
---

# A 股涨停强势股票池

## 目标

1. **做什么**：维护一个有进有出的强势股池，每天回答四个问题：
   - 今天哪些涨停票按规则进了池，各自凭什么涨、属于哪条主线；
   - 池内每只票走势健不健康，消息面有没有新催化或利空；
   - 哪些票走坏了被移出，或进了预警；
   - 哪些票出现了**放量突破**或**缩量回踩**，值得盯盘，失效价位在哪。
2. **不做什么**：不给仓位和目标价，不写"买入/卖出"指令；不在脚本里写判断；不编造未检索到的催化。
3. **给谁用**：做 A 股强势股和产业趋势波段的交易者。他们需要一份能每天打开扫一眼的池子日报，而不是一堆数字。

## 适用边界

- 默认处理最近一个已收盘的交易日；用户给日期时取该日或之前最近的交易日。盘中运行时当日日线不完整，提醒用户收盘后再跑。
- 价格一律前复权到最新交易日；入池时记录的锚点价（涨停价、涨停日最低价、启动价）同口径换算，除权不会让防守线错位。
- 成交额单位为亿元，换手率用 Tushare `daily_basic.turnover_rate`（可在规则文件里改成自由流通口径）。
- **首次封板时间取不到就不入池**。Tushare `limit_list_d` 优先，JRJ 涨停池兜底；两个都失败时 `degraded=true`，报告必须写明"今日未执行入池扫描"，不能写成"今日无票入池"。
- 停牌的票当天不判规则，放进 `suspended` 列表，只在报告里提一句。
- **脑 / 手边界**：入池、移除、介入信号是写在 `assets/pool_rules.yaml` 的硬规则，脚本执行，可复现、可回测；涨停原因、主线归属、走势四档、消息定性、提醒怎么措辞，全部由模型判断。

## 领域方法论

### 1. 入池：只收"早、强、有量、筹码不松"的涨停

规则（`pool_rules.yaml → entry`）：收盘涨停、非一字板、非 ST、上市满 20 天、成交额 ≥ 3 亿、换手率 < 15%、首次封板 ≤ 10:30。

背后的逻辑是：10:30 前封板说明资金主动、不是尾盘偷袭；3 亿成交额保证这是机构和游资都能参与的容量票；换手率 15% 以下说明封板时筹码没有大幅松动；一字板没有承接信息，也买不进，所以不收。

- 已经在池里的票再次涨停，**只记一笔 relimit 事件，不刷新锚点**，防守线仍然是第一次入池那天的。
- 被移除的票之后再次满足规则，会作为新的一段（新 `entry_date`）重新入池，老记录保留。
- 手动加股（`pool_cli.py add`）必须写理由。锚点默认取加入当天的收盘价和最低价，可以用 `--anchor-low` 指定自己的防守价。

每只新入池的票，模型要查清楚**为什么涨停**（见第 4 节消息面），然后用 `pool_cli.py annotate` 写入一句话理由和主线归属。主线归属的方法：读 `pool_scan` 输出里的 `theme_context`（daily-market-sense 当天的主线生命周期），按公司主营和这次涨停的催化，把票挂到最贴切的 `theme_id` 上；不属于任何现有主线就只写 `--theme-name`，并在报告里说明这是"池外新题材"。`theme_context.stale=true` 时说明主线数据不是当天的，要在报告里注明日期。

### 2. 走势跟踪：四档定性

`pool_track.py` 给每只票输出 `metrics`：入池以来涨幅与最高涨幅、距高点回撤、距涨停日最低价和启动价的位置、MA5/10/20 站上与否、连续收在 MA20 下方的天数、量比（对前 5 日均额）、对涨停日的量比、平台高点和横盘天数、是否再次涨停。

模型据此把每只票定为**健康 / 分歧 / 预警 / 走坏**四档之一，写一句理由，并用 `pool_cli.py verdict` 回写。判断口径和典型形态读 `references/trend_judgement.md`。要点：

- 先看位置再看量。离涨停价和 MA10 越近、量越小，越可能是良性整理；放量跌、跌破涨停日最低价，才是出货的信号。
- 主线状态是背景。同样的缩量整理，主线在"主线确认"阶段是蓄势，在"退潮"阶段就要往坏处想。
- 脚本的 `decision` 是规则结论，模型的四档是解读。二者可能不一致（比如规则仍是 active，但模型判"预警"），这很正常，报告里把理由讲清楚即可，**不要改规则结论**。

### 3. 移除：两段式

规则（`pool_rules.yaml → exit`）：

- **直接移除**：收盘跌破涨停日最低价；或距入池后最高收盘回撤 ≥ 20%。
- **先进预警**：连续 2 日收在 MA20 下方；放量大阴线（跌幅 ≤ -5% 且成交额 ≥ 前 5 日均额 2 倍）；时间止损（入池满 15 日、10 日没创新高、且跌破 MA10）。预警连续 3 个交易日没修复就移除，预警条件全部消失则回到 active。
- **手动股不自动移除**：命中任何移除条件只进预警，并打上 `suggest_remove`，报告里写"建议移除"，由用户决定。
- **实质利空由模型认定**：立案调查、重大减持、业绩爆雷、核心题材被公司正式否认等，模型确认后执行 `pool_cli.py remove --reason "..."`，理由里写清证据来源。

### 4. 消息面：分层取数，按需深挖

不是每只票每天都查全部来源。`pool_track` 输出的 `deep_news_targets` 列出今天需要深挖的票（新入池、再涨停、出现介入信号、状态变化或预警、单日涨跌超过 7%）。另外每周五对全池做一次全量扫描。

| 层级 | 来源 | 覆盖 |
|---|---|---|
| 每日必做 | 巨潮公告（`chstock-cninfo-announcement`） | 全池，查最近 3 个交易日 |
| 每日必做 | 金十快讯（`chstock-market-telegraphs`），按股票名和代码过滤 | 全池 |
| 按需 | 互动易问答（`chstock-interactive-qa`） | `deep_news_targets` |
| 按需 | 联网检索（`scripts/web_search.py`） | `deep_news_targets`，查涨停原因和催化 |

每条有价值的消息，模型都要归入**催化兑现 / 新催化 / 题材证伪 / 实质利空 / 中性**之一，用 `pool_cli.py event` 记入台账，附来源和链接。分类口径和利空认定见 `references/news_triage.md`。查不到消息就写"未检索到明确消息"，不要编。

### 5. 介入提醒：只认两种形态

规则（`pool_rules.yaml → signal`），只对当天仍为 active 的票计算：

- **放量突破**：入池后的平台高点之后至少横盘 3 天，今天收盘站上平台高点，且成交额 ≥ 前 5 日均额 1.5 倍。失效价为平台高点下方 3%。
- **临近突破**：收盘距平台高点不到 3%，且量缩到前 5 日均额 0.8 倍以下。这是预备提醒，告诉用户明天盯什么价位。
- **缩量回踩**：距入池后最高收盘至少回撤 5%，当日最低价踩到支撑位（涨停价、MA10、MA20）上方 2% 以内，收盘守住支撑位，量缩到前 5 日均额 0.7 倍或涨停日的一半以下，当日跌幅不超过 5%。失效价为支撑位下方 3%。

脚本只给命中事实、触发价和失效价。模型要结合主线状态、消息面和走势四档，判断这个信号的**质量**，并把提醒写成人话。如何评估质量、哪些情况要降级或不提，读 `references/entry_signals.md`。提醒永远是"参考"：写清楚触发了什么、哪里失效、有什么风险，不写"建议买入"。

## 工作流程

每日盘后按顺序执行：

1. **入池扫描**：`python scripts/pool_scan.py --output reports/<date>/scan.json`。看 `degraded`、`added`、`near_miss`。
2. **补入池理由**：对 `added` 里每只票查涨停原因（公告、快讯、联网），用 `pool_cli.py annotate <code> --reason ... --theme-id ...` 写入。
3. **每日跟踪**：`python scripts/pool_track.py --output reports/<date>/track.json`。规则状态转移在这一步自动落库；只想看结果不改状态时加 `--dry-run`。
4. **消息面**：全池查公告和快讯；对 `deep_news_targets` 查互动易和联网。有价值的消息用 `pool_cli.py event` 记录；认定实质利空的用 `pool_cli.py remove`。
5. **定性回写**：逐只给出四档定性，`pool_cli.py verdict <code> --verdict 分歧 --note "..."`。
6. **写日报**：按输出规范写，保存到 `reports/<date>/pool_report.md`。

用户只问某一件事时（例如"把 XX 加进池子""池里有没有回踩的票"），只做对应的步骤，不必出整份日报。

## 数据获取

```bash
# 入池扫描（默认最近交易日）
python scripts/pool_scan.py [--date 2026-10-23] [--dry-run] [--output reports/2026-10-23/scan.json]

# 每日跟踪
python scripts/pool_track.py [--date 2026-10-23] [--codes 600001.SH,300001.SZ] [--dry-run] [--output ...]

# 池子维护
python scripts/pool_cli.py list [--status active,watch,removed]
python scripts/pool_cli.py show 600001
python scripts/pool_cli.py add 600001 --reason "固态电池中试线落地，跟踪二波" [--anchor-low 18.5]
python scripts/pool_cli.py annotate 600001 --reason "..." --theme-id TH-固态电池 --theme-name 固态电池
python scripts/pool_cli.py remove 600001 --reason "公司公告收到立案告知书（巨潮 2026-10-23）"
python scripts/pool_cli.py event 600001 --category 新催化 --title "..." --source 巨潮 --url https://...
python scripts/pool_cli.py verdict 600001 --verdict 分歧 --note "缩量回踩 MA10 未破"
python scripts/pool_cli.py themes [--date 2026-10-23]

# 消息面（同级 skill 的脚本）
python ../chstock-cninfo-announcement/scripts/cninfo_announcement_search.py --tabtype fulltext --date 2026-10-21~2026-10-23 --stock 600001
python ../chstock-interactive-qa/scripts/interactive_qa_search.py --company 公司名 --date-from 2026-10-16 --date-to 2026-10-23
python ../chstock-market-telegraphs/scripts/market_telegraph_fullfeed.py --hours 24 --format normalized
python scripts/web_search.py "公司名 涨停 原因" --topic news --days 3
```

环境：`TUSHARE_TOKEN`（必需）、`ALPHA_PG_URL`（股票池状态库，见仓库 `shared/data/POSTGRESQL.md`）、`TAVILY_API_KEY`（联网检索）。数据表 `pool_member` / `pool_daily` / `pool_event` 首次运行时自动建表。日线优先读本地 `stock_daily` + `stock_adj_factor`，缺最新一天时走 Tushare。

关键输出字段：

- `pool_scan`：`added[]`（含首次封板时间、成交额、换手率）、`already_in_pool[]`、`near_miss[]`（只差一条规则的涨停票）、`reject_counts`、`theme_context`、`degraded`
- `pool_track`：`members[].metrics / exit_hits / decision / signals / theme_state / recent_events`，以及汇总的 `transitions[]`、`signals[]`、`deep_news_targets[]`、`suspended[]`、`errors[]`

## 输出规范

遵循仓库项目级文风默认：讲人话，少用模板腔；同类条目用 list，每条写成通顺完整的话，不要退化成"字段A - 字段B"的横杠拼接；结构化对照才用表格。

日报结构见 `references/report_template.md`，纲要如下：

1. **一句话结论**：今天池子整体是进攻还是退守，最值得盯的一两只票。
2. **介入提醒**：先写这一节，因为它最需要行动。每条说清形态、触发价、失效价、主线背景，以及模型对信号质量的判断。没有信号就写一句"今日无突破或缩量回踩信号"。
3. **今日新入池**：逐只写涨停原因和主线归属。
4. **移除与预警**：逐只写触发了哪条规则，手动股写"建议移除"。
5. **池内全景表**：代码、名称、入池天数、入池以来涨幅、距高点回撤、四档定性、主线状态。
6. **消息面**：只写有增量的消息，按票归类，标出分类和来源。
7. **数据说明**：降级、停牌、主线数据日期不一致等情况。

纪律：

- 每个判断都回到数据或来源；一段话里最多带两三个关键数字，其余放表格。
- 介入提醒必须同时写触发价和失效价，并在节末注明"以上为形态提醒，不构成买卖建议"。
- 脚本的规则结论和模型的定性分开写，不要混为一谈。

## 示例

### Input

> 收盘了，跑一下股票池，今天有新进的吗？池里有没有能上车的？

### 执行

```bash
python scripts/pool_scan.py --output reports/2026-10-23/scan.json
python scripts/pool_cli.py annotate 603xxx.SH --reason "..." --theme-id TH-机器人
python scripts/pool_track.py --output reports/2026-10-23/track.json
python ../chstock-cninfo-announcement/scripts/cninfo_announcement_search.py --tabtype fulltext --date 2026-10-21~2026-10-23 --stock 603xxx
python scripts/pool_cli.py verdict 300xxx.SZ --verdict 分歧 --note "缩量回踩涨停价"
```

### Output 片段

```markdown
# 涨停强势池日报 · 2026-10-23

==今天池子偏进攻：新进 3 只，全部是 10 点前封板的机器人链条票；池内最值得盯的是 300xxx，第三次缩量回踩涨停价没破，主线也还在"主线确认"阶段。==

## 介入提醒（形态参考）

- **300xxx 某某科技 · 缩量回踩涨停价**：从高点回撤了 9%，今天最低踩到 25.10，离涨停价 24.80 只差 1%，收盘又拉回 25.60，成交额只有前 5 日均额的六成。它所在的人形机器人主线仍处在"主线确认"阶段，这次回踩更像洗盘而不是出货。失效价 24.06，收盘跌破就说明支撑没守住。
- **603xxx 某某股份 · 临近突破**：在 18.9 附近横了 5 天，量越来越小，今天收在 18.55。明天如果放量站上 18.9 就是突破；但公司昨晚刚公告股东减持 1%，冲高时要留意抛压。

> 以上为形态提醒，不构成买卖建议。
```
