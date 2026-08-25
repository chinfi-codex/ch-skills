"""Vast.ai 采集器 —— ②即时 Offer 与 ③市场聚合报价的原始粒度，外加供给深度。

三个实测出来的坑，全部在这里处理，别在别处重复处理：

1. dph_total 是「整个 offer」的小时价，不是单卡价。一条 num_gpus=8 的 offer
   报 40 美元，单卡是 5 美元。所有价格一律除以 num_gpus 后入库，并把
   num_gpus 存进 node_gpu_count，让换算可审计（PRD §9 明确要求）。
2. dph_total 里裹着磁盘费与 SLA 溢价，跨平台不可比。offer 的 search 子对象
   把它拆开了：gpuCostPerHour 是纯 GPU 费。默认取纯 GPU 费（price_scope=gpu_only）。
3. is_bid=true 的是可抢占档（竞价），和按需档是两个市场。混进同一组分位数，
   分布会凭空多出一条低价尾巴，看着像"报价整体下移"。分成两个 market_segment。

另外：offer 数是查询口径的函数，不是市场普查。不带 gpu_name 过滤时实测只回
64 条，带过滤反而回 47 条（仅三个 SKU）。所以 offer_count 的绝对值没有跨口径
意义，只有同一 query_fingerprint 下的时间序列才可比；份额（offer_share）比
绝对数更能剔掉平台自身规模的影响。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .base import (
    CollectResult,
    CollectorError,
    percentile,
    price_row,
    query_fingerprint,
    request_json,
    save_raw,
    supply_row,
)

SOURCE = "vast"


def _headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    key = os.environ.get(cfg.get("api_key_env") or "", "")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _unit_price(offer: Dict[str, Any], scope: str) -> Optional[float]:
    """把一条 offer 折成 USD/GPU·hour。拿不到可信数值返回 None。"""
    num_gpus = offer.get("num_gpus")
    if not isinstance(num_gpus, int) or num_gpus < 1:
        return None
    search = offer.get("search") or {}
    if scope == "gpu_only":
        total = search.get("gpuCostPerHour")
        if total is None:
            total = offer.get("dph_base")
    else:
        total = search.get("totalHour", offer.get("dph_total"))
    if total is None:
        return None
    try:
        value = float(total) / num_gpus
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def _keep(offer: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    """质量过滤。低可靠性 / 已掉验证的机器不进分位数。

    为什么要过滤：Vast 是 C2C 市场，垃圾供给涌入会把 P25 压下去，
    读起来像"市场在降价"，其实只是低质机器变多了。
    """
    if filters.get("exclude_deverified") and offer.get("verification") == "deverified":
        return False
    floor = filters.get("min_reliability")
    if floor is not None:
        rel = offer.get("reliability2", offer.get("reliability"))
        if rel is None or float(rel) < float(floor):
            return False
    return True


def collect(cfg: Dict[str, Any], catalog, obs_date: str,
            defaults: Optional[Dict] = None) -> CollectResult:
    defaults = defaults or {}
    base = cfg["base_url"].rstrip("/")
    qcfg = cfg.get("query") or {}
    filters = cfg.get("filters") or {}
    scope = cfg.get("price_scope", "gpu_only")
    min_n = int(cfg.get("min_sample_for_quantiles", 8))
    req = dict(timeout=defaults.get("timeout_seconds", 30),
               retries=defaults.get("retries", 3),
               backoff_base=defaults.get("backoff_base_seconds", 2),
               user_agent=defaults.get("user_agent", "ch-gpu-compute-monitor/1.0"),
               headers=_headers(cfg))

    wanted = catalog.source_aliases(SOURCE)
    query = {"rentable": {"eq": bool(qcfg.get("rentable", True))},
             "gpu_name": {"in": wanted},
             "limit": int(qcfg.get("limit", 1000))}
    fp = query_fingerprint({"source": SOURCE, "query": query, "filters": filters,
                            "price_scope": scope})

    url = f"{base}/bundles/"
    payload = request_json(url, params={"q": json.dumps(query)}, **req)
    offers = (payload or {}).get("offers")
    if not isinstance(offers, list):
        raise CollectorError(f"bundles 返回结构异常（无 offers 数组）: {str(payload)[:200]}")

    result = CollectResult(source=SOURCE)
    result.raw_path = save_raw(SOURCE, obs_date, {"query": query, "offer_count": len(offers),
                                                  "offers": offers})
    if not offers:
        # 空结果是真实可能的（三个 SKU 都无货），但要显式标出来而不是当成 0 价
        result.notes.append("查询返回 0 条 offer；当天 Vast 侧记为无样本，不是价格为 0")

    # 分桶：(canonical model, segment)
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    dropped_quality = 0
    for offer in offers:
        model = catalog.resolve(SOURCE, offer.get("gpu_name"))
        if model is None:
            result.unmapped.append(str(offer.get("gpu_name")))
            continue
        if not _keep(offer, filters):
            dropped_quality += 1
            continue
        segment = "interruptible" if offer.get("is_bid") else "on_demand"
        if segment == "interruptible" and not qcfg.get("include_interruptible", True):
            continue
        buckets.setdefault(f"{model}||{segment}", []).append(offer)

    if dropped_quality:
        result.notes.append(
            f"质量过滤剔除 {dropped_quality} 条 offer"
            f"（reliability < {filters.get('min_reliability')} 或已掉验证）")

    kept_total = sum(len(v) for v in buckets.values())

    for key, group in sorted(buckets.items()):
        model, segment = key.split("||")
        priced = [(o, _unit_price(o, scope)) for o in group]
        values = [p for _, p in priced if p is not None]
        gpu_total = sum(int(o.get("num_gpus") or 0) for o, p in priced if p is not None)
        n = len(values)
        if n == 0:
            result.notes.append(f"{model}/{segment}: {len(group)} 条 offer 都算不出单卡价")
            continue

        common = dict(obs_date=obs_date, source=SOURCE, gpu_model=model,
                      market_segment=segment, sample_count=n, price_scope=scope,
                      unit_basis="gpu_hour_from_offer", query_fingerprint_=fp,
                      raw_ref=f"{url}?q={json.dumps(query)}")
        result.prices.append(price_row(price_type="offer_min", price=min(values), **common))
        # 样本太薄时分位数没有统计意义，宁可不出数也不要给一个假的中枢。
        if n >= min_n:
            for ptype, q in (("offer_p25", 0.25), ("offer_median", 0.50), ("offer_p75", 0.75)):
                result.prices.append(price_row(price_type=ptype, price=percentile(values, q),
                                               **common))
        else:
            result.notes.append(
                f"{model}/{segment}: 样本 {n} 条 < {min_n} 条门槛，只出 min 不出 P25/中位/P75")

        result.supply.append(supply_row(
            obs_date=obs_date, source=SOURCE, gpu_model=model, market_segment=segment,
            offer_count=n, available_gpu_count=gpu_total,
            source_total_offer_count=kept_total,
            offer_share=(n / kept_total) if kept_total else None,
            capacity_detail={"geolocations": sorted({str(o.get("geolocation"))
                                                     for o, p in priced if p is not None})},
            available_region_count=len({str(o.get("geolocation"))
                                        for o, p in priced if p is not None}),
            query_fingerprint_=fp, raw_ref=url,
            quality_flag="ok" if n >= min_n else "thin_sample"))

    return result
