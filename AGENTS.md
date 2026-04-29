# AGENTS.md — Skills 仓库

## 一、Skill 设计核心理念

### 1.1 Skill 的本质:Prompt 优先,代码为辅

**Skill 不是一个程序,而是给模型的一套专业方法论。**

真正完成任务的始终是模型本身,`SKILL.md` 才是核心产物。脚本只是模型无法直接完成的原子能力的"抓手":

- 调用外部 API(Tushare、Tavily 等)
- 读取本地文件、数据库或特定格式
- 执行确定性的计算或格式转换

其余环节 —— 判断、分析、措辞、结构组织、风险提示 —— 都应交由模型完成。

一句话:**把"手"留给脚本,把"脑"留给模型。**

### 1.2 内容分层与渐进式加载

Skill 系统采用三层加载,每一层的 token 经济性差异巨大,写作时必须心里有数:

| 层级 | 内容 | 加载时机 | 体量约束 |
|---|---|---|---|
| Metadata 层 | `name` + `description` | 始终在 context | ~100 词 |
| 主体层 | `SKILL.md` 正文 | skill 被触发时进入 | 理想 < 500 行 |
| 资源层 | `scripts/` `references/` `assets/` | 按需 | 无上限 |

核心推论:**SKILL.md 主体越精简,触发成本越低,长方法论才有地方放。** 大段领域知识、案例库、字段字典、表格速查应该拆到 `references/` 下,在主体里只留纲要 + 指引("当遇到 X 情况,读 `references/x.md`")。

资源层有三种角色,不要混用:

- `scripts/` —— **可执行**代码。模型不读源码也能用,只看输入输出契约。一个脚本只做一件原子事。
- `references/` —— 模型**按需读**进上下文的文档。长方法论、行业知识、错误码字典、案例集放这里。
- `assets/` —— 模型**不读**,但出现在最终产物里的素材。模板文件、Logo、字体、样式表。


### 1.3 反模式警告

以下信号通常说明 Skill 的设计偏了,需要把逻辑迁移到正确的位置:

- **脚本里拼接完整 Markdown 报告并返回** —— 把"脑"交给了脚本
- **脚本内部又调用另一个 LLM 做判断** —— 应该把判断交给主模型
- **`SKILL.md` 只有一句"运行 `xxx.py`"** —— 没方法论 = 没 skill
- **大段 `if/else` 替模型做领域判断**(如"PE > 30 就输出估值偏高")
- **一个脚本承担"取数 + 分析 + 生成结论"全链路**
- **`description` 写成功能罗列而不是触发场景**(如"本 skill 提供 K 线、PE、财报分析功能")—— 见 §2.3
- **方法论塞爆 SKILL.md 主体而不拆 `references/`** —— 拉高每次触发的 token 成本

代码越大,Skill 越僵。保持脚本的原子性、把领域知识下沉到 `references/`,Skill 才具备可组合性与可演化性。

---

## 二、SKILL.md 编写规范

`SKILL.md` 同时承担两个角色:**触发说明书**(让模型知道何时用)和 **工作手册**(让模型知道怎么用)。两者缺一不可。

### 2.1 标准结构

```markdown
---
name: skill-name
description: 见 §2.3,这是触发的唯一字段,把"何时使用 + 做什么 + 不做什么"全写在这里
---

# Skill Name

## 目标                 # §2.2  做什么 / 不做什么 / 给谁用
## 适用场景与边界       # §2.3  在 description 已写过的基础上展开
## 领域方法论           # §2.4  判断框架、分析维度
## 工作流程             # §2.5  按产物切分的有序步骤
## 数据获取(脚本抓手) # §2.6  调用哪些脚本、参数、返回
## 输出规范             # §2.7  结构、长度、风格,带"为什么"
## 示例                 # §2.8  至少一个完整 input → output
```

### 2.2 目标(Goal)

目标段必须同时说明三件事:

1. **做什么**:一句话概括产出形态
2. **不做什么**:划清边界,避免过度触发
3. **给谁用**:面向的使用者与典型场景

