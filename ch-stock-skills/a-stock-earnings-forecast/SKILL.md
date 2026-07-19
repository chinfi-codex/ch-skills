---
name: a-stock-earnings-forecast
description: A 股业绩预告全市场扫描与优秀个股筛选。以巨潮资讯 CNInfo 官方公告为主发现源和事实权威，独立分页扫描、下载并解析公告 PDF，Tushare forecast 只做差异校验及 IPO 文件内嵌预测等显式例外兜底；再拆解累计/单季同比/环比、扣非净利、年化PE、股价断层、主线归属、行业趋势与产业综述，生成随披露增量更新的报告期 HTML。脚本只出确定性证据和来源追溯，优秀分档、可持续性、行业方向由模型判断；不给买卖建议、目标价或仓位。
version: 1.3.0
---

# A 股业绩预告扫描与优秀个股筛选

## 目标

- **做什么**：按某个报告期（默认当前最新季度，如 2026H1=20260630）全市场扫描业绩预告，把净利润预告区间按中值折算成一个数，拆出「当年累计同比 / 最新单季度同比 / 环比」三个口径的利润增速，按最新总市值折算预告中值年化 PE（估值参照），读出业绩变动原因，对照最近实际报告期的营收趋势，最后由模型挑出真正业绩优秀的个股并排序。
- **不做什么**：不做单股基本面深挖（用 `a-stock-analyzer`）、不做盘面复盘（用 `a-stock-daily-market-sense`）；不给买卖建议、目标价、仓位；不在脚本里判断“谁优秀”。
- **给谁用**：面向要在财报季快速锁定「业绩超预期 / 加速 / 高景气」候选池的投研读者，具备基本财务常识。

## 数据现实（务必先读，直接决定能交付什么）

1. **CNInfo 是预告主源，不再由 Tushare 名单驱动。** `forecast_scan.py` 按公告日独立分页扫描巨潮“业绩预告”分类，处理中文年份、上半年/半年报/半度等标题变体并排除业绩快报；公告元数据和 PDF 解析结果落 PostgreSQL。CNInfo 发现但 Tushare 尚未收录的公司也会进入报告。
2. **CNInfo 不是现成结构化全量接口。** 脚本必须解析 PDF 才能得到归母、扣非、同比和变动原因；解析稳定时以 CNInfo 为准，失败或与同公告日 Tushare 结构化值出现正负号/重大数值冲突时才取对照值，并显式记录回退。Tushare 独有记录以 `source.authority=tushare_exception` 保留，主要覆盖招股书/上市文件内嵌预测，不伪装成独立 CNInfo 预告公告。
3. **预告通常没有营收。** CNInfo 原文多数不披露营业收入；收入侧仍使用最近实际报告期（如 Q1）的 trailing actual，并明确标注“非预告”。
4. **预告净利多为归母口径**，单季拆解统一用归母净利（`income.n_income_attr_p`）。
5. **只覆盖已披露预告的公司。** 业绩平稳、未触发强制预告的公司可能根本没有预告——“没有预告”≠“业绩差”。
6. **季初样本会偏少、随时间累积。** 过几天重跑或放宽 `--start-ann` 可补全。
7. **公告日期不等于交易日。** 按日历日扫描，默认截止北京时间运行日+1；自动化显式传 `--end-ann` 并用 `--require-ann-cutoff` 做同日门禁。

## 领域方法论（纲要，判断细则见 `references/methodology.md`）

读业绩预告的核心不是“同比多高”，而是**边际方向 + 增速含金量 + 可持续性**。四条主线：

