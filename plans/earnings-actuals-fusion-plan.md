# a-stock-earnings-forecast 正式披露期数据融合方案

日期：2026-07-21。背景：H1 强制预告披露已于 7/15 截止，当前进入自愿性披露阶段；8 月进入业绩快报与半年报正式披露期（8/31 截止）。本方案回答两件事：自愿性披露的抓取现状核查结论，以及正式披露数据如何与现有预告数据融合。

## 一、自愿性披露核查结论（无需改代码）

实测 CNInfo「业绩预告」分类 2026-07-16～07-21 窗口，共抓到 77 条半年度预告，按日分布 16 日 17 条、17 日 16 条、18 日 8 条、20 日 26 条、21 日（今天）10 条。其中大量标题明确为「……业绩预告的自愿性披露公告」（澜起科技、海光信息、摩尔线程、佰维存储、思特威、天合光能等，科创板居多——科创板本无强制预告义务，全部属自愿披露）。

结论：

- CNInfo 的预告分类不区分强制/自愿，`cninfo_client.is_forecast_announcement_title` 的标题规则（年份 + 半年关键词、排除快报/问询）对自愿披露标题命中正常，**现有管线零改动即可持续抓到自愿披露**。
- 扫描窗口默认止于 `min(今天+1, period+75d)`，period+75d ≈ 2026-09-13，覆盖整个 8 月；水位机制 + `--refetch-days 3` 保证日常增量跑就能补全。唯一要求是财报季期间保持日频跑 `forecast_scan.py`。
- 周末也有披露（7/18 周六 8 条），按日历日扫描的现有设计正确。

## 二、正式披露期融合方案

### 2.1 事实阶梯模型（核心抽象）

每只股票在一个报告期内的业绩事实最多经历三级演进：

1. **forecast** — 业绩预告：区间值 + 中值折算，多数无营收，扣非靠 PDF 解析（现状）。
2. **express** — 业绩快报：单值、未审计、自愿披露，有营收和归母，无扣非。7 月下旬开始零星出现，8 月增多。
3. **final** — 半年报正式报告：审计后完整口径，营收/归母/扣非全有，8/31 截止。

设计原则：**权威级 final > express > forecast，只叠加不覆盖**。预告数值永不删除——预告 vs 实际的偏差（兑现度）本身是最重要的新增信号，也是对本 skill「优秀分档」判断的天然回测。

### 2.2 数据源选型：实际数走 Tushare 结构化，不解析半年报 PDF

预告必须解析 CNInfo PDF，是因为 Tushare forecast 滞后/缺漏且无扣非。但快报和正式报告在 Tushare 是可靠结构化数据，无需重走 PDF 路线：

| 阶段 | 数值权威 | 字段 | CNInfo 角色 |
|---|---|---|---|
| express | Tushare `express` | revenue、n_income、yoy 系列 | 仅公告元数据（ann_date/链接，快报分类 `category_yjkb_szsh`），供溯源与断层计算，不解析 PDF |
| final | Tushare `income`（report_type=1）+ `fina_indicator`（`profit_dedt` 扣非） | 全口径 | 不需要（半年报几十上百页，解析成本高且无必要） |

现有 `_QUICK_OR_NOISE` 把「业绩快报」从预告流里排除是对的，保持不变——快报走独立通道，不混入预告发现。

### 2.3 已核实的现有管线缺口（融合的具体切入点）

- `forecast_scan.py::needed_income_periods()` **不包含报告期本身**（20260630），income 取数只服务基数/trailing 拆解。
- income 有缓存后按 code 不再重取（除非 `--refresh-income`/`--rebuild`）。因此 8 月正式报告在 Tushare 上线后，现有管线不会自动拿到本期实际值。
- 修复方向：不动基数逻辑，新增「实际期探测」——对 `stage < final` 的池内股票，在披露窗口内（period+1d ～ period+75d）允许重查本期 income；`fetch_income_periods` 的 range call 本来就会返回本期行（start=上年 0101、end=今天），只需把 needed 集合与缓存刷新条件扩展到本期，并以「本期行是否已出现」作为该 code 停止重查的水位。

### 2.4 存储

- 新表 `forecast_actual_cache`，PK `(ts_code, end_date)`，字段分组：`express_*`（revenue/n_income/ann_date）、`final_*`（revenue/n_income_attr_p/profit_dedt/ann_date）、`stage`、`updated_at`。沿用 store.py 的 upsert/建表模式。
- 快报公告元数据复用 `forecast_cninfo_announcement`，加 `ann_type` 列（forecast/express），或平行小表；水位照抄 `forecast_cninfo_fetch_log` 模式、独立 category 扫描。
- 不改 `forecast_cache`（预告事实层保持纯预告语义）。

