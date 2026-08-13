---
name: a-stock-quarterly-report
description: A 股正式季报/半年报/年报全市场扫描与优秀个股筛选。以 Tushare 结构化三大报表为数值权威（income/fina_indicator/cashflow/balancesheet 按报告期增量取数），巨潮 CNInfo 只做公告溯源与按需 PDF 分节抽取；拆解营收/归母/扣非/经营现金流的累计-单季-环比三口径，判扣非含金量、现金流验证、毛利率与费用率边际、合同负债与存货应收等资产负债表前瞻信号、对预告与快报的兑现度、真实 TTM 与年化 PE、股价断层、主线归属与行业趋势，生成随披露增量更新的报告期 HTML。适用于"看下这季财报""半年报出得怎么样""筛业绩优秀的票""谁的利润有现金支撑""这家增长是不是纸面利润""扣非之后还剩多少""预告兑现了没""财报季行业结构怎么变"等提问。脚本只出确定性证据与来源追溯，优秀分档、质量成色、可持续性、行业方向由模型判断；不给买卖建议、目标价或仓位。
---

# A 股正式财报扫描与优秀个股筛选

## 目标

- **做什么**：按某个报告期（季报/半年报/年报，如 2026Q1=`20260331`）扫描**全市场已披露**的正式财报，拆出营收 / 归母 / 扣非 / 经营现金流各自的「当年累计同比 / 最新单季同比 / 环比」三口径，再判利润质量（扣非占比、现金流覆盖、应收存货是否跑赢营收）、盈利能力边际（毛利率、费用率、ROE）、资产负债表前瞻信号（合同负债、在建工程、存货）、对自家预告/快报的兑现度、真实 TTM 与年化 PE、公布后的股价断层，最后由模型挑出真正业绩优秀的个股并排序。
- **不做什么**：不做单股基本面深挖（用 `a-stock-analyzer`）、不做业绩预告扫描（用 `a-stock-earnings-forecast`）、不做盘面复盘（用 `a-stock-daily-market-sense`）；不给买卖建议、目标价、仓位；不在脚本里判断"谁优秀"。
- **给谁用**：财报季要在全市场里锁定「增长真实、质量扎实、边际向上」候选池的投研读者，具备基本财务常识。

**和 `a-stock-earnings-forecast` 的分工**：预告 skill 看的是**触发式披露的边际变化**（只有大增大减扭亏的公司会出现，没有营收、没有现金流、数字是区间估计）；本 skill 看的是**强制全市场披露的实证**（每家都有，三张表齐全，数字是审计或至少是正式口径）。两者共用报告期与主线台账，但事实层互不覆盖：预告是前瞻，正式报告是兑现。

## 数据现实（务必先读，直接决定能交付什么）

1. **Tushare 结构化报表是数值权威，PDF 只在 API 没有字段时才开。** 预告必须解析 PDF 是因为 Tushare forecast 无扣非、有滞后；正式报告不同——`income`/`fina_indicator`/`cashflow`/`balancesheet` 覆盖了所有数字问题，扣非有 `profit_dedt` 权威值。PDF 留给 API 里根本没有的东西：分产品/分地区收入、管理层讨论、非经常性损益构成、客户集中度（见 `references/pdf_on_demand.md`）。
2. **发现源是披露日历，不是公告扫描。** `disclosure_date(end_date=period)` 一次调用返回全市场预约披露日与实际披露日（`actual_date`）；`actual_date` 已到的就是已披露池。CNInfo 只做溯源（标题 + 原文 PDF 链接）。
3. **四个报表接口没有 VIP 批量版，只能按股取数，每接口 200 次/分钟。** 首次全市场种子约 30 分钟（5500 家 × 4 次），之后按披露日增量，日更只取新披露的几十上百家（实测重跑 10 秒）。`tushare_client.py` 内置每接口令牌桶，不要绕过它并发。
4. **`income`/`balancesheet`/`cashflow` 的 `start_date`/`end_date` 过滤的是公告日，`fina_indicator` 过滤的是报告期。** 这是个安静的坑：传 `end_date=period` 会让本期报表（几周后才公告）在四个接口里消失三个，看起来像数据缺失而不是查询错误。脚本统一用「上年 1 月 1 日 ~ 披露截止日」的公告窗口。
5. **单季 = 累计 − 上一累计期，缺基数就是缺，不能拿累计冒充。** Q1 的累计本身就是单季；Q1 的上一单季要跨年到上年 Q4（年报累计 − 三季报累计）。资产负债表科目是时点值，Q1 的"环比"对的是上年末。
6. **全市场都要披露，但也不能全读。** 5500 家全在样本里，脚本会出**机械漏斗**：决策包 `stocks[]` 只放候选（默认 rank_score 前 200），全样本进 `qreport_universe_<period>.json` 供查询与渲染。
7. **上游有滞后，`fina_indicator` 与现金流常比利润表晚一两天。** 刚披露的股票在 `--refetch-days` 窗口内始终重取；追溯调整/更正报告靠同一机制补齐。
8. **公告日不等于交易日。** 按日历日处理，默认截止北京时间运行日+1；自动化显式传 `--end-ann` 并用 `--require-ann-cutoff` 做同日门禁。

