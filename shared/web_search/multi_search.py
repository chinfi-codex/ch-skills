#!/usr/bin/env python3
"""multi_search —— 三路搜索引擎的统一兼容入口（映射与切换，不做智能选路）。

tavily / deepseek / yuanbao 三个原子脚本的参数与返回形态各有差异，本入口把
它们对齐成统一契约。只做两类确定性工作：

1. 参数映射：统一参数翻译成各引擎的原生参数；某引擎表达不了的约束
   （如 deepseek 没有时间窗、元宝的 Site 不支持 include+exclude 并用），
   记成 notes / skipped 透明返回，绝不悄悄丢掉过滤条件继续搜。
2. 通道执行：--engine 单引擎 / 逗号列表（按序 failover，前一个报错或
   0 结果才尝试下一个）/ all（三路并发 fanout，交叉验证用）。

用哪条通道是模型按 shared/web_search/README.md 的选型表做的判断，
本脚本不猜、不自动选路。

输出契约：
- 单引擎 / failover 成功：{"query", "mode": "failover", "engine", "results"}，
  另有 "attempts"（前置失败引擎的 error）与 "notes"（映射说明）时才出现。
- failover 全败：{"query", "error": {"code": "all_engines_failed", ...}}。
- fanout：{"query", "mode": "fanout", "engines": {名: {"results"} 或 {"error"} 或 {"skipped"}}}。

统一结果字段：title / url / published_date / snippet / content，各引擎扩展
字段（deepseek 的 summary/rerank_score/authority/site_name，元宝的 site/score）
原样平铺保留，不合并口径（两家的 score 量纲不同：0-3 与 0-1）。

用法：
    python multi_search.py "寒武纪 业绩预告" --engine yuanbao --days 7
    python multi_search.py "Credo catalyst" --engine tavily,deepseek --max-results 10
    python multi_search.py "光模块 订单" --engine all            # 三路并发，注意 3 倍调用成本
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deepseek_search
import tavily_search
import yuanbao_search

ENGINE_NAMES = ("tavily", "deepseek", "yuanbao")
TAVILY_MAX_RESULTS = 20
DEEPSEEK_MAX_RESULTS = 50
YUANBAO_CNT_GRID = (10, 20, 30, 40, 50)
DEEPSEEK_QUERY_MAX_CHARS = 128
_DOMAIN_SPLIT = re.compile(r"[\s,]+")


def _split_domains(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [d for d in _DOMAIN_SPLIT.split(raw.strip()) if d]


def _call_tavily(query: str, *, max_results: int, days, start_date, end_date,
                 include: list[str], exclude: list[str]) -> tuple[dict, list[str]]:
    notes: list[str] = []
    n = max_results
    if n > TAVILY_MAX_RESULTS:
        notes.append(f"tavily: max_results 上限 {TAVILY_MAX_RESULTS}，已截断")
        n = TAVILY_MAX_RESULTS
    out = tavily_search.tavily_search(
        query,
        max_results=n,
        days=days,
        start_date=start_date,
        end_date=end_date,
        include_domains=include or None,
        exclude_domains=exclude or None,
    )
    return out, notes


def _call_deepseek(query: str, *, max_results: int, days, start_date, end_date,
                   include: list[str], exclude: list[str]) -> tuple[dict, list[str]]:
    notes: list[str] = []
    if days is not None or start_date or end_date:
        notes.append("deepseek: 无时间窗参数，本次结果未按时间过滤")
    n = min(max_results, DEEPSEEK_MAX_RESULTS)
    if n != max_results:
        notes.append(f"deepseek: count 上限 {DEEPSEEK_MAX_RESULTS}，已截断")

    ops = [f"site:{d}" for d in include] + [f"-site:{d}" for d in exclude]
    final_query = query
    if ops:
        candidate = " ".join([query, *ops])
        if len(candidate) > DEEPSEEK_QUERY_MAX_CHARS:
            return {
                "skipped": (
                    f"deepseek: query 并入域名约束后超 {DEEPSEEK_QUERY_MAX_CHARS} 字符，"
                    "已跳过该引擎"
                )
            }, notes
        final_query = candidate
        notes.append("deepseek: 域名约束以 site:/-site: 语法并入 query")

    out = deepseek_search.deepseek_search(final_query, count=n)
    return out, notes


def _call_yuanbao(query: str, *, max_results: int, days, start_date, end_date,
                  include: list[str], exclude: list[str]) -> tuple[dict, list[str]]:
    notes: list[str] = []
    if include and exclude:
        return {
            "skipped": "yuanbao: Site 只支持站内或排除二选一，无法同时表达 include+exclude"
        }, notes

    freshness = None
    if days is not None:
        if not 1 <= days <= 30:
            return {"skipped": f"yuanbao: --days {days} 超出 freshness d[1-30]"}, notes
        freshness = f"d{days}"
    elif start_date and end_date:
        freshness = f"{start_date},{end_date}"
    elif start_date or end_date:
        return {"skipped": "yuanbao: freshness 只支持完整日期区间，单边日期无法表达"}, notes

    cnt = next((g for g in YUANBAO_CNT_GRID if g >= max_results), YUANBAO_CNT_GRID[-1])
    if cnt != max_results:
        notes.append(
            f"yuanbao: Cnt 只能取 {list(YUANBAO_CNT_GRID)}，按 {cnt} 请求后裁剪到 {max_results}"
        )

    kwargs: dict = {"cnt": cnt}
    if include:
        kwargs["site"] = "|".join(include)
    elif exclude:
        kwargs["site"] = "exclude:" + "|".join(exclude)
    if freshness:
        kwargs["freshness"] = freshness

    out = yuanbao_search.yuanbao_search(query, **kwargs)
    if "results" in out:
        out["results"] = out["results"][:max_results]
    return out, notes


_ADAPTERS = {
    "tavily": _call_tavily,
    "deepseek": _call_deepseek,
    "yuanbao": _call_yuanbao,
}


def _normalize_items(items: list[dict]) -> list[dict]:
    """对齐成公共五字段，引擎扩展字段原样平铺保留。"""
    core = ("title", "url", "published_date", "snippet", "content")
    return [
        {
            **{key: item.get(key) for key in core},
            **{k: v for k, v in item.items() if k not in core},
        }
        for item in items
    ]


def _run_engine(engine: str, query: str, **params) -> tuple[str, dict, list[str]]:
    """跑单个引擎，返回 (engine, 归一化输出, notes)。"""
    out, notes = _ADAPTERS[engine](query, **params)
    if "skipped" in out:
        notes.append(out["skipped"])
        return engine, {"skipped": out["skipped"]}, notes
    if "error" in out:
        return engine, {"error": out["error"]}, notes
    return engine, {"results": _normalize_items(out.get("results") or [])}, notes


def multi_search(
    query: str,
    *,
    engines: list[str],
    mode: str = "failover",
    max_results: int = 5,
    days: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> dict:
    include = include_domains or []
    exclude = exclude_domains or []
    params = dict(
        max_results=max_results,
        days=days,
        start_date=start_date,
        end_date=end_date,
        include=include,
        exclude=exclude,
    )
    if days is not None and (start_date or end_date):
        return {
            "query": query,
            "error": {
                "code": "invalid_parameters",
                "detail": "days cannot be combined with start_date or end_date",
            },
        }

    if mode == "fanout":
        with ThreadPoolExecutor(max_workers=len(engines)) as pool:
            futures = [pool.submit(_run_engine, e, query, **params) for e in engines]
            ran = [f.result() for f in futures]
        engine_outputs = {name: out for name, out, _ in ran}
        notes = [n for _, _, ns in ran for n in ns]
        payload: dict = {"query": query, "mode": "fanout", "engines": engine_outputs}
        if notes:
            payload["notes"] = notes
        return payload

    # failover：按序尝试，报错或 0 结果才落到下一个引擎
    attempts: dict = {}
    last_success: tuple[str, list, list[str]] | None = None
    all_notes: list[str] = []
    for engine in engines:
        name, out, notes = _run_engine(engine, query, **params)
        all_notes.extend(notes)
        if "skipped" in out:
            attempts[name] = {"skipped": out["skipped"]}
            continue
        if "error" in out:
            attempts[name] = {"error": out["error"]}
            continue
        if not out["results"]:
            attempts[name] = {"empty": True}
            last_success = (name, out["results"], notes)
            continue
        payload = {"query": query, "mode": "failover", "engine": name, "results": out["results"]}
        if attempts:
            payload["attempts"] = attempts
        if all_notes:
            payload["notes"] = all_notes
        return payload

    if last_success is not None:
        name, results, _ = last_success
        payload = {"query": query, "mode": "failover", "engine": name, "results": results}
        if attempts:
            payload["attempts"] = attempts
        if all_notes:
            payload["notes"] = all_notes
        return payload

    return {
        "query": query,
        "error": {
            "code": "all_engines_failed",
            "detail": attempts or {"reason": "no engine attempted"},
        },
    }


def _parse_engines(raw: str) -> list[str]:
    if raw == "all":
        return list(ENGINE_NAMES)
    engines = [e.strip() for e in raw.split(",") if e.strip()]
    invalid = [e for e in engines if e not in ENGINE_NAMES]
    if invalid or not engines:
        raise argparse.ArgumentTypeError(
            f"unknown engine(s): {invalid or raw}; choose from {list(ENGINE_NAMES)} or 'all'"
        )
    return engines


def _arg_iso_date(value: str) -> str:
    try:
        tavily_search._parse_iso_date(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}: expected YYYY-MM-DD"
        ) from exc
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="多引擎搜索统一入口（tavily/deepseek/yuanbao；脚本只映射与切换，选路由模型判断）。"
    )
    parser.add_argument("query", help="检索词")
    parser.add_argument(
        "--engine",
        required=True,
        metavar="tavily|deepseek|yuanbao|all|tavily,deepseek",
        help="单引擎；逗号列表=按序 failover；all=三路并发 fanout（3 倍成本，交叉验证用）",
    )
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--days", type=int, default=None, help="时间窗（天）")
    parser.add_argument("--start-date", type=_arg_iso_date, default=None)
    parser.add_argument("--end-date", type=_arg_iso_date, default=None)
    parser.add_argument("--include-domains", default=None, help="逗号/空格分隔，限定域名")
    parser.add_argument("--exclude-domains", default=None, help="逗号/空格分隔，排除域名")
    parser.add_argument(
        "--mode",
        choices=["failover", "fanout"],
        default=None,
        help="逗号列表引擎的执行方式（默认 failover）；all 恒为 fanout",
    )
    args = parser.parse_args(argv)

    try:
        engines = _parse_engines(args.engine)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    if args.engine == "all":
        mode = "fanout"
    elif len(engines) == 1:
        mode = "failover"
    else:
        mode = args.mode or "failover"

    result = multi_search(
        args.query,
        engines=args.engine,
        mode=mode or "failover",
        max_results=args.max_results,
        days=args.days,
        start_date=args.start_date,
        end_date=args.end_date,
        include_domains=_split_domains(args.include_domains),
        exclude_domains=_split_domains(args.exclude_domains),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if "error" in result:
        return 1
    if result.get("mode") == "fanout":
        outputs = result.get("engines", {}).values()
        usable = [o for o in outputs if "results" in o]
        return 0 if usable else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
