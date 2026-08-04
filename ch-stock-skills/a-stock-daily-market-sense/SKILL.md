---
name: a-stock-daily-market-sense
description: 基于 Tushare Pro A 股日线与 Baostock 风格指数生成盘后市场研报。当用户要求复盘每日/历史 A 股盘面、指数与市场风格、赚钱效应与上涨主线、爆量下跌、特征分组，或做特征分组因子挖掘时使用。脚本只输出确定性证据与统计；主题归纳、星级评定、研报写作由模型完成。不提供买卖建议。
---

# Tushare Daily Market Sense

## 目标

基于 Tushare 日线、指数、成交额与本地情绪历史，为 A 股盘后复盘生成结构化研报：盘面趋势、成交额集中度、赚钱效应与上涨主线、爆量下跌风险、特征分组分析。

不做单股基本面深度研究、港股/美股/基金/期货/加密分析、超短线交易决策、自动下单、组合优化或买卖建议。脚本只负责取数、计算、筛选、切分 JSON；主题归纳、风险措辞和研报写作由模型完成。

## 核心理念

**成交额优先。** 所有强弱判断都要有成交额证据：上涨主线按成交额厚度确认，爆量下跌按放量异常与跌幅强度识别，特征分组按命中规则与成交额证据分开呈现。

主题主线由模型基于业务事实临时归纳，不套现成行业或概念标签。共同性不足时明确写“暂不构成主线”或“资金轮动”。

## 适用场景与边界

- 每日/历史 A 股盘后复盘。
- 指数与市场风格、情绪、成交额集中度分析。
- 赚钱效应与上涨主线、即时及短中期催化、主线细分线路。
- 爆量下跌、特征分组（容量上涨、月线平台突破、10:30 前涨停、折扣启动等）。
- 特征分组量化回溯与相对收益因子挖掘（独立研究线，不进日报）。

不用于盘中实时监控、个股深度基本面研究、买卖建议。默认只使用 D 及以前数据；只有用户明确要求后验时才允许 `--allow-future`。

## 工作流程

1. **生成证据包**：解析日期后运行 `scripts/run_daily_panel.py`，产出完整 evidence、模块级 JSON 与 K 线展示数据；模块 1 同时带机判的市场状态定位（`market_state`）与趋势状态卡（`trend_state_card`）区块。详见 `references/execution_flow.md`。
2. **分模块撰写**：按最小上下文边界加载各模块 JSON + 方法论 + 模板。模块 3 赚钱效应采用两阶段契约：首轮只输出临时主题映射与 `stars: null`；统计脚本完成后由模型按证据锁定星级，再进入催化与细分线路推演。详见 `references/methodology/module3_money_effect.md` 与 `references/execution_flow.md`。
3. **聚合成稿**：读取模块 1-5 输出与 `references/methodology/output_discipline.md`，补一句话盘面判断、风险传导提示和语气校准。
4. **生命周期落库与清理**：报告定稿后，把主线判定沉淀进 PG 生命周期台账，然后清理临时 evidence。详见 `references/theme_lifecycle.md` 与 `references/execution_flow.md`。

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

基础命令：

```bash
python3 scripts/run_daily_panel.py --asof 20260429 --lookback 120 --market-trend-days 90 --index 000300.SH
```

模块 3 首轮完成后，运行统计脚本：

```bash
python3 scripts/theme_group_stats.py \
  --context reports/module_context_YYYYMMDD/module3_money_effect.json \
  --mapping reports/module_context_YYYYMMDD/module3_theme_map.json \
  --output reports/module_context_YYYYMMDD/module3_theme_stats.json
```

HTML 输出（`--gate` 会在写完文件后用浏览器验一遍渲染契约，不通过就非零退出）：

```bash
python3 scripts/render_report_html.py --input reports/report_20260429.md --gate [--theme default|claude|print]
```

**上线前必须过渲染门禁。** 报告的图表全部是页面自己在浏览器里画出来的，静态看文件看不出图表画到哪儿了、有没有画。所以本地产物、复制到 Site 之后、线上页面三处各跑一次同一套检查，全绿才允许部署，全绿才允许 cleanup：

