---
name: us-ai-semi-earnings
description: 美股 AI 与半导体公司的财报与电话会议逐日跟踪、拆解与综合分析。覆盖超大厂、算力芯片、存储、代工封装、半导体设备、EDA/IP、模拟功率、光互连、网络服务器、AI 云与数据中心、电力散热、AI 软件应用共约 100 家。以 SEC EDGAR 的 XBRL 为数值权威、8-K/6-K 新闻稿为财报当晚事实与指引来源、Motley Fool 与 Alpha Vantage 双路取电话会议全文，拆 GAAP 与 non-GAAP 差额、三口径增减、毛利率、现金流成色、指引变化、EPS 超预期与公告后股价反应，产出每日简报与随披露增量更新的财报季 HTML；季报页使用单行筛选工具栏，并在公司详情展示包含财报发布标记的 120 个交易日 K 线。适用于"昨晚谁报了财报""英伟达这季怎么样""台积电电话会议说了什么""AMD 指引上修了吗""这季半导体谁超预期""美光财报对存储周期意味着什么""谁提到了 HBM 供应紧张"等提问。脚本只做取数、日历季对齐、确定性拆解与渲染；分档、利润质量成色与指引判断由模型完成。HTML 不展示运行覆盖统计卡片或产业链传导汇总，也不要求模型做产业链状态分析。不覆盖 A 股（用 a-stock-quarterly-report）与美股盘面复盘（用 chstock-usmarket-report）；不给买卖建议、目标价或仓位。
---

# 美股 AI / 半导体财报与电话会议跟踪

## 目标

- **做什么**：按日历季扫描约 100 家美股 AI 与半导体公司的财报披露，对每家拆出三口径增减、GAAP 与 non-GAAP 差额、毛利率与费用结构、现金流成色、资产负债表前瞻信号、EPS 超预期与公告后股价反应，取回**电话会议全文**并按预备发言/问答切分，最后由模型判断谁真正交付了、利润是不是真的、指引往哪走。
- **不做什么**：不做 A 股财报（用 `a-stock-quarterly-report`）、不做美股盘面与赚钱效应复盘（用 `chstock-usmarket-report`）、不做单股长线基本面深挖；不给买卖建议、目标价、仓位；不在脚本里判断"这季好不好"。
- **给谁用**：跟踪 AI 资本开支周期的投研读者。重点是快速看清单家公司本季交付、利润质量、指引变化与电话会议增量。

## 数据现实（务必先读，直接决定能交付什么）

1. **事实分两个阶段到达，报告必须说清手上是哪一个。** 财报当晚只有 8-K/6-K 新闻稿：营收、EPS、**下季指引**——没有现金流，没有资产负债表。10-Q 带着 XBRL 在 0–10 天后才落地。每家的 `data_stage` 标了是 `xbrl` 还是 `press_release_only`；**`press_release_only` 的公司不能评论现金流**。
2. **XBRL 里永远没有指引。** 美股股价当天的涨跌六七成由指引决定，而指引只在新闻稿和电话会议里。只讲已发生的季度等于没读财报。
3. **财年错位是常态，横向比较只能用日历季。** NVDA 财季二月到四月，AVGO 二月到五月，MU 三月到五月，LRCX 财年六月底结束——三家都叫"Q1"。脚本按**区间中点落在哪个日历季**统一对齐（与 SEC 自己的 CY frame 口径一致，已对 NVDA/AVGO/MU/TXN 逐一验证）。**不要用公司自报的 Q1/Q2 拼横截面。**
4. **GAAP 与 non-GAAP 是两套数。** XBRL 全是 GAAP；卖方一致预期与 `surprise_pct` 对的是 non-GAAP。差额主要是股权激励、并购摊销、重组。同时引用必须标口径，`margins.sbc.pct` 就是这个差额的主要来源。一致预期接近零时百分比超预期会失真，脚本以 `surprise_pct_unstable` 标出。
5. **ADR 不交 10-Q。** TSM / ASML / ARM / UMC / STM / ASX 交 20-F/6-K，季度 XBRL 稀疏甚至没有（TSM 走 IFRS 分类，ASML 只有年度数）。这些公司 `statement_source=adr_limited`，数字要从 `press_release_head` 和电话会议读。**空白报表块是披露制度差异，不是取数失败。**
6. **电话会议有两条免费路径，都不保证当晚就有。** Motley Fool 无配额但覆盖不齐（实测 TSM/ASML 次日即有，TXN 报告五天后仍无）；Alpha Vantage 覆盖更齐、带逐段情绪分，但免费额度 **25 次/天**（一次调用 = 一家一季，永久缓存后不再重取）。两路都没有时 `transcript.status=pending`——**这是财报当晚的常态，如实写待补，绝不凭数字推测会议内容**。
7. **现金流没有单季标签。** 美股只按年初至今披露现金流，脚本用相邻累计期差分补出单季（`derived=cumulative_diff`），Q4 同理由年报减前三季。数值可信，但公司做过追溯重述时差分会把重述额甩进某一季。
8. **一天只能看到财报季的一小片。** 六周里每晚几家到几十家，任何一天的 `reported_count / universe_size` 都是残缺的，写报告必须交代披露进度与 `not_reported` 名单。

