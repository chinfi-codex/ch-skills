# 四层价格 + 一层供给：口径定义与可比性规则

这份文件回答一件事：**手里这个数到底是什么价，它能跟谁比。**
跨平台比价出错，九成不是因为算错，而是因为把两个不同口径的数放进了同一个减法。

## 一、四层价格

| 层 | price_type | 是什么 | 代表源 | 拿它回答什么 |
|---|---|---|---|---|
| ① 市场成交价 | `transaction_index` | 基于真实成交构造的日度结算指数 | Ornn OCPI | 实际成交中枢在往哪走 |
| ①' 成交价实时值 | `transaction_live` | 同一指数的小时级实时值 | Ornn | 只用来显示"当前价"，不进日线 |
| ② 即时报价 | `offer_min` | 单条可租实例的挂牌价 | Vast、Runpod | 最低可得价在哪 |
| ③ 市场聚合报价 | `offer_p25/median/p75` | 对大量 offer 求分位数 | Vast | 报价分布整体在不在下移 |
| ④ 标准报价 | `on_demand` / `spot` / `preemptible` / `committed_3m` | 云厂商公开挂牌价 | Runpod、CoreWeave、Nebius | 运营商自己怎么定价、有没有促销压力 |

**②和③不是两个独立信号，是同一批数据的两个视图。** ③ 是 ② 的聚合。
评分时把两者都算进"价格维度"等于把 Vast 的同一批 offer 数了两遍，
所以 `metrics.py` 的跨平台中枢只吃 `offer_median`，不吃 `offer_min`。

## 二、供给层

供给不是第五种价格，是横向指标，用来回答"价格在动，是需求变了还是供给变了"。

- `offer_count` / `offer_share`：市场上主动找需求的供给量。**用份额，不用绝对数**——平台自身规模变大时所有 SKU 的 offer 数都会涨，那是平台效应。
- `available_gpu_count`：直接可租的 GPU 张数，比 offer 条数更接近真实产能。
- `stock_status`：Runpod 的 None/Low/Medium/High 四档，配 `consecutive_days_at_latest` 看是不是在升档。
- `available_region_count`：当前有货的地理区域数（来自 Vast offer 的 `geolocation`），看的是覆盖广度不是深度。
- `quote_dispersion`（P75−P25）：报价竞争程度。分散度扩大常常先于中枢下移。
- `supply_breadth`：多平台"有货"占比。宽松只有从单平台扩散到全市场才算数。

## 三、可比性规则（违反其中任何一条，减法就没有意义）

1. **同 price_type 才能比。** `committed_3m` 比 `on_demand` 便宜 20% 是合同结构，不是降价。Runpod 实测 H100 SXM：`securePrice` 3.29、`communityPrice` 2.69、`threeMonthPrice` 3.29。
2. **同 market_segment 才能比。** Runpod 的 secure（自营机房）和 community（P2P）是两个市场；Vast 的 `is_bid=true` 是可抢占档。混在一起，分布会凭空多出一条低价尾巴。
3. **同 query_fingerprint 才能比。** offer 数与分位数都是查询口径的函数。实测：不带 gpu_name 过滤时 Vast 回 64 条，带过滤回 47 条（只三个 SKU）。口径变了，序列就断了。
4. **同源集合才能比。** 跨平台中位数在源上下线的那天会跳。实测新接入 Runpod+Vast 的当天，H100 跨平台中枢凭空"跌" 25%——那是口径变动。`metrics.py` 用 `basis_match` 拦这个，并把锚点挪到最近 14 天出现最多的那个口径下。
5. **同结算频率才能比。** Ornn 的 `daily-index` 是 T-1 结算，`current` 是小时级实时。混进一条序列，最后一个点会莫名其妙地抖。
6. **单卡口径才能比。** Vast 的 `dph_total` 是整条 offer 的价（`num_gpus` 张卡），人工核对的整机挂牌价同理。都必须除以 GPU 数，并把 `node_gpu_count` 一起存，让换算可审计。
7. **同费用范围才能比。** Vast 的 `dph_total` 裹着磁盘费与 SLA 溢价，`search.gpuCostPerHour` 才是纯 GPU 费。默认取纯 GPU 费（`price_scope=gpu_only`）；Neocloud 的整机挂牌价通常含本地 NVMe 与网络，只能标 `bundled`，与纯 GPU 价对比时要说明这层差异。
8. **同 SKU 才能比。** "H100" 不是一个 SKU。Runpod 实测同日 SXM 2.69 / NVL 2.59 / PCIe 1.99，差 35%。canonical id 一律到 SKU 粒度。

## 四、折价的分子分母

- `Spot Discount = 1 − Spot ÷ On-demand`，**分子分母必须同源同 segment**。跨平台算折价没有含义：两边的成本结构不一样。
- Runpod 的 `minimumBidPrice` 实测常常等于 `uninterruptablePrice`。相等时不记为 spot，否则会算出一个恒等于 0 的假折价。
- Vast 的可抢占折价用 `interruptible` 段的 `offer_min` 比 `on_demand` 段的 `offer_median`——同平台同一批机器的两种租法。

## 五、历史深度不对称（读任何跨源比较前先看这条）

几条序列不同龄，而且短期内不会同龄：

- **Ornn 有 `index-history` 接口**，无 key 时给滚动 3 个月日度结算。上线当天就是完整的 90 天曲线，`backfill.py` 一次跑满。
- **Vast 与 Runpod 没有任何公开历史接口**，只回当下快照。它们的序列从首次采集当天开始往后长，**补不回去**。
- **CoreWeave / Nebius 是人工核对**，历史等于你核对过几次。

后果有三个，写报告时都要照顾到：

1. 「7D / 30D 变化率」在上线初期只有 Ornn 一路能算。跨平台中枢的同期比较要等 Vast/Runpod 攒够天数。
2. 评分的供给维度必然先缺席，`score.usable=false` 是冷启动的正常状态，不是故障。
3. 别拿 Ornn 的 90 天斜率去描述"整个市场过去 90 天"——那三个月里只有成交价指数在场。

`python scripts/backfill.py --report-only` 会逐条报出每个 (source, gpu, price_type) 实际覆盖到哪天、多少个点、有没有缺口。下判断之前先看它。

## 六、代际溢价

`B200/H200`、`H200/H100` 的比值只在两端**同一天**都有跨平台中枢时才算，
`date_aligned=false` 的比值掺了时间错位，不能拿来说"稀缺溢价在收窄"。

溢价收窄有两种完全不同的成因，得靠供给侧分辨：新一代供给放量（B200 offer 份额上升）
是真的稀缺缓解；老一代加速贬值（H100 份额没变但价在跌）是需求迁移。
