---
framework_version: ai-v1-agent
profile: ai_daily
regime_assumption: agent 是当前 AI 产品的主形态
supersedes: null
---

# ai_daily 分析框架 · v1 agent 主形态

> 本文件是 ai_daily **分析框架的唯一可信源**。框架契约——两条论点轴的 frame 维度、regime 失效触发器、输出板块——都收敛在这里。
> 跨框架不变的方法论常量（数据信息分层、输出判断边界、通用反例）见同目录 `methodology.md`；状态机制见 `../watchboard.md`；重点公司清单与别名见 `config/report_profiles.yaml` 的 `company_groups`（采集/归类字典，供 Agent 阅读）。
> **变更框架只改这一个文件。** 当 regime 质变（agent 不再是主形态）时，由慢思考层生成新版本框架整体替换，经人确认后切换。

---

## 机器区 — 脚本只解析下面这一个 yaml 块

```yaml
# frame schema：严格等同迁移前 report_profiles.yaml 的 state_schema。
# 两条论点轴取代旧的扁平 hot_themes，每条都带"走到哪一档"。
frame:
  agent_eng_vectors: {type: list, required: true}   # 轴一·工程演进矢量，至少 1 条
  product_forms:     {type: list}                   # 轴二·新产品形态与渗透
  watch_companies:   {type: list}                   # 预期近期有动作的前沿实验室/厂商
  watch_projects:    {type: list}                   # 待验证热度→趋势的开源项目

# regime 失效触发器：慢思考层检测命中即评估"框架换代"
invalidation_triggers:
  - 出现一种非 agent 的新 AI 交互范式并连续多日聚集证据成为主导 → "agent 主形态"假设失效，双轴框架换代
  - agent 工程演进的核心矢量集体进入"事实标准"、不再产生新增工程痛点 → 框架从"演进跟踪"转入"成熟期监测"
  - 主轴被全新第一序变量（如安全/监管成为主导）取代 → 框架重定义

output_sections: [一句话结论, 今日全景速读, 轴一·agent工程演进, 轴二·产品形态与渗透, 新项目雷达, 重点公司动态矩阵, 跨轴/中美速记, 风险与噪音, 未来24-72小时观察, 本期变更]
```

---

## 模型区 — 本框架的领域判定逻辑与写作要点

### 两条主轴

当前 AI 产品的主形态是 **AI agents**，本日报以 agent 为主轴，把当日多信源收敛成对两个问题的判断：

1. **轴一 · 工程演进（供给 / 能力侧）**：以 agent 为代表的形态，在工程与项目开发上往哪演进？两路看——前沿实验室（OpenAI / Anthropic / Google 等）在 agent 能力上推进了什么；开源项目在解哪些"建 agent"的工程痛点。多个证据同向 = 这条方向在**收敛**。
2. **轴二 · 产品形态与渗透（需求 / 采用侧）**：有没有新产品形态出现，能扩展 agent 渗透率、把使用量做大？新载体 / 新用户段 / 新自主度 / 新分发入口任一实质变化都算；没有就如实写"今日无新形态"。

两条主轴都横跨两个引擎：先用两个引擎把证据捞齐，再按两条主轴综合。大盘信息按"是否影响 agent 能力或形态"排序取舍，无关重大事件一句话带过。

### frame 字段填写约定

- **`agent_eng_vectors`（轴一）**：每条 `{vector, stage: 实验→收敛中→事实标准, open_pain（当前卡在哪）, side: lab|oss|both}`。至少 1 条。
- **`product_forms`（轴二）**：每条 `{form, segment: 开发者|专业用户|大众|企业, penetration_stage: 萌芽→早期采用→主流化, usage_signal（真实使用证据，非 star/votes）}`。
- **`watch_companies`**：预期近期有动作的重点公司。**`watch_projects`**：标了"值得继续观察"的开源项目/产品，带 id 进台账，等热度兑现成趋势再结算。

### 第一性原理：为什么用双引擎

AI 行业的边际信息在两个源头交替出现，单看都不够：公司只看不开源会丢早期信号（Agent/MCP/Coding Agent 的前沿常先在 Show HN 和 GH Trending 跑出来）；只看开源不看公司会丢规模化判断（真正决定能不能用、能不能赚钱的还是大厂 API、定价、产品入口）。所以先各自捞齐证据，再收敛到两条主轴。

