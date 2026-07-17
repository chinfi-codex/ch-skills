# Watchboard:活动状态层通用机制

> 所有 `state_enabled` 的 report profile（见 `config/report_profiles.yaml`）共用本机制。
> 各 profile 的 `methodology.md` 只声明自己的 frame 字段,机制本身在这里读一次即可。

## 1. watchboard 是什么

watchboard 是这个 profile 的**活动分析状态**:当前判断、在跟踪的事项、各维度的当下取值。它每天滚动一次,跨天累积,存在数据库 `report_state` 表里(按 `profile + date_key` 建行),**不是写死在方法论文件里的静态快照**。

它解决一个老问题:方法论一直要求"延续昨天的判定""填下一节点""写观察清单",但过去没有任何东西把昨天的状态喂回今天。watchboard 就是那个被喂回来的东西——你的"工作记忆"。

**判断永远是你(模型)的**:什么该跟踪、权重怎么动、某事项算不算结算,全由你定。脚本只负责把状态搬进搬出、做结构校验,绝不替你判断。

## 2. 每天的闭环

```
1. 读:prepare_report_data 已经把上一期 watchboard 放进 evidence packet 的 "Prior Watchboard" 段
2. 结算:逐条过上一期每个 open 跟踪项 —— 今天兑现了/证伪了/没动/过期了?
3. 写报告:在报告里显式体现变化(昨天盯 X → 今天 X 怎么样了 → 推动了什么)
4. 回写:把今天的新 watchboard 用 save_report_state.py 存回去
```

第 1 步和第 4 步把昨天与今天焊在一起。报告里的"框架演进 / 跟踪项结算"板块,本质就是 watchboard 上一期与这一期的 diff。

## 3. payload 结构

watchboard 是一个 JSON 对象,通用骨架(所有 profile 一致):

```json
{
  "as_of": "2026-06-02",
  "carried_from": "2026-06-01",
  "regime": "一句话给当前所处的状态贴个自由标签(取代旧的离散阶段)",
  "headline": "一句话当前判断",
  "tracking_items": [ ...见第 4 节... ],
  "next_nodes": [ {"name": "...", "date": "YYYY-MM-DD", "affects": "..."} ],
  "falsifiers": ["出现 X 则当前判断不成立", "..."],
  "grading_audit": [ ...仅启用价值定级审计的 profile，如 ai_daily... ],
  "frame": { ...本 profile 特有字段,见各自 methodology... },
  "frame_change": "今天 path/概率/权重为何这么挪(框架移动了就必填,见下)"
}
```

- `regime`:自由字符串,不是枚举。局势/格局换挡时你直接改这个标签,不需要换文件。
- `falsifiers`:**必须认真填**。每天写下"什么证据会推翻今天的判断",逼自己留反向通道,别锚定昨天。
- `frame`:profile 特有的快变量(如地缘日报的 path/区域/传导渠道、宏观的 swing_factor/位置档位、AI 的工程演进矢量/产品形态),字段清单和约束写在该 profile 的 `framework.md` 机器区。
- `frame_change`:**框架移动的意图**。只要相对上一期 `path` 变了、或任一概率桶移动 ≥ 0.5,就**必须**写明为什么这么挪(可以是一句话,也可以是按 path/概率/权重分条的 map)——否则 `save_report_state.py` 报错。框架没动时可省略。这是把"概率为什么从 45 挪到 40"从散文里捞出来、焊进状态的字段;冷启动(无上一期)不要求。
- `as_of` / `carried_from` 你不填时脚本会按日期自动补,但建议你显式写清。
- `grading_audit`:仅当 profile 的 `value_grading.audit_required=true` 时必填；它是候选定级的后台审计，不是正文。当天没有 S-candidate 也必须写空数组 `[]`，表示已完成候选检查。

### 价值定级审计结构（启用它的 profile）

`grading_audit` 每条只对应一个真正进入候选评审的实体，数量不得超过 profile 的 `candidate_budget`。脚本只校验流程和证据结构，不判断模型给出的等级是否“聪明”。推荐结构：

