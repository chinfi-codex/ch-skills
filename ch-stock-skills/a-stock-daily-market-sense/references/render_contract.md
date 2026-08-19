---
name: a-stock-daily-market-sense-render-contract
description: 仅供 a-stock-daily-market-sense 内部按需读取。说明日报 HTML 的章节契约、渲染清单、运行期签到与三阶段上线门禁怎么用、失败了怎么查。
---

# 渲染契约与上线门禁

## 为什么有这套东西

日报的图表不在 HTML 文件里，是页面加载后自己用 JS 画出来的。这带来一个后果：**静态看文件完全看不出图表画到哪儿了、有没有画。**

2026-08-03 就踩了这个坑。趋势图面板当时靠标题文字定位，判据写死成「标题里同时含 `1.1` 和 `情绪趋势`」。新增「1.1 市场状态定位」之后，情绪趋势顺延成 1.2，判据不再命中，而代码的兜底是「找不到就 `appendChild` 到 `report-body` 末尾」——五张趋势图整块挪到了文末。没有异常、没有空容器、控制台干净，只能靠人眼发现。

这条教训在 2026-08-14 又验了一次，方向相反：移除 1.1、并把原模块 2 整章拿掉之后，情绪趋势退回 1.1、赚钱效应从第 3 章变成第 2 章，整份报告的编号全体平移。因为定位早就改成了语义键，这次没有任何图表跟着跑掉。

所以现在三件事同时成立：章节按语义键定位而不是按编号，图表画完必须主动签到，上线前用真浏览器验一遍。

## 章节契约

`scripts/render_report_html.py` 顶部的 `DMS_CONTRACT` 是唯一允许依赖标题文字的地方。它把标题解析成稳定语义键：

- 匹配前先剥掉 `1.2`、`3.3、` 这类编号，所以**改编号不影响命中**。
- 一个键匹配到 0 个标题（且 `required`）、匹配到多个、或标题层级不对（比如 `##` 被降成 `###`），**构建期直接抛 `ContractError`，HTML 根本不会生成**。
- 命中后在标题上盖 `data-sec="<键>"`。**此后所有图表、装饰只准用 `window.__sec` 按键定位，不准再读标题文字。**

`window.__sec` 提供：`head(key)` 取标题、`tail(key)` 取该节最后一个块（往后插就落在本节末尾）、`find(key, sel)` 在节内找元素（常用 `.table-wrap`）、`contains(key, el)` 判断某元素是否落在本节范围内。章节范围按文档顺序算，没有额外的包裹元素——这是刻意的，包一层 `<section>` 会改变 `nextElementSibling` 的遍历边界，波及现有装饰。

改 `references/template/section*.md` 的章节结构时，**同步升 `DMS_CONTRACT` 的版本号**，否则契约会悄悄和模板失配。

`dms/1.4.0` 同时检查 Markdown 内容，规则一律从 `references/template/section*.md` 投影而来——每个契约条目都带 `source=<模板文件>:<行号>`，改模板就要同步改契约，不允许契约自己猜。

- **15 个章节，顺序与层级固定**。其中 14 个必填，**只有 2.2 允许整节缺席**：模板规定没有 ★★★ 主线时连标题带兜底句一起不输出，所以它的在场与否是双向硬判定——2.1 有 ★★★ 却没有 2.2 会红，没有 ★★★ 却留着 2.2 也会红。在场时，2.2 里 `### 主线名称（★★★）` 的个数必须等于 2.1 表里的 ★★★ 行数。
- **降级要有据**。模板为某节写明的降级句（如「暂无命中」「风格证据不足，不强行定性」）命中后记进审计的 `detail.content_contract.degraded`，但只有 evidence 确实是 `available=false` 或候选为空才放行；evidence 里明明有候选却写「暂无命中」＝谎报缺数据，直接红。
- **硬门禁**：`output_discipline.md` 点名的定性高亮段（1.1 趋势判断、指数趋势判断、市场风格判断、主线 vs 资金轮动结论、风险传导提示、特征分组一句话判断）缺任一即失败；买卖建议禁用词出现即失败。
- **软告警**（只进审计不红灯）：判断段首句以连续数字开头、同一自然段关键数字超过 3 个、段落里出现既不在取数集也不在本页表格里的数字。这三条只判**散文判断段**：frontmatter、页眉元信息、1.1 那张必须逐项照抄的状态卡、以及判据 / 数据源 / 口径声明整段跳过，审计的 `paragraph_discipline.paragraphs_skipped` 记下跳过了几段。数字计数也只数读数——日期、时点、`5 日均` 这类窗口标签、`科创50` 这类指数名里的数字都不计。口径没收窄之前，一份合规报告一天要报 27 条，其中 12 条打在模板强制的结构块上，而天天误报的门禁三天内就会被绕过。

