---
name: stock-ai-analyzer
description: A股股票基本面投研分析方法论。当用户要求按股票名称或代码分析中国A股、判断公司成长性与核心亮点、评估估值阶段与隐含预期、分析财务质量与业务结构、判断股票是否处于当前市场主线内、做竞争格局分析或风险排查，或基于Tushare/公开市场数据生成股票基本面分析时使用。用户提问形式包括"帮我分析XX股票""XX的基本面怎么样""XX成长性如何""XX估值贵不贵""XX为什么值得买""XX是不是当前主线""XX所在行业景气度如何"等。当用户要求"深度分析""深挖""把异常/业绩变化查清楚""读年报找线索""挖未被定价的看点"时，进入 Deep 模式（见 references/deep_mode.md）做命题先行的主动调研。该技能要求分析流程、判断框架、内容输出全部写在SKILL.md中；脚本只允许做原子数据获取与确定性扫描。
---

# A股基本面投研分析助手

把这个技能当作股票基本面投研方法论使用。不要依赖脚本生成报告、提示词、投资结论、组合分析或推荐。脚本只能取数；你作为智能体负责解释数据、判断重点，并直接写出最终内容。

---

## 一、总原则

- 所有内容逻辑都放在本 `SKILL.md` 中：分析路径、判断标准、报告结构、措辞边界。
- 只使用 `scripts/data_fetcher.py` 获取单一数据集，或解析股票名称/代码。
- 脚本输出只是证据，结论必须由你基于证据推理得出。
- **A股估值的核心哲学**：A股是高度叙事驱动的市场。一只股票是否处于当前市场主线内，会显著影响其估值定价方式。这一判断是定量分析中无法回避的关键变量，详见第 5.5 节。估值之前必须先对公司分型（成长股/成熟龙头/红利价值），不同类型适用不同估值模式，详见第 5.1 节。

---

## 二、数据获取

环境变量：

```bash
TUSHARE_TOKEN=your_token
```

默认一次只获取一个数据集；需要完整单股分析时，使用 `pack` 生成完整 evidence、轻量 `analysis_context` 和模块级 JSON。

```bash
python scripts/data_fetcher.py search 贵州茅台

python scripts/data_fetcher.py fetch company 600519.SH
python scripts/data_fetcher.py fetch financial 600519.SH --limit 8
python scripts/data_fetcher.py fetch income 600519.SH --limit 8
python scripts/data_fetcher.py fetch balance 600519.SH --limit 8
python scripts/data_fetcher.py fetch cashflow 600519.SH --limit 8
python scripts/data_fetcher.py fetch daily-basic 600519.SH --limit 60
python scripts/data_fetcher.py fetch valuation-band 600519.SH --years 5
python scripts/data_fetcher.py fetch main-business-product 600519.SH --limit 30
python scripts/data_fetcher.py fetch top10-holders 600519.SH --limit 4
python scripts/data_fetcher.py fetch managers 600519.SH
python scripts/data_fetcher.py fetch rewards 600519.SH
python scripts/data_fetcher.py fetch holder-number 600519.SH --limit 20
python scripts/data_fetcher.py fetch holder-trade 600519.SH --limit 30
python scripts/data_fetcher.py fetch share-float 600519.SH --limit 20
python scripts/data_fetcher.py fetch block-trade 600519.SH --limit 30

python scripts/data_fetcher.py fetch report-list 600519.SH --report-type all --limit 8
python scripts/data_fetcher.py fetch report-text 600519.SH --report-type annual --report-index 1 --max-pages 80 --max-chars 60000

python scripts/data_fetcher.py fetch announcements 600519.SH --date 2026-01-01~2026-05-16 --searchkey 回购 --limit 20
python scripts/data_fetcher.py fetch announcement-text 600519.SH --date 2026-01-01~2026-05-16 --searchkey 监管函 --announcement-index 1
python scripts/data_fetcher.py fetch institutional-research 600519.SH --start-date 20250101 --end-date 20260516 --limit 30

python scripts/data_fetcher.py pack 600519.SH --date 2026-01-01~2026-05-16 --evidence-out reports/evidence_600519.json --context-out reports/context_600519.json --module-context-dir reports/module_context_600519
```

