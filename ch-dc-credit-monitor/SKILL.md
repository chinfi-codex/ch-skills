---
name: ch-dc-credit-monitor
description: 当用户要跟踪数据中心与 AI 基建的债券信用、看融资成本在往哪走、判断 AI 资本开支有没有被债市单独定价、比较超大厂与纯算力商的利差梯级、观察 CoreWeave/Oracle/Meta/Equinix/Digital Realty 的信用曲线形状、判断某只债走宽是市场的事还是这家公司的事、跟踪 GPU 抵押融资载体与数据中心 SPV（如 Meta Hyperion 的 Beignet Investor）的条款与租户锚，或要生成数据中心信用监控日报与 HTML Dashboard 时使用此 skill。适用提问包括“数据中心债券利差怎么样”“AI 基建的债贵不贵”“CoreWeave 融资成本多少”“Oracle 的利差为什么这么宽”“Meta 的 AI capex 被债市定价了吗”“超大厂之间的信用差在拉大吗”“CRWV 的曲线倒挂了没”“利差走宽是不是这家公司自己的问题”“公用事业的利差反映数据中心负荷了吗”“Beignet 那笔 270 亿的 SPV 现在什么情况”“GPU 抵押贷款的抵押品还值多少”“出一份今天的数据中心信用日报”“把信用监控导成网页”，也用于配置每日采集、排查某个源为什么没采到数、补录 SPV 台账条款。脚本只做采集、标准化、确定性统计与渲染；“这算不算信用事件、体系该定几档、要不要采信这个信号”由模型判断。不覆盖股票分析与估值（用 us-ai-semi-earnings 或 chstock-usmarket-report）、CDS 与逐笔 TRACE 成交（已实测确认无免费源）、一级市场发行定价与发行折价（无免费结构化源，需人工录入）、数据中心 ABS 与 REIT 运营指标（本轮范围外）；不给买卖建议、目标价或仓位。
---

# 数据中心信用监控

## 目标

1. **做什么**：把 AI 基建的信用侧放进一把统一的标尺。覆盖三个体制——**公开公司债**
   （超大厂 MSFT/AMZN/GOOGL/META/ORCL、数据中心 REIT DLR/EQIX、受监管公用事业
   D/AEP/ETR、纯算力商 CRWV）、**可转债**（NBIS 与 CRWV/DLR 的转债栈）、
   **GPU 抵押融资载体与 SPV**（CoreWeave 的 DDTL 系列、Meta Hyperion 的 Beignet）。
   趋势窗口固定 90 天。
2. **不做什么**：不做股票分析与估值；不覆盖 CDS、逐笔 TRACE、一级市场发行定价、
   DC ABS 与 REIT 运营指标；不给买卖建议、目标价、仓位；不在脚本里下结论；
   不用前值冒充最新值。
3. **给谁用**：需要判断 AI 基建融资成本与杠杆迁移的信用研究员、固收投资人、
   算力产业分析师。

## 适用边界

- **这套框架回答三个问题**，不是「哪只债便宜」：这个体系的融资成本在往哪走、
  抵押品还值不值那个钱、表外那部分在扩张还是收缩。
- **锚定日不对齐是常态。** SPDR 持仓给的是当日 As of，FRED 国债曲线是 T-1 结算。
  实测 `curve_lag_days = 1`。报告里必须说明锚在哪一天，**不要拿一周前的曲线去减
  今天的收益率**——那算出来的「利差变动」其实是利率变动，所以超过 5 天的滞后
  直接不出利差。
- **三层数据的时间常数不同，不能等权。** 二级利差日频、领先但噪音大；
  一级市场事件驱动、领先且低噪音但稀疏；基本面与抵押品季度、滞后但确定。
  裁决规则见 `references/credit_framework.md` §五：**低频层确认高频层，不是反过来。**
- **单位**：利差一律 bp，收益率 `%`，金额 `$Xmn`。**G-spread 不是 OAS**——
  含赎回、浮息、次级永续的券算出来有偏，指标层已剔除并标 `option_biased`，
  但报告里仍要交代这个口径。
- **关联键必须落到 ISIN。** ORCL 样本内有 51 只债，按发行人聚合会把 2030 和 2052
  混成一个数。发行人层的任何聚合都要同时报出成分只数。
