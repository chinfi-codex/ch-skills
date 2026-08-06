---
framework_version: geopolitics-v2-wartime
profile: geopolitical_daily
regime_assumption: 中东能源战时 regime — 霍尔木兹/海湾能源通道进入实弹化封锁 + 美伊直接军事对抗成为主导风险，其余区域降为并行战线
supersedes: geopolitics-v1-global-risk
activated_on: 2026-07-25
activated_by: activation_triggers A（霍尔木兹实弹封锁≥5交易日、Kpler 确认零通行）+ B（美伊第27波持续规模交火）决定性命中；path A 连续 13 天=0、D=69 烈度钉死 10
---

# geopolitical_daily 分析框架 · v2 战时专框

> 本文件是 `geopolitical_daily` 的分析框架唯一可信源。机器区定义脚本校验用的 frame schema、regime 失效触发器和输出板块；模型区定义战时的战争轨迹分型、主/并行战线、能源与市场重定价传导和写作纪律。
> 跨框架不变的方法论常量见同目录 `methodology.md`；活动状态机制见 `../watchboard.md`；跨资产传导框架见 `cross_asset_impact_framework.md`。
> **换代记录**：2026-07-25 由慢思考复核 + 人批准激活，接替 v1（geopolitics-v1-global-risk，已归档为 `framework_v1_global-risk.ARCHIVED.md`）。激活当日的 report_state 按文末 `migration_plan` 迁移 v1 最后一期 open 项、并写 `framework_switch` 留痕。

---

## 为什么是 v2：v1 在战时会失配在哪

v1（全球多点摩擦期）把 A/B/C/D 建成"从缓和到系统冲击"的**单次激励阶梯**，基线假设是"多区域低烈度摩擦、偶发升级"。一旦某条主线锁进持续实弹化封锁 + 大国直接对抗，基线本身变了：

- **path 分型方向反了**：v1 的正常态是 A/B（缓和/可控摩擦），D 是极端例外。战时正常态是"交战中"，要跟踪的是**战争轨迹**（封锁执行→直接对抗→多战线→核/系统阈值，以及怎么收场），而不是"离缓和多远"。2026-07-12 起 path 连续 13 天钉在 D、path A 概率连续 13 天为 0，就是 v1 分型极点塌陷的实证。
- **单个 sub_branch 扛不住**：v1 只有一个 path 位，战时要同时 track 主战线烈度 + 各能源通道封锁状态 + 并行战线 + 升级阶梯位置 + 核阈值——这些在 v1 里只能挤进 sub_branch 自由字段（换代前已膨胀到 90-196 字符，命中 governance §9 头号反模式）。
- **传导重点变了**：v1 假设"风险溢价为主、实际中断为辅"；战时是"实际供给中断为主、溢价为辅"，且新增核阈值、大国直接介入两条 v1 没有的传导渠道。

---

## 机器区 — 脚本只解析下面这一个 yaml 块

```yaml
frame:
  path:              {required: true, enum: [W1, W2, W3, W4, De]}   # W1 封锁执行与代理战 / W2 美伊直接军事对抗 / W3 多战线扩大战 / W4 核阈值与系统性战争 / De 去升级与停火重建
  primary_theater:   {required: true}                               # 当前主战线，如 Hormuz-energy-war / US-Iran-direct
  war_theaters:      {required: true, type: list}                   # 主战线 + 并行战线（俄乌、印太台海等），每条标 active/degraded/frozen
  chokepoint_status: {type: map}                                    # {Hormuz, Bab-el-Mandeb, Black-Sea, Suez} 各自 open / degraded / blockaded
  escalation_rung:   {}                                             # 升级阶梯位置 1-10，10=大国直接开战/核使用
  belligerent_weights: {required: true, type: list}                 # 今日最能改变战争轨迹的交战方/联盟
  transmission_channels: {type: list}                               # energy / shipping / nuclear / great_power / sanctions / market
  signal_watchlist:  {type: list}                                   # 24-72h 核心观察信号
  probabilities:     {type: map, sum: 100}                          # {W1, W2, W3, W4, De}，和=100

# v2 的死亡条件：命中即由慢思考层评估"是否再换代"（回退 v1 / 进 v3 / 换非地缘框架）
invalidation_triggers:
  - 主要战线达成持续停火 + 能源通道恢复常态通行 ≥ 若干周 → regime 回落，评估退回 v1 全球多点摩擦框架或建 v3 后战时重建框架
  - 战争扩展为全球大国集团直接交战（北约-俄、或中美因台海直接卷入） → 单一中东战时框架不够，评估 v3 大国集团战框架
  - 全球风险主因从地缘战争切换为金融/经济危机或非地缘冲击 → 评估换非地缘框架

output_sections: [战况速览, 主战线, 并行战线, 能源航运与市场重定价, 升级阶梯与核阈值, 本期变更]
```

