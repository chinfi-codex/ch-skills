---
name: chstock-usmarket-report
description: 当用户要求复盘昨晚/昨夜美股、查看纳斯达克科技观察池表现、看池内每只票涨跌明细、判断纳指(QQQ)大盘走势、分析今晚纳斯达克科技板块的赚钱效应在哪个方向/哪个主题在承载资金(按成交额 dollar-volume)、识别纳指科技异动票(±7%)并联网查催化、或要求在现有观察池之外主动挖掘新方向/新主题并给纳入建议、回看指定 YYYY-MM-DD 美股交易日、把美股日报导出 HTML/网页时使用此 skill。本 skill 把跟踪范围收敛到纳斯达克科技：以纳指100(QQQ)为唯一大盘锚(不引入 SPY/DIA/IWM 风格四象限)；报告由三层组成——观察池逐股明细 + 纳指科技板块赚钱效应主线(模型按业务事实临时归纳主题、按 dollar-volume 占比定主线、不套 ETF/GICS 现成标签) + 池外新方向主动挖掘建议；异动只在纳指科技范围内提取，任何异动票/新方向票都必须联网核查催化（Tavily 优先）后再写入。脚本只输出确定性证据(Nasdaq 过滤、成交额计算、52周位置/量比/vs-QQQ)，是否属于科技、归属哪个主题、主线评级、归因与建议由模型完成。不用于盘中实时监控、个股深度基本面研究、目标价或买卖建议；不覆盖港股、A股、加密货币，以及黄金/航空/金融等非科技板块。
---

# 纳斯达克科技观察池日报

## 目标

1. **做什么**（三层，缺一不可）：
   - **观察池逐股明细**：基于 `assets/stock_pool.yaml`（默认从飞书同步）拉 QQQ + 全部成员股一年日线，逐只给当日 / 5 日 / 20 日 / 52 周位置 / vs-QQQ 超额 + 信号标记；
   - **纳指科技板块赚钱效应**：扫纳斯达克当日强势股，按 dollar-volume（成交额）找出今晚钱在哪个主题、够不够格算主线——这是报告的核心，方法论见 `references/sector_money_effect.md`；
   - **池外新方向主动挖掘**：把今晚有赚钱效应、但观察池没覆盖到的新方向挖出来并给纳入建议，方法论见 `references/new_direction_mining.md`。
2. **不做什么**：不引入 SPY/DIA/IWM 与风格四象限；不覆盖非科技板块（黄金/航空/金融等 Nasdaq 上市非科技股由模型剔除）；不在脚本里调 LLM 或写 Markdown；不替模型下结论；不给目标价 / 买卖建议；不杜撰未联网验证的催化。
3. **给谁用**：聚焦纳指科技的自选投资者 / 研究员——既看自己池子里的票，也要知道纳指科技里钱在往哪个方向走、有没有该补的新方向。

## 适用边界

- 默认取**最近一个已结束**的美股交易日；用户给 `YYYY-MM-DD` 时取该日或之前最近的可用交易日。
- 时区：Yahoo 的 `date` 是美东交易日；"昨晚美股" = 美东上一个收盘日（中国日历差 1 天）。只含**常规交易时段**收盘价，不含盘前 / 盘后。
- 货币 USD，不做汇率换算；成交额（dollar-volume）= 收盘价 × 当日成交股数。
- **脑 / 手边界**：脚本只做确定性的事——Nasdaq 交易所过滤、成交额计算、52周位置 / 量比 / vs-QQQ 派生、按成交额排序。**"是不是科技、归哪个主题、主线几星、要不要建议纳入"全部由模型判断**。Nasdaq 过滤是交易所级（确定性），不等于科技；池里会混入钢铁 / 生科 / 防务等 Nasdaq 非科技股，模型读 `ticker + name` 剔除。
- **全市场扫描只覆盖最近收盘日**（Yahoo 预设 screener 不支持历史回溯）。若 `--date` 指向更早交易日，evidence 里 `market_wide_movers.date_aligned=false`，此时板块赚钱效应 / 新方向段写明"扫描仅支持最近收盘日，本次回看不展示该层"或跳过。
- 异动票 / 新方向票的催化来自联网检索（**Tavily 优先**，命令见领域方法论 §6）公开结果，必须**带出处与日期**，查不到写"未检索到明确催化"。

