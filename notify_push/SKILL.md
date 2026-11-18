---
name: notify_push
description: >-
  当用户要把一份 Markdown 文档（日报、研报、复盘、周报、笔记、生成好的报告）推送到手机/微信/飞书时使用，
  两种形态都覆盖：① 直接把原文渲染推送（"把这份日报推送到我微信""用 pushplus 发一下""渲染成 HTML 发给我"）；
  ② 先总结再推送（"看完这篇研报总结一下发我""把要点摘要推到我微信/飞书""总结后发到手机"，总结由模型完成，脚本只管投递）。
  渠道三选一或多选：pushplus（微信/邮箱，HTML 长文）、hermes（微信纯文本短消息，走本机 hermes CLI）、
  飞书（lark-cli，markdown，默认发给自己）——"发飞书""推到飞书群""用 hermes 发到微信"都会触发。
  上游 skill（ch-news-reporter / a-stock-* / usmarket-report 等）产出的报告也常经本 skill 一键送达。
  本 skill 只做「（可选）总结 + 渠道投递」；不生成报告内容本身，不做 SMTP/Server酱/钉钉 webhook（飞书与 hermes 分别走本机 lark-cli 与 hermes CLI）。
---

# notify_push

把指定的 Markdown 文档——原样，或先由你总结成摘要——按一个或多个渠道推送到用户手上。

## 目标

用户已经有（或刚生成）一份 Markdown，想要它「送到手机/微信/飞书」；有时还要你先读一遍、把要点提炼成摘要再发。本 skill 负责后半段：总结是模型的事（见下方方法论），渠道适配与投递是脚本的事（`scripts/notify_send.py`），两者分工明确，脚本不做任何内容判断。

## 何时用 / 不用

- **用**：用户给出一个 `.md` 文件路径（或上一步刚写出的报告），要求推送、发送、push、通知；或明确要"总结一下发我"；或点名渠道——pushplus/微信/邮箱、hermes/微信纯文本、飞书/feishu/lark。
- **不用**：用户要的是生成报告内容（上游 skill 的事）；或要走邮件 SMTP、Server酱、钉钉 webhook 等未接入的通道。

## 渠道速查

| 渠道 | 内容形态 | 前置条件 | 默认目标 | 篇幅预算 |
|---|---|---|---|---|
| pushplus | 自包含 HTML（主题渲染） | 环境变量 `PUSHPLUS_TOKEN` | 一对一推送 | ≤4 万 UTF-8 字节，可承载全文级长文 |
| hermes | 纯文本（自动去 markdown） | 本机 `hermes` CLI + `~/.hermes` 凭据 | `weixin` home 频道 | 建议 500–800 字，短行、无宽表格 |
| feishu | markdown（内部转 post） | 本机 `lark-cli` 已登录 | 发给自己（读 config 的 userOpenId） | 建议 ≤2000 字；发群需 bot 在群里 |

用户没点名渠道时，默认 pushplus（历史习惯）；点名"微信纯文本/hermes"走 hermes，点名"飞书/feishu"走 feishu。`--channel pushplus,feishu` 可一次多渠道，逐渠道回报，互不拖累。

## 总结方法论（模型做，脚本不做）

要求"总结/摘要/提炼后再发"时：

1. **先完整读输入文档再总结**，不凭文件名或开头猜内容。关键数字（涨跌幅、价格、日期、计数）保留原文，不编造，也不添加原文没有的判断。
2. **按渠道定篇幅**：pushplus 可以推接近全文的长文（仍在 4 万字节内）；feishu 的 markdown 摘要控制在 2000 字内；hermes 到微信是纯文本短消息，500–800 字、每行一句话、不写宽表格——脚本虽会自动去 markdown 符号，但摘要本身按纯文本习惯写，观感才好。
3. **结构**：一句话定性（这是什么、最要紧的结论）→ 3–5 条要点（每条完整一句话）→ 关键数据（表格或短列）→ 风险/行动项（如有）。摘要的价值在取舍，不在压缩比率。
4. **把摘要写成文件再推**（如 `<输入名>.summary.md`），把摘要文件路径交给脚本。不要让脚本替你决定推全文还是推摘要。

## 工作流程

1. **确认输入与渠道**：拿到要推的 Markdown 路径（或 stdin）；确认渠道与目标（hermes 换目标用 `--target`，飞书发群要 `--target oc_…`）。
2. **需要总结时按方法论产出摘要文件**；直接推原文则跳过。
3. **想好标题**：标题是用户在通知里第一眼看到的东西。默认取第一个 `# 一级标题`（frontmatter `title:` 兜底，再退文件名）；不够清楚就**主动拟一个有信息量的标题**（如「6/24 宏观日报：美债走高、黄金回落」）用 `--title` 传入。这是模型该做的判断，别留给脚本。
4. **先 dry-run 自检**（推荐）：`--dry-run` 不发送——pushplus 打印推送包字符数/UTF-8 字节数（看字节，中文一字符 3 字节，约 4 万上限），配 `--save-html /tmp/preview.html` 可按手机视口本地预览；hermes/feishu 打印将执行的命令与最终正文。多渠道 dry-run 能一次看到三份载荷。
5. **正式发送**：去掉 `--dry-run`。脚本对每个渠道打一行结果 JSON（`ok` 字段判成败），最后 stderr 汇总一行。
6. **如实回报**：逐渠道报成功/失败与原因（pushplus 的 `code!=200`、hermes 的 stderr、飞书的 `message_id`），失败按「常见失败」排查，别假装成功。

