# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库定位

本仓库是 **Skills 的 canonical source(唯一可信源)**,不是被消费的产物。各 Agent(Kimi CLI / Claude Code / Codex / Hermes / OpenClaw)在自己的品牌目录(`~/.claude/skills` 等)里读到的副本,是从这里通过 `scripts/skill_sync.py` 单向同步过去的。**不要在 Agent 安装目录里修改 skill** —— 下次同步会被覆盖。

详细方法论在仓库根目录的 `AGENTS.md`(被 `.gitignore` 但本地存在,需要时直接 Read)与 `eval.md`(评估方法论)。本文只列大图。

## 顶层架构

### Skill 的扁平化扫描规则

仓库中**任何含 `SKILL.md` 的目录都被视为一个独立 skill**,无论嵌套多深。同步时目录名直接提升到目标 `skills/` 根下。嵌套(如 `stock-skills/a-stock-analyzer/`)会被自动打平为 `a-stock-analyzer/`。

当前 skill 分布:

- `stock-skills/*` —— A 股研究矩阵(基本面、互动问答、公告、宏观、电报、日线复盘等),每个子目录是独立 skill
- `ch-news-reporter/` —— 多源新闻采集与日报生成
- `iran-war-tracker - V2/` —— 地缘冲突日报(同步时由 `skill-sync.yaml` 重命名为 `iran-war-tracker-v2`)
- `ch-pd-flow/` —— 产品设计/工作流相关(占位中)

### Skill 设计的核心准则(决定怎么改 SKILL.md)

1. **Prompt 优先,代码为辅**。SKILL.md 才是核心产物,脚本只做模型无法直接完成的原子能力(API 调用、文件 I/O、确定性计算)。判断、分析、措辞、风险提示必须留给模型。
2. **三层加载,token 经济性差异巨大**:
   - Metadata(`name` + `description`):始终在 context,~100 词
   - SKILL.md 主体:触发时进入,理想 < 500 行
   - `references/` `scripts/` `assets/`:按需加载,无上限
   - 推论:**长方法论应拆到 `references/`,SKILL.md 只留纲要 + 指引**。
3. **`description` 是 skill 触发的唯一依据**。"何时使用"的所有信息必须集中在 description,不能放在 SKILL.md 正文(触发前看不到)。要场景化("当用户……时")、正反例同写、覆盖意图 × 表达方式的笛卡尔积。
4. **`scripts/` / `references/` / `assets/` 不要混用**:
   - `scripts/` 是可执行代码,一个脚本只做一件原子事
   - `references/` 是模型按需读进上下文的文档
   - `assets/` 是不读但出现在最终产物里的素材(模板、Logo)
5. **受管 Skill 的生产交付必须可证明**。以 `skill-framework.yaml` 为范围，修改后运行 `python3 scripts/skill_factory.py build-managed`；最终文件只能经 `capabilities.yaml` 的 terminal capability 从 `.staging` 晋级，并同时具备成功 receipt 与通过的 gate audit。`scripts/skill_sync.py` 会拒绝同步过期的 `gate-plan.json`。

### 反模式(看到要改)

- 脚本里拼接完整 Markdown 报告并返回 —— 把"脑"交给了脚本
- 脚本内部又调用另一个 LLM 做判断
- SKILL.md 只有一句"运行 `xxx.py`"
- 大段 `if/else` 替模型做领域判断("PE > 30 就输出估值偏高")
- `description` 写成功能罗列而不是触发场景

完整规范、SKILL.md 标准结构(目标 / 适用场景 / 领域方法论 / 工作流程 / 数据获取 / 输出规范 / 示例)、评估五大维度见 `AGENTS.md` §2 与 `eval.md`。

### Skill 输出的通用文风要求(所有 skill 适用)

每个 skill 的 `输出规范` 都要落实下面两条项目级默认,写报告/产出时也按此执行:

1. **文风讲人话,减少机械与僵硬**。像跟懂行的人当面把一件事讲清楚那样写——句子通顺、有逻辑衔接,该解释因果、给判断时把话说透。避免模板腔、翻译腔和套话:别成段堆砌"综上所述""值得注意的是""总体来看",别把每条都写成生硬的"主语+动词+宾语"公式句,也别为了凑结构把话说断、只丢关键词。
2. **同项罗列优先用 list,但每条要说人话**。同一维度的多个条目(多条新闻、多个项目、多个信号、多方行动、多个观察点)拆成 bullet 或编号,一条一项,别塞进一个长段落;但每条用完整通顺的话写,**不要退化成"字段A - 字段B - 字段C"式的横杠拼接**。结构化对照(厂商 × 动作、指标 × 数值、节点 × 日期)才用表格。

