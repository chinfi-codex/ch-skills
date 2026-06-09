# Framework Governance:分析框架治理层(慢思考)通用机制

> 所有 `state_enabled` 的 report profile(`iran_dynamic` / `macro_daily` / `ai_daily`)共用本机制。
> 各 profile 的 `framework.md` 只声明自己的 `regime_assumption`、`invalidation_triggers` 与候选换代线索;治理机制本身在这里读一次即可。
> 本文件是 `watchboard.md` 的**姊妹篇**:watchboard 管**框架内**的快思考(每天在既定框架里填值),本层管**框架本身**的慢思考(regime 质变时把整套框架换代)。

## 1. 治理层是什么

每一套分析框架,都是为某一个**宏观状态(regime)** 量身做的。iran 的 `framework.md` 从 path 定义(A 续期 / B 交战 / C 僵尸化)到 watchlist(防空补给、扫雷进展)全都假设了"停火后僵局"这一个 regime——它写在 frontmatter 的 `regime_assumption` 里。

regime 没变,框架就有效,快思考在里面每天填值就够了。**regime 一旦质变(如停火破裂、转入持续战争),整套框架的视角会同时失效**:都全面开战了,还判什么"续期 / 僵尸化"、还盯什么"扫雷进展"?这时不是改几个值的事,是要**换一整套框架**。这件事就是本层负责的"慢思考"。

| | 快思考(`watchboard.md`) | 慢思考(本层) |
|---|---|---|
| 管什么 | 框架**内**填值 | 框架**本身**换代 |
| 状态载体 | `report_state` 表(日频) | `framework_state` 表(稀疏) |
| 信任什么 | 信任 framework.md 是对的 | 怀疑 framework.md 会过期 |
| 动作 | 滚动概率、结算台账 | regime 质变时换框架、迁移状态 |
| 自主度 | 模型自主,脚本只校验 | 模型诊断 + 提案,**换代须人确认** |

一句话:**快思考在地图上走,慢思考盯着地基塌没塌——塌了就重画整张地图,并把人和货搬过去。**

## 2. 框架对象与生命周期

`framework.md` 本身就是"框架对象",frontmatter 是它的身份:

- `framework_version`:版本号,如 `iran-v1-ceasefire`。换代即升版本(`iran-v2-wartime`)。
- `regime_assumption`:这套框架假设的宏观状态。**它是框架的有效边界**——regime 还在,框架就在。
- `supersedes`:被本版本取代的上一版本(首版为 `null`)。`supersedes` 链就是换代史。

框架生命周期:

```
seed(冷启动种子) → active(在役,快思考每天用) → challenged(慢思考标记疑似失效)
   → superseded(被新版本替换,归档进 supersedes 链)
```

**框架的唯一可信源永远是各 profile 的 `framework.md`,变更框架只改这一个文件**(机器区的 `frame` schema、`invalidation_triggers`、`output_sections`,加模型区的领域逻辑)。本层做的是"何时换、换成什么、怎么迁",最终都落到对这一个文件的替换上。

## 3. 两级触发(分清框架内 vs 框架级)

这是整个治理层的命门,务必和 `watchboard.md` §7、各 `framework.md` 的"两级触发"表述对齐:

- **框架内触发(不归本层)**:path 在 A↔B↔C 间切换。这是快思考的活,`framework.md` 的"path 判定逻辑"处理,改的是 `frame.path` 的值。
- **框架级触发(本层)**:`regime_assumption` 不再成立。改的是整个框架版本。

本层在两种情况下被唤起:

1. **定期体检**:距上次 framework review > 3 天。例行问一句"regime 还稳吗",防止框架在"一切正常"里悄悄锈掉。
2. **事件触发**:当前 `framework.md` 机器区的 `invalidation_triggers` 命中(如 iran 的"任一主要方在停火窗口期外、确认的成规模军事行动,且报复链 ≥3 天")。命中即唤起,不等 3 天。

**谁来检测**:脚本只做**确定性检测**——算距上次 review 的天数、把 `invalidation_triggers` 的关键条件在当日 fact stream 里扫一遍命中没,然后**亮灯**。"灯亮之后 regime 到底变没变",是模型(诊断)和人(确认)的判断,脚本绝不替它们下结论。这和 watchboard"脚本只搬状态、不判断"的纪律一致。

## 4. 一次 review 的闭环(对称 watchboard 的"每天闭环")

```
1. 触发:prepare_report_data 在 packet 里亮灯(距上次 review N 天 / 触发器命中清单)
2. 诊断:读盲化 fact stream(不看当前 watchboard),判 regime 稳定 / 漂移 / 质变
3. 提案:质变时产出新 framework.md 草案 + 状态迁移计划
4. 确认:人审批 → 替换 framework.md、升版本、迁移 report_state、回写 framework_state
```

**盲化纪律(第 2 步的灵魂)**:诊断必须读**去结论的 fact stream**(`items` 原始快讯,剥掉 watchboard 写的 regime / headline / 判断措辞),并且在**不看当前 watchboard** 的独立上下文里做。否则"停火"叙事的惯性会让模型看不见 regime 已经变了——慢思考最该防的就是这个锚。零基重估在这里降级为**诊断探针**:盲推出的 path 分布若和当前 watchboard 持续大幅背离,本身就是"regime 可能在变"的信号之一。

## 5. regime 诊断:三档结论

读 fact stream + 当前 `invalidation_triggers`,判一个:

- **稳定**:regime 没变,框架继续用。不改任何东西,记一笔 review 通过。
- **漂移**:regime 在边缘晃,但还没质变(如停火更脆但尚未破裂)。**这归框架内处理**——调 watchlist、动 actor_weights、加跟踪项,全走 watchboard,不动框架版本。
- **质变**:`regime_assumption` 已不成立。进入换代(§6)。

