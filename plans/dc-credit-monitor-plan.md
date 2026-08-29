# 数据中心信用监控 · 落地方案

日期：2026-08-26 ｜ 状态：**P0–P4 已实现并端到端跑通**（22/22 测试通过）
｜ 目标 skill：`ch-dc-credit-monitor`（已建，已接入 `skill-sync.yaml`）

首日实跑：319 只工具入库、11 个发行人成曲线、七档梯级复现且相对锚点漂移全在 ±2.5bp 内、
3 条阈值穿越事件、同日重跑幂等、alpha 分解闭合残差 0.0bp。
P5（残值剪刀差）与 P6（一级市场人工录入）按计划未实现。

范围已从初版 20 个信用对象收敛到三层：公开公司债、可转债、GPU 抵押融资载体 + Beignet。

三份文档的分工：

- `plans/dc-credit-methodology.md` —— **分析方法论，本项目的核心产物**。
  信用梯级标尺、五个动态判据、体系状态机、三层冲突裁决、陷阱与反指。
  实施后落位到 `references/credit_framework.md`。
- `plans/dc-credit-monitor-spec.md` —— 数据规格。可得性实测结论、口径限制、三张表、
  指标命名空间、质量码。
- 本文 —— 边界、数据模型 DDL、脚本分工、分期与验收。

顺序不能反：**方法论决定要算什么，才决定要采什么**。派生指标
（固定期限插值、alpha 分解、跨档距离、曲线斜率、剪刀差）都是被判据倒推出来的，
不是采集的副产品。

---

## 已拍板

1. **新建 skill，不扩进 `ch-gpu-compute-monitor`。** 理由是领域和数据面几乎不重叠——
   那边是算力成本与推理需求，这边是信用与抵押品；那边的源是 GPU 市场，这边是
   SEC/SPDR/FRED。而且它的 description 明写"不覆盖云厂商股票分析"，塞信用监控进去
   会让触发条件糊掉。**跨层依赖不需要代码耦合**：两个 skill 都落在同一个 `alpha_data`
   库，残值校验直接只读查 `gpu_price_observations` 即可。
   若将来发现两边报告总是一起看，再合并的成本也只是搬 SKILL.md。

2. **范围只做三层。** ABS（QTS/Compass/DataBank/Vantage）、REIT 运营指标、
   其余 Campus SPV（Sopaipilla / QTS Thunder）**本轮不做**。原因是前者要月度受托人
   报告和 KBRA PDF、后者连主体全称都还没核实。数据模型给它们留 `regime` 枚举位，
   将来补上时不改表。

3. **不做的字段就不建列**：CDS、逐笔 TRACE、新发定价与发行折价（New Issue
   Concession）。实测确认无免费源，建空列会让面板长期挂着一排永远是 NULL 的格子。

4. **P0 = L1 日频 + 梯级与归因**。采集与分析同期落地——只出利差不出归因的版本没有
   使用价值，因为单看"ORCL 205bp"读不出任何东西，必须有梯级和 alpha 分解才成立。
   转债层、GPU 抵押层、基本面层同期做但只做取数落库，残值剪刀差留插槽不实现。

5. **判词不进代码。** 脚本算 alpha、跨档距离、曲线斜率、阈值穿越事件；
   定档、归因、措辞全部在 SKILL.md 由模型做。脚本可以报"CRWV 曲线在 5.10Y 与
   5.89Y 之间出现负斜率"，不可以报"CRWV 出现违约预警"。

6. **存储走 `shared/data/db_core.py`**，PostgreSQL，表名前缀 `dc_`，
   并把新 skill 加进 `skill-sync.yaml` 的 data 与 html bundle。

---

## 一、这件事要解决的问题

AI 基建的杠杆正在从**上市公司资产负债表**迁到**表外**——SPV、私募信贷、GPU 抵押
贷款。只看超大厂的季报会系统性低估这个体系的真实杠杆：Meta 的 Hyperion 园区
$27.29bn 债务不在 Meta 报表上，它在 Beignet Investor LLC，而 Beignet 的信息只能从
Blue Owl 的 10-Q 附注里挖。

