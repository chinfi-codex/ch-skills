# 判分台账 + 质量成色 + 兑现度 + 主线匹配 + 行业趋势 + 报告期 HTML

把模型的判断结构化落库（`qreport_verdict` / `qreport_theme_trend`），再投影成一期一页的 HTML。分档锚点见 `methodology.md` §二，行业趋势归因见 §九；这里讲**台账格式、判分工作流、增量与陈旧语义、HTML 契约**。

## 一、判分台账（qreport_verdict）

一条 = 一个 `(period, ts_code)` 的模型判断。比预告台账多两个字段，因为正式财报能回答预告回答不了的两个问题：

| 字段 | 枚举 | 说明 |
|---|---|---|
| `tier` | 强 / 中 / 观察 / 剔除 | 业绩优秀度分档 |
| `quality_call` | 扎实 / 尚可 / 存疑 / 虚高 | **季报独有**：利润质量三件套的综合定性（`methodology.md` §二） |
| `fulfillment` | 超预告上限 / 落区间上沿 / 符合 / 落区间下沿 / 低于预告 / 无预告 | **季报独有**：对公司自己指引的兑现 |
| `theme_id` | `theme_registry` 里的 id 或 `null` | 归属主线，对不上填 `null` |
| `match_confidence` | high / medium / low | 主线匹配置信度 |
| `reason` / `caveat` / `theme_rationale` | 自由文本 | 判断理由、风险、归属理由 |

脚本另外快照判分当时的证据指纹（`evidence_ann_date` / `evidence_np_single_yoy` / `evidence_rev_single_yoy`）用于陈旧检测。

- **增量判分**：`verdict.py context` 只列「新披露 + 待复判」的股，老判分保留。财报季每天只判增量，不重判全市场。
- **脑/手**：选谁哪档、质量算不算扎实、归哪条主线是模型写进 JSON 的；`record` 只做枚举/存在性校验 + 快照 + upsert，非法值跳过并在 `errors[]` 里报出。

`quality_call` 与 `tier` 是两个维度，不要合并：一家可以是「中档但扎实」（增速平淡、质量干净），也可以是「强档但存疑」（增速炸裂、现金流跟不上）。后者恰恰是最该写进 caveat 的组合。

## 二、主线匹配方法论（Agent 语义判断）

目标：把每只财报股对上 daily-market-sense 正在跟踪的**在场主线**（`theme_registry` + `theme_daily_state`），不是套行业标签。

匹配依据：每条主线的 `name`、`aliases`、`members_sample`、当前 `state`/`stars`；每只股的业务实质、行业、名称。

**和预告 skill 的差别**：预告自带《业绩变动原因》，业务实质一读就知道。正式报告没有这段文本，所以匹配依据是——公司名称与行业、`screen.hits` 反映的驱动特征（`margin_expanding` + `orderbook_building` 指向景气链）、以及必要时 `report_pdf.py --sections segment` 读到的分产品收入。**宁可标 `low` 或填 `null`，不要硬贴。**

给 `match_confidence`：`high` 业务实质与主线高度一致或已是成员样本；`medium` 属上下游/分支，方向对但不在核心；`low` 只是沾边（渲染成"疑似"，供人工复核）。对不上填 `null`——`null` 是"业绩强但暂无主线关注"的正向信号，HTML 会单列。一只股给一条主线；横跨两条时在 `theme_rationale` 里注明。`theme_registry` 为空（daily-market-sense 没跑）时 `theme_id` 一律 `null`。

主线的**当前状态**由 HTML 渲染时实时 join `theme_daily_state` 最新一日，不写进 verdict——所以看到的"在场★★/退潮"永远是最新的。

## 三、报告期行业趋势台账（qreport_theme_trend）

一条 = 一个 `(period, theme_id)` 的模型判断，材料是 `context` 的 `to_judge_themes`（每条主线的强/弱/无断层成员简报）。方法论见 `methodology.md` §九。

```json
{"theme_id": "TH-半导体链", "direction": "向上",
 "strong_common": "两只强表现都指向AI/存储需求驱动的量价齐升，且都伴随毛利率抬升，是同一条景气链而非各自的个体故事",
 "weak_common": null,
 "cross_validation": "强侧共性成立、弱侧无成员，行业向上；但两者现金成色分化，下一步盯Q2的OCF能否跟上利润",
 "confidence": "medium"}
```

- `direction` 枚举：**向上 / 向下 / 分化 / 证据不足**（"证据不足"是合法结论）。
- `strong_common` / `weak_common`：两侧归因结论，某侧无成员填 `null`。
- `cross_validation`：一句话交叉验证（方向是否成立、由什么驱动、下一步看什么），HTML 直接展示。
- `record` 校验 theme_id 存在、枚举合法，并机械快照判断时点的强/弱/成员数；**本期无归属成员的主线趋势会被拒**（无观察对象）。
- **哪些主线要判**：`to_judge_themes` 列出的，**加上本轮你新归入的主线**（context 生成于判分前，列不出它们）。