## 领域方法论（纲要，判断细则见 `references/methodology.md`）

正式财报比预告多的不是"更准的同比"，而是**能验证利润是不是真的**。五条主线：

1. **三口径 × 四条线，重仓看单季边际。** 营收 / 归母 / 扣非 / 经营现金流各有累计同比、单季同比、环比。**单季同比 > 当年累计同比 = 加速**；**扣非增速 > 归母增速 = 核心经营比表观更强**；**营收单季与利润单季同向加速 = 量价共振**。只报一个累计同比等于没读财报。
2. **利润质量三件套（季报独有，预告给不了）。** ①**扣非占归母**：`profit_dedt / n_income_attr_p`，贴近 100% 说明利润几乎全是主业；差额大就要问非经常损益是什么（用 `report_pdf.py --sections nonrecurring` 看构成）。②**经营现金流覆盖**：`OCF / 归母`，长期低于 50% 或为负而利润高增，是纸面利润的经典信号。③**应收/存货 vs 营收**：`receivable_vs_revenue_gap_pp` 明显为正 = 收入确认了但钱没回来。三项一致向好才叫"扎实"，任一项背离就要在报告里点破。
3. **盈利能力的边际比水平更重要。** 单季毛利率的同比 pp 与环比 pp（脚本用累计差分自算，不用 `fina_indicator` 的累计率，因为累计率会把季度拐点抹平）、净利率、四项费用率的同比 pp、研发费用增速。毛利率抬升 + 费用率下降 = 经营杠杆在起作用；毛利率抬升但费用率同步上行 = 增长是买来的。
4. **资产负债表讲的是下几个季度。** 合同负债（缺失回落到预收款项）同比/环比 = 在手订单的领先指标；在建工程同比 = 扩产周期；存货要结合营收增速分辨"备货"还是"滞销"；商誉占净资产 = 减值风险；净现金 = 抗风险与再投资能力。这些是利润表读不出来的前瞻信息。
5. **兑现度是对预告的回测，也是对本 skill 分档的回测。** 落在预告区间的哪一侧、离中值多远（`in_range` / `range_position` / `vs_median_pct`）。贴下限压线 + 营收没跟上 = 即便同比亮眼也该降档；超上限 + 现金流同步 = 强档的硬证据。

**"业绩优秀"排序锚点**（模型据此判断，不是脚本）：
- **强**：单季营收与利润双高且单季≥累计（加速）；扣非贴近归母；OCF 覆盖净利；毛利率同比抬升；应收存货没有跑赢营收；有断层且未回补更好。
- **中**：累计高但单季减速；或利润质量三件套有一项背离；或增长靠低基数/一次性；或营收没跟上利润。
- **观察/剔除**：净利高增但 OCF 为负、应收暴涨；扣非远小于归母；营收下滑靠降本撑利润；亏损或单季转弱。

## 工作流程