默认使用 JSON 输出。只有在表格查看更方便时才使用 `--format csv`。

### 2.1 数据维度说明

| 维度 | dataset | 用途 |
|---|---|---|
| 股票识别 | `search` | 名称/代码解析，确认 `ts_code`、地区、行业、市场、上市日期和板块 |
| 公司画像 | `company` | 公司简介、行业、注册地、主营概况 |
| 财务指标 | `financial` | ROE、毛利率、净利率、成长、偿债、周转等财务指标 |
| 三大报表 | `income`、`balance`、`cashflow` | 利润表、资产负债表、现金流量表，用于增长质量、利润质量和财务健康验证 |
| 行情估值 | `daily`、`daily-basic` | 日线、换手率、PE/PB/PS、股息率、市值、股本结构 |
| 历史估值分位 | `valuation-band` | 从多年 `daily_basic` 确定性计算 PE/PB/PS/股息率的近 1/3/5 年区间与当前分位（PE band）。Tushare 无现成分位接口，由脚本计算 |
| 主营结构 | `main-business-product`、`main-business-region` | 产品和地区收入/利润结构，判断业务驱动和集中风险 |
| 股东治理 | `top10-holders`、`managers`、`rewards` | 十大股东、董监高、管理层薪酬和持股 |
| 筹码变化 | `holder-number`、`holder-trade` | 股东户数、重要股东增减持 |
| 交易与供给压力 | `share-float`、`block-trade` | 限售股解禁、大宗交易 |
| 年报季报 | `report-list`、`report-raw`、`report-text` | 巨潮定期报告列表、PDF 下载、PDF 文本抽取 |
| 个股公告 | `announcements`、`announcement-raw`、`announcement-text` | 仅按个股查询巨潮公告，按标题规则标记问询回复、监管函、回购、减持、股权激励、定增、重大合作、业绩快报等 |
| 机构调研 | `institutional-research` | Tushare `stk_surv`，用于观察调研频率、参与机构、接待方式和调研内容关键词 |
| 上下文包 | `pack` | 单股完整 evidence、轻量 `analysis_context` 和模块级 JSON，供 subagent 或低上下文顺序分析 |

### 2.2 公告查询边界

- `announcements` 只支持按个股查询。不要用本 skill 做全市场公告扫描。
- 标题规则只负责定位事件类型，不负责判断影响大小。
- `tabtype=fulltext` 用于公告全文列表；`tabtype=relation` 仅作为互动易/调研相关列表的辅助入口。
- PDF 原文读取使用本 skill 内置下载与 `PyPDF2` 文本抽取，不跨 skill 引用。

### 2.3 把研报渲染成 HTML（可选）

写完 Markdown 研报后，可用 `render_report_html.py` 渲染成自包含的单页 HTML（Claude UI 风格），并自动从 evidence 里嵌入两类图表：**PE/PB/PS 估值带时间序列**（`valuation-band` 的 `series` + 历史分位带，横轴日期、纵轴估值倍数，叠加 P25–P75 带与当前分位）、营收/归母净利/同比增速/盈利能力**财务趋势**（`income`+`financial`）。

```bash
python scripts/render_report_html.py -i reports/report_600519.md --evidence reports/evidence_600519.json
# 不传 --evidence 时，会自动找同目录的 evidence_<代码>.json；找不到则只渲染正文、不嵌图表
```

**内容与输出格式解耦（重要）**：HTML 渲染器对正文是"只读装饰"——它不改写任何文字，只把 Markdown 转成 HTML，并在客户端用 JS 注入图表与 hero 卡片。因此写研报时**不需要为 HTML 迁就措辞或结构**：

- 图表锚点是"尽力而为"。找不到对应小标题（如「估值快照与历史分位」「成长性与财务质量诊断」「核心判断」）时，对应图表**静默跳过**，不报错。
- evidence 缺某个数据集时，对应图表自动不画，正文照常输出。
- 文本保全校验默认**只告警不中断**（`--no-validate` 完全跳过，`--strict` 才在不一致时报错），正文内容怎么改都不会导致 HTML 生成失败。
- 沿用模板（第八节）里的小标题命名能让图表落到最合适的位置；即使大改结构，最坏情况也只是少几张图，正文与样式不受影响。