```json
{
  "entity_id": "kimi-k3",
  "entity": "Kimi K3",
  "provisional_grade": "s_candidate",
  "final_grade": "s_candidate",
  "trigger_conditions": ["重点厂商旗舰模型，可能同时改变能力与开放性"],
  "candidate_exclusions": [],
  "evidence_sources": [
    {"source_family": "rss", "type": "official", "url": "https://example.com/official"},
    {"source_family": "hacker_news", "type": "independent", "url": "https://example.com/review"}
  ],
  "confirmation_blockers": ["权重尚未开放，独立复现不足"],
  "evidence_gaps": ["等待官方 model card 与可下载权重页"],
  "deep_enrichment": {
    "status": "partial",
    "target_urls": ["https://example.com/official", "https://example.com/review"],
    "note": "已确认发布动作，但可用性门槛尚未满足"
  },
  "rationale": "保留重点候选，24-72h 后按权重可用性和独立复现复核"
}
```

- `provisional_grade` 固定为 `s_candidate`；`final_grade` 只能是 `s_confirmed` / `s_candidate` / `a` / `b`。
- `candidate_exclusions` 与 `confirmation_blockers` 必须分开：前者命中会让对象失去候选资格，后者只阻止它升级为 S-confirmed。
- `evidence_sources[].type` 使用 `official` / `independent` / `supporting`；S-confirmed 必须同时具备 official 与 independent。
- `deep_enrichment.status=complete` 需要达到 profile 的最少 URL 数；`partial` 可提前停止，但至少记录一个已尝试 URL，并用 `note` 解释缺口。

## 4. 跟踪项台账(让报告跨天连贯的核心)

`tracking_items` 是一串带生命周期的观察事项。每条:

```json
{
  "id": "T-014",
  "opened": "2026-05-28",
  "statement": "在跟踪什么,一句话说清",
  "links_to": "关联到哪个变量/路径/维度,说明为什么值得盯",
  "status": "open",
  "update": "今天对这条做了什么动作、为何仍 open(statement 改了就必填)",
  "resolution": null,
  "expires_after": "2026-06-11"
}
```

- `id`:**稳定不变**。开了就不改,这样今天的报告才能引用"昨天的 T-011 今天结算了"。新项用没用过的新号。
- `opened` + `statement`:**每条必填**(缺了 `save_report_state.py` 直接报错)。没有开启日期和一句话说明,这条事项跨天就引用不起来。
- `status`:`open`(在盯) / `confirmed`(兑现了) / `dismissed`(被证伪/不再相关) / `expired`(过期未动,待复核)。
- `update`:**open 项的当日动作与意图**。如果这条从上一期 carry 过来、仍 `open`、但你改了 `statement`(说明它今天动了),就**必须**写 `update`:今天发生了什么、把哪个 frame 维度往哪推、为何还不结算——否则报错。statement 没改(纯顺延)时可省。它和 `resolution` 互补:`update` 管"仍 open 但有进展",`resolution` 管"已结算"。
- `resolution`:结算时填——发生了什么、推动了什么(影响了哪个 frame 字段)。**status 一旦不是 `open`,resolution 必填**;空着会报错(逼你把"为什么结算"写下来,而不是悄悄改状态)。
- `expires_after`:**open 项必填、且必须是未来日期**(到期即触发结算,见下"控制台账规模");open 项缺失或已过期 `save_report_state.py` 直接报错(冷启动首期除外)。已结算项可省。
- `sub_items`(可选):把一个跟踪项变成**母题**,收纳同一变量/路径下的多条子线。子项结构与顶层项相同(id / opened / statement / status / update|resolution / **独立 expires_after**),但**只允许一层**(子项不能再带 sub_items)。

**母题与子项(sub_items)——既瘦顶层又不丢颗粒度:**

同一谈判线、同一能源节点常裂成好几条独立事项,平铺成多个顶层 open 既挤预算、又让人看不清主线。把它们收进一个母题的 `sub_items`:

- **母题占 1 个 open 预算名额**,子线不计入预算——这是"瘦顶层"的来源。
- 但**每个子线保留自己的 `expires_after` 时钟和 status**:子线该到期就到期、该单独结算就单独结算,脚本对 open 子线一视同仁地查到期。子线**不会搭母题便车被掩盖**——这正是它和"糊成一条 statement"的本质区别,后者会丢掉子线的独立死活和独立时钟。
- **降级即归并**:把一个顶层 open 项移进某母题的 `sub_items`,silent-drop guard 视作"已处理"(按 id 仍在、没消失),不报错。
- 子线结算(转非 open)后,下一期可从 `sub_items` 移除,母题随之变薄。

**铁律:每个 open 项每天都要被碰一次。** 确认、证伪、顺延、过期,四选一,不许凭空消失。`save_report_state.py` 会对照上一期的 open 项,任何在今天 watchboard 里彻底不见的 id 直接报错——这是防"悄悄忘了承诺"的兜底。

### 控制台账规模(防膨胀)

台账只会越滚越长,除非主动"了断"比"顺延"更划算。四条规矩把顺延变贵、把了断和归并变便宜:

1. **到期即了断(脚本 error 兜底)**:每个 open 项都要带 `expires_after`(未来日期)。一旦 `expires_after` 到期(≤ 今天)它还 open,今天就**必须**二选一——结算掉(confirmed/dismissed/expired),或写明"为什么还值得盯"并把 `expires_after` 续到更晚。到期了无脑顺延、或 open 项干脆不写 `expires_after`,`save_report_state.py` 直接报错(冷启动首期除外)。
2. **open 预算(脚本 warning)**:每个 profile 的活跃 open 项软上限写在 `config/report_profiles.yaml` 的 `open_budget`。超了脚本告警,要求先归并或了断、再开新项。预算不硬卡(事件密集日不该拒绝回写),但持续超标就是该精简的信号。
3. **陈旧自动降级**:一个 open 项连续 3 期纯顺延(statement 没变、没有实质进展),第 4 期别再顺延——转 `expired`(待复核)或并入母题。连续"无新进展"本身就是它不该继续占用活跃名额的证据。
4. **母题归并,别碎开**:开新项前先问"能不能挂到现有母题下"。同一变量/路径/维度的多条细项(几条都指向同一谈判线、同一能源节点)收进一个母题的 `sub_items`(见上),母题占 1 个预算名额、子线各自保留独立 statement 与到期时钟——瘦了顶层又不丢颗粒度。**别把它们糊成一条 statement**,那会丢掉子线的独立死活。

`expired` 是现成的出口:silent-drop guard 只盯上一期的 **open** 项,一条项一旦转成非 open,下一期就能从台账里自然移除、不再每天结算。所以"精简"不需要新机制,只需要及时把不再活跃的项推出 open。

报告呈现也要消化臃肿:正文只逐条展开"今日有动作"的项(有进展/新开/结算),纯顺延的按主题聚合成一句话("path A 协议线 5 项均无新进展,顺延"),不要把十几条顺延平铺占满篇幅。

## 5. 怎么读上一期

`prepare_report_data.py` 的输出里会有一段 `## Prior Watchboard (carry-forward)`:
- 没有上一期(冷启动):按本 profile methodology 的默认/种子构造第一份 watchboard。注意**当前判断要用今天证据现判**,种子里的旧值只当结构脚手架。
- 有上一期:列出 `state date`、`regime`、以及今天**必须逐条结算的 open 跟踪项**,后面附完整 JSON。

同段还有 `## Coverage (DB-first)`,告诉你今天各信源在库里有多少条、哪个源缺数、最终 evidence packet 的分源数量，以及与本 profile 有关的具体 feed 失败。某源 missing 或 feed 失败时,报告里相应判断要降级标注，不能只凭“源有总行数”就写成完整覆盖。

## 6. 怎么回写

写完报告后,把今天的新 watchboard 存回去:

```bash
# 从 stdin 传入 JSON
cat today_watchboard.json | python scripts/save_report_state.py \
    --profile <profile> --date today --state-file -

# 或先验证不写
python scripts/save_report_state.py --profile <profile> --date today \
    --state-file today_watchboard.json --check-only
```

脚本只做结构校验,不评判分析对错。**报错(exit≠0)就是漏了结算或丢了字段,按提示补全再存**。当前会硬报错的项:

- 信封必填:`as_of` / `regime` / `tracking_items` / `next_nodes` 缺任一。
- `falsifiers` 缺失或为空 —— 必须至少给一条可证伪条件(不再是 warning)。
- profile 要求价值定级审计时，`grading_audit` 缺失、超过候选预算、深挖 URL / 状态不完整，或把缺官方 / 独立证据的对象写成 `s_confirmed`。
- 每条跟踪项缺 `id` / `opened` / `statement`,或 id 重复,或 `status` 不在四值内。
- 结算项(status 非 `open`)没写 `resolution`。
- carry 过来仍 `open` 的项改了 `statement` 却没写 `update`(动了就得说为什么动)。
- `frame` 里 `required` 字段缺失或为空集(`[]` / `{}` 也算缺);概率类字段求和超出容差(默认 ±0.2,容一位小数舍入如 33.3×3=99.9,但 99.6/100.4 这类偏差会报错;可在 `report_profiles.yaml` 用 `sum_tol` 调整)。
- 相对上一期 `path` 变了、或任一概率桶移动 ≥ 0.5,却没写 `frame_change`(框架挪了就得说为什么挪;冷启动无上一期时不要求)。
- 上一期任一 `open` 跟踪项在今天 watchboard 里彻底消失(没结算也没顺延)。
- 某 `open` 跟踪项的 `expires_after` 已到期(≤ 今天)或缺失,却仍标 `open`(冷启动首期除外)——到期即了断:结算掉,或写明理由并续到未来日期。

只出 warning(不阻塞)的:**非 open** 跟踪项缺 `expires_after`、**open 项数超过该 profile 在 config 中的预算**、profile 的 `state_enabled` 为假仍强存。

## 7. 什么时候该改框架（framework.md）而不是改 watchboard

watchboard 承接**框架内**的日常漂移（path 在本 profile 的枚举内移动、概率微调、台账增删）。但如果你发现**框架本身**要变了——老往台账里塞现有维度装不下的事项、或当前 path 分类对局势已经不够用、或 regime 发生质变——那不是 watchboard 能承接的，而是**框架该换代**的信号。

这时**不要硬塞进 watchboard**，应：

- 把"框架装不下"的观察写成一条挑战交给慢思考层，而不是用 `sub_branch` 等自由字段硬撑（自由字段长期承载本该属于别的 path 的事实，正是框架失配的征兆）；
- 框架级变更（改 `framework.md` 的 path 定义/维度/输出板块，乃至换整套框架版本）是**有人在环的慎重动作**，由慢思考层（见 `framework_governance.md`，建设中）诊断 + 提案、人确认后应用。

watchboard 在这里的作用是**预警**（框架边缘反复撞墙就是 regime 在变的信号），不是自己把框架改了。框架的唯一可信源是各 profile 的 `framework.md`。

## 8. 报告"本期变更"段的渲染约定（所有 state_enabled profile 通用）

报告末尾的"本期变更（框架 × 跟踪合并）"段，是 watchboard 结构化字段的**渲染**——不是另写一遍判断：

- **先填字段、再照着渲染本段**，别两头写出不一致。框架移动取自 `frame_change`，跟踪项动作取自各项 `update`（顺延）/ `resolution`（结算）。
- 把本期相对上一期的变化**合并成一段**，框架与跟踪混编、按重要性排序，逐条 bullet。
- **框架**（动了才写，未动写"沿用，N 项顺延"）：path / 概率 / 权重 / 各 profile 的 frame 维度怎么变——每条紧跟"为什么"（← `frame_change`）。
- **跟踪项结算**：上一期每个 open 项都要被处理（✅确认 / ❌证伪 / ⏸️顺延 / ⌛过期），不许遗漏——这是 watchboard **JSON 层**的铁律（由 silent-drop guard 校验）。报告**正文**里结算项逐条写、纯顺延项可按主题聚合成一句（见 §4「控制台账规模」末段），不必逐条平铺（← `update` / `resolution`）。
- **新开**：带稳定 id + 因为什么开（≥ 某证据共振 / 某节点临近）。
- 各 profile 的具体字段名映射见各自 `framework.md` 的"各板块写作要点"。
