---
name: a-stock-daily-market-sense-render-contract
description: 仅供 a-stock-daily-market-sense 内部按需读取。说明日报 HTML 的章节契约、渲染清单、运行期签到与三阶段上线门禁怎么用、失败了怎么查。
---

# 渲染契约与上线门禁

## 为什么有这套东西

日报的图表不在 HTML 文件里，是页面加载后自己用 JS 画出来的。这带来一个后果：**静态看文件完全看不出图表画到哪儿了、有没有画。**

2026-08-03 就踩了这个坑。趋势图面板当时靠标题文字定位，判据写死成「标题里同时含 `1.1` 和 `情绪趋势`」。新增「1.1 市场状态定位」之后，情绪趋势顺延成 1.2，判据不再命中，而代码的兜底是「找不到就 `appendChild` 到 `report-body` 末尾」——五张趋势图整块挪到了文末。没有异常、没有空容器、控制台干净，只能靠人眼发现。

所以现在三件事同时成立：章节按语义键定位而不是按编号，图表画完必须主动签到，上线前用真浏览器验一遍。

## 章节契约

`scripts/render_report_html.py` 顶部的 `DMS_CONTRACT` 是唯一允许依赖标题文字的地方。它把标题解析成稳定语义键：

- 匹配前先剥掉 `1.2`、`3.3、` 这类编号，所以**改编号不影响命中**。
- 一个键匹配到 0 个标题（且 `required`）、匹配到多个、或标题层级不对（比如 `##` 被降成 `###`），**构建期直接抛 `ContractError`，HTML 根本不会生成**。
- 命中后在标题上盖 `data-sec="<键>"`。**此后所有图表、装饰只准用 `window.__sec` 按键定位，不准再读标题文字。**

`window.__sec` 提供：`head(key)` 取标题、`tail(key)` 取该节最后一个块（往后插就落在本节末尾）、`find(key, sel)` 在节内找元素（常用 `.table-wrap`）、`contains(key, el)` 判断某元素是否落在本节范围内。章节范围按文档顺序算，没有额外的包裹元素——这是刻意的，包一层 `<section>` 会改变 `nextElementSibling` 的遍历边界，波及现有装饰。

改 `references/template/section*.md` 的章节结构时，**同步升 `DMS_CONTRACT` 的版本号**，否则契约会悄悄和模板失配。

## 章节内部的形状也是契约（1.1 状态卡）

契约管的是「章节在不在」，管不到「章节里长什么样」。1.1 市场状态定位的卡片就靠章节内部的形状定位：一个开头是 `回撤分层` 的列表、其中 `确认三要素` 那条挂着嵌套列表、每条勾选项以 `✓ / ✗ / —` 开头。这套形状写在 `references/template/section1.md` 里，**改模板就要同时改 `MARKET_STATE_CARD_JS` 的解析**。

形状对不上时装饰会安静退出，报告退回成普通列表——这是刻意的：装饰没生效是观感回退，图表没画出来才是数据回退，两者不该用同一种严厉程度对待。所以卡片不签到，只有状态标尺（`market-state.panel`）签到。

标尺本身是「一个读数 + 它的参照线」重复五遍：回撤分层 / 市场宽度 / 申万一级结构 / 融资余额 / 流动性，各自带独立坐标轴。**轴不共用是刻意的**——回撤 0→-23%、宽度 0→100%、扩散 0→31、融资 ±13%、量能围绕 1.0，硬拉到一根轴上会让本来不可比的条形看起来可比。每格的数值列宽在 Python 侧按最长标签算好（CJK 计双宽）传给 JS，轨道吃剩下的空间；这是因为 SVG 不裁剪，标签超宽会压在条形上而不是被切掉。

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
  --expect-contract dms/1.1.0 --expect-build <本地审计文件中的 build_id> --out <审计文件>
```

三段各查各的问题：

- `local`：锚点没命中、图表数量不对、缺口没原因码、图表画到了别的章节里。
- `site`：外置步骤把内联脚本剥掉或改写了，分片资产（K 线 JSON、图片）的相对路径没跟着过去。
- `online`：CSP 挡了内联脚本、线上是旧缓存、资产 404。

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

夹具是真实的 2026-08-03 报告 + 合成 evidence（真 evidence 每天 cleanup 掉了，靠它的测试活不过一天）。16 条用例覆盖：契约能否扛住改编号/改名/降级；门禁能否抓住图表落到文末、锚点丢失、脚本被剥、payload 损坏这四类失败；以及 1.1 状态卡的四件事——卡片确实由 Markdown 升级而成（徽章 / 勾选 / 计数）、状态标尺五格齐全且阈值刻度可见、五格的数值列都没有压到条形或溢出画布、evidence 缺 `market_state` 时**不声明**该 hook（数据缺口不该让门禁变红）。

后两条是有来历的：SVG 不裁剪，标签超宽不会被切、而是压在左边的条形上或溢到卡片外面——阶梯版溢过 24px，标尺版第一次也压过回撤那格的条形，所以数值列宽由最长标签在 Python 侧算出来，轨道吃掉剩下的空间。而如果把 hook 无条件声明成 `expect_count=1`，只要哪天 evidence 少了 `market_state`，门禁就会因为一个数据缺口报红——报红报久了就会被绕过。
