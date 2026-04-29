## 前置说明

- 先定位当前项目上下文：项目 `slug`、当前工作分支、当前 feature 名称或任务名
- 在开始任何判断或文档产出前，先读取现有上下文文档，再进入提问或写作
- 读取上游文档时，区分项目级文档与需求级文档：
  - 项目级文档：读取最新的 `project memo`
  - 需求级文档：先确定唯一 `feature-slug`，再读取该目录下的相关上游文档
- 优先读取顺序：
  1. 最新的 `project memo`
  2. 与当前 `feature-slug` 对应的最新 `feature brief`
  3. 与当前 `feature-slug` 对应的最新 `PRD`
  4. 与当前 `feature-slug` 对应的最新 `pd-review-report` 或已有评审结论
- 所有正式产物统一写入 artifact 根目录，不把关键上下文散落在临时回复中
- 统一行为边界：
  - 只做产品工作流内的判断、提问、整理与写作
  - 不输出技术实现方案、数据库设计、API 设计、任务拆解
  - 不把关键决策推迟到“实现时再说”
  - 若上下文不足，先显式说明缺口，再进入单问题补充
  - 若发现已有文档与当前结论冲突，必须指出并在新产物中统一口径

## `feature-slug` 识别规则

- `feature-slug` 是需求级唯一稳定标识，用于定位 `./prd/features/<feature-slug>/`，默认使用中文
- 当用户直接提供 `feature-slug` 时，优先按该 slug 定位
- 当用户提供中文需求名或口语化需求描述时，先在 `./prd/features/` 下做匹配，再决定是否继续
- 匹配时只使用可解释规则，不使用不可解释的模糊猜测

匹配输入来源：
- 目录名 `feature-slug`
- 文档头部的 `feature_slug`
- 文档头部的 `feature_name`
- 文档标题

匹配结果分为三类：
- `EXACT_MATCH`
  - 唯一高置信命中
  - 可直接继续，但必须回显：`当前需求已匹配到 <feature-slug>（<feature_name>）`
- `AMBIGUOUS_MATCH`
  - 存在多个合理候选
  - 必须提一个单问题确认，不能自行选择
- `NO_MATCH`
  - 没有可接受候选
  - `/pd-plan` 可作为新需求处理，但必须先确认新的 `feature-slug`
- `/prd` 与 `/pd-review` 不得擅自新建需求目录，应返回 `需补充上下文` 或 `阻塞`

## `feature-summary` 使用规则

- `feature-summary` 是需求级文档文件名中的中文摘要名，用于标识大功能下的具体子功能或本次子范围
- `feature-summary` 必须使用中文，保持简短、可搜索，推荐 4-12 个汉字
- `feature-summary` 不进入目录名，不替代 `feature-slug`
- 同一 `feature-slug` 下允许存在多个不同的 `feature-summary`
- 写需求级文档前，必须同时确定：
  - 唯一 `feature-slug`
  - 当前文档对应的 `feature-summary`
- 若用户只给了大功能名但未给子功能名，且当前场景无法从上下文唯一推断，应先提问确认
- 回显当前文档归档信息时，必须同时回显 `feature-slug` 与 `feature-summary`

## 按命令读取上游的规则

- `/ceo-office`
  - 默认读取最新 `project memo`
  - 仅当用户明确点名某个需求方向时，才进入 `feature-slug` 匹配流程
- `/pd-plan`
  - 先读取最新 `project memo`
  - 若命中已有 `feature-slug`，继续读取该目录下已有需求文档
  - 若是新需求，先确认 `feature_name`、`feature-slug` 与本次 `feature-summary`，再产出文档
- `/prd`
  - 必须先确定唯一 `feature-slug`
  - 必须先确定本次 `feature-summary`
  - 再按类型匹配读取该目录下最新 `feature brief`
  - 若 `feature brief` 不存在，或其状态不是 `待写PRD`，则直接 `阻塞`
- `/pd-review`
  - 必须先确定唯一 `feature-slug`
  - 必须先确定本次 `feature-summary`
  - 再按类型匹配读取该目录下最新 `PRD`
  - 再补读该目录下最新 `feature brief` 与最新 `project memo`
  - 若 `PRD` 不存在，则直接 `阻塞`
