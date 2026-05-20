---
name: tushare-daily-market-sense
description: 基于 Tushare Pro A 股 daily 日线数据生成盘后市场研报的方法论 skill。当用户要求做每日盘面趋势、上证/创业板指数趋势、情绪指数趋势、赚钱效应与上涨主线分析、爆量下跌识别、低位异动/科创板月线突破/10:30前涨停等特征分组分析、历史某日复盘、基于 daily/daily_basic/涨跌停/指数数据做量化选股观察时，必须优先使用本 skill。本 skill 先生成确定性证据包，再由模型或 Codex/Claude Code 等通用 agent 的 subagent 编排能力按模块撰写；不在脚本中调用 LLM，不提供买卖建议，不按申万、同花顺、东方财富等现成行业/概念口径分组。
version: 2.0.2
---

# Tushare Daily Market Sense

## 目标

基于 Tushare 日线、指数、成交额与本地情绪历史，为 A 股盘后复盘生成结构化研报：盘面趋势、成交额集中度、赚钱效应与上涨主线、爆量下跌风险、特征分组分析。

不做单股基本面深度研究、港股/美股/基金/期货/加密分析、分钟级交易决策、自动下单、组合优化或买卖建议。脚本只负责取数、计算、筛选、切分 JSON；主题归纳、风险措辞和研报写作由模型完成。

## 核心理念

成交额优先。所有强弱判断都要有成交额证据：上涨主线按成交额厚度确认，爆量下跌按放量异常与跌幅强度识别，特征分组按命中规则与成交额证据分开呈现。

主题主线由模型基于业务事实临时归纳，不套现成行业或概念标签。共同性不足时明确写“暂不构成主线”或“资金轮动”。

## 工作流程

1. 确定交易日：解析“今天/最近”或具体日期，默认只使用 `D` 及以前数据；只有用户明确要求后验时才允许 `--allow-future`。
2. 生成证据包：运行 `scripts/run_daily_panel.py`。脚本会直接调用数据管线，写出完整 evidence、轻量 `report_context` 和模块级 JSON。
3. 选择撰写模式：
   - 有 subagent 编排能力时，主 agent 将 5 个模块 JSON 分发给 5 个 subagent 并行撰写。
   - 没有 subagent 能力时，按同样模块顺序单会话执行，每次只加载当前模块的 JSON、方法论和模板段。
4. 聚合成稿：主 agent 读取 6 段输出、`assembled_checks.json` 与 `reference/methodology/output_discipline.md`，补一句话盘面判断、风险传导提示和最终语气校准。默认不做外部收评校验、不搜索第三方行情综述、不在报告中加入“外部校验参考”；只有用户明确要求时才补充外部来源。
5. 按需生成 HTML：当用户要求 HTML、网页、可视化报告或截图风格输出时，先完成并核对 `reports/report_YYYYMMDD.md`，再运行 `scripts/render_report_html.py` 生成同日期 HTML。HTML 是展示层产物，不新增研报判断、不删减 Markdown 正文；若同目录存在 `evidence_YYYYMMDD_utf8.json`，HTML 会自动读取其中的上证指数、创业板指数 120 日 K 线并插入对应指数趋势分析前。
6. 清理临时产物：确认 `reports/report_YYYYMMDD.md` 已写入并可读后，删除同日期的临时证据与上下文文件，只保留最终报告。若已按需生成 HTML，则同时保留 `reports/report_YYYYMMDD.html`。必须清理：
   - `reports/evidence_YYYYMMDD_utf8.json`
   - `reports/evidence_YYYYMMDD_utf8.stderr.log`
   - `reports/report_context_YYYYMMDD.json`
   - `reports/module_context_YYYYMMDD/`
   不要删除 `reports/report_YYYYMMDD.md` 和已生成的 `reports/report_YYYYMMDD.html`，不要跨日期批量清理，除非用户明确要求。

## 数据获取

环境变量：

```bash
TUSHARE_TOKEN=your_token
```

运行脚本会先更新 `reference/market_data.csv`，并同步维护派生文件 `reference/market_data.json`，再生成情绪趋势：AKShare `stock_market_activity_legu()` 默认提供上涨、涨停、下跌、跌停、平盘、活跃度、情绪值、成交额等盘面情绪字段；搜狐涨跌停历史页仅在 AKShare 不可用或字段缺失时作为 fallback；Tushare `margin` 汇总 T-1 交易日融资净买入，Tushare `daily` 与 `daily_basic.circ_mv` 计算流通市值加权的全市场换手率。因此环境中还需安装 `akshare`。

基础命令：

```powershell
cd C:\Users\chenh\OneDrive\skills\stock-skills\a-stock-daily-market-sense
python scripts\run_daily_panel.py --asof 20260429 --lookback 120 --market-trend-days 90 --index 000300.SH
```

主要输出：

- `reports/evidence_YYYYMMDD_utf8.json`：完整证据包。
- `reports/report_context_YYYYMMDD.json`：兼容旧流程的轻量上下文。
- `reports/module_context_YYYYMMDD/`：供 subagent 分工的模块级 JSON。
- `reference/market_data.json`：`market_data.csv` 的全量派生 JSON，按交易日升序保留所有列、清洗数值并提供 `series` 给 HTML 趋势图使用。

