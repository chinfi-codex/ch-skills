# Proactive Agent v2：内容收敛 · 统一唤醒 · Prompt 重设计

日期：2026-10-29 · 接续 `proactive-agent-改进方案-20261029.md`（P0–P3 问题清单）。本文回答三件事：东西放哪、怎么被叫醒、推送写成什么样。

> **进度（2026-10-29）**：§1 项目侧已落地并提交（目录、配置、契约、运行时、安装器、迁移脚本、52 项测试），线上切换按 `proactive-agent-切换手册.md` 执行；§3.3 的 SKILL.md v0.4 已上线；§2 与 §3.5 待做（P2）。

---

## 1. 内容收敛：项目归项目，金库归金库

### 1.1 归属原则

一条规则判断归属：**这个东西换掉 Agent 实现后还有意义吗？**

- 有意义的是用户的知识和方法论（交易系统、R-XXX、框架、催化台账、Wiki），留在 AlphaVault。
- 没意义的是 Agent 自己的协议、代码、契约、状态、审计、Prompt，归项目。

现在的问题是第二类大量落在金库里：协议 `27-主动目标协议.md`、状态契约、`proactive_goal.py`、安装器、测试、395 KB 运行状态、1.5 MB packets 和决策台账都在 `1-AlphaVault/`，还要求金库的协议清单、能力注册表、契约清单跟着登记。结果是金库被运行时数据每 30 分钟写一次（还在 OneDrive 同步目录里），项目代码却没有版本管理。

### 1.2 目标布局

```text
ch-skills/ch-proactive-agent/            ← 唯一可信源（git，经 skill_sync 分发）
├── SKILL.md                             评估 Prompt（评估端唯一能看到的文本）
├── config/
│   ├── goals.yaml                       长期目标定义：目标、路径、方法论引用、授权边界
│   ├── policy.yaml                      默认频控、时效、活跃时段
│   ├── sources.yaml                     数据源适配器与触发方式登记（见 §2.4）
│   └── schemas/
│       ├── state.schema.json            原 主动目标状态契约.schema.json
│       └── decision.schema.json         评估输出契约（与 SKILL.md 由测试互相校验）
├── scripts/
│   ├── proactive_goal.py                运行时与 CLI（P2 再拆成 sources/ store validate outbox pulse signal）
│   ├── proactive_config.py              配置加载与基础 I/O，替代对金库 _common 的依赖
│   ├── install_hermes.py                安装器，生成 wrapper 与 Hermes 任务（P2 加 launchd watcher）
│   └── migrate_from_vault.py            运行态迁移（dry-run 默认，只复制不删除）
├── references/
│   ├── protocol.md                      原 27-主动目标协议.md，作为项目协议
│   └── decision_method.md               给人和交互会话看的判断说明
└── tests/                               不随 skill_sync 同步

~/AlphaData/proactive/                   ← 运行态（本机磁盘，不进 OneDrive，不进 git）
├── state.json                           仅活跃项（或 state.sqlite）
├── index.json                           native_id → 最新 hash 与终态（压缩游标）
├── signals.jsonl                        唤醒信号收件箱（§2）
├── packets/  decisions.jsonl  receipts.jsonl  archive/
└── locks/

~/.hermes/                               ← 宿主，只放安装器生成的薄壳
├── scripts/alphavault-proactive-{pulse,evaluate}.py
└── cron/jobs.json                       两个任务（§2.3）

1-AlphaVault/                            ← 只读数据源，加一页接入说明
└── system/frameworks/外部Agent接入.md    说明主动 Agent 读什么、写什么、协议在哪
```

`~/AlphaData/` 已存在（`ALPHA_SQLITE_DIR` 用的就是 `~/AlphaData/db`），运行态放在这里与现有数据目录约定一致。

### 1.3 迁移映射