表格数值必须能回到脚本产物，这是「表格承载脚本给的确定性数据、段落承载判断」的机器化。**取数集不止 evidence**：同日 `module_context_<日期>/` 下的 `module3_theme_stats.json`、`module3_theme_map.json`、`assembled_checks.json` 自动并入，它们同样是脚本算出来的。2026-08-19 就栽在这上面——`27.45%`、`5.43%` 出自 theme_group_stats，却因为不在取数集里被判成编造，报告只好把它们从主线表挪进正文，结构反而变差。审计的 `table_numbers.sources` 列出这次实际用了哪几份。

**3.1 是例外**：风险类型是模型当场分的组，模板要它填组内中位数，而偶数样本的中位数按定义是中间两值的平均——这个数任何脚本都不会产出。只出现在 3.1、不见于别处的数字降级为 `derived_group_aggregate` 软告警；同一个数只要在别的表里也出现，就照旧硬判。没有这条口径，报告就会写成「3.85 / 4.25」这种并列中位数来绕开门禁。

**已知边界**：它是"这个数在 evidence 里存不存在"的全局包含判定，不是字段级比对。所以写错但恰好在别处出现过的数字抓不到——8-07 报告里国证2000 的 20 日表现写成 `-6.9%`（实际 `-5.97`）被抓到了，同一行今日表现写成 `+1.7%`（实际 `1.91`）却蒙混过关，因为 1.7 在 evidence 别处存在。要收紧就得为每张表建列→字段映射，成本另计。evidence 不完整（合成夹具）时整项跳过并记 warning，不误报。

## 签到：图表画完要报数

每个图表 hook 画完调用 `window.__render.attest(名字, {...})`，报四个数：

| 字段 | 含义 | 判定 |
|---|---|---|
| `rendered` | 实际插进 DOM 的图表数 | 必须等于 `matched` |
| `matched` | 解析出可绘制数据的输入数 | —— |
| `expected` | 本该画的输入数（如表格行数） | 必须等于 `matched + unmatched` |
| `unmatched` | `[{name, reason}]` 每个画不出来的输入 | `reason` 必须在闭集内 |

闭集原因码：`no_kline_data`、`no_records_in_window`、`suspended`、`not_in_universe`、`name_ambiguous`、`no_payload`。

口径是**允许有缺口，但每个缺口都要有名有姓**。新股、停牌、不在取数池的票本来就取不到 K 线，强行要求 `expected` 恒等于 `rendered` 会天天误报，而天天误报的门禁三天内就会被绕过。但 `rendered != matched`（数据都解析出来了却没画）是纯 bug，零容忍。

另外，`ChartHook` 在 Python 侧声明 `expect_count`（如趋势图 5 张、指数 K 线 3 张）。JS 自己维护图表清单，两边对不上就是漂移，正是要抓的。

**页面的 `data-render-status="ok"` 是推出来的，不是报出来的**：清单里每个 hook 都签到才算 ok。没签到即失败——脚本被 CSP 剥掉时不会有任何错误产生，只有"该来的没来"这个信号能抓到它。

## 三阶段门禁

```bash
python3 scripts/_shared/html_report/render_check.py --target <本地 HTML> --stage local --out <本地审计文件>
# 从本地审计文件取 build_id，Site 与线上门禁都必须传 --expect-build
python3 scripts/_shared/html_report/render_check.py --target <目标> --stage <site|online> \
  --expect-contract dms/1.4.0 --expect-build <本地审计文件中的 build_id> --out <审计文件>
```

