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
2. **渲染默认用共享主题模板**：走 `shared/html_report` 的 Markdown 引擎与主题 CSS（默认 `default` 主题，即 AlphaVault 站点风格），跟 a-stock-* / usmarket / ch-news-reporter 出的网页版报告同一套观感。**除非用户点名要别的主题或要极简体积，否则不要切换。**

用户没提要求时，这两条就是默认值，`python scripts/md_to_pushplus.py 报告.md` 一条命令即可，无需额外参数。

## 前置：token

PushPlus 的 token 从环境变量 `PUSHPLUS_TOKEN` 读取（在 [pushplus.plus](https://www.pushplus.plus/) 登录后于「一对一推送」页面获取）。也可用 `--token` 显式传入。若两者都没有，发送会失败并提示——这时向用户要 token，不要瞎编。

## 工作流程

1. **确认输入**：拿到要推送的 Markdown 文件路径。如果是上一步刚生成的报告，直接用那个路径。
2. **想好标题**：标题是用户在微信/邮件里第一眼看到的东西。
   - 默认会取 Markdown 里的第一个 `# 一级标题`；没有就用文件名。
   - 如果默认标题不够清楚（比如就是个日期），**主动替用户拟一个有信息量的标题**（如「6/24 宏观日报：美债走高、黄金回落」），用 `--title` 传入。这是模型该做的判断，别留给脚本。
3. **先 dry-run 自检**（推荐）：加 `--dry-run --save-html /tmp/preview.html` 先渲染不发送，脚本会打印推送包大小并拆成 `css + body` 两块。主题 CSS 摇树后约 5–6 千字符，典型日报整包 8–17K，离 PushPlus 约 4 万字符的上限还很宽；顶到上限看下面「常见失败」。预览文件按手机视口打开看最准——推送主要在微信里读。
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

**默认路径（`--renderer theme`）**：用 `shared/html_report` 的 Markdown 引擎和主题 CSS，但**推出去的是一个自包含的 HTML 片段**，不是整页文档——一个带作用域的 `<div id="pp">`，里面是内联的主题 CSS 加报告正文。

为什么是片段不是整页，三条都是在真实推送页面（pushplus.plus/shortMessage/…）上实测出来的：

- **PushPlus 把 content 以 innerHTML 注入自己的详情页，页面里的 `<script>` 一律不执行**。所以 shared 那套装饰脚本（表格数字红绿、h2 轮色、隐藏独立 `---`）全都失效，正文里会裸露 `---`。现在这些装饰改在 Python 侧构建期做完，静态写进 HTML，不再依赖 JS。
- **整页的主题 CSS 会漫出去改掉 PushPlus 自己的页面**：`*`、`html`、`body` 那几条规则实测把宿主的字体、底色、间距一起换了。现在所有选择器都加了 `#pp` 前缀，`:root` / `html` / `body` 收敛到容器本身，宿主一个属性都不受影响。
- **`<!doctype>` / `<head>` / `<title>` / `<meta>` 会被当正文解析成垃圾节点**，而 `.page` 的 `calc(100vw - 40px)` 算的是视口宽不是容器宽，在窄容器里会溢出。片段没有这些标签，宽度一律按 100% 走。

另外两处是专门为手机读做的（推送基本都在微信里看）：

- 主题给桌面报告的表格设了 `min-width:520px` + 单元格 `nowrap`，结果 375px 的手机上**连两三列的小表都被迫横滑**。窄屏下这两条被放开，同时给单元格加 `word-break:keep-all`——只放开 `nowrap` 的话中文短词会被逐字拆成一列一个字（实测同一张表从 468px 高涨到 891px）。现在窄表能收进屏幕，宽表保持一行一条、由 `.table-wrap` 横滑兜底。
- CSS 按片段里实际出现的 class 摇树——图表、时间轴、hero 卡、折叠更新那些规则在纯 Markdown 推送里用不到，直接不进包。主题 CSS 从约 1.3 万字符降到 5–6 千。

`--save-html` 存的预览文件比推送包多一层 `doctype + viewport` 外壳，正文与样式逐字节相同，就是为了能在本地按手机视口看到微信里的样子。

**降级路径（`--renderer inline`）**：脚本内置的纯标准库渲染，样式全部内联到元素 `style=""` 上。什么时候用它——用户点名要、或目标是对 `<style>` 支持差的老邮件客户端（如 Outlook 桌面版，不认 CSS 变量）。注意它**不是"更省字符"的选项**：inline 给每个元素都挂 style，正文越长越亏，实测 md 超过约 10KB 后整包就比主题版更大了。共享包导入不到时脚本也会自动落到这条路，并在 stderr 说明原因；这种情况要如实告诉用户，别当成主题版推送成功。

输入若是 Obsidian / Jekyll 笔记，开头的 YAML frontmatter（`--- ... ---` 元数据块）会被自动剥离，不会渲染进正文；若正文没有 `# 一级标题`，会用 frontmatter 里的 `title:` 兜底作推送标题。

改动这条渲染路径后跑一遍 `scripts/test_push_render.py`（静态装饰、CSS 作用域化、摇树、片段装配的断言都在里面）。

## 常见失败

- `no token`：没设 `PUSHPLUS_TOKEN` 也没传 `--token` → 向用户要 token。
- PushPlus 返回 `code != 200`：常见是 token 失效、当天免费额度用尽、或 `content` 超长。把 `msg` 原文转告用户。
- 内容超长（约 4 万字符）：脚本会先 WARNING。**别指望换 `--renderer inline` 能救**——主题版的 CSS 只占 5–6 千字符，长报告的体积几乎全在正文，inline 反而更大。正路是把长报告拆成几条分别推，或先精简正文。
- `shared/html_report unavailable`：共享包没同步进来（`scripts/_shared/html_report` 缺失，且不在仓库开发目录下）。跑一次 `python scripts/skill_sync.py` 补齐；在此之前脚本会用 inline 兜底，推送不会中断。

## 边界

只做渲染 + PushPlus 发送，不生成报告内容，不接入 PushPlus 以外的推送服务。报告内容的对错由上游负责，本 skill 不审校正文。
