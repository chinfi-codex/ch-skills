# CH News Reporter · 动态框架迭代设计

> 适用范围:`ch-news-reporter` 全部 report profile(`ai_daily` / `macro_daily` / `iran_dynamic`)。
> 本文件是架构与落地的唯一权威说明。它本身不是 skill 运行时资产,放在仓库 `docs/`,不随 `skill_sync` 同步、不进 ClawHub 发布包。

## 1. 要解决的问题

事件每天都在变,《伊朗日报》这类动态播报的**关注点和核心跟踪事项也随之改变**。原来的做法是给伊朗单独维护"离散阶段框架"(`frame_ceasefire.md` / `frame_postdeadline.md`),局势换挡时人工重写整份 frame 文件并改加载指针。三个问题:

1. **frame 是静态快照**。当前权重、信号清单、阶段判定冻在文件里,只有人工"根本性切换"才动,接不住每天的关注点漂移。
2. **"沿用上一日判定"是空指令**。方法论要求延续昨天的判定、填"下一节点"、写"观察清单",但没有任何机制把昨天的状态喂回今天——除非人工把昨天的报告贴进来。
3. **观察清单一次性蒸发**。今天写"盯扫雷进展",明天没有东西把它捞回来结算,报告之间不连贯。

而且这套"离散阶段"机制是**伊朗专属**的,`ai_daily` / `macro_daily` 没有,导致三个 profile 结构不统一,无法复用。

## 2. 设计目标

1. **去掉伊朗的离散阶段**,把 `frame_*.md` 这一层整个删掉。
2. **给整个 skill 加"活动状态层"(watchboard)**,让分析框架与跟踪项每天自动迭代,三个 profile 用同一套机制。
3. **数据读取改为 DB-first**:报告优先读库里对应日期的数据;缺哪个源,才去补哪个源。

贯穿铁律(CLAUDE.md / AGENTS.md 的 prompt-first):**判断永远留给模型,脚本只做确定性 I/O 和结构校验**。watchboard 是模型的"工作记忆",不是规则引擎。

## 3. 目标架构:两层 + 三 profile 同构

| | 慢层(方法论) | 快层(活动状态) |
|---|---|---|
| 物理形态 | `methodology.md` + `template.md` + 共享 `references/reports/watchboard.md` | DB 表 `report_state` 一行 /(profile, date) |
| 内容 | 怎么分析:判定逻辑、可信度分级、传导锚点、输出结构 | 当前 regime、跟踪项台账、权重、信号清单、概率、下一节点 |
| 变化速度 | 月级,人工编辑 | 天级,模型每天回写 |
| 是否 skill 资产 | 是,随 `skill_sync` 同步、可发布 | 否,运行时数据,与 `items` / `enrichments` 同级,不发布 |

改造前后:

```
改造前(iran 特例):   methodology.md + frame_ceasefire/postdeadline.md(离散相位) + template.md
改造前(ai/macro):    methodology.md + template.md
改造后(三者统一):     methodology.md + template.md + watchboard(DB,每日滚动) + 共享 watchboard.md
```

"阶段"概念不消失,只是从"一份要换的文档"降级成 watchboard 里一个 `regime` 自由字符串字段,由模型每天维护。

**唯一代价**:当分析结构本身(要问的问题 / A-B-C 这套分类)要变时,改为人工编辑 `methodology.md`,而不是换 frame 文件。这本该是慎重的、有人在环的动作;watchboard 负责预警(模型老顶着现有分类往外加跟踪项时),真正的重构留给方法论编辑。

## 4. watchboard 数据模型

### 4.1 存储:`report_state` 表

PostgreSQL(`init_alpha_data.sql`)与 SQLite(`db_adapter._init_sqlite_schema`)同时建表,字段一致:

| 字段 | 类型(PG / SQLite) | 说明 |
|---|---|---|
| `profile` | TEXT | ai_daily / macro_daily / iran_dynamic |
| `date_key` | DATE / TEXT | Asia/Shanghai 日期 |
| `payload` | TEXT | watchboard 全文(JSON 字符串,两端均存 TEXT 保证读写对称) |
| `created_at` / `updated_at` | TIMESTAMPTZ / TEXT | 审计 |

主键 `(profile, date_key)`,`ON CONFLICT(profile, date_key) DO UPDATE` 幂等。行历史即框架演进留痕。

### 4.2 payload 通用骨架(三 profile 共享)

