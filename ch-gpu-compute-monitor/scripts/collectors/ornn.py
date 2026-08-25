"""Ornn OCPI 采集器 —— 四层价格里的①市场成交价（唯一一个基于真实成交的锚）。

两个必须分开的口径（把它们混进一条序列是最容易犯也最难查的错）：
  * /api/daily-index      —— 已结算的日度收盘，T-1 落定，写成 transaction_index
  * /api/gpu/:gpuName     —— 小时级实时值，随时在动，写成 transaction_live
日线图只能用 transaction_index；transaction_live 只配出现在"最新价"那个数字上。

免费层给的是滚动 3 个月日度历史（实测 92 个点），正好等于首页要的 90D 窗口。
这意味着 Ornn 这一路上线当天就有完整历史，不用等积累——而 Vast/Runpod/Lambda
只能从今天开始攒。这个不对称必须在报告里说清楚，别让读者以为四条线同龄。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .base import (
    CollectResult,
    CollectorError,
    price_row,
    query_fingerprint,
    request_json,
    save_raw,
)

SOURCE = "ornn"


def _headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    key = os.environ.get(cfg.get("api_key_env") or "", "")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _to_date(value: str) -> str:
    """Ornn 的 timestamp 是 UTC ISO；结算日取其 UTC 日期。"""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc).date().isoformat()


def collect(cfg: Dict[str, Any], catalog, obs_date: str,
            history_days: int = 120, defaults: Optional[Dict] = None) -> CollectResult:
    defaults = defaults or {}
    base = cfg["base_url"].rstrip("/")
    eps = cfg["endpoints"]
    headers = _headers(cfg)
    req = dict(timeout=defaults.get("timeout_seconds", 30),
               retries=defaults.get("retries", 3),
               backoff_base=defaults.get("backoff_base_seconds", 2),
               user_agent=defaults.get("user_agent", "ch-gpu-compute-monitor/1.0"),
               headers=headers)

    result = CollectResult(source=SOURCE)
    fp = query_fingerprint({"source": SOURCE, "history_days": history_days,
                            "keyed": bool(headers)})
    raw_bundle: Dict[str, Any] = {}

    # 1) 先问平台自己"哪些 SKU 现在拿得到" —— 文档明确要求不要把这份名单写死。
    free_list = request_json(f"{base}{eps['gpu_types_free']}", **req)
    if not isinstance(free_list, dict) or not free_list.get("success"):
        raise CollectorError(f"gpu-types-free 返回结构异常: {str(free_list)[:200]}")
    available = {str(x.get("gpu_name")) for x in (free_list.get("data") or [])}
    raw_bundle["gpu_types_free"] = free_list
    if headers:
        try:
            all_list = request_json(f"{base}{eps['gpu_types']}", **req)
            available |= {str(x.get("gpu_name")) for x in (all_list.get("data") or [])}
            raw_bundle["gpu_types"] = all_list
        except CollectorError as exc:
            result.notes.append(f"带 key 取全量 SKU 失败，退回免费名单：{exc}")

    wanted = catalog.source_aliases(SOURCE)
    reachable = [n for n in wanted if n in available]
    missing = [n for n in wanted if n not in available]
    if missing:
        result.notes.append(
            f"以下 SKU 当前不在 Ornn 可取名单内（可能需要 API key）：{', '.join(missing)}")
    if not reachable:
        raise CollectorError(f"目标 SKU 一个都取不到；平台当前提供：{sorted(available)}")

    start = (datetime.fromisoformat(obs_date).date() - timedelta(days=history_days)).isoformat()

    for raw_name in reachable:
        model = catalog.resolve(SOURCE, raw_name)
        if model is None:
            result.unmapped.append(raw_name)
            continue

        # 2) 日度结算历史 —— 时间序列只认这一路
        hist_url = f"{base}{eps['history'].format(gpu=raw_name)}"
        hist = request_json(hist_url, params={"startDate": start, "endDate": obs_date}, **req)
        points = (hist or {}).get("data")
        if not isinstance(points, list):
            raise CollectorError(f"{raw_name} index-history 结构异常: {str(hist)[:200]}")
        raw_bundle[f"history::{raw_name}"] = {"count": len(points),
                                              "first": points[0] if points else None,
                                              "last": points[-1] if points else None}
        for point in points:
            value = point.get("index_value")
            if value is None:
                continue
            result.prices.append(price_row(
                obs_date=_to_date(point["timestamp"]), source=SOURCE, gpu_model=model,
                price_type="transaction_index", price=value,
                observed_at=str(point["timestamp"]), unit_basis="gpu_hour",
                price_scope="gpu_only", query_fingerprint_=fp,
                raw_ref=f"{hist_url}?startDate={start}&endDate={obs_date}"))

        # 3) 小时级实时值 —— 只作为"当前价"，单独一个 price_type
        try:
            cur = request_json(f"{base}{eps['current'].format(gpu=raw_name)}", **req)
            data = (cur or {}).get("data") or {}
            if data.get("index_value") is not None:
                raw_bundle[f"current::{raw_name}"] = data
                result.prices.append(price_row(
                    obs_date=obs_date, source=SOURCE, gpu_model=model,
                    price_type="transaction_live", price=data["index_value"],
                    observed_at=str(data.get("last_updated") or ""),
                    query_fingerprint_=fp,
                    raw_ref=f"{base}{eps['current'].format(gpu=raw_name)}"))
        except CollectorError as exc:
            # 实时值缺失不影响日线，降级即可
            result.notes.append(f"{raw_name} 实时价取用失败（日线不受影响）：{exc}")

    result.raw_path = save_raw(SOURCE, obs_date, raw_bundle)
    return result
