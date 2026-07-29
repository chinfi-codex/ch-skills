# 电话会议怎么读

财报数字告诉你上个季度发生了什么，电话会议告诉你下几个季度会发生什么。这个宇宙里,**会议的信息量通常大于季报本身**——指引、客户、产能、供需紧张度、管理层对追问的躲闪，都只在这里。

脚本把会议切成了 `prepared`（预备发言）和 `qa`（问答）两段，这个切分不是装饰：**预备发言是公司想让你听到的叙事，问答是卖方不肯放过的地方**，两段要用完全不同的方式读。

取全文用：

```bash
python3 scripts/read_source.py transcript NVDA --frame CY2026Q2 --section qa
python3 scripts/read_source.py transcript TSM --frame CY2026Q2 --find "CoWoS,产能,gross margin"
```

---

## 六个抓手

### 1. 指引的完整形状（预备发言，CFO 段）

不要只记一个营收数。一份完整的指引至少包含：营收区间、毛利率区间、经营费用、税率、股数。逐条对照上季给的同口径，判断**哪一项在变好、哪一项在变差**。

典型信号：营收指引超预期但毛利率指引下调 = 在用价格或 mix 换量；营收指引平淡但毛利率上调 = 结构改善，往往被市场低估。

还要抓**全年指引的动作**：上修 / 维持 / 下修，以及管理层给的理由。全年上修的分量远大于单季指引好看。

### 2. 需求侧的具体证据（预备发言 + 问答）

分辨"具体"和"漂亮话"是读会议的核心技能：

- **具体**：点名客户或客户类型、给出订单可见度期限（"visibility through 2027"）、给出 backlog 金额、说"sold out"、给出出货量或产能数字、给出某产品线的营收占比。
- **漂亮话**：`we're excited about the long-term AI opportunity`、`demand remains robust`、`we're well positioned`。这类句子零信息量,**不要引用进报告当证据**。

写报告引用需求证据时,必须能指到具体段落。用 `read_source.py transcript <T> --find "visibility,backlog,sold out,capacity"` 定位。

### 3. 供给与产能约束（多在问答）

AI 链的瓶颈常年不在需求在供给：CoWoS/先进封装产能、HBM 供应、光模块交付、电力与变压器交期。管理层怎么描述紧张度（"tight through next year" vs "supply has caught up"）直接决定下游能出多少货。

**这是跨公司交叉验证最有力的一环**——见 `cross_company.md`。

### 4. 毛利率桥（问答里被追问最多）

管理层几乎总会解释毛利率的同比/环比变动来自什么：产品 mix、良率爬坡、新厂折旧、关税、汇率、一次性存货冲销。把这个桥抄下来，因为：

- 它解释了数字层看不懂的毛利率跳变；
- 它包含前瞻信息（"新厂折旧会在未来两个季度继续拖累 2-3 个百分点"）；
- 它是判 `guidance_call` 的重要输入。

### 5. 问答攻防：分析师追问什么 = 市场担心什么

按顺序读问答，注意三件事：

- **重复出现的问题**。三个以上分析师问同一件事（库存、某客户份额、定价、竞争），那就是当下的核心争议，报告必须回答它。
- **管理层的回避措辞**。`we don't break that out` / `we're not going to comment on specific customers` / `I'd point you back to our prepared remarks` / 答非所问。**回避本身是信息**，尤其当被回避的正是重复出现的问题时。
- **第一个问题**。卖方的第一个问题通常是当晚最要紧的争议点。

### 6. 口径变化与新披露

管理层开始披露一个新指标（某业务线单独列示、给出 backlog、给出某产品营收占比），或**停止披露**过去披露过的东西——两者都是信号。新披露通常意味着这块要开始好看了；停止披露通常相反。

---

## 情绪分怎么用（只有 Alpha Vantage 路径有）

`segment.sentiment` 是 Alpha Vantage 自己的模型对该段的打分，**不是公司披露、也不是本 skill 的判断**。

- 可以用它**定位**：整场里最负面的几段往往就是问答里的硬骨头，值得先读。
- **不要**把它写进报告当结论（"管理层情绪偏正面"）。它是一个第三方模型的输出，来源必须标明，而且 Motley Fool 路径根本没有这一列，跨公司比较会因为源不同而不可比。

---

## 取不到的时候

`transcript.status` 不是 `ok` 时，两个免费源都还没发布这场会议。财报当晚到次日，这是常态。

**此时唯一正确的做法是把它写成待补**，例如"电话会议尚未公开，指引判断仅基于新闻稿摘出的区间"。

绝对不要：

- 凭数字推测管理层说了什么；
- 引用其他公司的会议内容来填补；
- 用 Motley Fool 页面上的 TAKEAWAYS / RISKS / SUMMARY 冒充会议内容——那是网站自己写的编辑摘要，`publisher_notes_warning` 已经在数据里标明了，它不是任何人在会上说过的话。

`verdict.py record` 会拒绝 `transcript_read: true` 而缓存里没有会议记录的判分，这条硬约束就是为了防止上面这些。

---

## 跨公司检索：这个 skill 真正的杠杆

会议按发言人逐段落库,所以"这季谁提到了 X"是一条 SQL 而不是三十份会议记录：

```bash
python3 scripts/read_source.py search "HBM" --frame CY2026Q2
python3 scripts/read_source.py search "CoWoS" --frame CY2026Q2 --section qa
python3 scripts/read_source.py search "tariff" --frame CY2026Q2
```

输出会告诉你**哪些公司提到、在预备发言还是问答里提到**。这个区分很重要：

- 在**预备发言**里主动提 = 公司认为这是卖点或必须先讲清的风险；
- 只在**问答**里被问出来 = 公司本来不想讲。

同一个主题，一家主动讲、一家被问才讲、一家完全没提，这三者的差别往往比任何单一数字都更能说明各自的处境。
