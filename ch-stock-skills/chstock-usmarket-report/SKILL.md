---
name: chstock-usmarket-report
description: 当用户要求复盘昨晚美股、查看纳斯达克科技观察池表现、判断纳指科技赚钱效应与资金方向、识别异动票并查催化、挖掘池外新方向，或把美股日报导出 HTML/网页时使用。脚本只输出确定性证据（Nasdaq 过滤、成交额计算、位置/量比/vs-QQQ）；主题归属、主线评级、归因与纳入建议由模型完成。不提供买卖建议。
---

# 纳斯达克科技观察池日报

## 目标

1. **做什么**：
   - 观察池逐股明细：基于 `assets/stock_pool.yaml` 拉 QQQ + 成员股一年日线，逐只给当日 / 5 日 / 20 日 / 52 周位置 / vs-QQQ 超额 + 信号标记。
   - 纳指科技板块赚钱效应：扫纳斯达克当日强势股，按 dollar-volume 找出钱在哪个主题、够不够格算主线。
   - 池外新方向主动挖掘：把有赚钱效应但观察池未覆盖的新方向挖出来并给纳入建议。
2. **不做什么**：不引入 SPY/DIA/IWM 与风格四象限；不覆盖非科技板块；不在脚本里调 LLM 或写 Markdown；不替模型下结论；不给目标价 / 买卖建议；不杜撰未联网验证的催化。
3. **给谁用**：聚焦纳指科技的自选投资者 / 研究员——既看自己池子里的票，也要知道纳指科技里钱在往哪个方向走、有没有该补的新方向。

## 适用边界

- 默认取最近一个已结束的美股交易日；用户给 `YYYY-MM-DD` 时取该日或之前最近的可用交易日。
- 时区：Yahoo 的 `date` 是美东交易日；"昨晚美股" = 美东上一个收盘日。只含常规交易时段收盘价，不含盘前 / 盘后。
- 货币 USD，不做汇率换算；成交额（dollar-volume）= 收盘价 × 当日成交股数。
- **脑 / 手边界**：脚本只做确定性的事——Nasdaq 交易所过滤、成交额计算、52周位置 / 量比 / vs-QQQ 派生、按成交额排序。"是不是科技、归哪个主题、主线几星、要不要建议纳入"全部由模型判断。
- 全市场扫描只覆盖最近收盘日（Yahoo 预设 screener 不支持历史回溯）。若 `--date` 指向更早交易日，evidence 里 `market_wide_movers.date_aligned=false`，此时板块赚钱效应 / 新方向段写明"扫描仅支持最近收盘日"或跳过。
- 异动票 / 新方向票的催化来自联网检索公开结果，必须带出处与日期，查不到写"未检索到明确催化"。

## 领域方法论

### 总览：三层 + 成交额优先

报告是三层叠加：**大盘锚（QQQ）→ 板块赚钱效应（纳指科技全景）→ 观察池（你已覆盖的部分）→ 池外新方向（你没覆盖的部分）**。贯穿全程的纪律是**成交额（dollar-volume）优先**：没有成交额支撑的涨幅只是弱证据；主题强弱、主线评级、新方向可信度都先看 dollar-volume 厚度。

### 1. 大盘锚（仅 QQQ）

QQQ 用四个数读，不与个股做扩散度对比、不做风格判断：

- 当日 `change_pct`：纳指今天偏多还是偏空。
- 5 日 / 20 日趋势：中短期方向。当日与 5 日同向才下"趋势"结论，不同向只写"短期 vs 中期分歧"。
- 52 周位置 `position_52w`：贴近高位（≥0.9）还是中位，决定"高位延续"还是"修复中"。
- 量能 `vol_vs_20d`：量比放大方向更可信，缩量方向意义打折。

### 2. 观察池逐股明细

每只成员股给：当日 `change_pct`、`five_day_trend_pct`、`vs_qqq_1d` / `vs_qqq_5d`、`position_52w`，加一个信号标记（图例见 `references/template/report_template.md`）。

### 3. 板块赚钱效应

读 `references/sector_money_effect.md` 执行完整流程：

