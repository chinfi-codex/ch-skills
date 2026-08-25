---
name: ch-gpu-compute-monitor
description: 当用户要跟踪 GPU 算力租赁的价格与可用供给、判断算力供需是紧还是松、看 B200/H200/H100 的租金走势与代际溢价、比较 Vast/Runpod/CoreWeave/Nebius 等平台的报价、观察 Ornn OCPI 成交价指数、判断 spot 折价与库存档位变化，或要生成 GPU 算力供需监控日报与 HTML Dashboard 时使用此 skill。适用提问包括“现在 H100 租金多少”“B200 价格在涨还是在跌”“GPU 算力紧不紧张”“算力价格拐点到了吗”“Vast 上 H200 报价分布怎么样”“Runpod 库存什么情况”“B200 相对 H200 的溢价在收窄吗”“spot 折价扩大了吗”“出一份今天的算力监控日报”“把算力监控导成网页”，以及配置每日定时采集、回填历史 90 天数据、补录 CoreWeave/Nebius 挂牌价、排查某个数据源为什么没采到数。脚本只做采集、标准化、确定性统计与渲染；“这是不是真拐点、要不要采信这个信号”由模型判断。不覆盖 GPU 芯片二级市场买卖价、云厂商股票分析、模型推理 token 价格；不给买卖建议、目标价或仓位。
---

# GPU 算力价格与供给监控

## 目标

1. **做什么**：把「实际成交价、市场化报价、标准云报价、可用供给」四类证据放进同一个口径框架，
   每日采集入库，产出证据包与日报，用来识别 GPU 算力从紧缺、平衡到宽松的边际拐点。
   覆盖 B200 / H200 SXM / H100 SXM 三个 MVP SKU，趋势窗口固定 90 天。
2. **不做什么**：不覆盖 GPU 芯片二级市场买卖价、云厂商股票、推理 token 价格；
   不给买卖建议、目标价、仓位；不在脚本里下结论；不用前值冒充最新值。
3. **给谁用**：需要判断算力成本与产能松紧的研究员、算力采购方、AI 基础设施投资人。

## 适用边界

- **时区与结算**：所有日期按 UTC。Ornn 的日度指数是 T-1 结算，所以「今天」的跨平台中枢
  结构性地不含 Ornn——这不是故障，报告里要说明锚定的是哪一天。
- **单位**：一律 USD / GPU·hour。整机报价除以 `node_gpu_count` 后入库，换算过程存在 `unit_basis` 里可审计。
- **SKU 粒度**：canonical id 到 SKU 级（`H100 SXM` 而不是 `H100`）。同日 Runpod 的
  SXM/NVL/PCIe 实测差 35%，合并会让「价格中枢」随混样比例漂移。
- **历史不同龄**：Ornn 是唯一有历史接口的源，免费层直接给滚动 3 个月日度历史，
  上线当天就有 90 天曲线；Vast 与 Runpod 只回当下快照，序列只能从首次采集当天
  开始往后长，**补不回去**。**报告里必须讲清这个不对称。**
- **脑 / 手边界**：脚本做采集、单位换算、分位数、变化率、折价、供给指数、评分算术、渲染。
  「这是不是真拐点、这个信号采不采信、缺的这块影响多大」全部由模型判断。
- **冷启动**：供给序列不足 21 天时评分会拒绝出数（`usable=false`）。这是正确状态，
  不要自己心算一个近似值填进去。
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

### 价格降了，先分辨成因

价格跌 + offer 份额涨 + 可用 GPU 涨 = 供给释放，这是真宽松；
价格跌但供给没动 = 更可能是需求走弱或单源促销；
价格跌只发生在一个源 = 先怀疑口径，再怀疑市场。
完整的读法、评分与告警怎么读、什么时候该说「不知道」，见 `references/signal_reading.md`。

## 工作流程

0. **首次部署先回填**：`python scripts/backfill.py`。把有历史接口的源（目前只有 Ornn）
   的 90 天日度历史一次拉满，并逐条报出每个 (source, gpu, price_type) 实际覆盖到哪天、
   多少个点、有没有缺口。日常不用重复跑。
1. **采集**：`python scripts/collect.py`。逐源独立，单源失败不阻断其它源，
   成败与耗时写进 `gpu_collect_runs`。
2. **算指标**：`python scripts/metrics.py --output evidence/gpu-<date>.json`。
   产出确定性证据包——变化率、分位数、折价、供给指数、评分分项、源健康度。
