---
name: ch-gpu-compute-monitor
description: 当用户要跟踪 GPU 算力租赁的价格与可用供给、判断算力供需是紧还是松、看 B200/H200/H100 的租金走势与代际溢价、比较 Vast/Runpod/CoreWeave/Nebius 等平台的报价、观察 Ornn OCPI 成交价指数、判断 spot 折价与库存档位变化，或要生成 GPU 算力供需监控日报与 HTML Dashboard 时使用此 skill。适用提问包括“现在 H100 租金多少”“B200 价格在涨还是在跌”“GPU 算力紧不紧张”“算力价格拐点到了吗”“Vast 上 H200 报价分布怎么样”“Runpod 库存什么情况”“B200 相对 H200 的溢价在收窄吗”“spot 折价扩大了吗”“出一份今天的算力监控日报”“把算力监控导成网页”，也用于跟踪推理需求端的 token 量与价——看 AI token 用量在涨还是在跌、token 价格跌里有多少是真降价多少是往便宜模型迁移、重建 OpenRouter 全站名义 spend、看付费与免费 token 的结构、判断算力涨价是真需求拉动还是供给收缩。适用提问还包括“AI token 用量涨了多少”“token 价格跌 20% 是真降价吗”“推理需求还在扩张吗”“下游收入池撑不撑得住这么贵的算力”，以及配置每日定时采集、回填历史 90 天数据、补录 CoreWeave/Nebius 挂牌价、排查某个数据源为什么没采到数。脚本只做采集、标准化、确定性统计与渲染；“这是不是真拐点、要不要采信这个信号”由模型判断。不覆盖 GPU 芯片二级市场买卖价、云厂商股票分析、推理毛利率与单位 token 成本地板（需人工吞吐表，尚未启用）；不给买卖建议、目标价或仓位。
---

# GPU 算力价格与供给监控

## 目标

1. **做什么**：两端各一套证据。**成本端**把「实际成交价、市场化报价、标准云报价、
   可用供给」四类放进同一个口径框架，识别 GPU 算力从紧缺、平衡到宽松的边际拐点，
   覆盖 B200 / H200 SXM / H100 SXM 三个 MVP SKU；**需求端**把推理 token 的量与价
   放进另一套口径，回答「算力这么贵，下游用量和付费跟不跟得上」。
   成交价的趋势窗口固定 90 天；供给与 token 两条序列补不回去，一律出全部历史。
2. **不做什么**：不覆盖 GPU 芯片二级市场买卖价、云厂商股票、推理毛利率与单位 token
   成本地板（要人工吞吐表，尚未启用）；不给买卖建议、目标价、仓位；
   不在脚本里下结论；不用前值冒充最新值。
3. **给谁用**：需要判断算力成本与产能松紧的研究员、算力采购方、AI 基础设施投资人。

## 适用边界

- **时区与结算**：所有日期按 UTC。Ornn 的日度指数是 T-1 结算，所以「今天」的跨平台中枢
  结构性地不含 Ornn——这不是故障，报告里要说明锚定的是哪一天。
- **单位**：成本端一律 USD / GPU·hour（整机报价除以 `node_gpu_count` 后入库，换算过程
  存在 `unit_basis` 里可审计）；需求端一律 USD / Mtok 与 tokens/day。
  **两套单位不许出现在同一个减法里**——把它们接起来要等成本地板（P1，未启用）。
- **SKU 粒度**：canonical id 到 SKU 级（`H100 SXM` 而不是 `H100`）。同日 Runpod 的
  SXM/NVL/PCIe 实测差 35%，合并会让「价格中枢」随混样比例漂移。
- **历史不同龄**：Ornn 是唯一有历史接口的源，免费层直接给滚动 3 个月日度历史，
  上线当天就有 90 天曲线；Vast 与 Runpod 只回当下快照，OpenRouter 的日榜同样没有
  历史接口，序列只能从首次采集当天开始往后长，**补不回去**。**报告里必须讲清这个不对称。**
