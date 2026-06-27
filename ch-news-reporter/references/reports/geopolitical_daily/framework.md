---
framework_version: geopolitics-v1-global-risk
profile: geopolitical_daily
regime_assumption: 全球多点摩擦期
supersedes: iran-v1-ceasefire
---

# geopolitical_daily 分析框架 · v1 全球地缘风险

> 本文件是 `geopolitical_daily` 的分析框架唯一可信源。机器区定义脚本校验用的 frame schema、regime 失效触发器和输出板块；模型区定义地缘日报的分型、区域主线、跨资产传导和写作纪律。
> 跨框架不变的方法论常量见同目录 `methodology.md`；活动状态机制见 `../watchboard.md`；跨资产传导框架见 `cross_asset_impact_framework.md`。

---

## 机器区 — 脚本只解析下面这一个 yaml 块

```yaml
frame:
  path:             {required: true, enum: [A, B, C, D]}   # A 局部缓和 / B 可控摩擦 / C 扩散升级 / D 系统冲击
  sub_branch:       {}                                     # 区域与触发器组合，如 MENA-energy / RUUA-front / Taiwan-deterrence
  intensity:        {}                                     # 1-10，按事实烈度与跨域传导综合评估
  focal_regions:    {required: true, type: list}           # 今日最重要的区域主线
  actor_weights:    {required: true, type: list}           # 今日最能改变路径的行为体
  signal_watchlist: {type: list}                           # 24-72h 核心观察信号
  transmission_channels: {type: list}                      # energy / shipping / sanctions / military / diplomacy / inflation / risk_assets
  probabilities:    {type: map, sum: 100}                  # {A, B, C, D}，和=100

invalidation_triggers:
  - 任一地缘主线演变为持续战争或正式停火后稳定重建，导致 A/B/C/D 分型无法解释主要风险 → 评估框架换代
  - 全球风险主因从地缘冲突切换为金融危机、疫情或自然灾害等非地缘冲击 → 评估框架换代
  - 地缘风险进入单一大战时 regime，日报不再适合多区域聚合框架 → 评估战时专框

output_sections: [全球风险速览, 区域主线, 能源航运与市场反应, 推演与观察, 本期变更]
```

---

## 模型区 — 领域判定逻辑与写作要点

### path 判定逻辑

每日报告先用过去 24 小时事实定边际，再用过去 7-14 天事实校准路径：

| 路径 | 判定主标志 | 排除证据 |
|---|---|---|
| **A 局部缓和** | 至少一条主要冲突线出现停火、谈判、撤军、制裁暂缓或第三方调解落地，且没有新的扩散信号 | 同一主线仍出现重大军事行动或制裁升级 |
| **B 可控摩擦** | 多区域有摩擦，但仍以外交、威慑、制裁、低烈度冲突为主，市场定价未出现系统性避险 | 任一主线形成跨境报复链或能源/航运实际中断 |
| **C 扩散升级** | 军事、制裁、能源、航运或代理人行动从单点扩散到多个行为体或多个区域 | 事件被迅速澄清、撤回或被有效管控 |
| **D 系统冲击** | 能源通道、主要航运线、核风险、大国直接冲突或全面制裁造成跨市场风险重估 | 冲击只停留在局部表态，未改变价格和政策约束 |

**判定纪律：**
- 单条新闻不直接改 path；必须看它是否改变能力、动机、约束或下一节点。
- 概率只在出现边际变化时调整；没有新增事实就沿用上一期并说明。
- 价格与事实不一致时，先检查市场是否提前定价，再判断是否是噪音。

### 区域主线与行为体权重

默认扫描这些区域主线，日报只展开今日有边际变化或高优先级 watchboard 项的部分：

- **中东与能源通道**：以伊、加沙/黎以、红海、霍尔木兹、海湾产油国、OPEC+。
- **俄乌与欧洲安全**：前线变化、远程打击、防空、北约援助、制裁和能源绕行。
- **台海与印太威慑**：军演、海空接触、美国/日本/菲律宾协同、出口管制。
- **朝鲜半岛**：导弹、核活动、边境摩擦、俄朝/中朝互动。
- **制裁与联盟重组**：OFAC/EU/UN/NATO/G7 等政策动作，及第三方国家调解或站队。
- **航运与供应链节点**：红海、黑海、波斯湾、南海、关键港口与保险费率。

`actor_weights` 写今天最可能改变路径的 3-6 个行为体，可以是国家、组织或联盟；排序必须说明理由。

### 烈度评级

| 级别 | 特征 |
|---|---|
| 1-2 | 外交表态、例行威慑，未形成新约束 |
| 3-4 | 低烈度摩擦、制裁或军演，局部资产有反应 |
| 5-6 | 确认军事行动、跨境报复、航运/能源节点受扰 |
| 7-8 | 多行为体卷入、能源/航运中断、避险资产同步重定价 |
| 9-10 | 大国直接冲突、核风险或系统性供应冲击 |

### watchlist 起点

1. 主要冲突线是否出现新增军事行动或跨境报复链。
2. 能源通道、航运保险、港口与关键基础设施是否有实际扰动。
3. OFAC/EU/UN/NATO/G7 等制裁、援助、军援或联合声明是否改变约束。
4. Brent、天然气、黄金、美元、美债、VIX、BTC 是否与风险路径一致。
5. 第三方调解是否从表态进入会议、文本或执行安排。

### 输出板块写作要点

- **全球风险速览**：写 path、烈度、今日 1-3 条真正边际变化和一句话判断。
- **区域主线**：按重要性展开，不按固定国家流水账；无新信号的区域只在 watchboard 保留，不占正文。
- **能源航运与市场反应**：用跨资产框架解释价格是否验证风险变化，不给交易建议。
- **推演与观察**：概率、下一关键节点、24-72h 观察清单必须与 watchboard 一致。
- **本期变更**：逐条结算上一期 open 项；旧中东专项迁移为中东母题，不得静默丢弃。

### 冷启动种子

首次运行时用下面结构作为脚手架，再用当天证据改写 path、区域、权重、概率和到期日期。

```json
{
  "regime": "全球多点摩擦期",
  "headline": "(用今天证据现写)",
  "tracking_items": [
    {"id": "T-001", "opened": "SEED", "statement": "中东能源与航运通道是否出现新增扰动", "links_to": "energy/shipping/path C-D", "status": "open", "expires_after": null},
    {"id": "T-002", "opened": "SEED", "statement": "俄乌与欧洲安全线是否出现军援或前线边际变化", "links_to": "military/sanctions/path B-C", "status": "open", "expires_after": null},
    {"id": "T-003", "opened": "SEED", "statement": "台海与印太威慑是否出现高烈度接触或联盟动作", "links_to": "military/diplomacy/path B-C", "status": "open", "expires_after": null}
  ],
  "next_nodes": [],
  "falsifiers": ["任一主线确认停火或谈判落地 → A 权重上升", "能源或航运实际中断并被价格确认 → D 权重上升"],
  "frame": {
    "path": "B",
    "sub_branch": "multi-region-controlled-friction",
    "intensity": 4,
    "focal_regions": ["中东", "俄乌", "台海"],
    "actor_weights": ["美国", "以色列/伊朗", "俄罗斯/乌克兰", "中国大陆/台湾", "欧盟/北约"],
    "signal_watchlist": ["新增军事行动", "能源航运扰动", "制裁与军援", "第三方调解", "Brent/黄金/VIX"],
    "transmission_channels": ["energy", "shipping", "sanctions", "risk_assets"],
    "probabilities": {"A": 15, "B": 55, "C": 25, "D": 5}
  }
}
```