### 2.4 Deep 模式工具（确定性 surfacing + 原文定位）

仅 Deep 模式用，方法论见 `references/deep_mode.md`：

- `python scripts/thesis_scan.py --evidence reports/evidence_<code>.json`：对 evidence pack 做**确定性**扫描，输出命题原型（含预期差 A1/A2/B）、异常旗标（业绩/利润质量/营运资本/减值/资本结构）和每条的 probe 建议。只 surfacing，不下结论。
- `fetch report-text … --section <章节/关键词> [--to-markdown]`：把"盲读前 6 万字"升级为**定向读章节**。年报标准章节别名 `mda`/`财务报告`/`重要事项`/`公司治理`/`股东`；不命中（如季报、或"研发"/"AI"等子主题）自动降级为关键词窗口。引擎优先级 pymupdf4llm → PyMuPDF → PyPDF2，缺高级库自动回退。
- `fetch daily <code> --limit 0`：取全量日线，供"月线横盘/价格 vs 盈利"等长周期判断（pack 默认仅 60 日）。

---

## 三、工作流程

### 收集证据

- 只获取完成任务所需的最小数据集。
- 财务趋势优先取最近 6-8 个报告期，覆盖至少 2 个完整财年。
- 如果任务是完整单股研究，优先运行 `pack`，再按模块级 JSON 分析，避免一次加载完整 evidence。
- 主线归属判断需要联网检索近期市场动态，不要依赖训练记忆。
- 数据缺失时要明确说明缺口，并降低结论置信度，不要补造数据。

### 原文升级规则

默认先看公告/报告元数据。只有出现以下情况，才升级读取 PDF 原文：

- 用户明确要求“看原文”“读公告”“解释公告”“公告具体说了什么”。
- 标题命中监管函、问询回复、重大合作/投资、股权激励、定增、员工持股、回购、增持、减持、业绩快报。
- 财务指标出现异常，需要查定期报告里的管理层解释、会计政策、业务拆分、风险提示。
- 仅凭标题无法判断事件性质，例如“签署协议”需要确认金额、约束力、履约条件和收入确认节奏。

需要原文时：

1. 先用 `announcements` 或 `report-list` 定位候选。
2. 按标题相关性、披露时间、是否全文公告选择 1-3 篇。
3. 用 `announcement-text` 或 `report-text` 读取 PDF 文本。
4. 输出时明确区分“原文事实”和“模型推断”，不得只凭标题判断影响。

### Subagent 编排与上下文管理

使用 `pack` 后，主 agent 得到三类产物：

- 完整 `evidence`：归档证据，不直接整体塞进模型上下文。
- 轻量 `analysis_context`：单会话分析时优先加载。
- `module_contexts/`：供 subagent 分模块撰写。

**分发前先定初步分型**：主 agent 在拆分 subagent 之前，先用增速、股息率、行业地位、主线做一次初步分型（4.1），并把"该公司初步判定为 X 型"写进每个 subagent 的指令——这样模块 1（定性侧重）和模块 2（估值模式）才能在同一透镜下展开，而不是各自默认成长股口径。

有 subagent 编排能力时，每个 subagent 只读取自己的模块 JSON 和本 `SKILL.md` 对应方法论：

| 模块 | JSON | 任务 |
|---|---|---|
| 1 | `module1_growth_financial.json` | 成长性、财务质量、业务结构 |
| 2 | `module2_valuation_market.json` | 估值快照、隐含预期、市场定价 |
| 3 | `module3_governance.json` | 股东结构、管理层、筹码、解禁、大宗交易 |
| 4 | `module4_announcements.json` | 公告事件筛查、原文升级候选、定期报告候选 |
| 5 | `module5_research_mainline.json` | 机构调研、主线连接线索、市场关注度 |