- **窗口只管成交价**：90 天窗口的前提是「掉出窗口的点明天还能重新取回来」，
  这只对 Ornn 成立。**可用供给、固定篮子真价格、混合价这三条一律出全部历史**——
  它们一旦被窗口滚过去，掐掉的那一段是永久丢失的，而且序列越早的一段越珍贵
  （首次采集日就是它的绝对起点）。供给面板会标出「全部历史 · 起止 · 天数」，
  写报告时不要把它们和旁边那张 90 天成交价曲线当同一个时间轴读。
  固定篮子与混合价**只出数不出图**：仪表盘给最新值与窗口变化，走势和两者之差
  （mix_shift）由正文用数字讲。
- **脑 / 手边界**：脚本做采集、单位换算、分位数、变化率、折价、供给指数、评分算术、渲染。
  「这是不是真拐点、这个信号采不采信、缺的这块影响多大」全部由模型判断。
- **token 历史是另一条序列**：日度模型级补不回去，但厂商级周度能回到 2025-09-01。
  两者口径不同，只有后者能回答"过去一年买家结构怎么搬的"——结构效应指数首跑
  100 → 44.8，光结构迁移一项就把均价拉低约 55%。**不要把它的百分比和日度
  `mix_shift` 放进同一个句子里比大小。**
- **token 侧的三条硬边界**：①OpenRouter 是 merchant API 层的偏斜样本，**不是全市场**
  （first-party 订阅、超大厂内部推理、企业直签合同都在外面）；②spend 是**按挂牌价计的
  名义支出**，不是实际账单——缓存命中只按约 12% 计费而命中率不可观测；
  ③`reasoning` / `cached` / `tool_calls` 三个字段源侧全为 0，**token 通胀拆不了**。
  这三句写报告时必须出现，完整口径见 `references/token_taxonomy.md`。
- **冷启动**：供给序列不足 21 天时评分会拒绝出数（`usable=false`）；token 序列不足 7 天时
  `mix_shift` 与三分解同样不出数。这是正确状态，不要自己心算一个近似值填进去。
- **告警未校准**：阈值是起始配置，默认 `record_only` 只入库不推送。积累 2–3 个月历史后才谈校准。

## 领域方法论

### 四层价格 + 一层供给

价格分四层：①市场成交价（Ornn OCPI）、②即时 Offer、③市场聚合报价（对 Offer 求分位数）、
④标准云报价（on-demand / spot / preemptible / committed）。供给不是第五种价格，
是横向指标，用来回答「价格在动，是需求变了还是供给变了」。

**②和③是同一批数据的两个视图，不是两个独立信号。** 评分时把两者都算进价格维度，
等于把 Vast 的同一批 offer 数了两遍。完整口径定义、八条可比性规则、折价的分子分母、
代际溢价怎么算，见 `references/pricing_taxonomy.md`。

### 可比性优先于精确性

跨平台比价出错，九成不是算错，而是把两个不同口径的数放进了同一个减法。
八条硬规则里最容易踩的三条：**同 price_type**（承诺期价不是按需价）、
**同 market_segment**（Runpod 的 secure 和 community 是两个市场）、
**同源集合**（跨平台中枢在源上下线那天会跳，实测能凭空「跌」25%）。

### 一致性才是拐点

单一价格信号下行不算拐点。确认型宽松要求价格类 ≥3 个信号 + 供给类 ≥2 个信号，
连续 ≥10 个真实采到数的日子，中间不允许缺口。同样的门槛反向用于确认型收紧——
**告警必须双边**：实测 2026-05~08 三个月 OCPI-B200 是 +23%、H100 +5%，方向是收紧，
单边告警在这种行情下会长期零触发。

### 参照系：不只看三个主力 SKU

`A100 SXM4` 与 `RTX 5090` 也进库（Ornn 免费层同样给 90 天历史，取它们零额外成本），
但不进首页三型号同屏。它们的用处是回答一个主力 SKU 自己答不了的问题：
**高端在涨，是某一代 GPU 结构性紧缺，还是整条算力曲线都在抬？** 如果连消费级的
RTX 5090 都在同步上行，那更像整体需求在抬。

### 需求端：量和价要一起看，价还要拆成两半

