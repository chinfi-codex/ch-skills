# Proactive Agent v0.5 发布记录

日期：2026-10-31 · 覆盖改进方案中的全部工程项（P1 正确性、P2 结构、迁移遗留）。

## 改了什么

| 工程项 | 做法 | 代码位置 |
|---|---|---|
| 同主题重复提醒 | 指纹剔除逾期天数、规则摘要、固定说明；`topic_key` 由代码确定，按 `cooldown_hours` 冷却；评估包注入 `recent_notifications` | `sources/__init__.py`、`outbox.py`、`evaluate.py` |
| 整批作废 | 逐项校验与确定性修正，修正写进审计 `repairs`；整份无法解析时给在途批次计失败 | `validate.py`、`runs.py` |
| 投递回执 | 读 Hermes `executions.delivery_outcome`，`attempted` → `delivered` / `failed` / `not_sent` / `unverified` | `outbox.reconcile_receipts` |
| 候选排序反了 | rank 小的源在前，同源内新的在前 | `sources.collect` |
| 解析失败无告警 | `state.health`：解析失败、轮转风险、PG 水位失败、回执失败、评估超时、触发失败、限流 | `store.add_health`、`runs.py` |
| 状态膨胀 | 终态候选进 `index.json`，过期意图与运行记录归档；live 数据上从 400 KB 降到 44 KB | `store.compact` |
| 一个 tick 多次采集 | `tick_cache`：一次进程内趋势子进程与事件表各采一次 | `sources.tick_cache` |
| Prompt 过长 | 规则只注入规则陈述、适用环境、执行细节、例外，每条 ≤ 1200 字；框架摘录缩短；术语表按需注入 | `sources/trend.py`、`board.py`、`render.glossary_for` |
| 单文件 1090 行 | 拆成 `proactive/` 包：context、lock、store、sources/、evaluate、validate、render、outbox、runs、pulse | `scripts/proactive/` |
| 事件唤醒 | pulse 每 2 分钟比较文件和 PG 水位、读信号收件箱；防抖、限流后扫描，有候选再 `hermes cron run` 评估任务；gate 只交出备好的批次 | `pulse.py`、`proactive_goal.py pulse/gate/signal` |
| 判断与排版分离 | 模型填 `brief`，代码渲染标签、事件日与版式，替换节点代号、删免责套话 | `render.py`、SKILL.md v0.5 |
| 迁移遗留 | 删除 `migrate_from_vault.py` 和金库旧 wrapper 模板；安装器只识别 0.4 版 wrapper 作为可升级版本 | `install_hermes.py` |

Hermes 任务变化：

- 评估任务：脚本由 `scan` 改为 `gate`，定时由 `*/30` 改为 `17 * * * *`（兜底）；
- 提醒任务：脚本由 `dispatch` 改为 `pulse`，定时由 `5,35` 改为 `*/2 * * * *`；
- 两个任务的 ID、名称、通道不变。

## 未完成

- **解释器（工程项 13）**：仍是系统自带的 Python 3.9。uv 已有 Python 3.12，但本机缓存里没有 psycopg2-binary、PyYAML、jsonschema，需要从 PyPI 下载。确认后执行：
  - `uv venv --python 3.12 ~/AlphaData/proactive/venv`
  - `uv pip install --python ~/AlphaData/proactive/venv/bin/python psycopg2-binary pyyaml jsonschema`
  - `ALPHAVAULT_PYTHON=~/AlphaData/proactive/venv/bin/python python3 scripts/install_hermes.py --apply --enable-live`
- **长任务 Skill 收尾信号**：pulse 已支持 `signal --source skill:<名称>`，但各 Skill 还没在收尾处调用。现在靠水位兜底，延迟约 2–5 分钟。
- **launchd 秒级监听**：没有装。水位轮询已经覆盖文件和 PG 变化，秒级需求出现再加。

## 回滚

1. 暂停两个任务。
2. 还原运行态：`tar -xzf ~/AlphaData/proactive/archive/pre-v0.5-20261031.tar.gz -C ~/AlphaData/proactive`。v0.4 不认识 v3 状态，必须还原。
3. 在 ch-skills 检出 v0.5 之前的提交并重新同步，再在项目目录运行 `python3 scripts/install_hermes.py --apply --enable-live`。新 wrapper 不在 0.4 安装器的托管列表里，需要先删掉 `~/.hermes/scripts/alphavault-proactive-{evaluate,delivery}.py`。
4. 恢复任务。