## 领域方法论

### 总览：三层 + 成交额优先

报告是三层叠加：**大盘锚（QQQ）→ 板块赚钱效应（纳指科技全景）→ 观察池（你已覆盖的部分）→ 池外新方向（你没覆盖的部分）**。贯穿全程的纪律是**成交额（dollar-volume）优先**：没有成交额支撑的涨幅只是弱证据；主题强弱、主线评级、新方向可信度都先看 dollar-volume 厚度。

### 1. 大盘锚（仅 QQQ）

QQQ 用四个数读，不与个股做扩散度对比、不做风格判断：

- **当日 `change_pct`**：纳指今天偏多还是偏空。
- **5 日 / 20 日趋势**：中短期方向。当日与 5 日同向才下"趋势"结论，不同向只写"短期 vs 中期分歧"。
- **52 周位置 `position_52w`**：贴近高位（≥0.9）还是中位，决定"高位延续"还是"修复中"。
- **量能 `vol_vs_20d`**：量比放大方向更可信，缩量方向意义打折。

### 2. 观察池逐行框架（每只都写）

每只成员股给：当日 `change_pct`、`five_day_trend_pct`、`vs_qqq_1d` / `vs_qqq_5d`（相对 QQQ 超额，**这是精选池的意义所在**——多数票只是复读大盘，超额才看出谁真强）、`position_52w`，加一个**信号标记**（紧凑符号，不是整句话）。

**`信号` 列图例（紧凑符号优先，禁止把同一句话铺满整列）：** 第一个箭头是**当日**方向，第二个箭头是 **5 日**方向。

| 符号 | 含义 | 触发条件 |
|---|---|---|
| `↑↑` | 当日与 5 日同向上行（趋势内·常态） | 当日 > 0 且 5 日 > 0 |
| `↓↓` | 当日与 5 日同向走弱 | 当日 < 0 且 5 日 < 0 |
| `↑↓` | 当日逆 5 日上行（逆势反弹·待观察） | 当日 > 0 且 5 日 < 0 |
| `↓↑` | 当日逆 5 日回落（回吐 / 转弱） | 当日 < 0 且 5 日 > 0 |
| `＋` | 当日跑赢 QQQ（`vs_qqq_1d` > 0），与方向符号并写如 `↑↑＋` | 当日相对 QQQ 为正 |
| `★` | 触及 ±7%，详见异动核查（与方向符号并写如 `★↑↑`） | \|当日\| ≥ 7% |

只有当符号不足以说明、且确有研究价值时，才在符号后补 **≤6 字**（如 `↓↑ 高位回吐`、`↑↑ 中期未转强`）。**禁止**把整句模板写满整列，也**禁止**杜撰原因。

### 3. 板块赚钱效应（核心）→ `references/sector_money_effect.md`

读 `references/sector_money_effect.md` 执行完整流程。纲要：

1. 取 `market_wide_movers.rises` 作赚钱效应池（已 Nasdaq 过滤 + 成交额降序）→ **模型剔除非科技** → 按业务事实**临时归纳主题**（不套 ETF/GICS）。
2. 按主题 **dollar-volume 占比**定"钱在哪"，占比是第一门槛。
3. **两遍走法**：Pass 1 按占比锁定领先 2–3 个候选主题；Pass 2 对其成员跑 `--enrich-tickers` 拿 52周位置 / 量比 / vs-QQQ，再按 **★ rubric**（★★★ / ★★ / ★）评级，并判位置（高位趋势 / 低位启动）、拥挤度、领导股 / 弹性股。

### 3b. 板块广度与轮动（可选 · curated universe）

赚钱效应池只看当晚涨幅榜，**看不到钱从哪个方向流出**。开 `--scan-universe` 会扫 `assets/nasdaq_tech_universe.yaml` 这份 curated 纳指科技宇宙，给一个稳定的广度分母，evidence 里多出 `universe_scan`：