token 侧不做 `V × P` 那种指数——按用量加权的平均单价 ≡ 总支出 ÷ 总 token 数，
乘出来是恒等式，不可证伪。真正有信息量的是把价格拆开，**并行跑两个指数**：

- **混合价**（当期权重）= 名义 spend ÷ 付费 token。它会被购买结构迁移污染，
  **单看没有意义**——没有任何一家降价，它也能跌。
- **固定篮子真价格**（锁基期权重的 Laspeyres）。篮子成员是「模型家族 × 变体」，
  家族 = 剥掉日期后缀的 slug，所以家族内的版本升级降价算真降价，跨家族迁移不算。

**两者之差就是 mix_shift**，即"往便宜模型迁移"贡献了多少百分点。这是本 skill
唯一的原创信息：市场上争论 token 价格跌是不是 AI 见顶的人，没有一个把它拆开。

量的主轴只用 **paid**：实测零价 token 占 40.1%，其中免费档只有 7.8%，主体是匿名
stealth 模型在免费放量，它进出榜单就能让总量凭空跳一大截。

### 需求端还要看「谁在消费」

模型 × 变体答的是钱花在哪个模型上，答不了谁在花。调用方维度补上后一半：
实测榜上 20 个应用吃掉当日 38.0% 的 token，却只占 9% 的请求——agentic 应用单次调用
10 万量级的 token，比长尾对话高一到两个数量级。**token 增长里有多少来自"更多人用"、
多少来自"每次用得更重"，这是唯一能分辨的抓手。**

三条硬边界：份额的分母是**榜上应用的合计**而不是全站（榜单名次不连续，两头不靠）；
榜上合计占全站的比例是**下界**，剩下那部分混着未上榜应用、不露出的名次和没有 app
归属的直连流量，拆不开；这一路**没有 spend**，只能答"谁吃 token 最多"，
答不了"谁花钱最多"。完整口径见 `references/token_taxonomy.md` §五之四。

### 价格降了，先分辨成因

价格跌 + offer 份额涨 + 可用 GPU 涨 = 供给释放，这是真宽松；
价格跌但供给没动 = 更可能是需求走弱或单源促销；
价格跌只发生在一个源 = 先怀疑口径，再怀疑市场。
完整的读法、评分与告警怎么读、什么时候该说「不知道」，见 `references/signal_reading.md`。

## 工作流程

0. **首次部署先回填**：`python scripts/backfill.py`。两路历史一次拉满——Ornn 的 90 天
   日度成交价，以及 OpenRouter 的**厂商级周度量**（51 个已结算周，回到 2025-09-01，
   落进 `token_volume_history`）。逐条报出实际覆盖到哪天、多少个点、有没有缺口。
   日常不用重复跑。**注意 token 的历史与日度序列不是一条线**：口径不同（实测同日
   各厂商比值 1.42–1.75，非常数倍），只能看份额与增速，不能读绝对水平。
1. **采集**：`python scripts/collect.py`。逐源独立，单源失败不阻断其它源，
   成败与耗时写进 `gpu_collect_runs`。入库前跑一遍跨日离群检测，命中的行
   标 `quality_flag=suspicious` 并在摘要里列出来（打标不丢弃）。
2. **算指标**：`python scripts/metrics.py --output evidence/gpu-<date>.json`。
   产出确定性证据包——变化率、分位数、报价分散度、折价、供给指数与广度、
   评分分项、确认型拐点、源健康度，以及需求端的 `token_market` 段
   （量的三条线、双价格指数、mix_shift、名义 spend 与它的缓存敏感性带、
   日度模型构成，以及调用方维度 `token_market.apps`）。
   触发的告警同时写进 `gpu_alerts`；**token 侧本轮不设告警**，序列没长出来之前
   阈值无从谈起。
3. **读证据**：先看 `source_health` 判断今天数据完不完整，再看每个型号的
   `cross_platform_median.anchor_date` / `anchor_basis` 确认锚在哪一天什么口径，
   然后逐条检查 `changes.*.usable`。**`usable=false` 的百分比不许进结论。**