1. **三口径一起看，重仓看单季边际。** 预告给的是累计口径（H1 = Q1+Q2）。但市场定价的是最新单季的同比和环比。**单季同比 > 当年累计同比 = 环比向上 = 加速**，往往比“高但减速”的累计增长更值钱。脚本已把三口径拆好，别只盯累计同比。
2. **基数质量决定增速含金量。** 几百上千%的同比先分辨是「低基数/困境反转」（扭亏、上年同期亏损、周期底部如养殖/化工/资源）还是「高基数持续高增」。前者是弹性、不等于经营质量；真正稀缺的是高基数上还能高增。结合 `type`（扭亏 vs 预增）、`last_parent` 绝对值、绝对利润规模一起看。
3. **读 `change_reason` 判可持续性，有扣非就直接看扣非。** 主业量增、价升、毛利率提升、新产能/新客户放量 = 可持续、含金量高；资产处置、政府补助、投资收益、公允价值变动、并购并表、税收返还、诉讼赔偿等一次性/非经常损益 = 要打折甚至剔除。文本里出现这些词就警惕。若 CNInfo 公告解析取到**扣非净利**，直接看扣非与归母的差额：差额大说明利润里一次性成分多，扣非增速比归母增速更能代表真实经营，是判含金量最硬的量化证据。
4. **营收-利润匹配度（用 trailing 实际营收）。** 利润高增但最近实际期营收不增，可能靠降本/减值转回/毛利修复，持续性存疑；营收利润同步高增更健康。

**“业绩优秀”排序锚点**（模型据此判断，不是脚本）：
- **强**：单季与累计双高且单季≥累计（加速）、绝对利润不小、原因是主业量价驱动、营收同步向好、不依赖低基数。
- **中**：累计高但单季减速；或高增但基数偏低/含一次性；或营收未同步。
- **观察/剔除**：扭亏但主要靠非经常损益、预减/首亏/续亏、单季环比转弱、原因含大额一次性收益。

## 工作流程

