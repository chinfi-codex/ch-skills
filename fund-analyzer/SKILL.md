---
name: fund-analyzer
version: 1.0.2
description: "Public fund analyzer for buy-point analysis, industry comparison, and fund screening in AI coding tools with configured models or OpenClaw."
trigger_patterns:
  - "基金 {fund_name} 买点"
  - "分析 {fund_name} 买点"
  - "{fund_name} 现在是买点吗"
  - "基金 {industry_name} 对比"
  - "{industry_name} 行业基金对比"
  - "基金推荐"
  - "推荐优质基金"
  - "筛选优质基金"
---

# 公募基金分析助手

## 适用场景

这个 skill 适合处理以下三类请求：

1. 单只基金买点分析
2. 某个行业主题下的基金横向对比
3. 按固定规则筛选一批基础面和收益表现较好的基金

它的定位是“基于 Tushare 数据的规则化分析工具”，不是泛化的理财顾问。

## 当前能力

### 1. 指定基金买点分析

- 命令：`python scripts/fund_analyzer.py analyze <基金名称或代码>`
- 用途：分析单只基金当前是否接近规则定义下的买点区间
- 分析输入：
  - 基金基本信息
  - 日频净值
  - 上证指数日线
  - 基金经理和管理人信息
- 输出特点：
  - 结构化买点报告
  - 显示关键条件是否满足
  - 给出等待、跟踪、试探性建仓等判断
  - 若配置了模型接口，会附加 AI 解释段落

### 2. 行业基金横向对比

- 命令：`python scripts/fund_analyzer.py compare-industry <行业词>`
- 别名命令：`python scripts/fund_analyzer.py industry-compare <行业词>`
- 当前内置支持的行业别名：
  - 医药生物：`医药`、`医疗`、`医疗健康`、`创新药`、`生物医药`
  - 电子：`电子`、`半导体`、`芯片`、`消费电子`
  - 计算机：`计算机`、`软件`、`信创`、`云计算`、`人工智能`、`AI`
  - 新能源：`新能源`、`光伏`、`锂电`、`储能`、`新能车`
  - 消费：`消费`、`食品饮料`、`白酒`
  - 军工：`军工`、`国防军工`、`航空航天`
- 用途：在某个行业主题下筛出 3 只可比基金并生成固定格式的对比报告
- 输出特点：
  - 固定输出 3 只基金
  - 固定按 4 个维度对比：超额水平、净值表现、回撤控制、基金经理
  - 输出综合排序，但不输出买卖建议
  - 会对“买入、卖出、加仓、减仓、高位、低位、择时”等词做清洗，保持客观比较口径

### 3. 优质基金筛选

- 命令：`python scripts/fund_analyzer.py recommend`
- 用途：在全市场基金列表中按硬规则筛出一批基础面和收益表现较好的基金
- 当前脚本中的主要筛选逻辑：
  - 基金规模约束：`2 亿 - 50 亿`
  - 有基金经理信息
  - 近 1 年和近 3 年年化收益均大于 0
  - 最终按 1 年和 3 年收益均值排序，输出前 20 只
- 输出特点：
  - 偏“快速初筛清单”
  - 输出列包括基金名称、经理、任期、规模、近 1 年收益、近 3 年收益
  - 不会深入展开行业归因、风格暴露和风险控制细节

## 命令语法

```bash
cd fund-analyzer

# 单只基金买点分析
python scripts/fund_analyzer.py analyze <基金名称或代码>

# 行业基金对比
python scripts/fund_analyzer.py compare-industry <行业词>
python scripts/fund_analyzer.py industry-compare <行业词>

# 优质基金筛选
python scripts/fund_analyzer.py recommend
```

示例：

```bash
python scripts/fund_analyzer.py analyze 中欧医疗健康混合A
python scripts/fund_analyzer.py analyze 260101
python scripts/fund_analyzer.py compare-industry 医药
python scripts/fund_analyzer.py compare-industry 半导体
python scripts/fund_analyzer.py recommend
```

## 买点分析逻辑

### 数据构建

`analyze` 模式当前会拉取并处理以下数据：

- 基金基本信息：`fund_basic`
- 基金净值：`fund_nav`
- 基金经理：`fund_manager`
- 基金规模：`fund_share + fund_nav` 或简化 AUM 估算
- 上证指数：`index_daily`

### 当前脚本实际使用的买点规则

当前实现更接近“左侧买点规则集”，并不是完整实现需求文档中的双模式系统。

实际会检查的关键条件包括：

