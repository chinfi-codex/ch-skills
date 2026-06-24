---
name: md-pushplus
description: >-
  当用户要把一个 Markdown 文件（如日报、研报、复盘、周报、笔记、待办、生成好的报告）渲染成 HTML 并通过 PushPlus 推送到微信/邮箱时使用。
  典型说法："把这份日报推送到我微信""用 pushplus 发一下这个 md""把 report.md 渲染成 HTML 发给我""推送到 pushplus""把刚生成的报告 push 出去""每天把宏观日报发到我手机"。
  也适用于：让任何上游 skill（ch-news-reporter / a-stock-* / usmarket-report 等）产出的 Markdown 报告，一键变成带样式的 HTML 推送通知。
  本 skill 只做「Markdown→HTML 渲染 + PushPlus 发送」这一件事；不负责生成报告内容本身，也不做邮件 SMTP、Server酱、钉钉/飞书等其它推送渠道（仅 PushPlus）。
---

# md-pushplus

把指定的 Markdown 文件渲染成带样式的 HTML，再通过 [PushPlus](https://www.pushplus.plus/doc/) 推送到微信、邮箱等渠道。

## 目标

用户已经有（或刚生成）一份 Markdown，想把它「变好看 + 送到手机/邮箱」。本 skill 负责后半段——渲染与发送，是一个确定性的收尾动作，报告内容本身由用户或上游 skill 提供。

## 何时用 / 不用

- **用**：用户给出一个 `.md` 文件路径（或上一步刚写出的报告文件），要求推送、发送、push、通知到微信/邮箱，并提到 pushplus，或没指定渠道但想"发到手机"。
- **不用**：用户要的是生成报告内容（那是上游 skill 的事）；或明确要走邮件 SMTP、Server酱、钉钉、飞书等非 PushPlus 渠道（本 skill 只接 PushPlus）。

## 前置：token

PushPlus 的 token 从环境变量 `PUSHPLUS_TOKEN` 读取（在 [pushplus.plus](https://www.pushplus.plus/) 登录后于「一对一推送」页面获取）。也可用 `--token` 显式传入。若两者都没有，发送会失败并提示——这时向用户要 token，不要瞎编。

## 工作流程

1. **确认输入**：拿到要推送的 Markdown 文件路径。如果是上一步刚生成的报告，直接用那个路径。
2. **想好标题**：标题是用户在微信/邮件里第一眼看到的东西。
   - 默认会取 Markdown 里的第一个 `# 一级标题`；没有就用文件名。
   - 如果默认标题不够清楚（比如就是个日期），**主动替用户拟一个有信息量的标题**（如「6/24 宏观日报：美债走高、黄金回落」），用 `--title` 传入。这是模型该做的判断，别留给脚本。
3. **先 dry-run 自检**（推荐）：加 `--dry-run --save-html /tmp/preview.html` 先渲染不发送，确认 HTML 字符数没超 PushPlus 上限（约 4 万字符），必要时可打开预览。
4. **发送**：去掉 `--dry-run` 正式推送。脚本返回 PushPlus 的 JSON，`code==200` 即成功。
5. **如实回报**：把发送结果（成功 / 失败原因）告诉用户。失败时按下面「常见失败」排查，别假装成功。

## 命令

```bash
# 最简：标题自动从 # 标题或文件名推断
python scripts/md_to_pushplus.py 报告.md

# 显式标题（推荐——一个好标题比正文更影响打开率）
python scripts/md_to_pushplus.py 报告.md --title "6/24 宏观日报：美债走高、黄金回落"

# 先自检不发送，并保存一份 HTML 预览
python scripts/md_to_pushplus.py 报告.md --dry-run --save-html /tmp/preview.html

# 群组推送（一对多）/ 指定渠道（邮件）
python scripts/md_to_pushplus.py 报告.md --topic 群组code
python scripts/md_to_pushplus.py 报告.md --channel mail
```

参数：`--token`（默认读 `$PUSHPLUS_TOKEN`）、`--template`（默认 `html`）、`--topic`（群组 code，一对多）、`--channel`（`wechat`|`mail`|`webhook`|`cp`|`sms`）、`--save-html`、`--dry-run`。

## 渲染说明

脚本内置纯标准库的 Markdown→HTML 渲染（标题、段落、有序/无序列表、引用、表格、代码块、行内 `code`/**粗体**/*斜体*/链接），并套一层有节制的内联样式。

输入若是 Obsidian / Jekyll 笔记，开头的 YAML frontmatter（`--- ... ---` 元数据块）会被自动剥离，不会渲染进正文；若正文没有 `# 一级标题`，会用 frontmatter 里的 `title:` 兜底作推送标题。

为什么用内联样式：PushPlus 的微信渠道会过滤掉 `<style>` 块和外链 CSS，只有写在元素 `style=""` 上的样式才可能保留。所以渲染刻意把样式内联，**在微信里表格/标题会降级但结构依然可读，在网页/邮件渠道里则完整呈现**。无需引入第三方 Markdown 库或图表框架。

## 常见失败

- `no token`：没设 `PUSHPLUS_TOKEN` 也没传 `--token` → 向用户要 token。
- PushPlus 返回 `code != 200`：常见是 token 失效、当天免费额度用尽、或 `content` 超长。把 `msg` 原文转告用户。
- 内容超长（约 4 万字符）：脚本会先 WARNING。建议把长报告拆成几条分别推，或先精简。

## 边界

只做渲染 + PushPlus 发送，不生成报告内容，不接入 PushPlus 以外的推送服务。报告内容的对错由上游负责，本 skill 不审校正文。
