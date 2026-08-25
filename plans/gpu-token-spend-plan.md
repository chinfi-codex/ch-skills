# GPU 算力监控 · 接入 AI Token 量价维度（方案已确认，口径已实测收敛）

日期：2026-08-25 ｜ 状态：**P0 已实现并跑通**（91/91 测试通过，端到端出证据包与仪表盘）｜
目标 skill：`ch-gpu-compute-monitor`

首日实跑结果（2026-08-24 结算）：529 行模型 × 变体观测，token 加权覆盖 99.57%，
付费 token 10.91T/日，名义 spend $8,693,024/日，混合价 $0.797/Mtok，
固定篮子 32 个家族、基期即首采日、指纹 `6c206dadf81e`，
token 锚定日与 GPU 锚定日 `alignment_lag_days = 0`。集中度告警按预期触发
（`stealth/ox-alpha` 占 32.3%），mix_shift 与三分解按预期 `usable=false`（序列仅 1 天）。

## 已拍板（2026-08-25）

1. **边界**：扩进现有 `ch-gpu-compute-monitor`，不新建 skill。它从「算力成本监控」变成
   「成本端 + 推理需求端」两端监控，`SKILL.md` 的 description 要相应改（当前明写
   「不覆盖模型推理 token 价格」，这句必须换掉）。
2. **深度**：**只做 P0**——量、双价格指数、mix 分解、spend 重建。成本地板与毛利池
   （原 P1）连同人工吞吐表一起推迟，但证据包要给它们留好插槽，将来补上时不改 schema。
3. **交叉验证**：先只用 OpenRouter 自建，不注册 Artificial Analysis key。

于是本轮**不做**：成本地板、毛利池、`config/attested_throughput.yaml`、token 侧告警。
面板只加一块（推理量价）。

---

## 一、这件事要解决的问题

现有 skill 只监控算力的**成本端**。它能告诉你 B200 涨了 23%，但答不出真正的那个问题：
**这么贵的算力，下游有没有足够大的收入池撑得住。** Token 维度补的是需求端。
两端放进同一份证据包，才能读出这类组合：

| 组合 | 读法 |
|---|---|
| GPU 租金↑ + token 量↑↑ + 真价格↓ | 真需求拉动，最健康 |
| GPU 租金↑ + token 量平 | 供给侧收缩或囤货，不是需求 |
| GPU 租金↓ + token 量↑ + 混合价↓ | 要先分清是真降价还是往便宜模型迁移 |

## 二、先否掉原始的 ATSI 公式

`ATSI_t = (V_t/V_0) × (P_t/P_0) × 100` 数学没错，但落到真实数据源上有硬伤：
**用量加权平均单价 ≡ 总支出 ÷ 总 token 数**，于是 `V × P ≡ 总支出`。同源是恒等式
（不如直接要 spend），异源（Silicon Data 的价 × OpenRouter 的量）则是两个不同市场的
交叉乘积。对数分解 `g_spend = g_V + g_P` 同样两边恒等，不可证伪。

**本方案做的是它的三分解**：把价格那一项拆成「真降价」与「购买结构迁移」两块，
spend 的增长才第一次变得可证伪。这也是当前市场上没人做的那块边际信息。

## 三、数据源（2026-08-25 全部 curl 实测）

| 用途 | 端点 | 认证 | 实测 |
|---|---|---|---|
| 量 | `openrouter.ai/api/frontend/v1/rankings/models?view=day` | 匿名 | 530 行，模型 × 变体级 prompt/completion token 与请求数，锚定 T-1 |
| 量（滚动） | 同上 `view=week` / `view=month` | 匿名 | 周 98T、月 320T。日期范围参数全部被忽略 |
| 价 | `openrouter.ai/api/v1/models` | 匿名 | 419 条，`pricing.{prompt,completion,input_cache_read,web_search}`，USD/token |
| 价（逐 provider） | `openrouter.ai/api/v1/models/{id}/endpoints` | 匿名 | 单模型最多 36 个 provider 报价，用来量化默认价的偏差 |
| 应用侧分母 | `.../rankings/apps` | 匿名 | Top 20 应用的日/周/月 token 与请求 |

## 四、六个口径决定（每条都有实测支撑）

### 1. join 键必须是「日期剥离后的 base + variant」，不是裸 slug