---

## 模型区 — 战时判定逻辑与写作要点

### path 判定逻辑（战争轨迹，不是离缓和多远）

| 路径 | 判定主标志 | 排除证据 |
|---|---|---|
| **W1 封锁执行与代理战** | 能源通道进入实弹化封锁/黑航，交战以封锁执行、代理人（胡塞等）袭击、tit-for-tat 打击为主，尚无大国地面/海空直接会战 | 封锁被解除或降为口头威胁 → 评估退回 v1 的 C/D |
| **W2 美伊直接军事对抗** | 主要交战方（美伊）进入直接军事交火——强行通航/护航开火、成规模互相打击本土目标 | 打击仍是单发报复、未成持续交火链 |
| **W3 多战线扩大战** | 主战线 + ≥1 并行战线（俄乌、印太台海等）同时进入高烈度，能源/航运多通道同步中断，联盟成建制卷入 | 并行战线仍是独立低烈度，未与主战线形成同步升级 |
| **W4 核阈值与系统性战争** | 核设施被实际打击并确认放射后果、核使用威胁进入可信部署、或大国直接开战造成全球供应/金融系统性重估 | 核风险仍停留在表态，未改变部署与价格 |
| **De 去升级与停火重建** | 出现停火文本、通道恢复、撤军、第三方调解落地执行，战争轨迹掉头向下 | 只是战术间歇，无机制化降级文本 |

**判定纪律：**
- path 只在战争轨迹发生**方向性**变化时切换；单日战报密度高不等于进档。
- `escalation_rung`（1-10）承接 v1 烈度评级的连续量感；path 是离散阶段，rung 是连续读数，两者并存。
- 核阈值（W4）必须有**放射后果确认或可信核部署**，IAEA/官方口径未确认前压在 W3 + falsifier，不擅自进 W4。

### 主战线与并行战线

- **主战线（primary_theater）**：当前吸走最多军事/能源/避险定价的那条线。2026-07 的默认主战线是 **Hormuz-energy-war**（霍尔木兹封锁 + 美伊对抗）。
- **并行战线（war_theaters 里非 primary 的）**：独立上推但尚未与主战线同步的战线，战时**仍要单独 track**（v1 的多区域扫描在这里保留为"并行战线"块）。当前两条：
  - **俄乌-黑海（RUUA-Black-Sea）**：CPC 里海终端暂停 + 黑海输油暂停 + 敖德萨夜袭 + EU 第21轮制裁锁定影子船队，能源通道第二封锁点在成形，是 W3 的核心触发变量。
  - **印太-台海（Indo-Pacific-Taiwan）**：换代前从 v1 尾期冒出的第三战线（台海实弹演训 + 南海仁爱礁摩擦），当前独立、低-中烈度，单独 track；若与中东主战线形成大国同步升级（尤其中美直接卷入）→ 触发 W3 及 v3 大国集团战评估（见 invalidation_triggers 第 2 条）。
- **通道状态（chokepoint_status）**：霍尔木兹 / 曼德海峡 / 黑海 / 苏伊士 各自 open / degraded / blockaded，是能源航运传导的结构化底座。

### 升级阶梯与核阈值（v2 新增核心块）

战时最需要一条清晰的**升级阶梯**，把"再往上一格是什么"写死，避免每天在散文里临时判断：

1-2 封锁威胁/口头 · 3-4 封锁执行（黑航/收费）· 5-6 实弹化封锁 + 代理人袭击 · 7 强行通航/成规模互击本土 · 8 联盟成建制卷入/多通道中断 · 9 核设施被实际打击 · 10 核使用/大国全面开战。

每期给 `escalation_rung` 读数 + "再上一格的触发事件是什么" + "什么会让它掉一格"。

### 传导重点（战时两套：实际中断为主 + 两条新渠道）

