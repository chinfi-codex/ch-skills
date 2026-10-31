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

7. 观察下一轮：`~/.hermes/cron/output/bba5ba3f1faf/` 出现新运行记录，`~/AlphaData/proactive/state.json` 的 `last_scan_at` 更新到整点或半点，金库 `主动目标状态.json` 不再变化。投递任务在每小时 :05 和 :35 运行，和扫描错开，避免抢状态锁。

> 2026-10-29 实际切换记录：12:13 迁移，12:14 恢复任务。12:30 两个任务同一秒启动，投递先拿到锁，扫描被跳过，于是把投递改到 `5,35 * * * *`。

## 回滚

金库清理（下一节）已于 2026-10-31 完成，旧代码只保存在备份包里。回滚到金库运行时需要先还原：

1. 暂停两个任务。
2. 在 AlphaVault 根目录还原备份：`tar -xzf ~/AlphaData/proactive/archive/vault-legacy-20261031.tar.gz`。这一步同时会把治理文件覆盖回 10-31 清理前的版本。
3. 把 `~/AlphaData/proactive/state.json` 改写回金库路径。注意 `packet_path` 要改回 `ledgers/runs/proactive/packets/…` 前缀，否则延期候选无法恢复。
4. 删除 `~/.hermes/scripts/alphavault-proactive-{evaluate,delivery}.py`（新 wrapper 不在旧安装器的托管列表里），再在金库内运行 `python3 system/tools/install_proactive_hermes.py --apply --enable-live`。
5. 在金库内运行 `python system/tools/compile_protocol_contracts.py`，然后恢复任务。

切换前的 wrapper 与任务定义另存于 `~/AlphaData/proactive/archive/pre-cutover-20261029/`。

## 观察稳定后清理金库（2026-10-31 已完成）

> 实际执行记录：10-29 12:14 至 10-31 10:22 共 184 次运行全部成功，金库状态哈希始终未变，之后执行清理。备份为 `~/AlphaData/proactive/archive/vault-legacy-20261031.tar.gz`（124 项，含清理前的治理文件）。重新编译后，`compile_protocol_contracts.py --check` 通过（派生产物从 11 个减到 10 个）；`lint_wiki.py --check` 前后都是 51 个错误，错误列表完全相同，均为既有的 Wiki 内容问题。`ledgers/runs/2026-10-27/` 下的两条历史运行台账保留未动。协议清单里的 `proactive_goal` 任务保留，改为指向接入说明，`allowed_write_paths` 为空。

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