- **脑 / 手边界**：脚本做采集、单位换算、YTM 与利差、分桶、固定期限插值、
  beta/alpha 分解、跨档距离、阈值穿越事件、渲染。
  「这算不算信用事件、体系该定几档、这个信号采不采信」全部由模型判断。
- **冷启动**：只有一个采集日时判据 1（alpha 分解）全部 `insufficient_history`，
  这是正确状态，不要自己心算一个近似值填进去。首日能讲什么、不能讲什么，
  见 `references/credit_framework.md` §十一。
- **告警未校准**：阈值是起始配置，`events.mode` 默认 `record_only`。
  积累 2–3 个月历史后才谈校准。

## 领域方法论

完整方法论在 `references/credit_framework.md`。这里只留纲要。

### 先有标尺，才谈变化

单看「ORCL 205bp」读不出任何东西。有意义的是它在梯级里的位置，以及这个位置在移动。
七档标尺（2026-08-25 实测，5–10Y）：MSFT 39 → AMZN/GOOGL 68/69 → 公用事业 74–82 →
META 97 → EQIX 107 → ORCL 205 → CRWV 775。

这张表本身就推翻两个常见预设：**受监管公用事业身上没有 AI 溢价**（贴着 IG 指数
81bp，所以它走宽不能归因到 AI，是反向证伪器）；**超大厂内部已经裂开**
（META 对 GOOGL 在 5–10Y 差 28bp，到 20Y+ 差 72bp，长端先动）。

### 五个动态判据

1. **alpha 分解** —— `Δ总 = 市场beta + 档位beta + alpha`。只有 alpha 连续同向累积
   才构成个体信用事件；单日 alpha 一律不定性，ETF 估值有粘滞。
2. **梯级离散度** —— 扩张 + 弱档走宽是质量分层；压缩 + 全档同向走宽是体系性；
   压缩 + 全档同向收窄是追逐收益。**三种读法完全不同。**
3. **曲线形状** —— 短端反超长端（倒挂）是高收益债里误报率最低的信号。
   **CRWV 曲线倒挂是整个监控里优先级最高的单一事件。**
4. **长短端分化** —— 长端先动。差值收窄（短端追上来）= 担忧前移，是升级信号。
5. **抵押品剪刀差** —— 账面按直线折旧走，租金按市场走，裂口就是残值高估的累积。
   本轮只到抵押品规模，剪刀差留插槽未实现。

### 三条最容易犯的错

- **rolldown 不是信用改善。** 债券在曲线上往下滚，利差会自然收窄。跟踪单只 ISIN
  的历史必然把它混进重定价。发行人层的时间序列**只能**走固定期限插值点
  （`constant_maturity_bp`），超出观测跨度 3 年的期限一律不外推。
- **转债的 G-spread 不是信用利差。** 1.75% 票息的债贴着面值，按直债折现算出来的
  收益率远低于国债，裸 G-spread 会是负几百 bp——那是期权价值不是信用。用发行人
  自己的直债曲线做参照可得期权价值下界（同期限直债利差 − 转债 G-spread）。
  实测 CRWV 两只转债价格看着贴债底（104.60 / 96.95），期权价值却有 975–1132bp，
  **当前宇宙里没有一只转债的信用信息可提取**。
- **组合级不能归因到单体。** Beignet 之后并入「8 项投资、15 处物业」的合计行，
  任何引用都要带这个限定。

### SPV 的风险在「利差不会动」里

SPV 的信用不是独立变量，是「租户信用 × 结构增信 × 抵押品残值」的函数。它不交易、
不再单独披露，所以敞口是真的、价格是冻的。**恶化的第一信号是租户长端走宽，
不是 SPV 自己。** 跟踪量是 `spv[].coupon_vs_tenant_bp`：实测 Beignet 票息 6.581%
比租户 META 同期限（22.8Y）的 6.815% 低 23bp——补偿偏薄，而且这个差价无法套利，
只意味着按市值计的损失躺在 sponsor 账上没被观测到。

## 工作流程

1. **采集**：`python scripts/collect.py --with-sec`。逐源独立，单源失败不阻断其它源，
   成败与耗时写进 `dc_collect_runs`。价格连续多日不动会标 `stale`（打标不丢弃）。