1. **定报告期**：解析用户说的季度/半年/年报；未指定就用当前最新季度末（脚本 `--period` 缺省即"今天之前最近的季度末"）。2026 半年报 → `20260630`。
2. **跑证据脚本**：交互运行 `python3 scripts/report_scan.py --period 20260630`；自动化先算 `NEXT_ANN_DATE=<北京时间运行日+1>` 再显式传 `--end-ann`。脚本按披露日历发现已披露公司，增量取四张表，按交易日抓全市场前复权日线算断层，最后出三口径、质量、边际、资产负债表信号、估值、行业聚合与机械漏斗。默认交互模式可扫描预告/快报作兑现度参照；**禁止触碰业绩预告的正式报告单源自动化必须显式传 `--formal-only`**：该模式不调用 Tushare `forecast`，SQL 不读取旧 `kind=forecast` 行，也不写 forecast fetch log，只保留业绩快报 `express` 参照。产出决策包 `reports/qreport_scan_<period>.json` 与全样本 `reports/qreport_universe_<period>.json`。
3. **模型读证据做初筛**：读决策包（不要读全样本文件，它是给查询和渲染用的）。按方法论**逐股判断是否优秀并排序**——用 `growth`（四条线三口径）、`quality`（三件套）、`margins`（边际 pp）、`balance_signals`（前瞻）、`fulfillment`（兑现度）、`screen.hits`（机械命中，不是结论）。先圈出「强 / 中 / 观察」候选池。
4. **需要原文才开 PDF**：数字之外的问题——增长来自哪个产品线？管理层怎么解释毛利率变化？非经常损益是处置还是补助？——用 `python3 scripts/report_pdf.py --period 20260630 --code 300750.SZ --sections segment,mdna,nonrecurring`。**不要凭数字编故事**：没读原文就说不出"因为某产品放量"。何时该开、开哪一节、季报和年报的差别见 `references/pdf_on_demand.md`。
5. **判分 + 质量成色 + 兑现度 + 主线匹配 + 行业趋势落台账**：跑 `python3 scripts/verdict.py context --period 20260630` 拿到待判集合（新披露 + 追溯调整/增速漂移的待复判）、主线注册表（daily-market-sense 的 `theme_registry`，只读）与待判主线的强/弱/无断层成员简报。模型逐只判 **`tier`（强/中/观察/剔除）+ `quality_call`（扎实/尚可/存疑/虚高）+ `fulfillment`（超预告上限/落区间上沿/符合/落区间下沿/低于预告/无预告）+ `theme_id`**，逐主线判 `theme_trends[]`。写 `reports/qreport_verdict_<period>.json`，再 `python3 scripts/verdict.py record --period 20260630 --input reports/qreport_verdict_<period>.json` 落 `qreport_verdict` / `qreport_theme_trend`。台账增量累积，每天只判 context 列出的增量。细则见 `references/verdict_and_html.md`。
6. **生成报告期 HTML**：`python3 scripts/render_period_html.py --period 20260630`；发布链路必须加 `--require-ann-cutoff "$NEXT_ANN_DATE"`。一期一页、每次从累积缓存整页重渲染：过滤条 → 左列表 + 右详情（详情以利润质量三件套开头，然后兑现度、资产负债表前瞻、K 线与断层）。**HTML 默认剔除 `theme_id=null` 的无归属主线个股**；仅在调试或人工复核时加 `--include-unassigned` 恢复全部候选。默认按**披露时间**排序。K 线走同级 `qreport_<period>.klines/` 分片，**发布时必须连同该目录一起复制**。
7. **（可选）写 Markdown 报告**：需要叙事版时按 `references/output_template.md` 输出。
8. **诚实标注缺口**：`*_note` 里的 `base_missing/base_nonpositive/cur_missing`、`dedt_note=dedt_missing`、金融类公司无 `oper_cost` 导致毛利率为空、`pre_pos_bars` 不足一年、上游尚未收录本期报表的家数、`theme_registry` 为空（需先跑 daily-market-sense）都要如实说明。

