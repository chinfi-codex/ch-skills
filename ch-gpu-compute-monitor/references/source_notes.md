# 数据源实测契约与陷阱

以下全部是 2026-08-25 直连实测的结果，不是从文档抄的。文档会过时，
所以每条都写明"实测到的样子"和"结构变了会怎样表现"。

## Ornn OCPI（P0，成交价锚）

- **正确的地址**：API base 是 `https://api.ornnai.com`，公开文档在 `https://dashboard.ornnai.com/docs`，取 key 与全量文档在 `data.ornn.com`。PRD 附录里写的 `index.ornn.com/docs` 打不开。
- **匿名可用**，按 IP 限频。日频 1 次完全够。设了 `ORNN_API_KEY` 会带上，解锁全历史与更多 SKU。
- **免费层给滚动 3 个月日度历史**（实测 92 个点），正好覆盖首页要的 90D 窗口。**这是全项目唯一一个有历史接口的源**：它上线当天就是完整曲线，而 Vast/Runpod 只能从首次采集当天往后长。报告里必须说清这个不对称，别让读者以为几条线同龄。
- 免费名单五个 SKU 全部登记在册：三个主力 `B200` / `H200` / `H100 SXM`，外加两个参照系 `A100 SXM4`（上一代的价格地板）与 `RTX 5090`（消费级溢出产能的温度计）。后两个不进首页三型号同屏，但它们的 90 天历史一分钱不多花，拿来判断"高端在涨是不是整条曲线在涨"很有用。
- 注意标识是 `H100 SXM` 带空格，不是 `H100`。
- 文档明确要求**不要把免费 SKU 名单写死**，每次从 `/api/gpu-types-free` 拉。采集器就是这么做的。
- `daily-index` 是 T-1 结算，`/api/gpu/:name` 是小时级实时。**今天的跨平台中枢结构性地不含 Ornn**——这不是故障。
- 结构变了会怎样：`success` 不为 true 或 `data` 不是数组时直接抛错，当天该源记为缺采。

## Vast.ai（P0，现货深度）

- **没有历史接口。** `bundles` 只回"此刻"的挂牌快照，翻不出昨天。所以 Vast 的所有序列都从首次采集当天开始，补不回去——这不是实现偷懒，是接口不存在（`/bundles/history` 之类的路径实测都是 404 或要登录）。
- **匿名可用**：`GET https://console.vast.ai/api/v0/bundles/?q=<json>`。PRD 说需要账号/API Key，实测查询类接口不需要。
- `dph_total` 是**整条 offer** 的小时价，不是单卡价。必须除以 `num_gpus`。
- `dph_total` 含磁盘费与 SLA 溢价；`search.gpuCostPerHour` 才是纯 GPU 费。默认取后者。
- `is_bid=true` 是可抢占档，和按需档分成两个 `market_segment`。
- **offer 数是查询口径的函数，不是市场普查**：不带 `gpu_name` 过滤实测只回 64 条，带过滤回 47 条（仅三个 SKU）。所以绝对值没有跨口径意义，只有同 `query_fingerprint` 下的时间序列可比。
- C2C 市场，供给质量参差。`reliability2 < 0.90` 与 `verification == "deverified"` 的机器会被过滤掉——垃圾供给涌入会压低 P25，读起来像降价。
- **样本很薄**：实测三个目标 SKU 各 10–20 条 offer。样本不足 `min_sample_for_quantiles`（默认 8）时只出 `offer_min`，不出分位数——15 条样本上的 P75 不是市场中枢，是噪音。
- 结构变了会怎样：返回里没有 `offers` 数组即抛错。

## Runpod（P0，库存分级）

- **同样没有历史接口**，而且 GraphQL 关掉了 introspection（实测报 `INTROSPECTION_DISABLED`），没法靠自省找出别的时序入口。序列同样只能从首次采集当天开始。
- **匿名可用**：`POST https://api.runpod.io/graphql`，不带 key 也能查 `gpuTypes`。PRD 说需要 API Key，实测不需要（设了 `RUNPOD_API_KEY` 会带上）。
- 实测同日：B200 5.98（Low）、B300 6.94（Low）、H100 SXM 2.69（High）、H100 NVL 2.59（Low）、H100 PCIe 1.99（Low）、H200 SXM 3.59（High）、H200 NVL 全 null。
- **`securePrice` vs `communityPrice` 是两个市场**（H100 SXM 实测 3.29 vs 2.69），分开存。`threeMonthPrice` 是承诺期价，另存 `committed_3m`。
- **`minimumBidPrice` 常常等于 `uninterruptablePrice`**（实测五个目标 SKU 全部相等）。相等时不记为 spot。
- **`lowestPrice` 全 null ≠ 接口坏了**，是该 gpuCount 下当前无货（实测 MI300X、A100 PCIe、H200 NVL 就是全 null）。这种情况 `quality_flag=no_stock`，与采集失败区分开。
- PRD 点名的 `availableGpuCounts` 字段在当前 schema 里没查到；供给侧目前靠 `stockStatus` + `maxGpuCount`。要用它得先确认字段名还在不在。
- 结构变了会怎样：`gpuTypes` 为空、或一个目标 SKU 都没匹配上，都直接抛错并提示去核对 `config/gpu_catalog.yaml` 的别名——**因为"匹配不上"和"市场上没有"看起来一模一样，必须靠报错区分。**

