---
name: chstock-usmarket-report
description: 当用户要求生成美股观察池日报、复盘昨晚/昨夜美股、查看我的美股池/美股自选表现、看每只个股的涨跌明细、找昨夜美股异动票（±7%）并查异动原因、回看指定 YYYY-MM-DD 美股交易日、判断纳斯达克大盘走势、扫描全市场当日大涨大跌的中大盘股（市值 ≥ $10B），或要求把美股日报输出为 HTML / 可浏览页面 / AlphaVault Site 派生浏览层时使用此 skill。本 skill 以纳斯达克 100（QQQ）为大盘锚（不引入 SPY/DIA/IWM）；报告由两个证据池组成——**观察池内每只个股逐行明细** + **全市场 ±7% / 市值 ≥ $10B 的中大盘异动扫描**；任何异动票（无论来自观察池还是全市场扫描）都必须通过 WebSearch 检索近期催化/利空后再写入异动段。脚本输出确定性 JSON 证据包，并可在最终 Markdown 定稿后生成派生 HTML；判断、归因、风险提示由模型完成。不用于盘中实时监控、个股深度基本面研究、目标价或买卖建议；不覆盖港股、A 股、加密货币、SPY/DIA/IWM 风格四象限。
---

# 美股观察池日报（纳指版）

## 目标

1. **做什么**：
   - 基于 `assets/stock_pool.yaml`（默认从飞书表格同步）的观察池，拉取 QQQ + 全部成员股的日线，生成**个股逐行明细**的盘后观察笔记；
   - 同步扫描**全市场**当日 |涨跌幅| ≥ 7% 且市值 ≥ $10B 的中大盘股，列出 Top 20 上涨 / Top 20 下跌，并对其中重点票联网核查近期催化；
   - 综合两块产出：大盘 / 观察池明细 / 观察池 ±7% 异动 / 全市场 ±7% 异动扫描（含联网核查）/ 后续核查。
2. **不做什么**：不引入 SPY/DIA/IWM 与风格四象限；不在脚本里调 LLM 或写 Markdown；不替模型下结论；不给目标价/买卖建议；不杜撰未联网验证的异动原因；不覆盖市值 < $10B 的小盘股扫描（噪声太大）。
3. **给谁用**：面向关注纳指生态的自选投资者或研究员，既想看自己池子里的票，也想知道市场里有谁在大动。

## 适用边界

- 默认取**最近一个已结束**的美股交易日；用户给 `YYYY-MM-DD` 时取该日或之前最近的可用交易日。
- 时区：Yahoo Finance 的 `date` 是美东交易日；"昨晚美股" = 美东上一个收盘日（中国日历差 1 天）。
- 只含**常规交易时段**收盘价，不含盘前/盘后。
- 货币 USD，不做汇率换算（市值阈值 $10B = 100 亿美元）。
- **全市场扫描只覆盖最近收盘日**（Yahoo 预设 screener 不支持历史回溯）。若 `--date` 指向更早交易日，evidence 里 `market_wide_movers.date_aligned=false`，此时报告中全市场段应写"全市场扫描仅支持最近收盘日，本次回看不展示"或跳过该段。
- 异动原因来自 WebSearch 的公开网络结果，必须**带出处与日期**，无法验证就写"未检索到明确催化"。

## 领域方法论

**核心是个股，不是组**。组只是个股的归类标签，便于读者按板块浏览；不要在组层面做"扩散 vs 集中"这种聚合判断。

### 1. 大盘锚（仅 QQQ）

QQQ 用三个数读：

- **当日 change_pct**：纳指今天偏多还是偏空。
- **5 日 trend**：中期方向。与当日同向才能下"趋势"结论，不同向只写"短期 vs 中期分歧"。
- **量能（volume）**：与近 5 日均值大致比较，量能放大方向更可信，缩量方向意义打折。

不与个股做"扩散度"对比，不做风格判断。

### 2. 个股逐行框架（每只都写）

每只成员股至少给两类数据 + 一个**信号标记**（不是整句话，是 `信号` 列里的紧凑符号）：

| 维度 | 怎么读 |
|---|---|
| 当日 `change_pct` | 今天的方向与幅度 |
| `five_day_trend_pct` | 短期趋势是同向加速、反向修正、还是高位回吐 |
| 同向 / 反向 | 单日与 5 日同向（趋势延续）vs 反向（短期反转或回吐） |
| 是否触及 ±7% | 是 → 进异动段并必须联网查原因 |

