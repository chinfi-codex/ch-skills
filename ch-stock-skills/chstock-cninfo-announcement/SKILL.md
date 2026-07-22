---
name: cninfo-announcement-search
description: 当用户要查询巨潮资讯网公告、按日期查某只 A 股公告、用 cninfo/fulltext/relation 参数抓公告、筛选问询回复/监管函/增持/减持/回购/股权激励/业绩快报/重大合作公告，或需要把公告结果导出 JSON 作为后续分析证据时使用此 skill。脚本只抓取公开公告元数据并按标题规则打标签或过滤，模型负责判断公告重要性、组织摘要和说明不确定性。不用于读取 PDF 正文、法律意见判断、公告真实性背书或投资建议。
---

# CNInfo Announcement Search

## 目标

1. **做什么**：查询巨潮资讯网公告列表，按标题规则补充分类标签，输出 JSON 证据。
2. **不做什么**：不读取 PDF 正文、不生成完整公告分析报告、不对公告做法律判断、不替用户下投资结论。
3. **给谁用**：面向需要快速定位 A 股公告、批量筛选事件类型、为后续研究准备公告证据的模型和研究者。

## 适用场景与边界

| 适用 | 不适用 |
|---|---|
| 查某股票某日期区间公告 | 逐页阅读 PDF 正文 |
| 按关键词或公告类别筛选 | 判断公告法律效力 |
| 标记问询回复、监管函、增减持、回购等类型 | 生成投资建议 |
| 导出公告 JSON 供后续分析 | 替代交易所/公司正式披露核验 |

规则过滤只基于公告标题，不能替代正文审阅。若用户需要公告正文，先返回 `adjunct_url`，再由其他 PDF/网页读取流程处理。

## 领域方法论

公告检索的核心是“先提高召回，再控制噪声”。

1. **查询收窄优先**：股票代码、日期、关键词、category、trade 能越早限定越好，避免结果过宽。
2. **标题规则只做初筛**：标题可以识别事件类型，但不能判断事件影响大小。
3. **默认降噪**：异常波动问询回复、减持进展/时间过半等低信息密度公告默认排除；用户明确要求时保留。
4. **重要性由模型二次判断**：重大合作、监管函、业绩快报等只是标签，模型要结合公司、日期、公告类型和后续任务判断优先级。
5. **保留可追溯字段**：输出必须保留公告时间、标题、股票代码、公告链接和 rule_id。

## 工作流程

1. **解析查询意图**
   - 识别股票、日期范围、关键词、公告类型和是否保留默认排除项。
   - 产出：脚本参数。

2. **运行公告抓取**
   - 使用 `scripts/cninfo_announcement_search.py`。
   - 输出 JSON，包含 query、统计、announcements。
   - 产出：公告证据包。

3. **模型筛选与解释**
   - 对结果按 `category/subcategory/tags/excluded` 分组。
   - 明确哪些公告只是标题命中，哪些需要进一步读正文。
   - 产出：用户可读摘要或下一步研究清单。

## 数据获取（脚本抓手）

脚本：`scripts/cninfo_announcement_search.py`

基础命令：

```bash
cd crawler/chstock-cninfo-announcement
python scripts/cninfo_announcement_search.py --tabtype fulltext --date 2026-03-01~2026-03-28 --stock 300017
```

按标题关键词搜索：

```bash
python scripts/cninfo_announcement_search.py --tabtype fulltext --date 2026-03-01~2026-03-28 --searchkey 回购
```

保留默认排除项：

```bash
python scripts/cninfo_announcement_search.py --tabtype fulltext --date 2026-03-01~2026-03-28 --stock 300017 --include-excluded
```

写入 JSON：

```bash
python scripts/cninfo_announcement_search.py --tabtype fulltext --date 2026-03-01~2026-03-28 --stock 300017 --output outputs/cninfo_300017.json
```

参数：

- `--tabtype`: `fulltext` 或 `relation`
- `--date`: `YYYY-MM-DD~YYYY-MM-DD`。**注意**：巨潮按公告**归档日**过滤，晚间披露的公告归档在次日；脚本默认自动把结束日 +1 天以免漏掉截止日晚间的公告（输出 `query.se_date_effective` 记录生效窗口），可能因此带入次日白天归档的公告，按 `announcement_time` 甄别即可
- `--stock`: `300017` 或 `300017,9900008387`
- `--searchkey`: 标题关键词
- `--category`: 巨潮原始分类参数
- `--trade`: 行业参数
- `--page-num` / `--page-size`
- `--include-excluded`
- `--no-archive-extend`: 关闭结束日 +1 天的自动顺延，严格按传入窗口查询
- `--disable-orgid-resolve`
- `--output`

依赖：

```bash
pip install requests
```

示例输出见 `examples/sample_output.json`。

## 输出规范

当用户要“查结果”，输出精简列表：

```markdown
**查询条件**
[stock/date/searchkey/category]

**结果概览**
- 返回公告数：...
- 过滤后公告数：...
- 主要类型：...

**重点公告**
| 时间 | 股票 | 标题 | 类型 | 链接 |
|---|---|---|---|---|

**过滤/噪声说明**
[默认排除了哪些类别；如 include-excluded 则说明保留]
```

当用户要“后续分析”，只把公告列表作为证据，不要凭标题直接写影响结论。需要正文时明确提示继续读取公告 PDF。

## 示例

### Input

> 查一下 300017 三月以来有没有回购、减持、监管函这类公告。

### 执行

```bash
python scripts/cninfo_announcement_search.py --tabtype fulltext --date 2026-03-01~2026-03-31 --stock 300017 --searchkey 回购
```

如需覆盖多类关键词，分多次查询或放宽 `searchkey` 后由规则标签过滤。

### Output 摘要

```markdown
**查询条件**
- 股票：300017
- 日期：2026-03-01 至 2026-03-31

**结果概览**
共返回 8 条公告，规则识别出 1 条回购相关、1 条监管类公告。

**重点公告**
| 时间 | 股票 | 标题 | 类型 | 链接 |
|---|---|---|---|---|
| 2026-03-12 | 300017 | 关于回购股份进展的公告 | 回购 | ... |

标题规则只能说明公告类型，具体影响需继续读取公告正文。
```
