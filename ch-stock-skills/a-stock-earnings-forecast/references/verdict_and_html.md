# 判分台账 + 主线匹配 + 报告期 HTML

把模型的"优秀分档"和"归属主线"结构化落库（`forecast_verdict`），再投影成一期一页的 HTML。分档判断的锚点见 `methodology.md` §二；这里讲**主线匹配方法论**、判分/渲染工作流、增量与陈旧语义。

## 一、判分台账（forecast_verdict）

一条 = 一个 `(period, ts_code)` 的模型判断：`tier`（强/中/观察/剔除）+ `theme_id`（归属主线）+ `reason`/`caveat`/`theme_rationale`/`match_confidence`，外加脚本快照的 `evidence_ann_date`/`evidence_cum_yoy`（判分当时的证据指纹，用于陈旧检测）。

- **增量判分**：`verdict.py context` 只列「新披露 + 待复判」的股，老判分保留。每天只判增量，不重判全市场。
- **脑/手**：选谁哪档、归哪条主线是模型写进 JSON 的；`verdict.py record` 只做枚举/存在性校验 + 快照 + upsert。

## 二、主线匹配方法论（Agent 语义判断）

目标：把每只预告股对上 daily-market-sense 正在跟踪的**在场主线**（`theme_registry` + `theme_daily_state`），不是套行业标签。

匹配依据（context 已给全）：每条主线的 `name`、`aliases`（历年归一的别名）、`members_sample`（近日成员样本）、当前 `state`/`stars`。每只股的 `change_reason`（**最强信号**——业绩由什么驱动）、行业、名称。

判断步骤：
1. 读 `change_reason` 抓业务实质：是什么产品/环节在驱动业绩（如"电解液添加剂 VC 涨价""无人机动力系统放量""铜箔/铝加工"）。
2. 和主线的 name/aliases/members_sample 做**语义**比对，不是字面包含。例：孚日股份的 VC 属电子化学品 → 归"锂电/新能源材料"链；三瑞智能无人机动力 → 归"军工低空"。
3. 给 `match_confidence`：
   - `high`：业务实质与主线高度一致，或已是该主线成员样本。
   - `medium`：属于该主线的上下游/分支，方向对但不在核心。
   - `low`：只是沾边，渲染成"疑似【X】"，供人工复核。
4. **对不上就填 `null`**。不要硬贴。`null`=无归属，是"业绩强但暂无主线关注"的**正向信号**（潜在未被市场发现），HTML 会单列。
5. 一只股给**一条主线**（primary）。确实横跨两条，在 `theme_rationale` 里注明，`theme_id` 仍取最主要那条。
6. `theme_registry` 为空（daily-market-sense 没跑）时，`theme_id` 一律 `null`，报告说明"主线台账未填充"。

注意主线的**当前状态**由 HTML 渲染时实时 join（`theme_daily_state` 最新一日），不写进 verdict——所以看到的"在场候选★★/退潮"永远是最新的；归"退潮"主线也合法（业绩兑现但主线在退，本身是有用对照）。

## 三、陈旧复判（staleness）

判分后预告可能被修订、或增速大幅漂移。`context`（和 HTML）用 `evidence_ann_date != 当前 ann_date`，或 `|当前累计同比 − 判分时累计同比| > 阈值(默认 20 个百分点)` 判为**待复判**：context 会重新列出该股、HTML 打 `待复判` 徽标。模型下一轮据新证据修 tier/theme。这样分档永远知道哪些基于当前证据、哪些已过时。

## 四、报告期 HTML（render_period_html）

一期一个 `forecast_<period>.html`，每次从累积缓存**整页重渲染**（幂等，不 patch DOM）：

- **KPI**：已披露、今日新增、已归属主线、无归属、已补扣非、分档计数、待判/待复判。
- **按主线分组**：每条在场主线一张卡（名称 + 当前★状态）列其下的业绩强票；末尾"无归属主线"卡单列（潜在未被发现）。
- **个股明细表**：名称·代码·类型·累计同比·单季同比(加速↑)·扣非·**归属主线(当前状态)**·分档；前端可搜索/排序/按分档或主线筛选。`NEW`（近 5 日首披）/`更新`（预告修订）/`待复判` 徽标。
- 自包含单文件、无外链 CDN、支持深色模式。渲染只投影 evidence+verdict+主线状态，不新增判断，不给买卖建议。

增量落在数据层：DB 每天累积新披露 → evidence 全量 → verdict 增量判 → HTML 重渲染即"长大一点"。历史披露不因换天而丢。