三段各查各的问题：

- `local`：锚点没命中、图表数量不对、缺口没原因码、图表画到了别的章节里。
- `site`：外置步骤把内联脚本剥掉或改写了，分片资产（K 线 JSON、图片）的相对路径没跟着过去。
- `online`：CSP 挡了内联脚本、线上是旧缓存、资产 404。

主线生命周期泳道另有一道**渲染期**门禁（在这三段之前）：台账里有窗口数据、却没有报告当日的记录时，`render_report_html.py` 直接 `ContractError`，不出 HTML。它挡的是"图画出来了、只是画了个洞"——2026-08-19 漏了执行流第 6 步，页面头部写着「截至本期 2026-08-19」，24 条泳道的最后一列全空，local/site/online 三段全绿也照样看不出来。全面退潮日按契约没有主线记录，只要 `theme_market_day` 有当天那行就算覆盖；历史回补用 `--no-lifecycle` 显式跳过。

`site`、`online` 缺少 `--expect-build` 会直接报参数错误。只比契约版本抓不住“模板没变但内容还是上一版”的缓存；必须把 local 审计 JSON 里的 `build_id` 带到后两段。浏览器门禁同时监听网络失败与 HTTP 4xx/5xx 响应，因为 Playwright 的 `requestfailed` 本身不会报告 404。

退出码：`0` 通过、`1` 失败、`2` 只跑了 `--static-only` 冒烟（拦不住位置类问题，不算门禁）。

**门禁没全绿之前不许部署、不许 cleanup。** 三份 `render-check-*.json` 留在 `reports/` 下，失败时正是排查依据——今天 evidence 文件被 cleanup 清掉之后就没法回头复验了。

渲染时加 `--gate` 可以把 local 那段直接并进渲染命令，它同时会把文本保全校验切成 strict。

浏览器门禁依赖 Playwright 与 Chromium。首次安装运行：

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

如果审计 JSON 报 `browser gate requires Playwright`，说明 Python 包未安装；如果 Playwright 提示找不到 executable，说明 Chromium 尚未安装。`--static-only` 只能临时定位文件结构问题，退出码为 `2`，不能替代浏览器门禁。

## 失败了怎么查

审计 JSON 的 `problems` 每条带 `scope`，直接指向责任方：

- `section:<键>` —— 章节锚点在 DOM 里找不到。多半是装饰把标题删了或改了，或者报告根本没写这一节。
- `hook:<名字>` + `hook did not check in` —— 该 hook 没跑。看 `console_errors`，或者它的脚本被剥了。
- `hook:<名字>` + `outside section [...]` —— 图表画错地方了，就是 8-03 那类问题。
- `hook:<名字>` + `rendered != matched` —— 数据解析出来了没画出来，纯 bug。
- `chart-data` —— payload 不是合法 JSON，这会让**全站图表一起消失**而页面看着完全正常。
- `attestation` + `page never wrote data-render-status` —— 页面脚本压根没跑。site/online 阶段最常见的就是这条。

`detail.hooks` 里有每个 hook 的完整台账，`unmatched` 逐条列出画不出来的是谁、为什么。

## 回归

```bash
python3 tests/test_render_gate.py
```

夹具是真实的 2026-08-03 报告（已按 2026-08 的章节重排迁移过）+ 合成 evidence（真 evidence 每天 cleanup 掉了，靠它的测试活不过一天）。23 条用例覆盖两类事：契约能否扛住改编号 / 改名 / 降级 / 降层级 / 乱序，以及门禁能否抓住图表落到文末、锚点丢失、脚本被剥、payload 损坏、资产 404 这几类失败。

原先还有四条盯着 1.1 状态卡与状态标尺（徽章 / 勾选 / 五格齐全 / 数值列不压条形），随 1.1 一起删了。它们的教训值得留一句：SVG 不裁剪，标签超宽不会被切、而是压在左边的条形上或溢到卡片外面，所以这类图的数值列宽必须在 Python 侧按最长标签算好、让轨道吃掉剩下的空间；而 hook 的 `expect_count` 不能无条件声明，evidence 少一块就报红的门禁，报久了就会被绕过。