1. **定报告期**：解析用户说的季度/半年/年报；未指定就用当前最新季度末（脚本 `--period` 缺省即“今天之前最近的季度末”）。确认 2026H1 → `20260630`。
2. **跑证据脚本**：交互运行 `python3 scripts/forecast_scan.py --period 20260630`；自动化先算 `NEXT_ANN_DATE=<北京时间运行日+1>`，再显式传 `--end-ann`。脚本先按日历日扫描 CNInfo 官方预告分类并解析新 PDF，再调用 Tushare `forecast` 做集合和字段差异校验；CNInfo 解析字段优先，解析缺口或同公告日结构化冲突才由 Tushare 补值/回退。随后取 Tushare `income`、前复权日线和 `daily_basic` 计算三口径、trailing 营收、断层和年化 PE。一次扫描同时生成 `forecast_scan_<period>.json` 与全样本 `cninfo_enrich_<period>.json`，不再需要候选名单驱动的增强步骤。
3. **模型读证据做初筛**：读 JSON，按上面的方法论**逐股判断是否优秀并排序**——重点用 `profit_growth`（三口径 + `base_consistency`）、`flags`（`accelerating/turnaround/positive_type` 等）、`change_reason`（一次性损益识别）、`revenue_trailing`（营收匹配）。先圈出「强 / 中 / 观察」候选池。
4. **复核 CNInfo 解析**：全样本公告已随 scan 下载并进入 `cninfo_enrich_<period>.json`。`cninfo_enrich.py --codes ... --refresh` 只用于定向重解析或人工核验，不再承担发现新增公司的职责。`parsed` 仍是最佳努力解析，重要数值应对照 `raw`/`text`。
5. **判分 + 主线匹配 + 行业趋势 + 产业综述落台账**：跑 `python3 scripts/verdict.py context --period 20260630` 拿到「待判个股（新披露 + 预告已修订/增速漂移的待复判）+ 主线注册表（daily-market-sense 的 `theme_registry`：名称/别名/当前★状态/成员样本）+ 待判主线（`to_judge_themes`：每条主线的强表现/弱表现/无断层成员简报，含变动原因）+ 产业综述状态（`period_overview`：样本指纹与上一版全文）+ `industry_summary` 透传」。模型据此做三层判断：**逐只判 `tier`（强/中/观察/剔除）+ `theme_id`（归属哪条在场主线，对不上填 `null`=无归属）**；**逐主线判报告期行业趋势 `theme_trends[]`**——分别读强表现（向上断层）与弱表现（向下断层）成员的变动原因做归因，判每侧是行业级共性还是个体因素，交叉验证出 `direction`（向上/向下/分化/证据不足）+ 强/弱侧归因 + 一句话交叉验证结论（方法论见 `references/methodology.md` §九）；**全样本写产业结构综述 `period_overview`**——据 `industry_summary`（含负向预告）把行业聚宏观组、判哪类产业在获得业绩增速、哪类延续疲软及筑底证据，(a)(b) 式成文（§十）。写 `reports/verdict_<period>.json`，再 `python3 scripts/verdict.py record --period 20260630 --input reports/verdict_<period>.json` 落 `forecast_verdict` / `forecast_theme_trend` / `forecast_period_overview` 台账。主线匹配是**语义判断**：用 `change_reason`/行业比对主线 name/aliases/成员样本，弱匹配标 `match_confidence=low`（渲染成"疑似"），完全对不上就 `null`（"无归属"本身是"业绩强但暂无主线关注"的信号）。台账增量累积，每天只判 context 列出的增量；主线成员构成变化后趋势自动标待复判、样本指纹变化后综述自动待复判。判分细则、trend/overview 格式与匹配方法见 `references/verdict_and_html.md`。
6. **生成报告期 HTML**：交互使用 `python3 scripts/render_period_html.py --period 20260630`；自动化/发布链路必须使用 `python3 scripts/render_period_html.py --period 20260630 --require-ann-cutoff "$NEXT_ANN_DATE"`。严格门禁会校验 evidence 的北京时间截止日、截止日股票数、总股票数与时区元数据，不一致时停止渲染，避免 scan 尚未完成或旧 JSON 被误发布。页面头部固定展示“公告扫描截至 YYYY-MM-DD（北京时间次日口径 · 截止日 N 家）”，因此 HTML 自身可审计是否包含当天+1数据。页面基础视觉统一加载仓库 `shared/html_report/themes/default.css`（同步安装后位于 `scripts/_shared/html_report/themes/default.css`），skill 内只保留 Earnings Forecast 的主从布局与交互增量样式；字号层级需保证标题、筛选器、列表、详情和脚注可读，宽屏容器充分展开。**一期一页、每次从累积缓存整页重渲染**：读 evidence + enrich + `forecast_verdict` + `forecast_theme_trend` + `forecast_period_overview` + `forecast_daily_cache`，实时 join `theme_daily_state`。页面为「标题 → **产业结构综述**（模型全文 + 判于日期 + 当时样本数）→ 过滤条 → **左列表+右详情**双栏」；默认按发布时间排序，保留既有断层、K线、年化PE、主线趋势与分档语义。为避免财报季样本增长后首屏被大体积 K 线阻塞，渲染器只把股票/判分/趋势数据内嵌到 HTML，并同时生成 `forecast_<period>.klines/` 下的 64 个确定性 JSON 分片；点击个股时按需加载一个分片并缓存。发布或同步 HTML 时必须连同同名 `.klines/` 目录一起复制，不能只复制 HTML。完整 HTML 契约见 `references/verdict_and_html.md`。
   **性能契约**：左侧列表按 120 家一批渐进渲染，避免财报季全量样本一次性创建超大 DOM；K 线仍按当前详情所在分片加载。
7. **（可选）写 Markdown 报告**：需要叙事版时按 `references/output_template.md` 输出整体画像 + 报告期行业趋势（开头放产业结构综述 (a)(b) 式分条，再按主线细看强/弱侧归因 + 交叉验证结论，与台账同一口径）+ 优秀个股 shortlist（每只三口径、扣非含金量、归属主线、caveat）。HTML 与 Markdown 同源于 evidence + verdict，二选一或都出。
8. **诚实标注缺口**：`meta.income_missing`、`single_q_note=base_missing/base_nonpositive/cur_missing`、`base_consistency=diverge`、cninfo `missing`、以及 `theme_registry` 为空（主线台账未填充需先跑 daily-market-sense）都要如实说明。

