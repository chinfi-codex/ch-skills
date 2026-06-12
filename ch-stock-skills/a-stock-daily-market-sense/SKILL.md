---
name: a-stock-daily-market-sense
description: 基于 Tushare Pro A 股 daily 日线数据和 Baostock 风格指数生成盘后市场研报的方法论 skill。当用户要求做每日盘面趋势、上证/创业板/科创50指数趋势、市场风格判断、情绪指数趋势、赚钱效应与上涨主线分析、爆量下跌识别、容量上涨/科创板月线突破/10:30前涨停等特征分组分析、历史某日复盘、基于 daily/daily_basic/涨跌停/指数数据做量化选股观察时，必须优先使用本 skill。本 skill 先生成确定性证据包，再由模型或 Codex/Claude Code 等通用 agent 的 subagent 编排能力按模块撰写；不在脚本中调用 LLM，不提供买卖建议，不按申万、同花顺、东方财富等现成行业/概念口径分组。
version: 2.1.0
---

# Tushare Daily Market Sense

## 目标

基于 Tushare 日线、指数、成交额与本地情绪历史，为 A 股盘后复盘生成结构化研报：盘面趋势、成交额集中度、赚钱效应与上涨主线、爆量下跌风险、特征分组分析。

不做单股基本面深度研究、港股/美股/基金/期货/加密分析、超短线交易决策、自动下单、组合优化或买卖建议。脚本只负责取数、计算、筛选、切分 JSON；主题归纳、风险措辞和研报写作由模型完成。

## 核心理念

成交额优先。所有强弱判断都要有成交额证据：上涨主线按成交额厚度确认，爆量下跌按放量异常与跌幅强度识别，特征分组按命中规则与成交额证据分开呈现。

主题主线由模型基于业务事实临时归纳，不套现成行业或概念标签。共同性不足时明确写“暂不构成主线”或“资金轮动”。

## 工作流程

1. 确定交易日：解析“今天/最近”或具体日期，默认只使用 `D` 及以前数据；只有用户明确要求后验时才允许 `--allow-future`。
2. 生成证据包：运行 `scripts/run_daily_panel.py`。脚本会直接调用数据管线，写出完整 evidence、个股 K 线展示数据（`kline_YYYYMMDD.json`）和模块级 JSON。
3. 选择撰写模式：
   - 有 subagent 编排能力时，主 agent 将 5 个模块 JSON 分发给 5 个 subagent 并行撰写。
   - 没有 subagent 能力时，按同样模块顺序单会话执行，每次只加载当前模块的 JSON、方法论和模板段。
