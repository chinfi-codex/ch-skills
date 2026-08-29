# 数据源实测契约（2026-08-26 逐个 curl 验证）

动手改采集之前先读这一页。这里记的是抄错概率最高的地方。

## 通过的源

| 源 | 端点 | 密钥 | 频率 | 实测 |
|---|---|---|---|---|
| SSGA / SPDR 每日持仓 | `ssga.com/.../holdings-daily-us-en-{ticker}.xlsx` | 无 | 日 | 6 只 ETF 全部 200，真 xlsx |
| FRED 国债曲线 | `fredgraph.csv?id=DGS{1,2,3,5,7,10,20,30}` | 无 | 日 | 200 |
| FRED 指数 OAS | `fredgraph.csv?id=BAMLC0A0CM` / `BAMLH0A0HYM2` | 无 | 日 | 200 |
| SEC submissions / companyfacts | `data.sec.gov/...` | 无（需 UA） | 事件 / 季 | 12 个 CIK 全命中 |
| SEC 财报渲染表 | `FilingSummary.xml` → `R{n}.htm` | 无（**需邮箱 UA**） | 季 | 见下 |

## 五个坑

1. **iShares 走不通，不要再试。** LQD/HYG 的 ajax 端点被 Akamai 拦：响应头写着
   `content-type: text/csv` 和 `content-disposition: attachment; filename=LQD_holdings.csv`，
   body 却返回完整产品页 HTML。加 Referer、加 cookie jar 两段式都无效。
   SSGA 无门禁且有转债 ETF，覆盖更全。

2. **SEC 的两条路严格程度不同。** `data.sec.gov`（submissions / companyfacts）没有邮箱
   也放行；`www.sec.gov/Archives`（FilingSummary、R-file）**没有邮箱一律 403**。
   实测：UA 写 `ch-dc-credit-monitor research research contact via repo owner` → 403，
   写 `ch-dc-credit-monitor research ops@example.com` → 200。
   所以 `SEC_CONTACT_EMAIL` 是 R-file 路径的**硬依赖**，缺了会被前置拦下并标
   `missing_contact`，而不是撞三次 403。
   注意 SEC 不在乎你声称是什么浏览器——通用浏览器 UA 反而会 403，这跟 Yahoo 的
   边缘分类器规则正好相反。

3. **工具级债务不在 companyfacts 里。** 那里只有合计 `LongTermDebt`。
   `crwv:DDTL4.0FacilityMember` 这种带维度的事实必须走
   `FilingSummary.xml` → `R{n}.htm`，按 ShortName 匹配
   "Schedule of Total Debt Obligations" 与 "Delayed Draw Term Loans"。
   实测能抽出：DDTL 4.0 $8,500m @ SOFR+225 / Treasury+200、DDTL 5.0 $3,100m @ 4.50%、
   VIE 资产非流动 $18,200m（上期 $12,700m）。

4. **指数 OAS 的单位是百分数。** FRED 给 `0.81` 表示 81bp，必须 ×100。
   这是本 skill 抄错概率最高的一个数。

5. **CWB 里混着股票。** 强制转股优先股的 `maturity` 是 `-`，par/MV 比算出来四位数
   （实测 Alphabet 4922、Oracle 4567）。必须按 `maturity == '-'` 剔掉，
   否则价格字段会出现 4900 这种「债券价格」。

## Beignet 的数据门在投资方，不在发行人

144A 不注册，Meta 的申报里也没有。唯一的门是
**Blue Owl Real Estate Net Lease Trust（OSNL，CIK 1944366）2025Q3 10-Q 的期后事项附注**
（accession `0001944366-25-000126`）：

- 2025-10-21 收购 Beignet Investor LLC 63.0% 间接权益，该主体持 Beignet JV 80.0%
- 园区租给并由 Meta Platforms 运营
- 发行优先担保票据 **$27,293,849 千 / 6.581% / 到期 2049-05-30**
- Blue Owl 按比例承诺出资 $1,533,307 千

**之后不再单独披露。** 2025 年 10-K、2026 Q1/Q2 的 10-Q 里 "Beignet" 出现 0 次，
已并入公允价值选择项下的合计行（2025-12-31 口径：net lease data centers，
8 项投资 / 15 处物业 / 持股 10.6–65.5% / 账面 $951,121 千）。
所以条款只能一次性入库、标 `disclosure_once`，**渲染层拒绝画成折线**。

## 已知无免费源（不建列，只备案）

| 项 | 为什么 |
|---|---|
| CDS | Markit/ICE 授权。`api.finra.org` 匿名可访问但只有 OTC 股票数据集 |
| 逐笔 TRACE | FINRA API 的固定收益 group 全部 404 |
| 新发定价与发行折价 | 一级市场指引→定价、认购倍数无免费结构化源，走人工录入 |

建空列会让面板长期挂着永远是 NULL 的格子，所以这三项**根本不建列**。
