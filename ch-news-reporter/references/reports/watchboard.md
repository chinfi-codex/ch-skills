# Watchboard:活动状态层通用机制

> 所有 `state_enabled` 的 report profile(`iran_dynamic` / `macro_daily` / `ai_daily`)共用本机制。
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
  "frame": { ...本 profile 特有字段,见各自 methodology... }
}
```

- `regime`:自由字符串,不是枚举。局势/格局换挡时你直接改这个标签,不需要换文件。
- `falsifiers`:**必须认真填**。每天写下"什么证据会推翻今天的判断",逼自己留反向通道,别锚定昨天。
- `frame`:profile 特有的快变量(伊朗的 path/权重/信号清单、宏观的 swing_factor/位置档位、AI 的热门主题/待观察项),字段清单和约束写在该 profile 的 `methodology.md` 与 `report_profiles.yaml` 的 `state_schema`。
- `as_of` / `carried_from` 你不填时脚本会按日期自动补,但建议你显式写清。

## 4. 跟踪项台账(让报告跨天连贯的核心)

`tracking_items` 是一串带生命周期的观察事项。每条:

```json
{
  "id": "T-014",
  "opened": "2026-05-28",
  "statement": "在跟踪什么,一句话说清",
  "links_to": "关联到哪个变量/路径/维度,说明为什么值得盯",
  "status": "open",
  "resolution": null,
  "expires_after": "2026-06-11"
}
```

- `id`:**稳定不变**。开了就不改,这样今天的报告才能引用"昨天的 T-011 今天结算了"。新项用没用过的新号。
- `status`:`open`(在盯) / `confirmed`(兑现了) / `dismissed`(被证伪/不再相关) / `expired`(过期未动,待复核)。
- `resolution`:结算时填——发生了什么、推动了什么(影响了哪个 frame 字段)。

**铁律:每个 open 项每天都要被碰一次。** 确认、证伪、顺延、过期,四选一,不许凭空消失。`save_report_state.py` 会对照上一期的 open 项,任何在今天 watchboard 里彻底不见的 id 直接报错——这是防"悄悄忘了承诺"的兜底。

控制台账规模:每条给 `expires_after`;长期不动的转 `expired` 待复核,别让台账无限膨胀。

## 5. 怎么读上一期

`prepare_report_data.py` 的输出里会有一段 `## Prior Watchboard (carry-forward)`:
- 没有上一期(冷启动):按本 profile methodology 的默认/种子构造第一份 watchboard。注意**当前判断要用今天证据现判**,种子里的旧值只当结构脚手架。
- 有上一期:列出 `state date`、`regime`、以及今天**必须逐条结算的 open 跟踪项**,后面附完整 JSON。

同段还有 `## Coverage (DB-first)`,告诉你今天各信源在库里有多少条、哪个源缺数。某源 missing 时,报告里相应判断要降级标注"今日该源无数据"。

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

脚本会做结构校验(必填字段、frame 字段是否齐、概率是否求和达标、上一期 open 项有没有被漏)。**报错就是漏了结算或丢了字段,按提示补全再存**;它不评判你的分析对不对。

## 7. 什么时候该改方法论而不是改 watchboard

watchboard 承接日常漂移。但如果你发现**分析结构本身**要变了——比如老往台账里塞现有变量分类装不下的事项、或者 A/B/C 这套路径分类不够用——那是方法论该升级的信号。这时**不要硬塞进 watchboard**,而应提出修改本 profile 的 `methodology.md`(有人在环的慎重动作)。watchboard 在这里的作用是预警,不是自己把分析框架改了。
