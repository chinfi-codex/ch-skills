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

    raw_ref = save_raw(SOURCE, obs_date, {"rankings": rankings, "models": models})
    result.raw_path = raw_ref

    # 观测日取数据自己的结算日（T-1），不是运行日。同 Ornn 的做法：
    # 行的日期是它自己的，跑批的日期只是我们什么时候去拿。
    dates = sorted({str(r.get("date"))[:10] for r in rows if r.get("date")})
    if not dates:
        raise CollectorError("rankings 每一行都没有 date 字段，无法定锚")
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
        if not slug:
            result.unmapped.append("<empty model_permaslug>")
            continue
        variant = (row.get("variant") or "standard").strip() or "standard"
        prompt_tokens = int(row.get("total_prompt_tokens") or 0)
        completion_tokens = int(row.get("total_completion_tokens") or 0)
        volume = prompt_tokens + completion_tokens
        total_tokens += volume

        entry, how = lookup(index, slug, variant)
        if entry is None:
            unmatched_tokens += volume
            result.unmapped.append(f"{slug}:{variant}")
            staged.append({"row": row, "slug": slug, "variant": variant,
                           "entry": None, "how": how, "spend": None,
                           "prompt_tokens": prompt_tokens,
                           "completion_tokens": completion_tokens})
            continue
        p_prompt = _price(entry, "prompt")
        p_completion = _price(entry, "completion")
        if p_prompt is None and p_completion is None:
            raise CollectorError(
                f"{slug}:{variant} 匹配到 {entry.get('id')} 但 pricing 里既无 prompt "
                f"也无 completion，价格字段结构可能已改")
        spend = (prompt_tokens * (p_prompt or 0.0)
                 + completion_tokens * (p_completion or 0.0))
        staged.append({"row": row, "slug": slug, "variant": variant, "entry": entry,
                       "how": how, "spend": spend, "prompt_tokens": prompt_tokens,
                       "completion_tokens": completion_tokens})

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
            requests=int(row.get("count") or 0) or None,
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
