---
name: chstock-usmarket-report
description: 当用户要求生成美股观察池日报、复盘昨晚美股、查看自定义美股池表现、分析观察池分组强弱或指定 YYYY-MM-DD 交易日回看时使用此 skill。脚本只从 Yahoo Finance chart 接口抓取观察池日线并输出 JSON 证据包；模型必须基于证据自行判断大盘状态、分组表现、异动风险和一句话结论。不用于实时盘中交易、盘后新闻归因、个股深度基本面分析或买卖建议。
---

# 美股观察池日报

## 目标

1. **做什么**：基于 `assets/stock_pool.yaml` 中的观察池，获取美股指数和股票池日线数据，生成可追溯的盘后观察报告。
2. **不做什么**：不自动抓新闻、不解释财报原因、不生成目标价、不提供买卖建议；脚本不直接拼完整 Markdown 日报。
3. **给谁用**：面向需要每天快速复盘自选美股池的投资者或研究员，用于发现分组强弱、异常波动和后续研究入口。

## 适用场景与边界

| 适用 | 不适用 |
|---|---|
| “生成美股日报”“复盘昨晚美股” | 实时盘中监控 |
| “看我的美股池表现” | 个股深度基本面研究 |
| 指定日期回看观察池表现 | 新闻、财报、电话会自动归因 |
| 调整 `stock_pool.yaml` 后重跑证据包 | 买入、卖出、仓位建议 |

默认日期取最近一个已结束的美股交易日；用户显式给出 `YYYY-MM-DD` 时按该日或其前一个可用交易日取数。

## 领域方法论

先分清“市场背景”和“观察池内部结构”。指数只回答大盘环境，观察池分组才回答用户真正关心的资产表现。

分析顺序：

1. **大盘环境**：看 QQQ、SPY、DIA、IWM 的当日涨跌和 5 日趋势，判断是成长强、宽基强、还是小盘拖累。
2. **分组表现**：按配置中的 groups 计算组内平均涨跌、上涨/下跌数量和极端标的，判断强弱来自少数个股还是组内扩散。
3. **异动识别**：用阈值桶区分大跌、大涨、预警下跌、强势上涨；异动只是研究线索，不直接归因。
4. **风险与反证**：若某组平均表现强但只有 1 只股票贡献，必须标注集中度；若指数强但观察池弱，要提示相对弱势。
5. **后续研究入口**：对极端波动标的，可以建议“后续核查财报、指引、监管文件或新闻”，但不能在没有数据时直接写原因。

## 工作流程

1. **确认日期和配置**
   - 使用用户指定日期，或默认最近已结束交易日。
   - 检查 `assets/stock_pool.yaml` 的 indices、groups、thresholds。
   - 产出：本次观察池范围。

2. **获取证据包**
   - 运行 `scripts/generate_report.py`。
   - 脚本只输出 JSON：指数快照、分组快照、异动桶、失败 ticker。
   - 产出：可被模型读取和核查的 evidence。

3. **模型分析**
   - 先写指数环境，再写观察池分组表现。
   - 对异常波动只做“待核查线索”，不凭价格变化杜撰原因。
   - 产出：日报正文。

4. **输出报告**
   - 按固定结构写 Markdown。
   - 保留数据日期、样本不足和失败 ticker。
   - 产出：可直接阅读的盘后观察笔记。

## 数据获取（脚本抓手）

脚本：`scripts/generate_report.py`

职责：从 Yahoo Finance chart 接口抓取观察池日线，计算当日涨跌、5 日趋势、分组统计和异动桶。脚本不生成 Markdown 报告，不写结论，不给建议。

默认命令：

```bash
cd crawler/chstock-usmarket-report
python scripts/generate_report.py
```

指定日期：

```bash
python scripts/generate_report.py --date 2026-03-30
```

写入 JSON：

```bash
python scripts/generate_report.py --output outputs/us-market-evidence-2026-03-30.json
```

指定配置：

```bash
python scripts/generate_report.py --config assets/stock_pool.yaml
```

返回结构：

- `type`：固定为 `us_market_watchlist_evidence`
- `date` / `generated_at`
- `thresholds`
- `indices`：指数快照
- `groups`：分组股票快照和组内统计
- `abnormal_moves`：`big_drops`、`big_rises`、`warning_drops`、`highlight_rises`
- `errors`：拉取失败的 ticker 与原因

依赖：

```bash
pip install requests pyyaml
```

## 输出规范

报告使用中立研究笔记风格，默认 600-1200 字。

固定结构：

```markdown
# 美股观察池日报 - YYYY-MM-DD

## 大盘环境
[指数当日涨跌 + 5 日趋势，说明成长/宽基/小盘相对强弱]

## 观察池分组表现
[按分组列出平均涨跌、上涨/下跌数量、代表强弱股票]

## 异动扫描
[大跌/大涨/预警/强势上涨；只写价格证据和待核查方向]

## 结构判断
[说明强弱是否扩散、是否由单一股票贡献、观察池相对指数强弱]

## 后续核查
[只列需要查证的财报、指引、新闻或公告方向]

数据来自 Yahoo Finance chart 接口，仅供研究记录，不构成投资建议。
```

数据呈现：

- 涨跌幅保留 2 位小数。
- 价格保留 2 位小数，美元计价。
- 组内表现优先用表格；解释文字不重复堆数字。
- 有拉取失败 ticker 时必须单列说明。

## 示例

### Input

> 复盘一下昨晚美股，我的观察池哪些组比较强，哪些票异动大？

### 执行

```bash
cd crawler/chstock-usmarket-report
python scripts/generate_report.py
```

### Output 摘要

```markdown
# 美股观察池日报 - 2026-03-30

## 大盘环境
QQQ 当日上涨 1.20%，5 日趋势强于 SPY；IWM 下跌 0.40%，说明小盘风险偏好弱于大盘科技线。

## 观察池分组表现
| 分组 | 平均涨跌 | 上涨/下跌 | 结构判断 |
|---|---:|---:|---|
| AI 基础设施 | +2.10% | 5/1 | 强势扩散 |

## 异动扫描
- 大涨：NVDA +8.30%，需后续核查财报、订单或产品事件。
- 预警下跌：XYZ -5.40%，需核查个股事件或业绩指引。

数据来自 Yahoo Finance chart 接口，仅供研究记录，不构成投资建议。
```