4. 聚合成稿：主 agent 读取 6 段输出、`assembled_checks.json` 与 `references/methodology/output_discipline.md`，补一句话盘面判断、风险传导提示和最终语气校准。默认不做外部收评校验、不搜索第三方行情综述、不在报告中加入“外部校验参考”；只有用户明确要求时才补充外部来源。
5. 主线生命周期落库：报告定稿后，把当日主线判定沉淀进 PG 生命周期台账。先运行 `python3 scripts/theme_lifecycle.py context --asof YYYYMMDD` 取注册表、各主线近期状态与 watchlist；模型完成别名归一（当日临时主题名 → canonical theme_id）和生命周期状态判定（低位启动/在场候选/主线确认/高位分歧/退潮/修复/再聚焦/沉寂），写出 `reports/lifecycle_YYYYMMDD.json` 后运行 `python3 scripts/theme_lifecycle.py record --input reports/lifecycle_YYYYMMDD.json` 落库。脚本只做确定性校验（枚举、状态机转移合法性、theme_id 存在性），判断留给模型；输入格式、状态机与判定基准见 `references/theme_lifecycle.md`。
6. 按需生成 HTML：当用户要求 HTML、网页、可视化报告或截图风格输出时，先完成并核对 `reports/report_YYYYMMDD.md`，再运行 `scripts/render_report_html.py` 生成同日期 HTML。HTML 是展示层产物，不新增研报判断、不删减 Markdown 正文；若同目录存在 `evidence_YYYYMMDD_utf8.json` 与 `kline_YYYYMMDD.json`，HTML 会自动读取指数 120 日 K 线与个股 K 线并插入对应正文附近。若 `theme_daily_state` 已有该日数据，HTML 还会在主线判定小节下方自动注入主线生命周期泳道图区块（近 22 个交易日，红 = 强势在场、绿 = 退潮、闪电 = 低位启动；`--lifecycle-days` 调窗口、`--no-lifecycle` 关闭）；区块只展示台账已落库数据，不新增判断。若 evidence 含风格序列，HTML 会在「市场风格」小节表格下方自动注入两张 60 日归一化对比图（规模轴五线 / 成长价值红利三线，起点=100），区块只展示 evidence 已有数据、不新增判断。
7. 证据包边界：`reports/evidence_YYYYMMDD_utf8.json` 是本 skill 的 Market Evidence Pack，只属于 skill 输出目录。即使在 AlphaVault 中写入趋势复盘，也不要把该证据包复制或登记为 `RAW/crawlers/` 来源；AlphaVault 侧只写最终趋势复盘 Markdown/HTML、索引和日志。生命周期台账同理：它是 skill 域运行时数据，归 PG 管，不进 AlphaVault 状态文件体系。
8. 清理临时产物：确认 `reports/report_YYYYMMDD.md`（及按需生成的 HTML）已写入并可读后，运行一条确定性清理命令，不要手工逐个删文件：

   ```bash
   python3 scripts/run_daily_panel.py --cleanup YYYYMMDD
   ```

   该命令删除同日期的 evidence、kline、stderr 日志、lifecycle 输入、report_context 与 module_context 目录，永远不会碰 `report_YYYYMMDD.md` / `report_YYYYMMDD.html`（生命周期数据已持久化在 PG，不受清理影响）。不要跨日期批量清理，除非用户明确要求。

## 数据获取

环境变量：

```bash
TUSHARE_TOKEN=your_token
ALPHA_DB_BACKEND=postgresql
ALPHA_PG_URL=postgresql://alpha_user:alpha_pass@/alpha_data?host=/tmp
```

数据库连接统一走 `scripts/_shared/db_core.py`（开发仓库中为 `shared/data/db_core.py`）。首次进入任意 Agent 环境时先运行 `python3 scripts/_shared/db_ping.py --alpha-schema`；源仓库开发态用 `python3 ../../shared/data/db_ping.py --alpha-schema`。如果不能使用 Unix socket，再把 `ALPHA_PG_URL` 改为 `postgresql://alpha_user:alpha_pass@localhost:5432/alpha_data`。

运行脚本会先更新 `references/market_data.csv`，并同步维护派生文件 `references/market_data.json`，再生成情绪趋势：盘面情绪计数默认由 Tushare `daily` 直接计算，其中涨跌停数按**板制规则精确判定**——未复权前收盘 ×(1±板块限幅) 四舍五入到分后与收盘价比对（主板 10%、主板 ST 5%、创业/科创 20%、北交所 30%），口径为收盘封板、不含盘中炸板，`limit_detection` 字段标注判定方式；搜狐涨跌停历史页仅在主路径不可用时作为 fallback；Tushare `margin` 汇总 T-1 交易日融资净买入，Tushare `daily` 与 `daily_basic.circ_mv` 计算流通市值加权的全市场换手率。市场风格代理指数使用 Baostock `query_history_k_data_plus`，默认覆盖超大盘、沪深300、中证500、中证1000、国证2000、中证红利、300成长、300价值；历史数据缓存在 PG 的 `stock_index_daily` 表（以 bs_code 为 ts_code），每次运行只向 Baostock 增量取缺口，Baostock 不可用时直接用缓存窗口出摘要。个股日线、复权因子（`stock_adj_factor`）、daily_basic、交易日历同样全部缓存在 PG。因此环境中还需安装 `baostock`（`akshare` 为历史遗留可选依赖）。

