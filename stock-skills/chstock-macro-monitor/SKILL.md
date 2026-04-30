---
name: macro-monitor
description: 当用户需要抓取宏观市场原始数据、汇率、美债收益率、BTC、黄金/原油/天然气、纳指期货、中国 CPI/PPI/社融/PMI，或为地缘、A 股、美股研究准备宏观 evidence 时使用此 skill。脚本只调用 Alpha Vantage、Tushare 与 Stooq 备用源并输出 JSON 数据包；模型负责判断数据含义、写报告和说明风险。不用于宏观预测、交易建议、自动报告生成或替代正式数据终端。
---

# 宏观数据抓取

## 目标

1. **做什么**：从 Alpha Vantage、Tushare、Stooq 获取宏观和市场原始数据，形成 JSON 证据包。
2. **不做什么**：不写宏观报告、不做预测、不生成交易结论、不在脚本中做主观分析。
3. **给谁用**：面向需要在投研、地缘风险或市场日报中引用宏观数据的模型和研究者。

## 适用场景与边界

| 适用 | 不适用 |
|---|---|
| 获取 USD/CNY、美债收益率、BTC、能源价格 | 预测宏观走势 |
| 获取中国 CPI/PPI/社融/PMI 最新值 | 生成完整宏观日报 |
| Alpha Vantage 失败时回退 Stooq | 高频交易或实时行情 |
| 为其他 skill 准备宏观 evidence | 替代 Bloomberg/Wind 等终端 |

缺少环境变量时，脚本应返回 `MISSING_CONFIG` 或空数据；模型要明确说明数据缺口。

## 领域方法论

宏观数据在报告里只应作为证据，不应被单点数据直接推导成结论。

分析时按三层处理：

1. **数据可用性**：先检查 `sources`，确认数据来自 Alpha Vantage、Tushare 还是 Stooq fallback。
2. **指标角色**：汇率看风险偏好和美元流动性，美债看折现率压力，能源看通胀/供给冲击，BTC/纳指看风险资产情绪，中国宏观看内需和政策环境。
3. **结论约束**：单日价格只能说明市场定价信号，不能单独证明宏观趋势；中国宏观月度数据常有滞后，需要写清数据日期。

## 工作流程

1. **识别数据需求**
   - 市场数据：`market` 或 `all`
   - 中国宏观：`china-macro` 或单项 `cpi`/`ppi`/`soci`/`pmi`
   - 备用行情：`backup-market`
   - 产出：需要调用的数据集。

2. **运行脚本取数**
   - 使用 `scripts/macro_monitor.py`
   - 输出 JSON，不输出报告。
   - 产出：宏观 evidence。

3. **模型解释**
   - 先说明来源状态，再解释指标可能含义。
   - 缺失数据要明示，不能补造。
   - 产出：可引用的数据解读。

## 数据获取（脚本抓手）

环境变量：

```bash
ALPHAVANTAGE_API_KEY=your_alphavantage_key
TUSHARE_TOKEN=your_tushare_token
```

依赖：

```bash
pip install requests pandas tushare
```

命令：

```bash
cd crawler/chstock-macro-monitor
python scripts/macro_monitor.py all
python scripts/macro_monitor.py market
python scripts/macro_monitor.py market --use-backup
python scripts/macro_monitor.py china-macro
python scripts/macro_monitor.py cpi
python scripts/macro_monitor.py fx
python scripts/macro_monitor.py us-rates
python scripts/macro_monitor.py energy
```

返回结构：

- `timestamp`：取数时间（`all`）
- `sources`：数据源状态，可能是 `OK`、`MISSING_CONFIG`、`RATE_LIMITED`、`ERROR`
- `data`：指标数据

常见降级：

- `ALPHAVANTAGE_API_KEY` 缺失：市场数据转用 Stooq 可得品种。
- Alpha Vantage 限流：标记 `RATE_LIMITED` 并尝试备用源。
- `TUSHARE_TOKEN` 缺失：中国宏观数据为空并标记 `MISSING_CONFIG`。

## 输出规范

当用户只要数据时，直接返回精简 JSON 摘要即可。

当用户要解读时，使用以下结构：

```markdown
**数据来源状态**
- Alpha Vantage: ...
- Tushare: ...
- Stooq backup: ...

**关键数据**
- USD/CNY: ...
- 10Y UST: ...
- WTI / Natural Gas: ...
- 中国宏观: ...

**解释边界**
[说明哪些只是单日市场定价，哪些是月度滞后数据，哪些数据缺失]
```

不要因为单个指标变化就写确定性宏观结论。

## 示例

### Input

> 帮我抓一下当前宏观风险资产数据，后面我要写伊朗局势报告。

### 执行

```bash
python scripts/macro_monitor.py market
```

### Output 摘要

```markdown
**数据来源状态**
- Alpha Vantage: OK

**关键数据**
- USD/CNY: 7.24
- 10Y UST: 4.25
- BTC: 67245
- WTI: 81.45

**解释边界**
以上是市场定价数据，只能作为风险偏好和能源压力的观察入口，不能单独证明宏观趋势。
```