- **轮动**：哪个 bucket 普涨（up≫down、中位 vs-QQQ 为正）、哪个在失血（down≫up、中位 vs-QQQ 为负）。例：某夜 semis_compute 12/12 普涨、software_app 9 涨 10 跌，就是"钱从软件切到半导体"——这是只看涨幅榜永远看不到的一面。
- **捞安静票**：`universe_scan.movers` 是宇宙内涨 ≥3% 的票，含没挤进涨幅榜 top-250 的"安静被买"名字，是新方向挖掘的补充输入。
- bucket 只是取数 / 广度的粗分类，**不是主题**——主题仍按 §3 由模型当晚归纳。证据不足或没开 `--scan-universe` 时，本层略过、不强行写轮动。

### 4. 板块亏钱效应

取 `market_wide_movers.drops`（同样 Nasdaq 过滤 + 成交额降序），模型剔非科技后按主题归纳，回答"今晚哪个纳指科技方向在爆量下跌"。和赚钱效应对照：若某主题既有强势成员又有成员进跌池，写"主题内部分歧"。

### 5. 池外新方向主动挖掘 → `references/new_direction_mining.md`

读 `references/new_direction_mining.md` 执行。纲要：任何今晚有赚钱效应、但观察池覆盖不到 / 不全的主题，按**三档**（新主题缺口 / 主题内龙头缺口 / 邻接个股补充）给纳入建议；每条都要过 dollar-volume 下限、`--enrich-tickers` 确认、联网核查催化（Tavily 优先，见 §6），区分一日脉冲 vs 多日持续，措辞只到"建议纳入观察"。**"多日持续"靠跨日台账查证**（见 §7 与 `references/cross_day_ledger.md`）：连续 ≥2–3 晚在赚钱效应池 = 强候选，单晚先观察。

### 6. 异动联网核查（±7%，Tavily 优先）

异动不单独成段，而是**内生在第 3 / 4 / 5 层里**：任何 `is_abnormal=true`（\|当日\| ≥ 7%）的票——无论它在赚钱主题、亏钱主题还是新方向——都标 `★` 并附一行联网核查。

**联网工具：Tavily 为主路径，WebSearch / WebFetch 仅作兜底。** 经验上 WebFetch 常报 "Unable to verify if domain … is safe to fetch"、WebSearch 直接返回 0 结果——这通常不是目标站反爬，而是部分网络（含本机 HTTPS 代理）下 Claude 的域名安全预检（要连 claude.ai）与 WebSearch 服务不可达，请求根本没碰到目标站。Tavily 是直连第三方检索 API，绕开这一层，所以默认走它。命令（路径已封装，canonical 仓库与同步副本通用）：

```bash
python scripts/web_search.py "ALAB stock news 2026-06-18" --topic news --days 7
python scripts/web_search.py "Credo CRDO catalyst 2026-06-18" --topic news --days 7 --max-results 8
```

返回 JSON（`results[].title / url / published_date / content`，已按相关度排序）；需 `TAVILY_API_KEY`（已在 `~/.zshrc` 导出，并回退仓库根 `.env`）。只有 Tavily 报错或查不到时，才退回 WebSearch / WebFetch。

核查流程（查询模板对 Tavily / WebSearch 通用）：

1. **查询 1（事件类）**：`{ticker} stock news {YYYY-MM-DD}` 或 `{ticker} {YYYY-MM-DD} catalyst`，建议加 `--topic news --days 7`。
2. **查询 2（财报 / 指引）**：临近财报窗口加查 `{ticker} earnings guidance {YYYY-MM-DD}`。
3. **查询 3（中文补充，可选）**：`{中文名} 异动 原因 {日期}`。
4. 综合 1–3 提炼一句**带来源**的解释（"据 Reuters {日期} 报道……"，来源取 `results[].url` 域名 + `published_date`）；找不到写"未检索到当日明确催化（已搜索 news / earnings / 中文异动），暂列待核查"。

**禁止**：基于价格方向反推事件（"跌 8% 大概率财报 miss"）；编造分析师评级；引用未真实出现在搜索结果里的标题。

**核查优先级（避免一晚几十只全查爆 token）**，按下面取并集后通常 10–18 只：① 观察池里的异动票必查；② 赚钱效应 / 新方向里 ★★ 及以上主题的领导股必查；③ dollar-volume 最大的 5 只必查；④ \|涨跌\| 最大的 5 只必查；⑤ 其余只列表 + 标"未做联网核查"。明示哪些查了、哪些没查。