基础命令（在 skill 根目录下执行）：

```bash
python3 scripts/run_daily_panel.py --asof 20260429 --lookback 120 --market-trend-days 90 --index 000300.SH
```

主要输出：

- `reports/evidence_YYYYMMDD_utf8.json`：完整证据包（紧凑 JSON，metadata 含 `stage_timings_seconds` 各阶段耗时与 `fetch_gaps` 各端点缺失交易日清单；任一端点缺日非空时必须在报告中显式说明）。
- `reports/kline_YYYYMMDD.json`：个股 K 线展示层数据，只供 HTML 渲染使用，模型撰写不读取。
- `reports/module_context_YYYYMMDD/`：供 subagent 分工的模块级 JSON。
- `references/market_data.json`：`market_data.csv` 的全量派生 JSON，按交易日升序保留所有列、清洗数值并提供 `series` 给 HTML 趋势图使用。

这些文件中，evidence、kline、lifecycle 输入和 module_context 是研报撰写过程中的临时产物。最终报告生成并核对后，按工作流程第 8 步用 `--cleanup` 一键删除，只保留 `reports/report_YYYYMMDD.md`、按需生成的 `reports/report_YYYYMMDD.html`，以及长期维护的 `references/market_data.csv` / `references/market_data.json`。

HTML 输出命令：

```bash
python3 scripts/render_report_html.py --input reports/report_20260429.md [--theme default|claude|print]
```

默认输出 `reports/report_20260429.html`，并将 `references/market_data.json` 内嵌到 HTML 中。本地浏览器可直接打开，图表不依赖外部 CDN。`--theme` 默认 `default`（AlphaVault 站点 / Google Material）；`--theme claude` 为 Claude.ai 暖色风格；`--theme print` 为黑白衬线、A4 友好，适合导出 PDF 或邮件附件。样式模板由仓库通用 `shared/html_report`（同步到 `scripts/_shared/html_report/`）提供，与 `a-stock-analyzer` 共用。

常用参数：

| 参数 | 含义 | 默认 |
|---|---|---:|
| `--fetch-workers` | cache/API 获取线程数；排查限流时设为 1 | 6 |
| `--index-kline-days` | HTML 上证/创业板/科创50 K 线展示窗口，独立于 `--market-trend-days` | 120 |
| `--money-pct-threshold` | 赚钱效应最低当日涨幅 | 7.0 |
| `--money-amount-threshold` | 赚钱效应最低成交额，单位亿元 | 2.0 |
| `--decline-pct-max` | 爆量下跌最大当日涨幅 | -3.0 |
| `--decline-volume-ratio` | 爆量下跌最低 20 日放量倍数 | 2.0 |
| `--capacity-market-cap-threshold` | 容量上涨最低总市值，单位亿元，严格大于 | 70.0 |
| `--capacity-amount-threshold` | 容量上涨最低成交额，单位亿元，严格大于 | 5.0 |
| `--capacity-pct-threshold` | 容量上涨最低当日涨幅，严格大于 | 8.0 |
| `--feature-sample-limit` | 模块 5 每组最大样本数 | 60 |

## Subagent 编排契约

主 agent 先生成模块级 JSON，然后按下列最小上下文分发。每个 subagent 只看自己的模块数据，不读取其他模块数据。

| 模块 | JSON | 方法论 | 模板 |
|---|---|---|---|
| 1 盘面趋势 | `module1_market_trend.json` | `references/methodology/module1_trend.md` | `references/template/section1.md` |
| 2 集中度 | `module2_concentration.json` | `references/methodology/module2_concentration.md` | `references/template/section2.md` |
| 3 赚钱效应 | `module3_money_effect.json` | `references/methodology/module3_money_effect.md` | `references/template/section3.md` |
| 4 爆量下跌 | `module4_decline.json` | `references/methodology/module4_decline.md` | `references/template/section4.md` |
| 5 特征分组 | `module5_feature_groups.json` | `references/methodology/module5_feature_groups.md` | `references/template/section5.md` |

