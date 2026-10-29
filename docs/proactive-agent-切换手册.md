# Proactive Agent 切换手册：从 AlphaVault 运行切到项目运行

日期：2026-10-29 · 适用版本：ch-proactive-agent 0.4.0（项目内运行时）

## 现状

- 项目侧已就绪：`ch-proactive-agent/` 内含协议、配置、契约、运行时、安装器、迁移脚本与 52 项测试，已提交到 ch-skills，并同步到 `~/.hermes/skills/ch-proactive-agent/`。
- 线上仍在跑金库版本：两个 Hermes 任务的 wrapper 仍指向 `1-AlphaVault/system/tools/proactive_goal.py`，状态仍写在金库 `system/frameworks/主动目标状态.json`。
- 已用真实数据做过演练：迁移到临时目录后，新运行时在真实金库与 PG 上 dry-run 扫描无报错，778 条候选通过项目状态契约，退役的协议文件没有被误报为“方法论删除”。

## 切换步骤（约 10 分钟，在交易时段外执行）

以下命令都在 `ch-skills/ch-proactive-agent/` 下运行。

1. 暂停两个任务，避免迁移期间旧运行时继续写金库状态：

   ```bash
   hermes cron pause bba5ba3f1faf
   ```

   ```bash
   hermes cron pause 835e252c3d65
   ```

2. 预览迁移，确认 `packets_missing` 为空、`runtime_exists` 为 false：

   ```bash
   python3 scripts/migrate_from_vault.py
   ```

3. 执行迁移。只复制，不删除金库原件；会持有旧运行时的状态锁，完成后写 `~/AlphaData/proactive/migration.json`：

   ```bash
   python3 scripts/migrate_from_vault.py --apply
   ```

4. 用新运行时看一眼状态，计数应与迁移前一致：

   ```bash
   python3 scripts/proactive_goal.py status
   ```

5. 预览并应用 Hermes 安装。wrapper 改为从 Hermes 技能副本运行，任务 prompt 换成短版，workdir 改为运行态目录：

   ```bash
   python3 scripts/install_hermes.py --enable-live
   ```

   ```bash
   python3 scripts/install_hermes.py --apply --enable-live
   ```

6. 恢复任务：

   ```bash
   hermes cron resume bba5ba3f1faf
   ```

   ```bash
   hermes cron resume 835e252c3d65
   ```

7. 观察下一轮：`~/.hermes/cron/output/bba5ba3f1faf/` 出现新运行记录，`~/AlphaData/proactive/state.json` 的 `last_scan_at` 更新，金库 `主动目标状态.json` 不再变化。

## 回滚

切换后 24 小时内，金库原件都还在。回滚只需把 wrapper 换回旧版本：

1. 暂停两个任务。
2. 在 AlphaVault 内运行旧安装器：`python3 system/tools/install_proactive_hermes.py --apply --enable-live`。新 wrapper 不在旧安装器的托管列表里，需要先手动删除 `~/.hermes/scripts/alphavault-proactive-{evaluate,delivery}.py`。
3. 恢复任务。切换期间新运行时产生的决策只在 `~/AlphaData/proactive/` 里，不会回写金库。

## 观察稳定后（建议 2 个交易日）清理金库

这一步会删除金库里未入 git 的文件，删除前先做一次备份：

1. 把以下内容打包备份到 `~/AlphaData/proactive/archive/vault-legacy-YYYYMMDD.tar.gz`：
   - `system/frameworks/27-主动目标协议.md`、`主动目标状态.json`、`主动目标状态.json.lock`
   - `system/frameworks/契约/主动目标状态契约.schema.json`
   - `system/tools/proactive_goal.py`、`install_proactive_hermes.py` 和两份测试
   - `ledgers/runs/proactive/`
2. 删除上述文件。
3. 写入接入说明：把 `docs/vault-stub/外部Agent接入.md` 放到 `system/frameworks/外部Agent接入.md`。
4. 修改治理入口：
   - `AGENTS.md` 和 `00-治理总则.md` 中的主动目标条目，改成一行指向接入说明；
   - `能力注册表.md/.json` 删除 `proactive_goal.py` 和 `install_proactive_hermes.py`，保留 `long_task_skill_outputs`；
   - `协议清单.md/.json` 与 `契约清单.md` 删除第 27 号协议和状态契约。
5. 在 AlphaVault 内运行 `python system/tools/compile_protocol_contracts.py`，再跑 `--check` 与 `python system/tools/lint_wiki.py --check`。
6. 删除 `~/.hermes/proactive-locks/`。它只被旧运行时使用。
