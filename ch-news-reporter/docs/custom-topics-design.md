# 自定义关注主题(Custom Topics)— 设计文档

> 状态:**Claude 已评审(2026-07-18,Claude Code 2.1.142),O1–O4 全部有结论,待用户最终拍板。**
> 已锁定决策见 §2,评审结论见 §3。本文是 ch-news-reporter「自定义关注主题」扩展的唯一设计稿。

## 1. 背景与目标

ch-news-reporter 现有三个固定日报(AI / 宏观 / 地缘),走「统一新闻库 → profile 证据包 → 方法论分析 → watchboard 跨天滚动」管线。用户需要在此之上扩展**自定义关注主题**:

- 同时跟踪 **5–10 个**窄主题,例如:
  1. 英伟达 Rubin 出货状态(是否顺利、量级)
  2. Kimi 算力部署(部署量级;能否验证"更少算力达到 GPT-4o 同级效果")
  3. 华为升腾芯片下半年出货进展与量级
  4. 日常工作中随时产生的新关注事项
- 信息来源不止现有 collect_news,还要补:Alpha Vault 知识库、本地 PG 库、实时 Web 搜索、以及未来其他通道。
- **每天形成汇总,汇总形态 = 一份合并日报**(2026-07-19 用户修订:「单主题」指 News Reporter 层面的一个主题,各关注事项是报告内板块,不各出一份)。

## 2. 已锁定决策

- **D1 每日汇总形态(2026-07-19 用户修订)**:「自定义主题」是 News Reporter 里一个与固定三日报平级的主题,`custom_topics.yaml` 里注册的各关注事项(英伟达 Rubin / Kimi 算力 / 华为升腾等)是其内部板块。每日产出**一份合并日报** `reports/custom_daily_<date>.md`,事项作为报告内板块(有增量 300-800 字,无增量 ≤100 字一句带过),不按事项各出一份。单事项有重大进展时,可应用户要求额外出该事项的单主题深挖报告(`custom_<slug>_<date>_deep.md`)。watchboard 跨天状态仍按事项各自滚动(`custom_<slug>`),合并日报只是展示层。
  （修订前:每主题一份独立日报——用户澄清"单主题汇总"指 News Reporter 层面的单主题,而非事项级。）
- **D2 多通道检索层**:证据获取从「profile 关键词过滤」升级为「每主题一组 retriever 通道」,脚本只取数/去重/落库/打包,判断全归模型。通道清单:
  - **A. 新闻库检索**(`news_db`):`items` 表关键词 + 全文检索,时间窗可配(`time_window_days`,日频主题 1–3 天,低频主题 7–14)。
  - **B. PG 数据查询**(`pg_data`):复用 `db_core` 查 `alpha_data` 结构化表(行情 `stock_daily`/`stock_index_daily`、个股跟踪 `stock_tracking_*`、主线 `theme_*` 等),按主题配置挂钩(如关联 ticker);字段设计保持可扩展(见 §4)。
  - **C. 实时网络搜索**(`web_search`):复用 `shared/web_search/tavily_search.py`,每主题一组定制 query,有每日 query 预算上限与全局日预算封顶。
  - **D. Alpha Vault 知识库**(`vault_fs`):**本地 Obsidian Vault 文件系统读取**(O1 已澄清,见 §3),按配置的 vault 内相对路径检索 Markdown 笔记。
  - **E. 扩展位**:新通道 = 在 `scripts/retrievers/` 下新增一个原子脚本 + 配置项,不改编排逻辑。
- **D3 主题生命周期**:`status: active / paused / archived`。随时新增(onboarding 流程)、随时暂停、跟完归档;归档主题不再每日检索,历史 watchboard 保留可回看。
- **D4 配置独立文件**:不动 `report_profiles.yaml` 三个固定日报,自定义主题放 `config/custom_topics.yaml`,profile 名 = `custom_<slug>`;slug 禁止与固定 profile(`ai_daily` / `macro_daily` / `geopolitical_daily`)冲突,加载时校验、冲突即报错。
- **D5 通用分析层**:不为每个主题手写 framework.md;分析锚点 = 通用 `custom_topic/methodology.md` + 该主题 `focus` 自然语言描述。符合「脑留给模型」原则。
- **D6 复用现有管线**:watchboard 滚动、`save_report_state.py` 回写、HTML 渲染(`render_report_html.py`)全部复用,不为自定义主题另建状态机制。
- **D7 检索器职责拆分**(采纳 Claude 意见):`topic_retrieve.py` 只做编排,通道实现拆成 `scripts/retrievers/` 下原子脚本(`news_db.py` / `pg_data.py` / `web_search.py` / `vault_fs.py`)。

