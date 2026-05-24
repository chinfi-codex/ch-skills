---
name: chstock-rise-attribution
description: 当用户给出 A 股个股名称或代码 + 某交易日涨幅，要求归因当日上涨原因、解释异动催化、复盘涨停或大涨逻辑、判断"为什么今天涨"，并希望按 AlphaVault 查询协议从知识库内已有个股词条、来源登记、库内 Raw/Pack 出发、库内不足时再调公告、互动问答、Web 一手来源补证、以 Evidence Pack 形式交付时使用此 skill。典型表达："XX 今天为什么涨"、"XX 昨天涨 8% 归因一下"、"XX 涨停催化"、"复盘 XX 异动并出证据包"、"XX 大涨原因是什么"。脚本只负责取证据，催化判定与可信度分档全部由模型完成；最终回答**只输出中高置信归因**，省略未解释部分、推测候选、反例排查、跟踪事项、信源冲突、能力边界等所有"找不到"或"过程性"陈述；产物默认是 Evidence Pack（pack_subtype=AttributionEvidence），write_permission 必须为 false，不直接写入 wiki/05-个股分析/，需写入研究报告时进入 20-查询协议.md 的研究报告写入流程。不用于预测未来走势、不给买卖建议、不查 L2 龙虎榜资金流明细、不读 PDF 全文。
---

# 个股上涨归因（AlphaVault 查询协议适配版）

## 目标

1. **做什么**：给定个股 + 交易日 + 涨幅，按**基本面（A1 产业信息 / A2 高权重研报 / A3 行业边际变化 / A4 主题概念炒作）+ 资金面（B1 K 线资金流 / B2 板块资金流）**两大角度六个子项逐项核查 → 输出 Evidence Pack（内部记录所有分档证据）→ **回答按"基本面 / 资金面"两段呈现，只含中高置信归因**，省略未解释、推测、反例排查等过程性内容。
2. **不做什么**：不预测后续走势、不给买卖建议、不查 L2 龙虎榜明细、不解读 PDF 全文（要 PDF 内容把 url 转交其他流程）。
3. **给谁用**：盘后复盘异动票、给研究员补背景、给量化策略做催化事件验证。

## 适用场景与边界

| 适用 | 不适用 |
|---|---|
| 单日异动归因 / 涨停催化复盘 | 跨日趋势研判 / 中长期投资逻辑 |
| 基本面 + 资金面两角度逐项核查 | 单一关键词式 WebSearch 一把梭 |
| 产出 Evidence Pack 供后续报告 | 直接写 wiki/05-个股分析/ |
| 跨源（公告 / 互动易 / 新闻 / 研报 / 板块）交叉验证 | 内幕信息判断 / 信源真实性背书 |

## AlphaVault 编排准入说明

本 skill 当前**未登记**进 AlphaVault `00-治理总则.md` 的 Skills 白名单与 `能力注册表.md` 的 skills 节，作为外部 skill 调用试跑。正式纳入 AlphaVault 编排链路前必须：

1. 在 `00-治理总则.md` § Skills 白名单新增一行（standard_output: `Evidence Pack`、can_write_raw: optional、can_write_wiki: false）。
2. 在 `能力注册表.md` § skills 新增对应 YAML 条目。
3. 跑 `python system/tools/compile_protocol_contracts.py`。

试跑期间产物只作为外部中间产物使用，不可视为 AlphaVault 认证 Pack。

## 归因两大角度（领域方法论纲要）

每次归因**必须**从下面两个角度分别核查，输出回答也按这两个角度分段。每个子项的**判定锚点**、**可支撑/不可支撑证据**、**置信度上限**见 [references/catalyst_taxonomy.md](references/catalyst_taxonomy.md)。

### 角度 A · 基本面

按**优先级降序**核查（高优先级子项命中后仍要继续看低优先级，但置信度排序时高优先级胜出）：

