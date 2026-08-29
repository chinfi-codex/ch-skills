# 数据中心信用监控 · 数据规格 v0.1

范围已收敛为三层：**公开公司债**、**可转债**、**GPU 抵押融资载体 + Beignet**。
本文只定两件事：哪些数据经实测确认免费可得，以及统一的数据规格。判断逻辑（利差
走宽算不算信号、残值假设是否被证伪）不在这里，留给 SKILL.md。

实测日期 2026-08-26，市场数据 as-of 2026-08-25。

---

## 一、可得性实测结论

### 1.1 通过的源

| 源 | 端点 | 密钥 | 频率 | 实测结果 |
|---|---|---|---|---|
| SSGA / SPDR 每日持仓 | `ssga.com/.../holdings-daily-us-en-{ticker}.xlsx` | 无 | 日 | 6 只 ETF 全部 200，真 xlsx，as-of 25-Aug-2026 |
| FRED 国债曲线 | `fredgraph.csv?id=DGS{1,2,3,5,7,10,20,30}` | 无 | 日 | 200，最新 2026-08-25 |
| FRED ICE BofA 指数 OAS | `fredgraph.csv?id=BAMLC0A0CM` / `BAMLH0A0HYM2` | 无 | 日 | 200，IG OAS = 0.81% |
| SEC submissions | `data.sec.gov/submissions/CIK{10}.json` | 无（需 UA） | 事件 | 12 个标的 CIK 全部命中 |
| SEC companyfacts | `data.sec.gov/api/xbrl/companyfacts/...` | 无 | 季 | CRWV 305 个 us-gaap 标签 |
| SEC 财报渲染表 | `Archives/.../FilingSummary.xml` → `R{n}.htm` | 无 | 季 | 工具级债务表可解析 |
| SEC 全文检索 | `efts.sec.gov/LATEST/search-index?q=` | 无 | 事件 | Beignet 命中 15 篇 |

### 1.2 未通过的源

- **iShares（LQD/HYG）**：Akamai 边缘拦截。响应头写着 `content-type: text/csv` 和
  `content-disposition: attachment`，body 却是完整产品页 HTML。加 Referer、加 cookie jar
  两段式都无效。**结论：不要把 iShares 放进自动链路**，SPDR 已完全替代。
- **FINRA API**：`api.finra.org` 匿名可访问，但只有 OTC 股票数据集；固定收益 group
  全部 404。逐 CUSIP 的 TRACE 日度价格没有免费路径。**CDS 同理，v1 直接不设该字段。**

### 1.3 覆盖度（六只 SPDR ETF 去重后）

| 发行人 | 唯一债券数 | 来源分布 |
|---|---:|---|
| ORCL | 52 | spbo 39 / spib 23 / splb 27 / cwb 1 |
| AEP（含 opco） | 43 | spbo 17 / spib 25 / splb 17 |
| ETR（含 opco） | 43 | spbo 20 / spib 24 / splb 19 |
| AMZN | 40 | spbo 29 / spib 20 / splb 20 |
| GOOGL | 32 | spbo 24 / spib 16 / splb 14 / cwb 2 |
| Dominion（含 VEPCO） | 32 | spbo 12 / spib 18 / splb 12 |
| META | 26 | spbo 20 / spib 13 / splb 12 |
| MSFT | 20 | spbo 9 / spib 1 / splb 19 |
| EQIX | 17 | spbo 9 / spib 14 / splb 3 |
| NBIS | 8 | cwb 8（全部为转债） |
| CRWV | 6 | sphy 4 / jnk 4 / cwb 2 |
| DLR | 5 | spbo 2 / spib 3 / cwb 1 |

合计约 324 只工具。除 DLR 外每个发行人都够拟合一条曲线；**DLR 只有 3–5 个点，
只能做点位比较不能做曲线形状**，这个限制要写进输出。

电力层意外收获：拿到的是 opco 级一按揭债（Appalachian Power、AEP Texas、
Entergy Louisiana/Texas），这比母公司无担保债更贴近数据中心负荷的真实担保结构。

### 1.4 价格与利差链路（已端到端跑通）

SPDR 持仓不直接给价格和收益率，但给 `Par Value` 与 `Market Value`，因此

```
clean_price = Market Value / Par Value × 100
ytm         = 二分法解 (price, coupon, maturity)
g_spread_bp = (ytm − 插值国债收益率) × 100
excess_bp   = g_spread_bp − 对应 ICE BofA 指数 OAS
```

实测样本：