## 3. 开放点评审结论(Claude,2026-07-18)

- **O1 Alpha Vault 知识库 → 已澄清:本地 Obsidian Vault,不是远程服务。**
  Claude 侧记忆与文件系统核实一致:AlphaVault 是用户的本地 Obsidian Vault,根目录
  `~/Library/CloudStorage/OneDrive-个人/Obsidian-Vault/1-AlphaVault/`(已核实存在),
  内含 `wiki/01-个股词条`、`02-概念词条`、`03-产业链图谱`、`05-行业研判`、`10-产业跟踪` 等结构化笔记,
  查询/写入协议见 `system/frameworks/20-查询协议.md`。
  **结论**:通道 D 实现为 `vault_fs`(本地文件系统通道),配置 `enabled` + `paths`(vault 内相对路径/glob 列表),
  用 `pathlib`/`glob` 读 `.md`,按 frontmatter 与正文做本地检索,不依赖任何 API key。
  实现时先读 `20-查询协议.md`,遵守 vault 自身的查询约定。
- **O2 Web 搜索结果落库 → 同意落库,补三条约束。**
  ① 落库记录必须带 `retrieved_at` 与原始 `query`(写入 `metadata_json`),否则证据无法回指到具体检索动作;
  ② 加保留策略:配置顶层 `web_search.retention_days: 90`,由清理逻辑定期删除过期 `web_search` 行,防止胀表;
  ③ 去重哈希不能只靠 URL——Tavily 结果可能与 collect_news 抓到同一篇文章,去重要同时覆盖 URL 与标题级判重。
- **O3 watchboard 默认开启 → 同意,`open_budget: 5`;另允许 `open_budget: 0` 显式关闭,与 `state_enabled: false` 等价。**
- **O4 检索预算 → 同意方向,调保守并加全局封顶。**
  新主题 onboarding 期默认 `max_queries_per_day: 2–3`,稳定后可升到 4;
  配置顶层加 `web_search.global_max_queries_per_day: 30`,主题数膨胀时防止 Tavily 费用失控;
  `time_window_days` 日频 3 天,低频主题写明可配 7–14 天。

### Claude 对 §4/§5 的采纳意见(已并入本文)

1. `alpha_vault` 从布尔改为结构化配置(`enabled` + `paths`)——已并入 §4。
2. `pg_data` 字段保持可扩展,未来可加 `tables` / `custom_sql`——已并入 §4 注释。
3. 新增可选 `frequency: daily | weekly`(默认 `daily`),低频主题不天天出"无增量"短报——已并入 §4。
4. `keywords` 文档说明支持引号短语(如 `"Rubin ramp"`)——已并入 §4 注释。
5. `topic_retrieve.py` 拆为编排层 + `retrievers/` 原子脚本——已并入 D7 与 §5。
6. 实现时确认 `items.source_type` 无需迁移即可容纳 `web_search`(现有为 TEXT 自由值,预计无需 DDL,实现时验证)——已列入 §5。
7. slug 与固定 profile 冲突校验——已并入 D4。

## 4. 配置 Schema(`config/custom_topics.yaml`)

