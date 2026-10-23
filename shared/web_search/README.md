# shared/web_search —— 原子联网搜索

三个互为备份的搜索 API 脚本 + 一个统一兼容入口，走同一份输出契约。
脚本只取数、只做确定性映射与切换，不下结论——查什么、信哪个源、结果
怎么读，由模型决定。

| 脚本 | 引擎 | 强项 | 密钥 |
|---|---|---|---|
| `tavily_search.py` | Tavily | 海外源、新闻 topic + 时间窗 | `TAVILY_API_KEY` |
| `deepseek_search.py` | DeepSeek 自研搜索 | 中文源、时效性、rerank_score/authority 可参考 | `DEEPSEEK_API_KEY` |
| `yuanbao_search.py` | 腾讯元宝搜索（WSA SearchPro） | 腾讯内容生态（腾讯新闻/搜狗百科/企鹅号）、站内与行业信源约束 | `TENCENTCLOUD_SECRET_ID` + `TENCENTCLOUD_SECRET_KEY` |
| `multi_search.py` | 统一入口 | 参数映射、条数对齐、failover / fanout | 沿用所引用擎的密钥 |

## 基础用法（原子脚本）

```bash
python shared/web_search/tavily_search.py "Credo CRDO catalyst" --topic news --days 7
python shared/web_search/deepseek_search.py "寒武纪 业绩预告" --count 8 --need-content
python shared/web_search/yuanbao_search.py "光模块 订单" --freshness d7 --cnt 20 --industry finance
```

成功：`{"query", "results"}`；失败：`{"query", "error": {code, detail}}`，exit 1。
密钥优先环境变量，否则就近 `.env`（cwd 或脚本上溯最多 6 层）。禁止硬编码。

## 统一入口与兼容矩阵（multi_search.py）

统一参数只有一组：`query --max-results --days --start-date/--end-date
--include-domains --exclude-domains --engine --mode`。映射规则：

| 统一参数 | tavily | deepseek | yuanbao |
|---|---|---|---|
| `--max-results N` | 原生支持，>20 截断并留痕 | `count=N`（1-50，>50 截断留痕） | `Cnt` 向上对齐 10/20/30/40/50，拿回后裁剪到 N 并留痕 |
| `--days D` | `days=D` | **不支持**，结果未过滤，notes 留痕 | `freshness=dD`（D≤30，否则跳过该引擎） |
| `--start-date/--end-date` | 原生支持 | 不支持，留痕 | `freshness="start,end"`（必须成对） |
| `--include-domains` | `include_domains` | `site:` 语法并入 query（超 128 字符则跳过） | `Site` **仅支持单个域名**，多个则跳过该引擎（API 限制） |
| `--exclude-domains` | `exclude_domains` | `-site:` 并入 query | `Site="exclude:..."`（≤5 个） |
| include+exclude 同时给 | 原生都支持 | 两种语法可并用 | **不支持**，跳过该引擎 |

铁律：某引擎表达不了的过滤条件，要么留痕降级（notes）、要么跳过该引擎
（skipped），绝不悄悄丢掉条件继续搜。结果统一成
`title/url/published_date/snippet/content` 五个公共字段，引擎扩展字段
（deepseek 的 `summary/rerank_score/authority/site_name`，元宝的
`site/score`）原样平铺保留，不合并口径——两家的 score 量纲不同（0-3 与 0-1）。

## 元宝搜索三版本（极速/标准/旗舰）支持度

`yuanbao_search.py` 对 SearchPro 接口的 7 个输入参数全部透传（Query 必填，
`--mode/--site/--cnt/--industry/--freshness/--deeplinks` 可选），档位差异
由 API 自己裁决（参数不可用时回 InvalidParameter，脚本原样透传）：

| 能力 | 极速版 | 标准版 | 旗舰版 |
|---|---|---|---|
| Query | ✅ | ✅ | ✅ |
| Mode / Cnt / Industry | ❌ 不支持 | ✅ | ✅ |
| Site（站内单域名 / 排除≤5）/ Freshness | ✅ | ✅ | ✅ |
| Deeplinks 参数 | ❌ | ❌ | ✅ |
| 结果含 content / pics | ❌ | ✅ | ✅ |
| 结果含 authority_level（0-5 权威分级） | ❌ | ❌ | ✅ |
| 结果含 deeplinks 子链 | ❌ | ❌ | ✅ |

输出侧：结果条目除公共字段外**全部原样保留**（site/score 恒有，
content/pics/authority_level/deeplinks 随档位出现）；顶层成功返回可能带
`meta: {version, msg}`——`version` 告知本次由哪一档应答，`msg` 承载
"hit black query" 之类的引擎提示。校验前置在本地：freshness 日期必须真实、
不早于 1970-01-01、不晚于当天、区间起点≤终点；Site 站内仅单域名。

## 通道怎么选（模型判断，脚本不自动选路）

`--engine` 必填，逼着每次检索先过一遍选型：

```bash
python multi_search.py "寒武纪 业绩预告" --engine yuanbao --days 7
python multi_search.py "Credo catalyst" --engine tavily,deepseek --max-results 10
python multi_search.py "光模块 订单" --engine all
```

- **单引擎**：查什么用哪个，按下表定。
- **`--engine a,b,c`（逗号列表）**：按序 failover——前一个报错或 0 结果
  才尝试下一个，成功即返回（`engine` 字段标明谁答的，`attempts` 记录前面
  的失败）。适合"主通道挂了/没结果自动换一家"。
- **`--engine all`**：三路并发 fanout，逐引擎返回结果或错误，**3 倍调用
  成本**，只用于重要结论的交叉验证；结果冲突时以可回访的原文 URL 为准。

选型经验值（写在方法学里，不写死在脚本里）：

- A 股公告/监管/官方中文信源 → `yuanbao` 优先，`--site` 限定官方域名，`deepseek` 交叉。
- 海外公司/英文催化/财报 → `tavily` 优先（`--topic news --days` 时间窗好用）。
- 中文时效热点（盘前盘后快讯） → `deepseek` 或 `yuanbao`。
- 结论要写进日报/影响判断 → `--engine all` 交叉后再下笔。

## 元宝搜索前置条件

需要在腾讯云控制台开通「元宝搜索」（https://console.cloud.tencent.com/wsapi/index)，
未开通时 API 回 `ResourceNotFound`，脚本原样透传。计费按次（极速/标准/旗舰
三档，接口可用参数随档位不同，见 https://cloud.tencent.com/product/wsa）。

## skill 侧接入

各 skill 的 `scripts/web_search.py` 是薄包装（runpy 转发到共享实现，兼容
skill-sync 后的 `scripts/_shared/web_search` 布局）。要切到统一入口时，把
包装目标改成 `multi_search.py` 并带上 `--engine` 即可；`skill-sync.yaml`
的 `shared.bundles` 会把本目录整体同步。
