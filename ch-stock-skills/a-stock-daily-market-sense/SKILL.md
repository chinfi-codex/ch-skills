---
name: a-stock-daily-market-sense
description: 基于 Tushare Pro A 股日线与 Baostock 风格指数生成盘后市场研报。当用户要求复盘每日/历史 A 股盘面、指数与市场风格、赚钱效应与上涨主线、爆量下跌、特征分组，或做特征分组因子挖掘时使用。脚本只输出确定性证据与统计；主题归纳、星级评定、研报写作由模型完成。不提供买卖建议。
---

# Tushare Daily Market Sense

## 目标

基于 Tushare 日线、指数、成交额与本地情绪历史，为 A 股盘后复盘生成结构化研报：盘面趋势、赚钱效应与上涨主线、爆量下跌风险、特征分组分析。

不做单股基本面深度研究、港股/美股/基金/期货/加密分析、超短线交易决策、自动下单、组合优化或买卖建议。脚本只负责取数、计算、筛选、切分 JSON；主题归纳、风险措辞和研报写作由模型完成。

## 核心理念

**成交额优先。** 所有强弱判断都要有成交额证据：上涨主线按成交额厚度确认，爆量下跌按放量异常与跌幅强度识别，特征分组按命中规则与成交额证据分开呈现。

主题主线由模型基于业务事实临时归纳，不套现成行业或概念标签。共同性不足时明确写“暂不构成主线”或“资金轮动”。

## 适用场景与边界

- 每日/历史 A 股盘后复盘。
- 指数与市场风格、情绪趋势分析。
- 赚钱效应与上涨主线、即时及短中期催化、主线细分线路。
- 爆量下跌、特征分组（容量上涨、月线平台突破、10:30 前涨停、折扣启动等）。
- 特征分组量化回溯与相对收益因子挖掘（独立研究线，不进日报）。

不用于盘中实时监控、个股深度基本面研究、买卖建议。默认只使用 D 及以前数据；只有用户明确要求后验时才允许 `--allow-future`。

## 工作流程

1. **生成证据包**：解析日期后运行 `scripts/run_daily_panel.py`，产出完整 evidence、模块级 JSON 与 K 线展示数据；模块 1 同时带三张机判卡——趋势状态卡（`trend_state_card`，所处阶段）、极值状态（`extreme_state`，底部出清分 / 顶部拥挤分）与前瞻轴（`forward_odds`，情绪脉冲 + 同类日之后的条件分布）。详见 `references/execution_flow.md`。
2. **分模块撰写**：按最小上下文边界加载各模块 JSON + 方法论 + 模板。模块编号沿用历史值、章节按 1-4 重排（模块 3 → 第 2 章赚钱效应，模块 4 → 第 3 章，模块 5 → 第 4 章）。模块 3 赚钱效应采用两阶段契约：首轮只输出临时主题映射与 `stars: null`；统计脚本完成后由模型按证据锁定星级，只有存在 ★★★ 主线时才触发 2.2 催化与细分线路推演。详见 `references/methodology/module3_money_effect.md` 与 `references/execution_flow.md`。
3. **聚合成稿**：读取模块 1、3、4、5 输出与 `references/methodology/output_discipline.md`，补一句话盘面判断、风险传导提示和语气校准。
4. **门禁晋级**：模型只把草稿写入 `reports/.staging/`；用 `report.finalize-markdown` 校验结构、数值证据与禁用语后原子晋级。HTML 只能由 `report.render-html` 基于已有成功收据的 Markdown 生成。
5. **生命周期落库与清理**：报告通过门禁后，把主线判定沉淀进 PG 生命周期台账——这是 HTML 渲染的**前置条件**，台账缺当日记录时 `report.render-html` 直接失败（泳道图会照画，但最后一列是空的）；只有最终收据与审计均通过才清理临时 evidence。详见 `references/theme_lifecycle.md` 与 `references/execution_flow.md`。

## 数据获取

环境变量：

```bash
TUSHARE_TOKEN=your_token
ALPHA_DB_BACKEND=postgresql
ALPHA_PG_URL=postgresql://alpha_user:alpha_pass@/alpha_data?host=/tmp
TAVILY_API_KEY=your_token  # 催化搜索主路径；缺失时可由宿主 Web Search 降级
```

数据库连接统一走 `scripts/_shared/db_core.py`（开发仓库中为 `shared/data/db_core.py`）。首次进入任意 Agent 环境时先运行 `python3 scripts/_shared/db_ping.py --alpha-schema`。

首次安装依赖时运行：

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

第二条命令安装渲染门禁所需的 Chromium；只安装 Python 包还不能运行浏览器门禁。

