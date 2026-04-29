---
name: chstock-interactive-qa
description: 当用户要查询上市公司互动问答、互动易/P5W 投资者问答、按关键词查公司回复、查看某家公司最近回复、检索公司对某个主题如 AI/CPO/算力/订单/客户的公开回应，或需要导出互动问答 JSON 作为事实证据时使用此 skill。脚本只访问公开问答接口、过滤低信息密度问题并输出结构化 JSON；模型负责判断回复可信度、提炼事实边界和组织摘要。不用于内幕信息判断、替代公告正文、股东户数专项统计或投资建议。
---

# 上市公司互动问答检索

## 目标

1. **做什么**：检索 P5W/互动易公开问答，按公司、关键词和日期过滤，输出结构化问答证据。
2. **不做什么**：不把互动问答当作正式公告，不判断内幕信息，不生成投资建议，不默认保留股东人数类低价值问答。
3. **给谁用**：面向需要快速查证公司公开回应、主题暴露和管理层表述的研究者或模型。

## 适用场景与边界

| 适用 | 不适用 |
|---|---|
| 查某家公司最近互动问答 | 正式公告或财报替代 |
| 按关键词查公司回复 | 股东户数专项统计（除非明确要求） |
| 查公司是否公开回应某主题 | 判断信息真伪或内幕消息 |
| 导出问答 JSON 作为证据 | 投资建议 |

互动问答常带有宣传和选择性披露倾向。输出时必须区分“公司在互动平台这样回复”和“事实已经被公告确认”。

## 领域方法论

互动问答的价值在于补充线索，不在于提供最终结论。

1. **优先限定公司 + 关键词**：单独关键词召回高但噪声多；公司 + 关键词更适合查证主题暴露。
2. **默认过滤股东人数问题**：这类问答数量多、信息密度低，除非用户明确要查股东户数。
3. **按回复时间排序看新鲜度**：旧回复可能已被业务变化推翻。
4. **事实边界要保守**：把回复分为“明确承认”“模糊表述”“否认/无相关”“无法判断”。
5. **需要公告交叉验证**：重大合同、客户、业绩影响等内容，必须提示继续查公告或财报。

## 工作流程

1. **解析查询**
   - 提取公司名/股票代码、关键词、日期区间、limit、是否保留股东户数问答。
   - 产出：脚本参数。

2. **运行检索**
   - 使用 `scripts/interactive_qa_search.py`。
   - 输出 JSON，包含 query、total、items。
   - 产出：问答证据包。

3. **模型归纳**
   - 按主题、公司、回复态度和时间排序。
   - 标注明确事实、模糊回复和需验证事项。
   - 产出：用户可读摘要。

## 数据获取（脚本抓手）

脚本：`scripts/interactive_qa_search.py`

按关键词：

```bash
cd crawler/chstock-interactive-qa
python scripts/interactive_qa_search.py --keyword 算力
```

按公司：

```bash
python scripts/interactive_qa_search.py --company 中际旭创
```

公司 + 关键词：

```bash
python scripts/interactive_qa_search.py --company 中际旭创 --keyword cpo
```

限定日期：

```bash
python scripts/interactive_qa_search.py --company 中际旭创 --date-from 2026-03-01 --date-to 2026-03-28
```

保留股东人数类问题：

```bash
python scripts/interactive_qa_search.py --company 中际旭创 --include-shareholder-count
```

写入 JSON：

```bash
python scripts/interactive_qa_search.py --company 中际旭创 --keyword cpo --output outputs/zjxc_cpo.json
```

参数：

- `--keyword`
- `--company`
- `--date-from` / `--date-to`
- `--limit`
- `--max-pages`
- `--include-shareholder-count`
- `--output`

至少提供 `--keyword` 或 `--company`。

依赖：

```bash
pip install requests
```

## 输出规范

默认输出 500-1000 字摘要，结构如下：

```markdown
**查询条件**
[公司、关键词、日期、是否过滤股东人数]

**结果概览**
- 命中数量：...
- 最新回复时间：...
- 主要主题：...

**关键问答**
| 日期 | 公司 | 问题摘要 | 回复要点 | 判断 |
|---|---|---|---|---|

**事实边界**
[哪些是公司明确回复；哪些只是模糊表达；哪些需要公告/财报交叉验证]
```

判断标签建议使用：`明确回应`、`模糊回应`、`否认/暂无`、`需交叉验证`。

## 示例

### Input

> 查一下中际旭创最近有没有在互动问答里提到 CPO。

### 执行

```bash
python scripts/interactive_qa_search.py --company 中际旭创 --keyword cpo --date-from 2026-03-01 --date-to 2026-03-28
```

### Output 摘要

```markdown
**查询条件**
- 公司：中际旭创
- 关键词：CPO
- 日期：2026-03-01 至 2026-03-28

**结果概览**
共命中 3 条互动问答，最新回复日期为 2026-03-20。

**关键问答**
| 日期 | 公司 | 问题摘要 | 回复要点 | 判断 |
|---|---|---|---|---|
| 2026-03-20 | 中际旭创 | 投资者询问 CPO 进展 | 公司提到相关产品布局但未披露订单金额 | 需交叉验证 |

互动问答只能作为公开回应线索，订单和业绩影响需继续核查公告或财报。
```