为什么:这是用户明确提出的通用要求——机械的模板腔会降低可读性与可信感,而把同类信息摊成"说人话的 list"既好扫读又不丢判断。详见 `AGENTS.md` §2.7。

## 常用命令

### Skill 同步(本仓库 → 各 Agent 目录)

```bash
python scripts/skill_sync.py              # 一次性 copy 同步
python scripts/skill_sync.py --link       # 建立 Junction/Symlink(开发推荐,实时生效)
python scripts/skill_sync.py --dry-run    # 预览变更
python scripts/skill_sync.py --watch --interval 5   # 监控并自动同步
python scripts/skill_sync.py --install-hook         # 安装 post-commit / post-merge / post-rewrite 钩子
```

同步目标、重命名映射、排除规则全部在 `skill-sync.yaml`。修改 SKILL.md 后想立即在 Agent 中生效,先跑一次 `--link`(或已安装 git hook 时直接 commit)。

### 发布到 ClawHub

```bash
clawhub publish <skill路径>
```

**关键边界(§4.3)**:发布目录只能包含运行时资产(`SKILL.md`、`scripts/`、`references/`、`assets/`、`examples/`、`README.md`)。以下绝不能进发布包:`evals/`、`benchmarks/`、`*-workspace/`、`benchmark.*`、`grading.json`、`timing.json`、`metrics.json`、`transcript.md`、`review.html`、`feedback.json`、`test_*.py`、`*_test.py`、`.pytest_cache/`、`__pycache__/`。`clawhub publish` **没有 `--exclude` 参数**,必须自己做 staging。

### 安全审查

```bash
skill-vetter <skill路径>   # 发布前必跑
```

## 环境变量

| 变量 | 何时需要 |
|---|---|
| `TUSHARE_TOKEN` | 任何 `stock-skills/*` 取数 |
| `TAVILY_API_KEY` | `iran-war-tracker - V2`、`ch-news-reporter` 的深度搜索 |
| `CLAWHUB_TOKEN` | `clawhub publish` |

脚本必须从环境变量读取敏感信息,**禁止硬编码**;`.env` 已在 `.gitignore`。

### PostgreSQL 快速连接

数据库统一走 `shared/data/db_core.py`。默认后端是 PostgreSQL，优先读取 `ALPHA_PG_URL`，再兼容 `DATABASE_URL`，最后 fallback 到本机 Unix socket：

```bash
export ALPHA_DB_BACKEND=postgresql
export ALPHA_PG_URL="postgresql://alpha_user:alpha_pass@/alpha_data?host=/tmp"
python3 shared/data/db_ping.py --alpha-schema
```

如果当前 Agent 只能走 TCP：

```bash
export ALPHA_PG_URL="postgresql://alpha_user:alpha_pass@localhost:5432/alpha_data"
```

不要在具体 skill 里新建私有连接方案；新增数据库型 skill 时，把它加入 `skill-sync.yaml` 的 `shared.bundles`（data 能力 bundle），让 `shared/data/` 同步到安装包的 `scripts/_shared/`。详细约定见 `shared/data/POSTGRESQL.md`。

## 修改 Skill 时的工作流

1. 改 `SKILL.md` 或脚本
2. 如需立即在 Agent 中验证:`python scripts/skill_sync.py --link`(已安装 hook 则 commit 即可)
3. 测试通过后提交;post-commit hook 会自动跑同步
4. 准备发布:跑 `skill-vetter`,确认发布边界,再 `clawhub publish`

## 评估迭代要点(详见 `eval.md`)

- **布尔断言 + Rubric 评分双层**,各自聚合,不混。
- **不是每个 skill 都打全五维**(指令遵循 / 数据完整度 / 输出质量 / 主动思考 / 创新发现)—— 先确定 first-class metric 是哪两三个。数据提取类主看维度 1+2,投研分析类主看 3+4+5。
- **rubric 必须锚点化**("3 分意味着……"而非"3 分代表中等"),否则不同次跑分会漂。
- **同一轮迭代内冻结测试集**,跑 with_skill vs without_skill(或 new vs old)两组对比。
- 改完后**回归检查老 case 是否还过**。