所以这个监控要回答的不是"哪只债便宜"，而是三个问题：

- **这个体系的融资成本在往哪走**——ORCL 30 年债的 267bp 和 CRWV 2030 的 683bp
  是同一件事的两个刻度，前者是超大厂被 AI capex 拖累的定价，后者是纯算力商的定价。
- **抵押品还值不值那个钱**——GPU 抵押贷款的残值假设，能不能被真实租金曲线证伪。
- **表外那部分在扩张还是收缩**——SPV 与私募信贷的新增规模和公允价值标记。

## 二、口径已定，不再讨论的部分

全部在 `plans/dc-credit-monitor-spec.md`，实施时按那份文件执行。这里只重复四条最容易
被实现者绕过去的硬约束：

- 关联键落到 **ISIN**，不是发行人。ORCL 52 只债按发行人聚合会把 2030 和 2052 混成一个数。
- ETF 持仓价是**管理人估值不是成交价**，连续多日不变要标 `stale`。
- **G-spread 不是 OAS**，含权券必须标 `option_biased`，不与普通高级无担保券同图比较。
- 缺失一律带原因码，`regime_na`（该体制不适用）与 `paywalled`（存在但无免费源）
  必须区分，不能都留白。

---

## 三、数据模型

三张表，长表形态。四个体制的指标集互斥，统一的是"一条观测的形状"而不是列。

```sql
CREATE TABLE IF NOT EXISTS dc_instruments (
    instrument_key      text        NOT NULL,   -- ISIN 优先；无 ISIN 用 issuer:type:coupon:maturity
    isin                text,                   -- SPV / 银团贷款为空
    issuer_key          text        NOT NULL,
    issuer_parent_key   text        NOT NULL,   -- Appalachian Power -> AEP
    regime              text        NOT NULL,   -- public_corp|convertible|gpu_secured|spv
    instrument_type     text        NOT NULL,
    coupon              numeric,
    coupon_type         text,                   -- fixed|float|zero
    maturity            date,                   -- 强制转股型为空
    currency            text        NOT NULL DEFAULT 'USD',
    is_144a             boolean     NOT NULL DEFAULT false,
    has_embedded_option boolean     NOT NULL DEFAULT false,
    recourse            text,                   -- recourse|nonrecourse
    collateral_type     text,                   -- none|real_property|gpu_equipment|lease_receivable
    first_seen          date        NOT NULL,
    last_seen           date        NOT NULL,
    PRIMARY KEY (instrument_key)
);

CREATE TABLE IF NOT EXISTS dc_observations (
    asof_date       date    NOT NULL,   -- 数据自身口径日（持仓 As of / 报告期末）
    instrument_key  text    NOT NULL,
    metric          text    NOT NULL,   -- px.*|yld.*|chg.*|cb.*|fin.*|col.*|spv.*
    value           numeric,
    value_text      text,               -- date/text 型指标（spv.tenant 等）
    unit            text    NOT NULL,   -- bp|pct|usd_mn|x|bool|date|text
    method          text    NOT NULL,   -- observed|derived|disclosed
    source_id       text    NOT NULL,
    obs_date        date    NOT NULL,   -- 采集日
    staleness_days  integer NOT NULL,
    quality         text    NOT NULL DEFAULT 'ok',
    raw_ref         text,               -- accession / 文件名 / URL
    PRIMARY KEY (asof_date, instrument_key, metric)
);

CREATE TABLE IF NOT EXISTS dc_collect_runs (
    run_id      text    NOT NULL,
    source_id   text    NOT NULL,
    obs_date    date,
    started_at  timestamptz,
    ended_at    timestamptz,
    status      text,                   -- ok|partial|failed
    rows_written integer,
    note        text,
    PRIMARY KEY (run_id, source_id)
);
```

