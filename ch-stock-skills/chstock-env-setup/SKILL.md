---
name: chstock-env-setup
description: A 股研究技能的环境自检与首次配置。当用户第一次使用业绩预告/季报扫描等 A 股技能、脚本报错缺依赖或缺 TUSHARE_TOKEN、提到"Tushare token""积分不够""没有接口权限""数据库连不上""没装 PostgreSQL"、问"我现在能用哪些功能""为什么单季拆解/PE 是空的"，或要保存/更换 Tushare token、切换 SQLite/PostgreSQL 时使用此 skill。脚本只检测依赖、逐个探测 Tushare 接口权限、试开数据库、测巨潮/全景网连通并写本地配置；模型负责把结果翻译成用户能做的下一步。不用于取数、分析或投资建议。
version: 1.0.0
---

# A 股研究技能环境自检与配置

## 目标

- **做什么**：在跑任何 A 股取数技能之前，查清楚这台机器"能跑什么、缺什么"，并把 Tushare token 和数据库选择存进本地配置，让后续脚本不用每次设环境变量。
- **不做什么**：不取行情、不分析、不替用户买 Tushare 积分；不把 token 打印进对话。
- **给谁用**：第一次安装 A 股研究技能、或者脚本报环境错误的用户（通常不是开发者）。

## 关键事实（决定怎么跟用户解释）

1. **配置文件**在 `~/.ch-skills/config.env`（可用 `CH_SKILLS_CONFIG` 改路径），`KEY=VALUE` 格式、权限仅本人可读。真实环境变量永远优先于它；`db_core` 和各技能的 Tushare 客户端都会读它。
2. **数据库**：独立安装默认用 SQLite（`~/.ch-skills/db/alpha.db`），不需要装任何服务。PostgreSQL 只给已有 PG 的用户。判分台账和 HTML 都依赖数据库，数据库打不开时技能能出 JSON 但没法落判分。
3. **Tushare 权限按接口算，不按"有没有 token"算**。同一个 token 可能能调 `daily` 却调不了 `income`。所以检查结果要逐个接口看，别笼统说"token 可用"。
4. **各技能对 Tushare 的依赖不同**：
   - `a-stock-earnings-forecast`：没有 Tushare 也能跑，产出巨潮公告事实（预告区间、上年同期、扣非、变动原因、原文）；Tushare 补的是单季拆解、营收、年化 PE、股价断层、行业。
   - `a-stock-quarterly-report`：Tushare 是数值权威，`disclosure_date / income / fina_indicator / cashflow / balancesheet` 任一不可用就跑不了。
   - `a-stock-earnings-review`：K 线与估值默认走公开接口（新浪、东财），不需要 Tushare；公开接口被网络拦截时才回退 Tushare。
   - `chstock-cninfo-announcement` / `chstock-interactive-qa`：不需要 Tushare。

## 工作流程

1. **先自检**：`python3 scripts/env_doctor.py check`（Windows 上 `python3` 不存在就用 `python`）。只关心部分技能时加 `--skills a-stock-earnings-forecast,a-stock-quarterly-report`。
2. **按结果处理**，顺序是：
   - `packages.missing` 非空 → 直接运行 `packages.pip_install` 给出的命令，再重跑 check。
   - `database.ok=false` → 运行 `python3 scripts/env_doctor.py setup`（默认写入 SQLite），再重跑 check。
   - `tushare.token_present=false` 且用户想要完整功能 → 告诉用户去 tushare.pro 注册并在"个人主页 → 接口TOKEN"复制 token，拿到后用 `echo <token> | python3 scripts/env_doctor.py setup --tushare-token-stdin` 保存（不要把 token 写进命令参数以外的地方，也不要在回复里复述完整 token）。
   - `tushare.token_valid=false` → token 抄错了，请用户重新复制。
   - `network.<target>.ok=false` → 网络或代理拦截，提示用户检查网络；这类问题不要用改代码绕过。
3. **向用户汇报**：按 `skills.<name>.level` 说清楚每个技能现在的状态，并把 `tushare_unavailable_features` 翻译成"哪些栏位会是空的"：
   - `full`：功能完整。
   - `cninfo_only`：只有巨潮公告事实，说清缺哪些栏位、补 token 后重跑即可补齐（PDF 缓存复用，不会重下）。
   - `partial`：token 有效但部分接口无权限，列出缺的功能；是否升级积分由用户决定，不要替用户承诺具体积分档位。
   - `blocked`：列出 `blockers` 和对应的解决步骤。

## 输出规范

- 讲人话：先一句话说"现在能用什么"，再分条说缺什么、怎么补。别把 JSON 原样贴给用户。
- 同类问题用 list，每条是完整的一句话（"季报扫描暂时跑不了：Tushare 的利润表接口没有权限"），不要写成"income - no_permission"。
- token 只显示脚本给的掩码（如 `abcd…wxyz`）。
- 不推荐、不评价任何付费方案；Tushare 积分规则以官网为准。

## 示例

用户：`第一次用，帮我看看环境`

执行 `python3 scripts/env_doctor.py check`，发现数据库后端是默认的 PostgreSQL 且连不上、没有 token。先运行 `python3 scripts/env_doctor.py setup` 切到 SQLite，重跑 check 后汇报："业绩预告扫描已经可以用了，不过目前只有巨潮公告里的数字和原因，单季拆解、估值和股价断层是空的；季报扫描需要 Tushare 的财报接口，暂时跑不了。如果要补齐，去 tushare.pro 注册后把 token 发我，我帮你存好再检查一次。"