判据要扎在 `invalidation_triggers` 上,而不是模型的主观感觉:触发器是各 profile 在 framework.md 里**预先声明的"本框架的死亡条件"**——把"何时该换代"的领域知识交给写框架的人,本层只负责检测命中 + 评估证据强度。多数 review 的结论应是"稳定"或"漂移",换代是少数。

## 6. 框架换代提案

判为质变后,产出一份新 `framework.md` 的**草案**(不是直接替换),内容是新框架的全套契约:

- 新 `framework_version` + 新 `regime_assumption` + `supersedes` = 旧版本号;
- 新机器区:`frame` schema(新 path enum / 维度)、新 `invalidation_triggers`(新 regime 的死亡条件)、新 `output_sections`;
- 新模型区:新 path 判定逻辑、新 watchlist、新行为体权重,以及**跨域推演重点怎么变**(对商品 / 金融 / 地缘的影响路径,新旧 regime 是两套);
- 配套的**状态迁移计划**(§7)。

以 iran 停火→战时为例,草案大致是:path 从"续期 / 交战 / 僵尸化"换成"升级失控 / 有限战 / 消耗僵持 / 外力停火";watchlist 从"防空补给 / 扫雷"换成"战线推进 / 核设施 / 第三方参战 / 海峡封锁";市场推演从"溢价高位震荡"换成"油价 spike / 封锁定价 / risk-off 避险暴动"。

## 7. 状态迁移规约(换代不断历史)

框架换代最怕断了跨天连续性。规约的精髓是**复用 watchboard 的 silent-drop 铁律**——换代日(记为 D)的每个旧 open 跟踪项,一个都不许凭空消失,必须在新框架下三选一被裁决:

- **迁移(migrate)**:新 regime 下仍 relevant(如"以色列防空补给"战时照盯),保留 `id` 与 `opened`,只把 `links_to` 改指向新框架维度。
- **作废(retire)**:新 regime 下失去意义(如"扫雷进展"——全面开战后不再是主矛盾),标 `status=dismissed`,`resolution` 写明"框架 v1→v2 换代,本项属停火 regime,战时不适用"。
- **升级(promote)**:旧项在新框架下升为核心(如"代理人自主行动密度"→战时主战线跟踪)。

其余迁移规则:

- `frame` **不能 carry**(path enum 变了不兼容):换代当日的 path / probabilities 用当日证据**现判**,走新 framework.md 的冷启动种子流程。
- `regime` / `headline` 改写为新 regime。
- 换代日 watchboard 加一个留痕字段,供 trace 看到"这天框架换了":

```json
"framework_switch": {
  "from": "iran-v1-ceasefire", "to": "iran-v2-wartime",
  "migrated": ["T-001", "T-004"], "retired": ["T-003"], "promoted": ["T-009"],
  "rationale": "06-12 以伊全面交火确认 + 报复链 4 天,停火 regime 失效"
}
```

`save_report_state.py` 据此校验"旧 open 项都被裁决了"——与现有 silent-drop guard 同构(**此校验为本层新增,待实现**)。

**历史不回溯**:D 之前的 report_state 保持原样(旧框架下的真实判断,不追改)。trace 在 D 处画一条框架分界线,而不是把过去重写成新框架的语言。

## 8. 状态承载:framework_state

慢思考的状态存在新表 `framework_state`(`profile` + `review_date_key` + `payload`),与 `report_state` 同构、但稀疏(每次 review 一行,不是日频)。payload 骨架:

```json
{
  "review_date": "2026-06-12",
  "framework_version": "iran-v1-ceasefire",
  "regime_verdict": "质变",
  "triggers_hit": ["窗口期外成规模军事行动 + 报复链 ≥3 天"],
  "zero_based": {"path_distribution": {"...": 0}, "evidence_ids": ["..."]},
  "divergence": [{"dim": "probabilities.B", "watchboard": 25, "zero_based": 35, "verdict": "framework_ambiguity"}],
  "proposal": {"new_version": "iran-v2-wartime", "migration_plan": {"migrate": [], "retire": [], "promote": []}},
  "challenges": [{"id": "FC-001", "statement": "...", "status": "open"}],
  "status": "proposed",
  "next_review_due": "2026-06-15"
}
```

`framework.md` 与 `framework_state` 的分工:**framework.md 是人读 + loader 解析的"当前框架定义"(框架是什么);framework_state 是机器写的"治理账本"(框架被怎么审、怎么换的历史)**。退役的 framework.md 版本归档(进 `supersedes` 链或 archive 目录)。

## 9. 人在环边界与反模式

**换代必须人批准**:改 `framework.md` 的 path 三分 / 维度、乃至换整套版本,影响所有后续判断,且最容易被模型自己的叙事带偏(让模型既当运动员又改规则很危险)。所以本层只能产 `proposal`,人确认后才替换文件、升版本、迁移状态。日常的 challenge 注入(框架内的挑战)可自动,框架级换代不行。

看到要改的反模式:

- **用自由字段硬扛框架级变化**:拿 `sub_branch` / `regime` 长期承载本该属于别的 path、甚至别的 regime 的事实(如"A-2 形式续期 / 内核空转"长期偷渡 C 的内核)。自由字段反复硬撑,正是 regime 在变、该上报本层的信号,不是继续往 watchboard 里塞。
- **让脚本判断 regime 变没变**:脚本只算时间差、扫触发器命中;判断与重构是模型 + 人的活。
- **频繁换代**:换代是大动作。只有 `invalidation_triggers` 命中或持续质变才动,不是每次 review 都换。