4. **判断**：按 `references/signal_reading.md` 的读法，分辨价格变动的成因，
   决定评分和告警采不采信，给出整体结论与三个型号各自的状态。需求端按第七节那张
   成因矩阵读：**GPU 涨价 + token 量涨 = 真需求拉动；GPU 涨价 + 量平 = 供给收缩**。
   混合价单独下跌不算降价，必须和固定篮子一起看。
5. **写报告**：按 `references/report_template.md` 写 Markdown 到
   `reports/gpu-<report_date>.md`。`report_date` 固定取开始生成报告时的本地执行日期，
   不从 `obs_date`、`evidence.asof` 或任一 `anchor_date` 推导；frontmatter 的 `date`
   与正文标题也用这个执行日期。另写 `data_asof: <evidence.asof>` 保留数据截止日，
   面板里的真实最新数据仍按各自 `anchor_date` 解释。
   需求端单独一小节，frontmatter 里对应 `verdict.token` 与 `verdict.panels.tokens`。
   **frontmatter 的 `verdict` 块是仪表盘唯一认的判断契约**，必须填；正文写仪表盘表达不了的
   推理，不要重抄标准报价矩阵——那是仪表盘的活。
6. **出仪表盘**：`python scripts/render_report_html.py
   --evidence evidence/gpu-<data_asof>.json --input reports/gpu-<report_date>.md`。
   HTML 默认同样输出为 `reports/gpu-<report_date>.html`，页头并列显示报告日与数据截止日。

补录 CoreWeave / Nebius 挂牌价时，编辑 `config/attested_prices.yaml`（每条必须带 `as_of` 与
`source_url`），再重跑 collect 与 metrics。**动手前先读 `references/source_notes.md` 的
「2026-08-25 核对到的东西」**——CoreWeave 报整机价要除以 8、Nebius 报的本来就是单卡价，
这是抄错概率最高的地方。条目超过 30 天自动降级 `stale`，退出核心指标。

## 数据获取（脚本抓手）

### `scripts/collect.py` —— 每日采集

```bash
python scripts/collect.py                              # 采全部启用的源
python scripts/collect.py --sources ornn,runpod        # 只采指定源
python scripts/collect.py --sources openrouter         # 只采需求端的 token 量价
python scripts/collect.py --date 2026-08-25            # 指定观测日
python scripts/collect.py --history-days 120           # Ornn 回填窗口
python scripts/collect.py --dry-run                    # 采但不写库
```

输出一份 JSON 摘要：每个源的 status（`ok` / `empty` / `failed`）、行数（价格 / 供给 /
token 三类分开计）、耗时、未映射的原始标识、降级说明。全部源都失败才返回非 0。

`openrouter` 这一路两个接口一起取：rankings 拿模型 × 变体级的日度 token 量，
`/api/v1/models` 拿挂牌价，按「剥掉日期后缀的 base + variant」join。**join 键少了
variant 就会错得很离谱**——`:batch` / `:free` 条目与标准条目共用同一个
`canonical_slug`（实测 69 处碰撞），标准流量会被按半价甚至零价计，spend 差 63%。
另外给 spend 前 10 名额外拉一次 `/endpoints`，量化默认价与各 provider 的价差带。

第三路 `rankings/apps?view=day` 补的是**调用方维度**——谁在消费这些 token。它只给
app_id、token 与请求数，不拆模型也没有价，落进 `token_app_observations` 单独一张表。
两个必须记住的事实：`total_tokens` 是**字符串**；返回的 20 行**不是前 20 名**
（实测 rank 跳过 2/3/4/15/16/18，有名次不公开露出），所以合计只是下界。
这一路是补强，取不到只降级成一条 note，不拖垮 token 主路。

### `scripts/backfill.py` —— 历史回填与覆盖体检

```bash
python scripts/backfill.py                    # 回填至今 90 天
python scripts/backfill.py --days 120         # 无 key 时 Ornn 会被钳到滚动 3 个月
python scripts/backfill.py --report-only      # 不取数，只看库里现在覆盖到哪
```

只有 `has_history_api: true` 的源会被回填；其余源会在 `no_history_api` 里列出
原因，而不是假装回填过。`coverage` 段逐条给出首末日期、点数与缺口天数，
缺口如实报出，不做插值。