> 反例:"这个 Skill 用来分析股票。"
>
> 正例:"基于 Tushare 数据,为 A 股个股生成基本面 + 技术面 + 估值 + 股东结构的投研简报。不覆盖港股、美股、加密货币;不提供买卖建议;面向具备金融常识的主动投资者。"

### 2.3 description:触发的唯一通道

**机制要点:**

1. **`description` 是 skill 触发的唯一依据。** 模型只看 metadata 决定要不要调 skill,所以"何时使用"的所有信息必须集中在 description,**不能放在正文里**(放正文里是看不到的——正文要在触发之后才被加载)。

2. **简单一步查询常常不触发 skill,即使描述完美匹配。** 例如"读这个 PDF"、"算下 5 + 3"模型会自己干。Skill 主要在多步骤、专业判断、格式特定的任务上稳定触发。所以堆触发词没用,真正该让 description 透露出来的是"这件事有专业判断、有固定流程、自己干会出错"。

3. **模型默认有 undertrigger 倾向(该用却不用)。** 因此 description 可以**略带推力**,明确写"当用户……时使用此 skill"。

**写作要求:**

- **场景化而非关键词化**:用"当用户要求……时"句式,而不是孤立罗列动词。
- **正例 + 反例同写**:"用于 X、Y、Z;不用于 A、B"。反例比正例更能防止误触发。
- **覆盖意图 × 表达方式的笛卡尔积**:同一意图常有多种措辞("分析 XX"、"XX 怎么样"、"帮我看看 XX"、"XX 最近表现如何"),都要纳入。
- **专名 + 任务动词都要有**:专名(K 线、PE、季报、ts_code)与动词(分析、生成、对比、筛选、推演)。

### 2.4 领域方法论(Philosophy)

**领域方法论是 Skill 区别于"让 Claude 凭通识回答"的关键。** 把领域内反复使用的思考框架显式写下来,例如:

- **股票分析**:基本面看五力 + 财务质量,技术面看趋势 + 量价 + 结构,估值看历史分位 + 横纵比较
- **代码审查**:安全性 > 正确性 > 可读性 > 性能
- **文档写作**:金字塔原理、结论先行、一段一意

方法论段的质量决定 Skill 的专业度。写得越具体,模型输出越稳定,越不容易退化到通识回答。

**长度策略**:如果方法论很长(超过 ~150 行),只在 SKILL.md 留纲要 + 索引,细节拆到 `references/` 下。

### 2.5 工作流程(Workflow)

以有序步骤描述模型如何推进任务。**核心原则:按"可独立验证的产物"切分,而不是按数量切分。**

每一步要回答三个问题:

1. **做什么**(动作)
2. **用什么**(脚本/reference/上下文)
3. **产出什么**(可被下一步消费、可被人核查)


### 2.6 能力抓手(脚本)

这一节对应代码部分,内容应极简:

- 脚本清单 + 一句话职责
- 调用方式、必需参数、返回格式
- 依赖的环境变量(如 `TUSHARE_TOKEN`)
- 常见错误与降级策略

**不要**在此描述脚本内部实现,那是脚本自身 docstring 的职责。

### 2.7 输出规范

不写输出规范,模型每次的产出都会漂移。但**与其堆砌 MUST,不如解释为什么**——模型理解了缘由会自动泛化,光看规则容易脆。

| 维度 | 怎么做 | 为什么 |
|---|---|---|
| 结构 | 标题层级、必选/可选段落明确列出 | 结构稳定,用户才能对比多份产出 |
| 长度 | 给字数范围或段落数上限 | 模型不约束就有越写越长的倾向,尤其是分析类任务 |
| 风格 | 中立分析性 / 报告性 / 口语化,选一种 | 风格不一致会让产出像不同人写的 |
| 数据呈现 | 小数位、单位、表格 vs bullet 统一 | 跨数据源拼装时,默认行为会一会儿 2 位、一会儿 4 位 |
| 附加要求 | 免责声明、数据时间戳、引用来源 | 投研、医疗、法律类内容的可追溯性 |

### 2.8 示例

至少给一个完整的 input → output。示例的价值不只在展示风格,更在替你**钉死边界条件**——例如当数据缺失、当行业属性模糊、当用户问得很短时,产出该长什么样。