**这是全案最大的坑，我第一版就踩了。** `/api/v1/models` 里 `:batch` / `:free` 变体与
标准条目**共用同一个 `canonical_slug`**——实测 69 处碰撞：

```
anthropic/claude-opus-5-20260723 -> [claude-opus-5 $5.00/Mtok, claude-opus-5:batch $2.50/Mtok]
nvidia/nemotron-3.5-lightning-…  -> [标准 $0.08/Mtok, :free $0.00]
```

按 `canonical_slug` 建索引会让后写入的变体覆盖标准条目，于是**标准流量被按 batch 半价、
甚至按 free 零价计价**。修正前后差距不是小数点：

| | 修正前（裸 slug） | 修正后（base + variant） |
|---|---|---|
| 重建 spend | $5,329,299/日 | **$8,693,024/日**（+63%） |
| 混合价 | $0.529/Mtok | **$0.797/Mtok**（+51%） |
| 零价 token 占比 | 44.7% | 40.1% |

变体取值实测为 `standard` / `free` / `batch` / `thinking`，`variant_permaslug` =
`model_permaslug:variant`，与 pricing 里的带后缀 id 一一对应（61 个 `:batch`、
17 个 `:free`、1 个 `:thinking`）。**入库主键必须含 variant。**

### 2. 价格取模型默认价，同时报出 provider 价差带

同一模型在不同 provider 上单价不同，而我们看不到 provider 份额。实测按 spend 加权
（Top 10）：默认价 → provider 中位 **1.152x**、最低 **0.840x**、最高 **1.632x**。
一方独家的模型（Anthropic 系）默认价 = 最低 = 中位，价差全来自多 provider 的开源模型
（glm-5.2 有 36 个 endpoint，$0.50–$2.31；deepseek-v4-flash 有 28 个，$0.035–$0.44）。

**决定**：默认价做主口径（它接近中位、且唯一可日采），同时把 min/median/max 带宽
随 spend 一起报成不确定性区间。真实路由偏向便宜 provider，所以真值大概率落在
默认价与中位之间。

### 3. spend 的定义是「按挂牌价计的名义支出」，不是实际账单

这条必须写死在文档里，否则整个指标会被误读。实测：**prompt token 占付费 token 的
97.3%、占 spend 的 91.0%**，而 `input_cache_read / prompt` 的价格比实测 **0.119**
（覆盖 100% 的 prompt spend）——也就是缓存命中只按约 12% 计费。但
`total_native_tokens_cached` 在 530 行里全是 0，**命中率不可观测**。敏感性：

| 假设缓存命中率 | 实际账单 | 名义 spend 高估 |
|---|---|---|
| 20% | $7.27M | 19.6% |
| 40% | $5.85M | 48.7% |
| 60% | $4.42M | 96.5% |

**决定**：spend 一律申明为名义口径，并在证据包里带上这张敏感性表。更要紧的是
**趋势也被污染**——缓存采用率在上升，名义 spend 增速会系统性高于真实账单增速。
所以对外结论的重心放在**价格结构分解**（blended vs Laspeyres）而不是 spend 水平：
在「统一按 list price 计价」这个固定约定下，两个价格指数内部自洽，测的是价格结构，
不假装是账单。

### 4. Laspeyres 篮子的成员单位是「模型家族」，不是 slug

实测当日有 4 个家族同时挂着多个日期版本，合计占 **16.3%** 的 token，其中
`deepseek/deepseek-v4-flash` 一家 **13.8%** 横跨 `-20260423` 与 `-20260731` 两个版本。
若按 slug 当篮子成员，版本迭代会被整块记成「结构迁移」，mix_shift 会被系统性高估。

**决定**：篮子成员 = 剥掉 `-20YYMMDD` 后缀的家族 × variant，家族内部按 token 加权求单价。
这样**家族内的版本升级降价进入「真降价」**（同一产品线自己变便宜，正是最有价值的技术通缩），
**跨家族迁移才进入 mix_shift**（从 Claude 迁到 DeepSeek 是购买结构变化）。这个切法与
方法论想区分的四类"降价"对得上。

### 5. 零价的主体是 stealth 模型，不是免费档