| 现位置（AlphaVault） | 去向 | 说明 |
|---|---|---|
| `system/frameworks/27-主动目标协议.md` | `ch-proactive-agent/PROTOCOL.md` | 金库留一页接入说明，链接到项目 |
| `契约/主动目标状态契约.schema.json` | `contracts/state.schema.json` | |
| `system/tools/proactive_goal.py` | `scripts/proactive_goal.py` | 去掉对金库 `_common` 的依赖，或把需要的 5 个函数随包内置；金库路径改由 `ALPHAVAULT_ROOT` 或 `config/goals.yaml` 给出 |
| `system/tools/install_proactive_hermes.py` | `scripts/install_hermes.py` | |
| `system/tools/tests/test_*proactive*` | `tests/` | |
| `system/frameworks/主动目标状态.json` | `~/AlphaData/proactive/state.json` | 迁移时做 §3.3（上一份文档）的瘦身 |
| `ledgers/runs/proactive/*` | `~/AlphaData/proactive/` | 金库如需留痕，每天导出一份精简日报到 `ledgers/runs/proactive/digest/YYYY-MM-DD.md` |
| `能力注册表` 中的 `proactive_goal.py` / `install_proactive_hermes.py` | 删除 | 它们不再是金库工具 |
| `能力注册表` 中的 `long_task_skill_outputs` | 保留，改挂到“外部 Agent 接入” | 这是金库对外的数据授权，归金库 |
| `trend_state.py`、`query_source_registry.py` | 原地不动 | 它们是金库的原子工具，项目按登记方式调用 |
| `AGENTS.md`、`00-治理总则.md` 中的主动目标条目 | 缩成一行指针 | 改完运行 `compile_protocol_contracts.py` 和 `--check` |

### 1.4 发布链路

```text
改 ch-skills → 测试通过 → git commit → post-commit hook 跑 skill_sync
→ ~/.hermes/skills/ch-proactive-agent/ 更新 → Hermes wrapper 从该副本运行
```

- wrapper 指向 Hermes 的技能副本，而不是开发目录。commit 天然就是发布闸门，未提交的改动不会上线。现在 Hermes 副本是拷贝模式（不是软链），这一点已经成立。
- 安装器把解释器写死成已通过 psycopg2 预检的绝对路径，wrapper 开头断言 Python ≥ 3.10。
- 副作用：`scripts/` 会被一起同步到另外 7 个 Agent 目录。无害，但会占位置；如果介意，可以给 `skill-sync.yaml` 加按目标排除。

### 1.5 已定：长期目标放项目

`config/goals.yaml` 放在项目内（2026-10-29 用户确认）。仓库约定的发布目录只收 `SKILL.md`、`scripts/`、`references/`、`assets/` 加既有的 `config/`，所以协议放在 `references/protocol.md`，契约放在 `config/schemas/`，没有另起 `PROTOCOL.md` 和 `contracts/`。

---

## 2. 统一唤醒：定时和事件走同一条流水线

### 2.1 核心原则

**触发器只投递“信号”，不直接唤醒模型。** 不管是定时、文件变化、数据库更新还是手动，进来的都只是“某处可能变了”。真正决定要不要叫醒模型的，仍然是确定性扫描加五道闸。这样新增一种触发方式，不会新增一条绕过去重、时效和频控的通道。

```text
┌─ 触发器 ───────────────────────────────────────────────┐
│ cron 心跳 │ 固定窗口 │ 文件变化 │ PG 变更 │ Skill 完成回执 │ 手动 │
└────────────────────────────┬───────────────────────────┘
                             ↓  proactive_goal.py signal --source <id> [--ref ...]
                 signals.jsonl（append-only 收件箱）
                             ↓
          Pulse（Hermes no-agent，每 2 分钟 + 被信号即时踢醒）
            1 合并信号：按源防抖（安静 3 分钟或最长等 10 分钟）
            2 只扫被点名的适配器 → 语义 diff → 五道闸前的确定性过滤
            3 有候选 → 写 ready_batch.json → hermes cron run <评估任务>
            4 处理已完成的评估输出 → 逐项校验 → outbox
            5 投递到期意图 → hermes send --json → 记回执
                             ↓
          评估任务（Hermes agent，零工具，不再自带定时扫描）
            pre-script 只读取 ready_batch.json；没有就 wakeAgent=false
```

