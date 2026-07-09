# 因子实验室 · 周度体检读法与判断 rubric

这是**慢循环**的收口：因子挖掘（`factor_mining.md`）挖出条件、选进画像后，靠这条周度体检持续
验证它们还灵不灵。跟每日复盘无关，周末或按需跑一次。

脑/手边界照旧：`factor_lab.py` 只铺确定性对账与对比证据，**不判维持/降级/刷新、不给买卖建议**。
读证据、下判断、改画像（bump 版本、进 git）是模型 + 人的活。

## 一条命令出体检包

```bash
python3 scripts/factor_lab.py weekly            # → reports/weekly_factor_pack_<asof>.json（≤150KB）
```

它把三块拼进一个决策包，一个上下文读完：`calibration`（承诺 vs 兑现）、`profile_refresh`
（旧条件还过不过闸）、`recent_experiments`（挖过什么、判没判）。也可分开跑
`calibrate` / `refresh` / `experiments`。

## 读包顺序与判断锚点

### 1. calibration —— 承诺 vs 兑现（验证的心脏）

`by_condition` / `by_tier` / `by_group` 各行：`promise` 是落库时回测快照，`realized` 是台账样本外
真实战绩，`gap = realized − promise`。**先看 n，再看 gap。**

- `insufficient_sample=true`（scored_n<10）：**只标注、不下结论**。台账还浅时这是常态（T+5/T+10
  常未成熟，只有 T+3 有数很正常），别据此改画像。
- 样本够（scored_n≥10 起有参考价值、≥20 才算稳）时看 `gap_win`：
  - `gap_win` 在 ±10pt 内：承诺基本兑现，维持。
  - `gap_win` ≤ −20pt 且持续多周：过拟合/环境漂移的实证，**建议给该条件降级为加分项或移出画像**，
    在周报里点名并给出 realized 数字。
- `by_tier` 检验分档是否真分出信息：`strong` 的 realized 应稳定优于 `watch`。若 `strong` 反而更差且
  样本够，说明信心分档的 rubric 需回炉——这是对第 6 节策略选股的反馈。
- 口径纪律：`win`（相对胜率）与 `rel_mean`（相对均值）分开读；均值右偏时同看 `rel_median`。

### 2. profile_refresh —— 旧条件在最新窗口还过闸吗

每个画像每条 `selected_conditions` 给 `then`（画像窗口）vs `now`（滚到最新的窗口，只加长不平移）。

- `now.passes_guardrails=false`（尤其 `oos_balance` 明显腰斩，如 0.43→0.27）：条件在新数据上边际
  偏到了单段行情——**建议降级该条件为加分项，或从画像移除**；若整个画像的稳健骨架都塌，考虑
  重挖。判断写进周报，改画像由人确认。
- `now` 标 `collapsed`（样本塌到 min_n 以下）：该条件在新窗口几乎没有有效样本，最直白的衰减，别再
  倚重。
- `new_window_top_candidates`：新窗口里冒出来、比旧条件更稳的候选。值得纳入时，走
  `factor_mining.md` 的选解流程重挖确认，再更新画像——**不直接照抄进画像**。

### 3. recent_experiments —— 挖过什么、判没判

`verdict=未判` 的实验提醒你有挖矿结论还没落判。判分：

```bash
python3 scripts/factor_lab.py experiments --set-verdict <group>@<window>@<hash前缀> \
    --verdict adopted|rejected|observing --note "一句话理由" [--promoted profile@version]
```

脚本只改这三列，确定性列一律不动；重跑挖矿也不会抹掉你的 verdict。

## 跨技能因子（读 calibration/refresh 时留意）

`in_active_theme` / `days_since_forecast_ann` / `forecast_cum_yoy_med` 是 v1 跨技能因子，覆盖随
台账/主线数据累积才变厚。`meta.factor_coverage` 里 null>60% 的只在分层观察、没进叠加网格——它们
出现在 `factor_layers` 但不会出现在 overlay 条件里，别当成"没用"，是"还没够样本验证"。

## 周报写法（产出 `reports/factor_weekly_<asof>.md`）

按项目文风（讲人话、同项用 list、每条说完整话）：

1. **一句话体检结论**：这周画像整体是"稳"还是"有条件在衰减/校准失准"，样本够不够下结论。
2. **校准状态**：分 tier / 关键 condition 说承诺 vs 兑现，带 n 与 caveat；样本不足就直说"暂不足以下结论"。
3. **画像衰减**：列 then→now 有变化的条件（尤其转不过闸的），给维持/降级/刷新建议 + 理由。
4. **新候选与实验**：新窗口值得追的候选、待判实验。
5. **建议动作清单**：具体到"哪个画像哪条条件建议怎么动"，但**改画像、bump 版本由人确认**。

红线：不给买卖建议、不写买点/仓位/目标价；样本不足如实标注，不硬凑高信心结论。

## 调度（默认手动，跑顺几轮再谈自动）

先人工周检两三轮、确认判断稳定，再考虑定时。参考接线（不在本期实现）：

- macOS launchd：一个 `~/Library/LaunchAgents/*.plist`，周日晚跑 `factor_lab.py weekly` 后发通知。
- 或 Claude Code scheduled task：周频触发"跑 weekly 并读包写周报"。

自动化只负责"按时出体检包"；判维持/降级/刷新、改画像永远留在人这一环。