## 领域方法论（纲要，细则见 `references/`）

判断分两层，逐层加深：

### 一、单家公司：数字层（细则见 `references/methodology.md`）

1. **三口径 × 五条线**：营收/毛利/经营利润/净利润/经营现金流各有同比与环比。**半导体看环比重于同比**——同比含去年基数的故事，环比才是当下动能。毛利增速 > 营收增速 = 结构在变好；经营利润增速 > 毛利增速 = 费用杠杆在起作用。
2. **GAAP vs non-GAAP 必须拆开**：GAAP 巨亏 + non-GAAP 大超预期是这个宇宙的常见组合，两个都要报并说清差在哪；`sbc` 占营收高且抬升 = non-GAAP 的漂亮有一部分是股东在稀释中买单。
3. **利润质量三件套**：经营现金流/净利润、存货与应收相对营收的 gap、自由现金流与 capex 强度。**gap 为正在半导体里有两种相反含义**（为订单备货 vs 卖不动积压），数字分不出来，**只有电话会议能分**。
4. **资产负债表讲下几个季度**：RPO（美股最接近在手订单的字段，只有部分公司披露）、合同负债、在建与固定资产、净现金。

### 二、单家公司：指引与电话会议（细则见 `references/transcript_reading.md`）

指引是美股财报的第一位信息。判 `guidance_call` 要做四个动作：这季实际落在**上季指引**的哪一侧、下季指引中值 vs 市场预期、**全年指引动没动**、毛利率指引方向。

电话会议六个抓手：指引的完整形状（营收/毛利率/费用/税率都要）、需求侧**具体**证据（客户名、可见度期限、backlog、sold out——"我们对 AI 长期机会感到兴奋"是零信息）、供给与产能约束、毛利率桥、**问答攻防**（重复出现的问题 = 市场担心什么；管理层回避本身是信息）、口径变化与新披露。

**股价与指引背离最有信息量**：超预期却大跌几乎总是指引问题，不及预期却大涨几乎总是指引超预期。

## 工作流程

1. **定日历季**：`CY2026Q2` = 2026 年 4-6 月。用户说"这季""昨晚"就取当前所在或最近结束的日历季。
2. **跑扫描**：`python3 scripts/earnings_scan.py --frame CY2026Q2`。发现已披露公司 → 取 XBRL → 抓 8-K/6-K 新闻稿与指引句 → 取 EPS 一致预期 → 取 Yahoo 日线并截取包含财报日的 120 个交易日窗口 → 算股价反应 → 按超预期幅度与体量优先级取电话会议 → 出决策包与全样本。首次约几分钟，之后增量。
3. **读决策包做初筛**：读 `reports/usearn_scan_<frame>.json`。先按 `priority_tickers` 指定的顺序读高优先级公司，再处理其余新披露公司；`companies[]` 保留全部已披露公司，不能把优先级截断误当成样本边界。按 `references/methodology.md` 逐家判断，先分出「强/中/观察/剔除」。
4. **要原话就开原文**：`read_source.py transcript <T> --frame <F> --section qa` 读问答，`read_source.py press-release <T> --frame <F>` 读新闻稿。**不要凭数字编故事**——没读原文说不出"因为某产品放量"。
5. **按主题检索原话**：需要比较多家公司对同一主题的说法时，可用 `read_source.py search "HBM" --frame CY2026Q2` 拉出命中段落；只把它作为相关公司判断的补充证据，不产出产业链状态。
6. **判分落台账**：`verdict.py context --frame <F>` 拿待判集合（新披露 + 证据已变化的待复判）。模型逐家判 `tier` + `quality_call` + `guidance_call` + `transcript_read` + `theme`，写 `reports/usearn_verdict_<frame>.json`，再 `verdict.py record` 落库。**台账增量累积，每天只判 context 列出的增量。**
7. **出交付物**：
   - **每日简报**：模型按 `references/output_template.md` 写 Markdown，再 `render_daily_html.py` 出 HTML。
   - **财报季页**：`render_period_html.py --frame <F>`，一季一页、每次从台账整页重渲染，随披露增量更新。
8. **诚实标注公司级缺口**：在相关公司详情中标明 `data_stage=press_release_only`、`transcript.status=pending`、ADR 的 `adr_limited`、`surprise_pct_unstable` 与股价缺口新鲜度。不要另做顶部运行状态、覆盖进度或配额统计区。