脑/手边界：脚本只做取数、确定性拆解、比率与 pp 计算、阈值命中、计数、渲染投影；"谁优秀、质量算不算扎实、兑现得好不好、归属哪条主线、行业往哪走"全是模型写进台账的判断。**不在脚本里替模型下业绩好坏或行业方向结论，不给买卖建议。** 主线台账只读（归 daily-market-sense 维护），本 skill 不写主线。

## 数据获取（脚本抓手）

环境变量：

```bash
TUSHARE_TOKEN=your_token          # 见 ~/.zshrc 的 export；脚本也会回退读 cwd/.env
ALPHA_DB_BACKEND=postgresql
ALPHA_PG_URL=postgresql://alpha_user:alpha_pass@/alpha_data?host=/tmp
# TUSHARE_CALLS_PER_MIN=180       # 每接口限速，默认 180（Tushare 上限 200）
```

**PostgreSQL 是默认与推荐后端**，累积台账以它为准。DB 不可用时脚本降级为「全量抓取、不落库」（`meta.fetch_stats.cache=off`），仍能出报告，只是每次都重抓——这是容错，不是常态。

依赖：`pip install tushare pandas requests PyMuPDF PyPDF2 psycopg2-binary`（见 `requirements.txt`；PDF 两件只在 `report_pdf.py` 用到）。CNInfo 走公开披露接口，用 `requests` 直连（不经 WebFetch，避免本机代理拦截）。

命令（在 skill 根目录执行）：

```bash
# 第一步：全市场证据扫描（首次种子约 30 分钟，之后日更 ~10 秒）
python3 scripts/report_scan.py --period 20260630
python3 scripts/report_scan.py                                   # 默认最新季度末
python3 scripts/report_scan.py --period 20260630 --min-rank 8     # 用分数线代替 --top 截断
python3 scripts/report_scan.py --period 20260630 --codes 300750.SZ,603986.SH   # 定向核验
# 自动化/发布：NEXT_ANN_DATE 按北京时间取运行日+1
python3 scripts/report_scan.py --period 20260630 --end-ann "$NEXT_ANN_DATE"
# 正式报告单源自动化：禁止读取/扫描/写入任何业绩预告数据
python3 scripts/report_scan.py --period 20260630 --end-ann "$NEXT_ANN_DATE" --formal-only

# 第二步（按需）：抽正式报告 PDF 的指定章节
python3 scripts/report_pdf.py --list-sections
python3 scripts/report_pdf.py --period 20260630 --code 300750.SZ --sections segment,mdna,nonrecurring
python3 scripts/report_pdf.py --period 20251231 --code 002594.SZ --find 海外收入,单车盈利

# 第三步：判分落台账（模型判，脚本只出上下文/校验落库）
python3 scripts/verdict.py context --period 20260630 --out reports/qreport_ctx_20260630.json
python3 scripts/verdict.py record  --period 20260630 --input reports/qreport_verdict_20260630.json

# 第四步：生成报告期 HTML（发布链路启用披露截止日硬门禁）
python3 scripts/render_period_html.py --period 20260630 --require-ann-cutoff "$NEXT_ANN_DATE"
# 人工复核才使用：包含无归属主线个股
python3 scripts/render_period_html.py --period 20260630 --include-unassigned

# 离线自检
python3 scripts/test_report_scan.py
```

`report_scan.py` 常用参数：

