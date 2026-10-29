# AlphaVault Proactive Agent：现状诊断与落地改进方案

日期：2026-10-29 · 依据：源码 `proactive_goal.py`（1090 行）、`主动目标状态.json`、`decisions.jsonl`（108 行）、Hermes 两个任务最近 50 次运行记录、`jobs.json`。

## 0. 结论

双段架构（无工具评估 + 确定性投递）方向是对的，安全边界、时效、版本校验做得扎实，不需要推倒重来。真正的问题在运行层：**同一提醒每天重发、44% 的评估批次被整批作废、投递结果没有闭环、状态里 86% 是死数据**。另外整套代码还没有进版本库。

改进路线是“保留骨架、修运行时、补反馈”，分四期：P0 当天止血，P1 两天修正确性，P2 三天做结构瘦身，P3 两周补产品闭环。

## 1. 实测现状（2026-10-27 11:58 → 10-29 11:00，约 47 小时）

| 指标 | 数值 | 说明 |
|---|---|---|
| 评估任务触发 | 50 次落盘，7 次真正唤醒模型 | gate 静默率约 86%，符合设计 |
| 评估批次处理 | 34 批：19 recorded / **15 invalid（44%）** | 见问题 P0-2 |
| 通知意图 | 6 条，全部停在 `attempted` | 没有一条确认送达，见 P0-4 |
| 其中“供需框架体检逾期” | **3 条**（10-27、10-28、10-29 各 1 条） | 同一事项天天提醒，见 P0-1 |
| 候选总数 | **778**（不是 340） | baseline 337 + backfill 332 = 669，占 86% |
| 状态体积 | 395 KB，其中 `candidates` 占 307 KB | |
| 单次唤醒 prompt | 55K 字符，trend 候选的 `rule_texts` 占 29K | 6 条规则 × 5000 字，每次原样重注入 |
| 评估→投递延迟 | 约 30 分钟 | 两个任务同为 `*/30`，投递总是处理上一轮评估 |

## 2. 问题清单

### P0：影响正确性或用户体验

**P0-1 同一事项每天重发。** `board_candidate()` 把 `overdue_days` 和 `review_excerpt` 放进证据，指纹按全量证据算，逾期天数每天加 1，于是每天生成新的 `candidate_id`，旧版本被 supersede，新版本又能通知一次。状态 policy 里已经有 `framework_review_reminder_days: 7`，但代码里没有任何地方读它。根因是**证据版本和提醒主题混为一谈**：现在只有 `native_id + evidence_hash` 一个维度，缺少“同一主题多久可以再提醒一次”的概念。

**P0-2 一项写错，整批作废，还会无限重试。** `record_batch()` 遇到任何一项不合格就抛异常，整批决策丢弃。15 次 invalid 里：
- 5 次空响应或非 JSON（`Expecting value`）；
- 2 次 `review_after` 填在非 defer 项上，2 次 defer 超过 `valid_until`，这些本可以确定性修正；
- 5 次“候选已完成或被取代”“未登记候选”，见 P0-3。

整批作废时 `attempts` 不加 1，所以 `max_decision_attempts` 拦不住。10-29 06:00、06:30、07:00 三次唤醒带的是同一批 3 个候选，每次 108 KB 运行记录，连续两次都因为 defer 越界被拒。

**P0-3 lease 比投递周期短，产生竞态。** 评估 lease 是 10 分钟，但评估结果要等下一个 `*/30` 投递 tick 才被处理。lease 过期后下一次 scan 会把同一候选再交给模型，两份评估先后到达，第二份就撞上“已完成或被取代”。

**P0-4 投递没有闭环。** `attempted` 是终点，没有回读 Hermes 的投递结果（`jobs.json` 里已有 `last_delivery_error` / `last_delivery_unverified`）。现在说不清 6 条提醒里有几条真的到了微信。

**P0-5 代码和协议未入库。** AlphaVault git 里 `proactive_goal.py`、协议、契约、测试、状态、ledgers 全部是 `??` 未跟踪，治理文件改动也未提交。

### P1：可靠性与成本

- **一个 tick 最多跑 3 次 `collect()`**：scan、`process_runs`、`dispatch` 各跑一次，每次都要起 `trend_state.py` 子进程查 PG，再重读重哈希 `events.jsonl`。
- **靠正则解析 Hermes 的 Markdown 运行记录**（`**Response Characters:**`）。Hermes 格式一变，`response_from_run()` 返回 `None` 直接 `continue`，系统会永久静默，而且不报错。
- **解释器没固定**：10-27 16:30 两个任务都因 `/usr/bin/env python3` 落到 CommandLineTools 的 3.9 报 SyntaxError。协议写了“解释器固定为已通过 psycopg2 预检的 Python”，wrapper 里没落实。
- **prompt 自相矛盾**：任务 prompt 要求“先阅读 27-主动目标协议.md”，但评估端没有工具，预检也没注入协议原文。
- **Hermes 输出目录只保留最近 50 份**，`process_runs` 只看最近 30 份。投递任务停摆超过约 15 小时，评估结果会被轮转掉，而且不留痕。