**`信号` 列图例（紧凑符号优先，禁止把同一句话铺满整列）：**

明细表里多数票都是"同向上行"，整列写满"单日 + 5 日同向上行，延续短期趋势"是纯噪音、会淹没真正的异常。所以 `信号` 列只标符号，让逆势 / 回吐 / 异动一眼跳出来。符号读法：第一个箭头是**当日**方向，第二个箭头是 **5 日**方向。

| 符号 | 含义 | 触发条件 |
|---|---|---|
| `↑↑` | 当日与 5 日同向上行（趋势内·常态） | 当日 > 0 且 5 日 > 0 |
| `↓↓` | 当日与 5 日同向走弱 | 当日 < 0 且 5 日 < 0 |
| `↑↓` | 当日逆 5 日上行（逆势反弹·待观察） | 当日 > 0 且 5 日 < 0 |
| `↓↑` | 当日逆 5 日回落（回吐 / 转弱） | 当日 < 0 且 5 日 > 0 |
| `★` | 触及 ±7%，详见异动段（与方向符号并写，如 `★↑↑`） | \|当日\| ≥ 7% |

只有当符号不足以说明、且确有研究价值时，才在符号后补 **≤6 字**（如 `↑↓ 待转正`、`↓↑ 高位回吐`、`↑↑ 中期未转强`）。**禁止**把整句模板写满整列，也**禁止**杜撰原因。

### 3. 异动定义（±7%，硬阈值，两个证据池都用同一阈值）

| 证据池 | 来源 | 阈值 | 桶字段 |
|---|---|---|---|
| 观察池异动 | `assets/stock_pool.yaml` 的成员股 | \|change_pct\| ≥ 7% | `abnormal_moves.rises` / `.drops` |
| 全市场扫描 | Yahoo Finance day_gainers / day_losers | \|change_pct\| ≥ 7% **且**市值 ≥ $10B | `market_wide_movers.rises` / `.drops`（各 Top 20） |

两个证据池都使用 ±7% 硬阈值——好处是用户对"异动"的语义在两段保持一致。市值阈值仅作用于全市场扫描，过滤掉微盘股的高百分比噪声。

所有进入这两个池的票都**必须**经过下一节联网核查后再写入报告；查不到的明示"未检索到明确催化"。

### 4. 异动原因联网核查（WebSearch / WebFetch）

对每一只进入 `rises` / `drops` 的票（观察池池 + 全市场扫描池合并去重后的清单），按顺序执行：

1. **查询 1（事件类）**：`{ticker} stock news {YYYY-MM-DD}` 或 `{ticker} {YYYY-MM-DD} catalyst` —— 找当日/前一日的具体新闻、公告、财报、监管事件。
2. **查询 2（财报/指引）**：若临近财报窗口，加查 `{ticker} earnings guidance {YYYY-MM-DD}`。
3. **查询 3（中文补充，可选）**：`{中文名} 异动 原因 {日期}` —— 中文财经媒体常对热门 ADR / 大票有快讯解释。
4. 综合 1–3 的结果，提炼一句**带来源**的解释，例如："据 Reuters {YYYY-MM-DD} 报道，公司宣布……"。
5. 找不到合适证据时写 "未检索到当日明确催化（已搜索 {ticker} news / earnings / 中文异动），暂列为待核查"。

**禁止**：基于价格方向反推事件（如"跌 8% 大概率是财报 miss"）；编造分析师评级；引用未真实出现在搜索结果里的标题。

### 5. 联网核查的优先级（避免 40 只票全查爆 token）

全市场扫描 rises/drops 合在一起最多 40 只，全员 WebSearch 太贵。模型自行裁剪，按下面优先级取并集（去重后通常 10–18 只）：

1. **观察池里的异动票**：必查（这是用户最关心的资产）。
2. **与观察池主题强相关的全市场异动票**：必查。判断关键词来自 stock_pool.yaml 的分组名 + 备注列，例如：
   - 分组 `AI光存` / 备注含 "光" → 优先 CRDO / LITE / COHR / FN / AAOI 等光通信票
   - 分组 `Semi` → 优先 QCOM / AVGO / SWKS / TSM / DELL（AI 服务器） 等
   - 分组 `AI Cloud` / `IGV` → 优先 IONQ / QBTS / NTAP / HPE 等 AI 基建 / 软件
