"""Lambda Cloud 采集器 —— ④标准报价锚，兼 region 容量覆盖度。

无 key 直接 401（实测），没有匿名降级路径，所以缺 key 时抛 CollectorError，
让当天该源如实记成"缺"，而不是悄悄跳过——跳过会让 supply_breadth 的分母
无声变小，读起来像"有货平台变少了"。

Lambda 卖的是整机实例（1x / 8x），price_cents_per_hour 是整机价。
必须除以实例的 GPU 数换算成 USD/GPU·hour，并把 node_gpu_count 一起存下来，
否则哪天 Lambda 改了实例规格，历史序列会在不知不觉中断层。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from .base import (
    CollectResult,
    CollectorError,
    price_row,
    query_fingerprint,
    request_json,
    save_raw,
    supply_row,
)

SOURCE = "lambda"

_GPU_COUNT_RE = re.compile(r"gpu_(\d+)x_", re.IGNORECASE)


def _gpu_count(instance_name: str, spec: Dict[str, Any]) -> Optional[int]:
    """从实例规格拿 GPU 数；规格里没有就退回名字里的 Nx。两个都拿不到返回 None。"""
    for field in ("gpus", "gpu_count"):
        value = spec.get(field)
        if isinstance(value, int) and value > 0:
            return value
    match = _GPU_COUNT_RE.search(instance_name or "")
    return int(match.group(1)) if match else None


def collect(cfg: Dict[str, Any], catalog, obs_date: str,
            defaults: Optional[Dict] = None) -> CollectResult:
    defaults = defaults or {}
    key = os.environ.get(cfg.get("api_key_env") or "", "")
    if not key:
        raise CollectorError(
            f"缺少 {cfg.get('api_key_env')}；Lambda 无匿名接口，当天该源记为缺采")

    base = cfg["base_url"].rstrip("/")
    url = f"{base}{cfg['endpoints']['instance_types']}"
    req = dict(timeout=defaults.get("timeout_seconds", 30),
               retries=defaults.get("retries", 3),
               backoff_base=defaults.get("backoff_base_seconds", 2),
               user_agent=defaults.get("user_agent", "ch-gpu-compute-monitor/1.0"),
               headers={"Authorization": f"Bearer {key}"})

    payload = request_json(url, **req)
    data = (payload or {}).get("data")
    if not isinstance(data, dict) or not data:
        raise CollectorError(f"instance-types 返回结构异常: {str(payload)[:200]}")

    fp = query_fingerprint({"source": SOURCE, "endpoint": url})
    result = CollectResult(source=SOURCE)
    result.raw_path = save_raw(SOURCE, obs_date, payload)

    # 同一 canonical SKU 可能对应 1x 与 8x 两种实例；取单卡价更低的那个作为该 SKU 的挂牌价，
    # 同时把两者的 region 覆盖合并。
    best: Dict[str, Dict[str, Any]] = {}
    for name, entry in data.items():
        model = catalog.resolve(SOURCE, name)
        if model is None:
            result.unmapped.append(name)
            continue
        spec = (entry.get("instance_type") or {})
        cents = spec.get("price_cents_per_hour")
        if cents is None:
            result.notes.append(f"{name}: 无 price_cents_per_hour，跳过")
            continue
        gpus = _gpu_count(name, (spec.get("specs") or {}))
        if not gpus:
            # 拿不到 GPU 数就无法换算成单卡价；宁可不入库也不猜一个 8
            result.notes.append(f"{name}: 取不到实例 GPU 数，无法换算 USD/GPU·hour，跳过")
            continue
        unit = (float(cents) / 100.0) / gpus
        regions = [r.get("name") for r in (entry.get("regions_with_capacity_available") or [])]
        slot = best.get(model)
        if slot is None or unit < slot["unit"]:
            best[model] = {"unit": unit, "gpus": gpus, "instance": name,
                           "regions": set(regions)}
        else:
            slot["regions"] |= set(regions)

    if not best:
        raise CollectorError(
            "instance-types 里一个目标 SKU 都没匹配上——核对 config/gpu_catalog.yaml 的 lambda 别名")

    for model, slot in sorted(best.items()):
        result.prices.append(price_row(
            obs_date=obs_date, source=SOURCE, gpu_model=model, price_type="on_demand",
            price=slot["unit"], node_gpu_count=slot["gpus"],
            unit_basis=f"node_hour/{slot['gpus']}gpu", price_scope="bundled",
            query_fingerprint_=fp, raw_ref=f"{url}#{slot['instance']}"))
        regions = sorted(r for r in slot["regions"] if r)
        result.supply.append(supply_row(
            obs_date=obs_date, source=SOURCE, gpu_model=model,
            available_region_count=len(regions),
            stock_status="High" if regions else "None",
            capacity_detail={"regions": regions, "instance": slot["instance"]},
            query_fingerprint_=fp, raw_ref=url,
            quality_flag="ok" if regions else "no_stock"))
    return result