| 债券 | 价格 | YTM | 插值 UST | G-spread |
|---|---:|---:|---:|---:|
| META 4.6 11/32 | 96.36 | 5.32% | 4.43% | 89 bp |
| ORCL 6.9 11/52 | 89.71 | 7.83% | 5.17% | 267 bp |
| EQIX 2.15 07/30 | 89.81 | 4.99% | 4.29% | 70 bp |
| Entergy LA 5.8 03/55（一按揭） | 95.57 | 6.13% | 5.17% | 96 bp |
| CRWV 9.25 06/30 | 94.11 | 11.12% | 4.29% | 683 bp |
| CRWV 9.75 10/31 | 90.85 | 12.25% | 4.36% | 790 bp |

数值层级与各自评级、期限、担保结构一致，链路可信。

**必须写进契约的四个口径限制**：

1. ETF 持仓价是**基金管理人的估值**，不是成交价。流动性差的券会有粘滞，
   连续多日价格不动要标 `stale`，不能当成"利差没变"。
2. 这是**该发行人在指数样本中的子集**，不是全部存量债。发行人层聚合只能说
   "样本内加权"，不能说"该发行人利差"。
3. G-spread 不是 OAS。含赎回条款、浮息、次级永续的券（Dominion / AEP / Entergy
   的 jr subordinated）算出来的 G-spread 有偏，必须单独标记 `has_embedded_option`，
   不与普通高级无担保券混在一张图里比较。
4. **关联键必须落到 ISIN**。ORCL 有 52 只债，按发行人聚合会把 2030 和 2052
   混成一个数——这与 GPU token 那次 canonical_slug 碰撞是同一类错误。
5. **单只债的时间序列不能直接当利差走势读**。债券在曲线上往下滚（rolldown），
   利差会自然收窄，混进重定价里就分不清了。发行人层的时间序列**必须**走
   `drv.cm_spread_*` 这种固定期限插值点，不能用某只债自己的历史。

---

## 二、统一数据规格

四个体制的指标集是**互斥**的：有日频价格的没有抵押品字段，有抵押品字段的没有日频
价格。所以统一的不是列，是**一条观测的形状**——长表，三张表。

### 2.1 `dc_instrument`（工具主数据）

| 字段 | 类型 | 说明 |
|---|---|---|
| `instrument_key` | text PK | ISIN 优先；无 ISIN 用 `{issuer_key}:{type}:{coupon}:{maturity}` |
| `isin` | text NULL | SPV / 银团贷款为空 |
| `issuer_key` | text | 稳定发行人键，opco 归到母公司 `issuer_parent_key` |
| `issuer_parent_key` | text | Appalachian Power → AEP |
| `regime` | enum | `public_corp` / `convertible` / `gpu_secured` / `spv` |
| `instrument_type` | enum | `senior_unsecured` / `senior_secured` / `first_mortgage` / `jr_subordinated` / `convertible` / `term_loan` / `spv_secured_notes` |
| `coupon` / `coupon_type` | num / enum | `fixed` / `float` / `zero` |
| `maturity` | date NULL | 强制转股型为空 |
| `is_144a` / `has_embedded_option` / `recourse` | bool / bool / enum | `recourse` / `nonrecourse` |
| `collateral_type` | enum | `none` / `real_property` / `gpu_equipment` / `lease_receivable` |
| `first_seen` / `last_seen` | date | 从持仓文件中消失 ≠ 到期，见 §2.4 |

### 2.2 `dc_observation`（长表事实）

一行 = 一个 `instrument_key` × 一个 `metric` × 一个 `asof_date`。

| 字段 | 说明 |
|---|---|
| `instrument_key` | 外键 |
| `metric` | 命名空间键，见 §2.3 |
| `value` / `unit` | `bp` / `pct` / `usd_mn` / `x` / `bool` / `date` |
| `asof_date` | 数据本身的口径日（持仓文件的 As of、报告期末） |
| `obs_date` | 采集日 |
| `method` | `observed`（源直接给） / `derived`（本地算，如 price、ytm） / `disclosed`（申报文件文字） |
| `source_id` | 外键到 `dc_source` |
| `quality` | 见 §2.4 |
| `staleness_days` | `obs_date − asof_date` |

### 2.3 指标命名空间（按体制分，缺失即"该体制不适用"而非空洞）

