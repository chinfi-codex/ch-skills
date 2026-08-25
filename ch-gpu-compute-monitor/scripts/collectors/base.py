"""采集器公共设施：HTTP 重试、原始响应落盘、查询指纹、结果容器。

设计约束（对应 PRD §9「网页结构发生变化时 Fetcher 必须失败显式化」）：
任何解析不出预期结构的情况都抛 CollectorError，绝不返回 0 或沿用前值。
上游 collect.py 会把失败记进 gpu_collect_runs，当天该源就是"缺"，
指标层会看见缺口并如实标出来，而不是拿一个假数字继续算。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

RAW_ROOT = Path(__file__).resolve().parents[2] / "raw"


class CollectorError(RuntimeError):
    """采集或解析失败。带上足够定位的信息，不吞异常。"""


@dataclass
class CollectResult:
    source: str
    prices: List[Dict[str, Any]] = field(default_factory=list)
    supply: List[Dict[str, Any]] = field(default_factory=list)
    unmapped: List[str] = field(default_factory=list)
    raw_path: Optional[str] = None
    attempts: int = 0
    latency_ms: int = 0
    notes: List[str] = field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def query_fingerprint(payload: Any) -> str:
    """把采集口径压成 12 位指纹。

    口径变了（过滤条件、limit、价格 scope）序列就不可比，指纹是判断依据。
    存进每一行观测，指标层比较前会检查两端指纹是否一致。
    """
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def save_raw(source: str, obs_date: str, payload: Any) -> str:
    """原始响应落盘，保留追溯能力（PRD §6.1 步骤 2）。"""
    out_dir = RAW_ROOT / obs_date
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{source}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path.relative_to(RAW_ROOT.parent))


def request_json(url: str, *, method: str = "GET", params: Optional[Dict] = None,
                 json_body: Optional[Dict] = None, headers: Optional[Dict] = None,
                 timeout: int = 30, retries: int = 3, backoff_base: float = 2.0,
                 user_agent: str = "ch-gpu-compute-monitor/1.0") -> Any:
    """带指数退避的 JSON 请求。全部重试用尽后抛 CollectorError。"""
    hdrs = {"User-Agent": user_agent, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(method, url, params=params, json=json_body,
                                    headers=hdrs, timeout=timeout)
            if resp.status_code == 401:
                raise CollectorError(f"{url} -> 401 未授权（缺 API key 或 key 无效）")
            if resp.status_code == 429:
                last_error = f"429 限流"
            elif resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            else:
                try:
                    return resp.json()
                except ValueError as exc:
                    # 返回了 HTML（通常是登录页或改版后的 404），这是结构变化，必须显式失败
                    raise CollectorError(
                        f"{url} 返回非 JSON（前 200 字符: {resp.text[:200]!r}）"
                    ) from exc
        except CollectorError:
            raise
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(backoff_base ** (attempt - 1))
    raise CollectorError(f"{url} 重试 {retries} 次仍失败：{last_error}")


def percentile(values: List[float], q: float) -> Optional[float]:
    """线性插值分位数。样本为空返回 None，绝不返回 0。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def price_row(*, obs_date: str, source: str, gpu_model: str, price_type: str,
              price: Optional[float], market_segment: str = "default",
              region: str = "global", observed_at: Optional[str] = None,
              sample_count: Optional[int] = None, node_gpu_count: Optional[int] = None,
              unit_basis: str = "gpu_hour", price_scope: str = "gpu_only",
              query_fingerprint_: Optional[str] = None, raw_ref: Optional[str] = None,
              quality_flag: str = "ok") -> Dict[str, Any]:
    return {
        "obs_date": obs_date,
        "source": source,
        "gpu_model": gpu_model,
        "price_type": price_type,
        "market_segment": market_segment,
        "region": region,
        "observed_at": observed_at or utc_now_iso(),
        "price_usd_gpu_hour": None if price is None else round(float(price), 6),
        "sample_count": sample_count,
        "node_gpu_count": node_gpu_count,
        "unit_basis": unit_basis,
        "price_scope": price_scope,
        "query_fingerprint": query_fingerprint_,
        "raw_ref": raw_ref,
        "quality_flag": quality_flag,
    }


def supply_row(*, obs_date: str, source: str, gpu_model: str,
               market_segment: str = "default", observed_at: Optional[str] = None,
               offer_count: Optional[int] = None, available_gpu_count: Optional[int] = None,
               stock_status: Optional[str] = None, available_region_count: Optional[int] = None,
               source_total_offer_count: Optional[int] = None,
               offer_share: Optional[float] = None, capacity_detail: Optional[Any] = None,
               query_fingerprint_: Optional[str] = None, raw_ref: Optional[str] = None,
               quality_flag: str = "ok") -> Dict[str, Any]:
    return {
        "obs_date": obs_date,
        "source": source,
        "gpu_model": gpu_model,
        "market_segment": market_segment,
        "observed_at": observed_at or utc_now_iso(),
        "offer_count": offer_count,
        "available_gpu_count": available_gpu_count,
        "stock_status": stock_status,
        "available_region_count": available_region_count,
        "source_total_offer_count": source_total_offer_count,
        "offer_share": None if offer_share is None else round(float(offer_share), 6),
        "capacity_detail": capacity_detail,
        "query_fingerprint": query_fingerprint_,
        "raw_ref": raw_ref,
        "quality_flag": quality_flag,
    }
