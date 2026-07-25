# ClawHub CLI 发布指南

本文件说明如何通过 ClawHub CLI 发布本仓库的 Skill。只在需要发布时阅读。

## 登录与配置

```bash
clawhub login
clawhub whoami          # 查看当前登录状态
```

## 发布 Skill

```bash
clawhub publish <skill路径>
```

## 发布包边界

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

## 发布前检查清单

1. **确认发布包边界**：发布目录中不得包含 `evals/`、`benchmarks/`、`*-workspace/`、`benchmark.*`、`grading.json`、`review.html` 等评测/benchmark 资产。
2. **移除测试产物**：`test_*.py`、`*_test.py`、`.pytest_cache/`、`__pycache__/`
3. **安全审查**：运行 `skill-vetter`（见 AGENTS.md §安全审查）
4. **依赖声明**：`SKILL.md` 中环境变量、Python 包、外部服务全部列出
5. **触发验证**：抽样几个真实用户表达，确认能正确触发，且不会在无关任务中误触发
6. **方法论完整性**：目标、触发、方法论、工作流、输出规范、示例六段齐备
7. **示例可复现**：示例中的命令、参数、输出能实际跑通