零价 token 占 40.1%，但 `free` 变体只有 7.8%。真正的主体是榜首 `stealth/ox-alpha`——
`standard` 变体、pricing 明写 `"0"`、一家占全站 **32.3%**。这是匿名模型在免费放量。

**决定**：量的主轴只用 paid token；free 变体与零价 standard（stealth）**分成两条单列**，
不合并。stealth 模型进出榜单会让总量序列直接跳一大截，所以总量线只做背景，
不参与任何变化率结论。单模型份额 >25% 时打 `concentration_warning`。

### 6. 与 GPU 侧的日对齐：T-1 对 T-1，且 token 侧那一天已冻结

实测在 UTC 10:57 与 11:17 各取一次 `view=day`，2026-08-24 的 530 行**逐行完全一致**
（总量都是 18,352,911,061,978）——T-1 是已结算冻结的，不是滚动 24 小时累加。
Ornn 的日度指数同样是 T-1 UTC 结算，**两侧天然同日**，毛利池将来接上时不用跨日凑。

**未决（实施时补验）**：日切是否严格卡在 UTC 午夜，只靠今天一天的观测定不了，
需要跨日采集后回看。所以 token 侧照样复用现有 `anchor_date` / `anchor_basis` 机制，
并额外报 `alignment_lag_days`（token 锚定日与 GPU 锚定日之差），差不为 0 时在报告里点名。

## 五、指标设计

1. **量 `volume`**：日度 paid / free / zero-priced-standard 三条线分开存，外加
   prompt·completion 拆分、请求数、`tokens_per_request`。主轴 paid。
2. **价 `price`**：
   - `blended`（当期权重）= spend ÷ paid tokens，等价于 SDLLMTK 的公开可审计替代品，
     **单看没有意义**。
   - `laspeyres`（锁基期家族篮子权重 w₀，只让单价变）——测同一批家族自己降没降价。
   - **`mix_shift` = blended 变化 − laspeyres 变化**，本方案唯一的原创信息。
   - 篮子带 `basket_fingerprint`，成员进出走链式重挂并记断点。
3. **spend `spend`**：`Σ(prompt_tok × p_prompt + completion_tok × p_completion)`，
   名义口径 + 缓存敏感性带 + provider 价差带。只当水平锚，报出去的是三分解
   `g_spend ≈ g_volume + g_laspeyres + g_mix`。
4. **`cost_floor` / `margin_pool`**：本轮只定键位形状，值恒为
   `usable=false, reason="未启用（P1）"`。

## 六、口径纪律

1. **单位**：价格 USD/1M tokens、量 tokens/day、spend USD/day。与 GPU 侧的
   USD/GPU·hour 各走各的，不混。
2. **快照不可回填**：排行榜只给当下，序列从首采日开始长。与 Vast/Runpod 同一个不对称，
   报告必须讲清哪几条线不同龄。`view=month` 的滚动总量只能给上线首日一个水平锚，
   那是一个数不是一条线。
3. **覆盖率守卫**：`unmatched_token_share > 3%` 时 spend 标 `usable=false`。当前实测
   未匹配 0.43%，主要是 embedding / STT / rerank 这类不按 token 计价的模型。
4. **集中度守卫**：单模型份额 > 25% 打 `concentration_warning`（今天榜首 32.3%）。
5. **样本偏斜声明**：OpenRouter 是 merchant API 层的偏斜样本（编码 agent + roleplay
   主导），**不是全市场**。first-party 订阅、超大厂内部推理、企业直签合同全在外面。
6. **失败显式化**：前端接口未文档化，随时可能改。解析不出预期结构一律抛
   `CollectorError`，绝不返回 0 或沿用前值。
7. **拆不了的要承认**：`total_native_tokens_reasoning` / `_cached` / `total_tool_calls`
   三个字段全为 0，字段在值不给。reasoning token 通胀当前**拆不了**，只能用
   `tokens/request` 做代理，报告必须承认。

## 七、落地改动清单

**新增**
- `scripts/collectors/openrouter.py`：rankings 取量 + models 取价，按
  (base, variant) join，输出模型×变体级行，附覆盖率、零价份额、集中度、未匹配清单。