```
px.clean                    公开债 / 转债      日
yld.ytm                     公开债              日
yld.gspread_bp              公开债              日
yld.excess_vs_index_bp      公开债              日   （减 BAMLC0A0CM 或 BAMLH0A0HYM2）
chg.gspread_1d/1w/1m        公开债              日   （不足窗口不外推，直接不出）

cb.parity                   转债                日
cb.conv_premium_pct         转债                日
cb.credit_extractable       转债                日   bool，见 §3

fin.net_debt_ebitda         全部有 SEC 申报者    季   companyfacts
fin.interest_coverage       同上                季
fin.capex_ttm               同上                季

col.vie_assets_current      gpu_secured         季   R-file
col.vie_assets_noncurrent   gpu_secured         季   R-file
col.facility_size           gpu_secured         季   R-file
col.facility_spread_bp      gpu_secured         季   R-file
col.implied_residual_curve  gpu_secured         月   派生，见 §4

spv.notes_outstanding       spv                 事件
spv.coupon / spv.maturity   spv                 事件
spv.tenant / spv.ownership  spv                 事件
spv.portfolio_fv            spv                 季   组合级代理，见 §3
spv.sponsor_nav_per_share   spv                 双周  8-K

drv.cm_spread_{5y,10y,30y}  发行人级            日   固定期限插值，见 §1.4 限制 5
drv.beta_market_bp          公开债              日   指数 OAS 变动贡献
drv.beta_tier_bp            公开债              日   同档中位数变动贡献（剔自己）
drv.alpha_bp                公开债              日   残差 = 总变动 − 两层 beta
drv.alpha_cum_{5d,20d}      公开债              日   alpha 累积
drv.rung_gap_{a}_{b}_bp     跨档                日   两档固定期限利差之差
drv.curve_slope_bp          发行人级            日   长端 − 短端固定期限点
drv.curve_inverted          发行人级            日   bool
drv.long_short_gap_bp       跨档                日   长端跨档差 − 短端跨档差
drv.collateral_scissors     gpu_secured         季   账面折旧率 − 租金衰减率（剥新增 capex）
```

派生层全部由 `metrics.py` 从上面的原始指标算出，**不从外部源取**，因此没有
`source_id`，`method` 恒为 `derived`。它们各自服务哪个判据，见
`plans/dc-credit-methodology.md` §三。

### 2.4 质量码（缺失必须带原因，不留白）

| 码 | 含义 |
|---|---|
| `ok` | 正常 |
| `stale` | 价格连续 N 日未变，疑似管理人未重估 |
| `thin_curve` | 该发行人样本内债券数 < 5，禁止拟合曲线（当前仅 DLR） |
| `option_biased` | 含权券的 G-spread，不可与普通券直接比 |
| `regime_na` | 该体制不存在此指标（不是抓取失败） |
| `disclosure_once` | 只在某一期申报中披露过一次，之后并入合计（Beignet） |
| `paywalled` | 已知存在但无免费源（CDS、逐笔 TRACE、新发定价） |
| `dropped_from_index` | 工具从持仓文件消失，需人工确认是到期、被剔还是回售 |

`dropped_from_index` 是必须的：ETF 持仓变动同时反映指数规则和市场变化，不加区分会
把"被剔出指数"误读成"债券消失"。

---

## 三、三个体制的具体落法

### 3.1 公开公司债

每日拉 6 只 SPDR ETF，按 ISIN 去重合并，算 price → ytm → g-spread → excess。
发行人层做样本内久期加权，但必须同时输出成分数量与 `thin_curve` 标记。
opco 与母公司分开存，聚合时经 `issuer_parent_key` 上卷。

### 3.2 可转债

**实测发现改变了这一层的做法。** NBIS 8 只转债当前价格在 142–176，深度价内：

| 工具 | 价格 |
|---|---:|
| NBIS 1.0% 09/30 | 176.45 |
| NBIS 2.75% 09/32 | 175.51 |
| NBIS 1.25% 03/31 | 142.60 |

这个位置上 delta 接近 1，债券已经是股票的线性代理，**剥离期权后的信用信息量趋近于零**。
CRWV 的两只转债（1.75% 2031 在 103.75、1.75% 2032 在 96.56）则贴近债底，信用信息还在。

所以这一层不做"统一剥离期权算信用利差"，改为：

- 日常只出 `cb.parity` 与 `cb.conv_premium_pct`，即它现在离债底多远；
- 只有当转股溢价率回升到阈值以上、价格回落到债底附近时，`cb.credit_extractable`
  才置 true，此时才值得算隐含信用利差；
- 深度价内期间，NBIS 的信用观察改看 §3.3 的基本面层，不看转债价格。

