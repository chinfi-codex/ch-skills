# 外部 Agent 接入

本页说明哪些外部 Agent 会读取 AlphaVault、能读什么、不能做什么。外部 Agent 的协议、代码、状态都不在金库里，以各自项目为准。

## 主动目标 Agent（ch-proactive-agent）

- **项目位置**：`ch-skills/ch-proactive-agent/`。协议见其中 `references/protocol.md`，长期目标见 `config/goals.yaml`。
- **运行方式**：Hermes 定时唤醒，零工具评估，确定性投递微信提醒。运行态在本机 `~/AlphaData/proactive/`。
- **读取范围**（只读）：
  - 方法论：`wiki/04-交易方法论/` 下的交易系统总纲、仓位体系、买点体系和 `经验法则/R-*.md`，以及 `system/frameworks/` 下的相关框架与协议；
  - 台账：`ledgers/catalyst-events/events.jsonl`、`ledgers/industry-board/board.json` 与 `board_history.jsonl`；
  - 原子工具：`system/tools/trend_state.py`、`system/tools/query_source_registry.py`；
  - PostgreSQL 中已有的行情、主题状态、财报、股票池等数据；
  - 能力注册表 `long_task_skill_outputs` 所列长任务 Skill 落在金库或 PG 中的产物。
- **不写金库**：不写 Wiki、方法论、台账或状态文件，不调用长任务 Skill，不开通外部数据。
- **授权**：向用户发送本目标范围内的提醒，已由用户于 2026-10-27 授权。其余有副作用的动作，按金库各自协议单独授权。

方法论文件的增删改会被该 Agent 视为“目标需要重新对齐”的信号，但它不会自行修改任何方法论。
