# Deep 模式：命题先行的主动调研挖掘

骨架分析（SKILL.md 主流程）回答"这家公司是什么、贵不贵"。Deep 模式在骨架之上，**主动调研补完公开信息中缺失的部分**：定位业绩/财务异常的成因，并从年报/季报里挖出尚未被骨架覆盖、与主线相关的线索（尤其是未被定价的"期权"）。

本文件是 Deep 模式的方法论，按需加载。脚本（`thesis_scan.py`、`data_fetcher.py` 的 `--section`）只做确定性 surfacing 与原文定位；**命题证实/证伪、归因、贵贱与买卖判断、措辞，全部由你完成**。

## 0. 何时进入

仅在用户**显式要求**时进入（"深度分析""深挖""把异常查清楚""读年报找线索""深度调研"等）。骨架版保持快、便宜；Deep 慢且贵（读 PDF + 多次联网），按需调用。进入后先声明本轮的调研预算（见 §6）。

## 1. 核心原则

- **命题先行**：用户特意来分析某家公司，一定有理由——它要么业绩暴涨/暴增、要么稳健增长却没被定价、要么有困境反转或未被定价的远期期权。平庸公司没人特意分析。所以 Deep 模式不是中立扫异常，而是**先把公司归到一个投资命题原型，再去证实/证伪它，并回答"为什么还没被市场定价"**。
- **脚本只 surfacing**：`thesis_scan.py` 给命题原型与异常旗标，都是线索不是结论；阈值命中 ≠ 利空/利好。
- **事实 vs 推断分离**：写进报告的每条结论标明来源——"原文事实"（引到公告/年报章节/页）还是"模型推断"。原文拿不到时降置信度，不编造。
- **预算有界**：设定最多读 N 篇原文、查 M 次新闻、跑到某条件即停（§6），不无限深挖。

## 2. 主循环：THESIS → INVESTIGATE → CATALYST GAP → INTEGRATE

```
THESIS       跑 thesis_scan.py → 得到命题原型(含预期差A1/A2/B) + 异常旗标 + probe 建议
  ↓
INVESTIGATE  对每个高优先命题/旗标，按 probe 拉原文/新闻验证（方向一归因 + 方向二线索挖掘）
  ↓
CATALYST GAP 回答：什么事件会让这个命题被市场重新定价？什么会证伪它？（预期差尤其要答这条）
  ↓
INTEGRATE    把发现写回骨架报告对应章节，标注事实 vs 推断，必要时上/下调置信度
```

先跑：

```bash
python scripts/thesis_scan.py --evidence reports/evidence_<code>.json
```

读 `thesis_candidates`（定命题）、`anomaly_flags`（定要查什么）、`notes`（数据缺口）。按 severity=high 与命题相关性排优先级，只查值得查的。

## 3. 命题原型（thesis_scan 的分类，你来确认）

| 原型 | scan 信号 | 你要证实/证伪什么 |
|---|---|---|
| 高增长已兑现 | 高 CAGR + 估值同步抬升 | 增速可持续性、是否透支 |
| 困境反转 / 边际拐点 | 增速·利润率·OCF 由负转正、毛利率拐点 | 拐点是否成立、可持续 |
| 业绩暴跌 | 大幅负增长 | 下跌是否一次性、是否出清（反转/抄底命题） |
| **预期差（重点）** | 见下三子型 | 增长为何没被定价？催化剂缺口在哪 |

**预期差三子型**：

- **A1 高估值消化型**：持续 CAGR + 月线横盘，起点是高估值，靠时间 + 盈利追赶把估值 de-rate 下来（PE 绝对值仍偏高但分位已低）。要查：消化是否到位、增速能否延续撑住当前估值。
- **A2 低估值 + 成长型**：低估值（PE 绝对值与分位双低）且持续增长——**最干净、最该主动挖出来的预期差**。要查：是不是真便宜（有没有隐藏的雷）、为什么市场没给（不在主线？流动性？被错杀？），以及什么会触发重估。
- **B 未被定价的"期权"型**：当前业绩无大问题且在增长，但有新的、远期的、未被定价的期权（机器人 / AI / 端侧 / 远期技术）。**这一型必须靠方向二读年报/季报确认**——scan 只能从研发强度上升给弱信号，期权的"实体"在公司自述里。

> `thesis_scan` 用估值分位 + 盈利 CAGR 做 A1/A2 的确定性初判；**"月线横盘"需用长周期日线确认**（pack 默认 daily 仅 60 日）：
> ```bash
> python scripts/data_fetcher.py fetch daily <code> --limit 0   # 取全量日线自算月线/区间
> ```

## 4. 方向一：异常驱动的归因挖掘

对 `anomaly_flags` 里 severity=high/med 的项，按其 `probe` 升级读原文：

1. **业绩暴增/暴跌** → 读对应期年报/季报的「管理层讨论与分析」找驱动（量/价、分产品、新客户），叠加联网检索行业景气与新闻。
2. **OCF/净利 弱、应收/存货扩张快于收入** → 读年报「财务报告」附注（应收账龄、信用政策、存货跌价计提），判断是利润含金量问题还是扩张期特征。
3. **商誉/净资产高、减值/计提** → 读「财务报告」附注的资产减值科目 + 收购标的公告，确认减值触发与会计政策。
4. **扣非与归母背离** → 查非经常性损益构成（政府补助/公允价值变动/处置收益）。

定向读原文（避免盲读前 60k 字）：

```bash
python scripts/data_fetcher.py fetch report-text <code> --report-type annual --section mda
python scripts/data_fetcher.py fetch report-text <code> --report-type annual --section 财务报告
python scripts/data_fetcher.py fetch announcement-text <code> --date <范围> --searchkey 减值 --announcement-index 1
```