顺带：CWB 里混有强制转股优先股（Alphabet、Oracle 那几条 par/MV 比算出来 4500+），
解析时必须按 `maturity == '-'` 剔掉，否则价格字段会出现四位数。

### 3.3 GPU 抵押融资载体

CoreWeave 2026Q2 10-Q（accession `0001769628-26-000366`）已确认工具级可机读，路径是
`FilingSummary.xml` → `R67.htm`（Schedule of Total Debt Obligations）与 `R68.htm`（DDTL）。

实测抽出的字段：

- DDTL 4.0：规模 $8,500m，SOFR + 225bp / Treasury + 200bp
- DDTL 5.0：规模 $3,100m，固定 4.50%，发行费用 $25m
- VIE 资产：非流动 $18,200m（上期 $12,700m）、流动 $2,600m（上期 $1,800m）
- 自定义 XBRL 成员：`crwv:DDTL{1.0,2.0,2.1,3.0,4.0,5.0}FacilityMember`，
  全部带 `us-gaap:RecourseMember` 维度

注意 `companyfacts` 只有合计 `LongTermDebt`，**带维度的工具级事实不在里面**，必须走
R-file。这是这一层唯一的技术要点。

VIE 资产余额就是 GPU 抵押品的账面规模，季度可得，是 §4 残值校验的分母。

### 3.4 Beignet

条款来自 **Blue Owl Real Estate Net Lease Trust（OSNL，CIK 1944366）2025Q3 10-Q**
的期后事项附注——不是 Meta 的申报，也不是发行人自己的（144A 不注册）。已确认内容：

- 2025-10-21，Blue Owl 取得 Beignet Investor LLC 的 63.0% 间接权益；
  该主体持有 Project Beignet Holdings（Beignet JV）80.0% 权益
- Beignet JV 的用途是为一个**租给并由 Meta Platforms 运营**的数据中心园区提供开发资金
- Beignet Investor 发行**优先担保票据 $27,293,849 千（≈$27.29bn），票息 6.581%，
  到期 2049-05-30**
- Blue Owl 按比例承诺出资权益 $1,533,307 千（≈$1.53bn）
- XBRL 成员：`osnl:BeignetInvestorLLCMember`、`osnl:SeniorSecuredNotesDue2049Member`

**关键负面结论：Beignet 之后不再单独披露。** 2025 年 10-K 与 2026 年 Q1/Q2 的
10-Q 中 "Beignet" 出现 0 次，已并入公允价值选择项下的合计行——2025-12-31 口径为
"net lease data centers：8 项投资、15 处物业、持股 10.6%–65.5%、账面 $951,121 千"。

所以这一层的规格是：

- 条款字段一次性入库，`quality = disclosure_once`，`asof_date = 2025-10-21`，
  **不做时间序列，不允许在图上画成一条线**；
- 持续可得的只有两个组合级代理：`spv.portfolio_fv`（季度，含 Beignet 在内的
  数据中心净租赁组合公允价值与投资/物业计数）与 `spv.sponsor_nav_per_share`
  （OSNL 作为非交易 REIT，8-K 约每两周披露，实测 2026-06 至 08 有 5 次）；
- 这两个代理是**组合级**，任何结论都必须说明它包含 8 项投资而非 Beignet 单体。

---

## 四、跨层派生：残值假设的现实校验

这是本方案里唯一不能从任何单一源买到、也是最有价值的一格。

`col.vie_assets_noncurrent` 给出 GPU 抵押品的账面规模与其季度变化；
`ch-gpu-compute-monitor` 已有的 H100 / H200 / B200 租金时间序列给出同代硬件的
真实租金衰减速度。两者相除得到隐含的残值衰减曲线，与融资文件里的折旧/残值假设对照。

这一步只产出 `col.implied_residual_curve` 这个派生序列和它与账面折旧的差值；
"这个差值算不算证伪"由模型在 SKILL.md 里判断，脚本不下结论。

---

## 五、v1 不做的事

- **CDS**：无免费源，不设字段（不是留空，是不存在这一列）。
- **逐笔 TRACE 成交与新发定价 / 发行折价**：FINRA 固定收益 API 无匿名访问，
  一级市场定价无免费结构化源。若要做，只能人工录入并标 `paywalled`，v1 不排期。
- **iShares 任何端点**：已实测被拦，不要再试。
- **OAS**：需要利率模型与波动率曲面，v1 只出 G-spread 并明确标注口径。