2. **算指标**：`python scripts/metrics.py --output evidence/dc-<date>.json`。
   产出确定性证据包——梯级、跨档距离、离散度、各发行人曲线、长短端分化、
   alpha 分解、转债位置、GPU 抵押品、SPV 租户锚、阈值穿越事件、源健康度。
3. **读证据**：**先看 `source_health` 和 `quality_summary` 判断今天数据完不完整**，
   再看 `anchors` 确认锚在哪一天、比日历日晚几天，然后逐段检查 `quality`。
   **`quality` 不是 `ok` 的字段不许进结论。** 判据与字段的对照表见
   `references/credit_framework.md` §十。
4. **判断**：按 `references/credit_framework.md` 的五个判据逐条过，再按 §五 的裁决
   规则处理三层冲突，最后按 §四 的状态机定档（或明说为什么定不了）。
   **每条判断都要跟一个证伪条件。**
5. **写报告**：按 `references/report_template.md` 的七节写到 `reports/dc-<date>.md`。
   **frontmatter 的 `verdict` 块是仪表盘唯一认的判断契约**，必须填。
6. **出仪表盘**：`python scripts/render_report_html.py --evidence evidence/dc-<date>.json
   --input reports/dc-<date>.md`。默认出日频精简页；要查明细加 `--full`。

补录 SPV 台账时编辑 `config/universe.yaml` 的 `spv` 段（每条必须带 `disclosed_in`
与 `disclosure_date`），再重跑 collect 与 metrics。**动手前先读
`references/source_notes.md`**——Beignet 的数据门在投资方 Blue Owl 的申报里，
不在发行人也不在 Meta，这是找错地方概率最高的一处。

## 数据获取（脚本抓手）

### `scripts/collect.py` —— 每日采集

```bash
python scripts/collect.py                 # 市场层：SPDR + FRED
python scripts/collect.py --with-sec      # 加基本面 + GPU 抵押载体 + SPV 台账
python scripts/collect.py --dry-run       # 不落库
```

六只 SPDR ETF（spbo/spib/splb/sphy/jnk/cwb）按 ISIN 去重，实测约 319 只工具。
价格是导出来的：`clean_price = Market Value / Par × 100`，持仓表没有价格列。

### `scripts/metrics.py` —— 指标与证据包

```bash
python scripts/metrics.py --output evidence/dc-2026-08-26.json
python scripts/metrics.py --asof 2026-08-26 --window 90
```

### `scripts/render_report_html.py` —— 单页 Dashboard

```bash
python scripts/render_report_html.py --evidence evidence/dc-2026-08-26.json          # 精简页（默认）
python scripts/render_report_html.py --evidence … --input reports/dc-2026-08-26.md   # 带模型判断
python scripts/render_report_html.py --evidence … --full                             # 完整明细版
```

**默认输出精简页，这是日频追踪该有的形态。** 四块：判断区、核心刻度、
今天值得看的、一行数据状态。核心刻度是唯一的一张表，13 行——七档梯级、
四个跨档距离、离散度、SPV 租户差——每行带 `1D / 1W / 1M / vs 锚点` 四档变化。
**水平值本身信息量很低，驱动判断的是位移**，所以变化量才是这张表的主体。

明细（逐只债、11×12 发行人曲线表、转债逐只、GPU 工具级债务、SPV 卡片）属于
**查证深度不是日频信息**，留在证据包与报告正文里；需要时 `--full` 渲染完整版
（含对数纵轴的信用曲线全景图），输出到 `dc-<date>-full.html`，不覆盖日频页。

自包含单页无外部依赖，claude 主题暖色纸面。**证据全部由脚本从 evidence 渲染，
判断全部来自 `--input` 报告 frontmatter 的 `verdict` 块**；不传 `--input` 也能出图，
判断区会显示「报告未提供整体判断」——脚本不会替模型编结论。异动说明超过 100 字
会在输出摘要的 `panel_notes_over_limit` 里点名，但不截断。

`disclosure_once` 的记录有单独的守卫函数拒绝画成折线。

### 环境变量

`ALPHA_PG_URL` 是必需的（走 `shared/data/db_core.py` 统一契约）。
`SEC_CONTACT_EMAIL` 是 **R-file 路径的硬依赖**——`www.sec.gov/Archives` 没有邮箱 UA
一律 403，缺了会被前置拦下标 `missing_contact` 而不是撞三次 403；
`data.sec.gov` 的基本面那一路不受影响。其余全部免费无密钥。

