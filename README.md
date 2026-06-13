# ch-skills

跨 Agent 的 Skills **唯一可信源（canonical source）**。这里写一次，单向同步到 Kimi CLI、Claude Code、Codex、Hermes、OpenClaw 各自的安装目录（`~/.claude/skills` 等）。

主线是一套 **A 股研究矩阵** 加 **多源新闻日报**：基本面投研、盘后复盘、公告/互动问答/电报采集、个股归因、美股与宏观监控，外加 AI / 宏观 / 地缘三类固定主题日报。

## 这是什么

每个含 `SKILL.md` 的目录就是一个独立 skill，模型读 `description` 决定何时触发、读 `SKILL.md` 主体执行方法论。本仓库遵循一条核心分工：

- **Prompt 优先，代码为辅。** `SKILL.md` 才是产物核心；脚本只做模型做不了的原子事——API 取数、文件 I/O、确定性计算。判断、分析、措辞、风险提示一律留给模型，脚本里不调用任何 LLM。
- **三层按需加载。** `description` + `name` 常驻上下文，`SKILL.md` 主体触发时加载，`references/` `scripts/` `assets/` 用到才读。长方法论拆进 `references/`，主体只留纲要。

各 Agent 安装目录里的副本是同步产物——**不要在那边改 skill，下次同步会被覆盖**，改动只在本仓库发生。

## Skill 目录

**A 股研究矩阵**（`ch-stock-skills/`，每个子目录是独立 skill）：

- `a-stock-analyzer` — 个股基本面投研方法论。成长性、估值隐含预期、财务质量、主线归属、竞争格局与风险排查；要求"深挖/读年报找线索"时进入命题先行的 Deep 模式。
- `a-stock-daily-market-sense` — 盘后市场研报。基于 Tushare 日线与 Baostock 风格指数，做盘面趋势、成交额集中度、赚钱效应与上涨主线、爆量下跌、市场风格对比与特征分组。
- `chstock-cninfo-announcement` — 巨潮资讯网公告查询与打标，输出 JSON 证据。
- `chstock-interactive-qa` — 互动易/投资者问答检索，过滤低信息密度后结构化输出。
- `chstock-macro-monitor` — 宏观行情数据包（汇率、美债、黄金原油、CPI/PPI/社融/PMI 等），金十 MCP 优先、多源兜底。
- `chstock-market-telegraphs` — 财联社 CLS 与金十电报**全量原文**，不筛选、不摘要、不截断。
- `chstock-rise-attribution` — 个股当日上涨归因，按 AlphaVault 查询协议产出 Evidence Pack，只输出中高置信归因。
- `chstock-usmarket-report` — 美股观察池日报，以纳指 100 为锚，逐票明细 + 全市场 ±7% 中大盘异动扫描。

**新闻日报**：

- `ch-news-reporter` — 金十 / GitHub Trending / Product Hunt / Hacker News / RSS 多源采集，建统一新闻表，产出 AI 日报、每日宏观日报、伊朗/中东局势动态三类固定主题日报，可导出 HTML。

## 仓库结构

```
ch-skills/
├── CLAUDE.md              # 给 Agent 的工作指引（架构、准则、命令）
├── skill-sync.yaml        # 同步目标 / 重命名映射 / shared bundle / 排除规则
├── scripts/
│   ├── skill_sync.py      # 本仓库 → 各 Agent 目录的单向同步器
│   ├── migrate_stock_to_pg.py
│   └── repair_market_history_board_rule.py
├── shared/                # 跨 skill 共享运行时（同步时打包进各 skill）
│   ├── data/              # PostgreSQL 连接层 db_core + 自检 db_ping + schema + 契约
│   └── html_report/       # Markdown→HTML 报告框架 + 内联图表 chartkit
├── ch-stock-skills/       # A 股研究矩阵
└── ch-news-reporter/      # 多源新闻日报
```

## 同步到各 Agent

同步目标、重命名映射、`shared/` bundle 归属、排除规则全部在 `skill-sync.yaml`。任何含 `SKILL.md` 的目录都会被自动发现，嵌套路径同步时打平到目标 `skills/` 根下（如 `ch-stock-skills/a-stock-analyzer/` → `a-stock-analyzer/`）。

```bash
python scripts/skill_sync.py              # 一次性 copy 同步
python scripts/skill_sync.py --link       # 建 Junction/Symlink，开发时实时生效
python scripts/skill_sync.py --dry-run    # 预览变更
python scripts/skill_sync.py --watch --interval 5   # 监控自动同步
python scripts/skill_sync.py --install-hook         # 装 post-commit / post-merge / post-rewrite 钩子
```

装了 git hook 后，commit 即自动同步；否则改完 `SKILL.md` 先跑一次 `--link`。

## shared/ 共享运行时

`shared/` 不是文档，是会被同步器打包进指定 skill 的运行时代码，保证同步/发布后的 skill 自包含。按能力分 bundle：

- **data**：PostgreSQL 连接层。所有数据库型 skill 走 `shared/data/db_core.py`，同步后落在安装包的 `scripts/_shared/db_core.py`，skill 脚本里 `from db_core import` 不变。新增数据库型 skill 要把它加进 `skill-sync.yaml` 的 `shared.bundles`。
- **html_report**：盘后/日报的 HTML 渲染框架，Markdown→HTML 加内联 SVG 图表，`a-stock-analyzer`、`a-stock-daily-market-sense`、`chstock-usmarket-report`、`ch-news-reporter` 共用。

边界原则：按"通用能力 vs 领域判断"切分，不按使用者数量。通用图表类型、连接层留 `shared/`；领域判断留各 skill。

## 发布与审查

```bash
skill-vetter <skill路径>      # 发布前安全审查，必跑
clawhub publish <skill路径>   # 发布到 ClawHub
```

发布包只能含运行时资产（`SKILL.md`、`scripts/`、`references/`、`assets/`、`examples/`、`README.md`）。评测产物、workspace、`test_*.py`、`__pycache__/` 等绝不入包；`clawhub publish` 没有 `--exclude`，需自己做 staging。

## 环境变量

| 变量 | 何时需要 |
|---|---|
| `TUSHARE_TOKEN` | 任何 `ch-stock-skills/*` 取数 |
| `TAVILY_API_KEY` | `ch-news-reporter` 深度搜索 |
| `CLAWHUB_TOKEN` | `clawhub publish` |
| `ALPHA_PG_URL` | PostgreSQL 连接，数据库型 skill 必需 |

脚本一律从环境变量读敏感信息，**禁止硬编码**；`.env` 已在 `.gitignore`。PostgreSQL 默认走本机 Unix socket，连接自检：

```bash
export ALPHA_DB_BACKEND=postgresql
export ALPHA_PG_URL="postgresql://alpha_user:alpha_pass@/alpha_data?host=/tmp"
python3 shared/data/db_ping.py --alpha-schema
```

## 深入

`CLAUDE.md` 是给 Agent 的完整工作指引——顶层架构、SKILL.md 标准结构、反模式清单、修改 skill 的工作流。改 skill 前先读它。