- **能源/航运**：战时以**实际供给/通道中断**为主判据（v1 是溢价为主），看 Kpler 黑航、AIS 过境数、保险费率、绕航，而非只看 Brent 点位。
- **核渠道（新）**：核设施打击、放射监测、核部署——直接进避险与全球风险重估，不走常规能源链。
- **大国直接介入渠道（新）**：美/俄/中是否从"支援/护航"升级为"直接交火"，是 W2→W3→W4 的分水岭。
- **市场重定价**：战时价格是"事实的滞后确认"而非领先信号；亚洲/欧洲风险资产真金白银定价（如韩股熔断、德债短端、Brent 破百后回落）是"从地缘推演过渡到市场重定价"的关键证据，单独 track。

### watchlist 起点

1. 主战线能源通道封锁状态是否升级（黑航→实弹化→强行通航）。
2. 主要交战方是否从间接/护航升级为直接持续交火。
3. 并行战线（俄乌、印太台海）是否与主战线形成同步升级。
4. 核设施打击与放射后果、核部署可信度。
5. 停火/调解文本是否出现（De 路径信号）。
6. 能源实际中断、避险与风险资产的同步重定价（市场确认）。

### 各输出板块写作要点

- **战况速览**：path（W）、escalation_rung、主战线、今日 1-3 条真正改变战争轨迹的边际、一句话判断。
- **主战线**：能源通道 + 美伊对抗为核心，展开封锁状态、直接交火、升级阶梯位置。
- **并行战线**：俄乌、印太台海等独立战线，标 active/degraded/frozen + 是否向主战线靠拢。
- **能源航运与市场重定价**：实际中断优先、通道状态表、市场确认（不给交易建议）。
- **升级阶梯与核阈值**：rung 读数 + 上下一格触发事件 + 核阈值 falsifier。
- **本期变更**：逐条结算上一期 open 项；换代日按 `migration_plan` 处理 v1→v2 的迁移留痕（`framework_switch` 字段）。

---

## activation_triggers（激活门槛 —— 2026-07-25 已命中，本框架已生效）

预置于 2026-07-15，激活于 2026-07-25。任一命中即由慢思考层复核 + 人批准激活，命中情况：

- **A. 主战线锁死** ✅ **命中**：霍尔木兹实弹化封锁持续 ≥ 5 个交易日且 Kpler/AIS 确认通行实质中断。T-050 母题：7/13 Kpler 确认封航执行 → 7/21 Kpler 确认零 VLCC 通行，跨约 11 个日历日。
- **B. 美伊直接交火** ✅ **命中**：美伊成规模、持续的直接军事交火。T-035：伊朗"第27波"多国同步实弹打击美军海湾基地、美军连续多晚打击伊朗本土，已成持续交火链。
- **C. 第二系统冲击点** 🟡 **成形中**：俄乌-黑海第二能源通道封锁在成形（CPC 暂停 + 哈萨克黑海输油暂停 + EU 第21轮制裁锁定影子船队），尚未与中东主战线完全同步升级；作为 W3 触发变量继续 track。

> A 或 B 命中 → 进 W1/W2，中东单战时框架成立；C 若坐实 → 考虑 W3 / 是否需要更宽的 v3 多战线框架。印太-台海第三战线若与主战线大国同步 → 直接触发 v3 评估。

---

## 状态迁移计划（v1 → v2，换代日 2026-07-25 执行，不断历史）

换代日对 v1 最后一期（2026-07-24）的每个 open 跟踪项三选一裁决，一个都不许凭空消失。以下按 v1 最后一期实际 13 条 open 项裁决：

**promote（旧项在战时升为核心主战线）：**
- `T-050` 霍尔木兹关闭状态母题 → 战时主战线（能源通道封锁），links_to 改指 `path W / primary_theater=Hormuz-energy-war / chokepoint_status.Hormuz=blockaded`。
- `T-035` 美伊互相打击与海湾外溢母题 → 战时主战线（美伊直接对抗），links_to 改指 `great_power / escalation_rung`。
- `T-045` 核设施被瞄准母题 → 升为核阈值核心跟踪，links_to 改指 `nuclear / path W4 falsifier`。

