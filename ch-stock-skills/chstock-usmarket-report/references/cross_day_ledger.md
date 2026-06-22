# 跨日连续性台账（theme ledger）

把每晚《板块赚钱效应》里判定的主题、以及《池外新方向》里挖出的候选，跨日沉淀成可查询序列。解决一个具体问题：**新方向最怕把一夜脉冲当成趋势**——单晚 +11% 和连续 3 晚在赚钱效应池里，在单份报告里看起来一样，台账就是区分它们的记忆，也用来给"上周建议纳入的票这周还在不在线"闭环。

对标 a-stock-daily-market-sense 的 `theme_lifecycle.py`：脚本确定性（提供上下文、校验枚举与状态机、归一别名、落库），判断（哪个主题是哪个、什么状态）全留给模型。

## 存储

不进版本库、不被 skill_sync 复制：默认 `~/.usmarket-ledger/`（`USMARKET_LEDGER_DIR` 可覆盖），所有 agent 副本读写同一份——和 DMS 用 Postgres 隔离同理。两个文件：`theme_registry.json`（canonical theme_id + 别名）、`theme_ledger.jsonl`（每日记录，append-only，按 asof 幂等 upsert）。

## 状态机（六态）

| 状态 | 判定基准（与日报口径一致） |
|---|---|
| 低位启动 | 新上榜且位置判断为低位启动 |
| 在场 | ★★ 且高位趋势，或启动后的延续 |
| 确认主线 | 当日 ★★★ |
| 高位分歧 | 主题内部撕裂 / 核心换锚 / 既有强势又有成员进亏钱池 |
| 退潮 | 从赚钱效应表消失、或成员进当日亏钱效应池 |
| 沉寂 | 连续约 5 个记录日缺席（隐式终态，可重新低位启动） |

合法转移由 `record` 强校验（非法报错并列出合法去向）。首次入库 / 沉寂后再启动只能是：低位启动 / 在场 / 确认主线 / 高位分歧。

## 当日落库流程

1. **取上下文**：`python scripts/theme_ledger.py context --asof YYYY-MM-DD`。拿到注册表、近期记录、watchlist（近期在场但当日尚无记录、需判退潮还是继续在场的主题）、状态机规则。
2. **模型写 `outputs/lifecycle_YYYYMMDD.json`**：完成别名归一（当晚临时主题名 → canonical theme_id；新主题用 `new_theme` 块注册，`theme_id` 必须 `TH-` 前缀）和状态判定。
   ```json
   {
     "asof": "2026-06-18",
     "records": [
       {"theme_id": "TH-semis-memory", "raw_theme_name": "半导体/存储",
        "stars": 3, "dollar_vol_share": 0.58, "position": "高位趋势",
        "state": "确认主线", "in_pool": true, "is_new_direction": false,
        "members": ["MU","MRVL","INTC","SNDK"], "evidence": "HBM/存储涨价，MRVL/MU 同步放量"},
       {"new_theme": {"theme_id": "TH-ai-interconnect", "name": "AI 互联/retimer", "aliases": ["retimer"]},
        "raw_theme_name": "AI 互联", "stars": 2, "dollar_vol_share": 0.09, "position": "低位启动",
        "state": "低位启动", "in_pool": false, "is_new_direction": true,
        "members": ["ALAB"], "evidence": "ALAB +11% 创新高放量"}
     ]
   }
   ```
   字段：`stars` 1/2/3，退潮/沉寂可为 null；`in_pool` 该主题是否被观察池覆盖；`is_new_direction` 是否为池外新方向候选；`force:true` 跳过状态机校验（仅人工确认特例）；watchlist 中当日缺席的主题，结合亏钱效应池判 `退潮` 或 `沉寂`。
3. **落库**：`python scripts/theme_ledger.py record --input outputs/lifecycle_YYYYMMDD.json`。校验失败逐条列错，修正重跑（幂等）。

## 报告里怎么用

- 新方向票标注持续性：`首现` / `连续 N 晚` / `已建议纳入待跟踪`。连续 ≥2–3 晚 + 放量 = 强候选；单晚先观察。
- 主线老化：`确认主线` 连续多日 + 高位 → 提示拥挤/分歧风险。
- 闭环建议：上次 `is_new_direction` 的票，本次状态是延续、纳入还是转冷，写一句。

落库是日报定稿后的收尾步骤，不影响正文真相源；台账只存序列数据。