| 子项 | 内容 | 默认主要数据源 |
|---|---|---|
| **A1 产业信息（最高优先级）** | 公司公告、订单/客户/产能/价格信号、上下游联动、海外对标公司动态 | wiki 候选页面 → `chstock-cninfo-announcement` → `chstock-interactive-qa` → `chstock-macro-monitor` → WebSearch |
| **A2 高权重研报** | Citi / 中金 / 华泰 / 东吴 / 大摩 / 小摩 / 摩根士丹利 / 高盛 等机构观点、目标价上调、评级变化、深度报告 | `RAW/clippings/` 研报 → WebSearch |
| **A3 行业重要边际变化** | 景气度拐点、渗透率突破、技术路线切换、新规/标准出台、产业政策、价格指数拐点 | wiki/02-概念词条、wiki/03-产业链图谱 → `chstock-macro-monitor` → WebSearch |
| **A4 主题概念炒作** | 低空经济 / 人形机器人 / 固态电池 / AI 算力 / CPO / 出海 / 国产替代 等热门主题；本身是市场对未来基本面预期定价，故归基本面 | WebSearch + `chstock-market-telegraphs` |

### 角度 B · 资金面

| 子项 | 内容 | 默认主要数据源 |
|---|---|---|
| **B1 K 线 / 个股资金流** | 主力 / 游资 / 散户净流入流出、量比、换手率、量价突破形态、连涨节奏、涨停状态 | Tushare daily + daily_basic（量价）→ WebSearch（LV1 资金流：证券之星 / 东方财富） |
| **B2 板块资金流** | 申万一级行业当日 pct_chg / 中位数 / 涨停家数、概念板块异动、板块龙头封板情况、板块净流入 | Tushare daily 同行业当日均值 + WebSearch（板块异动） |

**判定成对原则**：一条候选催化必须同时满足"**时间窗口对齐**" + "**逻辑机制可解释**"才能进入回答；只有任一对齐的属于低置信，**不进回答**。**孤立大涨 vs 板块普涨** 判定：用同申万一级行业 pct_chg 均值对比；板块普涨时归因偏 B2 + A3/A4，孤立大涨时优先 A1/A2。

## 工作流程（AlphaVault 查询协议适配）

按 `20-查询协议.md` 的窄→宽读取预算执行。**主 Agent 不读 Raw 原文**，Raw 原文一律走 SubAgent / 查询工作单元，只返回 Evidence Pack 片段，规则见 [references/subagent_protocol.md](references/subagent_protocol.md)。

| 步骤 | 动作 | 用什么 | 产物 |
|---|---|---|---|
| 0 | 输入标准化：股票名 → ts_code、日期 → 交易日、涨幅与 Tushare daily 对账 | Tushare daily | 标准化参数 |
| 1 | wiki/index.md 定向定位候选个股词条 | Read `wiki/01-个股词条/` 索引片段 | 候选页面路径或"无既有页面" |
| 2 | 候选页面结构化区块读取（frontmatter / 核心数据 / 可产出结论 / 未知与后续跟踪 / 更新历史） | Read 候选页面 | 业务/客户/产业链位置基线、既有 TODO |
| 3 | 库内 Raw 定向过滤（按页面路径、实体名、关键词、日期窗口过滤来源登记） | `scripts/query_alphavault_raw.py`（包装 AlphaVault `query_source_registry.py`） | 候选 Raw / Pack 路径列表 |
| 4 | 库内 Raw / Pack 回溯（公告 PDF、研报、互动问答、电报、Evidence Pack） | **SubAgent / 工作单元**返回 Pack 片段；Raw 原文不进主上下文 | 库内 Evidence Pack 片段 |
| 5 | 外部补证触发判断：仅当库内 Raw 不足、过期、单一低权重，或用户明确要求最新口径 | 触发条件清单（见下） | 是否进入 step 6 |
| 6 | **角度 A 外部补证**：T-7~T+1 公告 / T-30~T+1 互动问答 / WebSearch 检索 A1 产业信息 + A2 高权重研报 + A3 行业边际变化 + A4 主题概念炒作 | sibling skill 脚本 + WebSearch | 新 Raw 落到 `RAW/crawlers/` |
| 7 | **角度 B 量价 + 板块快照**：T-5~T+1 daily + daily_basic + 申万一级同行业 pct_chg 均值 + WebSearch 检索 LV1 资金流（主力/游资/散户）和板块异动 | `scripts/price_window_snapshot.py` + WebSearch | 资金面证据卡 |
| 8 | Evidence Pack 组装 | `scripts/build_attribution_pack.py` | Pack JSON |
| 9 | 归因综合（按"基本面 / 资金面"两段输出）+ 回答前自检 | 模型 | 最终回答（默认不写 Wiki） |

