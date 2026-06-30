# 模块 6：策略选股观察清单

使用 `module6_strategy_candidates.json`（由 `strategy_picks.py context` 生成）。**本节在聚合阶段写，
不是并行 subagent**——因为信心分档要引用 3.2 主线判定（主线归属是模型产出，脚本不知道），必须在
模块 3、5 草稿写完之后再写。

## 这一节是什么，不是什么

它把三样东西拧到一起，给出**信心分档的观察清单**：① 当日特征分组命中的个股（来自模块 5）；
② 这些分组历史上靠哪些**被验证过的叠加条件**赢（策略画像 `references/strategy_profiles/`）；
③ 这些条件的**回测胜率**与本策略的**样本外实盘战绩**。

**硬规则（与全 skill 一致）：这是观察清单，不是下单单。** 绝不写买入/卖出/止损/目标价/仓位/具体
买点。每只票配「持续性待验证条件」——下一交易日要看到什么、什么情况证伪，而不是"怎么买"。

## 脑/手边界

脚本（`context`）只铺确定性候选证据：命中组别、满足/未满足/无法评估的被验证条件及其历史
`n/win/rel_mean/delta/oos_balance`、当日因子值、交叉命中、样本外台账战绩、一个中性排序键。
**脚本不定信心档、不下结论。** 选谁进清单、信心几档、主线归属、归因措辞、持续性待验证条件，
全部是模型的判断。

## 信心分档 rubric（锚点化，必须照此判）

对每只候选股，按下面锚点定档。**技术命中 ≠ 可操作**，主线归属与稳健度是升档的关键。

- **强（strong）**：满足 ≥1 条 `robustness=strong/medium` 且 `oos_balance ≥ 0.5` 的被验证条件，
  **并且**（落在当日 3.2 的 ★★/★★★ 主线内 **或** 交叉命中 ≥2 组）。即：历史 edge 稳 + 今天有主线/多组共振。
  - 例：某票命中容量上涨、满足"总市值≥300亿∧量比<1.5"(win59%、bal0.8)，且在当日 ★★★ 算力主线内 → 强。
- **中（medium）**：满足有效条件，但只占其一——要么 edge 稳却不在主线/无交叉，要么在主线却只匹配到
  `oos_balance<0.5` 或 `robustness=fragile` 的小样本条件。即：有据但共振或稳健度欠一项。
- **观察（watch）**：命中了分组但**只匹配到脆弱/无法评估的条件**，或所属分组**无画像（未标定）**，
  或不在任何主线内。纯技术观察，不拔高。

补充判据：
- 实盘台账 `oos_ledger_stats` 的近 N 次该档胜率作**信心微调**——明显偏低时降一档并点明；但 N 小时
  只标注、不据此升档。
- `cannot_evaluate` 的条件不计入"满足"，要在理由里如实说"该条件因缺 XX 因子未能评估"。
- 画像 `status=stale`（过期）：可用但需在理由里标"画像已过期、仅供参考"，不据此定强档。

## 回测表现 vs 样本外表现：分开说

`base_backtest`/条件 `win`/`rel_mean` 是**回测画像表现**（样本内、单一窗口）；`oos_ledger_stats` 是
本策略**真实样本外战绩**（按 relc_5）。**两者分开引用，不要合成一个胜率。** 都要带样本量与 caveat：
单环境小窗口、N 小、均值右偏需同看中位、胜率口径（绝对 vs 相对）不同。

## 覆盖与降级

`meta.profiles_coverage` 标了每组画像状态。`missing`（如本轮 monthly_base_breakout / early_limit_up_1030
尚无画像）的组，命中股只作纯技术观察、不拼历史胜率、最高给"观察"档。日报照常生成，不因策略层缺画像而中断。

## 写法

- 先一句话判断（用 `==...==` 高亮）：今天策略层更像"有主线共振的高信心点位"还是"多为技术命中、缺主线确认"。
- 用表格列清单（强→中→观察排序）。每只给：命中组别、满足的有效条件（带回测 win/N）、主线归属(★)、
  交叉命中、**持续性待验证条件**。
- 表下补一行实盘战绩（引用 `oos_ledger_stats`，注明样本外、N 与 caveat）。
- 无任何候选满足有效条件时，如实写"今日命中股暂无匹配到历史有效叠加条件，仅作技术观察"，不硬凑。
- 文风讲人话、同项用 list；禁买卖建议。

## 写完之后：产出 record 输入

把本节定稿的清单写成 `reports/strategy_picks_YYYYMMDD.json`，交 `strategy_picks.py record` 落台账
（积累样本外战绩）。schema：

```json
{
  "asof": "YYYY-MM-DD",
  "source_report": "reports/report_YYYYMMDD.md",
  "source_evidence": "reports/evidence_YYYYMMDD_utf8.json",
  "picks": [
    {
      "ts_code": "002129.SZ", "name": "TCL中环", "board": "深主板",
      "groups_hit": ["capacity_up"], "conviction_tier": "strong",
      "in_main_line": "光伏/硅片 ★★", "rationale": "一句话讲清为什么是这档（有据 + 共振）",
      "matched_conditions": [{"condition_id":"mv_ge_300_x_volratio_lt_15","label":"总市值≥300亿∧量比<1.5","win":59.1,"rel_mean":2.73,"n":991}],
      "feature_snapshot": {"total_mv_100m_yuan":452.0,"volume_ratio":1.2,"...":"当日因子值，至少含 total_mv_100m_yuan 供基准/打分"},
      "profile_fingerprints": {"capacity_up":{"version":"2026-06-29.1","hash":"<profile_hash>","target_cell":"close_T+5"}},
      "backtest_stats_snapshot": {"base":{"rel_mean":0.69,"win":54.2}}
    }
  ]
}
```

`conviction_tier` 入库用枚举 `strong/medium/watch`，报告展示翻成"强/中/观察"。`feature_snapshot`、
`matched_conditions`、`profile_fingerprints` 直接取候选证据里对应字段（快照固化，便于日后样本外复盘
"当时按什么规则、什么数据选出来"）。只写真正进清单的票，纯技术观察、不进台账的可不写。