### 2.2 触发器一览

| 触发器 | 机制 | 延迟 | 说明 |
|---|---|---|---|
| 心跳 | Pulse 自身每 2 分钟一次 | ≤ 2 分钟 | 兜底：对登记的文件和表算水位（mtime+size / `max(id)`），漏掉的事件在下一次心跳补上 |
| 固定窗口 | Pulse 到 08:35 / 15:40 时自产 `scheduled` 信号 | 准点 | 改用交易日历判断，节假日不触发 |
| 文件变化 | launchd `WatchPaths`（`ai.alphavault.proactive-watch.plist`），触发后执行 `signal --source fs` | 秒级 | 只监听 `sources.yaml` 登记的路径。OneDrive 从其他设备同步下来的改动也会触发，靠内容 hash 过滤空写 |
| PG 变更 | 关键表加触发器：写 `alpha_data.change_log` 并 `pg_notify('alpha_change', …)` | MVP ≤ 2 分钟；升级后秒级 | MVP 由 Pulse 轮询 `change_log` 水位，不需要常驻进程；需要秒级再加 LISTEN 守护进程（launchd KeepAlive） |
| Skill 完成回执 | 长任务 Skill 收尾时执行一行 `proactive_goal.py signal --source skill:<name> --ref <产物路径>`；接入 `skill_runtime` 的 Skill 直接监听其 `receipts.jsonl` | 秒级 | **推荐作为长任务产物的主信号**：它表示“报告已定稿”，比文件写到一半就触发更准，也避免评估半成品 |
| 手动 | 交互会话执行 `signal --source manual --reason "…"` | 秒级 | 例如刚手动更新了某个框架 |

不建议用 `hermes webhook`：它要开 HTTP 入口和 HMAC 密钥，而所有触发源都在本机，`hermes cron run` 已经够用。

### 2.3 Hermes 任务调整

| 任务 | 现在 | 改为 |
|---|---|---|
| 评估 `bba5ba3f1faf` | `*/30`，pre-script 自己扫描 | 由 Pulse 用 `hermes cron run` 触发；保留每小时一次的低频兜底；pre-script 只读 `ready_batch.json`。prompt 换成 §3.4 的短版 |
| 投递 `835e252c3d65` | `*/30` no-agent，stdout 即消息 | 改造成 Pulse：`every 2m` no-agent。内部用 `hermes send -t weixin --json` 投递并记录回执，stdout 只在投递失败时输出本地告警 |

关于 `hermes send`：现在协议写着“`proactive_goal.py` 不直接调用任何消息 API”。`hermes send` 走的仍是 Hermes 网关，没有模型参与，还能拿到 JSON 回执，可以一并解决“`attempted` 永远停在原地”的问题（上一份文档的 P0-4）。这需要你同意修改协议里这一句；不同意的话，保留 no-agent stdout 投递，回执改为读 `jobs.json` 的 `last_delivery_error`。

`hermes cron run` 的语义是“下一个调度 tick 执行”，当前调度器心跳约 14 秒。上线影子期要实测一次触发延迟。

### 2.4 数据源适配器登记（`config/sources.yaml`）

触发方式和扫描逻辑登记在同一处，新增数据源只改这里加一个适配器文件：

```yaml
sources:
  catalyst:
    adapter: proactive.sources.catalyst
    watch:
      files: ["ledgers/catalyst-events/events.jsonl"]
    debounce: {quiet_seconds: 180, max_wait_seconds: 600}
    freshness: {max_age_days: 7}
    topic_key: "catalyst:{industry}:{driver}"
    default_priority: P2

  trend_state:
    adapter: proactive.sources.trend
    watch:
      pg_tables: ["<trend_state 依赖的日线/融资表>"]
      skills: ["a-stock-daily-market-sense"]
    debounce: {quiet_seconds: 120, max_wait_seconds: 300}
    topic_key: "trend:axis"
    default_priority: P1

  framework_review:
    adapter: proactive.sources.board_review
    watch:
      files: ["ledgers/industry-board/board.json", "ledgers/industry-board/board_history.jsonl"]
      schedule: daily@08:35
    topic_key: "framework_review:industry-board"
    cooldown_days: 7
    default_priority: P3

  framework_change:
    adapter: proactive.sources.framework_files
    watch:
      files: ["wiki/04-交易方法论/**/*.md", "system/frameworks/*框架.md"]
    debounce: {quiet_seconds: 600, max_wait_seconds: 3600}
    topic_key: "framework_change:{path}"
    default_priority: P3

  # 后续按影子验证逐个接入：pool_tracker / earnings_forecast / quarterly_report
```