**migrate（战时仍 relevant，保留 id/opened，只改 links_to）：**
- `T-038` 曼德海峡/胡塞/红海 → 第二能源通道（chokepoint Bab-el-Mandeb / Suez 绕航）。
- `T-040` 信源三层降级 → 战时更依赖单源高密度，coverage 跟踪照留。
- `T-045` 已在 promote。
- `T-049` 俄乌亚速海/黑海烈度母题 → **并行战线（war_theaters: RUUA-Black-Sea）**，W3 触发变量。
- `T-062` 多节点能源断点母题（CPC/霍尔木兹/黑海/红海/卡塔尔LNG）→ 能源航运实际中断母题，links_to `energy / chokepoint_status`。
- `T-063` 调解方美伊停火10天提议 → De 路径信号（diplomacy / 停火文本 falsifier）。
- `T-064` 地缘硬事实 vs 亚洲市场背离 falsifier → 市场重定价渠道（market 确认）。
- `T-066` EU 第21轮制裁锁定影子船队 → sanctions/shipping 物理管道约束，并入俄乌并行战线证据。
- `T-067` Lavrov-Rubio 马尼拉会面 → 并入 T-049 俄乌并行战线（great_power 外交渠道）。

**promote 为新并行战线（v1 尾期冒出、v2 结构化承接）：**
- `T-065` 印太/南海摩擦（仁爱礁 + 中方导弹试射）+ `T-068` 台海实弹演训 → 合并升为**并行战线 Indo-Pacific-Taiwan**（war_theaters），links_to `path W3 falsifier / 中美大国同步升级`。

**retire（战时失去意义）：**
- 暂无。v1 最后一期的 open 项在战时均仍 relevant。

**其余迁移规则（照 `framework_governance.md §7`）：**
- `frame` 不 carry：换代日 path（W）/probabilities 用当日证据现判，走本框架冷启动种子。
- `regime`/`headline` 改写为战时语言。
- 换代日 watchboard 加 `framework_switch` 留痕字段：`{from: geopolitics-v1-global-risk, to: geopolitics-v2-wartime, migrated:[...], promoted:[...], retired:[], rationale: "..."}`。
- 历史不回溯：2026-07-24 及之前的 report_state 保持 v1 原样，trace 在换代日画框架分界线。

---

## 冷启动种子（激活日 2026-07-25 用当日证据改写 path/概率/战线）

> 下面是脚手架，激活当日的地缘日报用真实证据现判 path、rung、概率、通道状态。种子已按 activation 时的事实（A+B 命中、rung≈7、Brent 破百后回落）给出偏向 W2 的先验，实际以当日运行为准。

```json
{
  "regime": "中东能源战时 regime",
  "headline": "(用激活当日证据现写)",
  "framework_switch": {"from": "geopolitics-v1-global-risk", "to": "geopolitics-v2-wartime", "migrated": ["T-038","T-040","T-049","T-062","T-063","T-064","T-066","T-067"], "promoted": ["T-050","T-035","T-045","T-065+T-068→Indo-Pacific-Taiwan"], "retired": [], "rationale": "activation_triggers A+B 决定性命中：霍尔木兹 Kpler 零通行≥5交易日 + 美伊第27波持续规模交火"},
  "tracking_items": ["(按 migration_plan 迁移/升级后的 T 项)"],
  "next_nodes": [],
  "falsifiers": ["主战线出现停火文本 → De 路径权重上升", "俄乌-黑海与主战线同步升级 → W3 权重上升", "印太-台海与中东形成中美大国同步 → 触发 v3 评估"],
  "frame": {
    "path": "W2",
    "primary_theater": "Hormuz-energy-war",
    "war_theaters": [
      {"name": "Hormuz-energy-war", "status": "active"},
      {"name": "US-Iran-direct", "status": "active"},
      {"name": "RUUA-Black-Sea", "status": "active"},
      {"name": "Indo-Pacific-Taiwan", "status": "degraded"}
    ],
    "chokepoint_status": {"Hormuz": "blockaded", "Bab-el-Mandeb": "degraded", "Black-Sea": "degraded", "Suez": "open"},
    "escalation_rung": 7,
    "belligerent_weights": ["美国", "伊朗", "海湾产油国/GCC", "胡塞武装", "俄罗斯/乌克兰", "中国大陆/台湾"],
    "transmission_channels": ["energy", "shipping", "nuclear", "great_power", "market"],
    "signal_watchlist": ["封锁执行升级", "美伊直接交火链", "俄乌-黑海同步升级", "核设施打击", "停火文本", "能源实际中断", "台海大国同步"],
    "probabilities": {"W1": 30, "W2": 50, "W3": 13, "W4": 2, "De": 5}
  }
}
```