```yaml
# 顶层全局设置
settings:
  web_search:
    global_max_queries_per_day: 30    # 全部主题合计的 Tavily 日调用封顶
    retention_days: 90                # web_search 落库证据的保留天数
  vault_root: "~/Library/CloudStorage/OneDrive-个人/Obsidian-Vault/1-AlphaVault"

topics:
  nvidia_rubin:
    title: 英伟达 Rubin 出货跟踪
    status: active                  # active / paused / archived
    frequency: daily                # daily / weekly;weekly 主题不出每日"无增量"短报
    focus: |                        # 自然语言关注问题,注入分析层当锚点
      判断 Rubin 出货是否顺利、量级如何;关注供应链(台积电/CoWoS/HBM)、
      云厂商 capex 指引、管理层表态中关于 Rubin ramp 的边际信息
    keywords: [Rubin, '"Rubin ramp"', Vera, CoWoS, HBM4]   # 支持引号短语
    exclude_keywords: []
    time_window_days: 3             # 日频 1–3;低频主题 7–14
    channels:
      news_db: true
      pg_data:
        stock_tickers: [NVDA, 2330.TW]
        # 可扩展:未来支持 tables / custom_sql
      web_search:
        queries:                    # onboarding 时模型起草、用户确认,滚动维护
          - "NVIDIA Rubin shipment ramp 2026"
          - "Rubin CoWoS capacity allocation"
        max_queries_per_day: 3      # onboarding 期 2–3,稳定后可升 4
      alpha_vault:
        enabled: true
        paths:                      # vault 内相对路径,支持 glob
          - "wiki/03-产业链图谱/"
          - "wiki/10-产业跟踪/"
    state_enabled: true
    open_budget: 5                  # 0 = 显式关闭跨天状态
```

## 5. 改动清单

| # | 文件 | 动作 |
|---|---|---|
| 1 | `config/custom_topics.yaml` | 新建(含 settings + topics) |
| 2 | `scripts/topic_retrieve.py` | 新建:检索编排(调各 retriever、去重、按 O2 落库、出主题证据包) |
| 3 | `scripts/retrievers/news_db.py` | 新建:items 表关键词 + FTS,时间窗过滤 |
| 4 | `scripts/retrievers/pg_data.py` | 新建:alpha_data 结构化表查询(走 `db_core`) |
| 5 | `scripts/retrievers/web_search.py` | 新建:Tavily 批量 query + 预算控制 + 落库(带 query/retrieved_at/去重) |
| 6 | `scripts/retrievers/vault_fs.py` | 新建:AlphaVault 本地 Markdown 检索(先读 `20-查询协议.md`) |
| 7 | `scripts/prepare_report_data.py` | 支持 `--topic <slug>` / `custom_<slug>` profile,输出主题 evidence packet(含 coverage + prior watchboard) |
| 8 | `scripts/profile_config.py` | 合并加载 topics 配置 + slug 冲突校验 |
| 9 | `references/reports/custom_topic/methodology.md` + `template.md` | 新建:通用方法论与单主题日报模板(含"今日无增量"短报形态) |
| 10 | `scripts/save_report_state.py` | 支持 `custom_<slug>` watchboard 回写与结构校验 |
| 11 | `SKILL.md` | description 补自定义主题触发场景;工作流程加「自定义主题日报」与 onboarding 一节 |
| 12 | 实现时验证 | `items.source_type` 容纳 `web_search` 无需 DDL;`web_search` 行清理逻辑(retention_days)落在 `topic_retrieve.py` 或独立 groom 入口 |

## 6. 每日运行流程(每主题)

1. `collect_news.py --date today --only-missing`(照旧,刷新新闻库)
2. `topic_retrieve.py --topic <slug> --date today`(多通道检索 → 去重/落库 → 主题证据包;支持 `--topic all` 遍历 active 主题)
3. 模型读 `custom_topic/methodology.md` + 主题 `focus` + 证据包 + prior watchboard,分析
4. 按 `custom_topic/template.md` 写**一份合并日报** `reports/custom_daily_<date>.md`(各事项为板块)
5. `save_report_state.py --profile custom_<slug>` 回写 watchboard
6. (可选)`render_report_html.py` 出网页版

## 7. 首批 onboarding 主题(实现后建)

1. `nvidia_rubin` — 英伟达 Rubin 出货跟踪
2. `kimi_compute` — Kimi 算力部署与效率验证
3. `huawei_ascend` — 华为升腾芯片出货进展

每个主题的 keywords / search queries / 通道挂钩在实现完成后由模型起草、用户确认落盘。
