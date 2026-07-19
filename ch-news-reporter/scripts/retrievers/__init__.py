"""自定义关注主题的多通道原子检索器。

每个模块只做一件原子事:取数 / 去重 / 落库 / 打包,绝不做"值不值得"的判断
(判断全归模型,见 docs/custom-topics-design.md D2/D7)。

通道契约:每个检索器暴露一个 retrieve(...) 函数,返回统一结构的 dict:

    {
        "status": "ok" | "degraded" | "skipped" | "error",
        "count": int,               # 本通道取到的证据条数
        "warnings": [str],          # 降级 / 跳过原因,进 coverage
        ...                         # 通道自有字段(items / notes / hooks / queries)
    }

新通道 = 在本目录新增一个原子脚本 + 在 custom_topics.yaml 加配置项,
编排层 topic_retrieve.py 不需要改动核心逻辑。
"""
