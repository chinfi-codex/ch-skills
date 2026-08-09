---
name: a-stock-daily-market-sense-execution-flow
description: 仅供 a-stock-daily-market-sense skill 内部按需读取。说明日报生成与因子挖掘的详细执行流、subagent 最小上下文、脚本参数与清理策略。
---

# A 股趋势复盘执行流

## 日报主流程

1. **确定交易日**：解析"今天/最近"或具体日期，默认只使用 D 及以前数据；只有用户明确要求后验时才允许 `--allow-future`。

2. **生成证据包**：运行 `scripts/run_daily_panel.py`。脚本会直接调用数据管线，写出完整 evidence、个股 K 线展示数据（`kline_YYYYMMDD.json`）和模块级 JSON；同时调用三张机判卡并把结果注入完整 evidence 与 `module1_market_trend.json`：
   - `scripts/trend_state_card.py`（趋势轴：五计数器 + 六档状态机，与 AlphaVault 盘前工具同参；2026-08 起阈值全部按占比 / bp / 滚动分位判）→ `trend_state_card`；
   - `scripts/market_state_card.py`（宽基位置 / 市场宽度 / 申万一级结构 / 融资余额趋势 / 流动性）→ `market_state`；
   - `scripts/extreme_state_card.py`（极值轴：底部出清分 0~6 / 顶部拥挤分 0~5，分位基准存 `dms_extreme_daily`）→ `extreme_state`。
   PG 不可达时这些区块降级为 `available: false`，不阻断研报；可分别用 `--no-market-state` / `--no-extreme-state` 跳过。**新环境第一次跑要先 `--extreme-backfill 300` 补分位基准**，否则阈值退回固定水平。

3. **生成首轮模块产物**：模块 1、2、4、5 各自只读自己的 JSON、方法论与模板。模块 3 首轮只根据 `module3_money_effect.json` 归纳临时主题、父主题成员与候选细分成员，先写 `stars: null` 的 `module3_theme_map.json`；不要搜索，也不要在统计前凭手算锁星。有 subagent 时分发最小上下文，没有时按相同边界顺序执行。

4. **统计并锁定模块 3 星级**：运行 `theme_group_stats.py` 生成 `module3_theme_stats.json`，再由模型严格按 Market Evidence Pack 与统计结果写回 `stars: 1/2/3`。星级锁定后，只对当日 ★★★ 主线强制尝试搜索，并按宿主能力选读知识库或产业链资料；★ 与 ★★ 方向都不搜索、不做产业推演、不进入 3.2。主 agent 将 Web 结果、可选的宿主知识证据与查询错误压缩成 `module3_enrichment_pack.json`。外部资料只用于解释催化、推演产业变量与挖掘细分线路，绝不回写或上调 3.1 星级。详细搜索、证据和评级纪律见 `references/methodology/catalyst_subline_mining.md`。

5. **聚合成稿**：模块 3 第二阶段只读取 theme map、统计结果、enrichment pack、方法论与模板，完成 3.1 主线判定和 3.3 领导股与弹性股；仅当存在 ★★★ 主线时才在两者之间输出 3.2 催化与细分线路推演，没有 ★★★ 时整节省略。主 agent 再读取模块 1-5 输出、`assembled_checks.json` 与 `references/methodology/output_discipline.md`，补一句话盘面判断、风险传导提示和最终语气校准。搜索或知识查询失败不阻断日报，但要披露证据缺口并降低产业推演确定性。

6. **主线生命周期落库**：报告定稿后，把当日 3.1 主线判定沉淀进 PG 生命周期台账。先运行 `python3 scripts/theme_lifecycle.py context --asof YYYYMMDD` 取注册表、各主线近期状态与 watchlist；模型完成别名归一（当日临时主题名 → canonical theme_id）和生命周期状态判定（低位启动/在场候选/主线确认/高位分歧/退潮/修复/再聚焦/沉寂），写出 `reports/lifecycle_YYYYMMDD.json` 后运行 `python3 scripts/theme_lifecycle.py record --input reports/lifecycle_YYYYMMDD.json` 落库。脚本只做确定性校验（枚举、状态机转移合法性、theme_id 存在性），判断留给模型；输入格式、状态机与判定基准见 `references/theme_lifecycle.md`。

7. **按需生成 HTML**：当用户要求 HTML、网页、可视化报告或截图风格输出时，先完成并核对 `reports/report_YYYYMMDD.md`，再运行 `scripts/render_report_html.py` 生成同日期 HTML。HTML 是展示层产物，不新增研报判断、不删减 Markdown 正文。

8. **清理临时产物**：最终报告生成并核对后，运行 `python3 scripts/run_daily_panel.py --cleanup --asof YYYYMMDD` 删除 `reports/module_context_YYYYMMDD/`、`evidence_YYYYMMDD_utf8.json`、`kline_YYYYMMDD.json`、`assembled_checks.json` 及 `lifecycle_YYYYMMDD.json` 等临时文件，只保留 `reports/report_YYYYMMDD.md`、按需生成的 `reports/report_YYYYMMDD.html`，以及长期维护的 `references/market_data.csv` / `references/market_data.json`。

