"""人工核对的挂牌价采集器（CoreWeave / Nebius / Crusoe）。

这几家只有营销 Pricing 页，没有稳定 API。写选择器爬虫的风险在于失效方式
太安静——页面一改版，解析器最容易做的事就是返回 0 或空，而空值会被下游
当成"降价"。所以这里改成读 config/attested_prices.yaml：人核对、留 URL、
留 as_of，超龄自动降级为 stale 并退出核心指标。

stale 的语义是"这个数还在，但不能当成今天的价"。指标层看到 quality_flag=stale
会把它排除出跨平台中位数与评分，只留在标准报价矩阵里显示，并标出核对日期。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .base import CollectResult, CollectorError, price_row, query_fingerprint

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "attested_prices.yaml"


def collect(source: str, cfg: Dict[str, Any], catalog, obs_date: str,
            config_path: Optional[Path] = None) -> CollectResult:
    path = Path(config_path or CONFIG_PATH)
    if not path.exists():
        raise CollectorError(f"找不到 {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries: List[Dict[str, Any]] = raw.get("entries") or []

    result = CollectResult(source=source)
    mine = [e for e in entries if str(e.get("source")) == source]
    if not mine:
        result.notes.append(
            f"config/attested_prices.yaml 里没有 {source} 的条目；"
            f"该源今天没有可用报价（去 {cfg.get('pricing_url')} 核对后补录）")
        return result

    max_age = int(cfg.get("max_age_days", 30))
    today = date.fromisoformat(obs_date)
    fp = query_fingerprint({"source": source, "mode": "attested", "max_age_days": max_age})

    for entry in mine:
        model = catalog.resolve(source, entry.get("gpu_model")) or entry.get("gpu_model")
        if model not in catalog.all_models:
            result.unmapped.append(str(entry.get("gpu_model")))
            continue
        as_of = entry.get("as_of")
        if not as_of:
            raise CollectorError(f"{source}/{model} 缺 as_of，人工报价必须带核对日期")
        age = (today - date.fromisoformat(str(as_of))).days
        gpus = int(entry.get("node_gpu_count") or 1)
        price = float(entry["price_usd"]) / gpus
        result.prices.append(price_row(
            obs_date=obs_date, source=source, gpu_model=model,
            price_type=str(entry.get("price_type", "on_demand")),
            price=price, region=str(entry.get("region", "global")),
            node_gpu_count=gpus,
            unit_basis="attested_node_hour/{}gpu".format(gpus) if gpus > 1 else "attested_gpu_hour",
            price_scope=str(entry.get("price_scope", "bundled")),
            observed_at=f"{as_of}T00:00:00+00:00",
            query_fingerprint_=fp, raw_ref=str(entry.get("source_url") or cfg.get("pricing_url")),
            quality_flag="ok" if age <= max_age else "stale"))
        if age > max_age:
            result.notes.append(
                f"{model}/{entry.get('price_type')} 的核对日期是 {as_of}，"
                f"已过期 {age - max_age} 天，已降级为 stale 并退出核心指标")
    return result
