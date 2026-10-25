# 信源抓取细节与运维手册

调信源、排障、加新源时读这里。检索词、账号名单、关键词都在 `config/sources.yaml`，改配置不改脚本。

## 总体约定

- 所有 fetch 脚本：argparse + `--days/--limit/--timeout/--output`；JSON 到 stdout（`--output` 则写文件，stdout 只报 `{"written", "items"}`）。
- 整跑失败返回 `{"error": {"code", "detail"}}` 并以非 0 退出；**部分失败不失败**——失败子项进 `degraded` 数组，其余数据照常返回。
- 常见错误码：`MISSING_CONFIG`（配置缺失，exit 2）、`MISSING_DEPENDENCY`（pip 依赖缺失，exit 2）、`NO_SOURCES`（配置为空，exit 2）、`ALL_FEEDS_FAILED` / `GITHUB_API_FAILED` / `BILIBILI_FETCH_FAILED` / `ALL_ENGINES_FAILED` / `CONTENT_FETCH_FAILED`（取数全败，exit 1）。
- 凭据只走环境变量：`GITHUB_TOKEN`（可选）、`TENCENTCLOUD_SECRET_ID/KEY`（元宝）、`DEEPSEEK_API_KEY`、`TAVILY_API_KEY`、`BILIBILI_SESSDATA`（可选）。

## 各信源机制

### 1. OpenAI 官方博客（fetch_official_blogs.py）

- feedparser 解析 RSS（`https://openai.com/news/rss.xml`），请求走 `http_get_text`（自定义 UA + 代理继承），不用 feedparser 自带网络栈。
- 无发布时间的条目丢弃；空 feed 记 `feed_empty` 进 degraded。
- **文章页直连 403**（Cloudflare 指纹拦截 Python），所以"精读"OpenAI 文章一律走 `fetch_article_text.py`（内置 r.jina.ai 阅读器降级，见下）。
- 加源：在 `official_blogs` 下加 `{name, url, enabled}`。新源须先验证 1) RSS 可抓 2) 每条带发布时间，否则条目会被静默丢弃。

### 2. OpenAI Cookbook（fetch_openai_cookbook.py）

- 主信号是**窗口期内合并的 PR**（`/repos/openai/openai-cookbook/pulls?state=closed&sort=updated`，本地过滤 `merged_at`）；commits 作补充（`/commits?since=`，配置可开 `path=examples` 过滤）。
- PR body 截 400 字符，供模型判断教程内容；完整 body 需要时用 PR 页链接。
- 无 `GITHUB_TOKEN` 时 api.github.com 限 60 req/h，脚本按 1.1s/请求节流，本脚本只用 2 个请求，配额充足。403/429 进 degraded。
- 加源：换 `openai_cookbook.repo` 即可跟踪其他官方教程仓库（如 Anthropic 的 cookbook），无需改代码。

### 3. GitHub 本周最火（fetch_github_trending.py）

- 三条路：① 爬 `github.com/trending?since=weekly`（HTML 解析 `article.Box-row`）；② Search API `created:>=窗口起点` 按 stars 排（查询词在 `new_repo_queries`）；③ 对榜单前 N（`enrich_top`）调 `/repos/{name}` 补 topics/license/created_at。
- `ai_eng_hint` 是关键词预筛（词表 `hint_keywords`），只标 true/false **不删条目**——最终筛选是模型的活。hint 匹配规则：≤3 字符的词按整词匹配（`ai` 不会误中 `mail`）。
- trending 页面的 `stars_in_period` 是"本周新增 stars"文本，原样返回；enrich 失败的仓库缺 topics 等字段，不阻塞。
- 反爬注意：github.com 网页爬取对 UA 敏感，脚本用浏览器 UA；同一 IP 高频爬 trending 可能 403，默认每次运行只爬 1 页。

### 4. B站视频（fetch_bilibili.py）