1. 取 `market_wide_movers.rises` 作赚钱效应池 → 模型剔除非科技 → 按业务事实临时归纳主题（不套 ETF/GICS）。
2. 按主题 dollar-volume 占比定"钱在哪"。
3. 两遍走法：Pass 1 按占比锁定领先 2–3 个候选主题；Pass 2 对其成员跑 `--enrich-tickers` 拿 52周位置 / 量比 / vs-QQQ，再按 ★ rubric 评级，并判位置、拥挤度、领导股 / 弹性股。

### 4. 板块亏钱效应

取 `market_wide_movers.drops`，模型剔非科技后按主题归纳，回答"今晚哪个纳指科技方向在爆量下跌"。和赚钱效应对照：若某主题既有强势成员又有成员进跌池，写"主题内部分歧"。

### 5. 池外新方向主动挖掘

读 `references/new_direction_mining.md` 执行。纲要：任何今晚有赚钱效应、但观察池覆盖不到 / 不全的主题，按三档（新主题缺口 / 龙头缺口 / 邻接个股补充）给纳入建议；每条都要过 dollar-volume 下限、`--enrich-tickers` 确认、联网核查催化，区分一日脉冲 vs 多日持续。多日持续靠跨日台账查证（见 `references/cross_day_ledger.md`）。

### 6. 异动联网核查

所有 `is_abnormal=true`（\|当日\| ≥ 7%）的票汇总进一张「异动扫描」表。联网工具以 Tavily 为主路径，WebSearch / WebFetch 仅作兜底。详细流程、查询模板与优先级见 `references/template/report_template.md#信号列图例` 与 `references/sector_money_effect.md`。

## 工作流程

1. **确认日期 + 配置**：用户给日期就用，否则脚本默认最近交易日。
2. **取证据包**：运行 `scripts/generate_report.py`（可加 `--date` / `--output` / `--scan-universe`）。
3. **写大盘 + 观察池明细**：QQQ 四数读；每组一张表列全部成员。
4. **板块赚钱效应**：剔非科技 → 归纳主题 → dollar-volume 占比排序 → enrich 回补 → ★ 评级 + 位置 + 拥挤 + 领导/弹性股。
5. **板块亏钱效应**：drops 池剔非科技 → 按主题归纳。
6. **池外新方向挖掘**：三档 + enrich 确认 + 联网核查。
7. **异动核查**：对 ★ 票按优先级用 Tavily 核查，把来源填进异动扫描表。
8. **校验 + 落稿**：每个数字结论能在 evidence 找到出处；每条催化能找到搜索来源。产出最终 Markdown。
9. **跨日台账落库**：定稿后运行 `scripts/theme_ledger.py` 落库，让新方向票可标注首现 / 连续 N 晚 / 已建议待跟踪。
10. **HTML 派生**（用户要求时）：Markdown 定稿后 `python scripts/render_report_html.py`；详见 `references/html_rendering.md`。

## 数据获取（脚本抓手）

### `scripts/generate_report.py`（主入口）

```bash
python scripts/generate_report.py                                 # 最近交易日 + 默认 sync + 默认扫描
python scripts/generate_report.py --date 2026-06-18 --output outputs/us-2026-06-18.json
python scripts/generate_report.py --no-sync                     # 跳过飞书同步
python scripts/generate_report.py --no-market-scan                # 跳过纳斯达克扫描
python scripts/generate_report.py --scan-min-dollar-volume-million 100
python scripts/generate_report.py --scan-universe               # 额外扫 curated 纳指科技 universe
python scripts/generate_report.py --enrich-tickers "ALAB,CRDO,AAOI" --output outputs/enrich.json
```

### `scripts/scan_market.py`

纳斯达克当日异动 / 赚钱效应扫描：Nasdaq 交易所过滤 → 算 dollar-volume → 按成交额降序 → 标 ±7% 异动。

```bash
python scripts/scan_market.py
python scripts/scan_market.py --min-change 4 --min-dollar-volume-million 100
python scripts/scan_market.py --all-exchanges
```

### `scripts/theme_ledger.py`（跨日台账）

