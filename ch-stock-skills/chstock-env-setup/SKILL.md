---
name: chstock-env-setup
description: A 股研究技能的环境自检与首次配置。当用户第一次使用业绩预告/季报扫描/季度业绩研判等 A 股技能、脚本报错缺依赖（No module named pandas/tushare/fitz）或缺 TUSHARE_TOKEN、提到"Tushare token""积分不够""没有接口权限""数据库连不上""python 环境不对"、问"我现在能用哪些功能""为什么单季拆解/PE 是空的"，或要保存/更换 Tushare token、切换 SQLite/PostgreSQL 时使用此 skill。脚本只建专用 Python 环境并从清华源装依赖、逐个探测 Tushare 接口权限、试开数据库、测数据源连通并写本地配置；模型负责把结果翻译成用户能做的下一步。不用于取数、分析或投资建议。
version: 1.1.0
---

# A 股研究技能环境自检与配置

## 目标

- **做什么**：在跑任何 A 股取数技能之前，准备好一个装齐依赖的 Python，查清楚这台机器"能跑什么、缺什么"，并把 Tushare token 和数据库选择存进本地配置。
- **不做什么**：不取行情、不分析、不替用户买 Tushare 积分；不把 token 打印进对话。
- **给谁用**：第一次安装 A 股研究技能、或者脚本报环境错误的用户（通常不是开发者）。

## 关键事实（决定怎么跟用户解释）

1. **必须用专用解释器跑技能脚本**。很多 Agent 宿主（如 WorkBuddy）自带一个托管的 `python3` 排在 PATH 最前面，里面没有 pandas、tushare、PyMuPDF。`setup --install-deps` 会在 `~/.ch-skills/venv` 建一个虚拟环境、从**清华源**（`https://pypi.tuna.tsinghua.edu.cn/simple`）装齐依赖，并把它的路径记为 `CH_SKILLS_PYTHON`。之后所有技能脚本、包括本自检的 `check`，都用这个解释器运行：macOS/Linux 是 `~/.ch-skills/venv/bin/python`，Windows 是 `%USERPROFILE%\.ch-skills\venv\Scripts\python.exe`。本脚本只用标准库，第一次 `setup` 用任何 Python 3.9+ 都能跑。
2. **配置文件**在 `~/.ch-skills/config.env`（可用 `CH_SKILLS_CONFIG` 改路径），`KEY=VALUE` 格式、权限仅本人可读，记录 `CH_SKILLS_PYTHON`、`TUSHARE_TOKEN`、数据库设置。真实环境变量永远优先于它。
3. **数据库**：默认用 SQLite（`~/.ch-skills/db/alpha.db`），不需要装任何服务；PostgreSQL 只给本来就有 PG 的用户（`setup --db postgresql --pg-url ...`）。判分台账和 HTML 依赖数据库。没跑过 `setup` 时 `check` 会显示后端是 postgresql 且连不上，这是"还没配置"，跑一次 `setup` 即可。
4. **Tushare 权限按接口算，不按"有没有 token"算**。同一个 token 可能能调 `daily` 却调不了 `income`，检查结果要逐个接口看。
5. **各技能对 Tushare 的依赖不同**：
   - `a-stock-earnings-forecast`：没有 Tushare 也能跑，产出巨潮公告事实（预告区间、上年同期、扣非、变动原因、原文）；Tushare 补的是单季拆解、营收、年化 PE、股价断层、行业。
   - `a-stock-quarterly-report`：Tushare 是数值权威，`disclosure_date / income / fina_indicator / cashflow / balancesheet` 任一不可用就跑不了。
   - `a-stock-earnings-review`：K 线与估值默认走公开接口（新浪、东财），不需要 Tushare；公开接口被网络拦截时才回退 Tushare。
   - `cninfo-announcement-search` / `chstock-interactive-qa`：不需要 Tushare。

## 工作流程

1. **首次配置**（同一台机器只做一次，约 2 分钟）：
   ```bash
   python3 scripts/env_doctor.py setup --install-deps
   ```
   - 输出的 `values.CH_SKILLS_PYTHON` 就是之后要用的解释器，`next` 给出下一条命令。
   - `install_deps.ok=false` 时看 `step` 和 `hint`：`step=venv` 说明当前 Python 不能建虚拟环境（缺 venv 模块），改用 `--python <一个完整安装的 Python 路径>`；pip 报 `externally-managed` 时不要加 `--no-venv`；网络装不上时可用 `--index-url` 换别的镜像。
   - Windows 上没有 `python3` 时用 `python` 或 `py -3`。
2. **自检**：用专用解释器运行 `<CH_SKILLS_PYTHON> scripts/env_doctor.py check`。只关心部分技能时加 `--skills a-stock-earnings-forecast,a-stock-quarterly-report`。
3. **按结果处理**，顺序是：
   - `python.target_source=running_interpreter`（还没有专用解释器）或 `packages.missing` 非空 → 回到第 1 步；只差个别包时也可以直接运行 `packages.pip_install` 给出的命令（已带清华源）。
   - `database.ok=false` → 运行 `setup`（不带参数即写入 SQLite），再重跑 check。
   - `tushare.token_present=false` 且用户想要完整功能 → 告诉用户去 tushare.pro 注册并在"个人主页 → 接口TOKEN"复制 token，拿到后用 `echo <token> | <CH_SKILLS_PYTHON> scripts/env_doctor.py setup --tushare-token-stdin` 保存，不要在回复里复述完整 token。
   - `tushare.token_valid=false` → token 抄错了，请用户重新复制。
   - `network.<target>.ok=false` → 网络或代理拦截，提示用户检查网络；不要用改代码绕过。
4. **向用户汇报**：按 `skills.<name>.level` 说清楚每个技能现在的状态，并把 `tushare_unavailable_features` 翻译成"哪些栏位会是空的"：
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

执行 `python3 scripts/env_doctor.py setup --install-deps`，约两分钟后拿到 `CH_SKILLS_PYTHON=~/.ch-skills/venv/bin/python`；再用它运行 `check`，发现依赖齐全、数据库是 SQLite、没有 token。汇报："环境准备好了，之后我都用 ~/.ch-skills/venv 里的 Python 跑脚本。业绩预告扫描、股价反映和估值分析已经可以用了，不过预告只有巨潮公告里的数字和原因，单季拆解是空的；正式季报扫描需要 Tushare 的财报接口，暂时跑不了。如果要补齐，去 tushare.pro 注册后把 token 发我，我帮你存好再检查一次。"
