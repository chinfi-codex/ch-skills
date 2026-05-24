# SubAgent / 工作单元使用边界

本文档把 AlphaVault `20-查询协议.md` § SubAgent 使用边界 + § Raw Sources 回溯条件落到本 skill 的工作流。

## 硬约束

**主 Agent 不读 Raw 原文。**

主 Agent 只能读：

- `wiki/index.md` 的定向命中片段；
- 候选页面 frontmatter、`## 核心数据 / 核心事实`、`## 可产出结论`、`## 未知 / 后续跟踪`、`## 更新历史`；
- `query_alphavault_raw.py` 输出的来源元数据（来源 ID、文件路径、机构、权重、摄取日期、更新页面）；
- 本 skill `build_attribution_pack.py` 编排器返回的 Evidence Pack JSON。

主 Agent **不读**：

- `RAW/clippings/*` 原文正文；
- `RAW/crawlers/*` 抓取产物（PDF、长 HTML、长 JSON）；
- 公告 PDF 全文；
- 长机构研报、长电话会纪要、长财报；
- 互动易问答原始 HTML（脚本输出的结构化 JSON 可以读）。

## 何时必须拆 SubAgent / 工作单元

只要满足任一条件：

1. 需要读取任一 Raw Source 原文正文或片段；
2. 候选页面超过 5 个，且每个都需提取证据点；
3. 单篇 Wiki 页面或 Raw Source 正文超过约 8,000 字；
4. 库内 Raw / Pack 数量 ≥ 3 且需要并行回溯交叉验证；
5. 比较多家公司、多个产业链或多个来源体系；
6. 公告窗口扫描返回 > 10 条公告，且每条都需要核对正文。

## 工作单元职责

每个 SubAgent / 查询工作单元负责一个原子任务，返回的是 **Evidence Pack 片段**而不是 Raw 原文：

| 工作单元类型 | 输入 | 输出 |
|---|---|---|
| `announcement_unit` | 1 条公告 PDF url + 待查证的催化候选 | Evidence Pack 片段（claim、source_path、raw_excerpt_or_summary ≤ 200 字、conflict、gap） |
| `report_unit` | 1 份机构研报 PDF | 同上，附 source_org 与 source_weight |
| `qa_unit` | 1 批互动问答 JSON（已结构化） | 主题汇总 + 明确承认/模糊表述/否认 分类 |
| `telegraph_unit` | 1 批 T-1 ~ T 电报 JSON | 命中关键词的电报列表 + 简要解读 |
| `web_unit` | 1 次 WebSearch（A3 行业边际变化 / A4 主题概念炒作 / B1 LV1 资金流 / B2 板块异动） | 命中链接 + 短摘录 + 信源权重 |

工作单元**不得**：

- 直接写 Wiki；
- 给出最终归因分档（主因/次因 留给主 Agent）；
- 把整篇 Raw 正文塞回主 Agent。

## 退化执行（环境不支持 SubAgent）

当运行环境不支持 SubAgent 时，主 Agent 按工作单元顺序**串行退化**执行：

1. 一次只读一个 Raw 原文，立即抽取 Evidence Pack 片段；
2. Pack 片段写入 outputs 目录（如 `outputs/units/<source_id>.json`）；
3. 立即丢弃 Raw 原文上下文，进入下一条；
4. 最后只把 Pack 片段汇总到主 Pack，**不**回灌任何 Raw 正文。

退化执行下仍必须保持 Pack 是唯一交付物。如果主 Agent 上下文已被 Raw 原文污染，必须重启该轮工作。

## 与 build_attribution_pack.py 的关系

`build_attribution_pack.py` 是**编排器**，不是 SubAgent：

- 它只调底层脚本，把 JSON 结果拼到一起；
- 它**不**读 PDF 正文；
- 公告/互动问答/电报脚本返回的是结构化 JSON（已经是"Pack 等价物"），不算 Raw 原文，主 Agent 可直接读；
- 当 Pack 中的 `raw_excerpt_or_summary` 段需要从 PDF 抽取时，必须由 SubAgent / 工作单元执行，**不**由编排器执行。

## 自检

每次任务结束时主 Agent 自问：

1. 我读过任何 PDF 正文吗？读过 → 重启该轮；
2. 我把任何 Raw 长文本回贴到主对话里了吗？是 → 重启该轮；
3. 所有催化证据是否都能定位到 Pack 中的 `source_path` + `raw_excerpt_or_summary`？否 → 补工作单元；
4. Pack 的 `write_permission` 是否为 `false`？否 → 修正。