安装器读这份登记，生成 launchd `WatchPaths` 列表、PG 触发器 SQL 和 Pulse 的水位清单。三者同源，不会漂移。

### 2.5 防护规则

- **夜间**（23:00–06:00）：信号照收，只入队，不扫描，不唤醒。早上第一个 Pulse 统一处理，时效规则照常生效，过期的不补发。
- **信号风暴**：Skill 批量写文件时，靠防抖合并成一次扫描。另设硬上限：每小时最多 4 次评估、每天最多 30 次，超出的信号顺延到下一个窗口，并写一条本地告警。
- **事件唤醒不提速投递**：最小间隔、每日上限、topic 冷却全部仍在投递段执行。事件驱动只缩短“发现”延迟，不增加打扰次数。
- **扫描范围收窄**：信号带 `source`，Pulse 只跑被点名的适配器。心跳兜底时才跑全量水位比较，而且只比水位，不读正文。
- **可观测**：`status` 增加每个源的最近信号时间、最近扫描时间、合并次数、被限流次数。

### 2.6 延迟对比

| 场景 | 现在 | v2 |
|---|---|---|
| 催化台账新增 A 级事件 → 微信 | 30–60 分钟 | 约 5–8 分钟（3 分钟防抖 + 约 1 分钟评估 + ≤ 2 分钟投递） |
| 盘后趋势读数更新 → 微信 | 下一个 `*/30` 加一轮投递 | Skill 回执即时触发，约 3–5 分钟 |
| 无变化时的模型调用 | 0（gate 拦截） | 0（不变） |

---

## 3. Prompt 重设计

### 3.1 实际推送的 6 条有什么问题

| 问题 | 出现次数 | 例子 |
|---|---|---|
| 内部编码直接给用户看 | 6/6 | S2-hbm、E2-asic、R-018/R-016/R-010/R-011、CE-20261026、board.json、board_history.jsonl、lead_order、equation_reading、stance |
| 第一句不是结论 | 5/6 | “存储涨价传导链首末端闭合，值得复核周期仓位框架” |
| 一大段，没有分行 | 6/6 | 微信里是一整块 |
| 免责套话占篇幅 | 4/6 | “不构成任何具体调仓指令”“无实际持仓数据”“仅提醒复核” |
| 同一主题重复推送 | 3 条 | 供需框架体检连发 3 天，每次措辞不同，看起来像新事 |
| 一条塞多个主题 | 1 条 | 苹果涨价 + 博通 + TPU + 国产推理 + InP |
| 旧事件写成新事件 | 1 条 | 10-27 推送 09-04 的事件，写作“本轮新增”（事件已 53 天，早期版本的时效闸没拦住） |

### 3.2 两个结构性发现

1. **`references/decision_method.md` 评估端从来没看到过。** 评估端零工具，Hermes 只把 `SKILL.md` 全文注入 prompt（运行记录可以确认）。所有判断例子都必须写在 `SKILL.md` 里。
2. **Hermes 任务里还有一份 prompt**，内容和 SKILL 重复，还要求模型“先阅读 27-主动目标协议.md”，可评估端读不到任何文件。两份指令打架，而且这一份不受 git 管理。

### 3.3 新 SKILL.md（已写入 `ch-proactive-agent/SKILL.md`，v0.4.0）

主要变化：