| 参数 | 含义 | 默认 |
|---|---|---|
| `--period` | 报告期末（季度末 YYYYMMDD） | 今天之前最近的季度末 |
| `--end-ann` | 披露截止日（北京时间） | 今天+1 |
| `--top` / `--min-rank` | 决策包候选数 / 用 rank_score 分数线截断 | 200 / 关闭 |
| `--codes` / `--limit` | 定向核验 / 只处理前 N 家（调试） | 全部 |
| `--refetch-days` | 最近 N 个日历日内披露的股票始终重取（补上游滞后与更正） | 3 |
| `--refresh-fin` | 忽略缓存重取全部财报（大面积追溯调整时用） | 关闭 |
| `--fetch-workers` | 报表取数并发 | 8 |
| `--no-price` / `--price-lookback` | 跳过断层 / 股价窗口回看天数（决定 `pre_pos_pct` 的样本长度） | 关闭 / 380 |
| `--gap-min` | 跳空幅度 ≥ N% 记为断层 | 2.0 |
| `--no-cninfo` | 跳过公告溯源（不影响任何数值） | 关闭 |
| `--formal-only` | 正式报告单源：禁用 forecast endpoint 与 forecast kind 缓存读写；仅保留快报 express 参照 | 关闭 |
| `--th-*` | 九个漏斗阈值（营收/利润同比、扣非占比、现金覆盖、ROE、毛利 pp、订单、扩产、gap pp） | 见 `--help` |
| `--require-ann-cutoff` | 披露截止日门禁，不符返回 2 | 关闭 |

输出 JSON 结构（关键字段，完整字典见 `references/methodology.md`）：

- `meta`：报告期、`quarters_elapsed`、`released_count` / `with_statements` / `statements_complete_count` / `statements_incomplete` / `income_missing_count` / `disclosure_progress_pct`（披露进度，写报告必须交代）、`ann_cutoff` + `ann_cutoff_stock_count` + `clock_timezone=Asia/Shanghai`、`shortlist_rule`、`thresholds`、`fetch_stats`、`data_notes`。`fetch_stats.endpoint_status_counts` 统计各接口的 `ok/current_period_not_returned/request_failed/response_parse_failed`，`statement_fetch_diagnostics` 只列异常或本期未返回的股票与接口，用来区分上游入库滞后与调用失败。`primary_source=tushare_statements`、`pdf_role=on_demand_only` 固定记录源角色。
- `industry_summary[]`：按 stock_basic 行业的**全样本**确定性聚合，供数据查询使用；不进入模型判定或 HTML 页面。
- `stocks[]`：候选池，每股一条。
  - `source`：`authority=tushare_statements` + `sources`（哪几个接口已落地）+ `missing_sources` / `missing_source_diagnostics`（缺口及原因）+ CNInfo 标题/链接/是否更正后。
  - `growth`：`revenue` / `np` / `dedt` / `ocf` 四块，每块含累计与单季的值、同比、环比及各自的 `*_note`。
  - `quality`：`dedt_ratio_pct`、`non_recurring_yi`、`ocf_to_np_pct`、`cash_sales_to_revenue_pct`、`capex_cum_yi`、`roe_cum_pct` / `roe_annualized_pct`、周转率。
  - `margins`：毛利率/净利率的累计与单季值及同比、环比 pp；四项费用率与其同比 pp；研发费用与增速。
  - `balance_signals`：合同负债 / 应收 / 存货 / 在建工程 / 固定资产各自的金额、同比、环比；商誉占净资产；资产负债率；净现金；应收与存货相对营收的 `gap_pp`。
  - `valuation`：总市值 + `mv_asof`、年化与 TTM 的归母/扣非净利、四个 PE 及其 `*_note`、`pe_ttm_market`（daily_basic 市场口径，用于交叉核对）。
  - `fulfillment`：对预告的 `in_range` / `range_position` / `vs_median_pct` + 预告原文变动原因；对快报的差异。无预告则为 `null`。
  - `price_reaction`：`gap_open_pct` / `gap_dir` / `gap_status`（intact/filled/none/pending）/ `r_day_pct` / `r_vol_ratio` / `pre_pos_pct` + `pre_pos_bars` / `since_ann_pct` / `trading_days_since_r`。
  - `screen`：`hits[]`（机械阈值命中）+ 四类分数 + `penalty` + `rank_score`。**只是漏斗，不是优秀度结论。**

## 数据存储与增量

财报是逐日增量披露的，脚本把抓到的都落库、每天只补新增：

