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
    # 推理 token 的量价观测。刻意不塞进 prices：单位是 USD/Mtok 与 tokens/day，
    # 跟 USD/GPU·hour 不是一个量纲，混进同一张表迟早会有人把它们相减。
    tokens: List[Dict[str, Any]] = field(default_factory=list)
    # 周度、厂商级的历史量。与 tokens 分开装：两者口径不同，实测同一天
    # 各厂商比值 1.42–1.75 不是常数倍，拼进一条序列就是造假。
    history: List[Dict[str, Any]] = field(default_factory=list)
    # 调用方（应用）维度的日度量。也单独装：应用榜只给 token 与请求数，
    # 没有模型拆分也没有价，跟模型 × 变体那张表不是一个可 join 的粒度。
    apps: List[Dict[str, Any]] = field(default_factory=list)
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


def token_row(*, obs_date: str, source: str, model_family: str, model_slug: str,
              variant: str, coverage_scope: str = "gateway", price_basis: str = "list",
              observed_at: Optional[str] = None,
              prompt_tokens: Optional[int] = None,
              completion_tokens: Optional[int] = None,
              requests: Optional[int] = None,
              price_prompt_usd_per_mtok: Optional[float] = None,
              price_completion_usd_per_mtok: Optional[float] = None,
              price_cache_read_usd_per_mtok: Optional[float] = None,
              spend_usd: Optional[float] = None,
              is_priced: Optional[bool] = None,
              price_match: str = "unmatched",
              provider_price_min_usd_per_mtok: Optional[float] = None,
              provider_price_median_usd_per_mtok: Optional[float] = None,
              provider_price_max_usd_per_mtok: Optional[float] = None,
              provider_count: Optional[int] = None,
              query_fingerprint_: Optional[str] = None, raw_ref: Optional[str] = None,
              quality_flag: str = "ok") -> Dict[str, Any]:
    """一行「模型 × 变体」的日度量价观测。

    variant 是主键的一部分，不是装饰：:batch 是折扣档、:free 是零价档，
    合并进标准档会让同一批 token 被按错误的价计。
    """
    def _r(value: Optional[float], digits: int = 8) -> Optional[float]:
        return None if value is None else round(float(value), digits)

    return {
        "obs_date": obs_date,
        "source": source,
        "model_family": model_family,
        "model_slug": model_slug,
        "variant": variant,
        "coverage_scope": coverage_scope,
        "price_basis": price_basis,
        "observed_at": observed_at or utc_now_iso(),
        "prompt_tokens": None if prompt_tokens is None else int(prompt_tokens),
        "completion_tokens": None if completion_tokens is None else int(completion_tokens),
        "requests": None if requests is None else int(requests),
        "price_prompt_usd_per_mtok": _r(price_prompt_usd_per_mtok, 6),
        "price_completion_usd_per_mtok": _r(price_completion_usd_per_mtok, 6),
        "price_cache_read_usd_per_mtok": _r(price_cache_read_usd_per_mtok, 6),
        "spend_usd": _r(spend_usd, 6),
        "is_priced": is_priced,
        "price_match": price_match,
        "provider_price_min_usd_per_mtok": _r(provider_price_min_usd_per_mtok, 6),
        "provider_price_median_usd_per_mtok": _r(provider_price_median_usd_per_mtok, 6),
        "provider_price_max_usd_per_mtok": _r(provider_price_max_usd_per_mtok, 6),
        "provider_count": provider_count,
        "query_fingerprint": query_fingerprint_,
        "raw_ref": raw_ref,
        "quality_flag": quality_flag,
    }


def token_app_row(*, obs_date: str, source: str, app_id: str,
                  app_slug: Optional[str] = None, app_title: Optional[str] = None,
                  app_url: Optional[str] = None,
                  categories: Optional[List[str]] = None,
                  rank: Optional[int] = None,
                  total_tokens: Optional[int] = None,
                  total_requests: Optional[int] = None,
                  coverage_scope: str = "gateway",
                  listing_scope: str = "public_ranked",
                  observed_at: Optional[str] = None,
                  query_fingerprint_: Optional[str] = None,
                  raw_ref: Optional[str] = None,
                  quality_flag: str = "ok") -> Dict[str, Any]:
    """一行「应用 × 日」的调用量观测。

    `listing_scope` 默认 `public_ranked`，说的是一件必须记住的事：榜单返回的
    名次**不连续**（实测 20 行里 rank 跳过了 2/3/4/15/16/18），所以这 20 行
    不是「前 20 名」，而是「前 26 名里愿意公开露出的那些」。把它们求和当作
    「应用侧总量」会系统性偏低，`__other__` 也不能被读成「未上榜的应用」。

    这张表里没有 spend：应用榜只给 token 与请求数，不拆模型，也就没法配价。
    """
    return {
        "obs_date": obs_date,
        "source": source,
        "app_id": str(app_id),
        "observed_at": observed_at or utc_now_iso(),
        "app_slug": app_slug,
        "app_title": app_title,
        "app_url": app_url,
        "categories": categories or None,
        "rank": None if rank is None else int(rank),
        "total_tokens": None if total_tokens is None else int(total_tokens),
        "total_requests": None if total_requests is None else int(total_requests),
        "coverage_scope": coverage_scope,
        "listing_scope": listing_scope,
        "query_fingerprint": query_fingerprint_,
        "raw_ref": raw_ref,
        "quality_flag": quality_flag,
    }


def token_history_row(*, week_start: str, source: str, author: str,
                      tokens: Optional[int] = None,
                      unit_basis: str = "provider_reported_unverified",
                      coverage_scope: str = "gateway", grain: str = "author_weekly",
                      settled: bool = True, observed_at: Optional[str] = None,
                      query_fingerprint_: Optional[str] = None,
                      raw_ref: Optional[str] = None,
                      quality_flag: str = "ok") -> Dict[str, Any]:
    """一行「厂商 × 周」的历史量观测。

    `unit_basis` 默认标成未核实：这条序列的 token 计数与日榜对不上
    （实测同日各厂商比值 1.42–1.75，不是常数倍），成因未定。
    所以它只能用来看份额与增速，不能用来读绝对水平，更不能和日度序列相减。
    """
    return {
        "week_start": week_start,
        "source": source,
        "author": author,
        "observed_at": observed_at or utc_now_iso(),
        "tokens": None if tokens is None else int(tokens),
        "unit_basis": unit_basis,
        "coverage_scope": coverage_scope,
        "grain": grain,
        "settled": settled,
        "query_fingerprint": query_fingerprint_,
        "raw_ref": raw_ref,
        "quality_flag": quality_flag,
    }