### `scripts/metrics.py` —— 指标与证据包

```bash
python scripts/metrics.py --output evidence/gpu-2026-08-25.json
python scripts/metrics.py --date 2026-08-25 --window 90
python scripts/metrics.py --no-persist-alerts        # 只算不写 gpu_alerts
```

`--window` 只作用于成交价序列与 `window_high` / 回撤这类窗口内统计。供给与 token
的序列读到底、出到底，调大调小 `--window` 都不会改变它们画出来的长度。

默认会把当天触发的告警落进 `gpu_alerts`（先清当天旧行再写，避免调完阈值后
旧告警赖着不走）。确认型拐点不落新表，每次从观测重算，所以口径改了能直接重跑。

### `scripts/validate.py` —— 跨日离群检测（被 collect.py 调用）

采集器里的守卫拦的是「这条数据本身不对」；这里拦的是「数据长得正常、只是跟
自己的历史对不上」。命中打 `quality_flag=suspicious`，指标层排除出核心中枢，
但行留在库里可追溯。偏离倍数贴近 2/4/8/16 时单独点名为单位换算错——那是
整机价忘了除以卡数、或者多除了一遍的签名。阈值在 `config/thresholds.yaml`
的 `validation` 块，冷启动期（历史点不足）一律不标。

### `scripts/render_report_html.py` —— 单页 Dashboard

```bash
python scripts/render_report_html.py --evidence evidence/gpu-2026-08-25.json
python scripts/render_report_html.py --evidence … --input reports/gpu-2026-08-25.md
python scripts/render_report_html.py --evidence … --output docs/index.html
python scripts/render_report_html.py --evidence … --report-date 2026-08-30
```

自包含单页，无外部依赖，用 claude 主题的暖色纸面。按 PRD §5 的硬约束渲染：
三型号同屏、无 GPU selector、无时间范围切换；成交价固定 90 天窗口，
可用供给与 token 量价出全部历史（见「窗口只管成交价」）。token 那块只有量的
构成图，价格是指标格不是曲线；块内顺序是指标格 → 一年结构史 → 日度总量构成 →
量的分层 → 调用方。面板依次是判断区、
成交价趋势、标准报价矩阵、供给趋势、token 需求、数据源状态；**中间四块各占整行**，
每块底部带一行 ≤100 字的异动说明。

**证据全部由脚本从 evidence 渲染，判断全部来自 `--input` 报告 frontmatter 的
`verdict` 块**（顶部结论 + 三个型号的状态 + 四块面板的异动说明 `verdict.panels`）。
报告日期来自 frontmatter 的 `date`，未传报告或未写 `date` 时才退回本地执行日；
它不读取 evidence 的观测日期来命名产物。`data_asof` 只用于定位证据包与展示数据截止日。
不传 `--input` 也能出图，判断区会显示「报告未提供整体判断」、面板说明整行不出现——
脚本不会替模型编结论。异动说明超过 100 字会在输出摘要的
`panel_notes_over_limit` 里点名，但不截断。

### `scripts/daily_update.py` —— 给 cron 用的流水线

```bash
python scripts/daily_update.py                         # collect → metrics → 写 snapshot
python scripts/daily_update.py --skip-collect
```

### 环境变量

只有 `ALPHA_PG_URL` 是必需的（走 `shared/data/db_core.py` 统一契约）。三个 P0 源
全部匿名可用，一把 key 都不用配就能跑通；`ORNN_API_KEY`（解锁 3 个月以外的历史与
更多 SKU）/ `VAST_API_KEY` / `RUNPOD_API_KEY` 都是可选项，设了限频更宽松。
各源的实测契约、字段陷阱与降级策略见 `references/source_notes.md`。

### 数据表

