---
name: a-stock-earnings-forecast
description: A 股业绩预告(forecast)全市场扫描与优秀个股筛选。按最新报告期(一季报/半年报/三季报/年报)扫业绩预告，净利润预告区间按中值折算，拆出当年累计同比、最新单季度同比、环比三口径增速，抽取业绩变动原因，并可选用巨潮 cninfo 公告原文补扣非净利(Tushare 结构化预告没有)与少数公司的营收，识别一次性损益、判断含金量；还能把每只预告股按语义匹配到 daily-market-sense 的上涨主线台账（归属主线），把「优秀分档 + 主线归属」落判分台账后，渲染成一期一页、随披露增量更新的 HTML 报告期页。当用户要“看最新业绩预告”“哪些公司预增/大增/高增”“业绩预告扣非净利多少”“按中值算预告净利和增速”“把预告拆成单季同比和环比”“中报/半年报/一季报/年报业绩预告筛优秀个股”“预告里谁在加速”“业绩预告变动原因”“这季谁的预告最好”“这只预告股归属哪条主线”“把业绩预告做成 HTML/网页/报告期页”时使用。脚本只出确定性证据(中值、三口径拆解、营收 trailing、拆解护栏)、落库增量抓取、校验落台账与渲染 HTML；谁优秀/如何排序/归属哪条主线/识别低基数/扭亏/一次性损益/营收利润背离全由模型判断。不做个股基本面深挖(用 a-stock-analyzer)、不做盘面复盘选股(用 a-stock-daily-market-sense)、不给买卖建议/目标价/仓位。
version: 1.0.0
---

# A 股业绩预告扫描与优秀个股筛选

## 目标

- **做什么**：按某个报告期（默认当前最新季度，如 2026H1=20260630）全市场扫描业绩预告，把净利润预告区间按中值折算成一个数，拆出「当年累计同比 / 最新单季度同比 / 环比」三个口径的利润增速，读出业绩变动原因，对照最近实际报告期的营收趋势，最后由模型挑出真正业绩优秀的个股并排序。
- **不做什么**：不做单股基本面深挖（用 `a-stock-analyzer`）、不做盘面复盘（用 `a-stock-daily-market-sense`）；不给买卖建议、目标价、仓位；不在脚本里判断“谁优秀”。
- **给谁用**：面向要在财报季快速锁定「业绩超预期 / 加速 / 高景气」候选池的投研读者，具备基本财务常识。

## 数据现实（务必先读，直接决定能交付什么）

1. **业绩预告只有净利润，没有营收。** Tushare `forecast` 字段只有 `net_profit_min/max`（万元）、`p_change_min/max`（同比%）、`last_parent_net`（上年同期归母净利）、`type`、`summary`、`change_reason`。所以“按中值算收入”在预告层面无法实现——脚本改为给出**最近实际报告期（如 Q1）的营收 trailing 值**作为收入侧参照，并明确标注“actual，非预告”。
2. **换到 cninfo 也补不了营收，但能补扣非净利。** 实测巨潮资讯网业绩预告原文**多数同样不披露营业收入**（创业板模板式、主板预增公告都如此），换源解决不了营收缺口——这是“业绩预告”这种披露类型本身的特性。但 cninfo 原文普遍披露**扣非净利润（扣除非经常性损益）**，这是 Tushare 结构化预告**没有**、判断业绩含金量最关键的一项，且只在公告原文里有。所以最优是**混合**：Tushare 结构化打底扫全市场，cninfo 作**按需增强层**只对优秀候选补「扣非净利 + 完整原因 +（少数有的）营收」（见工作流程第 4 步）。
3. **预告净利多为归母口径**（与 `last_parent_net` 对齐，实测一致），单季拆解统一用归母净利（`income.n_income_attr_p`）。
4. **只覆盖已披露预告的公司。** 业绩平稳、未触发强制预告的公司可能根本没有预告——“没有预告”≠“业绩差”，不要据此下负面结论。
5. **季初样本会偏少、随时间累积。** 例如 7 月初看 H1，预告刚开始密集披露；过几天重跑或放宽 `--start-ann` 可补全。

## 领域方法论（纲要，判断细则见 `references/methodology.md`）

读业绩预告的核心不是“同比多高”，而是**边际方向 + 增速含金量 + 可持续性**。四条主线：

