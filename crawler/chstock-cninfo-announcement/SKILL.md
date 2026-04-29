---
name: cninfo-announcement-search
description: 查询巨潮资讯网公告，支持按 tabtype、日期区间、stock、searchkey、category、trade 等参数抓取公告，并对 fulltext 公告按内置 cninfo rules 做分类和过滤。用户提到“巨潮公告”“cninfo 公告”“查公告”“按日期查某只股票公告”“按规则过滤公告”“问询回复/减持/增持/监管函公告筛选”等场景时，应优先使用此 skill。
trigger_patterns:
  - "查询巨潮公告"
  - "按日期查 cninfo 公告"
  - "查 {stock} 的公告"
  - "按规则过滤巨潮公告"
  - "查问询回复/减持/增持/监管函公告"
---

# CNInfo Announcement Search

用于查询巨潮资讯网历史公告，并按规则做分类与过滤。

## 适用场景

- 查询某只股票在指定日期区间内的公告
- 指定 `tabtype=fulltext` 或 `tabtype=relation` 抓取公告
- 通过 `searchkey`、`category`、`trade` 缩小范围
- 对 `fulltext` 公告按标题规则分类
- 默认排除异常波动问询回复、减持进展等低价值噪声公告

## 命令语法

```bash
cd cninfo-announcement-search
python scripts/cninfo_announcement_search.py --tabtype fulltext --date 2026-03-01~2026-03-28 --stock 300017
```

按标题关键词搜索：

```bash
cd cninfo-announcement-search
python scripts/cninfo_announcement_search.py --tabtype fulltext --date 2026-03-01~2026-03-28 --searchkey 回购
```

指定 `code,orgId`：

```bash
cd cninfo-announcement-search
python scripts/cninfo_announcement_search.py --tabtype fulltext --date 2026-03-01~2026-03-28 --stock 300017,9900008387
```

保留被规则排除的公告：

```bash
cd cninfo-announcement-search
python scripts/cninfo_announcement_search.py --tabtype fulltext --date 2026-03-01~2026-03-28 --stock 300017 --include-excluded
```

写入 JSON 文件：

```bash
cd cninfo-announcement-search
python scripts/cninfo_announcement_search.py --tabtype fulltext --date 2026-03-01~2026-03-28 --stock 300017 --output data/cninfo_300017.json
```

## 参数说明

- `--tabtype`: `fulltext` 或 `relation`
- `--date`: 日期范围，格式 `YYYY-MM-DD~YYYY-MM-DD`
- `--stock`: 股票代码，支持 `300017` 或 `300017,9900008387`
- `--searchkey`: 标题关键词
- `--category`: 巨潮原始分类参数，例如 `category_ndbg_szsh`
- `--trade`: 行业参数
- `--page-num`: 页码，默认 `1`
- `--page-size`: 每页条数，默认 `30`
- `--include-excluded`: 保留 rules 标记为 `excluded=true` 的公告
- `--disable-orgid-resolve`: 当 `stock` 仅给代码时，不自动补全 `orgId`
- `--output`: 输出 JSON 文件路径

## 输出格式

输出为 JSON，对每条公告补充以下字段：

- `announcement_time`
- `sec_name`
- `sec_code`
- `title`
- `adjunct_url`
- `announcement_id`
- `category`
- `subcategory`
- `rule_id`
- `excluded`
- `exclude_reason`
- `tags`

顶层结果包含：

- `query`
- `total_pages`
- `total_records`
- `filtered_count`
- `announcements`

## 规则过滤说明

当 `tabtype=fulltext` 时，会按标题规则分类：

- 问询回复
- 监管函
- 员工持股计划
- 特定对象发行
- 股权激励
- 增持
- 减持
- 重大合作/投资项目
- 业绩快报

默认排除：

- 异常波动类问询回复
- 减持进展、减持完成、时间过半等进度类公告

当 `tabtype=relation` 时，仅做基础标记，不做同样的标题规则过滤。

## 依赖

- Python 3.9+
- `requests`

## 环境变量说明

本 skill 不强依赖额外环境变量。

- 发布到 ClawHub 时需要 `CLAWHUB_TOKEN`
- 若后续扩展联网搜索，可按仓库规范使用环境变量保存密钥

## 注意事项

- `stock` 传入纯股票代码时，脚本默认会先查询 `orgId`，再拼成 `code,orgId`
- 规则过滤只基于公告标题，不读取 PDF 正文
- 如果需要更细的行业或公告类型筛选，优先通过 `category`、`trade`、`searchkey` 先收窄结果
