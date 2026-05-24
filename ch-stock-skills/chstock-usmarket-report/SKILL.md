---
name: chstock-usmarket-report
description: 当用户要求生成美股观察池日报、复盘昨晚/昨夜美股、查看我的美股池/美股自选表现、看每只个股的涨跌明细、找昨夜美股异动票（±7%）并查异动原因、回看指定 YYYY-MM-DD 美股交易日、判断纳斯达克大盘走势时使用此 skill。本 skill 只以纳斯达克 100（QQQ）作为大盘锚，不引入 SPY/DIA/IWM；报告**重心在个股逐行明细**而非分组聚合判断；任何单日涨跌幅 ≥ +7% 或 ≤ -7% 的票必须通过 WebSearch 检索近期催化/利空后再写入异动段。脚本只输出确定性 JSON 证据包，判断、归因、风险提示由模型完成。不用于盘中实时监控、个股深度基本面研究、目标价或买卖建议；不覆盖港股、A 股、加密货币、SPY/DIA/IWM 风格四象限。
---

# 美股观察池日报（纳指版）

## 目标

1. **做什么**：基于 `assets/stock_pool.yaml` 的观察池，拉取 QQQ 与全部成员股的日线，生成一份**以个股为单位**的盘后观察笔记 —— 大盘（仅 QQQ）/ 各组个股明细 / ±7% 异动（含联网查到的近期催化）/ 后续核查。
2. **不做什么**：不引入 SPY/DIA/IWM 与风格四象限；不在脚本里调 LLM 或写 Markdown；不替模型下结论；不给目标价/买卖建议；不杜撰未联网验证的异动原因。
3. **给谁用**：面向只关注纳指生态、希望每天先看个股、再决定是否单独深挖的自选投资者或研究员。

## 适用边界

- 默认取**最近一个已结束**的美股交易日；用户给 `YYYY-MM-DD` 时取该日或之前最近的可用交易日。
- 时区：Yahoo Finance 的 `date` 是美东交易日；"昨晚美股" = 美东上一个收盘日（中国日历差 1 天）。
- 只含**常规交易时段**收盘价，不含盘前/盘后。
- 货币 USD，不做汇率换算。
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

每只成员股至少给两类数据 + 一句性质判断：

| 维度 | 怎么读 |
|---|---|
| 当日 `change_pct` | 今天的方向与幅度 |
| `five_day_trend_pct` | 短期趋势是同向加速、反向修正、还是高位回吐 |
| 同向 / 反向 | 单日与 5 日同向（趋势延续）vs 反向（短期反转或回吐） |
| 是否触及 ±7% | 是 → 进异动段并必须联网查原因 |

一句话性质判断模板（任选其一，**禁止杜撰原因**）：

- "单日 + 5 日同向上行，延续短期趋势"
- "单日逆 5 日方向，疑似回吐 / 反弹（待核查）"
- "单日小幅波动，中期方向不变"
- "5 日累计 +X% 后单日 -Y%，高位回吐"
- "触及 ±7%，已在异动段联网核查"

### 3. 异动定义（±7%，硬阈值）

只有满足 |`change_pct`| ≥ 7% 的票才算异动。

| 桶 | 触发条件 |
|---|---|
| `rises` | `change_pct` ≥ +7% |
| `drops` | `change_pct` ≤ -7% |

异动票**必须**经过下一节的联网核查流程后再写入报告；未查到证据的，必须明示"未检索到明确催化"。

### 4. 异动原因联网核查（WebSearch / WebFetch）

对每一只进入 `rises` / `drops` 的票，按顺序执行：

1. **查询 1（事件类）**：`{ticker} stock news {YYYY-MM-DD}` 或 `{ticker} {YYYY-MM-DD} catalyst` —— 找当日/前一日的具体新闻、公告、财报、监管事件。
2. **查询 2（财报/指引）**：若临近财报窗口，加查 `{ticker} earnings guidance {YYYY-MM-DD}`。
3. **查询 3（中文补充，可选）**：`{中文名} 异动 原因 {日期}` —— 中文财经媒体常对热门 ADR / 大票有快讯解释。
4. 综合 1–3 的结果，提炼一句**带来源**的解释，例如："据 Reuters {YYYY-MM-DD} 报道，公司宣布……"。
5. 找不到合适证据时写 "未检索到当日明确催化（已搜索 {ticker} news / earnings / 中文异动），暂列为待核查"。

**禁止**：基于价格方向反推事件（如"跌 8% 大概率是财报 miss"）；编造分析师评级；引用未真实出现在搜索结果里的标题。

## 工作流程

1. **确认日期 + 配置**
   - 用户给日期就用；否则脚本默认最近交易日。
   - 检查 `assets/stock_pool.yaml` 的 `groups`、`thresholds`；要加票/分组时提示改 yaml 重跑。
   - 产出：本次观察池范围 + 阈值（默认 ±7%）。

2. **取证据包**
   - 在 skill 目录下 `python scripts/generate_report.py`（可加 `--date YYYY-MM-DD --output outputs/us-YYYY-MM-DD.json`）。
   - 脚本输出：`indices`（仅 QQQ）/ `groups`（每组含全部成员快照）/ `abnormal_moves`（`rises` / `drops` 两桶）/ `errors`。

3. **逐组逐股写明细表**
   - 每组一张表，列：ticker、close、change_pct、5 日趋势、性质判断（按§2 模板任选其一）。
   - 不要在组层面写"扩散 / 集中"等聚合判断。
   - 若组内有进入异动桶的票，明细表里在该行末尾打 `★ 异动` 标记。