### 数据表

| 表 | 装什么 |
|---|---|
| `dc_instruments` | 工具主数据，主键 ISIN；含档位、体制、含权标记、担保类型 |
| `dc_observations` | **长表**观测，键 (asof_date, instrument_key, metric)，带 method/source/quality/staleness |
| `dc_collect_runs` | 每源每次采集的成败、耗时、行数、篮子指纹 |
| `dc_events` | 阈值穿越事件，键 (asof_date, subject, rule_id) |

观测是长表不是宽表：四个体制的指标集互斥，有日频价格的没有抵押品字段，
有抵押品字段的没有日频价格，压进宽表会有 60%+ 的列永久为 NULL。

### 配置

| 文件 | 管什么 |
|---|---|
| `config/universe.yaml` | 发行人与**档位归属**、跨档距离定义、SPV 台账 |
| `config/sources.yaml` | 端点、ETF 篮子、SEC 三扇门、已备案的无免费源 |
| `config/thresholds.yaml` | 五个判据的阈值、质量码规则、状态机定义 |

**档位归属由配置维护，脚本不自动调整。** 标尺本身会漂，重新分档是判断不是计算。
改 ETF 篮子会改 `basket_fingerprint`，历史序列从此断成两段。

## 输出规范

**结构**：按 `references/report_template.md` 的七节，顺序不变。正文 900–1600 字。

**文风**：像跟懂行的人当面把事讲清楚——句子通顺、有逻辑衔接，给判断时把话说透。
不要模板腔，不要成段堆套话。同一维度的多个条目用 bullet，一条一项，
每条用完整通顺的话写完，**不要退化成「主体 − 利差 − 变动」式的横杠拼接**；
结构化对照（发行人 × 期限 × 利差）才用表格。

**必须做到**：

- 结论第一句是**框架移动**（哪个判据触发、档位有没有变），不是数字罗列。
- 每条判断跟一个证伪条件。
- 缺失写「暂无数据 / 该体制不适用」，不用前值冒充最新值。
- 标出锚定日与它比日历日晚几天。
- 每次都要出现的口径句：G-spread 不是 OAS；ETF 持仓价是管理人估值不是成交价；
  这是指数样本内的子集不是全部存量债；引用 SPV 组合级数字时带「它含 N 项投资，
  不是单体」；CDS 与逐笔 TRACE 无免费源。
- **利差收窄不许直接写成利好**——先按裁决规则确认它不是 rolldown、
  不是背离基本面的追逐收益。

## 示例

**输入**：「出一份今天的数据中心信用日报」

**做法**：`collect.py --with-sec` → `metrics.py` → 读证据 → 写 `reports/dc-<date>.md`
→ `render_report_html.py`。

**冷启动第一天的正确产出**（2026-08-26 真实运行结果）：

> 顶部判断写「未定档」，因为体系状态机的进入条件都含「连续」或「持续」，
> 一天不构成；11 个发行人的 alpha 分解全部 `insufficient_history`。
> 能写实的是横截面：梯级七档齐整，从 MSFT 40bp 到 CRWV 776bp 跨一个数量级，
> 与前日锚点的漂移全在 ±2.5bp 内，标尺本身没动。
> 受监管公用事业中位 79bp 压在 IG 指数 81bp 上（差 −1.8bp），
> **反向证伪器仍然成立**——AI 故事没进入受监管电力定价，所以这一层的任何走宽
> 都不能归因到 AI。CRWV 曲线向上倾斜未倒挂（3.76Y 683 → 5.89Y 762），
> 存在 5.10→5.89 的负斜率段，市场在给高溢价但没在定价迫近的违约。
> Beignet 票息 6.581% 比租户 META 同期限的 6.815% 低 23bp，补偿偏薄——
> **这是当天信息量最大的一条，可以写实**。转债层 11 只全部 `credit_extractable=false`，
> 面板上信用利差那一列整列写「不可提取」，这也是正确产出。

**边界示例**：用户问「CoreWeave 的债现在多少收益率」这类单点查询，
跑 collect + metrics 后报它那几只的利差与曲线形状即可，不用写完整日报。
问「CoreWeave 股票能不能买」不属于本 skill——那是股票分析，且本 skill 不给买卖建议。