## OpenRouter（P0，需求端量价）

两个接口一起用，缺一路就出不了 spend：

| 用途 | 端点 | 认证 | 实测（2026-08-25） |
|---|---|---|---|
| 量 | `/api/frontend/v1/rankings/models?view=day` | 匿名 | 530 行，模型 × 变体级 prompt/completion token 与请求数 |
| 价 | `/api/v1/models` | 匿名 | 419 条，`pricing.{prompt,completion,input_cache_read}`，USD/token |
| 价差带 | `/api/v1/models/{id}/endpoints` | 匿名 | 单模型最多 36 个 provider 报价 |
| 调用方 | `/api/frontend/v1/rankings/apps?view=day` | 匿名 | 20 行，app_id + token + 请求数 + 应用元信息 |

**字段陷阱（踩过的，按严重程度排）：**

1. **`:batch` / `:free` 变体与标准条目共用同一个 `canonical_slug`**，实测 69 处碰撞。
   按 canonical_slug 建索引会让变体覆盖标准条目，标准流量被按半价甚至零价计——
   整站 spend 从 $8.69M/日 掉到 $5.33M/日（差 63%），混合价从 $0.797 掉到 $0.529/Mtok。
   **join 键必须是「剥掉 `-20YYMMDD` 的 base + variant」**，`variant_permaslug` 就是
   `model_permaslug:variant`。
2. **`total_native_tokens_reasoning` / `total_native_tokens_cached` / `total_tool_calls`
   全部为 0**。字段在、值不给，别以为能拿它们拆 reasoning 通胀或缓存命中率。
3. **零价的主体不是免费档**。`free` 变体只占当日 token 7.8%，而零价合计 40.1%——
   差额几乎全来自榜首那个匿名 stealth 模型（`standard` 变体、pricing 明写 `"0"`、
   一家 32.3%）。它进出榜单会让总量序列直接跳一大截。
4. **日期范围参数全部被忽略**。`start_date` / `days` / `period` 传了不报错也不生效，
   只有 `view=day|week|month` 有用。别以为能靠参数回填历史。
5. **`view=day` 给的是已结算的 T-1**。相隔 20 分钟的两次快照逐行完全一致
   （总量都是 18,352,911,061,978），说明那一天是冻结的、不是滚动 24 小时累加。
   与 Ornn 的 T-1 UTC 结算天然同日。**日切是否严格卡在 UTC 午夜尚未验证**，
   要跨日采集后回看，所以证据包里照样报 `alignment_lag_days`。
6. **未匹配的 0.43% 主要是 embedding / rerank / STT / 图像视频模型**，
   它们本来就不按 token 对价。记进 `unmapped` 供审计，不静默丢。

**调用方那一路（`rankings/apps`）自己的四个坑：**

1. **响应形状是 `data.{day,week,month}` 三个桶，不是像模型榜那样 `data` 直接是数组。**
   取错桶就会把滚动 7 天的量当成单日量。
2. **`total_tokens` 是字符串**（bigint 序列化成 str），当整数直接相加会拼成字符串。
   `total_requests` 倒是整数——同一行里两种类型，很容易只 cast 一个。
3. **返回的 20 行不是「前 20 名」。** 实测 rank 是 `1,5,6,7,8,9,10,11,12,13,14,17,19,
   20,21,22,23,24,25,26`——跳过了 2/3/4/15/16/18，说明有名次不公开露出。所以合计
   只是应用侧总量的**下界**，「其他」也不能读成「未上榜的应用」。
4. **响应里没有 date 字段。** 观测日只能沿用同一次取数里模型榜的结算日（同站同 `view`，
   同一个 T-1）。这是个假设不是事实，跨日采集后要回看验证。

还有一条不是坑但决定了它能干什么：**这一路只给 token 与请求数，不拆模型，也没有价**，
所以它和 `token_model_observations` 没有任何可 join 的键，spend 无从归属。落进
`token_app_observations` 另一张表。实测 2026-08-25 榜上 20 个应用合计 7.13T token，
占当日全站 18.78T 的 38.0%；但请求数只占 9%——agentic 应用单次调用的 token 量比
长尾对话高一到两个数量级，这个错位本身就是信息。