这些文件中，evidence、report_context 和 module_context 是研报撰写过程中的临时产物。最终报告生成并核对后，应按工作流程第 6 步删除，只保留 `reports/report_YYYYMMDD.md`、按需生成的 `reports/report_YYYYMMDD.html`，以及长期维护的 `reference/market_data.csv` / `reference/market_data.json`。

HTML 输出命令：

```powershell
python scripts\render_report_html.py --input reports\report_20260429.md
```

默认输出 `reports/report_20260429.html`，并将 `reference/market_data.json` 内嵌到 HTML 中。本地浏览器可直接打开，图表不依赖外部 CDN。

常用参数：

| 参数 | 含义 | 默认 |
|---|---|---:|
| `--fetch-workers` | cache/API 获取线程数；排查限流时设为 1 | 6 |
| `--index-kline-days` | HTML 上证/创业板 K 线展示窗口，独立于 `--market-trend-days` | 120 |
| `--money-pct-threshold` | 赚钱效应最低当日涨幅 | 7.0 |
| `--money-amount-threshold` | 赚钱效应最低成交额，单位亿元 | 2.0 |
| `--intraday-freq` | 赚钱效应候选股分钟增强频率；用于日内高点时间 | 1min |
| `--skip-intraday` | 跳过分钟行情增强；无 Tushare 分钟权限或排查限流时使用 | false |
| `--intraday-workers` | 分钟行情抓取线程数；保持小并发避免限流 | 2 |
| `--decline-pct-max` | 爆量下跌最大当日涨幅 | -3.0 |
| `--decline-volume-ratio` | 爆量下跌最低 20 日放量倍数 | 2.0 |
| `--low-lookback-days` | 低位放量触发回看窗口 | 5 |

## Subagent 编排契约

主 agent 先生成模块级 JSON，然后按下列最小上下文分发。每个 subagent 只看自己的模块数据，不读取其他模块数据。

| 模块 | JSON | 方法论 | 模板 |
|---|---|---|---|
| 1 盘面趋势 | `module1_market_trend.json` | `reference/methodology/module1_trend.md` | `reference/template/section1.md` |
| 2 集中度 | `module2_concentration.json` | `reference/methodology/module2_concentration.md` | `reference/template/section2.md` |
| 3 赚钱效应 | `module3_money_effect.json` | `reference/methodology/module3_money_effect.md` | `reference/template/section3.md` |
| 4 爆量下跌 | `module4_decline.json` | `reference/methodology/module4_decline.md` | `reference/template/section4.md` |
| 5 特征分组 | `module5_feature_groups.json` | `reference/methodology/module5_feature_groups.md` | `reference/template/section5.md` |

聚合 agent 额外读取：

- `assembled_checks.json`：M3 赚钱效应池与 M4 爆量下跌池的确定性交叉检查。
- `reference/methodology/output_discipline.md`：最终成稿纪律。

Python 不调用 Anthropic API、不调用任何 LLM、不硬编码模型名。Codex、Claude Code 或其他通用 agent 的 subagent 编排能力负责并行撰写。

## 输出规范

完整研报按五个模块输出。每段结论先行，表格只放关键证据，所有强弱判断必须有成交额或放量倍数支撑。不要写“板块轮动明显”这类空句；要写“候选数、合计成交额、最大主题占比、代表股成交额”。

每个一级大章节（1-5）里已有的总结/定性段落必须使用 Markdown 高亮样式 `==...==` 包裹，例如“盘面定性”“拥挤度判断”“主线 vs 资金轮动结论”“风险传导提示”“特征分组一句话判断”。不要为了高亮额外新增“本节总结”段落；高亮的是原本就承担总结作用的段落。

禁止输出买卖建议。可以写“风险传导”“持续性待验证”“主线确认度”，不要写“买入/卖出/止损/目标价”。

HTML 输出只改变呈现方式：必须保留 Markdown 研报中的所有文字、表格、引用和免责声明。`==...==` 高亮段落在 HTML 中渲染为浅蓝提示块；正文前可以增加 `market_data.json` 驱动的趋势图区域。若可读取同日期 evidence，HTML 可以在“上证指数趋势”“创业板指数趋势”正文前插入对应指数的 120 日 K 线图，并在图中展示成交金额柱；也可以在 3.3、5.2、5.3 股票明细表下方插入表内股票的 120 日 K 线图。这些图表只展示 evidence 中已有的 OHLC 与成交金额数据，不得新增与 Markdown 不一致的分析结论。

## 示例

用户：`复盘 2026-04-29 的 A 股盘面，重点看赚钱效应和低位放量。`

执行：

```powershell
python scripts\run_daily_panel.py --asof 20260429
```

然后按 subagent 契约加载 `reports/module_context_20260429/` 下的模块 JSON。若没有 subagent，就顺序加载每个模块的 JSON + 方法论 + 模板段，最后聚合为完整研报。