- **角色**从“证据评估器”改为“投研值班员”，明确读者是在微信上读消息的用户本人。
- **五道闸**：时效 → 真增量 → 落到方法论的具体位置 → 用户能做什么 → 紧迫度。每道都有不过就 silent 或 defer 的明确出口，`reason` 要写卡在哪一道。
- **P1/P2/P3 优先级**，P3 同主题 7 天内不重复。
- **固定六行消息结构**：标题 / 发生了什么 / 变化在哪 / 对你 / 失效 / 来源。总长 150–400 字，标题 ≤ 28 字。
- **禁用清单**：内部标识、免责套话、交易指令、过程描述。规则一律写“编号 + 名称”。
- **示例改写**：用 10-27 实际发出的趋势提醒做“差 → 好”对照；09-04 旧事件示范应选 silent；框架体检示范以可直接回复的选项结尾。
- **输出契约**新增 `priority`、`topic_key`，其余字段不变。现有投递器会忽略新字段，所以**不改代码也能直接上线**。

### 3.4 Hermes 任务 prompt 换成短版

```text
[alphavault-trading-fundamental-watch]
按已加载的 ch-proactive-agent 完成本轮无工具评估。下方是预检注入的候选包；只输出 SKILL 规定的 JSON 对象。
```

规则只保留在 SKILL.md 一处。这一段由安装器写入，不要手改 `jobs.json`。

### 3.5 下一步：把“判断”和“排版”分开（代码改动，排在 P2）

只靠 prompt 约束格式，总会偶尔漏。更稳的做法：

- **模型只给内容字段**：`brief = {headline, what_changed, delta, so_what, falsifier, sources}`。
- **投递器负责排版**：按模板渲染；事件日、评估日、优先级标签取自候选包而不是模型，旧事件就不可能被写成“今日”。
- **预检注入两个辅助字段**：
  - `glossary`：编码 → 人话，例如 `S2-hbm → HBM 供给`、`R-016 → 退潮减仓`，从规则标题和 board 节点名确定性生成；
  - `recent_notifications`：各 topic 最近推送时间和标题，供第 2 道闸判断“用户已经知道”。
- **投递前 lint**：命中 `\b[SE]\d-[a-z]+`、`CE-\d{8}`、`\.jsonl?\b`、免责套话时退回改写或降级为 defer，并写审计。

### 3.6 怎么验证新 prompt

用现有 7 次真实唤醒的 packets 回放（上一份文档 P3 的 `replay` 子命令，没做之前可以手工喂给模型）：

- 决策层：09-04 旧事件应为 silent；三次框架体检只应 needs_user 一次。
- 文本层：0 个内部编码、0 条免责套话；首行 ≤ 28 字且包含方向；总长 ≤ 400 字。
- 先在 `deliver=local` 影子下跑 2 个交易日，再切回微信。

---

## 4. 实施顺序（合并上一份文档）

| 阶段 | 内容 | 依赖 |
|---|---|---|
| **P0 当天** | 金库提交现状；固定解释器；**同步 v0.4 SKILL.md**，换 Hermes 短 prompt | 无。SKILL 向后兼容 |
| **P1 1–2 天** | topic_key + 冷却；逐项校验；lease 修正；投递回执 | — |
| **P1.5 1–2 天** | **内容收敛迁移**：代码、协议、契约、测试搬进 `ch-proactive-agent`；运行态迁到 `~/AlphaData/proactive/`；金库注册表瘦身并重新编译 | P1 完成后搬，避免边修边搬 |
| **P2 2–3 天** | Pulse + signals 收件箱 + 适配器登记；心跳水位兜底；评估改为按需触发；判断与排版分离、glossary、lint | P1.5 |
| **P2.5 1 天** | 事件触发器：launchd WatchPaths、Skill 完成回执（先接 daily-market-sense 和催化摄取）、PG `change_log` 触发器 | P2 |
| **P3 1–2 周** | 反馈回路（微信回复）、回放评估、新数据源逐个影子接入、LISTEN 秒级升级（按需） | P2.5 |