生产执行统一走能力运行时。同步后的 Skill 使用 `scripts/_shared/skill_runtime/runner.py`；源仓库开发态对应 `../../shared/skill_runtime/runner.py`。底层脚本只用于调试，直接调用不会产生可交付收据。

基础命令：

```bash
python3 scripts/_shared/skill_runtime/runner.py --skill-root . run daily.build-evidence -- \
  --asof 20260429 --lookback 120 --market-trend-days 90 --index 000300.SH
```

极值状态卡的分位基准存在 `dms_extreme_daily` 表里，**新环境第一次跑要先补历史**（只需一次，之后每日增量）：

```bash
python3 scripts/extreme_state_card.py --asof 20260429 --backfill 300
```

没补历史也能出卡，只是阈值会退回固定水平，`percentile_source` 会写明。

前瞻轴不需要预热——它直接读 `market_history` 的 6 年市场宽度和拼接后的中证1000 序列，滚动分位攒满 250 天即生效：

```bash
python3 scripts/forward_odds.py --asof 20260429          # 随卡输出，未命中的信号只留存根
python3 scripts/forward_odds.py --asof 20260429 --full   # 附事件清单与逐年留一，调参时用
```

模块 3 首轮完成后，运行统计脚本：

```bash
python3 scripts/_shared/skill_runtime/runner.py --skill-root . run daily.compute-theme-stats -- \
  --context reports/module_context_YYYYMMDD/module3_money_effect.json \
  --mapping reports/module_context_YYYYMMDD/module3_theme_map.json \
  --output reports/module_context_YYYYMMDD/module3_theme_stats.json
```

模型聚合后先写暂存稿，再完成 Markdown 和 HTML 两阶段晋级：

```bash
python3 scripts/_shared/skill_runtime/runner.py --skill-root . run report.finalize-markdown \
  --output-id dms-markdown --staged-path reports/.staging/report_20260429.md \
  --final-path reports/report_20260429.md --evidence reports/evidence_20260429_utf8.json

python3 scripts/_shared/skill_runtime/runner.py --skill-root . run report.render-html \
  --output-id dms-html --final-path reports/report_20260429.html \
  --source-artifact reports/report_20260429.md --evidence reports/evidence_20260429_utf8.json -- \
  --theme default
```

运行时把结果写到 `.staging/<run_id>/`，核对 evidence 与同日前置收据的路径和 SHA-256，完成内容、文本保全和浏览器门禁后才用原子替换写入正式路径。成功 audit 按运行保存在 `reports/.audits/<run_id>/`；失败时不得发布或 cleanup，保留暂存件、`reports/.receipts.jsonl` 与失败 audit，并在回复中给出 `run_id`、审计路径和首批失败项。完整规则见 `scripts/_shared/output_gate/references/output_gate.md`。

复制到 Site 或上线后仍要复验同一个构建，防止搬运或 CSP 破坏页面：

```bash
python3 scripts/_shared/html_report/render_check.py --target reports/report_20260429.html --stage local --out reports/report_20260429.render-check-local.json
# 从上一步审计 JSON 读取 build_id；site/online 必须与本地产物完全一致
BUILD_ID=$(python3 -c 'import json; print(json.load(open("reports/report_20260429.render-check-local.json"))["build_id"])')
python3 scripts/_shared/html_report/render_check.py --target <Site 路径>/report_20260429.html --stage site --expect-contract dms/1.5.0 --expect-build "$BUILD_ID" --out reports/report_20260429.render-check-site.json
python3 scripts/_shared/html_report/render_check.py --target <线上 URL> --stage online --expect-contract dms/1.5.0 --expect-build "$BUILD_ID" --out reports/report_20260429.render-check-online.json
```

退出码：`0` 通过、`1` 失败（**不得部署、不得 cleanup，留着审计文件排查**）、`2` 只跑了 `--static-only` 冒烟不算门禁。Site/online 审计留在 `reports/` 下，不进发布包。

契约与门禁的工作方式见 `references/render_contract.md`；改章节结构时要同步升 `scripts/render_report_html.py` 里 `DMS_CONTRACT` 的版本号。

常用参数与 `factor_backtest.py` / `factor_lab.py` 说明见 `references/cli_reference.md`。

## 输出规范

完整研报按四个章节输出：1 盘面趋势、2 赚钱效应与上涨主线、3 亏钱效应（爆量下跌）、4 特征分组分析。每个判断段先给自然语言结论，再选择少量关键证据支撑；表格承载细项数据，段落解释这些数据意味着进攻、分歧、退潮、修复、拥挤还是扩散。

1.1 情绪趋势按 `references/template/section1.md` 保持趋势状态卡结构：卡面读数逐项照抄 `trend_state_card`、`extreme_state` 与 `forward_odds`，三根轴不一致时并列写；小节末尾的 `==趋势判断==` 是模块 1 唯一下方向性结论的位置。