1. **三口径一起看，重仓看单季边际。** 预告给的是累计口径（H1 = Q1+Q2）。但市场定价的是最新单季的同比和环比。**单季同比 > 当年累计同比 = 环比向上 = 加速**，往往比“高但减速”的累计增长更值钱。脚本已把三口径拆好，别只盯累计同比。
2. **基数质量决定增速含金量。** 几百上千%的同比先分辨是「低基数/困境反转」（扭亏、上年同期亏损、周期底部如养殖/化工/资源）还是「高基数持续高增」。前者是弹性、不等于经营质量；真正稀缺的是高基数上还能高增。结合 `type`（扭亏 vs 预增）、`last_parent` 绝对值、绝对利润规模一起看。
3. **读 `change_reason` 判可持续性，有扣非就直接看扣非。** 主业量增、价升、毛利率提升、新产能/新客户放量 = 可持续、含金量高；资产处置、政府补助、投资收益、公允价值变动、并购并表、税收返还、诉讼赔偿等一次性/非经常损益 = 要打折甚至剔除。文本里出现这些词就警惕。若已用 cninfo 增强层取到**扣非净利**，直接看扣非与归母的差额：差额大说明利润里一次性成分多，扣非增速比归母增速更能代表真实经营，是判含金量最硬的量化证据。
4. **营收-利润匹配度（用 trailing 实际营收）。** 利润高增但最近实际期营收不增，可能靠降本/减值转回/毛利修复，持续性存疑；营收利润同步高增更健康。

**“业绩优秀”排序锚点**（模型据此判断，不是脚本）：
- **强**：单季与累计双高且单季≥累计（加速）、绝对利润不小、原因是主业量价驱动、营收同步向好、不依赖低基数。
- **中**：累计高但单季减速；或高增但基数偏低/含一次性；或营收未同步。
- **观察/剔除**：扭亏但主要靠非经常损益、预减/首亏/续亏、单季环比转弱、原因含大额一次性收益。

## 工作流程

1. **定报告期**：解析用户说的季度/半年/年报；未指定就用当前最新季度末（脚本 `--period` 缺省即“今天之前最近的季度末”）。确认 2026H1 → `20260630`。
2. **跑证据脚本**：`python3 scripts/forecast_scan.py --period 20260630`。脚本会逐交易日扫 `forecast(ann_date=)`（接口不支持区间/纯 period 查询）、按报告期过滤、每股留最新一版预告，再取实际季报拆三口径增速、挂 trailing 营收，写出 `reports/forecast_scan_<period>.json`。**已抓的预告/季报都存进 DB（`forecast_*` 表），每天重跑只增量抓新公告日与新个股，不重复全量抓取**（详见「数据存储与增量」）。`meta.fetch_stats` 会报告本次实际抓了几个公告日、几只走了缓存。
3. **模型读证据做初筛**：读 JSON，按上面的方法论**逐股判断是否优秀并排序**——重点用 `profit_growth`（三口径 + `base_consistency`）、`flags`（`accelerating/turnaround/positive_type` 等）、`change_reason`（一次性损益识别）、`revenue_trailing`（营收匹配）。先圈出「强 / 中 / 观察」候选池。
4. **（可选）cninfo 增强优秀候选**：对初筛出的强/中候选跑 `python3 scripts/cninfo_enrich.py --period 20260630 --codes <候选代码>`（或 `--from reports/forecast_scan_20260630.json --positive --top 15`）。脚本抓每家预告原文，补出 Tushare 没有的**扣非净利**、完整变动原因、以及少数公司披露的营收，写 `reports/cninfo_enrich_<period>.json`。用扣非/归母差额复核含金量、剔除靠一次性冲高的票，再定档。`parsed` 是最佳努力解析（带 `raw` 与 `confidence`），要对照同一条记录的 `text` 全文核对后再采用。
5. **判分 + 主线匹配落台账**：跑 `python3 scripts/verdict.py context --period 20260630` 拿到「待判个股（新披露 + 预告已修订/增速漂移的待复判）+ 主线注册表（daily-market-sense 的 `theme_registry`：名称/别名/当前★状态/成员样本）」。模型据此**逐只判 `tier`（强/中/观察/剔除）+ `theme_id`（归属哪条在场主线，对不上填 `null`=无归属）**，写 `reports/verdict_<period>.json`，再 `python3 scripts/verdict.py record --period 20260630 --input reports/verdict_<period>.json` 落 `forecast_verdict` 台账。主线匹配是**语义判断**：用 `change_reason`/行业比对主线 name/aliases/成员样本，弱匹配标 `match_confidence=low`（渲染成"疑似"），完全对不上就 `null`（"无归属"本身是"业绩强但暂无主线关注"的信号）。台账增量累积，每天只判 context 列出的增量。判分细则与匹配方法见 `references/verdict_and_html.md`。
6. **生成报告期 HTML**：`python3 scripts/render_period_html.py --period 20260630` → `reports/forecast_<period>.html`。**一期一页、每次从累积缓存整页重渲染**：读 evidence + enrich + `forecast_verdict`，实时 join `theme_daily_state` 取主线当前状态；页面含 KPI、按主线分组视图、带「归属主线」列的可筛选明细表，`NEW/更新/待复判` 由披露日与判分快照派生。自包含单文件、无 CDN。渲染只投影不新增判断。
7. **（可选）写 Markdown 报告**：需要叙事版时按 `references/output_template.md` 输出整体画像 + 优秀个股 shortlist（每只三口径、扣非含金量、归属主线、caveat）。HTML 与 Markdown 同源于 evidence + verdict，二选一或都出。
8. **诚实标注缺口**：`meta.income_missing`、`single_q_note=base_missing/base_nonpositive/cur_missing`、`base_consistency=diverge`、cninfo `missing`、以及 `theme_registry` 为空（主线台账未填充需先跑 daily-market-sense）都要如实说明。