- **连接层**：统一走 `shared/data/db_core.py`（开发态）/ 同步后的 `scripts/_shared/db_core.py`，不自建连接方案。表以 `qreport_` 前缀建在共享库里（首次运行自动建表）。
- **落库的东西**：`qreport_disclosure`（披露日历，发现层）、`qreport_fin_cache`（按 (股, 报告期) 存一份报表行项目 JSON + 已取到哪几个接口）、`qreport_basic_cache`、`qreport_daily_cache`（前复权日线）、`qreport_cninfo_fetch_log` / `qreport_cninfo_announcement`（公告溯源）、`qreport_pdf_section`（按需 PDF 章节）、`qreport_forecast_ref` / `qreport_forecast_fetch_log`（兑现度参照）、`qreport_verdict` / `qreport_theme_trend`（模型台账）。
- **增量逻辑**：披露日历每次全量刷新（一次调用）；已取全四个接口且不在 `--refetch-days` 窗口内的股票直接走缓存；日线按交易日水位只补新增交易日；CNInfo 公告按公告日水位增量枚举。
- **性能**：报表、日线、预告扫描都走有界线程池 + 每接口令牌桶；PostgreSQL 批量 upsert。首次全市场种子约 30 分钟，之后日更 ~10 秒。

## 输出规范

- **文风讲人话，减少机械与僵硬。** 像跟懂行的人当面把这季财报讲清楚：先给整体判断，再用少量关键数字支撑，把"为什么优秀 / 利润是不是真的 / 有什么坑"说透。不堆"综上所述/值得注意的是"，不写"字段A - 字段B - 字段C"式横杠拼接。
- **同项罗列优先用 list，每条说完整话。** 多只个股、多条原因、多项 caveat 拆成 bullet，一条一项、每条完整通顺；结构化对照（个股 × 三口径数值）才用表格。
- **每只优秀个股必须同时交代增长与质量**：三口径（营收/归母/扣非的单季同比与环比）+ 利润质量三件套（扣非占比、现金流覆盖、应收存货 vs 营收）+ 一句可持续性判断。只报同比不报质量，是这个 skill 最不该犯的错。
- **利润质量背离必须点破**：净利高增而 OCF 为负、扣非远低于归母、应收或存货明显跑赢营收——任一出现都要写出来并降档，不能只说"业绩亮眼"。
- **引用 PE 必标口径**：头条用**扣非 TTM PE**（滚动四季、已消化季节性），年化 PE 只作对照且必须注明"未调季节性"；扣非 ≤ 0 时退回归母并点破"利润主要为非经常性损益"；亏损股不给 PE。
- **兑现度要写清落在哪一侧**：有预告的股票必须交代落区间上沿/下沿/超上限/低于预告以及离中值多远；贴下限压线要点破。
- **数字之外的话要有出处**：说"某产品线放量""管理层指引"就必须来自 `report_pdf.py` 取到的原文并标章节页码；没取原文就只讲数字能支持的结论。
- **诚实 caveat 必写**：低基数弹性、一次性损益、上游尚未收录本期、金融类无毛利率、`pre_pos_bars` 不足一年、断层新鲜度（`trading_days_since_r` 只有几天时"未回补"没经过检验）。
- **红线**：不写买入/卖出/止损/目标价/仓位；可以写"业绩优秀度分档""质量成色""持续性待验证条件""需结合估值另判"。

## 示例

用户：`2026 一季报出完了，帮我筛业绩优秀的票，要看利润有没有现金支撑。`

执行：

```bash
python3 scripts/report_scan.py --period 20260331
```

然后读 `reports/qreport_scan_20260331.json`，按 `references/methodology.md` 的锚点逐股判断。例如兆易创新：营收单季 +119%、归母单季 +523%、扣非 +530%，单季毛利率同比 +19.6pp，扣非占归母 96.5%、经营现金流覆盖净利 122%、应收与存货都远慢于营收——量、价、现金三条线同时兑现，列入"强"、质量成色"扎实"；caveat 是扣非 TTM PE 已 120 倍、断层未回补但已过 57 个交易日，需结合估值另判。若要说清增长来自 DRAM 还是 MCU，再跑 `report_pdf.py --code 603986.SH --sections segment` 读分产品收入，不要凭数字推测。