```bash
python scripts/theme_ledger.py context --asof 2026-06-18
python scripts/theme_ledger.py record --input outputs/lifecycle_20260618.json
```

### `scripts/render_report_html.py`

```bash
python scripts/render_report_html.py --input reports/us-2026-06-18.md --evidence outputs/us-2026-06-18.json
python scripts/render_report_html.py --input reports/us-2026-06-18.md --theme print
```

### `scripts/sync_from_lark.py`

读飞书表格 → 覆写 `assets/stock_pool.yaml`。由 `generate_report.py` 默认触发，也可手跑（`--dry-run` 只打印）。

### 联网检索

```bash
python scripts/web_search.py "ALAB stock news 2026-06-18" --topic news --days 7
python scripts/web_search.py "Credo CRDO catalyst 2026-06-18" --topic news --days 7 --max-results 8
```

返回 JSON（`results[].title / url / published_date / content`）。需 `TAVILY_API_KEY`。Tavily 查不到时才退回 WebSearch / WebFetch。

### evidence JSON 主要字段

- `type=us_market_watchlist_evidence`、`date`、`generated_at`、`thresholds`
- `indices`：仅 QQQ
- `groups`：每组成员快照 + summary
- `group_indices`：各非空分组等权合成 ETF 指数序列，rebase 到 100，与 QQQ 同窗
- `abnormal_moves.rises/.drops`：观察池内 ±7% 票
- `market_wide_movers`：纳斯达克扫描结果（`rises` / `drops`、含 `dollar_volume` / `is_abnormal` / `market_state` 等）
- `universe_scan`（仅 `--scan-universe`）：板块广度与轮动数据
- `errors`：拉取失败清单

## 输出规范

风格：中立研究笔记；1000–3000 字；不写"我"。

遵循仓库项目级文风默认：讲人话、减少模板腔；同项罗列用 list 但每条说人话，结构化对照用表格。

固定结构与数据呈现规则见 `references/template/report_template.md`；HTML 渲染规则见 `references/html_rendering.md`。

核心纪律：

- 证据优先：所有强弱判断回到 dollar-volume / vs-QQQ / 量比 / 52周位置 / 涨跌幅，同一自然段最多 2–3 个关键数字，其余留表格。
- 人话先行：每个判断段第一句是自然语言结论，不以连续数字开头。
- 每节只回答一个问题：进攻 / 分歧 / 退潮 / 修复 / 拥挤 / 扩散 / 新方向。
- 不写买卖建议。

## 示例

### Input

> 复盘下昨晚美股，纳指科技板块今晚钱在哪个方向？我池子覆盖到没有，有没有该补的新方向？

### 执行

```bash
python scripts/generate_report.py --output outputs/us-2026-06-18.json
python scripts/generate_report.py --enrich-tickers "ALAB,CRDO" --output outputs/enrich.json
python scripts/web_search.py "ALAB stock news 2026-06-18" --topic news --days 7
```

按领域方法论与 `references/template/report_template.md` 成稿。

### Output 片段

```markdown
# 纳斯达克科技观察池日报 - 2026-06-18（纳指版）

## 今晚一句话
==纳指偏多、贴近 52 周高位放量上行，今晚的钱高度集中在半导体/存储：MRVL、MU、INTC、SNDK 同步放量，是当晚唯一的 ★★★ 确认主线，而你的 Semi + AI光存 两组已基本覆盖。真正的缺口在 AI 互联——ALAB 一夜 +11.3%、跑赢 QQQ 8.8pp、3.7 倍量创新高，不在池里，建议纳入。==

## 板块赚钱效应（纳指科技）
| 主题 | ★ | 成交额占比 | 位置 | 拥挤度 | 领导股 | 催化逻辑 |
|---|:--:|---:|---|---|---|---|
| 半导体/存储 | ★★★ | ~58% | 高位趋势 | 中 | MU（$73B）、MRVL（$78B） | HBM/存储涨价周期，财报前 positioning |
| AI 互联/retimer | ★★ | ~9% | 低位启动 | 高 | ALAB（$9B） | retimer 在 hyperscaler 起量 |
```
