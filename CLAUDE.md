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

### 反模式(看到要改)

- 脚本里拼接完整 Markdown 报告并返回 —— 把"脑"交给了脚本
- 脚本内部又调用另一个 LLM 做判断
- SKILL.md 只有一句"运行 `xxx.py`"
- 大段 `if/else` 替模型做领域判断("PE > 30 就输出估值偏高")
- `description` 写成功能罗列而不是触发场景

完整规范、SKILL.md 标准结构(目标 / 适用场景 / 领域方法论 / 工作流程 / 数据获取 / 输出规范 / 示例)、评估五大维度见 `AGENTS.md` §2 与 `eval.md`。

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