### P2：产品层缺口

- **没有反馈回路**：只能用 CLI `ack`，用户无法在微信里回“有用/噪音/这类别再提醒”，阈值无从校准。
- **数据源只有 5 类**（trend、catalyst、board、框架文件、固定窗口）。协议列出的 pool-tracker、业绩预告、季报、美股等产物都没接进来。
- **没有离线评估集**：packets 和 decisions 已经是现成的回放素材，但没被用来测评估质量。
- 固定复核窗口按周一到周五判断，没用交易日历，节假日会空转。
- 1090 行单文件，扫描、状态、校验、投递、CLI 全耦合在一起。

### 对你两点观察的补充

1. 状态膨胀：方向同意，但实际是 778 条，增长主力是 baseline 和 backfill，不是 superseded。只要把“终态候选”从主状态移到压缩索引，主状态可以降到 20 KB 以内（见 §3.3）。
2. 锁文件：`~/.hermes/proactive-locks/` 里那个空文件确实无害。另外 OneDrive 里还有一个 v1 时代的 `system/frameworks/主动目标状态.json.lock`（0 字节，已被 `.gitignore` 忽略），同样无害。协议禁止的是运行时自动清理，手动删不受限制。

## 3. 目标架构：保留骨架，补三个概念

```text
Source Adapters（trend / catalyst / board / framework / schedule / 后续扩展）
        ↓  每 tick 只采一次，生成 snapshot_id，scan 与 dispatch 共用
Candidate Store
  active.json（仅非终态）+ index.jsonl（native_id→最新 hash/终态，压缩游标）+ archive/
        ↓  有候选才唤醒
Evaluator（无工具，精简 packet）
        ↓
Validator（逐项校验；确定性字段自动修正；失败项单独计 attempts）
        ↓
Topic Gate（主题冷却 / 升级节奏 / 用户静音）  ← 新增
        ↓
Outbox 状态机 pending → attempted → delivered | failed | unverified  ← 补闭环
        ↓
Channel（local 影子 / weixin） → Feedback（微信回复 → ack / mute_topic / useful）  ← 新增
```

### 3.1 新增 `topic_key`：把“证据版本”和“提醒主题”分开

- 指纹只算语义字段，剔除派生值（`overdue_days`、`age_days`、excerpt 等）。
- 每个候选带 `topic_key`，例如 `framework_review:industry-board`、`trend:axis`、`catalyst:<industry>:<driver>`。
- Topic Gate 按主题执行冷却：同一主题在 `cooldown_days` 内只在“证据语义升级”时重发。框架体检建议按 0 / 7 / 14 天升级，之后转周度汇总。`framework_review_reminder_days` 就在这里落实。

### 3.2 逐项校验 + 确定性修正

| 情况 | 现在 | 改为 |
|---|---|---|
| defer 超过 `valid_until` | 整批拒 | 夹到 `valid_until`；若 `valid_until` 已过则转 silent，写审计 |
| 非 defer 带 `review_after` | 整批拒 | 忽略该字段，写 warning |
| 候选已终态或被取代 | 整批拒 | 跳过该项，其余照常入账 |
| 多于 1 项 notify | 整批拒 | 保留优先级最高的一项，其余降为 defer（+1 tick） |
| 空响应 / 非 JSON | 整批拒，不计次数 | 本批所有候选 `attempts += 1`，达上限转 dead 并本地告警 |

原则：**模型的判断（action、message）不改；格式和边界字段由确定性代码修正**。

### 3.3 状态瘦身

- `active.json`：只放 pending / defer / 未过期 outbox，预计 < 20 KB。
- `index.jsonl` 或 `index.json`：`native_id → {hash, terminal_action, at}`，去重只需要这一张表，340 条事件约 40 KB。
- 终态候选和 `processed_runs` 超过 7 天的部分滚动进 `ledgers/runs/proactive/archive/YYYY-MM.jsonl`。
- packets 按同样规则归档。延期恢复只依赖 active 里的条目。

存储介质有两种选择：

- **A（推荐先做）**：继续用 vault 里的 JSON，只做拆分。不动 `80-状态文件约定`，改动最小。
- **B（数据源超过 8 类再考虑）**：运行态挪到 `~/.hermes/alphavault-proactive/state.sqlite`（WAL），vault 里每日导出一份快照。好处是避开 OneDrive 每 30 分钟同步一次大文件和多端冲突副本，代价是要修订状态文件约定。

### 3.4 调度串联