所有强弱判断都要能回到成交额、放量倍数、涨跌幅、相对收益或回撤证据，但不要把所有可用指标塞进同一段。模块 3 的主题分组只作为内部推理步骤，不输出单独的主题分组陈列表，赚钱效应总览后直接进入主线判定；2.1 主线表的拥挤度列读 `amount_concentration` 的全市场成交额榜定档，成交额集中度本身不再单独成章。

遵循仓库项目级文风默认：讲人话、减少模板腔；同项罗列用 list 但每条说人话，结构化对照用表格。

每个一级大章节（1-4）里已有的总结/定性段落使用 Markdown 高亮样式 `==...==` 包裹。不要为了高亮额外新增“本节总结”段落。

禁止输出买卖建议。可以写“风险传导”“持续性待验证”“主线确认度”，不要写“买入/卖出/止损/目标价”。

**前瞻轴给的是条件分布，不是预测。** `forward_odds` 回答的是"历史上出现同类读数之后发生了什么"——引用时必须带样本量与全样本基准对照（"历史上 14 次同类日之后，+3 日 84.6% 收涨、均值 +3.40%，全样本基准 52.5% / +0.06%"），不得压缩成"大概率会涨""反弹在即"这类去掉了样本与基准的断言（契约会硬拦）。`pulse` 是四腿合取门槛不是分数，不得据腿数定性；只能引用 `gate_detail.subsample_consistent.horizons` 里列出的视窗。**顶部侧一律只输出回撤风险分布、不输出方向**，也不进 `==趋势判断==`——实证里多数顶部条件之后的前瞻收益仍为正，唯一站得住的只有"尾部变肥"。详见 `references/methodology/forward_odds.md`。

**退潮 / 深度退潮 / 冰点是风险状态描述，不等于看空。** 5 年回放（`evals/trend_state_review_2026-08.md`）里，冰点日之后 20 个交易日平均上涨 8.92%、81.8% 的时候在涨，深度退潮 +1.54%，都高于全样本的 +0.44%——档位越差前瞻收益反而越好。写模块 1 时不得把"退潮"翻译成"看空"，也不得暗示应当离场或减仓。同理，趋势轴与极值轴不一致时（退潮档里出现出清极值是 A 股最常见的底部形态）照实并列写，不许为了口径一致改写任一边的读数。

HTML 输出只改变呈现方式：必须保留 Markdown 研报中的所有文字、表格、引用和免责声明。图表只展示 evidence 中已有的 OHLC、成交金额与风格指数收盘序列数据，不得新增与 Markdown 不一致的分析结论。详见 `references/methodology/output_discipline.md`。

## 示例

### Input

用户：`复盘 2026-04-29 的 A 股盘面，重点看赚钱效应和容量上涨。`

### 执行

```bash
python3 scripts/_shared/skill_runtime/runner.py --skill-root . run daily.build-evidence -- --asof 20260429
python3 scripts/_shared/skill_runtime/runner.py --skill-root . run daily.compute-theme-stats -- \
  --context reports/module_context_20260429/module3_money_effect.json \
  --mapping reports/module_context_20260429/module3_theme_map.json \
  --output reports/module_context_20260429/module3_theme_stats.json
```

按 `references/execution_flow.md` 的最小上下文边界，加载各模块 JSON + 方法论 + 模板，逐模块撰写后聚合。

### Output 片段

```markdown
# A 股趋势复盘 - 2026-04-29

## 今晚一句话
==指数震荡收红，成交额向头部主线集中；赚钱效应落在半导体设备与容量上涨两个方向，但后者仍缺细分共振，暂评 ★★。爆量下跌池有 3 只前期高位票放量破位，风险传导可控。==

## 1. 盘面趋势
今日沪指 +0.3%、创业板指 +0.8%，两市成交额 1.08 万亿，较前 20 日均量放大 8%。风格上小盘成长跑赢大盘价值约 1.2pp，但成交额占比未出现极端偏离，市场仍在存量轮动区间。

## 2. 赚钱效应与上涨主线
| 主线 | 星级 | 成交额占比 | 位置 | 拥挤度 | 领导股 | 催化逻辑 |
|---|---:|---:|---|---|---|---|
| 半导体设备 | ★★★ | ~18% | 趋势中段 | 中 | 北方华创、中微公司 | 国产线招标加速 |
| 容量上涨 | ★★ | ~9% | 低位修复 | 低 | 宁德时代、比亚迪 | 动力电池排产回暖 |

## 4. 特征分组
容量上涨今日命中 12 只，成交额中位数 23 亿，较前 20 日放大 1.8 倍；10:30 前涨停 5 只，封板资金集中在半导体与光伏。月线平台突破组 3 只，均伴随放量，但板块分散，未形成新主线。
```