没有 subagent 能力时，按同样模块顺序逐段加载模块 JSON。聚合阶段只读取模块结论、`analysis_context`、必要的公告/报告原文摘录。

### 分析与判断

先按 4.1 做初步分型，再按下文第四节（定性框架，重心随类型调整）和第五节（定量框架，估值模式随类型选择）的方法论展开分析。

### 写作输出

- 先给核心结论和置信度。
- 用取到的数据作为证据，给出具体数字和报告期。
- 显式说明关键假设（包括主线归属假设）。
- 避免单边结论，写出最强反方观点。
- 结尾写清风险和非投资建议声明。

### Deep 模式（可选，用户显式要求时）

骨架流程产出"骨头架子"。当用户要求**深度分析 / 深挖 / 把异常或业绩变化查清楚 / 读年报找线索 / 挖未被定价的看点**时，进入 Deep 模式，按 `references/deep_mode.md` 做**命题先行的主动调研**：

1. 先跑确定性扫描定命题与疑点：`python scripts/thesis_scan.py --evidence reports/evidence_<code>.json` → 得到命题原型（含预期差 A1/A2/B）、异常旗标、每条的 probe 建议。
2. 按主循环 **THESIS → INVESTIGATE → CATALYST GAP → INTEGRATE** 调研：
   - **方向一（异常归因）**：对 high/med 旗标定向读原文——`fetch report-text <code> --report-type annual --section mda|财务报告`、`fetch announcement-text ... --searchkey 减值`——定位业绩暴增/暴跌、商誉计提、OCF 背离、应收/存货异常的成因。
   - **方向二（主线线索挖掘）**：先联网定当前主线关键词，再 `fetch report-text <code> --section <主线词>` 从年报/季报捞出公司自述、与主线相关但骨架漏掉的线索（尤其 B 型"未被定价的期权"）。
3. 用 subagent 隔离读 PDF 的上下文，设调研预算（读 ≤3–5 篇原文、联网 ≤5 次），原文拿不到降置信度不编造。
4. 发现以"深度调研发现"小块嵌进下文报告模板对应章节（成长/财务诊断、核心看点、主线归属修正、风险），严格区分原文事实 vs 推断。

完整 playbook、命题原型表、`--section` 用法、停止条件见 `references/deep_mode.md`。

---

## 四、定性分析框架：成长性判断与核心亮点

定性分析的核心任务是回答两个问题：**这家公司在成长吗？为什么值得关注？**

**完整方法论见 `references/qualitative_framework.md`**。本节保留纲要：

1. **先分型**（成长股/成熟龙头/红利价值）——分型决定后续五维度的权重分配
2. **五维度诊断**——收入增速质量、利润含金量、现金流验证、业务结构与壁垒、ROE 分解
3. **成长阶段定位**——高速成长期/稳健成长期/成熟稳定期/增速放缓/下行困境
4. **核心亮点提炼**——2-4 条，落在该类型的核心矛盾上，附数据与反方观点

脚本已做确定性计算：`pack` 产出的 `business_structure.product_analysis`（产品结构跨期变化）和 `region_analysis`（地区结构与出海趋势）可直接引用，无需手动重算。

---

## 五、定量分析框架：估值阶段与隐含预期

定量分析的核心任务是回答：**当前价格隐含了什么样的未来预期？这个预期合理吗？**

**完整方法论见 `references/quantitative_framework.md`**。本节保留纲要：

1. **先分型再选模式**——成长股用隐含预期/三情景，成熟龙头用 PE/PB 历史分位，红利价值用股息率分位/利差/DDM
2. **估值快照与历史分位**——`valuation-band` 提供 PE/PB/PS/股息率的近 1/3/5 年区间与当前分位
3. **逆向推算隐含预期**——从当前 PE 反推市场隐含增速，与实际成长能力对比
4. **三情景推演**——基准/乐观/压力情景的假设、推算与偏离
5. **主线归属修正**（A股关键调节项）——强/弱/无连接决定估值容忍度
6. **估值阶段定位**——深度低估/合理偏低/合理/偏高/极度乐观
7. **财务健康度交叉验证**——偿债安全、利润质量、资本效率