3. **市值最大的 5 只异动票**：必查（大市值异动一般有清晰催化，性价比高）。
4. **|change_pct| 最大的 5 只异动票**：必查（极端波动通常意味着重大事件）。
5. 剩余票只列表格 + "未做联网核查"，不强行查。

工作产物中应明示哪些查了、哪些没查，便于用户复核。

## 工作流程

1. **确认日期 + 配置**
   - 用户给日期就用；否则脚本默认最近交易日。
   - 检查 `assets/stock_pool.yaml` 的 `groups`、`thresholds`；要加票/分组时提示改 yaml 重跑。
   - 产出：本次观察池范围 + 阈值（默认 ±7%）。

2. **取证据包**
   - 在 skill 目录下 `python scripts/generate_report.py`（可加 `--date YYYY-MM-DD --output outputs/us-YYYY-MM-DD.json`）。
   - 默认会顺带做全市场扫描；要跳过加 `--no-market-scan`。
   - 脚本输出：
     - `indices`（仅 QQQ）/ `groups`（每组含全部成员快照）/ `abnormal_moves`（观察池 `rises` / `drops` 两桶）
     - `market_wide_movers`：`rises` / `drops`（各 Top 20，含 `market_cap_billion` / `name` / `change_pct`）；`scan_date` 与 `date_aligned` 字段标明扫描窗口
     - `errors`（拉取失败的 ticker）

3. **逐组逐股写明细表**
   - 每组一张表，列：ticker、close、change_pct、5 日趋势、信号（按§2 图例标符号，**不写整句**）。
   - **空组省略**：若某分组在 evidence 的 `groups` 中 `summary.valid_count = 0`（成员为空 / 飞书同步后该组无数据），整组跳过——不要输出占位表，也不要写 `| - | - | … | 该分组为空 |` 这种占位行；把"X 组本日无成员"并入文末「数据来源与拉取失败说明」一行即可。
   - 不要在组层面写"扩散 / 集中"等聚合判断。
   - 若组内有进入异动桶的票，在该行 `信号` 列并上 `★`（如 `★↑↑`），不再单独加"★ 异动"文字。

4. **对异动票联网查原因（§4 + §5 流程）**
   - 把观察池 `abnormal_moves` 与全市场 `market_wide_movers` 的 rises/drops 合并去重；
   - 按 §5 优先级裁剪到 10–18 只必查清单；
   - 每只异动票各做一次 WebSearch（必要时 WebFetch 打开来源页面读 1–2 段）；
   - 把搜索查询、来源链接/媒体名、关键事实记成可追溯的引用；
   - 找不到的明示"未检索到明确催化"；
   - 未列入必查清单的票，仅在表格里列出 + 标注"未做联网核查"。

5. **校验 + 落稿**
   - 每个数字结论 → 都能在 evidence JSON 找到出处。
   - 每条异动归因 → 都能找到搜索来源；未找到的不强行解释。
   - `errors` 失败 ticker 必须在末尾透明披露。
   - 产出：最终 Markdown。

6. **HTML 派生输出（用户要求 HTML / Site 时才做）**
   - Markdown 是日报真相源；HTML 只做浏览层，不反向改写正文判断。
   - 在最终 Markdown 定稿后运行 `python scripts/render_report_html.py --input <report.md> --evidence <evidence.json> --output <report.html>`。
   - renderer 默认剥离 YAML frontmatter，避免 `report_tag`、`primary_sources` 等机器元数据在浏览页可见；只有 frontmatter 本身就是正文时才加 `--keep-frontmatter`。
   - 产出：自包含 HTML，含 QQQ / 观察池分组 / 异动扫描的轻量图表；如果没有 evidence JSON，也能退化为纯 Markdown HTML。

## 数据获取（脚本抓手）

三个脚本，各做一件原子事：

### `scripts/sync_from_lark.py`
读飞书表格 → 覆写 `assets/stock_pool.yaml`。由 `generate_report.py` 默认调用，也可手跑：

```bash
python scripts/sync_from_lark.py                # 真正写文件
python scripts/sync_from_lark.py --dry-run      # 只打印解析结果
```

### `scripts/generate_report.py`（主入口）
拉观察池日线 + 触发同步 + 触发全市场扫描，组装一个 evidence JSON。脚本不写 Markdown、不写结论、不联网搜新闻。