**降级策略**：这是未文档化的前端接口，随时可能改版或封禁。解析不出预期结构一律抛
`CollectorError`，绝不返回 0 或沿用前值。逐 provider 价差带是补强不是核心，
取不到时降级成一条 note，不拖垮整次采集。

**历史只有一路，而且是另一个口径**：`/api/frontend/v1/rankings/market-share`
给厂商级周度序列，实测 52 个点回到 2025-09-01（丢掉未结算的最新点后 51 个）。
它与日榜对不上（同日各厂商比值 1.42–1.75，非常数倍），落进 `token_volume_history`
另一张表，只能看份额与增速。**最后一个点是活的**：两次取数间它从 2.78443e13
降到 2.75664e13，会往下走说明是滑动窗口，采集时就丢。厂商级是硬上限——
grouping / by / level / dimension / limit 五个参数试过，都不改变返回的键集合。

**日度序列没有历史接口**：rankings 只回当下，序列从首采日往后长，补不回去。
`view=month` 的滚动总量能给上线首日一个水平锚，但那是一个数不是一条线；
能不能靠每日差分构造 30 日滚动序列，要拿两天以上真实数据验过才敢用。

## CoreWeave / Nebius / Crusoe（P1/P2，人工核对）

只有营销 Pricing 页，没有稳定 API，**不做自动抓取**。理由：这类页面改版频繁，
选择器一失效，解析器最容易做的事就是悄悄返回 0 或空，而空值会被下游读成"降价"——
比没有数据更危险。

改成 attested 模式：人核对后写进 `config/attested_prices.yaml`，每条带 `as_of` 与 `source_url`，
超过 `max_age_days`（默认 30 天）自动降级 `stale`，退出跨平台中位数与评分，
只留在标准报价矩阵里并标出核对日期。

哪天要给它们写爬虫，必须先加**结构指纹校验**：抓取时确认页面上的关键锚点文本还在，
不在就报错，而不是让选择器返回空。

### 2026-08-25 核对到的东西（下次核对前先读这段）

**两家的计价单位不一样，这是抄错概率最高的地方。**

- **CoreWeave 报整机价。** 表格里明写 `GPU Count`，HGX 节点是 8 卡，$49.24/小时是整台 HGX H100 的价，单卡要除以 8。写进 `attested_prices.yaml` 时 `node_gpu_count: 8`。
- **Nebius 报单卡价。** 表头明写 `On-demand, GPU-hour`，旁边的 `16` / `200` 是每卡配的 vCPU 与内存，不是 GPU 数。`node_gpu_count: 1`。把 16 当成 GPU 数再除一遍，价格会缩到十六分之一。
- 两家的价都含配套 vCPU、系统内存、本地 NVMe 与网络，`price_scope` 一律 `bundled`，跟 Vast 的纯 GPU 费不是一个口径。

**当天抄到的数**（单卡口径，USD/GPU·hour）：CoreWeave 北美区 H100 6.16 / H200 6.31 / B200 8.60（spot 2.46 / 2.62 / 4.26）；Nebius H100 3.85 / H200 4.50 / B200 7.15（preemptible 2.15 / 2.45 / 3.95），另有 B300 7.85 / 4.30。

**看到了但没抄的，以及为什么：**

- CoreWeave 的 HGX B300 与 GB200/GB300 NVL72，按需价写的是「Contact sales」，没有公开价可抄。
- CoreWeave 的 `NVIDIA A100`（8 卡 80GB，$21.60/小时）。页面 tech specs 里 A100 有 NVLINK 与 PCIe 两个变体，定价表这一行没写是哪个。按「认不出就不猜」的规矩不抄——猜错一次，A100 这个参照系的整条序列就废了。
- Nebius 的 RTX PRO 6000 与 L40S，不在 SKU 目录里。
- CoreWeave 欧洲区。按需价与北美完全相同，只有 spot 略有差异（H100 $19.51 / H200 $20.64 / B200 $34.87 对北美的 $19.71 / $20.93 / $34.11）。**只录一个地区**，否则同一天同一 price_type 会出现两个地区值——虽然 region 是主键的一部分不会覆盖，但下游按 (type, segment) 取值时哪个胜出是随机的。

**下次核对：2026-09-24 之前。** 超过 `max_age_days`（30 天）这批条目会自动降级 `stale`，退出跨平台中位数与评分，标准报价层重新只剩 Runpod 一路。

## 凭证

| 变量 | 必需性 | 说明 |
|---|---|---|
| `ALPHA_PG_URL` | 必需 | PostgreSQL 连接串，走 `shared/data/db_core.py` 统一契约 |
| `ORNN_API_KEY` | 可选 | 解锁 3 个月以外的历史与更多 SKU |
| `VAST_API_KEY` | 可选 | 匿名限频更紧，有 key 更稳 |
| `RUNPOD_API_KEY` | 可选 | 同上 |

全部从环境变量读，不写进仓库。