4. **对异动票联网查原因（§4 流程）**
   - 每只异动票各做一次 WebSearch（必要时 WebFetch 打开来源页面读 1–2 段）。
   - 把搜索查询、来源链接/媒体名、关键事实记成可追溯的引用。
   - 找不到的明示"未检索到明确催化"。

5. **校验 + 落稿**
   - 每个数字结论 → 都能在 evidence JSON 找到出处。
   - 每条异动归因 → 都能找到搜索来源；未找到的不强行解释。
   - `errors` 失败 ticker 必须在末尾透明披露。
   - 产出：最终 Markdown。

## 数据获取（脚本抓手）

脚本：`scripts/generate_report.py`

职责：拉取 `stock_pool.yaml` 中的 QQQ 与全部成员股日线（Yahoo Finance chart），生成指数快照、各组成员快照与统计、`rises` / `drops` 两个异动桶（带 `groups` 字段）、`errors` 失败清单。脚本不写 Markdown、不写结论、不联网搜新闻。

调用：

```bash
python scripts/generate_report.py                                          # 最近交易日
python scripts/generate_report.py --date 2026-05-22                        # 指定日期
python scripts/generate_report.py --date 2026-05-22 --output outputs/us-2026-05-22.json
python scripts/generate_report.py --config assets/stock_pool.yaml
```

返回字段：

- `type` = `us_market_watchlist_evidence`
- `date` / `generated_at` / `thresholds`（含 `drop` / `rise`）
- `indices`：仅 QQQ
- `groups`：`[{name, stocks: [{ticker, snapshot:{close, prev_close, change_pct, five_day_trend_pct, volume}}], summary:{valid_count, up_count, down_count, avg_change_pct}}]`（`summary` 仅作弱参考，正文以个股为主）
- `abnormal_moves.rises` / `abnormal_moves.drops`：每项含 `ticker / groups / change_pct / close / prev_close / five_day_trend_pct / volume`
- `errors`

依赖：`pip install requests pyyaml`。
联网检索由模型在第 4 步用 WebSearch / WebFetch 工具完成，不在脚本内。

## 输出规范

风格：中立研究笔记；500–1500 字（个股越多越偏上限）；不写"我"。

固定结构：

```markdown
# 美股观察池日报 - YYYY-MM-DD（纳指版）

## 大盘（QQQ）
[当日 change + 5 日趋势 + 量能；2–3 句]

## 观察池个股明细

### {分组1}
| Ticker | 收盘 | 当日 | 5 日趋势 | 性质判断 |
|---|---:|---:|---:|---|
| ... | ... | ... | ... | ... |

### {分组2}
（同上格式）

…（重复每个分组）

## 异动扫描（±7%）
- **{TICKER}**（{分组}）：当日 {±X.XX%}，5 日 {±X.XX%}。
  - 联网核查：{Search 来源/媒体} {日期} —— {一句话事实}。
  - 待核查方向：{若证据不充分，写"未检索到明确催化，待核查"}。

（无异动时写："本日观察池内无单日涨跌幅触及 ±7% 的个股。"）

## 后续核查
[按§3 异动票 + §2 中"单日逆 5 日方向"且 |change|≥3% 的票收录]

---
数据日期：YYYY-MM-DD（美东）｜来源：Yahoo Finance chart 接口 + WebSearch（异动原因）
拉取失败：<errors 或"无">
仅供研究记录，不构成投资建议。
```

数据呈现规则：

| 规则 | 为什么 |
|---|---|
| 涨跌幅 / 趋势 2 位小数 | 跨数据源拼装一致性 |
| 价格 2 位小数、USD | 美股惯例 |
| 每组一张表，列出**全部**成员 | 用户要个股粒度，不能省略 |
| 异动行打 `★ 异动` | 便于回到异动段查原因 |
| 异动归因必须带来源 | 区分价格证据与新闻证据 |
| 未查到原因要写"未检索到" | 不杜撰，不留空 |

## 示例

### Input

> 复盘下昨晚美股，看看我池子里每只票的涨跌，有没有 ±7% 异动。

### 执行

```bash
python scripts/generate_report.py
# 拿到 abnormal_moves.rises / drops 后，对每只异动 ticker 用 WebSearch 查原因
```

### Output（节选）

```markdown
# 美股观察池日报 - 2026-05-22（纳指版）

## 大盘（QQQ）
QQQ 当日 +0.42%，5 日 +1.65%，量能 32.99M 接近近期均值。单日与 5 日同向上行，纳指偏多结构延续，动能温和。

## 观察池个股明细

### Mag7
| Ticker | 收盘 | 当日 | 5 日趋势 | 性质判断 |
|---|---:|---:|---:|---|
| NVDA | 215.33 | -1.90% | -3.14% | 单日 + 5 日同向走弱 |
| TSLA | 426.01 | +1.95% | +3.91% | 单日 + 5 日同向上行 |
| ...  | ...    | ...    | ...    | ... |

### AI 硬件
（同上）

## 异动扫描（±7%）
本日观察池内无单日涨跌幅触及 ±7% 的个股。距离阈值最近：SNDK -4.12%、SNOW +4.02%，未达 ±7%，不列入异动核查流程。

## 后续核查
- NVDA：Mag7 中单日 + 5 日同向走弱最明显。
- SNDK：5 日 +10.93% 后单日 -4.12%，高位回吐特征。

---
数据日期：2026-05-22（美东）｜来源：Yahoo Finance chart 接口 + WebSearch（异动原因）
拉取失败：无
仅供研究记录，不构成投资建议。
```