```bash
python scripts/generate_report.py                                          # 最近交易日 + 默认 sync + 默认 market scan
python scripts/generate_report.py --date 2026-05-22                        # 指定日期
python scripts/generate_report.py --date 2026-05-22 --output outputs/us-2026-05-22.json
python scripts/generate_report.py --no-sync                                # 跳过飞书同步（离线）
python scripts/generate_report.py --no-market-scan                         # 跳过全市场扫描
python scripts/generate_report.py --scan-limit 30 --scan-min-cap-billion 5 # 调全市场扫描的口径
```

### `scripts/scan_market.py`
独立的全市场异动扫描，调 Yahoo Finance 预设 screener。可单跑：

```bash
python scripts/scan_market.py                                       # 默认 ±7% / $10B / 每方向 20 只
python scripts/scan_market.py --min-change 5 --min-cap-billion 50   # 更宽涨幅 + 只看 ≥$50B 巨头
python scripts/scan_market.py --output outputs/market-scan.json
```

### `scripts/render_report_html.py`
最终 Markdown → 自包含 HTML。脚本只负责浏览层渲染与 evidence 驱动的小图表，不生成正文、不补充归因、不改写投资判断。

```bash
python scripts/render_report_html.py --input reports/us-2026-05-22.md --evidence outputs/us-2026-05-22.json
python scripts/render_report_html.py --input reports/us-2026-05-22.md --evidence outputs/us-2026-05-22.json --output reports/us-2026-05-22.html
python scripts/render_report_html.py --input reports/us-2026-05-22.md --theme print
```

返回 JSON 摘要包含 `input / output / evidence / data_date / frontmatter_stripped / charts`。依赖来自仓库级 `shared/html_report`，同步后会打包到 `scripts/_shared/html_report/`。

### evidence JSON 返回字段

`generate_report.py` 输出：

- `type` = `us_market_watchlist_evidence`
- `date` / `generated_at` / `thresholds`（含 `drop` / `rise`） / `sync_note`（如有）
- `indices`：仅 QQQ
- `groups`：`[{name, stocks: [{ticker, snapshot:{close, prev_close, change_pct, five_day_trend_pct, volume}}], summary:{valid_count, up_count, down_count, avg_change_pct}}]`（`summary` 仅作弱参考，正文以个股为主）
- `abnormal_moves.rises` / `.drops`：每项含 `ticker / groups / change_pct / close / prev_close / five_day_trend_pct / volume`
- `market_wide_movers`（除非 `--no-market-scan`）：
  - `scan_date` / `date_aligned`（与 evidence.date 一致才为 true）
  - `thresholds`：`min_change_pct / min_market_cap_usd / min_market_cap_billion / limit_per_side`
  - `screener_raw_counts`：`day_gainers` / `day_losers` 原始返回条数（裁剪前）
  - `rises` / `drops`：Top N（默认 20）；每项含 `ticker / name / change_pct / close / prev_close / market_cap / market_cap_billion / volume / exchange / trade_date`
  - `errors`：screener 拉取失败原因
- `errors`：观察池 ticker 拉取失败清单

依赖：`pip install requests pyyaml`。联网检索由模型在第 4 步用 WebSearch / WebFetch 工具完成，不在脚本内。

## 输出规范

风格：中立研究笔记；800–2500 字（票多、全市场扫描有命中则偏上限）；不写"我"。

**文风默认（项目级硬性要求）：**

- **文风讲人话，减少机械与僵硬。** 像跟懂行的人当面把一件事讲清楚那样写，句子通顺、有逻辑衔接，该解释因果和给判断时把话说透。避免模板腔、翻译腔和套话——别成段堆砌"综上所述""值得注意的是""总体来看"，别把每条都写成生硬的"主语+动词+宾语"公式句，也别为了凑结构把话说断、只丢关键词。
- **同项罗列优先用 list，但每条要说人话。** 同一维度的多个条目（多个信号、多只异动票、多项观察点）拆成 bullet 或编号，一条一项，别塞进一个长段落；但每条用完整通顺的话写，不要退化成"字段A - 字段B - 字段C"式的横杠拼接。结构化对照（指标 × 数值、ticker × 涨跌 × 原因）才用表格。

固定结构（顺序不能变）：