1. 时间条件：距近期高点回撤时间是否不少于 15 个交易日
2. 空间条件：当前净值相对前高是否落在 `0.65 - 0.75`
3. 均线支撑：当前净值是否仍高于 60 日或 200 日均线
4. 市场量能：上证成交量是否萎缩到峰值的 70% 以下
5. 市场形态：上证最近是否出现长下影线、十字星或小阳线

报告会按通过条件数给出三类结论：

- 条件大多满足：买点信号具备
- 通过条件中等：买点信号待确认
- 通过条件较少：买点信号不完全具备

### 与需求文档的关系

需求文档里定义了更完整的“双模式”框架：

- 左侧折扣买点
- 右侧突破回踩买点

当前代码尚未完整落地右侧突破回踩逻辑、双模式冲突处理和严格的阶段化仓位控制。因此调用这个 skill 时，应按“当前实现优先”理解：

- 可以参考需求文档的表达风格
- 但不能把未实现的规则伪装成已经执行过的计算结果

## 行业对比规则

行业对比功能已经较完整地落地了需求文档中的主体思路，核心约束如下：

### 标的筛选

- 仅保留股票型、偏股混合型、混合型、灵活配置型基金
- 排除指数类产品
- 成立时间至少约 1.5 年
- 规模区间：`2 亿 - 100 亿`
- 现任经理任职至少 1 年
- 先按行业词做文本匹配，再去重不同份额类别
- 最终按 Sharpe 排序后取前 3 只做比较

### 四个对比维度

1. 超额水平
   - 行业超额 alpha
   - 大盘超额
   - 超额稳定性
   - 跑赢行业胜率
2. 净值表现
   - 年化收益
   - 夏普比率
   - 卡玛比率
3. 回撤控制
   - 最大回撤
   - 回撤修复天数
   - 下跌保护
4. 基金经理
   - 任职年限
   - 行业专注度
   - 历史业绩分位
   - 当前管理总规模

### 输出边界

行业对比报告必须遵守这些边界：

- 只做客观对比，不给操作建议
- 不写买入、卖出、加仓、减仓等动作词
- 不做“高位/低位/择时”判断
- 固定输出 3 只基金；不足时直接返回“该行业可选基金不足”

## 输出格式要求

### 买点分析

建议输出为研究笔记风格，包含以下信息：

- 基本信息
- 净值数据
- 市场环境
- 买点综合判断
- 操作建议
- 风险提示

若已配置模型，可在规则结果后附加一段 AI 分析，但前提是：

- 规则结论优先
- AI 只做解释，不覆盖规则判断
- 不制造虚假确定性

### 行业对比

建议严格保持以下结构：

1. 对比标的
2. 超额水平
3. 净值表现
4. 回撤控制
5. 基金经理
6. 综合排序
7. 数据截止与免责声明

## 环境变量

### 必需

- `TUSHARE_TOKEN`
  - 用途：所有基金和指数数据拉取的基础依赖

### 运行前提

- 本 skill 默认面向“已配置模型”的 AI 编程工具使用
- 在 OpenClaw 环境中可直接调用或转交模型能力
- 如果运行环境没有可用模型，AI 分析段只能退化为占位提示，不属于推荐使用方式

## 数据源

- 公募基金列表：<https://tushare.pro/document/2?doc_id=19>
- 基金净值：<https://tushare.pro/document/2?doc_id=119>
- 基金经理：<https://tushare.pro/document/2?doc_id=208>
- 基金份额 / 规模估算：Tushare `fund_share`
- 上证指数：Tushare `index_daily`
- 申万行业指数：Tushare `sw_daily`
- 行业成分：Tushare `index_member`

## 使用建议

- 查询单只基金时，优先输入完整基金名或 6 位代码
- 做行业对比时，尽量使用脚本已支持的行业别名
- `recommend` 结果只适合当作候选池，不应直接替代深入研究

## 文件结构

```text
fund-analyzer/
├── SKILL.md
└── scripts/
    ├── ai_client.py
    ├── analyze_cloud.py
    ├── buy_point_analyzer.py
    ├── fund_analyzer.py
    ├── fund_data_fetcher.py
    └── industry_compare_analyzer.py
```

## 免责声明

本 skill 基于公开历史数据和规则化计算生成结果，仅供研究与比较参考，不构成任何投资建议。基金投资有风险，市场风格切换、流动性变化和情绪波动都可能导致历史规律失效，使用前请结合自己的风险承受能力独立判断。