- 主路径：官方搜索 `api.bilibili.com/x/web-interface/search/type`（search_type=video）。需要两件事才能不被 -412 拦：
  1. **buvid3/buvid4 cookie**：启动时调 `x/frontend/finger/spi` 获取并种到 session（失败则回退访问主页种 cookie）；
  2. **wbi 签名**：从 `x/web-interface/nav` 取 img/sub key，按公开混淆表（MIXIN_KEY_ENC_TAB）拼 mixin key，对 query 参数做 `w_rid` md5 签名。keys 每日轮换，每次运行现取。
- 请求头必须带 `Referer: https://www.bilibili.com/`；页间 1.2–2.2s 随机抖动。`order=pubdate` 取最新，本地按窗口期过滤后按播放量排。
- 降级链：官方 API 全失败（或窗口期 0 条）→ multi_search，site 限定 bilibili.com，引擎序 `fallback_engine`（默认 yuanbao,tavily,deepseek），最多取前 `fallback_keywords` 个关键词。`--no-fallback` 可禁用降级只看官方路径。
- 风控严重时可设 `BILIBILI_SESSDATA`（登录态 cookie）提高通过率。
- 标题里的 `<em class="keyword">` 高亮标签已剥离；`play` 为 `"--"` 时归一为 null。

### 5. 大厂技术公众号（fetch_wechat_articles.py）

- **search 模式**：`账号名 × 检索词` 走 multi_search，site 限定 mp.weixin.qq.com。公众号无官方 API，按账号名检索是唯一可落地方案。
- **归属必须核实**：检索引擎对"账号名"只是关键词匹配，返回的文章可能来自其他号（实测搜"腾讯技术工程"会混入机器之心对同一事件的报道）。**真实归属以 content 模式抓回的 `account` 字段（页面 js_name）为准**；对要写进报告的条目，建议抽查 content 确认归属后再署账号名。
- **content 模式**：抓正文，`div#js_content` 优先、`og:description` 兜底；同时回传真实 title/account/publish_time。命中"环境异常/完成验证"验证墙时标 `verify_wall`，正文不可用。
- 引擎选型：元宝（腾讯生态，对公众号收录最好）> tavily（Google 收录的 mp.weixin 页面，有 TAVILY_API_KEY 即可用）> deepseek。全缺凭据时报 `ALL_ENGINES_FAILED`，明细在 detail 里。
- 加账号：`wechat.accounts` 下加 `{name, queries}`。加检索词同理。

### 6. Hacker News（fetch_hackernews.py）

- Algolia API `search_by_date`，`numericFilters=points>N,created_at_i>窗口起点`，免费无凭据。
- 关键词过滤在本地做（词表 `hackernews.keywords`），标题+URL 都参与匹配；≤3 字符词按整词匹配。
- `min_points` 默认 50；噪音多就调高（100），窗口长就调低。

### 7. 通用正文抓取（fetch_article_text.py）

- 直连抓取：og:title/<title> 取标题，article:published_time 取时间，`<article>`/`<main>` 内 `<p>` 拼正文。
- **直连 403（Cloudflare 等）自动降级 r.jina.ai 阅读器**（无需 key，限速宽松）。openai.com、多数 Cloudflare 站点靠这条路通。
- 正文 < 200 字符视为失败（多半是 JS 渲染页），标 note；单条失败不阻塞其他 URL。
- 公众号文章不要用这个脚本（它不认微信页面结构），用 fetch_wechat_articles.py content。

## 报告渲染（render_report_html.py）

- 薄清单封装 shared/html_report：Markdown → 自包含单页 HTML。主题 default / claude / print。
- `--strict` 才让校验失败硬报错；日常用默认 warn 策略。
- 周报无图表需求，未接 ChartHook；要加图表时参考 shared/html_report/README.md 的 hook 协议。

## 加新信源的判断顺序

1. **有 RSS/API** → 现有脚本加配置（official_blogs / cookbook repo / bilibili keyword / HN keyword 都支持纯配置扩展）。
2. **要靠搜索引擎**（无 RSS、无官方 API，如微信公众号、Anthropic 博客）→ 说明该源只能"按关键词/站点检索"，收录及时性受索引延迟影响，要在报告里注明。
3. **要全新抓取逻辑**（如新平台反爬复杂）→ 新建 fetch_*.py，遵守 _lib 约定（JSON 契约、degraded 数组、结构化错误码），并在本文件补机制说明。
