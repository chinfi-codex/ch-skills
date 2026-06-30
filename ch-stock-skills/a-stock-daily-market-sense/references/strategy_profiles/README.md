# 策略画像（strategy_profiles）

这是「策略选股」慢循环的产物，也是每日复盘第 6 节的**只读输入**。一个特征分组一份
`<group_id>.json`，记录该组经回测挖掘、再由模型按 `factor_mining.md` §四 rubric 选定的
**有效叠加条件 + 历史回测表现**。

这里是画像的 **canonical 真相源**（进 git，可 diff / review / rollback），不进 PG。
实盘选股台账（样本外战绩）才在 PG 的 `strategy_pick_ledger`。两者分工见 SKILL.md。

## 字段约定

| 字段 | 含义 |
|---|---|
| `profile_id` | = 分组键（`discount_relaunch` / `capacity_up` / …），需与 evidence `groups` 的键一致 |
| `profile_version` / `asof` | 版本与挖掘基准日；`strategy_picks.py` 用 `asof + max_age_days` 判过期 |
| `window_start/end` | 回测信号窗口（诚实标注，样本/环境单一） |
| `target_cell` | 目标格，如 `close_T+5`（进场 T+1 尾盘、持有到 T+5），与下面各条统计口径一致 |
| `base` | 基础组 6 格里目标格的表现（n/mean/median/win/rel_mean/rel_win） |
| `selected_conditions[]` | 模型选定的叠加条件，每条 `all` 是原子 AND 列表（factor/op/threshold），带 `n/win/rel_mean/delta/oos_balance/robustness/rationale` |
| `robustness` | 画像整体可信度 strong/medium/fragile（模型判，非脚本算） |
| `max_age_days` | 超过则 `context` 标 stale 降级（默认 60） |
| `model_note` / `caveats` | 经济逻辑与诚实告警 |

**只用当日可知的因子**：`t1_gap`（次日高开）等未来值不能进画像——当日选股时不可知。
脚本 `strategy_picks.py` 对画像里引用了缺失因子的条件会标 `cannot_evaluate`。

## 覆盖现状（2026-06-29）

- `discount_relaunch`、`capacity_up`：已标定（本仓库可回测）。
- `monthly_base_breakout`（事件型多日 + 月线）、`early_limit_up_1030`（依赖 JRJ 历史封板时间，
  DB 无历史）：**暂无画像**，命中只作纯技术观察，等有可回测口径再补。

## 刷新（慢循环，每周/按需）

```bash
# 1. 跑回测挖掘（内置组直接点名；自定义组用 spec）
python3 scripts/factor_backtest.py --group discount_relaunch --min-n 15           # 全量(需 TUSHARE_TOKEN 回补 daily_basic)
python3 scripts/factor_backtest.py --group custom --spec capacity_up_spec.json    # 容量上涨
# 2. 读 reports/factor_mining_<group>_<asof>.json，按 factor_mining.md §四 rubric 选叠加条件
# 3. 更新本目录对应 <group_id>.json（bump profile_version / asof / window），提交 git
```

脚本不替模型选条件、不自动改这里——选解是模型的判断，落盘由人确认。