**外部补证触发条件**（任一满足才进入 step 6）：

- 库内没有可读的关联 Raw / Pack；
- 库内 Raw 已过期，无法回答最新口径；
- 用户明确要求最新公开披露、官网、公告或原始链接；
- 库内来源为低权重或单一来源，且催化属于核心判断；
- 库内来源之间存在潜在冲突，需要外部一手交叉验证；
- 候选催化涉及 **A3 行业边际变化**、**A4 主题概念炒作**、**B1 LV1 资金流**——这三类库内通常不足，默认补 WebSearch。

## 数据获取（脚本抓手）

### 本 skill 脚本

| 脚本 | 职责 | 关键参数 |
|---|---|---|
| `scripts/query_alphavault_raw.py` | 调 AlphaVault `system/tools/query_source_registry.py`，按页面/实体/关键词 + 日期窗口过滤来源登记 | `--page`、`--keyword`、`--date-from`、`--date-to`、`--alphavault-root` |
| `scripts/price_window_snapshot.py` | Tushare `daily` + `daily_basic` + `stock_basic.industry` 同行业当日 pct_chg 均值；不接龙虎榜 | `--ts-code`、`--trade-date`、`--window` |
| `scripts/build_attribution_pack.py` | 编排：依次跑 query_alphavault_raw + price_window_snapshot + sibling 公告/QA 脚本，汇总成 Evidence Pack JSON | `--name`、`--ts-code`、`--trade-date`、`--pct-chg`、`--output`、`--skip-external`、`--alphavault-root` |

### Sibling skill 脚本（不重复造）

- **公告**：`ch-stock-skills/chstock-cninfo-announcement/scripts/cninfo_announcement_search.py`
  - 调用示例：`python cninfo_announcement_search.py --tabtype fulltext --date 2026-05-15~2026-05-23 --stock 300017 --output outputs/announcements.json`
- **互动问答**：`ch-stock-skills/chstock-interactive-qa/scripts/interactive_qa_search.py`
  - 调用示例：`python interactive_qa_search.py --company 中际旭创 --date-from 2026-04-23 --date-to 2026-05-23 --limit 30 --output outputs/qa.json`

### 依赖

```bash
pip install tushare requests
export TUSHARE_TOKEN=...
# 用到 AlphaVault 查询协议时还需指向 AlphaVault 根目录（含 system/tools/query_source_registry.py）
export ALPHAVAULT_ROOT="/path/to/Obsidian-Vault/1-AlphaVault"
```

`ALPHAVAULT_ROOT` 缺失时，`query_alphavault_raw.py` 与 `build_attribution_pack.py` 会跳过库内回溯并在 Pack 的 `gaps` 段显式标注"AlphaVault 库内回溯未执行"，不报错退出。

## Evidence Pack 格式（沿用 00-治理总则.md §标准 Pack 格式）