## Subagent 编排契约

主 agent 先生成模块级 JSON，然后按下列最小上下文分发。每个 subagent 只看自己的模块数据，不读取其他模块数据。

| 模块 | JSON | 方法论 | 模板 |
|---|---|---|---|
| 1 盘面趋势 | `module1_market_trend.json`（含机判 `trend_state_card`、`market_state`、`extreme_state` 三个区块） | `references/methodology/module1_trend.md`、`references/methodology/market_state_framework.md` | `references/template/section1.md` |
| 2 集中度 | `module2_concentration.json` | `references/methodology/module2_concentration.md` | `references/template/section2.md` |
| 3 赚钱效应（首轮） | `module3_money_effect.json` | `references/methodology/module3_money_effect.md` | 先输出临时主题短名单与 `stars: null` 的 `module3_theme_map.json` |
| 4 爆量下跌 | `module4_decline.json` | `references/methodology/module4_decline.md` | `references/template/section4.md` |
| 5 特征分组 | `module5_feature_groups.json` | `references/methodology/module5_feature_groups.md` | `references/template/section5.md` |

模块 1、2、4、5 互不读取。模块 3 使用两阶段契约：首轮只做临时主题与成员映射；统计脚本完成后，由模型按量价 rubric 写回并锁定星级；第二阶段只读取已锁星的 `module3_theme_map.json`、`module3_theme_stats.json`、`module3_enrichment_pack.json`、模块 3 方法论和 `references/template/section3.md`，不回读其他模块的完整 JSON。Skill 只规定最小上下文边界，不规定 subagent 数量、并发槽位或模型。

聚合 agent 额外读取：

- `assembled_checks.json`：M3 赚钱效应池与 M4 爆量下跌池的确定性交叉检查。
- `references/methodology/output_discipline.md`：最终成稿纪律。

Python 不调用 Anthropic API、不调用任何 LLM、不硬编码模型名。Codex、Claude Code 或其他通用 agent 的 subagent 编排能力负责撰写；知识 evidence pack 是宿主可选输入，不在 core skill 中硬编码知识库路径或图谱实现。

## 特征因子挖掘（量化回溯 · 按需研究流程）

这是独立于每日日报的一条研究线：**你提一个特征分组，skill 在分组之上做额外因子挖掘，给出分组内的"叠加条件最优解"**。不进日报，想挖时才跑。完整方法论、基准表、spec 格式、最优解选择 rubric 见 `references/methodology/factor_mining.md`。

脑/手边界照旧：脚本 `scripts/factor_backtest.py` 只铺确定性证据（历史回放命中、6 格前向相对收益、因子分层、单/配对候选叠加条件 + 过拟合护栏标记），**不挑"最优解"、不下结论**；读证据、选叠加条件、写归因与 caveat 是模型的活。

流程（两处人工，其余自动）：

1. **你提分组**：内置组直接点名（如折扣启动）；新组用大白话给硬条件，模型译成 filter spec 给你确认。
2. **脚本回放 + 回测 + 铺网格**：
   ```bash
   # 内置折扣启动（多日序列组，复用生产函数，语义同线上）
   python3 scripts/factor_backtest.py --group discount_relaunch --min-n 15
   # 自定义单日特征阈值组（含容量上涨式），用 spec
   python3 scripts/factor_backtest.py --group custom --spec my_group.json
   ```
   产物：`reports/factor_mining_<group>_<asof>.json`（证据包，gitignore，跑完即临时）。
3. **模型选最优叠加解**：读 JSON，按 reference §四 rubric（稳健优先于大 Δ、看 `oos_balance` 与中位数胜率、深度≤2、经济逻辑）选定叠加条件，写 `reports/factor_mining_<group>_<asof>.md`。
4. **你决定要不要用**：把结果作为研究参考，或手动把叠加条件提级成分组生产阈值；脚本不替你改生产。

口径要点：进场 T+1 开盘/尾盘 × 持有 T+3/T+5/T+10，后复权；相对收益挂**板块/市值匹配基准**（科创→科创50、创业→创业板指、主板按市值→沪深300/中证500/中证1000），沪深300 作宽基对照。脚本默认会把信号窗口缺失的 `daily_basic` 从 Tushare 回补入库（需 `TUSHARE_TOKEN`）。折扣启动要完整 200 日历史，信号只落在数据最近端、样本偏小——所以护栏与诚实 caveat 是骨架，结论按"单一环境证据扫描、非统计定论"来写。挖矿证据分**决策包**（≤150KB，模型读：结论 + 每条过闸条件 ≤3 例证）与 `_detail.json`（整列 signals，按需读）；每次挖矿自动登记进 PG `factor_experiment_log`（可查、可判分）。

需要回看已跑过的实验或补人工判定时，使用 `factor_lab.py experiments` 查询 PG `factor_experiment_log`。实验台账只记录挖矿参数、样本数、过闸数量、证据路径与人工 verdict，不进入每日日报。