## 工作流程

1. **确认日期 + 配置**：用户给日期就用，否则脚本默认最近交易日。检查 `assets/stock_pool.yaml` 的 `groups`，要加票提示改 yaml（或飞书）重跑。
2. **取证据包**：在 skill 目录下 `python scripts/generate_report.py`（可加 `--date YYYY-MM-DD --output outputs/us-YYYY-MM-DD.json`）。默认顺带做纳斯达克扫描；要跳过加 `--no-market-scan`；要看板块广度 / 轮动加 `--scan-universe`（重 pass，§3b）。
3. **写大盘 + 观察池明细**：QQQ 四数读；每组一张表列全部成员（含 vs-QQQ + 信号）。空组（`valid_count=0`）整组省略，不写占位行。
4. **板块赚钱效应**（§3 + `references/sector_money_effect.md`）：剔非科技 → 归纳主题 → dollar-volume 占比排序 → 对领先主题成员 `--enrich-tickers` 回补 → ★ 评级 + 位置 + 拥挤 + 领导/弹性股。
5. **板块亏钱效应**（§4）：drops 池剔非科技 → 按主题归纳。
6. **池外新方向挖掘**（§5 + `references/new_direction_mining.md`）：三档 + enrich 确认 + 联网核查（Tavily，`python scripts/web_search.py`）。
7. **异动核查**（§6）：对所有 ★ 票按优先级用 Tavily（`python scripts/web_search.py`，WebSearch 兜底）核查，把查询、来源、关键事实记成可追溯引用。
8. **校验 + 落稿**：每个数字结论能在 evidence 找到出处；每条催化能找到搜索来源；`errors` 失败 ticker 末尾透明披露。产出最终 Markdown。
9. **跨日台账落库**（`references/cross_day_ledger.md`）：日报定稿后 `python scripts/theme_ledger.py context --asof YYYY-MM-DD` 取注册表 + 近期状态 + watchlist；模型写 `outputs/lifecycle_YYYYMMDD.json`（临时主题名 → canonical theme_id 归一 + 六态判定），再 `python scripts/theme_ledger.py record --input outputs/lifecycle_YYYYMMDD.json` 落库。台账让新方向票标注首现 / 连续 N 晚 / 已建议待跟踪，也给主线老化与建议闭环提供依据。
10. **HTML 派生**（用户要求时）：Markdown 定稿后 `python scripts/render_report_html.py --input <report.md> --evidence <evidence.json>`；renderer 默认剥离 YAML frontmatter，只做浏览层、不反向改写正文。

## 数据获取（脚本抓手）

### `scripts/generate_report.py`（主入口）

拉观察池一年日线（含 52周位置 / 20日趋势 / 量比 / vs-QQQ）+ 触发飞书同步 + 触发纳斯达克扫描，组装 evidence JSON。脚本不写 Markdown、不写结论、不联网搜新闻。

```bash
python scripts/generate_report.py                                 # 最近交易日 + 默认 sync + 默认扫描
python scripts/generate_report.py --date 2026-06-18 --output outputs/us-2026-06-18.json
python scripts/generate_report.py --no-sync                       # 跳过飞书同步（离线）
python scripts/generate_report.py --no-market-scan                # 跳过纳斯达克扫描
python scripts/generate_report.py --scan-min-dollar-volume-million 100   # 抬高成交额下限（更聚焦）
python scripts/generate_report.py --scan-universe                 # 额外扫 curated 纳指科技 universe（广度/轮动，重 pass）
# 领先主题成员回补（Pass 2）：只对这些票 + QQQ 取一年历史，算 52周位置/量比/vs-QQQ
python scripts/generate_report.py --enrich-tickers "ALAB,CRDO,AAOI" --output outputs/enrich.json
```

观察池快照字段：`close / prev_close / change_pct / five_day_trend_pct / trend_20d_pct / position_52w / drawdown_from_high_pct / high_52w / low_52w / vol_vs_20d / volume`，QQQ 已知后注入 `vs_qqq_1d / vs_qqq_5d`。

### `scripts/scan_market.py`