- 投递任务改为 `5,35 * * * *`，评估→投递延迟从约 30 分钟降到约 5 分钟。
- lease 从“固定 10 分钟”改为“直到对应 run 被 dispatch 处理，或 45 分钟兜底”，消除 P0-3 的竞态。
- `dispatch` 增加健康检查：连续 N 次唤醒都解析不出 Response，或评估输出目录里存在未处理却即将被轮转的文件，就通过 `failure_deliver=local` 告警，不再静默。

### 3.5 Prompt 精简

- `rule_texts` 改为每条规则只注入 `status / preconditions / action / 例外` 四段，每条 ≤ 800 字，并带规则文件 hash。预计 trend 候选从 32K 降到约 6K 字符。
- 删除“先阅读协议”这句；把协议里评估者需要的 10 条硬约束内联进 SKILL.md 输出契约附近。
- catalyst 的 `update_history`、完整 `source_refs` 截短，只保留最近 2 次变更。

## 4. 落地计划

### P0 止血（当天，约 1 小时）

1. AlphaVault 提交一次：协议、代码、测试、契约、治理文件改动。先决定 `主动目标状态.json` 入不入 git：建议先 `.gitignore` 掉，由 §3.3 拆分后的小文件入库。
2. 固定解释器：`install_proactive_hermes.py` 生成的 wrapper 用 shebang 写死已通过 psycopg2 预检的绝对路径，并在 wrapper 开头断言 `sys.version_info >= (3, 10)`。
3. 临时止住框架体检重复提醒：`control mute` 不够精准，直接对当前 board 候选 `ack`，或等 P1-1 修复。

### P1 正确性（1–2 天，每项配测试）

| # | 改动 | 新增测试 |
|---|---|---|
| 1 | 指纹剔除派生字段；`topic_key` + 冷却；落实 `framework_review_reminder_days` | 逾期天数变化不产生新候选；7 天内同主题不重发 |
| 2 | 逐项校验与确定性修正（§3.2） | 混合批次中合法项入账；越界 defer 被夹住；空响应计 attempts |
| 3 | lease 绑定 run 处理；投递 cron 偏移 5 分钟 | 未处理 run 期间同候选不被重复呈现 |
| 4 | 投递回执对账：dispatch 下一轮读投递任务的 `last_delivery_error` / 输出，把 `attempted` 推进到 `delivered`/`failed`/`unverified` | 三种回执各一例；`attempted` 永不自动重发 |
| 5 | Response 解析失败的健康告警 | 格式变化时本地告警，不静默 |

### P2 结构（2–3 天）

1. 状态拆分 + 一次性迁移脚本（dry-run → apply，迁移前后计数对账）。
2. `collect()` 结果按 `snapshot_id` 缓存，一个 tick 内共享，PG 子进程最多 1 次。
3. Prompt 精简（§3.5），对比前后 10 次回放的决策一致性。
4. 拆模块：`sources/`、`store.py`、`validate.py`、`outbox.py`、`cli.py`，`proactive_goal.py` 只保留入口，保持命令行接口不变。
5. `status` 增加运行指标：近 24 小时唤醒数、invalid 率、平均 prompt 字符、各 topic 最近提醒时间、未确认投递数。

### P3 产品闭环（1–2 周，逐项影子验证后上线）

1. **反馈回路**：利用微信 `attach_to_session`，约定回复词（`有用` / `噪音` / `静音此类 7 天` / `已处理`），由交互会话调用 `proactive_goal.py feedback`，写入 `feedback.jsonl` 和 topic 静音表。
2. **回放评估**：用 packets + decisions 建 golden set（先人工标 30 条），`replay` 子命令对比新旧 SKILL.md / prompt 的决策差异。改 SKILL 前先跑回放。
3. **数据源扩展**：按 adapter 接口逐个接入 `a-stock-pool-tracker`、`a-stock-earnings-forecast`、`a-stock-quarterly-report` 的落盘产物。每个先 local 影子 3 个交易日，再进 weixin。
4. 交易日历替换周一到周五判断。
5. 低优先级候选（silent 但有 falsifier 的）进周度摘要，不占即时提醒额度。

## 5. 验收指标

| 指标 | 现状 | 目标 |
|---|---|---|
| 评估批次 invalid 率 | 44% | < 5% |
| 同主题冷却期内重复提醒 | 3 次 / 3 天 | 0 |
| 投递结果可确认率 | 0 / 6 | 100%（delivered / failed / unverified 三选一） |
| 主状态体积 | 395 KB | < 30 KB，且不随历史增长 |
| 单次唤醒 prompt | 约 55K 字符 | < 25K 字符 |
| 评估→送达延迟 | 约 30 分钟 | 约 5 分钟 |
| 用户反馈“有用”占比 | 无数据 | P3 后跟踪，目标 ≥ 60% |

## 6. 不建议做的事

- 不给评估端开工具或 MCP，零工具是这套设计最值钱的安全属性。
- 不让模型自己管理冷却或去重，这些继续由确定性代码负责。
- 不在 P1 之前接新数据源。现在每多一个源，重复提醒和 invalid 问题就多放大一层。