## 四、陈旧复判（staleness）

判分后报表可能被追溯调整或更正重发。`context`（和 HTML）判为**待复判**的两种情况：

- `stale_restated`：`evidence_ann_date` 变了（追溯调整/更正报告）——数字底座换了，必须重判。
- `stale_drift`：单季同比相对判分时漂移超 `--drift`（默认 25 个百分点）。

行业趋势同理：台账快照了判断时点的强/弱/成员数，新成员出现或成员被改判后 `context` 以 `stale_membership` 重新列出，HTML 标"成员已变化，待复判"。把新成员并入原有两组重新归因即可。

## 五、报告期 HTML（render_period_html）

一期一个 `reports/qreport_<period>.html`，每次从累积缓存**整页重渲染**（幂等），并生成同级 `reports/qreport_<period>.klines/` K 线分片目录。**发布时两者必须一起复制**，只复制 HTML 会让 K 线提示分片加载失败。

**默认收录范围**：只渲染已在 `qreport_verdict` 归入主线（`theme_id` 非空）的个股；无归属主线个股在 Python 构建页面数据时就剔除，不是前端默认勾选的视觉隐藏。调试或人工复核可加 `--include-unassigned`，页头会显示本次剔除家数或已恢复全量。

**发布日期门禁（发布链路必开）**：按北京时间算 `NEXT_ANN_DATE=运行日+1`，扫描传 `report_scan.py --end-ann "$NEXT_ANN_DATE"`，渲染传 `render_period_html.py --require-ann-cutoff "$NEXT_ANN_DATE"`。不一致时返回 2 并拒绝生成，防止旧 evidence 被误发布。页头固定展示「披露扫描截至 YYYY-MM-DD（当日 N 家）」，即使 N=0 也显示——让读者区分"查到了次日但没记录"和"根本没扫次日"。

页面结构：**标题与披露进度条 → 过滤条 → 左列表 + 右详情双栏**。

- **左列表**（每股两行）：名称/代码/分档/质量成色徽标 + 断层标注 + 兑现度标注；第二行是「营收单季 · 归母单季 · 扣非 · 现金覆盖 · 披露日」。断层行整行淡色着色，**非 K 线信息统一红=上涨/强、绿=下跌/弱**。按 120 家一批渐进渲染，避免财报季全量样本一次性建超大 DOM。
  - 排序/分组：按披露时间（默认）/ 按机械得分排序 / 按主线分组（组头显示行业趋势）/ 按行业分组 / 平铺。
  - 过滤器：搜索、行业、主线、分档、**质量成色**、**兑现度**、反应（断层向上/向下/跳空未回补/未反应观察）、信号（hits）、扣非 TTM PE 分档。
- **右详情**（sticky）：
  - **扣非 TTM PE 醒目条**：大号数字 = 扣非 TTM PE，副行给总市值（含 asof）、扣非 TTM 净利、归母 TTM PE 对照、扣非年化 PE、市场 PE-TTM 交叉核对。亏损或缺市值如实说明，不硬给数。
  - **利润质量三件套**：扣非占归母 / 经营现金流覆盖 / 应收存货 vs 营收，三个大字块并列——这是本页相对预告页最重要的增量，放在最前。
  - **K 线**（SVG 自绘，~130 根前复权日 K + 成交量，红涨绿跌）：蓝虚线 = 披露日、橙虚线 = 披露前收盘，断层缺口一眼可见。数据走 `.klines/` 分片按需加载。
  - **股价反应**（精简：跳空幅度 + 披露后累计 + 新鲜度 D+n）。
  - **增长与盈利能力**：四条线三口径、毛利率与费用率 pp。
  - **兑现度**：落区间哪一侧、离中值多远、预告原文变动原因。
  - **资产负债表前瞻信号**：合同负债/存货/应收/在建工程及其同比环比与 gap。
  - **所属主线 · 报告期行业趋势**：先给机械层（强/弱/无断层分布、净方向、成员样本），再给判断层（模型的方向、两侧归因、交叉验证、confidence、判断时点）。
  - **判分**：tier / quality_call / fulfillment / reason / caveat。

断层方向、两型分类、行业趋势净值都是**机械阈值切分**，渲染只投影 + 计数；**定性（判·方向与归因）来自台账里模型的判断**，渲染只展示它，不替它判，不给买卖建议。

增量落在数据层：DB 每天累积新披露 → evidence 全量重算 → verdict 增量判 → HTML 重渲染即"长大一点"。历史披露不因换天而丢。