- `config/token_basket.yaml`：基期日、家族级篮子成员与权重锁、链式重挂记录。
- `references/token_taxonomy.md`：量价口径、四类"降价"的分辨、可比性规则、已知盲区。
- 表 `token_model_observations`，主键 `(obs_date, source, model_family, model_slug, variant)`。
  另带两个**为将来多源准备的判据字段**：`coverage_scope`（gateway / production_app /
  hyperscaler / benchmark）与 `price_basis`（list / realized / synthetic_blended）。
  本轮全部填 `gateway` / `list`，成本几乎为零，省的是以后加源时改表加回填（见第十一节）。

**改动**
- `config/sources.yaml`：注册 `openrouter`（`has_history_api: false`）。
- `scripts/collect.py`：`API_COLLECTORS` 加一项。
- `scripts/metrics.py`：新增顶层 `token_market` 段；evidence schema `1.0 → 1.1`。
- `scripts/render_report_html.py`：`PANEL_KEYS` 四块扩到五块，加「推理量价」整行面板。
- `references/report_template.md`：frontmatter 加 `verdict.token` 块；正文加一小节。
- `references/signal_reading.md`：加成因判别矩阵（只留不依赖成本地板的行）。
- `SKILL.md`：换掉"不覆盖模型推理 token 价格"，正文加"两端"方法论。

**明确不动**
- **不改评分权重**。token 是需求侧上游证据，与算力侧 tight/loose 不是同一根轴。
- **不加 token 侧告警**。序列还没长出来，阈值无从谈起。

## 八、P0 验收标准

1. `collect.py --sources openrouter` 单跑出摘要，含覆盖率、零价份额（分 free / stealth）、
   单模型最大份额、未匹配清单；接口改版时显式失败而非静默 0。
2. 证据包 `token_market` 段里 spend / blended / laspeyres / mix_shift 四个数各带
   `usable` 与 `reason`；spend 同时带缓存敏感性表与 provider 价差带。
3. 首日 HTML 出「推理量价」面板；序列只有一天时显示"序列自 YYYY-MM-DD 起累积"，
   不画假横线。
4. 报告必须出现三句话：merchant API 层的偏斜样本、spend 是名义口径不是账单、
   reasoning/cached 拿不到所以 token 通胀拆不了。

## 八之二、历史回填（2026-08-25 追加实现）

原方案判定"快照不可回填"，那是在只探到 `rankings` 的时候。后来探到
`/api/frontend/v1/rankings/market-share` **有真历史**：厂商级、周度、52 个点回到
2025-09-01，丢掉未结算的最新点后 51 个已结算周，`backfill.py` 一次拉满、无缺口。

两条硬事实决定了它必须单独存：

1. **最后一个点是活的。** 相隔几分钟的两次取数，它从 2.78443e13 **降到** 2.75664e13——
   会往下走说明是滑动窗口而不是在累积。倒数第二点两次逐键一致。采集时就丢。
2. **与日榜不是一个口径。** 同日按厂商比对，比值 1.42（xiaomi）–1.75（google），
   非常数倍。所以落进另一张表 `token_volume_history`，`unit_basis` 标
   `provider_reported_unverified`，只出份额、增速、指数，不出绝对水平。
   桥接比值等日度序列盖满一个已结算周后自动算得出来，在那之前报 `usable=false`。

粒度只有厂商级（grouping / by / level / dimension / limit 五个参数都试过，
不改变返回的键集合），所以厂商内部的模型迁移看不见——这是固有盲区，写进文档。

**结构效应指数**（`Σ 历史份额 × 今天的厂商均价`，只有份额在动）首跑：
2025-09-01 = 100 → 2026-08-17 = **44.8**，即光是买家往便宜厂商迁移这一项，
过去一年就把均价拉低约 55%；同期量指数 100 → 2030。份额从一年前的
x-ai 25.2% / google 21.4% / anthropic 15.0%，变成 deepseek 21.9% / stealth 12.4% /
xiaomi 11.2%。

## 八之三、日度构成图（2026-08-25 追加实现）

日度量原来是一条"付费 token"单线，信息量太薄。改成**堆叠面积图**：总量拆成
前 7 个模型 × 变体 + 「其他」，配一张带色块的图例表（份额 / token / 调用次数 /
tok每次 / 名义 spend / 是否计价）。两条口径写死在实现里：