---

## 四、ClawHub CLI 管理

### 4.1 登录与配置

```bash
clawhub login
clawhub whoami          # 查看当前登录状态
```

### 4.2 发布 Skill

```bash
clawhub publish <skill路径>
```

### 4.3 发布包边界

ClawHub 发布的是 **Skill 运行时包**，不是开发工作区快照。发布目录只应包含模型实际使用该 Skill 时需要加载或调用的资产：

- 必需：`SKILL.md`
- 可选：`scripts/`、`references/`、`assets/`、`examples/`、`README.md`

以下内容属于开发、评测或本地工作产物，**不得随 `clawhub publish` 提交**：

- `evals/`
- `benchmarks/`
- `*-workspace/`
- `benchmark.json`、`benchmark.md`
- `grading.json`、`timing.json`、`metrics.json`
- `transcript.md`、`review.html`、`feedback.json`
- `test_*.py`、`*_test.py`
- `.pytest_cache/`、`__pycache__/`

如果某个 Skill 目录下保留了 `evals/` 或 benchmark 脚手架用于本地迭代，发布前必须创建临时 staging 目录，只复制运行时资产到 staging，再执行：

```bash
clawhub publish <staging-skill路径>
```

当前 `clawhub publish` 没有 `--exclude` 参数，不要依赖 CLI 自动忽略开发资产。Windows / PowerShell 下推荐使用 staging 目录：

```powershell
$skill = "stock-ai-analyzer"
$stage = "$env:TEMP\clawhub-stage\$skill"
Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item "$skill\SKILL.md" $stage
Copy-Item "$skill\scripts" $stage -Recurse -ErrorAction SilentlyContinue
Copy-Item "$skill\references" $stage -Recurse -ErrorAction SilentlyContinue
Copy-Item "$skill\assets" $stage -Recurse -ErrorAction SilentlyContinue
Copy-Item "$skill\examples" $stage -Recurse -ErrorAction SilentlyContinue
Copy-Item "$skill\README.md" $stage -ErrorAction SilentlyContinue
clawhub publish $stage
```

不要直接对包含 `evals/`、benchmark 结果或 workspace 的完整开发目录执行 `clawhub publish`。

### 4.4 发布前检查清单

1. **确认发布包边界**：发布目录中不得包含 `evals/`、`benchmarks/`、`*-workspace/`、`benchmark.*`、`grading.json`、`review.html` 等评测/benchmark 资产。
2. **移除测试产物**：`test_*.py`、`*_test.py`、`.pytest_cache/`、`__pycache__/`
3. **安全审查**：运行 `skill-vetter`（见 §5）
4. **依赖声明**：`SKILL.md` 中环境变量、Python 包、外部服务全部列出
5. **触发验证**：抽样几个真实用户表达，确认能正确触发，且不会在无关任务中误触发
6. **方法论完整性**：目标、触发、方法论、工作流、输出规范、示例六段齐备
7. **示例可复现**：示例中的命令、参数、输出能实际跑通

---

## 五、安全审查

### 5.1 必守红线

- **禁止硬编码密钥**：代码中不得出现任何 key、token、password
- **禁止提交 `.env` 文件**
- **必须用环境变量读取敏感信息**：`TUSHARE_TOKEN`、`OPENAI_API_KEY` 等
- **外部输入需校验**：URL、查询参数、用户提供的片段清洗后再使用
- **Prompt 注入防护**：用户输入在拼接到 prompt 前需做边界标记或清洗
- **最小权限**：脚本只请求完成任务所需的最少权限与数据

### 5.2 skill-vetter

```bash
skill-vetter /path/to/skill
# 或在 skill 目录下
skill-vetter .
```

审查通过后方可发布。

---

## 六、环境变量

| 变量 | 描述 | 必需场景 |
|---|---|---|
| `TUSHARE_TOKEN` | Tushare Pro API token | 股票相关 Skill |
| `TAVILY_API_KEY` | Tavily 搜索 API key | iran-tracker 等可选 |
| `CLAWHUB_TOKEN` | ClawHub CLI token | 发布 Skill 必需 |