纳斯达克当日异动 / 赚钱效应扫描：调 Yahoo 预设 screener → **Nasdaq 交易所过滤**（NMS/NGM/NCM，确定性）→ 每条算 `dollar_volume = close×成交股数` → 按成交额降序 → `is_abnormal` 标 \|涨跌\|≥7%。可单跑：

```bash
python scripts/scan_market.py                                     # 默认 进池≥3% / 异动≥7% / 成交额≥$50M / 每方向60只
python scripts/scan_market.py --min-change 4 --min-dollar-volume-million 100
python scripts/scan_market.py --all-exchanges                     # 调试：关 Nasdaq 过滤
python scripts/scan_market.py --output outputs/nasdaq-scan.json
```

由 `generate_report.py` 默认调用，结果挂在 evidence 的 `market_wide_movers`（键名保留以兼容 HTML 渲染器）。

### `scripts/theme_ledger.py`（跨日台账，确定性、无 LLM）

每晚把主题判定 + 新方向候选沉淀成跨日序列，区分一夜脉冲 vs 多日趋势、给建议闭环。脑 / 手分工同 DMS 的 `theme_lifecycle.py`：脚本提供上下文、校验六态状态机、归一别名、落库；状态判定与主题命名归一由模型完成。完整契约见 `references/cross_day_ledger.md`。

```bash
python scripts/theme_ledger.py context --asof 2026-06-18           # 取注册表 + 近期状态 + watchlist
python scripts/theme_ledger.py record --input outputs/lifecycle_20260618.json   # 校验并落库
```

存储默认 `~/.usmarket-ledger/`（`USMARKET_LEDGER_DIR` 可覆盖），在版本库与 skill_sync 之外，所有 agent 副本共用一份。

### `scripts/sync_from_lark.py`

读飞书表格 → 覆写 `assets/stock_pool.yaml`，由 `generate_report.py` 默认触发，也可手跑（`--dry-run` 只打印）。

### `scripts/render_report_html.py`

最终 Markdown → 自包含 HTML，只做浏览层渲染与 evidence 驱动的轻量图表，不生成正文、不补归因、不改写判断。

```bash
python scripts/render_report_html.py --input reports/us-2026-06-18.md --evidence outputs/us-2026-06-18.json
python scripts/render_report_html.py --input reports/us-2026-06-18.md --theme print
```

### evidence JSON 主要字段

- `type=us_market_watchlist_evidence`、`date` / `generated_at` / `thresholds`
- `indices`：仅 QQQ（含全部派生字段）
- `groups`：每组成员快照 + `summary{valid_count,up_count,down_count,avg_change_pct}`（summary 仅弱参考，正文以个股为主）
- `abnormal_moves.rises/.drops`：观察池内 ±7% 票
- `market_wide_movers`（除非 `--no-market-scan`）：`type=us_nasdaq_movers_evidence`、`scan_date` / `date_aligned`、`market_states`（扫描时的盘口状态集合，正常应为 `["CLOSED"]` 或 `["POST"]`；若含 `REGULAR` 说明在盘中跑、成交额是半日口径，正文须提示"盘中扫描、dollar-volume 为不完整口径"）、`thresholds`、`rises` / `drops`（每项含 `dollar_volume` / `dollar_volume_million` / `is_abnormal` / `market_cap_billion` / `exchange` / `full_exchange_name` / `market_state`）、`errors`
- `universe_scan`（仅 `--scan-universe`）：`type=us_nasdaq_universe_scan`、`date`、`benchmark`(QQQ)、`buckets`（每个含 `valid/up/down/dollar_volume_million/median_change_pct/median_vs_qqq_5d/leaders`，读板块广度与轮动）、`movers`（宇宙内涨 ≥3% 的票，按成交额降序，含 `vs_qqq` / `position_52w` / `vol_vs_20d`，捞安静被买的名字）、`errors`
- `errors`：观察池 ticker 拉取失败清单

依赖：`pip install requests pyyaml`。联网检索由模型在第 6/7 步用 `python scripts/web_search.py`（Tavily，需 `TAVILY_API_KEY`）完成，WebSearch / WebFetch 兜底；检索是原子取数（脚本不下结论），归因判断仍由模型做。

## 输出规范