脑/手边界：脚本只做取数、按中值折算、确定性拆解、护栏标记、校验落库、渲染投影；“谁优秀、怎么排、归属哪条主线、原因可不可持续、要不要剔除”全是模型写进 `forecast_verdict` 台账的判断。**不在脚本里替模型下业绩好坏或主线归属结论，不给买卖建议。** 主线台账只读（归 daily-market-sense 维护），本 skill 不写主线。

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

依赖：`pip install tushare pandas requests PyMuPDF PyPDF2`（见 `requirements.txt`；后三者仅 cninfo 增强需要）。cninfo 走公开披露接口，用 `requests` 直连（不经 WebFetch，避免本机代理拦截）。

命令（在 skill 根目录执行）：

```bash
# 第一步：全市场结构化扫描 + 三口径拆解
python3 scripts/forecast_scan.py --period 20260630            # 指定报告期
python3 scripts/forecast_scan.py                              # 默认最新季度末
python3 scripts/forecast_scan.py --period 20260630 --positive-only   # 只看向好方向
python3 scripts/forecast_scan.py --period 20260630 --start-ann 20260501   # 放宽扫描窗口补早鸟

# 第二步（可选）：对初筛出的优秀候选，用 cninfo 补扣非净利 / 完整原因 / 营收(若有)
python3 scripts/cninfo_enrich.py --period 20260630 --codes 600872.SH,002648.SZ,300014.SZ
python3 scripts/cninfo_enrich.py --period 20260630 --from reports/forecast_scan_20260630.json --positive --top 15

# 第三步：判分 + 主线匹配落台账（模型判 tier+theme_id，脚本只出上下文/校验落库）
python3 scripts/verdict.py context --period 20260630          # 出待判个股 + 主线注册表 → 模型判 → 写 verdict_<period>.json
python3 scripts/verdict.py record  --period 20260630 --input reports/verdict_20260630.json

# 第四步：生成报告期 HTML（一期一页，从累积缓存整页重渲染）
python3 scripts/render_period_html.py --period 20260630       # → reports/forecast_20260630.html
```

常用参数：

| 参数 | 含义 | 默认 |
|---|---|---|
| `--period` | 报告期末（季度末 YYYYMMDD） | 今天之前最近的季度末 |
| `--start-ann` / `--end-ann` | 公告日扫描窗口（逐交易日调 `forecast(ann_date=)`） | period−45d ~ min(今天, period+75d) |
| `--positive-only` | 丢弃恶化方向（预减/首亏/续亏/增亏/略减） | 关闭 |
| `--min-pchange` | `prefilter_pass` 对当年累计同比的阈值(%) | 0 |
| `--refetch-days` | 最近 N 个交易日始终重扫（抓当日新披露/修订） | 3 |
| `--rebuild` | 忽略缓存水位、整窗重扫并刷新季报 | 关闭 |
| `--refresh-income` | 强制重取实际季报（季报追溯调整时用） | 关闭 |
| `--no-cache` | 本次不落库、全量抓取 | 关闭 |
| `--fetch-workers` | forecast/income 并发线程数（限流时设 1 串行） | 4 |
| `--out` / `--stdout` | 输出路径 / 同时打印完整 JSON | `reports/forecast_scan_<period>.json` |

输出 JSON 结构（关键字段，完整字典见 `references/methodology.md`）：

- `meta`：报告期、单季标记、扫描窗口、家数、`income_missing`、`data_notes`（含营收/拆解口径 caveat）。
- `stocks[]`：每股一条，按当年累计同比降序。
  - `net_profit`：`min_yi/max_yi/median_yi`（预告净利中值，亿元）、`last_parent_yi`（上年同期）。
  - `profit_growth`：`cum_yoy_pct`（当年累计同比，`cum_yoy_source=p_change/derived`）、`single_q_yoy_pct`（单季度同比）+ `single_q_note`、`qoq_pct`（环比）+ `qoq_note`、`single_q_cur_yi/prev_yi`（拆出的单季归母净利）、`base_consistency`（上年基数与实际季报是否一致）。
  - `revenue_trailing`：最近实际报告期营收（`cum_yi`、`cum_yoy_pct`、`single_q_yi`、`single_q_yoy_pct`、`qoq_pct`），标注 actual、非预告。
  - `flags`（机械阈值命中，非结论）：`positive_type/negative_type/turnaround/accelerating/qoq_positive/cum_yoy_ge_min/prefilter_pass`。
  - `type/summary/change_reason/ann_date/first_ann_date`。

