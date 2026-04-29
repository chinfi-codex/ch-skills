# `product-workflow` Skill Pack

这是仓库里的技能源码目录，面向维护者。
如果你只是想安装并使用这些 skills，请先看仓库根目录的 `README.md`。

## 包含内容

- `ceo-office/`: 顶层商业判断 skill
- `pd-plan/`: 需求澄清与产品方案收敛 skill
- `issue/`: 已有功能的小范围需求更新 skill
- `prd/`: 正式 PRD 编写 skill
- `pd-review/`: PRD 交接审查与补齐 skill
- `shared/fragments/`: 多个 skill 共享的提示词片段
- `scripts/`: 生成 `SKILL.md` 的构建脚本

## 维护规则

- `SKILL.md.tmpl` 是源码
- `SKILL.md` 是生成产物

维护方式：
1. 优先修改各目录下的 `SKILL.md.tmpl`
2. 多个 skill 共用的规则，修改 `shared/fragments/*`
3. 不要直接编辑生成后的 `SKILL.md`
4. 修改后必须重新生成并同步

## 构建

在仓库根目录执行任一方式：

```bash
node --experimental-strip-types skills/product-workflow/scripts/gen-skill-docs.ts
```

```bash
bash skills/product-workflow/scripts/build-all.sh
```

```powershell
powershell -ExecutionPolicy Bypass -File skills/product-workflow/scripts/build-all.ps1
```

## 同步到 Codex

如果你已经把这个仓库安装到了 Codex，修改模板后还需要同步一次，否则 Codex 里的版本不会自动更新。

```bash
bash ./sync.sh
```

或：

```powershell
powershell -ExecutionPolicy Bypass -File .\sync.ps1
```

同步脚本会自动做两件事：
1. 重新生成各个 `SKILL.md`
2. 把最新的 `skills/product-workflow` 复制到你的 Codex skills 目录

## 产物约定

这些 skills 默认把正式产物写入当前项目根目录下的 `./prd/`。

- `project memo` -> `./prd/project-memos/project-memo-YYYY-MM-DD.md`
- `feature brief` -> `./prd/features/<feature-slug>/<feature-summary>-feature-brief-YYYY-MM-DD.md`
- `change request` -> `./prd/features/<feature-slug>/<feature-summary>-change-request-YYYY-MM-DD.md`
- `PRD` -> `./prd/features/<feature-slug>/<feature-summary>-prd-YYYY-MM-DD.md`
- `pd-review-report` -> `./prd/features/<feature-slug>/<feature-summary>-pd-review-report-YYYY-MM-DD.md`

命名分工：
- `feature-slug`：稳定、中文、目录级标识，用于归档同一需求
- `feature-summary`：中文、文件级摘要名，用于表达大功能下的具体子功能或本次子范围

补充规则：
- 同一需求的后续文档必须沿用同一个 `feature-slug`
- 同一 `feature-slug` 下允许有多个不同的 `feature-summary`
- 读取上游文档时，先按类型匹配，再选择最新日期版本，不依赖旧固定文件名
