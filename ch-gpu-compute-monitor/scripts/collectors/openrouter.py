"""OpenRouter 采集器 —— 需求端的量与价，本 skill 唯一同时给两边的源。

两路取数，一次 join：
  * /api/frontend/v1/rankings/models?view=day —— 已结算的 T-1 日，模型 × 变体级
    prompt / completion token 与请求数。两次相隔 20 分钟的快照实测逐行一致，
    说明 T-1 是冻结的，不是滚动 24 小时累加。
  * /api/v1/models —— 逐模型挂牌价（USD/token），含 input_cache_read。

**join 键必须是「剥掉 -20YYMMDD 的 base + variant」，这是全案最大的坑。**
`:batch` / `:free` 变体与标准条目共用同一个 canonical_slug（实测 69 处碰撞）：

    anthropic/claude-opus-5-20260723 -> [claude-opus-5 $5.00/Mtok, :batch $2.50/Mtok]
    nvidia/nemotron-3.5-lightning-…  -> [标准 $0.08/Mtok, :free $0.00]

按 canonical_slug 建索引，后写入的变体会覆盖标准条目，于是标准流量被按半价甚至
零价计。实测差别：spend $5.33M/日 → $8.69M/日（+63%），混合价 $0.529 → $0.797/Mtok。

口径边界（详见 references/token_taxonomy.md）：
  * spend 是**按挂牌价计的名义支出**，不是实际账单。prompt 占付费 token 97.3%，
    而缓存命中只按约 12% 计费、命中率又不可观测（total_native_tokens_cached 全为 0）。
  * 价取模型默认价。同模型跨 provider 有价差，实测 spend 加权 默认→中位 1.152x，
    所以给 spend 前 N 名额外拉一次 /endpoints，把 min/median/max 带宽存进行里。
  * rankings 没有任何历史接口，序列只能从首采日往后长，补不回去。

第三路是调用方维度（/api/frontend/v1/rankings/apps?view=day），回答"谁在消费"：
  * 只给 app_id、total_tokens、total_requests 与应用元信息，**没有模型拆分也没有价**，
    所以它和模型 × 变体那张表没有可 join 的键，spend 也无从归属。
  * `total_tokens` 是**字符串**（bigint 序列化成 str），当整数直接加会拼字符串。
  * **返回的 20 行不是「前 20 名」**：实测 rank 跳过 2/3/4/15/16/18，说明有名次不公开露出。
    所以它们的和是应用侧总量的**下界**，「其他」也不能读成「未上榜的应用」。
  * 响应里没有 date 字段，日期只能沿用同一次取数里模型榜的结算日（同站同 view）。
"""

from __future__ import annotations

import re
import statistics
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .base import (
    CollectResult,
    CollectorError,
    query_fingerprint,
    request_json,
    save_raw,
    token_app_row,
    token_history_row,
    token_row,
)

SOURCE = "openrouter"

# 模型 slug 的日期后缀：deepseek-v4-flash-20260731 → deepseek-v4-flash。
# 只剥结尾（或 :variant 之前）的那一段，别把 gpt-4.1-nano-2025-04-14 这种
# 带连字符的版本号误伤——它不匹配 -20YYMMDD 的形状，会原样保留。
DATE_SUFFIX_RE = re.compile(r"-20\d{6}(?=$|:)")
PER_MTOK = 1_000_000.0


def strip_date(slug: str) -> str:
    return DATE_SUFFIX_RE.sub("", slug)


def split_variant(slug: str) -> Tuple[str, str]:
    """`anthropic/claude-opus-5:batch` → ('anthropic/claude-opus-5', 'batch')。"""
    if ":" in slug:
        base, variant = slug.split(":", 1)
        return base, variant
    return slug, "standard"