脑/手边界：脚本只做取数、按中值折算、确定性拆解、护栏标记、断层方向分组、校验落库、渲染投影；“谁优秀、怎么排、归属哪条主线、原因可不可持续、要不要剔除、行业趋势判什么方向”全是模型写进 `forecast_verdict` / `forecast_theme_trend` 台账的判断。**不在脚本里替模型下业绩好坏、主线归属或行业趋势结论，不给买卖建议。** 主线台账只读（归 daily-market-sense 维护），本 skill 不写主线。

## 数据获取（脚本抓手）

环境变量：

```bash
TUSHARE_TOKEN=your_token          # 见 ~/.zshrc 的 export；脚本也会回退读 cwd/.env（仅本地读取，不随包发布）
# 存储/增量缓存统一走仓库共享连接层 shared/data/db_core.py，正式路径是 PostgreSQL：
ALPHA_DB_BACKEND=postgresql
ALPHA_PG_URL=postgresql://alpha_user:alpha_pass@/alpha_data?host=/tmp
# ALPHA_DB_BACKEND=sqlite ALPHA_SQLITE_DIR=~/AlphaData/db   # 仅本地调试/无 PG 时临时用，不当持久台账
```

**PostgreSQL 是默认与推荐后端**，累积台账以它为准；sqlite 只作离线调试或无 PG 时的临时兜底，别把 sqlite/降级结果当持久 truth。若连 DB 都不可用，脚本会兜底降级为「全量抓取、不落库」（`meta.fetch_stats.cache` 显示 `off`），仍能出报告、只是不省抓取——这是容错，不是常态运行方式。

依赖：`pip install tushare pandas requests PyMuPDF PyPDF2`（见 `requirements.txt`；后三者用于 CNInfo 主源公告抓取与 PDF 解析）。cninfo 走公开披露接口，用 `requests` 直连（不经 WebFetch，避免本机代理拦截）。

命令（在 skill 根目录执行）：

```bash
# 第一步：CNInfo主源全市场扫描 + PDF解析 + Tushare差异校验 + 三口径拆解
python3 scripts/forecast_scan.py --period 20260630            # 指定报告期
python3 scripts/forecast_scan.py                              # 默认最新季度末
python3 scripts/forecast_scan.py --period 20260630 --positive-only   # 只看向好方向
python3 scripts/forecast_scan.py --period 20260630 --start-ann 20260501   # 放宽扫描窗口补早鸟
# 自动化/发布：NEXT_ANN_DATE 必须按北京时间取运行日+1，例如 20260714
python3 scripts/forecast_scan.py --period 20260630 --end-ann "$NEXT_ANN_DATE"

# 定向重解析（仅核验/修复；不再是名单增强步骤）
python3 scripts/cninfo_enrich.py --period 20260630 --codes 600872.SH,002648.SZ,300014.SZ
python3 scripts/cninfo_enrich.py --period 20260630 --from reports/forecast_scan_20260630.json --positive --top 15

# 第三步：判分 + 主线匹配落台账（模型判 tier+theme_id，脚本只出上下文/校验落库）
python3 scripts/verdict.py context --period 20260630          # 出待判个股 + 主线注册表 → 模型判 → 写 verdict_<period>.json
python3 scripts/verdict.py record  --period 20260630 --input reports/verdict_20260630.json

# 第四步：生成报告期 HTML（发布链路启用公告截止日硬门禁）
python3 scripts/render_period_html.py --period 20260630 --require-ann-cutoff "$NEXT_ANN_DATE"
```

常用参数：