| 表 | 装什么 |
|---|---|
| `gpu_price_observations` | 标准化后的价格观测，幂等键含 obs_date/source/gpu/price_type/segment/region |
| `gpu_supply_observations` | 供给观测：offer 数与份额、可用 GPU、库存档位、region 覆盖 |
| `gpu_collect_runs` | 每源每次采集的成败、耗时、行数、未映射标识 |
| `gpu_alerts` | 触发的告警，键 (obs_date, gpu_model, rule_id)，供跨日追溯 |
| `token_model_observations` | 需求端量价，键 (obs_date, source, model_slug, variant)，带 `coverage_scope` / `price_basis` 两个判据字段 |
| `token_volume_history` | 厂商级周度历史量，键 (week_start, source, author)，口径独立不与日度相减 |
| `token_app_observations` | 调用方（应用）日度量，键 (obs_date, source, app_id)，只有 token 与请求数、没有 spend |

### 配置

| 文件 | 管什么 |
|---|---|
| `config/gpu_catalog.yaml` | canonical SKU 与各源别名映射、代际溢价对 |
| `config/sources.yaml` | 端点、认证、冻结的查询口径、质量过滤、样本量门槛 |
| `config/thresholds.yaml` | 评分权重与压缩尺度、告警规则与阈值、确认型拐点门槛 |
| `config/attested_prices.yaml` | 人工核对的 CoreWeave / Nebius / Crusoe 挂牌价 |
| `config/token_basket.yaml` | token 价格指数的篮子（基期、成员粒度、在场权重下限）、缓存敏感性假设、覆盖率与集中度守卫、日度构成图的 top_n 与排名依据 |

改 `sources.yaml` 里的 query 会改变 `query_fingerprint`，历史序列从此断成两段——
指标层不会拿不同指纹的观测相减。改之前先在 `references/source_notes.md` 记一笔。

## 输出规范

**结构**：按 `references/report_template.md` 的七个小节，顺序不变。
正文 900–1600 字，表格不计入。

**文风**：像跟懂行的人当面把事讲清楚——句子通顺、有逻辑衔接，给判断时把话说透。
不要模板腔，不要成段堆套话。同一维度的多个条目用 bullet，一条一项，
每条用完整通顺的话写完，**不要退化成「字段A - 字段B - 字段C」式的横杠拼接**；
结构化对照（平台 × GPU × 价格）才用表格。

**数据呈现**：GPU 价格两位小数、单位 USD/GPU·hour；token 价格三位小数、
单位 USD/Mtok；token 量用 T / B 缩写；百分比一位小数带正负号；
每个数字都要能在 evidence 里找到出处。**两套单位不许出现在同一个减法里。**

**必须做到**：
- 缺失就写「暂无数据 / 采集失败」，不用前值冒充最新值。
- 不可比的变化率写「不可比 + 原因」，不许填近似值。
- 状态用文字表达，不能只靠颜色。
- 标出锚定日与它比日历日晚几天。
- 说「可能是 X」时讲清凭什么，以及什么证据会推翻它。
- 写到需求端就得把三句边界说出来：merchant API 层的偏斜样本而非全市场、
  spend 是名义挂牌价口径而非实际账单、reasoning/cached 拿不到所以 token 通胀拆不了。

## 示例

**输入**：「出一份今天的 GPU 算力监控日报」

**做法**：`collect.py` → `metrics.py` → 读证据 → 写 `reports/gpu-<date>.md`。

**冷启动第一天的正确产出长这样**（真实运行结果）：

> 顶部判断写「暂不定论」，因为供给序列只有 1 天历史，评分三个型号全部
> `usable=false`，blockers 是「在场供给信号 0 个」。
> 价格侧可以讲：锚定 2026-08-24（Ornn T-1 结算，比日历日晚 1 天），
> H100 SXM 7D +9.5%、B200 7D +6.2%、H200 SXM 7D −13.4%，
> 三个型号里只有 H200 触发了快速降价告警，且模式是 `record_only`。
> 标准报价层有三家（Runpod 直采，CoreWeave / Nebius 人工核对于 2026-08-25），
> 折价维度因此有数：CoreWeave 的 spot 折价 H100 −60.0% / H200 −58.5% / B200 −50.4%，
> B200 折价最小说明闲置最少，跟 Runpod 那边 B200 库存 Low 互相印证——
> **这是当天唯一一个有交叉验证的判断，可以写实**。

**边界示例**：用户问「H100 现在多少钱」这类单点查询，直接跑 collect + metrics 后
报锚定日的跨平台中枢和各源报价即可，不用写完整日报。