```bash
python scripts/data_fetcher.py fetch valuation-band 600519.SH --years 5
```

---

## 六、股东与治理分析

输出落位：报告第三部分「背景与风险」，作为背景与风险信号（管理层激励、大股东增减持对成长股/红利股判断有信息量的，可在定性里点一句）。

使用 `top10-holders`、`managers`、`rewards`、`holder-number`、`holder-trade`、`share-float`、`block-trade`。

### 6.1 判断要素

- **股权集中度**：第一大股东持股 > 40% 且与第二大股东差距大 → 控制力强但需关注一言堂风险。机构/社保/QFII 占比上升 → 通常为正面信号。
- **管理层激励**：有期权/限制性股票激励的管理团队与股东利益更绑定；对比管理层薪酬与公司利润规模。
- **股东户数变化**：连续下降 → 筹码集中，通常与价格上行共振；连续上升 → 筹码分散。
- **重要股东增减持**：`holder-trade` 大额减持需关注原因；增持通常是信心信号（但也可能是护盘）。
- **限售股解禁**：`share-float` 近期有大额解禁 → 供给压力。
- **大宗交易**：`block-trade` 频繁出现且折价明显，需结合股东变动和公告判断是否存在减持压力。

### 6.2 注意事项

- 不要基于单个报告期过度推断治理质量。
- 股东户数变化是辅助信号而非决策依据。

---

## 七、公告事件与机构调研分析

输出落位：报告第一部分「近期动态：公告事件与机构调研」，作为对公司现状的补充；其中与主线相关的佐证在定量「主线归属修正」处被引用。

### 7.1 公告事件验证

使用 `announcements` 先定位事件，再决定是否读取 `announcement-text`。

- **监管/问询**：监管函、关注函、问询回复通常需要读原文，确认问题指向、公司解释和整改承诺。
- **资本运作**：定增、股权激励、员工持股要确认发行对象、价格、解锁条件、业绩考核和摊薄影响。
- **回购/增减持**：确认规模、价格区间、资金来源、执行进度和主体身份，不要只凭“回购/增持”定性利好。
- **重大合作/项目**：必须读原文确认协议约束力、金额、履约期限、收入确认条件、对手方和风险条款。
- **业绩快报/预告**：重点比较收入、利润、扣非利润和现金流线索，必要时再读定期报告解释。

公告标题只是索引。对公司基本面、估值或风险有影响的结论，必须来自公告原文、定期报告正文或其他可验证来源。

### 7.2 机构调研使用方法

使用 `institutional-research` 观察市场关注度和产业问题焦点。

- **调研频率**：短期明显升温说明关注度上升，但不等同于基本面改善。
- **机构类型**：公募、券商、险资、QFII、私募等参与结构可以辅助判断关注质量。
- **调研方式**：现场、电话、线上会议等只说明沟通形式，不直接说明信息增量。
- **调研内容关键词**：提取 AI/算力/出海/订单/价格/产能/客户/毛利率等关键词，验证公司是否被市场放入当前主线。
- **风险边界**：机构调研记录是公开披露信息，不能当作业绩承诺或内幕信息。

---

## 八、输出模板

下面模板里的几个小标题同时是 HTML 渲染（2.3 节）的图表锚点：「核心判断」→ hero 卡片，「估值快照与历史分位」→ PE/PB/PS 估值带时间序列图，「成长性与财务质量诊断」→ 财务趋势图。沿用这些命名能让图表自动落位；但锚点是尽力而为，命名变了也只是少几张图，不影响正文。

