# 共享 PostgreSQL 连接

本文件说明本仓库 Skill 如何连接共享 PostgreSQL 数据库。只在 Skill 需要读写 PostgreSQL 时阅读。

## 标准连接

所有需要数据库的 skill 默认复用仓库级入口 `shared/data/db_core.py`，不要在单个 skill 中重新写 `psycopg2.connect`、连接池或私有 DSN 解析。

标准连接变量：

```bash
export ALPHA_DB_BACKEND=postgresql
export ALPHA_PG_URL="postgresql://alpha_user:alpha_pass@/alpha_data?host=/tmp"
```

如果当前 Agent 环境没有 Unix socket，改用：

```bash
export ALPHA_PG_URL="postgresql://alpha_user:alpha_pass@localhost:5432/alpha_data"
```

连接诊断优先跑：

```bash
python3 shared/data/db_ping.py --alpha-schema
```

完整契约见 `shared/data/POSTGRESQL.md`。新增需要数据库的 skill 时，脚本必须通过 `db_core` 连接，并在 `skill-sync.yaml` 的 `shared.bundles` 中把它加入 data bundle 的 `skills`，保证各 Agent 同步后仍能快速连接。

## 同步后的路径

同步后的 skill 包中，`shared/data/` 会按 `skill-sync.yaml` 打包到 `scripts/_shared/`（dest 扁平，`db_core.py` 仍在 `_shared/` 根）。安装目录里排查连接问题时运行：

```bash
python3 scripts/_shared/db_ping.py --alpha-schema
```