```yaml
as_of: 2026-06-02
carried_from: 2026-06-01          # 上一期来源日期;首期为 null
regime: "停火到期后·僵尸化延续"      # 自由标签,取代离散阶段
headline: 一句话当前判断
tracking_items: [ ... ]           # 跟踪项台账,见 4.4
next_nodes:
  - {name: IAEA 季度理事会, date: 2026-06-09, affects: "B/A"}
falsifiers:                       # 反向证据/可证伪条件,防锚定
  - 出现 X 则当前判断不成立
frame: { ... }                    # profile 特化块,字段由 state_schema 声明
```

### 4.3 frame 特化块(各 profile 不同,由 `report_profiles.yaml` 的 `state_schema` 声明)

| profile | frame 字段 |
|---|---|
| **iran_dynamic** | `path`(A/B/C)、`sub_branch`、`intensity`、`actor_weights`(有序)、`signal_watchlist`(每日可增删)、`probabilities`{A,B,C 和=100} |
| **macro_daily** | `swing_factor`(当下主驱动维度)、`liquidity_bias`(收紧/中性/宽松)、`position_regime`{纳指,上证: 高/中/低}、`imminent_data_events`(临近的 CPI/FOMC 等带日期) |
| **ai_daily** | `hot_themes`(在跟踪的方向信号)、`watch_companies`(预期有动作的厂商)、`watch_projects`(待验证热度→趋势的开源项目) |

### 4.4 跟踪项台账 `tracking_items[]`(让报告跨天连贯的核心)

```yaml
- id: T-014                       # 稳定 id,跨天可引用
  opened: 2026-05-28
  statement: 盯伊朗海军是否对 CENTCOM 扫雷采取对抗动作
  links_to: 合规层 / 路径B早期信号    # 关联到哪个变量/路径,说明为什么跟踪
  status: open                    # open · confirmed · dismissed · expired
  resolution: null                # 结算时写:发生了什么 + 推动了什么
  expires_after: 2026-06-11       # 到期未动转 expired 待复核,防台账膨胀
```

**纪律:每个 `open` 项每天必须被碰一次**(确认 / 证伪 / 顺延 / 过期)。`save_report_state.py` 会对照上一期的 open 项,任何在新状态里彻底消失(既没结算也没顺延)的 id 直接报错——杜绝"悄悄忘了承诺"。

## 5. 每日迭代闭环(已折进 DB-first)

```
第 N 天生成某 profile 报告:
  1. 【脚本】DB-first 补数:collect_news --date N --only-missing
            查库里 date=N 各源行数,只补缺失的源
  2. 【脚本】prepare_report_data --profile P --date N
            组装 evidence packet,并捞最近一期 watchboard(date<N)与 coverage 一并塞进 packet
  3. 【模型】逐条结算上一期 open 跟踪项:确认 / 证伪 / 顺延 / 过期
  4. 【模型】写报告,显式体现 delta(昨天盯X→今天X兑现→概率/权重怎么动)
  5. 【模型】产出第 N 期新 watchboard → save_report_state 回写 report_state
```

第 2 步与第 5 步把昨天和今天焊死。DB-first 保证重算历史日时用的是**当时那批数据**,跨天结算才可复现。

## 6. DB-first 数据读取策略

报告默认**先用库里对应日期的数据;只对缺失的源做补采**。

- **覆盖检查纯 SQL**:`SELECT source_type, COUNT(*) FROM items WHERE date_key=? GROUP BY source_type`(`db_adapter.count_items_by_source`)。零行 = 缺,才采。
- **`collect_news.py --only-missing`**:先算覆盖,只对库里当天缺的源跑采集;已有的源连网络请求都不发。
- 报告工作流第 1 步从无条件 `collect_news --date today` 改为 **`collect_news --date today --only-missing`**。今天首跑→库空→全采(与现状一致);重跑 / 补历史→读库不重采。
- **`--replace-date`** 保留为显式强制刷新的口子(改规则、修数据时用),优先级高于 `--only-missing`。
- **`prepare_report_data`** 在 packet 里多带 `coverage` 摘要(各期望源行数 + 哪些缺),让模型知道今天哪个源空、好在报告里降级标注。

判断"够不够"不交给模型:一个源对某日期要么有行要么没有(确定性)。"数据稀薄要不要降级表述"由模型看 `coverage` 自行决定。

## 7. 代码 vs 模型边界

