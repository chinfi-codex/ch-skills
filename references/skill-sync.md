# 跨 Agent 技能同步（Skill Sync Hub）

本文件说明如何以本仓库为主仓，向各 Agent 的 skills 目录单向同步 Skill。只在维护同步机制或排查同步问题时阅读。

## 为什么需要同步

本仓库是技能开发的 **canonical source**（唯一可信源），但各 Agent 读的是各自孤岛里的副本：

| Agent | Skills 路径 | 加载机制 |
|---|---|---|
| **Kimi Code** | `~/.kimi-code/skills` | 独立品牌目录 |
| **Kimi Work** | `~/.kimi-work/skills` | 独立品牌目录 |
| **Claude Code** | `~/.claude/skills` | 仅读自身品牌目录 |
| **Codex** | `~/.codex/skills` | 仅读自身品牌目录 |
| **Hermes** | `~/.hermes/skills` | 仅读自身品牌目录 |
| **OpenClaw** | `~/.openclaw/skills` | 仅读自身品牌目录 |
| **WorkBuddy** | `~/.workbuddy/skills`（用户级）+ `{workspace}/.workbuddy/skills`（项目级） | 用户级跨项目可用；项目级仅当前项目可见，两者合并加载 |

修改本仓库后，各 Agent 安装目录里的副本不会自动更新，导致 Kimi / Claude / Codex 读到的可能是旧版本，甚至完全缺失某些 skill。

参考 [neuDrive](https://github.com/agi-bar/neudrive) 的 Bundle Sync 思路，我们建立 **Skill Sync Hub**：以本仓库为主仓，通过声明式配置 + Git Hook，在 commit 后自动单向同步到所有 Agent 目录。

## 工作原理

```
┌─────────────────────────────────────────┐
│  ch-skills/  (canonical source)         │
│  • ch-stock-skills/*/SKILL.md           │
│  • other-skill/SKILL.md                 │
└──────────────┬──────────────────────────┘
               │  scripts/skill_sync.py
               │  (扁平化扫描 + robocopy /MIR)
               ▼
    ┌──────────────┬──────────────┬──────────────┐
    ▼              ▼              ▼              ▼
 ~/.kimi-code/skills  ~/.kimi-work/skills  ~/.claude/skills ~/.codex/skills
 ~/.hermes/skills  ~/.openclaw/skills  ~/.workbuddy/skills
```

**扁平化规则**：仓库中任何包含 `SKILL.md` 的目录都被视为一个独立 skill，同步时目录名作为目标 skill 名，直接提升到各 Agent `skills/` 根目录下。嵌套结构（如 `stock-skills/a-stock-analyzer/`）会被自动打平为 `a-stock-analyzer/`。

## 快速开始

### 1. 确保依赖

```bash
pip install pyyaml
```

### 2. 安装 Git Hook（只需一次）

```bash
python scripts/skill_sync.py --install-hook
```

此后每次 `git commit` 会自动执行同步。

### 3. 手动同步（可选）

```bash
# 预览变更（不实际执行）
python scripts/skill_sync.py --dry-run

# 立即同步（copy 模式）
python scripts/skill_sync.py

# 建立 Junction 链接（开发推荐，零拷贝、实时生效）
python scripts/skill_sync.py --link

# 监控文件变更并自动同步（不想用 Git Hook 时）
python scripts/skill_sync.py --watch --interval 5
```

## 配置说明（skill-sync.yaml）

```yaml
targets:
  kimi-code: "~/.kimi-code/skills"
  kimi-work: "~/.kimi-work/skills"
  claude: "~/.claude/skills"
  codex: "~/.codex/skills"
  hermes: "~/.hermes/skills"
  openclaw: "~/.openclaw/skills"
  workbuddy: "~/.workbuddy/skills"

rename: {}

exclude:
  dirs: [.git, __pycache__, tests, evals, benchmarks, "*-workspace", node_modules, venv, .venv]
  files: [README.md, benchmark.json, grading.json, review.html, test_*.py, "*_test.py", .env, "*.pyc"]
  skill_root_dirs:
    ch-news-reporter: [data, reports]
    chstock-usmarket-report: [outputs]
```

- **targets**：定义要同步的 Agent 安装目录。使用 `~` 保持跨环境可移植。
- **rename**：当源目录名和目标 skill 名不一致时显式映射（如去除空格）。
- **exclude**：开发/评测产物不会同步到 Agent 目录，保持安装包干净。`skill_root_dirs` 只排除对应 skill 根目录下的运行输出，不会误伤 `references/` 内的同名资料目录。

## 开发工作流建议

**日常迭代**：

```bash
# 1. 修改 SKILL.md 或脚本
# 2. 保存后如需立即在 Agent 中生效（Link 模式）
python scripts/skill_sync.py --link

# 3. 测试验证
# 4. 提交（自动触发同步）
git add .
git commit -m "feat: 优化 a-stock-analyzer 的估值分析框架"
# → post-commit hook 自动执行 skill_sync.py
```

**发布到 ClawHub**：

Sync Hub 只解决"本地多 Agent 同步"。若要发布到 ClawHub 供他人安装，仍需：

```powershell
clawhub publish <skill路径>
```

发布包边界遵守 `references/clawhub.md`。

## 注意事项

1. **单向同步**：Skill Sync Hub 是单向的（仓库 → Agent）。不要在 Agent 安装目录里直接修改 skill，否则下次同步会被覆盖。
2. **Link 模式限制**：Windows Junction 要求源和目标在同一卷；macOS/Linux 使用符号链接。
3. **Codex / Claude 的额外文件**：如果 Agent 目录里原有非本仓库管理的 skill（如 Codex 的 `.system/`、`codex-primary-runtime/`），同步脚本不会触碰它们——只覆盖/新增本仓库扫描到的 skill。