风格：中立研究笔记；1000–3000 字（票多、新方向多则偏上限）；不写"我"。

**文风默认（项目级硬性要求）：**

- **文风讲人话，减少机械与僵硬。** 像跟懂行的人当面把一件事讲清楚那样写，句子通顺、有逻辑衔接，该解释因果和给判断时把话说透。避免模板腔、翻译腔和套话——别成段堆砌"综上所述""值得注意的是""总体来看"，别把每条都写成生硬的"主语+动词+宾语"公式句，也别为了凑结构把话说断、只丢关键词。
- **同项罗列优先用 list，但每条要说人话。** 同一维度的多个条目（多个主题、多只异动票、多个新方向）拆成 bullet 或编号，一条一项，别塞进一个长段落；但每条用完整通顺的话写，不要退化成"字段A - 字段B - 字段C"式的横杠拼接。结构化对照（主题 × dollar-volume × ★、ticker × 涨跌 × 催化）才用表格。

**判断纪律（移植 DMS 输出纪律）：**

- 证据优先：所有强弱判断回到 dollar-volume / vs-QQQ 超额 / 量比 / 52周位置 / 涨跌幅，但正文只挑最能解释状态的证据，同一自然段最多 2–3 个关键数字，其余留表格。
- 人话先行：每个判断段第一句是自然语言结论，不以连续数字开头。
- 每节只回答一个问题：这节说明"进攻 / 分歧 / 退潮 / 修复 / 拥挤 / 扩散 / 新方向"中的哪一种。
- **高亮定性段落**：每个大节里承担总结作用的段落用 `==...==` 包裹（如"今晚一句话""大盘判断""板块赚钱效应判断""亏钱效应判断""新方向小结"），在 HTML 渲染为浅蓝提示块。不额外新增"本节总结"段落。
- 不写买卖建议：可写"风险传导""持续性待验证""主线确认度""建议纳入观察"，不写"买入/卖出/止损/目标价"。

固定结构（顺序不能变）：

```markdown
# 纳斯达克科技观察池日报 - YYYY-MM-DD（纳指版）

## 今晚一句话
==[大盘状态 + 钱在哪个主题（确认度）+ 我池子覆盖到没有 + 有没有该补的新方向；3–4 句人话，不以连续数字开头]==

## 大盘（QQQ）
[当日 + 5/20 日趋势 + 52周位置 + 量能；2–3 句。==大盘判断== 高亮收尾]

## 板块赚钱效应（纳指科技）
> 数据基础：market_wide_movers.rises（Nasdaq 过滤 + 成交额降序），剔非科技后按业务事实归纳主题。

| 主题 | ★ | dollar-volume 占比 | 位置 | 拥挤度 | 领导股（成交额$M） | 催化逻辑 |
|---|:--:|---:|---|---|---|---|
| ... | ★★★/★★ | XX% | 高位趋势/低位启动 | 高/中/低 | TICKER（$XXXX） | ... |

[每个 ★★/★★★ 主题：领导股 1–3 / 弹性股 1–3，关键证据用人话写。★ 主题只进下面的结论句、不入表。]
==板块赚钱效应判断：[今晚钱主要在哪个主题、是确认主线还是个别巨头拉动、是高位延续还是低位启动；2–3 个关键证据]==

## 板块亏钱效应
[drops 池剔非科技后按主题归纳；==亏钱效应判断== 高亮收尾，并与赚钱主题对照是否内部分歧]

## 观察池个股明细
> `信号` 列读法：当日箭头 + 5日箭头；`＋`=当日跑赢 QQQ，`★`=触及 ±7%（详见各票核查）。常态只标符号，异常才加 ≤6 字。

### {分组}
| Ticker | 收盘 | 当日 | vs QQQ | 5 日 | 52周位置 | 信号 |
|---|---:|---:|---:|---:|---:|:--:|
| ... | ... | ... | +X.XX% | ... | 0.XX | ↑↑＋ |
（每组一段，列全部成员；valid_count=0 的空组整组省略，不写占位行。每票可在行后注"属今晚 X 主题"。）

## 池外新方向（主动挖掘建议）
[按三档（新主题缺口 / 龙头缺口 / 邻接补充）列；每条：主题 + 代表票 + dollar-volume/vs-QQQ/位置证据 + 联网催化（Tavily） + 一句纳入理由。无则写"今晚无池外新方向，赚钱效应都落在已覆盖主题内"。]

## 后续核查
[观察池异动（必收）+ 新方向强候选（建议）+ 观察池里逆 5 日方向且 |当日|≥3% 的票]

---
数据日期：YYYY-MM-DD（美东）｜来源：Yahoo Finance chart + screener + Tavily/WebSearch（催化）
纳斯达克扫描：scan_date=YYYY-MM-DD，date_aligned={true/false}
联网核查：已查 {n} 只 / 未查 {m} 只（列未查清单）
拉取失败：<errors 或"无">
仅供研究记录，不构成投资建议。
```