```markdown
# 美股观察池日报 - YYYY-MM-DD（纳指版）

## 大盘（QQQ）
[当日 change + 5 日趋势 + 量能；2–3 句]

## 观察池个股明细

> `信号` 列读法：第一个箭头=当日、第二个箭头=5 日；`↑↑` 同向上行 / `↓↓` 同向走弱 / `↑↓` 逆势反弹 / `↓↑` 回吐转弱 / `★` 触及 ±7%（详见§2）。常态只标符号，异常才加 ≤6 字。

### {分组1}
| Ticker | 收盘 | 当日 | 5 日趋势 | 信号 |
|---|---:|---:|---:|:--:|
| ... | ... | ... | ... | ↑↑ |
（每组一段，列出该组全部成员；evidence 中 `valid_count=0` 的空组整组省略，不写占位行）

## 异动扫描（±7%）

合并两个证据池：**观察池** + **全市场（市值 ≥ $10B）**。观察池放最前，便于一眼看到"我的资产里有没有事"；全市场放后面，作为板块/主题信号源。

> scan_date：YYYY-MM-DD（若与本日报日期不一致，须在此说明：例如"本日报回看 X 日，全市场扫描仅支持最近收盘日，本段为 Y 日数据"或整段跳过全市场子节。）

**观察池**（abnormal_moves.rises / .drops）

- 有异动时逐条列出：
  - **{TICKER}**（{分组}）：当日 ±X.XX%，5 日 ±X.XX%。
    - 联网核查：{来源} {日期} —— {一句话事实}。
    - 待核查方向：{若未查到，写"未检索到明确催化，待核查"}。
- 无异动时**只写一行**："本日观察池内无单日涨跌幅触及 ±7% 的个股（最接近的 X / Y 未达，不进入核查流程）。"

**全市场 Top N**（market_wide_movers.rises / .drops，市值 ≥ $10B）

按"与观察池主题相关性"排序，不要把黄金 / 贵金属矿 / 航空 / 纯金融等非 AI 主线的票和核心票混在一张表里平铺（否则半张表都是"未做联网核查"的噪音）：

1. **与观察池分组主题相关、或已联网核查的票排在前**，逐行给核查结论；
2. **明显非主线、且未联网核查的尾部票**（黄金/白银矿、航空、纯金融 beta 等），折叠成一行汇总，不逐行铺满：
   `> 另有 N 只非主线异动（黄金/航空/金融等）：TICKER1 +X.XX%、TICKER2 +Y.YY% …，仅价格信号、未做联网核查。`

上涨（共扫到 {M} 只；主线相关 {k} 只逐行列出，其余按上面规则折叠）：

| Ticker | 名称 | 涨幅 | 市值 | 联网核查 |
|---|---|---:|---:|---|
| ... | ... | +X.XX% | $XXB | {一句来源} |

下跌（共扫到 {M} 只）：

（同上格式。rises + drops 合计 ≤ 5 只时可合并成一张表；非主线尾部同样可折叠。）

## 与观察池主题的交叉
[全市场异动票中，与你的分组主题相关的票（按§5 优先级 2），单独列一节，提示是否值得加入观察池]

## 后续核查
[收录条件：观察池异动（必收）+ 全市场扫描里与主题相关的票（建议）+ 观察池里单日逆 5 日方向且 |当日| ≥ 3% 的票]

---
数据日期：YYYY-MM-DD（美东）｜来源：Yahoo Finance chart + screener API + WebSearch（异动原因）
全市场扫描：scan_date=YYYY-MM-DD，date_aligned={true/false}
拉取失败：<errors 或"无">
仅供研究记录，不构成投资建议。
```

数据呈现规则：

| 规则 | 为什么 |
|---|---|
| 涨跌幅 / 趋势 2 位小数 | 跨数据源拼装一致性 |
| 价格 2 位小数、USD | 美股惯例 |
| 市值用 `$XXB` 形式（保留 1 位小数） | 全市场扫描的票市值跨度大（$10B–$3T），$XXB 最易扫读 |
| 每组一张表，列出**全部**成员 | 用户要个股粒度，不能省略 |
| 全市场扫描表完整列出 Top N | 即使没查原因也要让用户看到完整名单 |
| 联网核查行注明来源（媒体 + 日期） | 区分价格证据与新闻证据 |
| 未查的票必须标"未做联网核查" | 让用户知道哪些是表格事实、哪些有故事 |
| 未查到原因要写"未检索到" | 不杜撰，不留空 |

HTML 输出规则：