- **条带由锚定日的排名一次定死**，之后每天按同一组条带拆。每天各取前 N 名的话，
  同一条带子今天代表 A、明天代表 B，叠出来的面积看着连续其实是拼接。
- **「其他」是减出来的余数**（当日总量 − 前 N 之和），不是把长尾再数一遍。

排名依据 `composition.rank_by` 默认 `tokens`（与图上画的量自洽），改成 `requests`
就按调用次数排。两者差别很大：实测 `deepseek-v4-flash-20260423` 有 7300 万次调用
只占 4.4% token，而 `stealth/ox-alpha` 用 7000 万次调用吃掉 32.3%。

序列只有 1 天时自动退成**横向 100% 堆叠条**——面积图需要至少两天才有"面积"，
但构成本身当天就成立，显示"暂无数据"是浪费。攒够 2 天自动切回面积图。

## 九、留到实施期验证的两件事

- **日切是否严格 UTC**：需要跨日采集后回看当日行的落定时刻。
- ~~日度模型级历史~~：已确认只有厂商级周度那一路能补，见第八之二节。
- **能否用每日采 `view=month` 的相邻差分构造 30 日滚动序列**：分母巨大、榜单成员变动、
  四舍五入三重噪声叠加，站不站得住要拿两天以上真实数据验，验不过就老实说序列从首采日起。

## 十、推迟的分期

| 期 | 内容 | 依赖 |
|---|---|---|
| P1 | 成本地板 + 毛利池 + 吞吐表 + token 侧双边告警 | 人工维护吞吐表 |
| P2 | Artificial Analysis 第二价格源 / Vercel spend 交叉验证 | 注册免费 key |
| P3 | Epoch 能力恒定价 + 超大厂季度披露 token 量 | 人工季度录入 |

## 十一、将来接入其他源的整合规则

本轮的量价基础只有 OpenRouter 一家。将来加 Artificial Analysis / Vercel / 超大厂披露时，
**不许把它们平均或合并成一个"全市场指数"**——它们不是同一个指标的多次测量，
而是不同指标的碎片。

### 源 × 角色

| 源 | 实际给的是 | 够格担任的角色 |
|---|---|---|
| OpenRouter | 量 + 挂牌价，唯一两边都有 | **主干**：唯一能做 mix 分解的源，主指数只能由它建 |
| Artificial Analysis | 逐模型价格与吞吐，没有量 | 价格校验位：校篮子的价，产出背离度，不合并成新价 |
| Vercel AI Gateway | 真实账单支出，没有独立价格指数 | 水平校验位：只比增速方向，且须归一到 per-app |
| 超大厂披露 | 量，且含无价格的内部推理 | 规模锚：季度频率，说明我们看的这块占全市场多小 |
| Epoch | 能力恒定价 | 另一根轴，不与上面任何一层相减 |

### 三条机制

1. **用字段钉死口径**：`coverage_scope` + `price_basis` 在 token 侧的地位等同 GPU 侧的
   `price_type` + `market_segment`。**同 (coverage_scope, price_basis) 才能进同一条序列**，
   不同的只能做背离度。
2. **证据包分层**：`token_market.index` 是主指数并注明 `built_from: ["openrouter"]`，
   `token_market.sources.{…}` 各存各的原始视图。加源是加 key，不改结构。
3. **唯一合法的合并场景**：出现第二个同样能给量又能给价的网关时，才可按各自 token 量
   加权合成"网关层中枢"。除此之外一律不合并。

### 三个必须防的坑

- **源集合变化会让合成中枢凭空跳。** GPU 侧实测过：新接入 Runpod+Vast 当天 H100 跨平台
  中枢"跌" 25%，那是口径变动不是市场。token 侧若做合成，同样要上 `basis_match`，
  源集合变更当天把 `comparable` 打成 false。
- **Vercel 的 spend 不能和我们的 spend 相加。** 都叫支出、量纲相同，但一个是真实账单、
  一个是挂牌价名义值。而且 Vercel 是公司指标，含它自己抢网关份额的 S 曲线。
- **AA 的 blended price 与我们的 blended 不是一个东西。** 它按固定 input:output 比
  （通常 3:1）合成，我们按真实 prompt/completion 权重（实测 prompt 占付费 token 97.3%）。
  要校验必须用同一个合成假设把我们的重算一遍再比，否则背离度全是口径造成的假象。
