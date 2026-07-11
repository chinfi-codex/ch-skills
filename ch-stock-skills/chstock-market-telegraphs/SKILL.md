---
name: market-telegraph-fullfeed
description: 获取金十数据的全量原始快讯，不做主题筛选、相关性过滤、去重、摘要或截断。当用户要求“金十数据”“市场电报全量”“快讯全量”“返回原文”“不要筛选”“抓全部快讯”或“汇总输出”时使用此 skill，直接返回完整数据或写入 JSON 文件。不用于新闻分析、分类或摘要。
---

# Market Telegraph Fullfeed

抓取金十 `flash-api.jin10.com/get_flash_list?channel=-8200&vip=1` 接口返回的完整数据，并输出全量结果。

## 适用场景

- 用户要“金十数据全量内容”
- 用户明确说“不做筛选”“不要过滤”“保留原文”
- 用户想把金十快讯导出成 JSON
- 用户要复用电报数据做后续分析，但第一步只需要完整原始数据

## 行为要求

- 直接返回接口中的全量原始记录
- 不做主题筛选
- 不做去重
- 不做截断
- 不做摘要改写
- 除非用户明确要求，否则不要替用户提炼重点

## 命令语法

```bash
cd market-telegraph-fullfeed
python scripts/market_telegraph_fullfeed.py
```

写入文件：

```bash
cd market-telegraph-fullfeed
python scripts/market_telegraph_fullfeed.py --output data/market_fullfeed.json
```

限制返回条数，仅用于调试或抽样：

```bash
cd market-telegraph-fullfeed
python scripts/market_telegraph_fullfeed.py --limit 100
```

时效筛选（默认近2小时）：

```bash
cd market-telegraph-fullfeed
python scripts/market_telegraph_fullfeed.py --hours 2
```

近6小时：

```bash
cd market-telegraph-fullfeed
python scripts/market_telegraph_fullfeed.py --hours 6
```

不限制时效（返回全部数据）：

```bash
cd market-telegraph-fullfeed
python scripts/market_telegraph_fullfeed.py --hours 0
```

输出规范化字段而不是原始接口结构：

```bash
cd market-telegraph-fullfeed
python scripts/market_telegraph_fullfeed.py --format normalized
```

## 输出格式

默认输出 `raw` JSON，即金十接口的原始响应对象。

若使用 `--format normalized`：

规范化输出字段为：

- `status`
- `message`
- `items`
- `item_count`

## 参数说明

- `--output`: 输出 JSON 文件路径
- `--limit`: 可选，仅返回前 N 条；默认不限制
- `--timeout`: 请求超时秒数，默认 `20`
- `--format`: `raw` 或 `normalized`，默认 `raw`
- `--hours`: 时效筛选，返回最近 N 小时的数据；默认 `2`（设为 `0` 表示不限制时效）

## 依赖

- Python 3.9+
- `requests`
- `pandas`

## 注意事项

- 该 skill 的目标是“完整抓取与汇总”，不是“分析”
- 如果用户后续要筛选、分类或生成摘要，应在完整结果返回后再执行下一步