```yaml
pack_type: Evidence Pack
pack_subtype: AttributionEvidence
task_id:
created_at:
producer: chstock-rise-attribution
input_sources:
source_paths:        # 候选页面 / 库内 Raw / 新抓 Raw 路径
source_urls:         # 公告 url / 互动问答 url / Web url
related_pages:       # wiki/01-个股词条/XXX.md
data_points:         # 涨幅、换手、量比、同行业均值、涨停状态
claims:
  - claim:
    angle: 基本面|资金面
    subcategory: A1_产业信息|A2_高权重研报|A3_行业边际变化|A4_主题概念炒作|B1_K线资金流|B2_板块资金流
    tier: 主因|次因|辅助|推测|未解释    # 内部记录全档；回答只输出高/中置信
    source_org:
    source_weight: 高|中|低
    source_path:
    page_path:
    confidence:
    raw_excerpt_or_summary:
    tracking_relevance:
    conflict:
    gap:
analysis:            # 基本面 + 资金面两角度六子项逐项判断（内部完整记录，含反例排查与未解释部分）
conflicts:           # 多源差异 >10% 或方向相反
confidence:
recommended_action:  # 是否需要追加 Raw 回溯 / 是否值得形成研究报告 / 是否新增跟踪事项
raw_storage_path:
write_permission: false
```

完整示例见 [examples/sample_evidence_pack.json](examples/sample_evidence_pack.json)。

## 回答前自检（对齐 20-查询协议.md §回答前自检）

回答前必须逐条检查（Pack 内部仍保留全档证据，下面这些检查只约束**最终回答的可见内容**）：

1. 回答中每条催化是否带机构 + 信源权重（高/中），且权重为高或中——**低权重一律不出现在回答**；
2. 回答是否**只含**中高置信归因，**已剔除**：未能解释部分、推测候选、反例排查、跟踪事项建议、信源冲突段、skill 执行回溯、能力边界声明（"不接 L2"之类）；
3. 中高置信归因 0 条时是否退化为单句"`{name} {date} +X.XX% 的归因证据不足`"，没有罗列尝试过哪些来源；
4. `板块普涨 vs 孤立大涨` 是否已用同申万一级行业 pct_chg 均值对比（用于决定基本面 vs 资金面权重）；
4b. 回答是否分**基本面**和**资金面**两段输出，且每段都给到对应子项（A1~A4 / B1~B2）的具体证据；
5. Raw 原文回溯是否经过 SubAgent / 工作单元并以 Evidence Pack 形式交付——Raw 原文是否被错误地塞进主上下文；
6. 总长度是否 < 250 字（不含 Sources）、主条目 ≤ 3；
7. Pack 的 `write_permission` 是否为 `false`。

冲突、单一来源、跟踪事项 TODO、未解释部分仍**完整记入 Pack 的 `claims` / `conflicts` / `gaps` 字段**，供后续报告写入流程使用，**只是不进入对话回答**。

## 输出规范

最终回答**只输出中高置信归因**，**严格省略**以下内容：

- ❌ 未能解释部分 / 剩余涨幅 / 无明确催化 类陈述
- ❌ 推测候选 / 仅时间或仅机制对齐 类低置信项
- ❌ 反例排查 / "看似相关但被否决" 类排除说明
- ❌ 跟踪事项建议 / TODO 写入建议
- ❌ 信源冲突提示段（冲突直接体现在置信度分档里，不单列）
- ❌ skill 执行回溯 / 工作流透明化 / 自我打分
- ❌ 任何"本 skill 不接 L2"之类的能力边界陈述

风格：精炼、结论先行、信息密度高，**总长度 < 300 字**（不含 Sources）。**强制按"基本面 / 资金面"两段输出**，每段至少一条；某段全无中高置信归因时该段写"—"占位（不要解释为什么没有）。结构如下：

