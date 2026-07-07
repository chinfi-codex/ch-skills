# 批次 5 执行报告：Phase 5 weekly 一键 + 文档收尾（已完成）

执行者：Claude 直接执行。日期 2026-07-04。

## 产物

- 改：`scripts/factor_lab.py`（weekly 子命令 + 150KB 硬上限梯度截断 `_enforce_cap`）
- 新：`references/methodology/factor_lab.md`（周度体检读法 + 判断 rubric + 周报模板，约 115 行）
- 新：`references/cli_reference.md`（各脚本参数集中地）
- 改：`SKILL.md`（加周度体检指引，参数表挪走）
- 改：`scripts/run_daily_panel.py`（cleanup 清单加因子实验室 + 挖矿明细产物）

## weekly 实测

- 一条命令合并 refresh + calibrate + experiments → `reports/weekly_factor_pack_20260703.json`
- compact **14.7 KB**（≤150KB，无截断）；超限时按「校准例证→1 → refresh候选→3 → 清空例证」梯度砍，标 meta.truncated
- 摘要自动高亮衰减条件：discount_relaunch「前高折扣≥0.78∧换手≥5%」then过闸→now不过闸

## 验收（全过）

- [x] weekly 一条命令产出单一 pack ≤150KB（14.7KB compact）
- [x] SKILL.md 净变化 **9 增 26 删 = 净 −17 行**（远优于 ≤+20；git diff --stat 佐证），参数表已挪 cli_reference.md
- [x] `--cleanup` 实测：造 calibration/profile_refresh/weekly/factor_mining pack+detail/evidence + report → 清理后只剩 report md/html
- [x] 终验全套回归（方案 §5）：
  - factor_backtest 冒烟正常 + 自动登记
  - strategy_picks score（--asof）/ context 结构不变
  - 两画像过 _validate_profile（含 mining_spec）
  - render_report_html 渲染 report_20260626.md 成功（2.0MB，149 records，9 themes）
  - 全脚本 py_compile 通过

## 调度

factor_lab.md 末尾附 launchd / Claude Code scheduled task 接线示例（注释），**默认手动周检**，不在本期实现自动定时——判维持/降级/刷新、改画像永远留人这一环。