| 脚本只做(确定性) | 模型独占(判断) |
|---|---|
| 覆盖检查 + 按缺补采(`collect_news --only-missing`) | 跟踪项算不算结算、该开该关 |
| 捞上一期 watchboard、算 coverage,塞进 packet(`prepare_report_data`) | 信号该不该进/出清单 |
| 回写新 watchboard,结构校验(`save_report_state`) | 权重要不要调、路径有没有切、regime 怎么标 |
| 建表 / 读写 / JSON 解析(`db_adapter`) | 报告怎么写、watchboard 怎么写 |

脚本永远不决定"今天该盯什么"。守不住这条就退化成 `if PE>30 就估值偏高` 的反模式。

## 8. 文件级改动清单

**删除**
- `references/reports/iran_dynamic/frame_postdeadline.md`
- `references/reports/iran_dynamic/frame_ceasefire.md`

**新增**
- `references/reports/watchboard.md`:通用 watchboard 机制(读→结算→回写、台账生命周期、脚本用法),任何 `state_enabled` 的 profile 共享。
- `scripts/save_report_state.py`:回写 + 结构校验 watchboard。

**改写 / 编辑**
- `references/reports/iran_dynamic/methodology.md`:吸收 `frame_postdeadline.md` 的稳定内容(路径判定逻辑、共用合规层、烈度标尺、边际变化判定、可信度、传导锚点);删"阶段感知加载";加 watchboard 小节 + 冷启动种子(JSON 块)。
- 三个 `methodology.md`:各加一节统一的"活动状态"说明(列本 profile 的 frame 字段 + 指向 `watchboard.md`)。
- 三个 `template.md`:各加一块"框架演进 / 跟踪项结算"板块;把"观察清单 / 下一节点"改成 watchboard 的投影。
- `SKILL.md`:第 1 步改 DB-first;工作流加载入/回写状态;删所有 `frame_*.md` 引用;`report_state` 写进数据表说明;三 profile 通用化。
- `config/report_profiles.yaml`:每个 profile 加 `state_enabled: true` + `state_schema`。
- `init_alpha_data.sql`:加 `report_state` 表(PG)。
- `scripts/db_adapter.py`:`_init_sqlite_schema` 加 `report_state`(SQLite);新增 `count_items_by_source` / `write_report_state` / `get_latest_report_state` / `get_report_state`。
- `scripts/prepare_report_data.py`:packet 里加 `prior_state` + `coverage`,markdown 输出同步展示。
- `scripts/collect_news.py`:加 `--only-missing`。

## 9. 伊朗迁移与冷启动

- **拆 `frame_postdeadline.md`**:稳定部分(判定逻辑/合规层/烈度/边际判定/传导/可信度)上并进 `methodology.md`;活的快照(当前 path、`actor_weights: [以色列, 伊朗内部, 美国]`、核心信号清单、probabilities、当前子分支)写成 `methodology.md` 里的"冷启动种子"JSON 块。
- **首次无上期状态**(`prepare_report_data` 报告 `prior_state: null`):模型按 methodology 默认 + 种子构造初始 watchboard,但 **path / probabilities 必须用当日证据现判**,种子里的旧值只作结构脚手架,不当 gospel。
- **不回填历史**:从今天起步,老报告不动。
- `ai_daily` / `macro_daily` 无现成快照,首跑由模型直接按 methodology 生成初始 watchboard。

## 10. 失效模式与防护

- **锚定昨天**:有记忆易退化成抄昨天微调。→ methodology 保留"不写死、现判"纪律;watchboard 强制 `falsifiers` 字段,逼模型每天找反证。
- **台账膨胀**:→ `expires_after` + 校验兜底。
- **事后诸葛**:→ 概率调整必须引当日新证据;历史行不许偷改(按日期独立成行)。
- **悄悄丢字段**:→ `save_report_state` 比对上一期 open 项,消失的 id 报错。
- **回归**(eval.md §3.6):固化"连续三天伊朗证据序列"eval,断言第 3 天报告正确引用第 1 天开、第 2 天结算的跟踪项;另加 DB-first 幂等测试(同日跑两次,第二次不重采)。

## 11. 验证

- `python -m py_compile` 全部脚本。
- SQLite 往返:建库→写 items→`prepare_report_data`(coverage + 无 prior_state)→`save_report_state` 写一期→再 `prepare_report_data`(应载入 prior_state)。
- `collect_news --only-missing` 离线验证:预置某源行→该源被跳过、不发请求。