脑/手边界：脚本只做取数、日历季对齐、差分与比率、阈值命中、计数、渲染投影；"谁交付了、利润是不是真的、指引算不算上修"由模型写进公司台账。`screen.hits` 与 `rank_score` 是**决定先读谁的漏斗，不是结论**——它看不到指引、读不了电话会议，会把靠一次性税收优惠超预期的公司排在上修全年指引的公司前面。

## 数据获取（脚本抓手）

环境变量：

```bash
SEC_CONTACT_EMAIL=you@example.com     # 必填：EDGAR 对没有身份标识的 UA 返回 403
ALPHAVANTAGE_API_KEY=...              # 电话会议兜底路径；缺失则只走 Motley Fool
ALPHA_DB_BACKEND=postgresql
ALPHA_PG_URL=postgresql://alpha_user:alpha_pass@/alpha_data?host=/tmp
# ALPHAVANTAGE_DAILY_LIMIT=25         # 升级 premium 后调大即可解除配额
# ALPHAVANTAGE_RESERVE=5              # 预留额度，不被 transcript 抓取吃光
# SEC_CALLS_PER_SEC=8                 # SEC 公布上限 10/秒
```

依赖：`pip install requests pyyaml psycopg2-binary`（见 `requirements.txt`）。SEC / Nasdaq / Motley Fool 都是 `requests` 直连公开接口，不经 WebFetch（避免本机代理拦截）。

```bash
# 主扫描
python3 scripts/earnings_scan.py --frame CY2026Q2
python3 scripts/earnings_scan.py --frame CY2026Q2 --tickers NVDA,AMD,AVGO   # 定向核验
python3 scripts/earnings_scan.py --frame CY2026Q2 --buckets semi_equipment  # 只看一环
python3 scripts/earnings_scan.py --frame CY2026Q2 --no-av                   # 绝不动 AV 配额
python3 scripts/earnings_scan.py --frame CY2026Q2 --refresh-xbrl            # 大面积重述时

# 按需读原文
python3 scripts/read_source.py transcript NVDA --frame CY2026Q2 --section qa
python3 scripts/read_source.py transcript TSM  --frame CY2026Q2 --find "CoWoS,capacity"
python3 scripts/read_source.py press-release TXN --frame CY2026Q2 --find "outlook"
python3 scripts/read_source.py search "HBM" --frame CY2026Q2 --section prepared

# 判分落台账
python3 scripts/verdict.py context --frame CY2026Q2
python3 scripts/verdict.py record  --frame CY2026Q2 --input reports/usearn_verdict_CY2026Q2.json

# 交付物
python3 scripts/render_daily_html.py --input reports/daily-2026-07-27.md --frame CY2026Q2
python3 scripts/render_period_html.py --frame CY2026Q2

# 单源探查与自检
python3 scripts/sec_client.py NVDA --frame CY2026Q1 --press-release
python3 scripts/transcript_fetch.py TSM --call-date 2026-07-16 --list
python3 scripts/transcript_fetch.py X --budget-status
python3 scripts/test_earnings_scan.py
```

`earnings_scan.py` 常用参数：

| 参数 | 含义 | 默认 |
|---|---|---|
| `--frame` | 日历季 `CY####Q#` | 最近结束的日历季 |
| `--tickers` / `--buckets` | 限定公司 / 限定业务类别 | 全部 |
| `--since` / `--end-date` | 公告发现下界 / 数据截至日 | 季末前 30 天 / 今天 |
| `--transcript-limit` | 本次最多新取多少份会议（已缓存的不计） | 40 |
| `--no-av` / `--no-transcript` | 不动 AV 配额 / 完全跳过会议 | 关闭 |
| `--no-price` / `--no-press-release` | 跳过股价反应 / 跳过新闻稿抓取 | 关闭 |
| `--refresh-xbrl` / `--no-cache` | 忽略 XBRL 缓存水位 / 完全不落库 | 关闭 |
| `--top` | `priority_tickers` 保留的高优先级公司数；不截断 `companies[]` | 60 |
| `--th-*` | 漏斗阈值（营收同比/环比、净利同比、毛利 pp、现金覆盖、超预期、gap pp） | 见 `--help` |

决策包 JSON 关键字段：