**幂等键刻意用 `asof_date` 而不是 `obs_date`**，与 gpu-monitor 同理：同一天重跑必须
覆盖那一行而不是追加。但两个日期都要存——`staleness_days` 是它们的差，也是面板上
判断"这个数还新不新"的唯一依据。

`dc_instruments.last_seen` 用来处理 §六 的 `dropped_from_index`：工具从持仓文件消失
不等于到期，需要人工确认是到期、被剔出指数，还是回售。

## 四、脚本清单与原子边界

按 CLAUDE.md 的准则，脚本只做取数、标准化、确定性计算、落库、只读渲染；
**分档、是否证伪、要不要采信信号全部留给 SKILL.md**。

| 脚本 | 职责（一件事） | 不允许做 |
|---|---|---|
| `db_adapter.py` | 建表、幂等 upsert、按窗口读回 | 任何计算 |
| `collectors/base.py` | HTTP 重试、配置读取、限频、篮子指纹 | 领域逻辑 |
| `collectors/spdr.py` | 拉 6 只 SPDR ETF xlsx，解析成工具主数据 + `px.clean`；发行人匹配与 opco 上卷 | 算利差 |
| `collectors/fred.py` | 拉 UST 曲线与 ICE BofA OAS，提供插值 | 插值以外的加工 |
| `collectors/sec.py` | submissions / companyfacts / FilingSummary→R-file / SPV 台账 | 判断披露重要性 |
| `pricing.py` | 二分法解 YTM、修正久期、G-spread、超额利差、曲线锚日选择 | 决定哪个数可信 |
| `curve.py` | 分桶、固定期限插值、斜率、倒挂与分桶单调性 | 判断曲线形状意味着什么 |
| `ladder.py` | 档位聚合、跨档距离、离散度、长短端分化 | 判断离散度扩张属于哪一类 |
| `attribution.py` | 三段分解：市场 beta / 档位 beta / alpha，及累积与事件 | 判断 alpha 算不算信用事件 |
| `collect.py` | 编排一次采集，写 `dc_collect_runs`，stale 判定 | — |
| `metrics.py` | 组装证据包；转债期权价值、SPV 租户锚、阈值穿越事件清单 | 阈值告警的措辞、定档 |
| `daily_update.py` | cron 流水线 collect → metrics → snapshot | **渲染 HTML**（那需要模型的 verdict） |
| `render_report_html.py` | 只读渲染 | 生成结论文字 |

与计划时的差异（实现后回填）：原表里的 `convertible.py` 与 `instruments.py` 没有单独
成文件——转债逻辑只有几十行且强依赖发行人直债曲线，放进 `metrics.py` 比跨文件传递
曲线上下文更简单；工具主数据的维护天然发生在解析持仓的那一步，留在
`collectors/spdr.py`。`collectors/sec_filings.py` 实际命名为 `collectors/sec.py`。

`curve.py` 是方法论 §六 陷阱 1 的解药，优先级高于它在表里的位置：**没有固定期限插值
就无法区分 rolldown 和真实重定价**，发行人层的时间序列会系统性偏向"利差在收窄"。
`ladder.py` 的档位归属用配置文件写死初始映射，但档位本身会漂，
每次复盘由模型先重算梯级——**脚本不自动调整档位归属**。

`collectors/sec_filings.py` 的一个实现要点写死在这里，免得实现时踩：**工具级债务事实
不在 `companyfacts` 里**（那里只有合计 `LongTermDebt`），必须走
`FilingSummary.xml` → `R{n}.htm`，按 ShortName 匹配 `Debt - Schedule of Total Debt
Obligations` 与 `Debt - Delayed Draw Term Loans`。

## 五、分期与验收

每期的验收标准都要可证伪——能跑出具体数字，不是"跑通了"。

### P0 · 基础设施 + L1 日频 + 梯级与归因（先做）

落地：建表、instrument 主数据、SPDR + FRED 采集、pricing 全链路、`curve.py`、
`ladder.py`、`attribution.py`、`collect.py` 编排。