def build_price_index(models: List[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """(base, variant) → pricing 条目。

    同一个 key 只认第一次写入的条目：id 与 canonical_slug 剥完日期后可能撞在
    一起，先到先得比后来居上安全——后者会让变体覆盖标准条目。
    """
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for entry in models:
        raw_id = entry.get("id")
        if not raw_id:
            continue
        base_id, variant = split_variant(raw_id)
        canonical, _ = split_variant(entry.get("canonical_slug") or "")
        for base in (base_id, strip_date(base_id), canonical, strip_date(canonical)):
            if base:
                index.setdefault((base, variant), entry)
    return index


def lookup(index: Dict[Tuple[str, str], Dict[str, Any]], slug: str,
           variant: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """返回 (pricing 条目, 匹配方式)。匹配方式进库，便于事后审计口径。"""
    hit = index.get((slug, variant))
    if hit is not None:
        return hit, "exact"
    hit = index.get((strip_date(slug), variant))
    if hit is not None:
        return hit, "date_stripped"
    return None, "unmatched"


def _price(entry: Dict[str, Any], key: str) -> Optional[float]:
    raw = (entry.get("pricing") or {}).get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _required_nonnegative_int(row: Dict[str, Any], key: str, identity: str) -> int:
    """读取排行榜必需计数字段；缺失或形状异常时显式失败，绝不补 0。"""
    if key not in row or row.get(key) is None:
        raise CollectorError(f"rankings 行 {identity} 缺少必需字段 {key}")
    raw = row[key]
    if isinstance(raw, bool):
        raise CollectorError(f"rankings 行 {identity} 的 {key} 不是整数：{raw!r}")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise CollectorError(
            f"rankings 行 {identity} 的 {key} 不是整数：{raw!r}") from exc
    if isinstance(raw, float) and not raw.is_integer():
        raise CollectorError(f"rankings 行 {identity} 的 {key} 不是整数：{raw!r}")
    if isinstance(raw, str) and not re.fullmatch(r"\d+", raw.strip()):
        raise CollectorError(f"rankings 行 {identity} 的 {key} 不是整数：{raw!r}")
    if value < 0:
        raise CollectorError(f"rankings 行 {identity} 的 {key} 不能为负数：{value}")
    return value


def _provider_band(base_url: str, path_tpl: str, model_id: str,
                   req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """逐 provider 的 prompt 报价分布。拿不到就返回 None——这是补强不是核心。"""
    url = base_url + path_tpl.format(model=model_id)
    payload = request_json(url, **req)
    data = (payload or {}).get("data") or {}
    prices = []
    for ep in data.get("endpoints") or []:
        raw = (ep.get("pricing") or {}).get("prompt")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            prices.append(value * PER_MTOK)
    if not prices:
        return None
    return {"min": min(prices), "median": statistics.median(prices),
            "max": max(prices), "count": len(prices)}


def parse_app_rows(payload: Any, view: str) -> Tuple[List[Dict[str, Any]], int]:
    """把应用榜的响应拆成 (行, 名次缺口数)。

    响应形状是 `data.{day,week,month}`，每个桶一个数组——和模型榜的 `data` 直接
    是数组不一样，取错桶就会把滚动 7 天的量当成单日量。

    名次缺口是这一路最容易被忽略的事实：返回 20 行、最大 rank 却是 26，中间的
    2/3/4/15/16/18 不在里面。**这 20 行不是前 20 名**，求和只是下界。
    """
    data = (payload or {}).get("data")
    if not isinstance(data, dict):
        raise CollectorError(
            f"apps 未返回预期的 data 对象（拿到 {type(data).__name__}）")
    rows = data.get(view)
    if not isinstance(rows, list) or not rows:
        raise CollectorError(
            f"apps 的 data 里没有 {view} 这个桶（现有 {sorted(data)}）")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CollectorError(
                f"apps 第 {i} 行不是对象（拿到 {type(row).__name__}）")
        if row.get("app_id") is None:
            raise CollectorError(f"apps 第 {i} 行缺少必需字段 app_id")
    ranks = [r["rank"] for r in rows if isinstance(r.get("rank"), int)]
    hidden = (max(ranks) - len(rows)) if ranks else 0
    return rows, max(hidden, 0)


def build_app_rows(rows: List[Dict[str, Any]], *, settled: str,
                   coverage_scope: str, fingerprint: str,
                   raw_ref: Optional[str]) -> Tuple[List[Dict[str, Any]], int]:
    """把应用榜的行转成落库行，顺带算榜上合计。任何形状问题一律抛 CollectorError。

    抽成独立函数是为了让调用方能用一个 try 把**整段**包住。之前逐行校验散在
    降级用的 try 外面，任一应用缺 `total_requests` 或 `app` 元信息变形，都会
    把整个 OpenRouter 源判失败——连带 500 多行核心模型观测一起不落库。
    应用榜是补强维度，它的脏数据没有资格拖垮主路。
    """
    out: List[Dict[str, Any]] = []
    listed_tokens = 0
    for row in rows:
        app_id = row["app_id"]
        identity = f"{settled} app:{app_id}"
        tokens = _required_nonnegative_int(row, "total_tokens", identity)
        requests_ = _required_nonnegative_int(row, "total_requests", identity)
        listed_tokens += tokens
        meta = row.get("app") or {}
        if not isinstance(meta, dict):
            raise CollectorError(
                f"apps 行 {identity} 的 app 不是对象（拿到 {type(meta).__name__}）")
        cats = meta.get("categories")
        out.append(token_app_row(
            obs_date=settled,
            source=SOURCE,
            app_id=app_id,
            app_slug=meta.get("slug"),
            app_title=meta.get("title") or meta.get("slug") or f"app-{app_id}",
            app_url=meta.get("origin_url") or meta.get("main_url"),
            categories=[str(c) for c in cats] if isinstance(cats, list) else None,
            rank=row.get("rank") if isinstance(row.get("rank"), int) else None,
            total_tokens=tokens,
            total_requests=requests_,
            coverage_scope=coverage_scope,
            query_fingerprint_=fingerprint,
            raw_ref=raw_ref,
        ))
    return out, listed_tokens


def collect(cfg: Dict[str, Any], catalog, obs_date: str,
            defaults: Optional[Dict] = None) -> CollectResult:
    """catalog 参数只为对齐其它采集器的签名——token 侧不认 GPU SKU 目录。"""
    defaults = defaults or {}
    base = cfg["base_url"].rstrip("/")
    eps = cfg["endpoints"]
    query = cfg.get("query") or {}
    view = query.get("view", "day")
    probe_top_n = int(query.get("provider_probe_top_n", 0) or 0)
    req = dict(timeout=defaults.get("timeout_seconds", 30),
               retries=defaults.get("retries", 3),
               backoff_base=defaults.get("backoff_base_seconds", 2),
               user_agent=defaults.get("user_agent", "ch-gpu-compute-monitor/1.0"))

    result = CollectResult(source=SOURCE)
    fp = query_fingerprint({"source": SOURCE, "view": view,
                            "coverage_scope": cfg.get("coverage_scope"),
                            "price_basis": cfg.get("price_basis"),
                            "join": "base+variant"})

    rankings = request_json(f"{base}{eps['rankings']}", params={"view": view}, **req)
    rows = (rankings or {}).get("data")
    if not isinstance(rows, list) or not rows:
        raise CollectorError(
            f"rankings 未返回预期的 data 数组（view={view}，拿到 {type(rows).__name__}）")

    pricing_payload = request_json(f"{base}{eps['models']}", **req)
    models = (pricing_payload or {}).get("data")
    if not isinstance(models, list) or not models:
        raise CollectorError("models 未返回预期的 data 数组，无法给量配价")

    index = build_price_index(models)
    if not index:
        raise CollectorError("models 返回了数据但一条都建不出价格索引，字段结构可能已改")

    # 应用榜是补强维度：拿不到只降级成一条 note，不拖垮 token 主路。
    apps_payload: Any = None
    apps_error: Optional[str] = None
    apps_path = eps.get("apps")
    if apps_path and query.get("collect_apps", True):
        try:
            apps_payload = request_json(f"{base}{apps_path}",
                                        params={"view": view}, **req)
        except CollectorError as exc:
            apps_error = str(exc)

    raw_ref = save_raw(SOURCE, obs_date, {"rankings": rankings, "models": models,
                                          "apps": apps_payload})
    result.raw_path = raw_ref

    # 观测日取数据自己的结算日（T-1），不是运行日。同 Ornn 的做法：
    # 行的日期是它自己的，跑批的日期只是我们什么时候去拿。
    # 响应级的结构问题必须显式失败；但「530 行里有一行是全零占位」不是结构问题，
    # 为它把一整天的采集判死，代价远大于收益（实测那一行 prompt/completion/requests
    # 全是 0，token 占比 0.0000%）。所以分两档：
    #   * 没有标识、也没有任何量  → 空占位行，跳过并记账
    #   * 没有标识、却带着真实的量 → 有量却无从归属，这是真的结构问题，失败
    placeholder_rows = 0
    kept: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CollectorError(
                f"rankings 第 {i} 行不是对象（拿到 {type(row).__name__}）")
        if not row.get("date"):
            raise CollectorError(f"rankings 第 {i} 行缺少必需字段 date")
        if not (row.get("model_permaslug") or "").strip():
            volume = ((row.get("total_prompt_tokens") or 0)
                      + (row.get("total_completion_tokens") or 0)
                      + (row.get("count") or 0))
            if volume:
                raise CollectorError(
                    f"rankings 行 {str(row.get('date'))[:10]} 缺少 model_permaslug，"
                    f"却带着 {volume} 的量——有量却无从归属，不能静默丢")
            placeholder_rows += 1
            result.unmapped.append("<empty model_permaslug>")
            continue
        kept.append(row)
    if not kept:
        raise CollectorError("rankings 里没有任何带标识的行")
    if placeholder_rows:
        result.notes.append(
            f"跳过 {placeholder_rows} 行无标识的全零占位行（不带任何量，已记进 unmapped）")
    rows = kept
    dates = sorted({str(r["date"])[:10] for r in rows})
    settled = dates[-1]
    if view == "day" and len(dates) > 1:
        result.notes.append(
            f"view=day 返回了 {len(dates)} 个日期（{dates[0]}…{dates[-1]}），"
            f"只取最新的 {settled}，其余按非当日观测丢弃")

    staged: List[Dict[str, Any]] = []
    unmatched_tokens = 0
    total_tokens = 0
    for row in rows:
        if str(row.get("date"))[:10] != settled:
            continue
        slug = (row.get("model_permaslug") or "").strip()
        variant = (row.get("variant") or "standard").strip() or "standard"
        identity = f"{settled} {slug}:{variant}"
        prompt_tokens = _required_nonnegative_int(
            row, "total_prompt_tokens", identity)
        completion_tokens = _required_nonnegative_int(
            row, "total_completion_tokens", identity)
        requests = _required_nonnegative_int(row, "count", identity)
        volume = prompt_tokens + completion_tokens
        total_tokens += volume

        entry, how = lookup(index, slug, variant)
        if entry is None:
            unmatched_tokens += volume
            result.unmapped.append(f"{slug}:{variant}")
            staged.append({"row": row, "slug": slug, "variant": variant,
                           "entry": None, "how": how, "spend": None,
                           "prompt_tokens": prompt_tokens,
                           "completion_tokens": completion_tokens,
                           "requests": requests})
            continue
        p_prompt = _price(entry, "prompt")
        p_completion = _price(entry, "completion")
        missing_sides = []
        if prompt_tokens > 0 and p_prompt is None:
            missing_sides.append("prompt")
        if completion_tokens > 0 and p_completion is None:
            missing_sides.append("completion")
        if missing_sides:
            raise CollectorError(
                f"{slug}:{variant} 匹配到 {entry.get('id')}，但有对应 token 的价格字段缺失："
                f"{', '.join(missing_sides)}")
        spend = (prompt_tokens * (p_prompt or 0.0)
                 + completion_tokens * (p_completion or 0.0))
        staged.append({"row": row, "slug": slug, "variant": variant, "entry": entry,
                       "how": how, "spend": spend, "prompt_tokens": prompt_tokens,
                       "completion_tokens": completion_tokens, "requests": requests})

    if not staged:
        raise CollectorError(f"rankings 里没有 {settled} 这一天的行")

    # 逐 provider 价差带只给 spend 前 N 名拉：它是给默认价定不确定区间用的补强，
    # 不是核心数据，失败降级成一条 note，不拖垮整次采集。
    bands: Dict[str, Dict[str, Any]] = {}
    if probe_top_n > 0:
        ranked = sorted((s for s in staged if s["spend"]),
                        key=lambda s: -(s["spend"] or 0))[:probe_top_n]
        for item in ranked:
            model_id, _ = split_variant(item["entry"].get("id") or "")
            if not model_id or model_id in bands:
                continue
            try:
                band = _provider_band(base, eps["endpoints_of"], model_id, req)
            except CollectorError as exc:
                result.notes.append(f"provider 价差带取数失败 {model_id}：{exc}")
                continue
            if band:
                bands[model_id] = band

    for item in staged:
        entry = item["entry"]
        row = item["row"]
        band: Dict[str, Any] = {}
        if entry is not None:
            model_id, _ = split_variant(entry.get("id") or "")
            band = bands.get(model_id) or {}
        p_prompt = _price(entry, "prompt") if entry is not None else None
        p_completion = _price(entry, "completion") if entry is not None else None
        p_cache = _price(entry, "input_cache_read") if entry is not None else None
        is_priced = None
        if entry is not None:
            is_priced = bool((p_prompt or 0) > 0 or (p_completion or 0) > 0)
        result.tokens.append(token_row(
            obs_date=settled,
            source=SOURCE,
            model_family=strip_date(item["slug"]),
            model_slug=item["slug"],
            variant=item["variant"],
            coverage_scope=cfg.get("coverage_scope", "gateway"),
            price_basis=cfg.get("price_basis", "list"),
            prompt_tokens=item["prompt_tokens"],
            completion_tokens=item["completion_tokens"],
            requests=item["requests"],
            price_prompt_usd_per_mtok=None if p_prompt is None else p_prompt * PER_MTOK,
            price_completion_usd_per_mtok=(None if p_completion is None
                                           else p_completion * PER_MTOK),
            price_cache_read_usd_per_mtok=None if p_cache is None else p_cache * PER_MTOK,
            spend_usd=item["spend"],
            is_priced=is_priced,
            price_match=item["how"],
            provider_price_min_usd_per_mtok=band.get("min"),
            provider_price_median_usd_per_mtok=band.get("median"),
            provider_price_max_usd_per_mtok=band.get("max"),
            provider_count=band.get("count"),
            query_fingerprint_=fp,
            raw_ref=raw_ref,
        ))

    # 应用维度：观测日沿用模型榜的结算日——响应里没有 date，而两者是同站同 view。
    #
    # 整段被一个 try 包住，而且只有全部构造成功才写进 result.apps：应用榜是补强，
    # 它坏了只能少一个维度，不能让 500 多行核心模型观测跟着不落库；半截行也不许
    # 落库——那会在库里留下一份残缺的当日榜单，比没有更难发现。
    # 这里刻意捕获 Exception 而不只是 CollectorError：主路的安全比"异常类型纯洁"
    # 重要，但类型名会写进 note，坏了看得见。
    if apps_error:
        result.notes.append(f"应用榜取数失败，本次不出调用方维度：{apps_error}")
    elif apps_payload is not None:
        try:
            app_rows, hidden_ranks = parse_app_rows(apps_payload, view)
            app_fp = query_fingerprint({"source": SOURCE, "dataset": "apps",
                                        "view": view,
                                        "coverage_scope": cfg.get("coverage_scope"),
                                        "listing": "public_ranked"})
            built, listed_tokens = build_app_rows(
                app_rows, settled=settled,
                coverage_scope=cfg.get("coverage_scope", "gateway"),
                fingerprint=app_fp, raw_ref=raw_ref)
        except Exception as exc:  # noqa: BLE001 —— 补强维度不许拖垮核心 token 采集
            result.notes.append(
                f"应用榜异常，本次不出调用方维度（{type(exc).__name__}: {exc}）；"
                f"模型 × 变体的核心观测不受影响")
        else:
            result.apps.extend(built)
            share = (listed_tokens / total_tokens * 100) if total_tokens else 0.0
            result.notes.append(
                f"调用方维度 {len(result.apps)} 个应用，合计 "
                f"{listed_tokens / 1e12:.2f}T token，占当日全站 {share:.1f}%")
            if hidden_ranks:
                result.notes.append(
                    f"应用榜有 {hidden_ranks} 个名次不公开露出（返回 {len(app_rows)} 行、"
                    f"最大 rank 更靠后）——这不是前 {len(app_rows)} 名，求和只是下界")

    matched_share = (1 - unmatched_tokens / total_tokens) if total_tokens else 0.0
    result.notes.append(
        f"{settled}：{len(result.tokens)} 行（模型 × 变体），token 加权匹配率 "
        f"{matched_share * 100:.2f}%，逐 provider 价差带 {len(bands)} 个模型")

    # 零价与集中度当场报出来：这两件事决定了"量涨了"这句话能不能说。
    free_tokens = zero_std_tokens = 0
    by_key: Dict[str, int] = {}
    for row in result.tokens:
        volume = (row.get("prompt_tokens") or 0) + (row.get("completion_tokens") or 0)
        by_key[f"{row['model_slug']}:{row['variant']}"] = volume
        if row.get("price_match") == "unmatched":
            continue
        if not row.get("is_priced"):
            if row["variant"] == "free":
                free_tokens += volume
            else:
                zero_std_tokens += volume
    if total_tokens:
        zero_total = free_tokens + zero_std_tokens
        result.notes.append(
            f"零价 token 占 {zero_total / total_tokens * 100:.1f}%："
            f"free 变体 {free_tokens / total_tokens * 100:.1f}%、"
            f"零价 standard（stealth 免费放量）{zero_std_tokens / total_tokens * 100:.1f}%")
        if by_key:
            top_key, top_volume = max(by_key.items(), key=lambda kv: kv[1])
            result.notes.append(
                f"单模型最大份额 {top_volume / total_tokens * 100:.1f}%（{top_key}）"
                f"——份额过高时它进出榜单就能让总量序列跳一大截")
    if view != "day":
        result.notes.append(
            f"view={view} 是滚动窗口总量，不是单日量；它不该进日度序列")
    return result


def settled_points(points: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]],
                                                          Dict[str, Any]]:
    """按时间排序，并把最后一个点摘出来当作「未结算」丢弃。

    为什么一定要丢：相隔几分钟的两次取数，最后一点从 2.78443e13 降到 2.75664e13。
    **会往下走**说明它是个滑动窗口而不是在累积——存进库就是存了一个随取数时刻
    变化的数。倒数第二点两次逐键一致，已结算的周是冻结的。
    """
    ordered = sorted(points, key=lambda p: str(p["x"]))
    return ordered[:-1], ordered[-1]


def collect_history(cfg: Dict[str, Any], defaults: Optional[Dict] = None) -> CollectResult:
    """回填：厂商级的周度 token 量，实测 52 个点、回到 2025-09-01。

    这是 OpenRouter 唯一一条有真历史的序列，但它和日榜**不是一个口径**，
    两条硬事实决定了它只能单独存、单独读：

      1. **最后一个点是活的。** 相隔几分钟的两次取数，最后一点从 2.78443e13
         降到 2.75664e13——会往下走说明它是个滑动窗口而不是在累积。倒数第二点
         两次逐键一致，说明已结算的周是冻结的。所以**最后一个点一律丢弃**。
      2. **和日榜对不上。** 同一天按厂商比对，比值从 1.42（xiaomi）到 1.75（google），
         不是常数倍，成因未定（大概率是跨模型可比的归一化计数）。所以
         `unit_basis` 标成 `provider_reported_unverified`：**只能看份额与增速，
         不能读绝对水平，更不能和日度序列相减。**

    粒度只有厂商级（Top 9 + others），实测所有 grouping / by / level 参数都不改变
    返回的键集合，拿不到模型级历史。
    """
    defaults = defaults or {}
    base = cfg["base_url"].rstrip("/")
    eps = cfg["endpoints"]
    path = eps.get("market_share")
    if not path:
        raise CollectorError("sources.yaml 里没有配 endpoints.market_share，无法回填")
    req = dict(timeout=defaults.get("timeout_seconds", 30),
               retries=defaults.get("retries", 3),
               backoff_base=defaults.get("backoff_base_seconds", 2),
               user_agent=defaults.get("user_agent", "ch-gpu-compute-monitor/1.0"))

    payload = request_json(f"{base}{path}", **req)
    points = (payload or {}).get("data")
    if not isinstance(points, list) or not points:
        raise CollectorError("market-share 未返回预期的 data 数组")
    for point in points:
        if "x" not in point or not isinstance(point.get("ys"), dict):
            raise CollectorError(
                f"market-share 的点缺 x 或 ys，字段结构可能已改：{str(point)[:200]}")

    result = CollectResult(source=SOURCE)
    ordered, live = settled_points(points)
    if not ordered:
        raise CollectorError("market-share 只回了一个点，丢掉未结算的那个就什么都不剩")
    raw_ref = save_raw(f"{SOURCE}-history", str(live["x"]), payload)
    result.raw_path = raw_ref
    fp = query_fingerprint({"source": SOURCE, "dataset": "market-share",
                            "grain": "author_weekly"})

    for point in ordered:
        week = str(point["x"])[:10]
        for author, tokens in point["ys"].items():
            result.history.append(token_history_row(
                week_start=week, source=SOURCE, author=author,
                tokens=int(tokens), coverage_scope=cfg.get("coverage_scope", "gateway"),
                settled=True, query_fingerprint_=fp, raw_ref=raw_ref))

    weeks = sorted({r["week_start"] for r in result.history})
    result.notes.append(
        f"厂商级周度历史 {len(weeks)} 周（{weeks[0]} … {weeks[-1]}），"
        f"{len(result.history)} 行；已丢弃未结算的最新点 {str(live['x'])[:10]}")
    result.notes.append(
        "口径提示：这条序列与日榜对不上（实测同日各厂商比值 1.42–1.75，非常数倍），"
        "只能看份额与增速，不能读绝对水平，也不能与日度序列相减")
    gaps = [(weeks[i], weeks[i + 1]) for i in range(len(weeks) - 1)
            if (date.fromisoformat(weeks[i + 1])
                - date.fromisoformat(weeks[i])).days != 7]
    if gaps:
        result.notes.append(f"周间隔异常（不是 7 天）：{gaps[:5]}")
    return result