- 只在用户要求 HTML、可浏览页面、Site 派生层、或需要本地 HTML 归档时生成；普通日报只交 Markdown。
- HTML 必须从已经定稿的 Markdown 渲染，不能让 renderer 代写正文，也不能把 HTML 作为 Wiki 真相源。
- 若输入 Markdown 带 YAML frontmatter，默认剥离后再渲染；这样浏览层不会露出机器字段，同时不影响 Wiki 侧保留 frontmatter。
- HTML 图表只读 evidence JSON 中已经存在的价格/市值字段；异动原因仍以正文里的 WebSearch/WebFetch 来源为准。

## 示例

### Input

> 复盘下昨晚美股，看看我池子里每只票的涨跌，全市场有没有 ±7% 异动？

### 执行

```bash
python scripts/generate_report.py
# 拿到 evidence 后：
#   1) 观察池 abnormal_moves.rises / .drops → 必查（通常为空）
#   2) market_wide_movers.rises / .drops → 按§5 优先级裁剪 → 必查清单 WebSearch
# Markdown 定稿后，如用户要求 HTML：
#   python scripts/render_report_html.py --input reports/us-2026-05-22.md --evidence outputs/us-2026-05-22.json
```

### Output（节选）

```markdown
# 美股观察池日报 - 2026-05-22（纳指版）

## 大盘（QQQ）
QQQ +0.42%，5 日 +1.65%，量能 32.99M 接近近期均值。单日与 5 日同向上行，纳指偏多结构延续，动能温和。

## 观察池个股明细

### 🚀 Mag7
| Ticker | 收盘 | 当日 | 5 日 | 信号 |
|---|---:|---:|---:|:--:|
| NVDA | 215.33 | -1.90% | -3.14% | ↓↓ |
| TSLA | 411.15 | +1.16% | -3.65% | ↑↓ 待转正 |
| ...  | ...    | ...    | ...    | … |
（其它分组同上；空组直接省略，不写占位行）

## 异动扫描（±7%）

> scan_date：2026-05-22，与本日报对齐（date_aligned=true）。

**观察池**：本日观察池内无单日涨跌幅触及 ±7% 的个股（最接近的 FIG +5.19% / SNOW +4.02% / APLD -4.48% / SNDK -4.12% 均未达，不进入核查流程）。

**全市场 Top 5 上涨**（共扫到 20 只，市值 ≥ $10B）：

| Ticker | 名称 | 涨幅 | 市值 | 联网核查 |
|---|---|---:|---:|---|
| DELL | Dell Technologies | +16.77% | $191.8B | {来源 + 日期} —— AI Factory 在手订单 $43B，分析师上调目标价。 |
| HPQ  | HP Inc.           | +15.25% | $23.2B  | {来源} —— 财报前 positioning + Lenovo 强业绩外溢。 |
| QBTS | D-Wave Quantum    | +14.22% | $10.9B  | {来源} —— $2B 联邦量子 grants 计划，TD Cowen 列为 top 3 受益者。 |
| CRDO | Credo Technology  | +12.94% | $40.3B  | {来源} —— **★AI光存** 光 DSP/AECs 在 hyperscaler 起量。 |
| NTAP | NetApp            | +12.44% | $27.6B  | {来源} —— **★AI光存** Google Cloud 合作扩展。 |

**全市场下跌**（共扫到 5 只）：

| Ticker | 名称 | 跌幅 | 市值 | 联网核查 |
|---|---|---:|---:|---|
| FUTU | Futu Holdings    | -27.53% | $12.6B | {来源} —— CSRC 监管处罚 RMB 1.85B（≈ TTM 净利润 16%），针对境外证券跨境引流。 |
| ...  | ...               | ...     | ...    | ... |

## 与观察池主题的交叉
- **CRDO / NTAP / HPE**：与 AI光存（光 DSP / 存储）+ AI Cloud（基础设施）高度相关，但均不在当前观察池，建议考虑加入。
- **QCOM / SWKS**：与 Semi 分组直接相关，QCOM 单日 +11.60% / $251B 市值，是当日 Semi 板块最大量级异动。

## 后续核查
- NVDA：观察池中 Mag7 单日 + 5 日同向走弱最明显，需独立核查。
- SNDK：5 日 +10.93% 后单日 -4.12%，高位回吐特征。
- CRDO / NTAP：全市场扫描里与 AI光存 直接同主题且强催化，建议下钻判断要否纳入观察池。

---
数据日期：2026-05-22（美东）｜来源：Yahoo Finance chart + screener + WebSearch
全市场扫描：scan_date=2026-05-22，date_aligned=true
拉取失败：无
仅供研究记录，不构成投资建议。
```