采集侧验收：
- 一次采集写入 **≥ 300 条** `dc_instruments`（实测去重后约 324 只），
  12 个发行人全部有记录，opco 正确上卷到 AEP / ETR / Dominion。
- `dc_observations` 当日产出 `px.clean` / `yld.ytm` / `yld.gspread_bp` /
  `yld.excess_vs_index_bp` 四个指标全覆盖。
- 抽样复核对得上实测值：META 4.6 11/32 ≈ 89bp、ORCL 6.9 11/52 ≈ 267bp、
  CRWV 9.25 06/30 ≈ 683bp（容差 ±5bp，允许因当日行情移动）。
- DLR 被标 `thin_curve`，Dominion/AEP/Entergy 的次级永续被标 `option_biased`。
- 同日重跑两次，`dc_observations` 行数不变（幂等）。

分析侧验收（这部分才是这一期真正的交付）：
- **梯级能复现**。跑出来的分桶表要对得上方法论 §二 那张实测表：
  5–10Y 上 MSFT ≈ 39、AMZN/GOOGL ≈ 68/69、公用事业 74–82、META ≈ 97、
  EQIX ≈ 107、ORCL ≈ 205、CRWV ≈ 775（容差 ±10bp）。
- **跨档距离对得上**：META−GOOGL ≈ 28bp、ORCL−GOOGL ≈ 136bp、
  CRWV−ORCL ≈ 570bp、公用事业−IG指数 ≈ −3bp。
- **曲线形状判对**：CRWV 四个点（3.77Y 683 / 4.44Y 727 / 5.10Y 790 / 5.89Y 761）
  产出 `curve_inverted = false`，且能定位到 5.10→5.89 那段负斜率。
- **rolldown 被隔离**：同一发行人的固定期限序列与单只债序列必须是两条不同的线，
  有测试断言二者不相等（否则说明 `curve.py` 没生效）。
- **alpha 分解闭合**：`Δ总 = beta_market + beta_tier + alpha`，残差 < 0.5bp。
- 冷启动首日 `chg.*` 与 `alpha_cum_*` 不出数（窗口不足），标 `regime_na` 而非填 0。

### P1 · 转债层（已实现，验收标准被实跑推翻并重写）

落地：转债的期权价值下界计算，CWB 解析剔除强制转股优先股。

**原验收「NBIS 8 只全部 credit_extractable=false、CRWV 两只为 true」是错的。**
错在用价格判位置。转债的 G-spread 根本不是信用利差——1.75% 票息的债贴着面值，
按直债折现算出来的收益率远低于国债，裸 G-spread 是负几百 bp，那是期权价值。
改用发行人自己的直债曲线做参照后，实际验收改为：

- CWB 中 `maturity == '-'` 的行被剔除（实测 3 行），库里不出现价格 > 300 的「债券」。
- 每只转债产出 `option_value_bp = 同期限直债利差 − 转债 G-spread`；
  发行人在样本内没有直债时标 `no_straight_curve`（NBIS 8 只全部如此）。
- `credit_extractable` 要价格 ≤115 **且** 期权价值 ≤300bp 两个条件同时成立。
  实测 11 只全部为 false——CRWV 那两只价格看着贴债底（104.60 / 96.95），
  期权价值却有 1,132bp / 975bp。
- 面板上信用利差那一列在不可提取时整列写「不可提取」，**不展示裸 G-spread**。

### P2 · GPU 抵押载体与 Beignet

落地：`collectors/sec_filings.py` 的 R-file 解析与全文检索。

验收：
- 从 CRWV `0001769628-26-000366` 抽出 DDTL 4.0 = $8,500m @ SOFR+225、
  DDTL 5.0 = $3,100m @ 4.50%、VIE 非流动资产 = $18,200m。
- Beignet 条款入库一条：$27,293,849 千 / 6.581% / 2049-05-30 / 租户 Meta，
  `quality = disclosure_once`，`asof_date = 2025-10-21`。
- **渲染层拒绝把 `disclosure_once` 的记录画成折线**——这条要有测试。

