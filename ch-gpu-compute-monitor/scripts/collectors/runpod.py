"""Runpod 采集器 —— 库存分级（stockStatus）的主要观察源，兼一路标准报价。

三个实测要点：

1. secure（自营数据中心）与 community（P2P 众包）是两个不同的市场，
   同一天 H100 SXM 实测 3.29 vs 2.69。合并成一个"Runpod 价"会让数字随
   两个市场的相对可得性漂移。分成两个 market_segment 存。
2. minimumBidPrice 常常等于 uninterruptablePrice（实测三个目标 SKU 全部相等）。
   相等时不能把它记成 spot 报价，否则会算出一个恒等于 0 的假折价。只有
   严格小于时才写 price_type=spot。
3. lowestPrice 全 null 不等于接口坏了，而是该 gpuCount 下当前无货
   （实测 MI300X、A100 PCIe 就是全 null）。这种情况写 stock_status=None
   并把 quality_flag 标成 no_stock，与"采集失败"区分开。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .base import (
    CollectResult,
    CollectorError,
    price_row,
    query_fingerprint,
    request_json,
    save_raw,
    supply_row,
)

SOURCE = "runpod"

QUERY = """
query GpuTypes {
  gpuTypes {
    id
    displayName
    memoryInGb
    maxGpuCount
    securePrice
    communityPrice
    oneMonthPrice
    threeMonthPrice
    lowestPrice(input: {gpuCount: 1}) {
      minimumBidPrice
      uninterruptablePrice
      stockStatus
    }
  }
}
""".strip()

_STOCK_RANK = {"None": 0, "Low": 1, "Medium": 2, "High": 3}


def stock_rank(status: Optional[str]) -> Optional[int]:
    """库存档位转序数，供指标层比较"是不是在升档"。认不出返回 None。"""
    return _STOCK_RANK.get(str(status)) if status is not None else None


def collect(cfg: Dict[str, Any], catalog, obs_date: str,
            defaults: Optional[Dict] = None) -> CollectResult:
    defaults = defaults or {}
    key = os.environ.get(cfg.get("api_key_env") or "", "")
    url = cfg["base_url"]
    params = {"api_key": key} if key else None
    req = dict(timeout=defaults.get("timeout_seconds", 30),
               retries=defaults.get("retries", 3),
               backoff_base=defaults.get("backoff_base_seconds", 2),
               user_agent=defaults.get("user_agent", "ch-gpu-compute-monitor/1.0"))

    fp = query_fingerprint({"source": SOURCE, "query": QUERY, "gpu_count": 1,
                            "keyed": bool(key)})
    payload = request_json(url, method="POST", params=params,
                           json_body={"query": QUERY}, **req)
    if not isinstance(payload, dict) or "data" not in payload:
        raise CollectorError(f"GraphQL 返回结构异常: {str(payload)[:200]}")
    if payload.get("errors"):
        raise CollectorError(f"GraphQL 报错: {str(payload['errors'])[:300]}")
    gpu_types = (payload.get("data") or {}).get("gpuTypes")
    if not isinstance(gpu_types, list) or not gpu_types:
        raise CollectorError("gpuTypes 为空，视为结构变化而非市场无货")

    result = CollectResult(source=SOURCE)
    result.raw_path = save_raw(SOURCE, obs_date, payload)

    matched = 0
    for entry in gpu_types:
        model = catalog.resolve(SOURCE, entry.get("id"))
        if model is None:
            continue
        matched += 1
        lowest = entry.get("lowestPrice") or {}
        on_demand = lowest.get("uninterruptablePrice")
        bid = lowest.get("minimumBidPrice")
        stock = lowest.get("stockStatus")
        raw_ref = f"{url}#gpuTypes/{entry.get('id')}"
        no_stock = on_demand is None and stock is None

        common = dict(obs_date=obs_date, source=SOURCE, gpu_model=model,
                      node_gpu_count=1, unit_basis="gpu_hour",
                      price_scope="gpu_only", query_fingerprint_=fp, raw_ref=raw_ref)

        # community / secure 是两个市场，各自的挂牌价分开存
        for segment, field in (("community", "communityPrice"), ("secure", "securePrice")):
            value = entry.get(field)
            if value is None:
                continue
            result.prices.append(price_row(price_type="on_demand", price=value,
                                           market_segment=segment, **common))

        # lowestPrice 是跨 segment 的"当前最低可得"，单列一个 segment 避免与上面混淆
        if on_demand is not None:
            result.prices.append(price_row(price_type="on_demand", price=on_demand,
                                           market_segment="lowest", **common))
        # 只有严格低于按需价，竞价档才是真的折价
        if bid is not None and on_demand is not None and float(bid) < float(on_demand):
            result.prices.append(price_row(price_type="spot", price=bid,
                                           market_segment="lowest", **common))
        elif bid is not None and on_demand is not None:
            result.notes.append(
                f"{model}: minimumBidPrice 等于按需价（{bid}），不记为 spot")

        committed = entry.get("threeMonthPrice")
        if committed is not None:
            # 承诺期价单独存，绝不能混进"按需价"去比降价
            result.prices.append(price_row(price_type="committed_3m", price=committed,
                                           market_segment="secure", **common))

        result.supply.append(supply_row(
            obs_date=obs_date, source=SOURCE, gpu_model=model, market_segment="lowest",
            stock_status=stock,
            capacity_detail={"max_gpu_count": entry.get("maxGpuCount"),
                             "stock_rank": stock_rank(stock),
                             "secure_cloud_price": entry.get("securePrice"),
                             "community_cloud_price": entry.get("communityPrice")},
            query_fingerprint_=fp, raw_ref=raw_ref,
            quality_flag="no_stock" if no_stock else "ok"))

    if matched == 0:
        raise CollectorError(
            "gpuTypes 里一个目标 SKU 都没匹配上——多半是平台改了 id 命名，"
            "请核对 config/gpu_catalog.yaml 的 runpod 别名")
    return result