```bash
python3 scripts/_shared/html_report/render_check.py --target reports/report_20260429.html --stage local --out reports/report_20260429.render-check-local.json
# 从上一步审计 JSON 读取 build_id；site/online 必须与本地产物完全一致
BUILD_ID=$(python3 -c 'import json; print(json.load(open("reports/report_20260429.render-check-local.json"))["build_id"])')
python3 scripts/_shared/html_report/render_check.py --target <Site 路径>/report_20260429.html --stage site --expect-contract dms/1.0.0 --expect-build "$BUILD_ID" --out reports/report_20260429.render-check-site.json
python3 scripts/_shared/html_report/render_check.py --target <线上 URL> --stage online --expect-contract dms/1.0.0 --expect-build "$BUILD_ID" --out reports/report_20260429.render-check-online.json
```

退出码：`0` 通过、`1` 失败（**不得部署、不得 cleanup，留着审计文件排查**）、`2` 只跑了 `--static-only` 冒烟不算门禁。三份 `render-check-*.json` 留在 `reports/` 下，不进发布包。

契约与门禁的工作方式见 `references/render_contract.md`；改章节结构时要同步升 `scripts/render_report_html.py` 里 `DMS_CONTRACT` 的版本号。

常用参数与 `factor_backtest.py` / `factor_lab.py` 说明见 `references/cli_reference.md`。

## 输出规范

完整研报按五个模块输出。每个判断段先给自然语言结论，再选择少量关键证据支撑；表格承载细项数据，段落解释这些数据意味着进攻、分歧、退潮、修复、拥挤还是扩散。模块 1 开头先做市场状态定位（宽基与成长小盘的回撤分层、调整是否接近尾声，证据来自 `market_state` 区块，判断手册见 `references/methodology/market_state_framework.md`）。所有强弱判断都要能回到成交额、放量倍数、涨跌幅、相对收益或回撤证据，但不要把所有可用指标塞进同一段。模块 3 的主题分组只作为内部推理步骤，不输出单独的主题分组陈列表，赚钱效应总览后直接进入主线判定。

遵循仓库项目级文风默认：讲人话、减少模板腔；同项罗列用 list 但每条说人话，结构化对照用表格。

每个一级大章节（1-5）里已有的总结/定性段落使用 Markdown 高亮样式 `==...==` 包裹。不要为了高亮额外新增“本节总结”段落。

禁止输出买卖建议。可以写“风险传导”“持续性待验证”“主线确认度”，不要写“买入/卖出/止损/目标价”。

HTML 输出只改变呈现方式：必须保留 Markdown 研报中的所有文字、表格、引用和免责声明。图表只展示 evidence 中已有的 OHLC、成交金额与风格指数收盘序列数据，不得新增与 Markdown 不一致的分析结论。详见 `references/methodology/output_discipline.md`。

## 示例

### Input

用户：`复盘 2026-04-29 的 A 股盘面，重点看赚钱效应和容量上涨。`

### 执行

```bash
python3 scripts/run_daily_panel.py --asof 20260429
python3 scripts/theme_group_stats.py \
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

## 3. 赚钱效应与上涨主线
| 主线 | 星级 | 成交额占比 | 位置 | 拥挤度 | 领导股 | 催化逻辑 |
|---|---:|---:|---|---|---|---|
| 半导体设备 | ★★★ | ~18% | 趋势中段 | 中 | 北方华创、中微公司 | 国产线招标加速 |
| 容量上涨 | ★★ | ~9% | 低位修复 | 低 | 宁德时代、比亚迪 | 动力电池排产回暖 |

## 5. 特征分组
容量上涨今日命中 12 只，成交额中位数 23 亿，较前 20 日放大 1.8 倍；10:30 前涨停 5 只，封板资金集中在半导体与光伏。月线平台突破组 3 只，均伴随放量，但板块分散，未形成新主线。
```