3. **读证据**：先看 `source_health` 判断今天数据完不完整，再看每个型号的
   `cross_platform_median.anchor_date` / `anchor_basis` 确认锚在哪一天什么口径，
   然后逐条检查 `changes.*.usable`。**`usable=false` 的百分比不许进结论。**
4. **判断**：按 `references/signal_reading.md` 的读法，分辨价格变动的成因，
   决定评分和告警采不采信，给出整体结论与三个型号各自的状态。
5. **写报告**：按 `references/report_template.md` 写 Markdown 到 `reports/gpu-<date>.md`。
   **frontmatter 的 `verdict` 块是仪表盘唯一认的判断契约**，必须填；正文写仪表盘表达不了的
   推理，不要重抄报价表和标准报价矩阵——那是仪表盘的活。
6. **出仪表盘**：`python scripts/render_report_html.py --evidence evidence/gpu-<date>.json
   --input reports/gpu-<date>.md`。

补录 CoreWeave / Nebius 挂牌价时，编辑 `config/attested_prices.yaml`（每条必须带 `as_of` 与
`source_url`），再重跑 collect 与 metrics。**动手前先读 `references/source_notes.md` 的
「2026-08-25 核对到的东西」**——CoreWeave 报整机价要除以 8、Nebius 报的本来就是单卡价，
这是抄错概率最高的地方。条目超过 30 天自动降级 `stale`，退出核心指标。

## 数据获取（脚本抓手）

### `scripts/collect.py` —— 每日采集

```bash
python scripts/collect.py                              # 采全部启用的源
python scripts/collect.py --sources ornn,runpod        # 只采指定源
python scripts/collect.py --date 2026-08-25            # 指定观测日
python scripts/collect.py --history-days 120           # Ornn 回填窗口
python scripts/collect.py --dry-run                    # 采但不写库
```

输出一份 JSON 摘要：每个源的 status（`ok` / `empty` / `failed`）、行数、耗时、
未映射的原始标识、降级说明。全部源都失败才返回非 0。

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
```

### `scripts/render_report_html.py` —— 单页 Dashboard

```bash
python scripts/render_report_html.py --evidence evidence/gpu-2026-08-25.json
python scripts/render_report_html.py --evidence … --input reports/gpu-2026-08-25.md
python scripts/render_report_html.py --evidence … --output docs/index.html
```

自包含单页，无外部依赖，用 claude 主题的暖色纸面。按 PRD §5 的硬约束渲染：
三型号同屏、无 GPU selector、无时间范围切换、窗口固定 90 天。五块面板依次是
判断区、成交价趋势、供给趋势、市场报价、标准报价矩阵、数据源状态。

**证据全部由脚本从 evidence 渲染，判断全部来自 `--input` 报告 frontmatter 的
`verdict` 块。** 不传 `--input` 也能出图，判断区会显示「报告未提供整体判断」——
脚本不会替模型编结论。

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

### 配置

| 文件 | 管什么 |
|---|---|
| `config/gpu_catalog.yaml` | canonical SKU 与各源别名映射、代际溢价对 |
| `config/sources.yaml` | 端点、认证、冻结的查询口径、质量过滤、样本量门槛 |
| `config/thresholds.yaml` | 评分权重与压缩尺度、告警规则与阈值、确认型拐点门槛 |
| `config/attested_prices.yaml` | 人工核对的 CoreWeave / Nebius / Crusoe 挂牌价 |

改 `sources.yaml` 里的 query 会改变 `query_fingerprint`，历史序列从此断成两段——
指标层不会拿不同指纹的观测相减。改之前先在 `references/source_notes.md` 记一笔。

## 输出规范

**结构**：按 `references/report_template.md` 的七个小节，顺序不变。
正文 900–1600 字，表格不计入。

**文风**：像跟懂行的人当面把事讲清楚——句子通顺、有逻辑衔接，给判断时把话说透。
不要模板腔，不要成段堆套话。同一维度的多个条目用 bullet，一条一项，
每条用完整通顺的话写完，**不要退化成「字段A - 字段B - 字段C」式的横杠拼接**；
结构化对照（平台 × GPU × 价格）才用表格。

**数据呈现**：价格两位小数，单位 USD/GPU·hour；百分比一位小数带正负号；
每个数字都要能在 evidence 里找到出处。

**必须做到**：
- 缺失就写「暂无数据 / 采集失败」，不用前值冒充最新值。
- 不可比的变化率写「不可比 + 原因」，不许填近似值。
- 状态用文字表达，不能只靠颜色。
- 标出锚定日与它比日历日晚几天。
- 说「可能是 X」时讲清凭什么，以及什么证据会推翻它。

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