`cninfo_enrich.py` 输出 `reports/cninfo_enrich_<period>.json`：

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
- **落库的东西**：`forecast_cache`（每股最新一版预告）、`forecast_fetch_log`（已扫过的「报告期×公告日」水位）、`forecast_income_cache`（实际季报归母净利/营收，含 NULL 行标记「查过但无数据」，避免次新股每次重查）、`forecast_basic_cache`（名称/行业）、`forecast_enrich_cache`（cninfo 扣非/原文）、`forecast_verdict`（模型判分 tier + 主线归属 + 证据指纹，增量判、待复判可查）。`theme_registry`/`theme_daily_state` 是 daily-market-sense 维护的主线台账，本 skill **只读**。
- **增量逻辑**：`forecast_scan` 只抓 `forecast_fetch_log` 里没有的公告日，外加最近 `--refetch-days`（默认 3）天始终重扫（抓当日新披露与修订版）；然后从 `forecast_cache` 读回该报告期**全量累积**结果做拆解。个股实际季报按 `(ts_code, period)` 命中缓存，只有新出现的个股才发 income 请求。`cninfo_enrich` 按 `(code, period)` 复用已下载解析的公告，不重复下 PDF。
- **控制开关**：`--rebuild`（忽略水位、整窗重扫并刷新季报）、`--refresh-income`（强制重取季报，用于季报追溯调整）、`--no-cache`（本次不落库、全量抓）。`cninfo_enrich --refresh` 强制重下原文。
- **性能**：落库写用单条批量语句（PostgreSQL 走 `execute_values`、SQLite 走 `executemany`），缓存读用 IN 批量、`forecast_cache(end_date)` 建索引；冷启动的 forecast 按公告日、income 按个股用线程池并发（`--fetch-workers` 默认 4；cninfo `--workers` 默认 6，配 thread-local `requests.Session` 复用连接）。限流时把 workers 设 1 串行，`TushareProxy` 自带退避重试。
- **效果**：首日/`--rebuild` 全量但已并发+批写（实测 29 只约 6.4s→2.5s）；此后每天重跑只抓最近 3 个公告日、季报几乎全走缓存（约 0.9s）。cninfo 冷抓 6 只约 5.6s→0.7s。台账累积，历史披露不因换天而丢。

## 输出规范

- **文风讲人话，减少机械与僵硬。** 像跟懂行的人当面把这季预告讲清楚：先给整体判断，再用少量关键数字支撑，把“为什么优秀 / 有什么坑”说透。不堆“综上所述/值得注意的是”，不写“字段A - 字段B - 字段C”式横杠拼接。
- **同项罗列优先用 list，每条说完整话。** 多只个股、多条原因、多项 caveat 拆成 bullet，一条一项、每条完整通顺；结构化对照（个股 × 三口径数值）才用表格。
- **每只优秀个股必须交代三口径**（当年累计同比 / 单季度同比 / 环比）和一句可持续性判断（主业驱动还是含一次性），别只丢一个同比数字。
- **区分预告与实际**：净利来自预告（中值、区间），营收来自最近实际报告期（trailing）——两者基期不同，报告里要说清楚，不能把 trailing 营收当成本期预告营收。
- **取了扣非就用扣非佐证含金量**：写强档时若 cninfo 增强拿到扣非净利，点出扣非与归母的差额（一次性成分大小）；扣非贴近归母 = 含金量高，差额大 = 高增里有水分。扣非数字要标 cninfo 来源与 `confidence`。
- **诚实 caveat 必写**：低基数/扭亏弹性、一次性损益、`base_consistency=diverge`、`income_missing`、样本随时间累积、“没有预告≠业绩差”。
- **红线**：不写买入/卖出/止损/目标价/仓位；可以写“业绩优秀度分档”“持续性待验证条件”“需结合估值另判”。

## 示例

用户：`看下 2026 半年报业绩预告，帮我筛出业绩优秀的票，要单季同比和环比。`

执行：

```bash
python3 scripts/forecast_scan.py --period 20260630
```

然后读 `reports/forecast_scan_20260630.json`，按 `references/methodology.md` 的锚点逐股判断，套 `references/output_template.md` 输出「整体画像 + 强/中/观察三档 shortlist（每只带三口径与可持续性判断）+ 统一 caveat」。例如某只预增票：当年累计同比 +60%、单季度同比 +80%（Q2 明显加速）、环比 +18%，原因是主业量价齐升、最近实际期营收同步增长 → 列入“强”，caveat 是需结合估值另判。