| 参数 | 含义 | 默认 |
|---|---|---|
| `--period` | 报告期末（季度末 YYYYMMDD） | 今天之前最近的季度末 |
| `--start-ann` / `--end-ann` | 公告日扫描窗口（按日历日逐日调 `forecast(ann_date=)`，含周末） | period−45d ~ min(北京时间今天+1天, period+75d) |
| `--positive-only` | 丢弃恶化方向（预减/首亏/续亏/增亏/略减） | 关闭 |
| `--min-pchange` | `prefilter_pass` 对当年累计同比的阈值(%) | 0 |
| `--refetch-days` | CNInfo 与 Tushare 最近 N 个日历日始终重扫（抓新披露/修订及上游滞后） | 3 |
| `--rebuild` | 忽略缓存水位、整窗重扫并刷新季报 | 关闭 |
| `--refresh-income` | 强制重取实际季报（季报追溯调整时用） | 关闭 |
| `--no-cache` | 本次不落库、全量抓取 | 关闭 |
| `--fetch-workers` | forecast/income/日线 并发线程数（限流时设 1 串行） | 4 |
| `--gap-min` | 跳空开盘幅度 ≥ N% 记为断层 | 2.0 |
| `--no-price` | 跳过股价反应(净利润断层)计算 | 关闭 |
| `--out` / `--stdout` | 输出路径 / 同时打印完整 JSON | `reports/forecast_scan_<period>.json` |

HTML 渲染参数：`--require-ann-cutoff YYYYMMDD` 是发布门禁；它要求 evidence 同时具备相同的 `meta.ann_cutoff`、`ann_cutoff_stock_count` 与 `clock_timezone=Asia/Shanghai`，否则返回非零并拒绝生成 HTML。

HTML 产物不是孤立单文件：`reports/forecast_<period>.html` 是轻量首屏和完整股票/判分/趋势视图；`reports/forecast_<period>.klines/` 是 K 线按需分片（含 `_manifest.json`）。复制到 Site 时两者必须保持同级相对路径；若仅复制 HTML，列表仍可用，但 K 线会明确提示分片加载失败。

输出 JSON 结构（关键字段，完整字典见 `references/methodology.md`）：

- `meta`：报告期、单季标记、扫描窗口、家数、`income_missing`、`data_notes`；`primary_source=cninfo`、`tushare_role=reconciliation_and_exception_fallback` 固定记录源角色。日期交接字段为 `ann_cutoff`、`ann_cutoff_stock_count`、`clock_timezone=Asia/Shanghai` 与带 `+08:00` 的 `generated_at`。
- `industry_summary[]`：按 stock_basic 行业的全样本聚合（`n/positive_n/negative_n/turnaround_n/accelerating_n/gap_up_n/gap_down_n`、三口径增速中位数、按利润规模取前 5 的成员样本）——产业结构综述的原料，行业口径只是原料分组、结论由模型聚宏观组后下。
- `stocks[]`：每股一条，按当年累计同比降序。
  - `source`：`authority=cninfo` 表示由官方公告发现；`structured_source=cninfo_pdf` 表示结构化数字来自公告解析，`tushare_parse_fallback` 表示 PDF 解析缺口由差异源补值，`tushare_reconciliation_fallback` 表示同公告日解析出现正负号或重大数值冲突后采用差异校验值；`authority=tushare_exception` 是没有独立 CNInfo 预告公告的显式例外。
  - `net_profit`：`min_yi/max_yi/median_yi`（预告净利中值，亿元）、`last_parent_yi`（上年同期）。
  - `valuation`：预告中值年化 PE 估值参照（**归母口径打底**）——`total_mv_yi`+`mv_asof`（最新交易日总市值）、`annualized_np_yi`+`annualize_label`（年化净利=中值÷报告期季数×4）、`pe_annualized`（+`pe_annualized_note`：`ok/np_missing/np_nonpositive/mv_missing`，年化净利≤0 不给 PE）、`rolling_np_yi`/`pe_rolling`（滚动口径=上年年报实际+本期中值−上年同期实际，季节性对照）。**扣非优先在渲染层做**：`render_period_html` join cninfo 扣非中值后按同一年化系数重算扣非年化PE 当头条口径（scan 阶段没扣非，故 valuation 块本身只到归母）。读法与坑见 `references/methodology.md` §十一。
  - `profit_growth`：`cum_yoy_pct`（当年累计同比，`cum_yoy_source=p_change/derived`）、`single_q_yoy_pct`（单季度同比）+ `single_q_note`、`qoq_pct`（环比）+ `qoq_note`、`single_q_cur_yi/prev_yi`（拆出的单季归母净利）、`base_consistency`（上年基数与实际季报是否一致）。
  - `revenue_trailing`：最近实际报告期营收（`cum_yi`、`cum_yoy_pct`、`single_q_yi`、`single_q_yoy_pct`、`qoq_pct`），标注 actual、非预告。
  - `price_reaction`：净利润断层证据（`gap_open_pct` 公告日跳空、`r_day_pct`/`r_vol_ratio` 反应日涨幅与放量、`pre_pos_1y_pct`/`pre_mom_20d_pct` 公告前位置与动量、`since_ann_pct` 公告后累计、`gap_status=intact/filled/none/pending`、`trading_days_since_r` 新鲜度；字段语义与判断要点见 `references/methodology.md` §八）。
  - `flags`（机械阈值命中，非结论）：`positive_type/negative_type/turnaround/accelerating/qoq_positive/cum_yoy_ge_min/prefilter_pass`。
  - `type/summary/change_reason/ann_date/first_ann_date`。