### P3 · 基本面层

落地：companyfacts 取 `fin.*`，季度。

验收：12 个发行人中有 SEC 申报的全部产出 net_debt/EBITDA 与利息保障倍数；
NBIS 作为外国私人发行人若无对应标签，标 `regime_na` 而不是空值。

### P4 · SKILL.md、方法论落位与 HTML

落地：
- `plans/dc-credit-methodology.md` 搬到 `references/credit_framework.md`，
  SKILL.md 只留纲要与指引（按 CLAUDE.md 三层加载，SKILL.md 控制在 500 行内）。
- SKILL.md 里写死工作流程：**先重算梯级 → 跑五个判据 → 三层裁决 → 定档 → 写结论**，
  以及每条判断必须跟证伪条件。
- 接 `shared/html_report`。

验收：
- description 能被"数据中心债券利差怎么样""CoreWeave 融资成本""AI 基建的债贵不贵"
  触发，且不被"CoreWeave 股票能不能买"触发。
- 拿一天的真实数据端到端跑一次，输出的第一句是**框架移动**（哪个判据触发、
  档位有没有变），不是数字罗列；每条判断都带证伪条件。
- 构造一个"利差收窄 + 基本面恶化"的样本，检查输出是否按 §五 裁决规则点名背离，
  而不是把收窄写成利好。
- 构造一个"公用事业走宽"的样本，检查输出是否按陷阱 5 拒绝归因到 AI。

### P5 · 残值剪刀差（留插槽，本轮不实现）

只读查 `gpu_price_observations` 得同代硬件真实租金衰减，对 `col.vie_assets_noncurrent`
得隐含残值曲线，产出 `drv.collateral_scissors`——即判据 5。
实现时必须先用 capex 剥掉新采购部分，否则账面增长会稀释出假的剪刀差（见方法论
§三 判据 5 的证伪条件）。**差值算不算证伪由模型判断，脚本不下结论。**

### P6 · 一级市场层（人工，随时可起）

方法论 §五 把一级市场定为权重最高的单一输入——发不出来债，二级利差是多少都不重要。
但规格里它标了 `paywalled`，没有免费结构化源。所以这一层是**人工录入 + Tavily
新闻辅助**：每笔 DC 相关新发记规模、期限、评级、指引→最终定价、认购倍数、募资用途。

这一期不排在 P0–P5 的依赖链上，可以随时并行开始，**而且越早开始越好**——
它是事件驱动的，错过的发行补不回来。

## 六、必须防的坑

1. **按发行人聚合丢 variant。** 与 GPU token 那次 `canonical_slug` 碰撞同类：
   ORCL 52 只债、AEP 43 只，聚合前必须先按 ISIN 落地，聚合时带久期与成分数。
2. **把 ETF 持仓变动当市场信号。** 持仓消失同时反映指数规则变化和市场变化，
   必须走 `dropped_from_index` 人工确认，不能自动解释。
3. **把组合级代理说成单体。** Beignet 之后并入"8 项投资、15 处物业"的合计行，
   任何引用这个数的结论都要带上"该值含 8 项投资"的限定。
4. **深度价内转债当信用信号读。** NBIS 现在 delta 接近 1，它的价格变动是股票信息。
5. **iShares 不要再试。** 已实测被 Akamai 拦（头写 CSV、body 是 HTML），
   加 Referer 与 cookie jar 均无效。写进注释，免得后来者重跑一遍。

## 七、留到实施期验证的两件事

- **SPDR 持仓文件的历史回填**：SSGA 是否提供历史日期的持仓，还是只有最新一天。
  若只有最新一天，则 `chg.gspread_1d/1w/1m` 在冷启动后需要积累 22 个交易日才可用，
  这期间该指标直接不出，不做外推。
- **OSNL 双周 8-K 的 NAV 字段位置**：已确认 2026-06 至 08 有 5 次 items 3.02+8.01
  的申报，但 NAV 具体落在正文还是附件、能否稳定正则抽取，需要实现时确认。