```markdown
**{name} ({ts_code}) · YYYY-MM-DD · +X.XX%**
换手 X% / 量比 X / vs 同行业 +Y.YY%（孤立大涨 | 板块普涨）/ 连涨第 N 日

**一、基本面**

1. **[A1 产业信息]{标题}**（高/中）—— 一句话事实 + 证据来源
2. **[A2 高权重研报]{标题}**（高/中）—— 一句话事实 + 证据来源
3. **[A4 主题概念炒作]{主题}**（中）—— 多个关联线索时用 sub-bullet：
   - **{子主题}**：一句话
   - **{子主题}**：一句话

**二、资金面**

1. **[B1 K 线/个股资金流]{标题}**（高/中）—— 主力 +X 万 / 游资 +X 万 / 散户 +X 万 + 量价形态描述
2. **[B2 板块资金流]{标题}**（高/中）—— 申万一级 X% / 板块龙头封板情况

Sources：[标题1](url1) · [标题2](url2) · ...
```

**判定门槛**：

- 一条催化只在置信度为**高**或**中**时才进入输出；**低置信、推测、未解释的内容一律不写**。
- 单段最多 3 条主条目（每条可含 sub-bullet）；两段合计 ≤ 5 条。
- 两段都为空时（基本面 / 资金面均无中高置信归因），整体退化为一句话："`{name} {date} +X.XX% 的归因证据不足`"，不要罗列尝试过哪些来源、不要解释为什么找不到、不要建议下一步动作。
- 每条前缀**必须**标 `[A1/A2/A3/A4/B1/B2]` 子项标签，便于读者直接定位归因角度。

## 示例

### Input

> 帮我归因一下中际旭创 2026-05-23 涨 8.5% 的催化。

### 执行

```bash
cd ch-stock-skills/chstock-rise-attribution

# 一把梭编排（默认会跑库内回溯 + 量价 + 公告 + 互动问答）
python scripts/build_attribution_pack.py \
  --name 中际旭创 \
  --ts-code 300308.SZ \
  --trade-date 2026-05-23 \
  --pct-chg 8.5 \
  --alphavault-root "$ALPHAVAULT_ROOT" \
  --output outputs/中际旭创_20260523_pack.json
```

如果只想跑库内 + 量价、不抓外部：

```bash
python scripts/build_attribution_pack.py \
  --name 中际旭创 \
  --ts-code 300308.SZ \
  --trade-date 2026-05-23 \
  --pct-chg 8.5 \
  --skip-external \
  --output outputs/中际旭创_20260523_pack_internal.json
```

### Output 摘要

```markdown
**中际旭创 (300308.SZ) · 2026-05-23 · +8.50%**
换手 5.2% / 量比 2.3 / vs 通信设备 +2.10%（孤立大涨）

**一、基本面**

1. **[A1 产业信息] 1.6T 光模块大客户订单确认**（高）—— 5/22 晚间公告披露与北美云厂签订重大合同
2. **[A2 高权重研报] Citi 跟随上调目标价**（高）—— 5/23 早间报告确认 1.6T 出货节奏与订单可见度
3. **[A3 行业边际变化] 海外算力 CapEx 加速**（中）—— 同日 NVDA 财报 +4.1%，北美云厂 CapEx 指引上修

**二、资金面**

1. **[B1 K 线] 量比 2.3 放量加速**（高）—— 换手 5.2% 创近 60 日新高，非涨停但单日成交破 40 亿
2. **[B2 板块资金流] 通信设备 +2.1%**（中）—— 申万一级板块普涨贡献约 1/4 涨幅，新易盛/天孚通信同步联动

Sources：[5/22 重大合同公告](http://static.cninfo.com.cn/...) · [Citi 跟随报告](...) · [NVDA Q1 财报](...) · [通信设备板块表现](...)

## 五、未能解释部分
扣除主因、次因后剩余涨幅约 2.5pct 无明确催化；可能涉及融资盘加仓或小作文，需进一步查龙虎榜（本 skill 不接 L2 资金流）。
```

完整 Evidence Pack JSON 见 `examples/sample_evidence_pack.json`。