- `meta`：`frame`、`as_of`、`reported_count`/`universe_size`、`data_stage_counts`、`transcript_stats`、`av_budget`、`source_roles`、`data_notes`。
- `buckets[]`：按业务类别的聚合与已披露/待披露名单，用于筛选和定位公司，不要求据此判断产业链传导状态。
- `priority_tickers[]`：按机械漏斗排序的优先阅读名单，长度由 `--top` 控制；它只是阅读顺序，不是样本截断。
- `companies[]`：全部已披露公司，包含 `data_stage`、`statement_source`、`announcement`（含 `provenance`：8-K / 6-K 按一致预期日匹配 / 仅一致预期日）、`growth`、`margins`（含 `sbc`）、`quality`、`balance`（含 `rpo`）、`surprise`（含 `surprise_pct_unstable`）、`price_reaction`（当日与次日双报）、`price_history`（最多 120 个交易日 OHLCV，保证财报日位于窗口内）、`guidance_excerpts`、`press_release_head`、`transcript`（状态/来源/分段统计）、`screen`。非美元 XBRL 会保留原始 `unit` 与 `value_local_millions`，绝不冒充 `value_musd`。
- `not_reported[]`：本季尚未披露的名单。

## 数据存储与增量

统一走 `shared/data/db_core.py`，表以 `usearn_` 前缀建在共享库（首次运行自动建表）。落库的东西：`usearn_company`（宇宙解析结果）、`usearn_filing`（发现水位）、`usearn_xbrl_fact`（按「公司×日历季×科目」存行）、`usearn_press_release`、`usearn_transcript` + `usearn_transcript_segment`（逐发言人存，供按主题检索）、`usearn_surprise`、`usearn_calendar`、`usearn_price_cache`、`usearn_verdict`。

增量逻辑：XBRL 按「最新 10-Q 公告日 > 缓存水位」才重取；新闻稿与电话会议一经落库永不重取（**这是 25 次/天的配额能覆盖 100 家宇宙的原因**）；日线按交易日水位补。DB 不可用时降级为全量抓取不落库，仍能出报告——这是容错，不是常态，因为那样每次都会重花 AV 配额。

## 输出规范

- **文风讲人话。** 像跟懂行的人当面把这季讲清楚：先给判断，再用少量关键数字支撑，把"为什么好/利润是不是真的/指引往哪走"说透。不堆"综上所述""值得注意的是"，不写"字段A - 字段B - 字段C"式横杠拼接。
- **同项罗列用 list，每条说完整话。** 多家公司、多条原因、多项 caveat 拆成 bullet，一条一项、每条通顺；结构化对照（公司 × 三口径数值）才用表格。
- **每家公司必须同时交代已发生的季度与指引。** 只报营收同比不报指引，是这个 skill 最不该犯的错。
- **引用 EPS 必标口径。** GAAP 还是 non-GAAP、对的是哪个一致预期；`surprise_pct_unstable` 时只写美分差额不写百分比。
- **数字之外的话要有出处。** 说"管理层称产能已售罄""某客户放量"必须来自 `read_source.py` 取到的原文并能指到发言人与段落；`transcript.status=pending` 就只讲数字能支持的结论。
- **Motley Fool 页面的 TAKEAWAYS / RISKS / SUMMARY 是网站自己的编辑内容，不是管理层原话**，永远不能当会议内容引用（数据里已有 `publisher_notes_warning`）。
- **诚实 caveat 必写**：在相关公司段落写清 `press_release_only`、`pending` 会议、ADR 限制、追溯重述与股价缺口新鲜度。
- **HTML 明确不展示**：数据截至/生成时间、未披露数、10-Q/新闻稿数量、已披露/已判分/已取电话会议卡片、Alpha Vantage 余额，以及产业链传导汇总。
- **财报季页布局**：把业务类别、分档、数据阶段和搜索放在股票列表与详情上方的同一行；窄屏可以横向滚动，但不拆成多行。公司详情在正文指标之前展示近 120 个交易日 K 线，用竖线标出财报发布日，并以淡色背景区分发布后的走势。
- **红线**：不写买入/卖出/止损/目标价/仓位；不要求产业链状态判断；可以写"交付分档""利润质量成色""指引方向""需要跟踪的证伪点"。

## 示例

用户：`昨晚谁报了？台积电这季怎么样？`

```bash
python3 scripts/earnings_scan.py --frame CY2026Q2
python3 scripts/read_source.py transcript TSM --frame CY2026Q2 --find "capacity,CoWoS,capex"
python3 scripts/verdict.py context --frame CY2026Q2
```

读决策包后判断。台积电这季走 `press_release_only` + `adr_limited`——它不交 10-Q，季度 XBRL 是空的，所以报表块全是 `—`，营收与 EPS 要从 `press_release_head` 读（NT$1,270.38 亿营收、EPS NT$27.25、同比 +36.0%），下季指引从 `guidance_excerpts` 读（营收 US$44.6–45.8B、毛利率 65–67%）。电话会议已取到，可以引 C.C. Wei 关于 CoWoS 与玻璃基板的原话。最终 HTML 把筛选项单行放在列表和详情上方，并在台积电详情中用 120 日 K 线标出财报发布日及之后走势；不附运行统计或产业链状态区。