### 2.5 evidence JSON 增量（`forecast_scan_<period>.json`）

每股新增 `actuals` 块（无实际数时缺省 stage=forecast）：

```json
"actuals": {
  "stage": "express | final",
  "np_yi": 3.21, "kf_np_yi": 3.05, "revenue_yi": 18.6,
  "ann_date": "20260815", "source": "tushare_express | tushare_income",
  "vs_forecast": {
    "in_range": "within | above | below",
    "vs_median_pct": 4.2,
    "range_position": 0.73
  }
}
```

派生口径升级（脚本只算数，判断仍归模型）：

- **三口径切实际**：stage≥express 后，Q2 单季 = 实际 H1 − 实际 Q1（精确值替代中值估计），`cum_yoy/single_q_yoy/qoq` 全部换实际基数，同时保留预告口径数值供对照；`single_q_note` 增加 `actual_based` 标记。
- **营收补齐**：`revenue_trailing`（Q1 trailing 代理）升级为本期实际营收，「预告无营收」的缺口在 express/final 阶段自然消失；利润-营收匹配度判断从代理证据变成同期实证。
- **估值切实际**：年化 PE 的分子换实际数；final 阶段扣非以 `fina_indicator.profit_dedt` 为权威，替代 PDF 解析值（PDF 解析值保留作对照）。
- **兑现度只出证据**：`in_range/vs_median_pct/range_position` 是机械计算；「超预期兑现/贴下限压线/低于预告」的定性由模型下。

### 2.6 verdict 台账联动：兑现复判

复用现有「预告修订 → 待复判」机制，同一模式扩展：

- stage 升级（forecast→express、express→final）触发该股进入 `verdict.py context` 的待复判队列。
- 模型补判新字段 `fulfillment`（兑现超中值 / 符合 / 贴下限 / 低于预告 / 反向），并有权调整 `tier`（如强档但实际贴预告下限且营收未同步 → 降中档）。
- 主线趋势与产业综述的样本指纹把 stage 与实际值纳入，实际数批量落地后自动触发趋势/综述复判——8 月底正式报告集中披露时，这会自然生成一轮「预告季 vs 实证季」的行业级校验。

### 2.7 HTML 呈现

- 列表行加 stage 徽标（预告/快报/年报）与兑现度标记（超/符/低）；过滤条支持按 stage 筛。
- 详情页加事实阶梯时间线：预告（ann_date、区间、中值）→ 快报（数值、vs 预告中值）→ 年报（终值、扣非、vs 预告），每级标数据来源。
- 头部审计条与 K 线分片机制不变。

### 2.8 范围决策（开放项，需拍板）

正式披露期全市场都有实际数，「无预告但半年报优秀」的公司要不要进来？

- **a) 推荐：skill 锚定预告池不变**，实际数只用于升级池内事实。全市场财报扫描是另一个 skill 的边界，本 skill 的叙事是「预告 → 兑现」闭环。
- b) 快报通道兼做第二发现源（快报也是超预期披露事件、有断层效应），池会显著膨胀。
- 中间态：快报里预告池外的高增速公司以「池外提示」附录名单列出（只列不判、不进主表、不进 verdict 台账），信息不丢、边界不破。

默认按 a) + 池外提示附录执行，除非另有指示。

### 2.9 分期落地

- **Phase 1（现在～7 月底）**：express 通道（Tushare express 日频增量 + CNInfo 快报元数据扫描）、`forecast_actual_cache` 建表、`actuals` 块与兑现度计算、`needed_income_periods` 本期探测修复。
- **Phase 2（8 月）**：final 阶段接入（income + fina_indicator 扣非）、三口径/营收/PE 切实际、verdict 兑现复判、HTML stage 徽标与事实阶梯。
- **Phase 3（8 月底～9 月初）**：期末收官——全池兑现统计（多少超中值/贴下限/miss、按 tier 分层的兑现率），作为该报告期收尾章节素材，同时是对本 skill 分档质量的一次自评。

### 2.10 SKILL.md 需同步的点

- 「数据现实」加一条：8 月起事实分三级（预告/快报/年报），实际数以 Tushare 结构化为权威、CNInfo 只供快报溯源；预告数值保留供兑现度对照。
- 「输出规范」加：写到 stage≥express 的个股必须交代兑现度（落区间哪一侧、vs 中值多少）；预告口径与实际口径并存时以实际为准、预告为对照。
- description 补触发词：业绩快报、兑现、正式财报 vs 预告偏差等表达。