`forecast_scan.py` 同步输出 `reports/cninfo_enrich_<period>.json`；`cninfo_enrich.py` 可定向重解析：

- `meta`：`requested/found/missing`、`se_date`、`notes`。
- `stocks[]`：每个候选一条。
  - `announcement`：`title/ann_date/url`（原文 PDF 链接）。
  - `parsed`：`kf_net_profit_yi`（**扣非净利**，亿元，`low/high/point`）、`revenue_yi`（营收，多数为空）、`parent_net_yi`（归母，与 Tushare 交叉核对）、`revenue_disclosed`。每项带 `raw`（原文片段）与 `confidence`（high=单位贴着数字、med=用表头“单位：万元”推断）。
  - `text`：预告全文（预告很短，直接给模型核对 `parsed`、读完整变动原因）。
  - `notes`：如“公告未披露营业收入”“未检出扣非净利”。

常用参数：`--codes`（逗号分隔候选）、`--from <forecast_scan JSON>` + `--top N` + `--positive`（从初筛结果取候选）、`--workers`（并发线程，默认 6）、`--refresh`（忽略缓存重下）、`--no-cache`。

## 数据存储与增量

预告是逐日增量披露的，所以脚本把抓到的都落库、每天只补新增，不重复全量抓：

- **连接层**：统一走仓库共享的 `shared/data/db_core.py`（开发态）/ 同步后的 `scripts/_shared/db_core.py`，不自建连接方案。后端由 `ALPHA_DB_BACKEND` 决定（PostgreSQL 默认，sqlite 离线），表都建在共享库里、以 `forecast_` 前缀命名（首次运行自动建表）。
- **落库的东西**：`forecast_cninfo_fetch_log`（CNInfo公告日水位）、`forecast_cninfo_announcement`（官方公告元数据与链接）、`forecast_tushare_cache`（非权威差异对照）、`forecast_cache`（CNInfo优先合并后的报告事实层）、`forecast_enrich_cache`（公告PDF解析/原文），以及原有 income/basic/daily/verdict/theme/overview 台账。
- **增量逻辑**：CNInfo 与 Tushare 使用各自独立的公告日水位；未扫描日期补抓、最近 `--refetch-days` 日重扫。CNInfo 新公告下载 PDF 一次后复用缓存；每股留最新公告、更正公告覆盖旧版。最终事实层是 CNInfo 集合加显式 Tushare 例外，不以 Tushare 候选名单限制 CNInfo 发现。
- **控制开关**：`--rebuild`（忽略水位、整窗重扫并刷新季报）、`--refresh-income`（强制重取季报，用于季报追溯调整）、`--no-cache`（本次不落库、全量抓）。`cninfo_enrich --refresh` 强制重下原文。
- **性能**：公告日、PDF、income 与日线均使用有界线程池；PostgreSQL 批量 upsert，CNInfo 每线程复用 HTTP Session。首次迁移需补历史公告 PDF，之后日更只下载新增/修订公告。