### 引擎 A · 新项目发现（GitHub Trending + Product Hunt + Show HN）四步

1. **项目特征定性**：归入统一标签——基础模型与权重 / Agent 框架与编排 / 开发者工具链 / 推理与部署 / 数据与 RAG / 应用产品 / 评测与基准 / 开源基础设施。
2. **解决的 agent 工程痛点画像**：一句话回答"真实需求、给谁用"；agent 项目落到具体痛点（编排 / 记忆与上下文 / 工具标准化 MCP / 可观测 / 沙箱与运行时 / eval / 成本可靠性）。非 agent 项目登记但标"大盘背景"。
3. **热度真伪辨别**：star / votes / comments 只代表注意力。看 GH 的 stars/day、release、license、pricing、issues、push 活跃；看 PH 是否有 pricing/docs/API/enterprise 入口、是否 AI 原生；看 Show HN 回帖是积极采用还是技术质疑。
4. **趋势聚类**：同类项目当天 ≥ 2 个才单列一条方向信号，**单点不写趋势**。

### 引擎 B · 重点公司动作

完整公司清单与别名见 `config/report_profiles.yaml` 的 `company_groups`（国际：OpenAI/Anthropic/Google/Meta/Mistral/NVIDIA/HuggingFace；中国：Kimi/智谱/Minimax/腾讯/阿里/字节/DeepSeek/百度）。

按**四类动作矩阵**组织：① 模型发布（基础/推理/多模态/专用/开源权重/版本号）；② 产品更新（C 端 App、Agent 平台、IDE 插件、企业版）；③ API & 定价（新端点、企业入口、token 定价、免费额度）；④ 融资 & 政策（融资、并购、合作、监管、人事）。

**三个分析维度**：每个动作先过 agent 透镜（解锁了哪类 agent 能力 / 是不是新形态 / 是否降低渗透门槛，上提到对应主轴）；模型 vs 产品分开（别把加按钮当模型升级）；中美阵营对照（同一窗口内呼应还是错位）；商业化层级（研究→权重→API→产品→企业→生态，今天在哪层）。

### 两轴综合（证据写完后必做）

- **轴一定档**：每条矢量给档位（实验→收敛中→事实标准），判档看证据强度不看热度；**收敛判定**：同一矢量当天 ≥ 2 个独立证据（理想一个实验室 + 一个开源）攻同一痛点 → 进 `agent_eng_vectors`；写清 `open_pain`。
- **轴二五追问**：形态/载体、用户段、自主度（copilot→有监督→后台自治）、渗透信号 vs 注意力信号（只有真实使用进 `usage_signal`）、分发入口。给每条形态定档（萌芽→早期采用→主流化）。**无新形态时如实写"今日无新产品形态信号"，绝不硬凑**。
- **跨轴与中美对照**：供给↔需求是否对得上、开源↔公司谁填了谁的空、中美各自集中/空缺在哪条矢量或形态。

> 参考矢量库（从当日证据自然聚类，别硬套）：长任务可靠性 / 工具标准化(MCP) / 多智能体编排 / 记忆与上下文 / computer use / 推理成本与小模型 / agent eval / 沙箱与运行时。

### 各输出板块写作要点

- **一句话结论**：2-3 句覆盖轴一、轴二（无形态直说）、后续 24-72h 最该跟踪的 1-2 个对象。
- **今日全景速读**：4-6 条 bullet，每条引具体证据。
- **轴一 / 轴二**：按上面"两轴综合"组织；轴二用五追问表。
- **新项目雷达 / 重点公司矩阵**：证据层，表格；没有可核实动作的类别整段省略；中国厂商无官方信源写"无可核实更新"。
- **跨轴/中美速记**：3-5 条横向 bullet。**风险与噪音**：列不应过度解读项 + 降级理由。
- **未来 24-72h 观察**：三栏即本期 watchboard 投影（待印证矢量=`agent_eng_vectors`、待观察形态=`product_forms`、待发布/验证=`watch_companies`+`watch_projects`）。
- **本期变更**：渲染约定见 `../watchboard.md`。

### 冷启动

首次无上一期时，按当日两轴结果直接构造第一份 watchboard；若上一期还是旧的 `hot_themes` 字段，把那些方向迁移进 `agent_eng_vectors` / `product_forms`。