数据呈现规则：

| 规则 | 为什么 |
|---|---|
| 涨跌幅 / 趋势 / vs-QQQ 2 位小数 | 跨数据源一致 |
| 价格 2 位小数、USD | 美股惯例 |
| 成交额用 `$XXXXM` 或 `$X.XB` | dollar-volume 跨度大，统一量纲好扫读 |
| 52 周位置用 0–1 两位小数 | 一眼看出贴高位还是中低位 |
| 每组一张表、列全部成员 | 用户要个股粒度，不省略 |
| 主题表按 dollar-volume 占比排序 | 钱多的主题在前，符合成交额优先 |
| 异动 / 新方向核查行注明来源（媒体 + 日期） | 区分价格证据与新闻证据 |
| 未查的票标"未做联网核查"、查不到写"未检索到" | 不杜撰、不留空 |

HTML 输出规则：

- 只在用户要求 HTML / 可浏览页面 / Site 派生层 / 本地归档时生成；普通日报只交 Markdown。
- HTML 必须从已定稿 Markdown 渲染，不让 renderer 代写正文，也不作为 Wiki 真相源。
- 带 YAML frontmatter 默认剥离后渲染；`==...==` 渲染为浅蓝提示块。
- HTML 图表只读 evidence 已有的价格 / 成交额 / 位置字段；催化仍以正文联网检索（Tavily/WebSearch）来源为准。

## 示例

### Input

> 复盘下昨晚美股，纳指科技板块今晚钱在哪个方向？我池子覆盖到没有，有没有该补的新方向？

### 执行

```bash
python scripts/generate_report.py --output outputs/us-2026-06-18.json
# 拿到 evidence 后：
#   1) market_wide_movers.rises → 剔非科技(STLD/LEGN/KTOS…) → 归纳主题 → 按 dollar-volume 占比排序
#   2) 领先主题成员回补：python scripts/generate_report.py --enrich-tickers "ALAB,CRDO,…"
#   3) ★ rubric 评级 + 位置 + 拥挤 + 领导/弹性股
#   4) 对比观察池覆盖 → 三档挖新方向 → 对 ★/新方向票用 Tavily(python scripts/web_search.py) 核查催化
# Markdown 定稿后，如用户要求 HTML：
#   python scripts/render_report_html.py --input reports/us-2026-06-18.md --evidence outputs/us-2026-06-18.json
```

### Output（节选）

```markdown
# 纳斯达克科技观察池日报 - 2026-06-18（纳指版）

## 今晚一句话
==纳指偏多、贴近 52 周高位放量上行，今晚的钱高度集中在半导体/存储：MRVL、MU、INTC、SNDK 同步放量，是当晚唯一的 ★★★ 确认主线，而你的 Semi + AI光存 两组已基本覆盖。真正的缺口在 AI 互联——ALAB 一夜 +11.3%、跑赢 QQQ 8.8pp、3.7 倍量创新高，不在池里，建议纳入。==

## 板块赚钱效应（纳指科技）
| 主题 | ★ | 成交额占比 | 位置 | 拥挤度 | 领导股 | 催化逻辑 |
|---|:--:|---:|---|---|---|---|
| 半导体/存储 | ★★★ | ~58% | 高位趋势 | 中 | MU（$73B）、MRVL（$78B） | HBM/存储涨价周期，财报前 positioning |
| AI 互联/retimer | ★★ | ~9% | 低位启动 | 高 | ALAB（$9B） | retimer 在 hyperscaler 起量 |
...
```