聚合 agent 额外读取：

- `assembled_checks.json`：M3 赚钱效应池与 M4 爆量下跌池的确定性交叉检查。
- `references/methodology/output_discipline.md`：最终成稿纪律。

Python 不调用 Anthropic API、不调用任何 LLM、不硬编码模型名。Codex、Claude Code 或其他通用 agent 的 subagent 编排能力负责并行撰写。

## 输出规范

完整研报按五个模块输出。每个判断段先给自然语言结论，再选择少量关键证据支撑；表格承载细项数据，段落解释这些数据意味着进攻、分歧、退潮、修复、拥挤还是扩散。所有强弱判断都要能回到成交额、放量倍数、涨跌幅、相对收益或回撤证据，但不要把所有可用指标塞进同一段。模块 3 的主题分组只作为内部推理步骤，不输出单独的主题分组陈列表，赚钱效应总览后直接进入主线判定。

**文风默认（项目级硬性要求）：**

- **文风讲人话，减少机械与僵硬。** 像跟懂行的人当面把一件事讲清楚那样写，句子通顺、有逻辑衔接，该解释因果和给判断时把话说透。避免模板腔、翻译腔和套话——别成段堆砌"综上所述""值得注意的是""总体来看"，别把每条都写成生硬的"主语+动词+宾语"公式句，也别为了凑结构把话说断、只丢关键词。
- **同项罗列优先用 list，但每条要说人话。** 同一维度的多个条目（多个信号、多只个股观察点、多项风险传导）拆成 bullet 或编号，一条一项，别塞进一个长段落；但每条用完整通顺的话写，不要退化成"字段A - 字段B - 字段C"式的横杠拼接。结构化对照（指标 × 数值、分组 × 判定）才用表格。

文风目标：僵硬度约 5/10。报告应像一位有经验的盘后研究员在做复盘：先给人能立刻理解的盘面状态，再用少量关键数字支撑，最后给出下一交易日需要验证的条件。避免把所有可用指标都塞进段落；同一自然段最多放 2-3 个核心数字，其余数字放表格。

每个一级大章节（1-5）里已有的总结/定性段落必须使用 Markdown 高亮样式 `==...==` 包裹，例如“指数趋势判断”“市场风格判断”“盘面定性”“拥挤度判断”“主线 vs 资金轮动结论”“风险传导提示”“特征分组一句话判断”。不要为了高亮额外新增“本节总结”段落；高亮的是原本就承担总结作用的段落。

禁止输出买卖建议。可以写“风险传导”“持续性待验证”“主线确认度”，不要写“买入/卖出/止损/目标价”。

HTML 输出只改变呈现方式：必须保留 Markdown 研报中的所有文字、表格、引用和免责声明。`==...==` 高亮段落在 HTML 中渲染为浅蓝提示块；正文前可以增加 `market_data.json` 驱动的趋势图区域。若可读取同日期 evidence，HTML 可以在“指数趋势”正文附近插入上证指数、创业板指数、科创50 的 120 日 K 线图，并在图中展示成交金额柱；也可以在 3.3、5.2、5.3 股票明细表下方插入表内股票的 120 日 K 线图；若 evidence 含风格序列，还可以在“市场风格”表格下方插入两张 60 日归一化对比图（规模轴五线 / 成长价值红利三线，起点=100）。这些图表只展示 evidence 中已有的 OHLC、成交金额与风格指数收盘序列数据，不得新增与 Markdown 不一致的分析结论。

## 示例

用户：`复盘 2026-04-29 的 A 股盘面，重点看赚钱效应和容量上涨。`

执行：

```bash
python3 scripts/run_daily_panel.py --asof 20260429
```

然后按 subagent 契约加载 `reports/module_context_20260429/` 下的模块 JSON。若没有 subagent，就顺序加载每个模块的 JSON + 方法论 + 模板段，最后聚合为完整研报。
