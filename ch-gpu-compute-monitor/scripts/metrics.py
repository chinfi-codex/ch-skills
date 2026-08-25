#!/usr/bin/env python3
"""确定性指标层：把库里的观测算成证据包，交给模型去读。

这个脚本刻意不写一句结论性文字。它算变化率、分位数、折价、供给指数、
样本量、新鲜度、评分分项，并把每个数字的来源、样本量、可比性标记一起吐出来。
"这是不是真拐点"由模型判断——所以每个聚合值旁边都留着推翻它所需的原料。

几条不肯让步的纪律：
  * 缺数就是缺数。窗口两端有一头没有观测，变化率返回 null，不用前值补。
  * query_fingerprint 不同的两个观测不许相减——口径变了序列就断了。
  * 冷启动（历史不足 min_history_days）不出评分，出 usable=false 加原因。
  * stale 的人工报价不进跨平台中位数，也不进评分。
  * offer 数用份额比绝对数，剔掉平台自身规模变化带来的伪信号。

用法：
    python scripts/metrics.py                          # 最新观测日，90D 窗口
    python scripts/metrics.py --date 2026-08-25 --window 90
    python scripts/metrics.py --output evidence/gpu-2026-08-25.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import db_adapter  # noqa: E402
from collectors.base import percentile  # noqa: E402
from collectors.runpod import stock_rank  # noqa: E402
from gpu_catalog import load_catalog  # noqa: E402

CONFIG_DIR = SCRIPT_DIR.parent / "config"

# 进入"跨平台中位数"的价格类型。刻意排除：
#   transaction_live —— 小时级实时值，和日度结算不同频，混进去会让当日点跳动
#   committed_3m     —— 承诺期价，和按需价差 20%+，混进去会假装成降价
#   spot             —— 可抢占档，单独算折价，不进中枢
CORE_PRICE_TYPES = ("transaction_index", "offer_median", "on_demand")
CHANGE_WINDOWS = (("1d", 1), ("7d", 7), ("30d", 30))


def _f(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def _d(value: Any) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old) * 100.0


def nearest_on_or_before(series: Dict[str, float], target: str) -> Optional[Tuple[str, float]]:
    """取 target 当天或之前最近的一个观测。

    容忍单日缺采，但不会跨越任意长的缺口——调用方拿到返回的日期后
    自己判断偏离了多少天，偏太远就该当成缺数。
    """
    keys = [k for k in series if k <= target]
    if not keys:
        return None
    key = max(keys)
    return key, series[key]


class Evidence:
    def __init__(self, asof: str, window: int) -> None:
        self.catalog = load_catalog()
        self.thresholds = yaml.safe_load(
            (CONFIG_DIR / "thresholds.yaml").read_text(encoding="utf-8"))
        self.sources_cfg = yaml.safe_load(
            (CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8")).get("sources", {})
        self.asof = asof
        self.window = window
        self.start = (date.fromisoformat(asof) - timedelta(days=window + 45)).isoformat()
        self.prices = [r for r in db_adapter.read_prices(self.start, asof)
                       if _f(r.get("price_usd_gpu_hour")) is not None]
        self.supply = db_adapter.read_supply(self.start, asof)
        self.runs = db_adapter.read_runs(self.start, asof)

    # ---------- 序列构造 ----------
    def price_series(self, model: str, source: str, price_type: str,
                     segment: Optional[str] = None,
                     region: Optional[str] = None) -> Dict[str, Any]:
        """一条 (model, source, price_type) 的日度序列 + 口径指纹集合。"""
        points: Dict[str, float] = {}
        samples: Dict[str, Any] = {}
        fingerprints = set()
        flags = set()
        for row in self.prices:
            if row["gpu_model"] != model or row["source"] != source:
                continue
            if row["price_type"] != price_type:
                continue
            if segment is not None and row["market_segment"] != segment:
                continue
            if region is not None and row["region"] != region:
                continue
            if row["quality_flag"] in ("stale", "suspicious"):
                flags.add(row["quality_flag"])
                continue
            value = _f(row["price_usd_gpu_hour"])
            if value is None:
                continue
            points[_d(row["obs_date"])] = value
            if row.get("sample_count") is not None:
                samples[_d(row["obs_date"])] = int(row["sample_count"])
            if row.get("query_fingerprint"):
                fingerprints.add(row["query_fingerprint"])
        return {"points": points, "samples": samples,
                "fingerprints": sorted(fingerprints),
                "excluded_flags": sorted(flags)}

    def changes(self, series: Dict[str, float],
                basis: Optional[Dict[str, Any]] = None,
                anchor_date: Optional[str] = None) -> Dict[str, Any]:
        """1D/7D/30D 变化率 + 90D 回撤。每个数字带上实际比对的日期。

        basis 是每天的"口径身份"（比如当天参与中位数的源集合）。两端口径不同的
        变化率一律判为不可用——拿 1 个源的中位数去比 3 个源的中位数，得到的
        百分比是数据源上下线造成的，不是价格动了。这是本项目最容易出的假信号：
        实测新接入 Runpod+Vast 的那天，H100 跨平台中枢会凭空"跌" 25%。
        """
        out: Dict[str, Any] = {}
        latest = nearest_on_or_before(series, anchor_date or self.asof)
        if latest is None:
            return {"latest": None, "changes": {}, "drawdown_from_window_high_pct": None}
        latest_date, latest_value = latest
        out["latest"] = {"date": latest_date, "value": round(latest_value, 6)}
        changes: Dict[str, Any] = {}
        for label, days in CHANGE_WINDOWS:
            target = (date.fromisoformat(latest_date) - timedelta(days=days)).isoformat()
            found = nearest_on_or_before(series, target)
            if found is None:
                changes[label] = {"pct": None, "reason": "窗口起点无观测"}
                continue
            base_date, base_value = found
            drift = (date.fromisoformat(target) - date.fromisoformat(base_date)).days
            # 找到的基准点偏离目标日太远，就不该当成"7 日前"来读
            usable = drift <= max(2, days // 3)
            reason = None if usable else f"基准点偏离目标日 {drift} 天"
            basis_now = basis.get(latest_date) if basis else None
            basis_then = basis.get(base_date) if basis else None
            basis_match = True
            if basis is not None and basis_now != basis_then:
                basis_match = False
                usable = False
                reason = (f"两端口径不同（{base_date}: {basis_then} → "
                          f"{latest_date}: {basis_now}），变化率反映的是口径变动而非价格变动")
            entry = {
                "pct": round(pct_change(latest_value, base_value), 4),
                "from_date": base_date, "from_value": round(base_value, 6),
                "date_drift_days": drift,
                "usable": usable,
            }
            if basis is not None:
                entry["basis_match"] = basis_match
                entry["basis_from"] = basis_then
                entry["basis_to"] = basis_now
            if reason:
                entry["reason"] = reason
            changes[label] = entry
        out["changes"] = changes
        window_start = (date.fromisoformat(self.asof) - timedelta(days=self.window)).isoformat()
        window_values = [v for k, v in series.items() if window_start <= k <= self.asof]
        if window_values:
            high = max(window_values)
            out["window_high"] = round(high, 6)
            out["drawdown_from_window_high_pct"] = round(
                (latest_value - high) / high * 100.0, 4) if high else None
            out["window_points"] = len(window_values)
        else:
            out["drawdown_from_window_high_pct"] = None
            out["window_points"] = 0
        return out

    # ---------- 跨平台中枢 ----------
    def cross_platform_median(self, model: str) -> Dict[str, Any]:
        """同一 GPU 跨平台的日度中位数。

        每天分别取各源的核心价格类型，一源一票（同一源多个 segment 先取该源
        的中位再投票），再对各源的票取中位数。这样某个源的 segment 数量变化
        不会左右结果。
        """
        by_day: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
        for row in self.prices:
            if row["gpu_model"] != model or row["price_type"] not in CORE_PRICE_TYPES:
                continue
            if row["quality_flag"] in ("stale", "suspicious"):
                continue
            value = _f(row["price_usd_gpu_hour"])
            if value is not None:
                by_day[_d(row["obs_date"])][row["source"]].append(value)

        series: Dict[str, float] = {}
        contributors: Dict[str, List[str]] = {}
        for day, per_source in by_day.items():
            votes = [percentile(sorted(v), 0.5) for v in per_source.values() if v]
            votes = [v for v in votes if v is not None]
            if not votes:
                continue
            series[day] = percentile(sorted(votes), 0.5)
            contributors[day] = sorted(per_source)
        basis = {day: "+".join(names) for day, names in contributors.items()}
        # 锚点不一定是最后一天。Ornn 是 T-1 结算，所以"今天"的中枢常常只剩
        # runpod+vast，拿它去比昨天的 ornn-only 中枢，跌出来的百分比全是口径差。
        # 改成：取最近 14 天里出现最多的那个口径（modal basis），锚在该口径下
        # 最新的一天，再在同口径内做同期比较。
        anchor_date, modal_basis = self._modal_basis_anchor(basis)
        out = self.changes(series, basis=basis, anchor_date=anchor_date)
        out["anchor_date"] = anchor_date
        out["anchor_basis"] = modal_basis
        raw_latest_day = max(series) if series else None
        out["raw_latest"] = ({"date": raw_latest_day,
                              "value": round(series[raw_latest_day], 6),
                              "basis": basis.get(raw_latest_day)}
                             if raw_latest_day else None)
        out["anchor_lags_raw_latest_days"] = (
            (date.fromisoformat(raw_latest_day) - date.fromisoformat(anchor_date)).days
            if raw_latest_day and anchor_date else None)
        out["series"] = [{"date": k, "value": round(v, 6), "sources": contributors[k]}
                         for k, v in sorted(series.items())]
        out["basis_by_day"] = basis
        latest_day = max(series) if series else None
        out["source_set"] = contributors.get(latest_day, []) if latest_day else []
        # 参与打分的源集合变了，跨日比较就不干净——标出来让模型自己决定采不采信
        prev_days = sorted(d for d in contributors if d < (latest_day or ""))
        out["source_set_changed_vs_prev_day"] = bool(
            prev_days and contributors[prev_days[-1]] != out["source_set"])
        return out

    def _modal_basis_anchor(self, basis: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
        """在最近 14 天里找出现最频繁的口径，并返回该口径下最新的一天。

        口径最全的那个不一定最频繁，但"最频繁"才是能拿来做同期比较的那个：
        它两侧都有同口径的历史点。频次并列时取涉及源更多的那个。
        """
        if not basis:
            return None, None
        recent_start = (date.fromisoformat(self.asof) - timedelta(days=14)).isoformat()
        recent = {d: b for d, b in basis.items() if d >= recent_start} or basis
        counts: Dict[str, int] = defaultdict(int)
        for value in recent.values():
            counts[value] += 1
        modal = max(counts, key=lambda b: (counts[b], len(b.split("+"))))
        days = sorted(d for d, b in basis.items() if b == modal)
        return (days[-1] if days else None), modal

    # ---------- 供给 ----------
    def supply_view(self, model: str) -> Dict[str, Any]:
        """供给三件套：offer 份额、可用 GPU 数、库存档位 + region 覆盖。

        offer 份额而不是绝对数是有意的：Vast 整体规模变大时所有 SKU 的
        offer 数都会涨，那是平台效应不是这块 GPU 的供需。份额剔掉了这一层。
        """
        share: Dict[str, float] = {}
        gpus: Dict[str, float] = {}
        stock_series: Dict[str, Any] = {}
        regions: Dict[str, float] = {}
        detail: List[Dict[str, Any]] = []
        # 供给指标同样要认口径。offer 份额的分母是"当天这次查询覆盖了哪些 SKU"，
        # 往目录里加一个 SKU 就会让所有份额重算——那是口径变动不是供给变动。
        # 把每天的采集指纹当 basis 传给 changes()，复用价格侧那套守卫。
        fingerprints: Dict[str, set] = defaultdict(set)
        for row in self.supply:
            if row["gpu_model"] != model:
                continue
            day = _d(row["obs_date"])
            if row.get("query_fingerprint"):
                fingerprints[day].add(row["query_fingerprint"])
            if row.get("offer_share") is not None:
                share[day] = max(share.get(day, 0.0), _f(row["offer_share"]) or 0.0)
            if row.get("offer_count") is not None:
                gpus[day] = gpus.get(day, 0.0) + float(row.get("available_gpu_count") or 0)
            if row.get("stock_status"):
                stock_series[day] = row["stock_status"]
            if row.get("available_region_count") is not None:
                regions[day] = max(regions.get(day, 0.0),
                                   float(row["available_region_count"]))
            if day == self.asof:
                detail.append({k: (_d(v) if isinstance(v, date) else v)
                               for k, v in row.items() if k not in ("capacity_detail",)})

        latest_stock = stock_series.get(max(stock_series)) if stock_series else None
        stock_days = sorted(stock_series)
        streak = 0
        for day in reversed(stock_days):
            if stock_series[day] == latest_stock:
                streak += 1
            else:
                break
        basis = {day: "+".join(sorted(fps)) for day, fps in fingerprints.items()}
        return {
            "offer_share": {**self.changes(share, basis=basis), "series":
                            [{"date": k, "value": round(v, 6)} for k, v in sorted(share.items())],
                            "basis_by_day": basis},
            "available_gpu_count": {**self.changes(gpus, basis=basis), "series":
                                    [{"date": k, "value": v} for k, v in sorted(gpus.items())]},
            "available_region_count": self.changes(regions, basis=basis),
            "stock_status": {"latest": latest_stock,
                             "rank": stock_rank(latest_stock),
                             "consecutive_days_at_latest": streak,
                             "history": [{"date": k, "status": stock_series[k]}
                                         for k in stock_days]},
            "today_rows": detail,
        }

    # ---------- 报价分散度 / 供给广度 ----------
    def quote_dispersion(self, model: str) -> List[Dict[str, Any]]:
        """P75 − P25，PRD §4.2 的 Quote Dispersion。

        分散度是「报价竞争程度与市场分化」的直接读数：供给稀缺时个别机器能
        要出高价，分布被拉开；供给端定价趋同则收窄。绝对值跨型号不可比
        （B200 单价本来就高），所以同时给相对中位数的百分比。
        """
        out: List[Dict[str, Any]] = []
        latest_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for row in self.prices:
            if row["gpu_model"] != model or row["quality_flag"] != "ok":
                continue
            if row["price_type"] not in ("offer_p25", "offer_p75", "offer_median"):
                continue
            key = (row["source"], row["market_segment"])
            day = _d(row["obs_date"])
            slot = latest_by_key.setdefault(key, {"date": day})
            if day < slot["date"]:
                continue
            if day > slot["date"]:
                slot.clear()
                slot["date"] = day
            slot[row["price_type"]] = _f(row["price_usd_gpu_hour"])
            slot["sample_count"] = row.get("sample_count")

        for (source, segment), slot in sorted(latest_by_key.items()):
            p25, p75 = slot.get("offer_p25"), slot.get("offer_p75")
            median = slot.get("offer_median")
            if p25 is None or p75 is None:
                out.append({"source": source, "segment": segment, "date": slot["date"],
                            "spread": None,
                            "reason": "样本不足，未产出 P25/P75"})
                continue
            out.append({
                "source": source, "segment": segment, "date": slot["date"],
                "p25": round(p25, 6), "p75": round(p75, 6),
                "spread": round(p75 - p25, 6),
                "spread_pct_of_median": (round((p75 - p25) / median * 100, 2)
                                         if median else None),
                "sample_count": slot.get("sample_count"),
            })
        return out

    def supply_breadth(self, model: str) -> Dict[str, Any]:
        """多平台「有货」占比，PRD §4.2 的 Supply Breadth。

        单平台放量可能只是那家在促销；宽松要算数，得从一个平台扩散到全市场。
        「有货」的判定按各源能给的信号：报了 offer 就算有货，报了库存档位就看
        档位不是 None。判不出来的源不进分母——分母里塞一个没表态的源，
        会把「没数据」读成「没货」。
        """
        latest_day = None
        for row in self.supply:
            if row["gpu_model"] != model:
                continue
            day = _d(row["obs_date"])
            latest_day = day if latest_day is None or day > latest_day else latest_day
        if latest_day is None:
            return {"date": None, "with_stock": 0, "reporting": 0, "breadth": None,
                    "reason": "当日没有任何供给观测"}

        per_source: Dict[str, Optional[bool]] = {}
        for row in self.supply:
            if row["gpu_model"] != model or _d(row["obs_date"]) != latest_day:
                continue
            source = row["source"]
            has_stock: Optional[bool] = None
            if row.get("offer_count") is not None:
                has_stock = int(row["offer_count"]) > 0
            elif row.get("stock_status") is not None:
                has_stock = str(row["stock_status"]) not in ("None", "none")
            if has_stock is None:
                continue
            per_source[source] = bool(per_source.get(source)) or has_stock

        reporting = len(per_source)
        with_stock = sum(1 for v in per_source.values() if v)
        return {
            "date": latest_day,
            "with_stock": with_stock,
            "reporting": reporting,
            "breadth": round(with_stock / reporting, 4) if reporting else None,
            "sources": {k: bool(v) for k, v in sorted(per_source.items())},
        }

    # ---------- 折价 ----------
    def discounts(self, model: str) -> List[Dict[str, Any]]:
        """折价必须同源同 segment 比。跨平台算 spot 折价是错的——

        分子分母来自两个不同的成本结构，算出来的数没有含义。
        """
        by_key: Dict[Tuple[str, str, str], Dict[str, float]] = defaultdict(dict)
        for row in self.prices:
            if row["gpu_model"] != model or row["quality_flag"] in ("stale", "suspicious"):
                continue
            value = _f(row["price_usd_gpu_hour"])
            if value is None:
                continue
            by_key[(row["source"], row["market_segment"], row["region"],
                    row["price_type"])][_d(row["obs_date"])] = value

        out: List[Dict[str, Any]] = []
        for (source, segment, region, ptype) in list(by_key):
            if ptype not in ("spot", "preemptible", "offer_min"):
                continue
            base_key = (source, segment, region, "on_demand")
            if ptype == "offer_min":
                base_key = (source, "on_demand", region, "offer_median")
                if segment != "interruptible":
                    continue
            base = by_key.get(base_key)
            if not base:
                continue
            cheap = by_key[(source, segment, region, ptype)]
            days = sorted(set(cheap) & set(base))
            if not days:
                continue
            ratio = {d: (1 - cheap[d] / base[d]) * 100.0 for d in days if base[d]}
            latest_day = max(ratio)
            trailing = [v for k, v in ratio.items()
                        if k > (date.fromisoformat(latest_day) - timedelta(days=30)).isoformat()]
            avg30 = sum(trailing) / len(trailing) if trailing else None
            out.append({
                "source": source, "segment": segment, "region": region,
                "cheap_type": ptype,
                "base_type": base_key[3], "base_segment": base_key[1],
                "latest_date": latest_day,
                "discount_pct": round(ratio[latest_day], 4),
                "avg_30d_pct": round(avg30, 4) if avg30 is not None else None,
                "delta_vs_30d_avg_pct_points": (
                    round(ratio[latest_day] - avg30, 4) if avg30 is not None else None),
                "sample_days": len(ratio),
            })
        return out

    # ---------- 评分 ----------
    def score(self, model: str, price_block: Dict[str, Any],
              supply_block: Dict[str, Any], discount_block: List[Dict[str, Any]]
              ) -> Dict[str, Any]:
        """规则型 Supply-Demand Score：正 = 偏紧，负 = 偏松。

        这不是判断，是把已有证据压成一个数。每个分项的原始值、权重、
        以及"为什么不出分"都一并返回，模型必须能靠这些原料推翻它。
        """
        cfg = self.thresholds["scoring"]
        scale = cfg["squash_scale"]
        weights = cfg["weights"]

        def squash(value: Optional[float], key: str) -> Optional[float]:
            if value is None:
                return None
            return math.tanh(value / float(scale[key]))

        blockers: List[str] = []
        components: List[Dict[str, Any]] = []

        def add(dim: str, name: str, raw: Optional[float], key: str, sign: int) -> None:
            squashed = squash(raw, key)
            components.append({
                "dimension": dim, "name": name, "raw_pct": raw,
                "squashed": None if squashed is None else round(squashed, 4),
                # sign=+1 表示"该项上升 = 偏紧"
                "sign": sign,
                "contribution": None if squashed is None else round(squashed * sign, 4),
            })

        pc = price_block.get("changes", {})
        add("price", "cross_platform_median_7d", _usable_pct(pc.get("7d")), "price_7d_pct", +1)
        add("price", "cross_platform_median_30d", _usable_pct(pc.get("30d")), "price_30d_pct", +1)

        sc = supply_block["offer_share"].get("changes", {})
        gc = supply_block["available_gpu_count"].get("changes", {})
        add("supply", "offer_share_7d", _usable_pct(sc.get("7d")), "supply_share_7d_pct", -1)
        add("supply", "available_gpu_7d", _usable_pct(gc.get("7d")), "supply_gpu_7d_pct", -1)

        widest = None
        for entry in discount_block:
            delta = entry.get("delta_vs_30d_avg_pct_points")
            if delta is not None and (widest is None or delta > widest):
                widest = delta
        add("discount", "discount_widening_vs_30d", widest, "discount_delta_pct", -1)

        history_days = price_block.get("window_points", 0)
        if history_days < cfg["min_history_days"]:
            blockers.append(
                f"跨平台中枢只有 {history_days} 个观测日，少于 {cfg['min_history_days']} 天门槛，"
                "7D/30D 变化率不可信")
        live_price = sum(1 for c in components
                         if c["dimension"] == "price" and c["contribution"] is not None)
        live_supply = sum(1 for c in components
                          if c["dimension"] == "supply" and c["contribution"] is not None)
        if live_price < cfg["min_price_signals"]:
            blockers.append(f"在场价格信号 {live_price} 个，少于 {cfg['min_price_signals']} 个门槛")
        if live_supply < cfg["min_supply_signals"]:
            blockers.append(f"在场供给信号 {live_supply} 个，少于 {cfg['min_supply_signals']} 个门槛")

        dim_scores: Dict[str, Optional[float]] = {}
        for dim in ("price", "supply", "discount"):
            vals = [c["contribution"] for c in components
                    if c["dimension"] == dim and c["contribution"] is not None]
            dim_scores[dim] = round(sum(vals) / len(vals), 4) if vals else None

        total = None
        if not blockers:
            used = {d: v for d, v in dim_scores.items() if v is not None}
            weight_sum = sum(weights[d] for d in used)
            if weight_sum > 0:
                total = round(sum(v * weights[d] for d, v in used.items()) / weight_sum * 100, 2)

        return {
            "value": total,
            "usable": total is not None,
            "blockers": blockers,
            "dimension_scores": dim_scores,
            "weights": weights,
            "components": components,
            "source_set": price_block.get("source_set", []),
            "source_set_changed_vs_prev_day": price_block.get(
                "source_set_changed_vs_prev_day", False),
            "comparable_across_days": not price_block.get(
                "source_set_changed_vs_prev_day", False),
            "interpretation_note": (
                "正值偏紧、负值偏松。这是对已有证据的算术压缩，不是判断；"
                "读之前先看 components 里每一项的 raw_pct 与在场情况。"),
        }

    # ---------- 告警 ----------
    def alerts(self, model: str, price_block: Dict[str, Any],
               supply_block: Dict[str, Any], discount_block: List[Dict[str, Any]]
               ) -> List[Dict[str, Any]]:
        cfg = self.thresholds["alerts"]
        fired: List[Dict[str, Any]] = []
        values = {
            "cross_platform_median_7d_pct": _usable_pct(price_block.get("changes", {}).get("7d")),
            "cross_platform_median_30d_pct": _usable_pct(price_block.get("changes", {}).get("30d")),
            "offer_share_7d_pct": _usable_pct(
                supply_block["offer_share"].get("changes", {}).get("7d")),
            "stock_status_rank_change_days": (
                supply_block["stock_status"]["consecutive_days_at_latest"]
                if supply_block["stock_status"]["latest"] else None),
            "spot_discount_vs_30d_avg_pct_points": max(
                [e["delta_vs_30d_avg_pct_points"] for e in discount_block
                 if e.get("delta_vs_30d_avg_pct_points") is not None] or [None]
            ) if discount_block else None,
        }
        for rule in cfg["rules"]:
            if cfg.get("direction") == "loosening_only" and rule["id"].endswith(
                    ("spike", "rise", "contraction")):
                continue
            value = values.get(rule["metric"])
            if value is None:
                continue
            if rule["metric"] == "stock_status_rank_change_days":
                if supply_block["stock_status"]["latest"] != rule.get("target_status"):
                    continue
            triggered = (value <= rule["threshold"]) if rule["op"] == "<=" else (
                value >= rule["threshold"])
            if not triggered:
                continue
            fired.append({
                "id": rule["id"], "label": rule["label"], "meaning": rule["meaning"],
                "direction": rule.get("direction"),
                "metric": rule["metric"], "observed": round(float(value), 4),
                "threshold": rule["threshold"], "op": rule["op"],
                "mode": cfg.get("mode", "record_only"),
                "note": "阈值来自 config/thresholds.yaml 的起始配置，尚未按实际波动率校准；"
                        "触发只代表越过了这条线，不代表这是真信号。",
            })
        return fired

    # ---------- 确认型拐点 ----------
    def _daily_signal_tally(self, model: str) -> Dict[str, Dict[str, Any]]:
        """逐日数一遍：有几类价格信号在下行、有几类供给信号在改善。

        PRD §4.3 的「至少 3 类价格信号同时下行 + 至少 2 类供给信号改善」，
        「类」指的是独立的观测序列，不是告警条数——同一个变化触发两条告警
        只是同一个证据被数了两遍。所以这里按 (source, price_type) 逐条序列
        算 7 日方向，一条序列一票。

        不落新表：全部从已有观测重算。这样历史可回溯、口径改了能重跑，
        也不会出现「表里的旧账和现在的算法对不上」。
        """
        noise = 1.0  # 7 日变化在 ±1% 以内当没动，避免噪音凑数

        price_series: Dict[Tuple[str, str], Dict[str, float]] = {}
        for row in self.prices:
            if row["gpu_model"] != model or row["quality_flag"] != "ok":
                continue
            if row["price_type"] not in CORE_PRICE_TYPES:
                continue
            value = _f(row["price_usd_gpu_hour"])
            if value is not None:
                price_series.setdefault(
                    (row["source"], row["price_type"]), {})[_d(row["obs_date"])] = value

        supply_series: Dict[Tuple[str, str], Dict[str, float]] = {}
        for row in self.supply:
            if row["gpu_model"] != model:
                continue
            for field in ("offer_share", "available_gpu_count", "available_region_count"):
                value = _f(row.get(field))
                if value is not None:
                    supply_series.setdefault(
                        (row["source"], field), {})[_d(row["obs_date"])] = value
            rank = stock_rank(row.get("stock_status"))
            if rank is not None:
                supply_series.setdefault(
                    (row["source"], "stock_rank"), {})[_d(row["obs_date"])] = float(rank)

        def direction_on(series: Dict[str, float], day: str) -> Optional[int]:
            """该序列在 day 这天的 7 日方向：+1 上行 / -1 下行 / 0 基本没动。"""
            if day not in series:
                return None
            target = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
            found = nearest_on_or_before(series, target)
            if found is None:
                return None
            base_day, base_value = found
            drift = (date.fromisoformat(target) - date.fromisoformat(base_day)).days
            if drift > 2 or not base_value:
                return None
            change = (series[day] - base_value) / abs(base_value) * 100.0
            return 0 if abs(change) < noise else (1 if change > 0 else -1)

        days = sorted({d for s in list(price_series.values()) + list(supply_series.values())
                       for d in s})
        tally: Dict[str, Dict[str, Any]] = {}
        for day in days:
            price_dirs = [direction_on(s, day) for s in price_series.values()]
            supply_dirs = [direction_on(s, day) for s in supply_series.values()]
            price_dirs = [d for d in price_dirs if d is not None]
            supply_dirs = [d for d in supply_dirs if d is not None]
            tally[day] = {
                "price_down": sum(1 for d in price_dirs if d < 0),
                "price_up": sum(1 for d in price_dirs if d > 0),
                "price_series_live": len(price_dirs),
                # 供给「改善」= 可得性上升 = 偏松
                "supply_up": sum(1 for d in supply_dirs if d > 0),
                "supply_down": sum(1 for d in supply_dirs if d < 0),
                "supply_series_live": len(supply_dirs),
            }
        return tally

    def confirmation(self, model: str) -> Dict[str, Any]:
        """确认型宽松 / 收紧（PRD §4.3 与 §8 最后一行）。

        门槛：价格类 ≥N 个信号同向 + 供给类 ≥M 个信号同向，
        且连续 ≥K 个「真实采到数的日子」，中间不允许缺口。

        两个方向对称判定。达不到就明确说差在哪一步，不给模糊结论——
        「还没确认」和「确认没有」是两回事。
        """
        cfg = self.thresholds["confirmation"]
        need_price = int(cfg["min_price_signals"])
        need_supply = int(cfg["min_supply_signals"])
        need_days = int(cfg["min_consecutive_collection_days"])
        allow_gap = int(cfg.get("allow_gap_days", 0))

        tally = self._daily_signal_tally(model)
        days = sorted(tally)
        window_start = (date.fromisoformat(self.asof)
                        - timedelta(days=self.window)).isoformat()
        days = [d for d in days if window_start <= d <= self.asof]

        def run_for(direction: str) -> Dict[str, Any]:
            price_key = "price_down" if direction == "loosening" else "price_up"
            supply_key = "supply_up" if direction == "loosening" else "supply_down"
            streak, best, streak_start, best_span = 0, 0, None, None
            prev_day: Optional[str] = None
            for day in days:
                row = tally[day]
                meets = row[price_key] >= need_price and row[supply_key] >= need_supply
                gap = ((date.fromisoformat(day) - date.fromisoformat(prev_day)).days - 1
                       if prev_day else 0)
                if meets and (prev_day is None or gap <= allow_gap):
                    streak += 1
                    streak_start = streak_start or day
                elif meets:
                    # 采集有缺口，按 allow_gap_days=0 的规矩重新起算
                    streak, streak_start = 1, day
                else:
                    streak, streak_start = 0, None
                if streak > best:
                    best, best_span = streak, (streak_start, day)
                prev_day = day
            return {"streak_days": best, "span": best_span,
                    "confirmed": best >= need_days}

        loosening, tightening = run_for("loosening"), run_for("tightening")

        # 参考日不取最后一天。Ornn 是 T-1 结算，「今天」那一行的价格序列必然
        # 是空的，拿它报 blockers 会写成「在场价格序列 0 条」，读起来像全挂了。
        # 取最近一个真的有序列在场的日子。
        ref_day = next((d for d in reversed(days)
                        if tally[d]["price_series_live"] or tally[d]["supply_series_live"]),
                       None)
        latest = tally.get(ref_day) if ref_day else None

        blockers: List[str] = []
        if latest is None:
            blockers.append("窗口内没有任何序列能算出 7 日方向")
        else:
            if latest["price_series_live"] < need_price:
                blockers.append(
                    f"能算出 7 日方向的价格序列只有 {latest['price_series_live']} 条"
                    f"（{ref_day}），凑不满 {need_price} 类同向信号的门槛")
            if latest["supply_series_live"] < need_supply:
                blockers.append(
                    f"能算出 7 日方向的供给序列只有 {latest['supply_series_live']} 条"
                    f"（{ref_day}），凑不满 {need_supply} 类同向信号的门槛")

        verdict = "none"
        if loosening["confirmed"]:
            verdict = "loosening"
        elif tightening["confirmed"]:
            verdict = "tightening"

        return {
            "verdict": verdict,
            "loosening": loosening,
            "tightening": tightening,
            "thresholds": {"min_price_signals": need_price,
                           "min_supply_signals": need_supply,
                           "min_consecutive_collection_days": need_days,
                           "allow_gap_days": allow_gap},
            "reference_date": ref_day,
            "latest_tally": latest,
            "collection_days_in_window": len(days),
            "blockers": blockers,
            "note": ("「还没确认」不等于「确认没有」。blockers 非空时说明"
                     "连判定所需的信号条数都还凑不齐，此时任何方向都不该下定论。"),
        }

    # ---------- 健康度 ----------
    def freshness(self) -> List[Dict[str, Any]]:
        """各源最近一次采集的状态。

        只报 config 里还在的源。下线一个数据源后，它在 gpu_collect_runs 里的
        历史行仍然留着（历史观测要能追溯），但不该继续出现在"今天缺了谁"的
        面板上——否则一个早就移除的源会永远显示成红色的采集失败。
        """
        configured = set(self.sources_cfg)
        out = []
        for row in db_adapter.latest_run_per_source():
            if row["source"] not in configured:
                continue
            obs = _d(row.get("obs_date")) if row.get("obs_date") else None
            age = ((date.fromisoformat(self.asof) - date.fromisoformat(obs)).days
                   if obs else None)
            cfg = self.sources_cfg.get(row["source"], {})
            out.append({
                "source": row["source"],
                "priority": cfg.get("priority"),
                "role": cfg.get("role"),
                "mode": cfg.get("mode", "api"),
                "status": row.get("status"),
                "last_obs_date": obs,
                "age_days": age,
                "price_rows": row.get("price_rows"),
                "supply_rows": row.get("supply_rows"),
                "latency_ms": row.get("latency_ms"),
                "error": row.get("error"),
                # 超过 1 天没有新观测就不能再当成"最新"用
                "fresh": age is not None and age <= 1 and row.get("status") in ("ok", "empty"),
            })
        return sorted(out, key=lambda r: (r.get("priority") or "Z", r["source"]))

    def generation_premium(self, medians: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for pair in self.catalog.generation_pairs:
            num = medians.get(pair["numerator"], {}).get("latest")
            den = medians.get(pair["denominator"], {}).get("latest")
            if not num or not den or not den.get("value"):
                out.append({"name": pair["name"], "ratio": None,
                            "reason": "两端缺一，无法计算"})
                continue
            aligned = num["date"] == den["date"]
            out.append({
                "name": pair["name"],
                "ratio": round(num["value"] / den["value"], 4),
                "numerator": {**num, "model": pair["numerator"]},
                "denominator": {**den, "model": pair["denominator"]},
                # 两端不是同一天的价，比值就掺了时间错位
                "date_aligned": aligned,
            })
        return out

    def reference_models(self) -> Dict[str, Any]:
        """参照系 SKU（watchlist）的轻量视图：只给成交价序列与变化率。

        它们不进首页三型号同屏，也不参与评分和代际溢价。留着是为了回答一个
        主力 SKU 自己答不了的问题——高端在涨，是某一代结构性紧缺，还是整条
        算力曲线都在抬？A100 是上一代的价格地板，RTX 5090 是消费级溢出产能
        的温度计；两者在 Ornn 免费层里同样有 90 天历史，取它们零额外成本。
        """
        out: Dict[str, Any] = {}
        primary = set(self.catalog.primary_models)
        for model in self.catalog.all_models:
            if model in primary:
                continue
            built = self.price_series(model, "ornn", "transaction_index")
            if len(built["points"]) < 2:
                continue
            block = self.changes(built["points"])
            window_start = (date.fromisoformat(self.asof)
                            - timedelta(days=self.window)).isoformat()
            clipped = {k: v for k, v in built["points"].items()
                       if window_start <= k <= self.asof}
            block["series"] = [{"date": k, "value": round(v, 6)}
                               for k, v in sorted(clipped.items())]
            if clipped:
                days = sorted(clipped)
                first, last = clipped[days[0]], clipped[days[-1]]
                block["window_change_pct"] = (
                    round(pct_change(last, first), 4) if first else None)
                block["window_from"] = {"date": days[0], "value": round(first, 6)}
            block["role"] = "reference_only"
            out[model] = block
        return out

    def build(self) -> Dict[str, Any]:
        models = self.catalog.primary_models
        per_model: Dict[str, Any] = {}
        medians: Dict[str, Dict[str, Any]] = {}
        for model in models:
            cross = self.cross_platform_median(model)
            medians[model] = cross
            supply = self.supply_view(model)
            disc = self.discounts(model)
            by_source = {}
            for source in sorted({r["source"] for r in self.prices
                                  if r["gpu_model"] == model}):
                # 必须按 (price_type, market_segment) 展开。Runpod 同一天的
                # on_demand 有 secure / community / lowest 三个 segment，
                # 只按 price_type 分组会让三个数字互相覆盖，剩下的那个是随机的。
                # region 也是主键的一部分：CoreWeave 同一天有北美与欧洲两套 spot，
                # 只按 (type, segment) 分组会让两个地区的值互相覆盖，剩下哪个是随机的。
                pairs = sorted({(r["price_type"], r["market_segment"], r["region"])
                                for r in self.prices
                                if r["gpu_model"] == model and r["source"] == source})
                by_source[source] = {}
                for ptype, segment, region in pairs:
                    key = ptype if segment == "default" else f"{ptype}@{segment}"
                    if region != "global":
                        key = f"{key}#{region}"
                    built = self.price_series(model, source, ptype, segment=segment,
                                              region=region)
                    if not built["points"]:
                        continue
                    block = self.changes(built["points"])
                    window_start = (date.fromisoformat(self.asof)
                                    - timedelta(days=self.window)).isoformat()
                    block["series"] = [{"date": k, "value": round(v, 6)}
                                       for k, v in sorted(built["points"].items())
                                       if window_start <= k <= self.asof]
                    block["fingerprints"] = built["fingerprints"]
                    block["fingerprint_stable"] = len(built["fingerprints"]) <= 1
                    if built["excluded_flags"]:
                        block["excluded_flags"] = built["excluded_flags"]
                    block["market_segment"] = segment
                    block["region"] = region
                    latest_day = (block.get("latest") or {}).get("date")
                    if latest_day and latest_day in built["samples"]:
                        block["sample_count"] = built["samples"][latest_day]
                    by_source[source][key] = block
            per_model[model] = {
                "label": self.catalog.label(model),
                "cross_platform_median": cross,
                "by_source": by_source,
                "supply": supply,
                "discounts": disc,
                "quote_dispersion": self.quote_dispersion(model),
                "supply_breadth": self.supply_breadth(model),
                "score": self.score(model, cross, supply, disc),
                "alerts": self.alerts(model, cross, supply, disc),
                "confirmation": self.confirmation(model),
            }

        return {
            "schema": "gpu-compute-monitor/evidence/1.0",
            "asof": self.asof,
            "window_days": self.window,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "models": per_model,
            "reference_models": self.reference_models(),
            "generation_premium": self.generation_premium(medians),
            "source_health": self.freshness(),
            "config": {
                "core_price_types": list(CORE_PRICE_TYPES),
                "scoring": self.thresholds["scoring"],
                "alert_mode": self.thresholds["alerts"]["mode"],
                "alert_direction": self.thresholds["alerts"]["direction"],
                "confirmation": self.thresholds["confirmation"],
            },
        }


def persist_alerts(evidence: Dict[str, Any]) -> int:
    """把当天触发的告警写进 gpu_alerts（PRD §6.1 步骤 8）。

    先清掉当天的旧行再写：阈值调过之后，昨天触发、今天不该触发的那条
    否则会永远留在库里，把后面的连续性判断带偏。
    """
    asof = evidence["asof"]
    models = list((evidence.get("models") or {}).keys())
    db_adapter.clear_alerts(asof, models)
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    for model, block in (evidence.get("models") or {}).items():
        for alert in block.get("alerts") or []:
            rows.append({
                "obs_date": asof, "gpu_model": model, "rule_id": alert["id"],
                "label": alert.get("label"), "direction": alert.get("direction"),
                "metric": alert.get("metric"), "observed": alert.get("observed"),
                "threshold": alert.get("threshold"), "op": alert.get("op"),
                "meaning": alert.get("meaning"), "mode": alert.get("mode"),
                "fired_at": now,
            })
    return db_adapter.save_alerts(rows)


def _usable_pct(change: Optional[Dict[str, Any]]) -> Optional[float]:
    """只有基准点落在容差内的变化率才拿来用。"""
    if not change or change.get("pct") is None or not change.get("usable"):
        return None
    return float(change["pct"])


def main() -> int:
    parser = argparse.ArgumentParser(description="GPU 价格与供给指标计算")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--window", type=int, default=90, help="趋势窗口天数，默认 90")
    parser.add_argument("--output", default=None)
    parser.add_argument("--no-persist-alerts", action="store_true",
                        help="只算不落库；默认会把触发的告警写进 gpu_alerts")
    args = parser.parse_args()

    evidence = Evidence(args.date, args.window).build()
    persisted = None if args.no_persist_alerts else persist_alerts(evidence)
    text = json.dumps(evidence, ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
        print(json.dumps({"ok": True, "output": args.output,
                          "models": list(evidence["models"]),
                          "asof": evidence["asof"],
                          "alerts_persisted": persisted}, ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