`--section` 可用：`mda`/管理层讨论、`财务报告`/附注、`重要事项`/募集、`公司治理`、`股东`；不命中章节时自动降级为关键词窗口。年报有标准「第X节」章节、季报没有（会走关键词窗口）。

归因输出落位：报告「成长性与财务质量诊断」与「风险提示」，财务异常在「估值结论」下调置信度。

## 5. 方向二：主线线索的主动挖掘（含 B 型期权确认）

骨架最容易漏的，是**公司自己在定期报告里披露、但与主线相关、未被骨架点出的线索**（如思特威 Q1 的"视觉AI–AI互连–端侧AI ASIC"技术生态）。流程：

1. **先定当前主线关键词集**（联网检索，主线时变、勿凭记忆）：如 AI/算力/端侧/国产替代/车载/机器人/CPO/SoC/ASIC…
2. **定向抽取定期报告的相关章节**：年报读 `--section mda`（经营情况、核心竞争力、研发）；季报用关键词窗口直接搜主线词：
   ```bash
   python scripts/data_fetcher.py fetch report-text <code> --report-type annual --section mda --to-markdown
   python scripts/data_fetcher.py fetch report-text <code> --report-type q1 --section AI       # 季报：关键词窗口
   python scripts/data_fetcher.py fetch report-text <code> --report-type annual --section 研发  # 关键词窗口
   ```
   （装了 `pymupdf4llm` 时 `--to-markdown` 保留标题/表格结构；否则自动用 PyMuPDF 纯文本。）
3. **关键词交叉**：在抽出的文本里找"新产品/在研项目/募投投向/客户订单/第二曲线 + 主线词"的命中句，连同上下文 surface 出来。
4. **逐条判材料性**：是实质布局（有募投/产能/客户/收入节奏）还是套话？与主线连接强/弱？对 B 型期权，重点判断"市场是否已经定价了这块"。

线索输出落位：报告「核心看点」（作为成长动力/远期空间）与「主线归属修正」（作为公司级催化与连接证据）；B 型期权写清"当前未被定价"的判断依据与置信度。

## 5.5 重点资料类型优先级（Deep 挖掘时按此顺序覆盖）

方向二的线索挖掘不是"看到什么读什么"，而是按信息密度和可信度维护一个优先级列表。对每家公司，至少覆盖以下五类，前面的没读完不跳到后面：

| 优先级 | 资料类型 | 读什么 | 工具命令 |
|---|---|---|---|
| 1 | **年报管理层讨论与分析** | 经营情况、核心竞争力、研发方向、风险因素、募投进展 | `fetch report-text <code> --report-type annual --section mda` |
| 2 | **最新季报经营相关内容** | 当季经营亮点、产品/客户进展、与年报的增量信息 | `fetch report-text <code> --report-type q1 --section <经营关键词>` |
| 3 | **机构调研记录** | 市场关注焦点、公司在研/在产新品线索、客户导入节奏 | `fetch institutional-research <code>` |
| 4 | **定增/再融资审核问询函回复** | 募投产品详细规格、技术路线、商业化安排、客户名单、效益测算依据、对异常财务指标的监管问询解释 | `fetch announcements <code> --searchkey "" --tabtype fulltext` → 定位"审核问询函回复" → `announcement-text` |
| 5 | **其他重要公告** | 重大合作、订单、股权激励、回购/增减持、监管函 | `fetch announcements <code>` 按标题筛选 |

> **为什么定增回复函排第四却信息密度极高**：审核问询函是监管替投资者提问，公司必须逐项回复。其中往往包含年报不会细写的——产品规格参数、技术难点与攻克保障、客户导入名单、收入结构变动的定量解释、存货/应收/现金流的监管质询回复。对 B 型期权确认和财务异常归因来说，这是**仅次于年报 MD&A 的高价值源**。

## 6. subagent 编排、预算与停止条件

- **subagent 隔离**：读 PDF 原文很占上下文。每个异常/线索派一个调研 subagent，指令里给定"读哪篇、查什么、只回结论 + 引用（章节/页/URL）"，主上下文只收结论，不收全文。
- **预算**：默认上限——读 ≤ 10篇原文、联网 ≤ 10次。命题已被证实/证伪、或继续查边际信息很低时即停。
- **停止条件**：高优先命题与 high-severity 旗标查完即可收口；不为补全而补全。
- **数据缺口**：scan `notes` 指出的缺口（样本不足、缺 valuation-band、无长周期日线）要在结论里明说并降置信度。

## 7. 工具速查

| 目的 | 命令 |
|---|---|
| 命题 + 异常扫描 | `python scripts/thesis_scan.py --evidence reports/evidence_<code>.json` |
| 定向读年报章节 | `fetch report-text <code> --report-type annual --section mda\|财务报告\|重要事项` |
| 季报/关键词窗口 | `fetch report-text <code> --report-type q1 --section <关键词>` |
| 读公告原文 | `fetch announcement-text <code> --date <范围> --searchkey <词> --announcement-index 1` |
| 长周期日线（月线横盘） | `fetch daily <code> --limit 0` |
| 估值分位 | `fetch valuation-band <code> --years 5` |
| 定增问询函回复（高信息密度） | `fetch announcements <code> --searchkey "" --tabtype fulltext --limit 50` 手动定位 → `fetch announcement-text` |
| 当前主线 / 行业景气 / 竞争 | 联网检索（本 skill 无研报数据） |

## 8. 与骨架的关系

Deep 模式不另起报告，发现以"深度调研发现"小块嵌进骨架模板对应章节。骨架的判断框架（分型、估值模式、主线归属修正）不变；Deep 只是把"骨头架子"填上有原文支撑的肉，并据此调整置信度。
