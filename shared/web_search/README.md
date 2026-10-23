# shared/web_search —— 原子联网搜索

三个互为备份的搜索 API 脚本，走同一份输出契约，skill 侧用法完全一致。
脚本只取数，不下结论——查什么、怎么读结果、信哪个源，由模型决定。

| 脚本 | 引擎 | 强项 | 密钥 |
|---|---|---|---|
| `tavily_search.py` | Tavily | 海外源、新闻 topic + 时间窗 | `TAVILY_API_KEY` |
| `deepseek_search.py` | DeepSeek 自研搜索 | 中文源、时效性、rerank_score/authority 可参考 | `DEEPSEEK_API_KEY` |
| `yuanbao_search.py` | 腾讯元宝搜索（WSA SearchPro） | 腾讯内容生态（腾讯新闻/搜狗百科/企鹅号）、站内与行业信源约束 | `TENCENTCLOUD_SECRET_ID` + `TENCENTCLOUD_SECRET_KEY` |

## 统一契约

- 成功：`{"query": ..., "results": [...]}`，每条含 `title` / `url` / `published_date` / `content`（各引擎的附加字段见各自 docstring）。
- 失败：`{"query": ..., "error": {"code", "detail"}}`，exit code 1。参数非法在本地就报 `invalid_parameters`，不打无效请求。
- 密钥：优先环境变量，否则就近的 `.env`（cwd 或脚本上溯最多 6 层）。禁止硬编码。

```bash
python shared/web_search/tavily_search.py "Credo CRDO catalyst" --topic news --days 7
python shared/web_search/deepseek_search.py "寒武纪 业绩预告" --count 8 --need-content
python shared/web_search/yuanbao_search.py "光模块 订单" --freshness d7 --cnt 20 --industry finance
```

## 选型与交叉验证

- 中文时政/公告/官方信源核查：`yuanbao_search.py` 优先（腾讯生态收录好，`--site` 可站内限定官方域名），`deepseek_search.py` 交叉验证。
- 海外公司/英文催化：`tavily_search.py` 优先（`--topic news --days` 时间窗好用）。
- 重要结论不要单源定案：两个源都查，结果不一致时以可回访的原文 URL 为准。

## 元宝搜索前置条件

需要在腾讯云控制台开通「元宝搜索」（https://console.cloud.tencent.com/wsapi/index），
未开通时 API 回 `ResourceNotFound`，脚本原样透传。计费按次（极速/标准/旗舰三档，
接口可用参数随档位不同，见 https://cloud.tencent.com/product/wsa）。

## skill 侧接入

各 skill 的 `scripts/web_search.py` 是薄包装（runpy 转发到共享实现，兼容
skill-sync 后的 `scripts/_shared/web_search` 布局）。需要新引擎时，照同一模式
加一个转发入口即可；`skill-sync.yaml` 的 `shared.bundles` 会把本目录整体同步。