```markdown
# [公司名称]（[股票代码]）基本面研究

## 核心判断
[2-4 句结论先行：公司类型（成长股/成熟龙头/红利价值）+ 一句话看点 + 行业地位 + 估值状态（贵/合理/便宜，当前价格是否合理）+ 最关键的变量。标注总体置信度：高/中/低]

---

## 第一部分 · 定性：这是一家什么公司，好在哪

### 1. 公司速览
[一句话说清主营做什么；主营产品/收入结构、所属行业、总市值、上市时间等关键事实。克制，不堆 `company` 原始字段]

### 2. 公司分型
[归入成长股/成熟龙头/红利价值哪一型 + 依据（增速/股息率/行业地位/主线）。这是后续定性侧重与定量估值模式的纲。标注置信度，并说明是否处于类型切换期]

### 3. 行业地位与竞争格局
[行业景气度与所处阶段；公司是领导者/挑战者/跟随者；护城河类型与强弱；市占率/份额趋势。外部事实需联网检索并引用来源]

### 4. 成长性与财务质量诊断
[五维度评估（按分型调整权重）：收入增长质量/利润含金量/现金流验证/业务结构/再投资能力与 ROE；并入财务健康：偿债安全/利润质量/资本效率。最后归入成长阶段，并核对与分型是否自洽]

### 5. 核心看点
[2-4 条亮点，落在该类型的核心矛盾上（成长股=成长动力与远期空间、成熟龙头=盈利稳定与股东回报、红利价值=可持续现金分红）。每条附数据和最强反方观点]

### 6. 近期动态：公告事件与机构调研
[作为对公司现状的补充：近期重大公告事件（区分原文事实 vs 推断，必要时读原文）；机构调研频率、参与机构、问题关键词。注明这些只反映关注度与现状，不等同业绩改善]

---

## 第二部分 · 定量：贵不贵，当前价格合不合理

### 1. 估值模式
[依据第一部分的分型，说明这家公司适用哪种估值法：成长股=隐含预期/三情景、成熟龙头=PE/PB 历史分位、红利价值=股息率分位/利差/DDM]

### 2. 估值快照与历史分位
[PE/PB/PS/股息率/市值，标注日期；按类型给出关键指标近 1/3/5 年分位——贵不贵先对自身历史（及同业，如有）。成长股说明 band 仅作下行参考]

### 3. 隐含预期分析
[从当前市值/PE 反推市场隐含的增长预期，与第一部分判断的实际成长能力对比。增速假设需显式标注为假设]

### 4. 主线归属修正（A股关键调节项）
- 当前市场主线：[简要说明，附依据]
- 与主线连接强度：[强/弱/无连接]
- 验证信号：[机构调研/公司催化公告/相对涨跌；北向、研报需联网，注明数据边界]
- 主线判断置信度：[高/中/低]
- 对估值的修正：[膨胀容忍度/谨慎程度]

### 5. 三情景推演
[基准/乐观/压力情景的假设、推算与对当前价的偏离]

### 6. 估值结论
[收口：当前估值处于哪个阶段（深度低估/合理偏低/合理/偏高/极度乐观），当前价格是否合理；说明主线归属如何修正了这个判断；第一部分若有财务异常，在此下调置信度]

---

## 第三部分 · 背景与风险

### 1. 股东与治理
[股权结构/管理层激励/股东户数/重要股东增减持/限售解禁/大宗交易。作为背景与风险信号]

### 2. 风险提示
[针对这家公司的具体风险，包括主线轮换风险]

---
以上基于公开数据和当前可得信息，不构成投资建议。数据存在滞后性，主线判断带有主观性，分析结论可能因新信息而改变。
```
---

## 九、质量要求

- 能给具体数字时优先给数字，数据有日期时写明报告期。
- 不要堆原始表格，用关键数字支撑论点。
- 不要编造竞争对手、客户、政策、产业事实或市场主线；这类当前外部事实需要联网检索并引用来源。
- 主线归属判断必须基于近期可验证的市场观察，不要凭训练记忆判断当前主线。
- 避免单边结论，每个判断都要写出最强反方观点。
- 区分"我从数据中看到了什么"和"我推断了什么"——前者是事实陈述，后者需标注为推断。
- 公告标题只能用于定位事件；涉及重大影响时必须读取公告或报告原文。
- 机构调研只能用于关注度和问题焦点验证，不得写成业绩改善证据。
- 主线归属作为可被推翻的假设而非事实，必须标注置信度。
- 最终回答要适合投资者阅读：简洁、有证据、清楚表达不确定性。
