---
name: md-pushplus
description: >-
  当用户要把一个 Markdown 文件（如日报、研报、复盘、周报、笔记、待办、生成好的报告）渲染成 HTML 并通过 PushPlus 推送到微信/邮箱时使用。
  典型说法："把这份日报推送到我微信""用 pushplus 发一下这个 md""把 report.md 渲染成 HTML 发给我""推送到 pushplus""把刚生成的报告 push 出去""每天把宏观日报发到我手机"。
  也适用于：让任何上游 skill（ch-news-reporter / a-stock-* / usmarket-report 等）产出的 Markdown 报告，一键变成带样式的 HTML 推送通知。
  默认用仓库共享的 shared/html_report 主题模板渲染成 HTML 再推送，与各报告型 skill 的网页版同一套观感。
  本 skill 只做「Markdown→HTML 渲染 + PushPlus 发送」这一件事；不负责生成报告内容本身，也不做邮件 SMTP、Server酱、钉钉/飞书等其它推送渠道（仅 PushPlus）。
---

# md-pushplus

把指定的 Markdown 文件渲染成带样式的 HTML，再通过 [PushPlus](https://www.pushplus.plus/doc/) 推送到微信、邮箱等渠道。

## 目标

用户已经有（或刚生成）一份 Markdown，想把它「变好看 + 送到手机/邮箱」。本 skill 负责后半段——渲染与发送，是一个确定性的收尾动作，报告内容本身由用户或上游 skill 提供。

## 何时用 / 不用

- **用**：用户给出一个 `.md` 文件路径（或上一步刚写出的报告文件），要求推送、发送、push、通知到微信/邮箱，并提到 pushplus，或没指定渠道但想"发到手机"。
- **不用**：用户要的是生成报告内容（那是上游 skill 的事）；或明确要走邮件 SMTP、Server酱、钉钉、飞书等非 PushPlus 渠道（本 skill 只接 PushPlus）。

## 默认行为（不要问，直接这么做）

1. **推送格式默认 HTML**：PushPlus 的 `template` 固定 `html`，正文一律先渲染成 HTML 再发，不发裸 Markdown、不发纯文本。
2. **渲染默认用共享主题模板**：走 `shared/html_report` 的主题（默认 `default` 主题，即 AlphaVault 站点风格），跟 a-stock-* / usmarket / ch-news-reporter 出的网页版报告同一套观感与排版引擎。**除非用户点名要别的主题或要极简体积，否则不要切换。**

用户没提要求时，这两条就是默认值，`python scripts/md_to_pushplus.py 报告.md` 一条命令即可，无需额外参数。

## 前置：token

PushPlus 的 token 从环境变量 `PUSHPLUS_TOKEN` 读取（在 [pushplus.plus](https://www.pushplus.plus/) 登录后于「一对一推送」页面获取）。也可用 `--token` 显式传入。若两者都没有，发送会失败并提示——这时向用户要 token，不要瞎编。

## 工作流程

1. **确认输入**：拿到要推送的 Markdown 文件路径。如果是上一步刚生成的报告，直接用那个路径。
2. **想好标题**：标题是用户在微信/邮件里第一眼看到的东西。
   - 默认会取 Markdown 里的第一个 `# 一级标题`；没有就用文件名。
   - 如果默认标题不够清楚（比如就是个日期），**主动替用户拟一个有信息量的标题**（如「6/24 宏观日报：美债走高、黄金回落」），用 `--title` 传入。这是模型该做的判断，别留给脚本。
3. **先 dry-run 自检**（推荐）：加 `--dry-run --save-html /tmp/preview.html` 先渲染不发送，确认 HTML 字符数没超 PushPlus 上限（约 4 万字符），必要时可打开预览。主题 CSS 本身约占 1.3 万字符，长报告更容易顶到上限——超了看下面「常见失败」。
4. **发送**：去掉 `--dry-run` 正式推送。脚本返回 PushPlus 的 JSON，`code==200` 即成功。
5. **如实回报**：把发送结果（成功 / 失败原因）告诉用户，并说明用的是哪套渲染（主题名或 inline 降级）。失败时按「常见失败」排查，别假装成功。

## 命令

```bash
# 最简：默认 html 模板 + 共享 default 主题，标题自动从 # 标题或文件名推断
python scripts/md_to_pushplus.py 报告.md

# 显式标题（推荐——一个好标题比正文更影响打开率）
python scripts/md_to_pushplus.py 报告.md --title "6/24 宏观日报：美债走高、黄金回落"

# 换共享主题：claude（暖色衬线）/ print（黑白衬线，适合转 PDF）
python scripts/md_to_pushplus.py 报告.md --theme claude

# 先自检不发送，并保存一份 HTML 预览（存的就是要推的那份，所见即所推）
python scripts/md_to_pushplus.py 报告.md --dry-run --save-html /tmp/preview.html

# 降级成纯内联样式（体积小、兼容老邮件客户端）
python scripts/md_to_pushplus.py 报告.md --renderer inline

# 群组推送（一对多）/ 指定渠道（邮件）
python scripts/md_to_pushplus.py 报告.md --topic 群组code
python scripts/md_to_pushplus.py 报告.md --channel mail
```

参数：`--token`（默认读 `$PUSHPLUS_TOKEN`）、`--template`（PushPlus 模板，默认 `html`）、`--renderer`（`theme` 默认 / `inline` 降级）、`--theme`（`default` 默认 / `claude` / `print`）、`--topic`（群组 code，一对多）、`--channel`（`wechat`|`mail`|`webhook`|`cp`|`sms`）、`--save-html`、`--dry-run`。

## 渲染说明

**默认路径（`--renderer theme`）**：调用仓库共享的 `shared/html_report`——同一套 Markdown 引擎（标题、嵌套列表、引用、表格、代码块、行内样式、`==高亮==`）、同一套主题 CSS，产出自包含单页 HTML 整体作为 PushPlus 的 `content` 发出。好处是主题只维护一份：改 `shared/html_report/themes/*.css`，推送和各 skill 的网页版一起变。

几个已知取舍，心里有数即可：

- **体积**：主题 CSS 约 1.3 万字符会计入 PushPlus 约 4 万字符的 content 上限。脚本推送前会自动剥掉 CSS 注释与缩进（只删空白，不动选择器与声明），但正文余量仍然只有约 2.5 万字符。
- **装饰 JS 可能不跑**：页面自带的轻量装饰脚本（表格数字红绿染色、h2 轮色、隐藏独立 `---`）在 PushPlus 以 innerHTML 注入正文时不会执行——排版、表格、卡片、配色全都正常，只是少了这几处点缀。这是渲染侧无法左右的，不必为此改报告。
- **外链字体已关**：推送包不引 Google Fonts，落回系统字体，保证离线与弱网下也能正常显示。

**降级路径（`--renderer inline`）**：脚本内置的纯标准库渲染，样式全部内联到元素 `style=""` 上，体积只有几 KB。什么时候用它——用户点名要、报告长到主题版超限、或目标是对 `<style>` 支持差的老邮件客户端（如 Outlook 桌面版，不认 CSS 变量）。共享包导入不到时脚本也会自动落到这条路，并在 stderr 说明原因；这种情况要如实告诉用户，别当成主题版推送成功。

输入若是 Obsidian / Jekyll 笔记，开头的 YAML frontmatter（`--- ... ---` 元数据块）会被自动剥离，不会渲染进正文；若正文没有 `# 一级标题`，会用 frontmatter 里的 `title:` 兜底作推送标题。

## 常见失败

- `no token`：没设 `PUSHPLUS_TOKEN` 也没传 `--token` → 向用户要 token。
- PushPlus 返回 `code != 200`：常见是 token 失效、当天免费额度用尽、或 `content` 超长。把 `msg` 原文转告用户。
- 内容超长（约 4 万字符）：脚本会先 WARNING。按这个顺序处理——先试 `--renderer inline`（省掉约 1.3 万字符的主题 CSS），仍超就把长报告拆成几条分别推，或先精简正文。
- `shared/html_report unavailable`：共享包没同步进来（`scripts/_shared/html_report` 缺失，且不在仓库开发目录下）。跑一次 `python scripts/skill_sync.py` 补齐；在此之前脚本会用 inline 兜底，推送不会中断。

## 边界

只做渲染 + PushPlus 发送，不生成报告内容，不接入 PushPlus 以外的推送服务。报告内容的对错由上游负责，本 skill 不审校正文。