## 命令

```bash
cd <skill>/scripts

# pushplus（默认渠道）：主题渲染 + 推送，标题自动从 # H1 推断
python3 notify_send.py 报告.md
python3 notify_send.py 报告.md --title "6/24 宏观日报：美债走高、黄金回落"

# 总结后推微信纯文本（摘要由模型写好落盘）
python3 notify_send.py 报告.summary.md --channel hermes

# 推飞书：默认发给自己；发群/发别人显式给 --target
python3 notify_send.py 摘要.md --channel feishu
python3 notify_send.py 摘要.md --channel feishu --target oc_xxx   # 指定群

# 一次推多渠道；dry-run 全链自检；pushplus 本地预览
python3 notify_send.py 摘要.md --channel pushplus,hermes,feishu --dry-run
python3 notify_send.py 报告.md --dry-run --save-html /tmp/preview.html

# 群组推送（pushplus 一对多）/ pushplus 走邮件
python3 notify_send.py 报告.md --topic 群组code
python3 notify_send.py 报告.md --pushplus-channel mail
```

参数：`--channel`（`pushplus|hermes|feishu`，逗号分隔或重复传参，默认 pushplus）、`--title`、`--target`（hermes 目标，默认 `weixin`；飞书 `oc_…`/`ou_…`，默认发给自己）、`--token`（默认 `$PUSHPLUS_TOKEN`）、`--theme`（`default|claude|print`）、`--renderer`（`theme` 默认 / `inline` 降级）、`--topic`、`--pushplus-channel`（`wechat|mail|webhook|cp|sms`）、`--save-html`、`--idempotency-key`（飞书，默认由标题+正文 hash 派生）、`--dry-run`。输入支持 `-`（stdin）。

## 渲染说明（pushplus）

推送的是**自包含 HTML 片段**（带作用域的 `<div id="pp-doc">`），不是整页——PushPlus 以 innerHTML 注入详情页且不执行 `<script>`，整页 CSS 会污染宿主页面，doctype/head 会成垃圾节点（三条都是实测结论，细节在 `push_render.py` 模块 docstring）。装饰走 `shared/html_report/static_decorations.py` 构建期静态投影，CSS 按片段实际 class 摇树（1.3 万→5–6 千字符），窄屏放开表格 `min-width/nowrap` 并加 `word-break:keep-all`。`--renderer inline` 是降级路径（老邮件客户端点名要、或共享包缺失时自动回落，stderr 会说明）；注意 inline 不是省体积的选项，正文超过约 10KB 后反而更大。Obsidian/Jekyll frontmatter 自动剥离，`title:` 兜底标题。改动渲染路径后跑 `scripts/test_push_render.py`。

## 常见失败

- pushplus `no token`：没设 `PUSHPLUS_TOKEN` 也没传 `--token` → 向用户要 token。
- pushplus `code != 200`：常见是 token 失效、当日免费额度用尽、content 超长（约 4 万 UTF-8 字节，脚本会先 WARNING）→ 把 `msg` 原文转告；超长的正路是拆条推送或先精简正文，别指望换 inline 救。
- pushplus `shared/html_report unavailable`：共享包没同步进来 → 跑 `python scripts/skill_sync.py`；此前脚本用 inline 兜底，推送不中断，但要如实告知用户。
- hermes：CLI 找不到（`$HERMES_BIN`/PATH/`~/.local/bin/hermes` 都没有）→ 报告安装缺失；weixin 未扫码配对或网关异常 → `hermes send` 返回非零退出码，把 stderr 转告；目标写法用 `hermes send --list` 查。
- feishu：`lark-cli` 不在 PATH 或未登录（`~/.lark-cli/config.json` 无登录用户）→ 提示 `lark-cli auth login`；bot 发群消息要求 bot 已在群里，权限不足时错误里带 `console_url`；默认幂等键意味着同一内容 1 小时内重推不会重发——这是预期行为，不是丢消息。

## 边界

只做（可选的模型总结）+ 渠道投递，不生成报告内容，不接入以上三渠道之外的推送服务。报告内容的对错由上游负责，本 skill 不审校正文。主动提醒链（ch-proactive-agent）有自己的投递通道，不经本 skill，避免双通道重复推送。