## 输出规范

- **文风讲人话，减少机械与僵硬。** 像跟懂行的人当面把这季预告讲清楚：先给整体判断，再用少量关键数字支撑，把“为什么优秀 / 有什么坑”说透。不堆“综上所述/值得注意的是”，不写“字段A - 字段B - 字段C”式横杠拼接。
- **同项罗列优先用 list，每条说完整话。** 多只个股、多条原因、多项 caveat 拆成 bullet，一条一项、每条完整通顺；结构化对照（个股 × 三口径数值）才用表格。
- **每只优秀个股必须交代三口径**（当年累计同比 / 单季度同比 / 环比）和一句可持续性判断（主业驱动还是含一次性），别只丢一个同比数字。
- **行业趋势写归因，不写计数**：“强3/弱1”只是入口；要说出强表现的共性驱动是什么、弱表现是行业级拖累还是个体事故、两侧交叉验证后行业方向是否成立（`references/methodology.md` §九），与落台账的 `theme_trends` 同一结论；样本少就诚实判“证据不足”。
- **产业结构综述前瞻有据、偏差必标**：(a)(b) 式分条，每条=宏观组+方向+依据（家数/增速中位/断层计数/原因共性）+前瞻判断与证伪条件（§十）；必须点明预告是触发式披露、样本偏向剧烈变化，综述反映的是边际结构，某行业缺席≠没事。
- **区分预告与实际**：净利来自预告（中值、区间），营收来自最近实际报告期（trailing）——两者基期不同，报告里要说清楚，不能把 trailing 营收当成本期预告营收。
- **引用 PE 必标口径且扣非优先**：有 cninfo 扣非就写“扣非年化PE X 倍（归母年化 Y 倍）”，注明是预告中值年化（中值÷报告期季数×4，未调季节性），与静态PE/PE-TTM 不同；扣非≤0（归母正扣非负）要点破“利润主要为非经常性损益、归母 PE 参考意义有限”；扭亏小基数的数百倍 PE 按低基数失真处理、改讲绝对额（§十一）。
- **取了扣非就用扣非佐证含金量**：写强档时若 CNInfo 公告解析拿到扣非净利，点出扣非与归母的差额（一次性成分大小）；扣非贴近归母 = 含金量高，差额大 = 高增里有水分。扣非数字要标 CNInfo 来源与 `confidence`。
- **诚实 caveat 必写**：低基数/扭亏弹性、一次性损益、`base_consistency=diverge`、`income_missing`、样本随时间累积、“没有预告≠业绩差”。
- **红线**：不写买入/卖出/止损/目标价/仓位；可以写“业绩优秀度分档”“持续性待验证条件”“需结合估值另判”。

## 示例

用户：`看下 2026 半年报业绩预告，帮我筛出业绩优秀的票，要单季同比和环比。`

执行：

```bash
python3 scripts/forecast_scan.py --period 20260630
```

然后读 `reports/forecast_scan_20260630.json`，按 `references/methodology.md` 的锚点逐股判断，套 `references/output_template.md` 输出「整体画像 + 强/中/观察三档 shortlist（每只带三口径与可持续性判断）+ 统一 caveat」。例如某只预增票：当年累计同比 +60%、单季度同比 +80%（Q2 明显加速）、环比 +18%，原因是主业量价齐升、最近实际期营收同步增长 → 列入“强”，caveat 是需结合估值另判。
