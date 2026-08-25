# 数据源实测契约与陷阱

以下全部是 2026-08-25 直连实测的结果，不是从文档抄的。文档会过时，
所以每条都写明"实测到的样子"和"结构变了会怎样表现"。

## Ornn OCPI（P0，成交价锚）

- **正确的地址**：API base 是 `https://api.ornnai.com`，公开文档在 `https://dashboard.ornnai.com/docs`，取 key 与全量文档在 `data.ornn.com`。PRD 附录里写的 `index.ornn.com/docs` 打不开。
- **匿名可用**，按 IP 限频。日频 1 次完全够。设了 `ORNN_API_KEY` 会带上，解锁全历史与更多 SKU。
- **免费层给滚动 3 个月日度历史**（实测 92 个点），正好覆盖首页要的 90D 窗口。**这意味着 Ornn 这一路上线当天就有完整历史，而 Vast/Runpod/Lambda 只能从今天开始攒。** 报告里必须说清这个不对称，别让读者以为四条线同龄。
- 三个 MVP 型号全在免费名单里：`B200`、`H200`、`H100 SXM`（另有 `A100 SXM4`、`RTX 5090`）。注意标识是 `H100 SXM` 带空格，不是 `H100`。
- 文档明确要求**不要把免费 SKU 名单写死**，每次从 `/api/gpu-types-free` 拉。采集器就是这么做的。
- `daily-index` 是 T-1 结算，`/api/gpu/:name` 是小时级实时。**今天的跨平台中枢结构性地不含 Ornn**——这不是故障。
- 结构变了会怎样：`success` 不为 true 或 `data` 不是数组时直接抛错，当天该源记为缺采。

## Vast.ai（P0，现货深度）

- **匿名可用**：`GET https://console.vast.ai/api/v0/bundles/?q=<json>`。PRD 说需要账号/API Key，实测查询类接口不需要。
- `dph_total` 是**整条 offer** 的小时价，不是单卡价。必须除以 `num_gpus`。
- `dph_total` 含磁盘费与 SLA 溢价；`search.gpuCostPerHour` 才是纯 GPU 费。默认取后者。
- `is_bid=true` 是可抢占档，和按需档分成两个 `market_segment`。
- **offer 数是查询口径的函数，不是市场普查**：不带 `gpu_name` 过滤实测只回 64 条，带过滤回 47 条（仅三个 SKU）。所以绝对值没有跨口径意义，只有同 `query_fingerprint` 下的时间序列可比。
- C2C 市场，供给质量参差。`reliability2 < 0.90` 与 `verification == "deverified"` 的机器会被过滤掉——垃圾供给涌入会压低 P25，读起来像降价。
- **样本很薄**：实测三个目标 SKU 各 10–20 条 offer。样本不足 `min_sample_for_quantiles`（默认 8）时只出 `offer_min`，不出分位数——15 条样本上的 P75 不是市场中枢，是噪音。
- 结构变了会怎样：返回里没有 `offers` 数组即抛错。

## Runpod（P0，库存分级）

- **匿名可用**：`POST https://api.runpod.io/graphql`，不带 key 也能查 `gpuTypes`。PRD 说需要 API Key，实测不需要（设了 `RUNPOD_API_KEY` 会带上）。
- 实测同日：B200 5.98（Low）、B300 6.94（Low）、H100 SXM 2.69（High）、H100 NVL 2.59（Low）、H100 PCIe 1.99（Low）、H200 SXM 3.59（High）、H200 NVL 全 null。
- **`securePrice` vs `communityPrice` 是两个市场**（H100 SXM 实测 3.29 vs 2.69），分开存。`threeMonthPrice` 是承诺期价，另存 `committed_3m`。
- **`minimumBidPrice` 常常等于 `uninterruptablePrice`**（实测五个目标 SKU 全部相等）。相等时不记为 spot。
- **`lowestPrice` 全 null ≠ 接口坏了**，是该 gpuCount 下当前无货（实测 MI300X、A100 PCIe、H200 NVL 就是全 null）。这种情况 `quality_flag=no_stock`，与采集失败区分开。
- PRD 点名的 `availableGpuCounts` 字段在当前 schema 里没查到；供给侧目前靠 `stockStatus` + `maxGpuCount`。要用它得先确认字段名还在不在。
- 结构变了会怎样：`gpuTypes` 为空、或一个目标 SKU 都没匹配上，都直接抛错并提示去核对 `config/gpu_catalog.yaml` 的别名——**因为"匹配不上"和"市场上没有"看起来一模一样，必须靠报错区分。**

## Lambda（P0，Neocloud 标准报价）

- **必须有 key**：无 key 实测直接 401，没有匿名降级路径。缺 `LAMBDA_API_KEY` 时采集器抛错，当天该源如实记成缺采——不能静默跳过，跳过会让 `supply_breadth` 的分母无声变小。
- 卖整机实例（1x / 8x），`price_cents_per_hour` 是整机价，必须除以实例 GPU 数。取不到 GPU 数时宁可跳过也不猜一个 8。
- `regions_with_capacity_available` 是 region 覆盖的来源；空列表记 `stock_status=None`。
- 同一 SKU 有 1x 与 8x 两种实例时，取单卡价更低的那个作挂牌价，region 覆盖合并。

## CoreWeave / Nebius / Crusoe（P1/P2，人工核对）

只有营销 Pricing 页，没有稳定 API，**不做自动抓取**。理由：这类页面改版频繁，
选择器一失效，解析器最容易做的事就是悄悄返回 0 或空，而空值会被下游读成"降价"——
比没有数据更危险。

改成 attested 模式：人核对后写进 `config/attested_prices.yaml`，每条带 `as_of` 与 `source_url`，
超过 `max_age_days`（默认 30 天）自动降级 `stale`，退出跨平台中位数与评分，
只留在标准报价矩阵里并标出核对日期。

哪天要给它们写爬虫，必须先加**结构指纹校验**：抓取时确认页面上的关键锚点文本还在，
不在就报错，而不是让选择器返回空。

## 凭证

| 变量 | 必需性 | 说明 |
|---|---|---|
| `ALPHA_PG_URL` | 必需 | PostgreSQL 连接串，走 `shared/data/db_core.py` 统一契约 |
| `LAMBDA_API_KEY` | Lambda 必需 | 无 key 该源整天缺采 |
| `ORNN_API_KEY` | 可选 | 解锁 3 个月以外的历史与更多 SKU |
| `VAST_API_KEY` | 可选 | 匿名限频更紧，有 key 更稳 |
| `RUNPOD_API_KEY` | 可选 | 同上 |

全部从环境变量读，不写进仓库。
