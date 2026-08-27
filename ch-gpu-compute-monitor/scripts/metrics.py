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
import hashlib
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

# 供给与 token 是两条补不回去的序列：Vast / Runpod 只回当下快照，OpenRouter 的
# 日榜也没有历史接口，它们只能从首次采集当天往后长。90 天窗口一旦滚过它们的
# 起点，掐掉的那一段就是**永久丢失**的——没有任何办法再取回来。所以这两类
# 一律读到底、出到底，窗口只管成交价（Ornn 自带滚动 3 个月历史，掉出窗口的点
# 明天还能重新取到）与各种 *_change_pct 的比较基准。
FULL_HISTORY_START = "2000-01-01"
FULL_HISTORY_SCOPE = "full_history"


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
        # 供给读到底：见 FULL_HISTORY_START，序列补不回去，掐头等于永久丢失
        self.supply = db_adapter.read_supply(FULL_HISTORY_START, asof)
        self.runs = db_adapter.read_runs(self.start, asof)
        self.basket_cfg = yaml.safe_load(
            (CONFIG_DIR / "token_basket.yaml").read_text(encoding="utf-8")) or {}
        # token 观测可能整体缺席（老库、或者只跑了 GPU 侧的源），
        # 缺就是缺，token_market 会如实报 usable=false，不影响 GPU 侧任何指标。
        try:
            self.tokens = db_adapter.read_tokens(FULL_HISTORY_START, asof)
        except Exception:
            self.tokens = []
        # 应用榜是补强维度，老库里可能压根没这张表：缺就是缺，token_market.apps
        # 会如实报 usable=false，不影响模型侧任何指标。
        try:
            self.apps = db_adapter.read_token_apps(FULL_HISTORY_START, asof)
        except Exception:
            self.apps = []

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
            # 两条序列都不裁窗口：Vast / Runpod 没有历史接口，掐掉的一段补不回来
            "offer_share": {**self.changes(share, basis=basis), "series":
                            [{"date": k, "value": round(v, 6)} for k, v in sorted(share.items())],
                            "series_scope": FULL_HISTORY_SCOPE,
                            "basis_by_day": basis},
            "available_gpu_count": {**self.changes(gpus, basis=basis), "series":
                                    [{"date": k, "value": v} for k, v in sorted(gpus.items())],
                                    "series_scope": FULL_HISTORY_SCOPE},
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
        「有货」的判定按各源能给的信号：offer_count > 0 或库存档位 Low 以上；
        明确的 no_stock 计作无货，未知档位不进分母——分母里塞一个没表态的源，
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
            if row.get("quality_flag") == "no_stock":
                # Runpod 在价格与库存字段同时为空时会明确写 no_stock；这是已知无货，
                # 不能从分母里丢掉，否则供给广度会系统性偏高。
                has_stock = False
            elif row.get("offer_count") is not None:
                has_stock = int(row["offer_count"]) > 0
            elif row.get("stock_status") is not None:
                rank = stock_rank(row.get("stock_status"))
                has_stock = rank > 0 if rank is not None else None
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
        只是同一个证据被数了两遍。所以这里按 (source, price_type) 逐条序列算
        7 日方向，一条序列一票；多个 segment/region 先按日聚合，两端
        query_fingerprint 集合不同则不可比。

        不落新表：全部从已有观测重算。这样历史可回溯、口径改了能重跑，
        也不会出现「表里的旧账和现在的算法对不上」。
        """
        noise = 1.0  # 7 日变化在 ±1% 以内当没动，避免噪音凑数

        # 同一 source/price_type 的多个 segment/region 仍只算一票，但必须先按日
        # 确定性聚合，不能让最后一行覆盖前面的行。口径指纹集合也跟着日聚合。
        price_buckets: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
        for row in self.prices:
            if row["gpu_model"] != model or row["quality_flag"] != "ok":
                continue
            if row["price_type"] not in CORE_PRICE_TYPES:
                continue
            value = _f(row["price_usd_gpu_hour"])
            if value is not None:
                slot = price_buckets.setdefault(
                    (row["source"], row["price_type"]), {}).setdefault(
                        _d(row["obs_date"]), {"values": [], "fingerprints": set()})
                slot["values"].append(value)
                if row.get("query_fingerprint"):
                    slot["fingerprints"].add(str(row["query_fingerprint"]))

        price_series: Dict[Tuple[str, str], Dict[str, Tuple[float, Optional[str]]]] = {}
        for key, by_day in price_buckets.items():
            price_series[key] = {
                day: (percentile(sorted(slot["values"]), 0.5),
                      "+".join(sorted(slot["fingerprints"])) or None)
                for day, slot in by_day.items()
            }

        supply_buckets: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
        for row in self.supply:
            if row["gpu_model"] != model:
                continue
            for field in ("offer_share", "available_gpu_count", "available_region_count"):
                value = _f(row.get(field))
                if value is not None:
                    slot = supply_buckets.setdefault((row["source"], field), {}).setdefault(
                        _d(row["obs_date"]), {"values": [], "fingerprints": set()})
                    slot["values"].append(value)
                    if row.get("query_fingerprint"):
                        slot["fingerprints"].add(str(row["query_fingerprint"]))
            rank = stock_rank(row.get("stock_status"))
            if rank is not None:
                slot = supply_buckets.setdefault(
                    (row["source"], "stock_rank"), {}).setdefault(
                        _d(row["obs_date"]), {"values": [], "fingerprints": set()})
                slot["values"].append(float(rank))
                if row.get("query_fingerprint"):
                    slot["fingerprints"].add(str(row["query_fingerprint"]))

        supply_series: Dict[Tuple[str, str], Dict[str, Tuple[float, Optional[str]]]] = {}
        for key, by_day in supply_buckets.items():
            supply_series[key] = {
                day: (percentile(sorted(slot["values"]), 0.5),
                      "+".join(sorted(slot["fingerprints"])) or None)
                for day, slot in by_day.items()
            }

        def direction_on(
                series: Dict[str, Tuple[float, Optional[str]]], day: str) -> Optional[int]:
            """该序列在 day 这天的 7 日方向：+1 上行 / -1 下行 / 0 基本没动。"""
            if day not in series:
                return None
            target = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
            base_days = [d for d in series if d <= target]
            if not base_days:
                return None
            base_day = max(base_days)
            base_value, base_fingerprint = series[base_day]
            current_value, current_fingerprint = series[day]
            drift = (date.fromisoformat(target) - date.fromisoformat(base_day)).days
            if drift > 2 or not base_value:
                return None
            if current_fingerprint != base_fingerprint:
                return None
            change = (current_value - base_value) / abs(base_value) * 100.0
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
                "token_rows": row.get("token_rows"),
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

    # ---------- 推理 token 量价（需求端）----------
    #
    # 与价格侧刻意隔开：单位是 USD/Mtok 与 tokens/day，跟 USD/GPU·hour 不同量纲，
    # 也不进供需评分——token 是需求侧的上游证据，不是算力松紧的同一根轴。
    def _token_days(self) -> Dict[str, List[Dict[str, Any]]]:
        days: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in self.tokens:
            if (row.get("quality_flag") or "ok") != "ok":
                continue
            days[_d(row["obs_date"])].append(row)
        return dict(days)

    def token_daily(self) -> Dict[str, Dict[str, Any]]:
        """每天一份 token 聚合。全部从观测重算，不落表——口径改了直接重跑。

        三条量刻意分开：付费、free 变体、零价的 standard（stealth 模型）。
        实测 2026-08-24 零价占 40.1%，其中 free 变体只有 7.8%，主体是榜首那个
        匿名 stealth 模型（一家 32.3%）。合并成一条"总量"，它进出榜单就会让
        序列直接跳一大截。
        """
        out: Dict[str, Dict[str, Any]] = {}
        for day, rows in self._token_days().items():
            total = paid = free_variant = zero_std = unmatched = 0
            prompt = completion = requests = 0
            prompt_spend = completion_spend = 0.0
            per_key: Dict[str, int] = defaultdict(int)
            families: Dict[str, Dict[str, float]] = {}
            band_num = band_den = 0.0
            band_lo = band_hi = 0.0
            for row in rows:
                pt = int(row.get("prompt_tokens") or 0)
                ct = int(row.get("completion_tokens") or 0)
                volume = pt + ct
                total += volume
                per_key[f"{row['model_slug']}:{row['variant']}"] += volume
                if row.get("price_match") == "unmatched":
                    unmatched += volume
                    continue
                if not row.get("is_priced"):
                    if (row.get("variant") or "") == "free":
                        free_variant += volume
                    else:
                        zero_std += volume
                    continue
                paid += volume
                prompt += pt
                completion += ct
                requests += int(row.get("requests") or 0)
                pp = _f(row.get("price_prompt_usd_per_mtok")) or 0.0
                pc = _f(row.get("price_completion_usd_per_mtok")) or 0.0
                prompt_spend += pt * pp / 1e6
                completion_spend += ct * pc / 1e6
                key = f"{row['model_family']}|{row['variant']}"
                fam = families.setdefault(key, {"prompt": 0.0, "completion": 0.0,
                                                "prompt_cost": 0.0, "completion_cost": 0.0})
                fam["prompt"] += pt
                fam["completion"] += ct
                fam["prompt_cost"] += pt * pp
                fam["completion_cost"] += ct * pc
                # 逐 provider 价差带只在少数模型上有，按 spend 加权汇总成一个倍数
                median = _f(row.get("provider_price_median_usd_per_mtok"))
                lo = _f(row.get("provider_price_min_usd_per_mtok"))
                hi = _f(row.get("provider_price_max_usd_per_mtok"))
                if median and pp:
                    weight = pt * pp / 1e6
                    band_den += weight
                    band_num += weight * median / pp
                    band_lo += weight * (lo / pp if lo else 1.0)
                    band_hi += weight * (hi / pp if hi else 1.0)
            spend = prompt_spend + completion_spend
            fam_prices: Dict[str, Dict[str, Optional[float]]] = {}
            for key, fam in families.items():
                fam_prices[key] = {
                    "paid_tokens": fam["prompt"] + fam["completion"],
                    "prompt_tokens": fam["prompt"],
                    "completion_tokens": fam["completion"],
                    # 家族内部按当期各版本的 token 加权 —— 版本升级带来的降价
                    # 就该体现成这个家族自己变便宜了（技术通缩），不是结构迁移。
                    "price_prompt": (fam["prompt_cost"] / fam["prompt"]
                                     if fam["prompt"] else None),
                    "price_completion": (fam["completion_cost"] / fam["completion"]
                                         if fam["completion"] else None),
                }
            top_key, top_volume = (max(per_key.items(), key=lambda kv: kv[1])
                                   if per_key else (None, 0))
            out[day] = {
                "total_tokens": total,
                "paid_tokens": paid,
                "free_variant_tokens": free_variant,
                "zero_priced_standard_tokens": zero_std,
                "unmatched_tokens": unmatched,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "requests": requests,
                "spend_usd": spend,
                "prompt_spend_usd": prompt_spend,
                "completion_spend_usd": completion_spend,
                "blended_usd_per_mtok": (spend / paid * 1e6) if paid else None,
                "tokens_per_request": (paid / requests) if requests else None,
                "unmatched_share": (unmatched / total) if total else None,
                "top_model": top_key,
                "top_model_share": (top_volume / total) if total else None,
                "family_prices": fam_prices,
                "row_count": len(rows),
                "provider_band": ({"median_ratio": band_num / band_den,
                                   "min_ratio": band_lo / band_den,
                                   "max_ratio": band_hi / band_den,
                                   "covered_spend_share": (band_den / prompt_spend
                                                           if prompt_spend else None)}
                                  if band_den else None),
            }
        return out

    def token_basket(self, daily: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """从基期那天的观测确定性地推出篮子。不手工维护成员名单，不落盘。"""
        cfg = (self.basket_cfg.get("basket") or {})
        days = sorted(d for d, v in daily.items() if v["paid_tokens"] > 0)
        if not days:
            return {"usable": False, "reason": "没有任何付费 token 观测"}
        wanted = cfg.get("base_date")
        base_date = None
        if wanted:
            later = [d for d in days if d >= str(wanted)]
            base_date = later[0] if later else None
            if base_date is None:
                return {"usable": False,
                        "reason": f"配置的基期 {wanted} 之后没有任何观测"}
        else:
            base_date = days[0]
        base = daily[base_date]
        ranked = sorted(base["family_prices"].items(),
                        key=lambda kv: -(kv[1]["paid_tokens"] or 0))
        coverage_target = float(cfg.get("min_coverage_share", 0.9))
        max_members = int(cfg.get("max_members", 60))
        total_paid = float(base["paid_tokens"]) or 0.0
        members: Dict[str, Dict[str, Any]] = {}
        cumulative = 0.0
        for key, stats in ranked:
            if len(members) >= max_members:
                break
            unit = _unit_price(stats, stats)
            if unit is None or unit <= 0:
                continue
            members[key] = {
                "weight": (stats["paid_tokens"] / total_paid) if total_paid else 0.0,
                "base_unit_price_usd_per_mtok": unit,
                "base_prompt_share": (stats["prompt_tokens"] / stats["paid_tokens"]
                                      if stats["paid_tokens"] else None),
            }
            cumulative += members[key]["weight"]
            if cumulative >= coverage_target:
                break
        if not members:
            return {"usable": False, "reason": "基期当天没有可定价的家族"}
        weight_sum = sum(m["weight"] for m in members.values()) or 1.0
        for m in members.values():
            m["weight"] = m["weight"] / weight_sum
        return {
            "usable": True,
            "base_date": base_date,
            "member_count": len(members),
            "base_coverage_share": round(cumulative, 4),
            "grain": cfg.get("member_grain", "family_variant"),
            "intra_family_weighting": cfg.get("intra_family_weighting",
                                              "base_period_io_mix"),
            "chain_links": cfg.get("chain_links") or [],
            "members": members,
            "fingerprint": _basket_fingerprint(base_date, members),
        }

    def token_laspeyres(self, daily: Dict[str, Dict[str, Any]],
                        basket: Dict[str, Any]) -> Dict[str, Any]:
        """锁基期权重的固定篮子价格指数：只让单价动，权重与输入输出结构不动。

        它和当期权重的 blended 之差，就是「往便宜模型迁移」贡献了多少——
        本方案唯一的原创信息，也是唯一能把"token 价格跌了 20%"拆开的东西。
        """
        if not basket.get("usable"):
            return {"usable": False, "reason": basket.get("reason", "篮子不可用"),
                    "points": {}}
        members = basket["members"]
        min_weight = float((self.basket_cfg.get("basket") or {})
                           .get("min_in_basket_weight", 0.85))
        points: Dict[str, float] = {}
        coverage: Dict[str, float] = {}
        for day, block in daily.items():
            prices = block["family_prices"]
            numerator = denominator = in_weight = 0.0
            for key, member in members.items():
                stats = prices.get(key)
                if not stats:
                    continue
                unit = _unit_price(stats, {"prompt_tokens": member["base_prompt_share"],
                                           "paid_tokens": 1.0})
                if unit is None or unit <= 0:
                    continue
                numerator += member["weight"] * unit
                denominator += member["weight"] * member["base_unit_price_usd_per_mtok"]
                in_weight += member["weight"]
            coverage[day] = round(in_weight, 4)
            if denominator > 0 and in_weight >= min_weight:
                points[day] = numerator / denominator * 100.0
        return {"usable": bool(points), "points": points, "in_basket_weight": coverage,
                "min_in_basket_weight": min_weight,
                "reason": None if points else
                          f"在场权重始终低于 {min_weight}，篮子塌了不出指数"}

    def token_apps(self, anchor: str, site_tokens: Optional[int]) -> Dict[str, Any]:
        """调用方维度：谁在消费这些 token。展示逻辑同日度构成——锚定日定死条带。

        份额的分母刻意是**榜上应用的合计**，不是全站总量。原因是榜单只回一段
        名次，而且名次不连续（实测 20 行里 rank 跳过 2/3/4/15/16/18），拿全站
        总量当分母算出来的百分比既不是"占全站"（漏了没上榜的和没有 app 归属的
        直连流量），也不是"占应用侧"（漏了不公开露出的名次），两头不靠。

        所以这里出两个数，各说各话：
          * `bands[].share` —— 在**榜上这些应用之间**的占比，分母干净；
          * `listed_share_of_site` —— 榜上合计占当日全站的比例，**是下界**。
        """
        cfg = (self.basket_cfg.get("composition") or {})
        top_n = int(cfg.get("apps_top_n", cfg.get("top_n", 7)))
        rows_by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in self.apps:
            if (row.get("quality_flag") or "ok") != "ok":
                continue
            rows_by_day[_d(row["obs_date"])].append(row)
        if not rows_by_day:
            return {"usable": False, "reason": "没有可用的应用榜观测"}
        if anchor not in rows_by_day:
            # 应用榜沿用模型榜的结算日，正常情况下两者同日；真不同日时宁可退到
            # 应用榜自己最新的那天，也不要拿别的日子冒充锚定日。
            anchor = max(rows_by_day)

        def tally(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
            out: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                key = str(row["app_id"])
                slot = out.setdefault(key, {
                    "tokens": 0, "requests": 0, "rank": row.get("rank"),
                    "label": row.get("app_title") or row.get("app_slug") or key,
                    "slug": row.get("app_slug"),
                    "url": row.get("app_url"),
                    "categories": _json_list(row.get("categories")),
                })
                slot["tokens"] += int(row.get("total_tokens") or 0)
                slot["requests"] += int(row.get("total_requests") or 0)
            return out

        anchor_tally = tally(rows_by_day[anchor])
        ranked = sorted(anchor_tally.items(), key=lambda kv: -kv[1]["tokens"])
        top_keys = [k for k, _ in ranked[:top_n]]
        listed_total = sum(v["tokens"] for v in anchor_tally.values())

        series = []
        for day in sorted(rows_by_day):
            day_tally = tally(rows_by_day[day])
            values = {k: day_tally.get(k, {}).get("tokens", 0) for k in top_keys}
            total = sum(v["tokens"] for v in day_tally.values())
            values["__other__"] = total - sum(values.values())
            series.append({"date": day, "total": total, "values": values})

        bands = []
        for key in top_keys:
            stats = anchor_tally[key]
            bands.append({
                "key": key,
                "label": stats["label"],
                "slug": stats["slug"],
                "url": stats["url"],
                "categories": stats["categories"],
                "rank": stats["rank"],
                "tokens": stats["tokens"],
                "share": (round(stats["tokens"] / listed_total, 4)
                          if listed_total else None),
                "requests": stats["requests"],
                "tokens_per_request": (round(stats["tokens"] / stats["requests"], 1)
                                       if stats["requests"] else None),
            })
        other_tokens = listed_total - sum(b["tokens"] for b in bands)
        other_requests = (sum(v["requests"] for v in anchor_tally.values())
                          - sum(b["requests"] for b in bands))
        bands.append({
            "key": "__other__",
            "label": "榜上其余应用",
            "slug": None, "url": None, "categories": None, "rank": None,
            "tokens": other_tokens,
            "share": round(other_tokens / listed_total, 4) if listed_total else None,
            "requests": other_requests,
            "tokens_per_request": (round(other_tokens / other_requests, 1)
                                   if other_requests else None),
            "app_count": len(anchor_tally) - len(top_keys),
        })

        ranks = [v["rank"] for v in anchor_tally.values() if v["rank"] is not None]
        max_rank = max(ranks) if ranks else None
        hidden = (max_rank - len(anchor_tally)) if max_rank else 0

        # 类别是应用自己填的标签，只做归并展示，不当分类学用
        cat_tokens: Dict[str, int] = defaultdict(int)
        for stats in anchor_tally.values():
            for cat in (stats["categories"] or ["未标类别"]):
                cat_tokens[cat] += stats["tokens"]

        return {
            "usable": True,
            "anchor_date": anchor,
            "top_n": top_n,
            "series": series,
            "bands": bands,
            "listed_tokens_anchor": listed_total,
            "listed_app_count_anchor": len(anchor_tally),
            "site_tokens_anchor": site_tokens,
            "listed_share_of_site": (round(listed_total / site_tokens, 4)
                                     if site_tokens else None),
            "max_rank": max_rank,
            "hidden_ranks": max(hidden, 0),
            "categories_by_tokens": dict(sorted(cat_tokens.items(),
                                                key=lambda kv: -kv[1])),
            "caveat": ("榜单只回一段名次且名次不连续，合计是应用侧总量的下界；"
                       "剩下的部分混着未上榜应用、不公开露出的名次，"
                       "以及压根没有 app 归属的直连 API 流量，三者拆不开"),
            "no_spend_reason": "应用榜只给 token 与请求数，不拆模型，spend 无从归属",
        }

    def token_composition(self, anchor: str) -> Dict[str, Any]:
        """日度总量拆成「前 N 个模型 + 其他」，给堆叠面积图用。

        条带集合由**锚定日**的排名一次定死，然后每一天都按同一组条带拆。
        不这么做的话，每天各自取前 7 名，条带的含义会天天变，叠出来的面积图
        看着连续、其实是一堆不同的东西拼在一起。

        量用总 token（含零价），因为问题问的是"调用量的构成"；但每条带子都标了
        是否有价——榜首那条 32.3% 是匿名 stealth 模型在免费放量，把它当成收入看
        会错得离谱。
        """
        cfg = (self.basket_cfg.get("composition") or {})
        top_n = int(cfg.get("top_n", 7))
        rank_by = str(cfg.get("rank_by", "tokens"))
        split_variant = bool(cfg.get("split_variant", True))
        days = self._token_days()
        if not days or anchor not in days:
            return {"usable": False, "reason": "没有可用的日度 token 观测"}

        def key_of(row: Dict[str, Any]) -> str:
            slug = row.get("model_slug") or "?"
            return f"{slug}:{row.get('variant')}" if split_variant else slug

        def tally(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
            out: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                key = key_of(row)
                slot = out.setdefault(key, {"tokens": 0, "requests": 0,
                                           "spend": 0.0, "priced_tokens": 0,
                                           "is_priced": False})
                volume = ((row.get("prompt_tokens") or 0)
                          + (row.get("completion_tokens") or 0))
                slot["tokens"] += volume
                slot["requests"] += int(row.get("requests") or 0)
                slot["spend"] += _f(row.get("spend_usd")) or 0.0
                # 单价的分母只能是「配到了挂牌价的 token」。未匹配的行 spend 是
                # 缺失不是 0，混进分母会把一个未知稀释成一个看起来很便宜的数。
                if row.get("price_match") != "unmatched":
                    slot["priced_tokens"] += volume
                slot["is_priced"] = bool(slot["is_priced"] or row.get("is_priced"))
            return out

        anchor_tally = tally(days[anchor])
        metric = "requests" if rank_by == "requests" else "tokens"
        ranked = sorted(anchor_tally.items(), key=lambda kv: -kv[1][metric])
        top_keys = [k for k, _ in ranked[:top_n]]
        anchor_total = sum(v["tokens"] for v in anchor_tally.values())

        series = []
        for day in sorted(days):
            day_tally = tally(days[day])
            values = {k: day_tally.get(k, {}).get("tokens", 0) for k in top_keys}
            total = sum(v["tokens"] for v in day_tally.values())
            values["__other__"] = total - sum(values.values())
            series.append({"date": day, "total": total, "values": values})

        bands = []
        for key in top_keys:
            stats = anchor_tally[key]
            bands.append({
                "key": key,
                "label": key.rsplit(":", 1)[0] if split_variant else key,
                "variant": key.rsplit(":", 1)[-1] if split_variant else None,
                "tokens": stats["tokens"],
                "share": (round(stats["tokens"] / anchor_total, 4)
                          if anchor_total else None),
                "requests": stats["requests"],
                "tokens_per_request": (round(stats["tokens"] / stats["requests"], 1)
                                       if stats["requests"] else None),
                "spend_usd": round(stats["spend"], 2),
                "unit_price_usd_per_mtok": _unit_price_per_mtok(
                    stats["spend"], stats["priced_tokens"]),
                "priced_tokens": stats["priced_tokens"],
                "is_priced": stats["is_priced"],
            })
        other_tokens = anchor_total - sum(b["tokens"] for b in bands)
        other_requests = (sum(v["requests"] for v in anchor_tally.values())
                          - sum(b["requests"] for b in bands))
        bands.append({
            "key": "__other__", "label": "其他",
            "variant": None,
            "tokens": other_tokens,
            "share": round(other_tokens / anchor_total, 4) if anchor_total else None,
            "requests": other_requests,
            "tokens_per_request": (round(other_tokens / other_requests, 1)
                                   if other_requests else None),
            "spend_usd": round(sum(v["spend"] for v in anchor_tally.values())
                               - sum(b["spend_usd"] for b in bands), 2),
            "unit_price_usd_per_mtok": None,   # 下面按余数重算，摆在这里只是占位
            "priced_tokens": (sum(v["priced_tokens"] for v in anchor_tally.values())
                              - sum(b["priced_tokens"] for b in bands)),
            "is_priced": None,
            "model_count": len(anchor_tally) - len(top_keys),
        })
        bands[-1]["unit_price_usd_per_mtok"] = _unit_price_per_mtok(
            bands[-1]["spend_usd"], bands[-1]["priced_tokens"])
        return {
            "usable": True,
            "anchor_date": anchor,
            "top_n": top_n,
            "ranked_by": metric,
            "split_variant": split_variant,
            "total_tokens_anchor": anchor_total,
            "model_count_anchor": len(anchor_tally),
            "bands": bands,
            "series": series,
            "note": ("条带由锚定日的排名定死，之后每天按同一组条带拆——"
                     "每天各取前 N 名会让条带含义天天变，叠出来的面积图是假的"),
        }

    def token_history(self, daily: Dict[str, Dict[str, Any]],
                      anchor: str) -> Dict[str, Any]:
        """厂商级周度历史：本 skill 唯一一条能回填到一年前的量序列。

        三条纪律写进数据结构里，不靠读者自觉：
          * `unit_basis` 标成未核实，因为它与日榜对不上（实测同日各厂商比值
            1.42–1.75，非常数倍）。所以这里只出**份额、增速、指数**，不出绝对水平。
          * 不与日度序列相减。桥接比值要等日度序列盖满一个已结算周才算得出来，
            算不出来就明说算不出来。
          * 「结构效应指数」用的是**今天的厂商均价 × 历史的厂商份额**，
            测的只有购买结构迁移这一件事，不含任何真实降价。
        """
        try:
            rows = db_adapter.read_token_history("2000-01-01", self.asof)
        except Exception:
            rows = []
        if not rows:
            return {"usable": False,
                    "reason": "库里没有周度历史（跑 scripts/backfill.py 回填）"}

        weeks: Dict[str, Dict[str, float]] = defaultdict(dict)
        unit_bases = set()
        for row in rows:
            if (row.get("quality_flag") or "ok") != "ok" or not row.get("settled"):
                continue
            value = _f(row.get("tokens"))
            if value is None:
                continue
            weeks[_d(row["week_start"])][row["author"]] = value
            unit_bases.add(row.get("unit_basis"))
        if not weeks:
            return {"usable": False, "reason": "周度历史里没有已结算的行"}

        ordered = sorted(weeks)
        totals = {week: sum(values.values()) for week, values in weeks.items()}

        # 今天的厂商均价（USD/Mtok），只用明码有价的行
        latest_rows = [r for r in self.tokens if _d(r["obs_date"]) == anchor]
        author_spend: Dict[str, float] = defaultdict(float)
        author_tokens: Dict[str, float] = defaultdict(float)
        for row in latest_rows:
            if not row.get("is_priced"):
                continue
            slug = row.get("model_slug") or ""
            author = slug.split("/")[0] if "/" in slug else "others"
            spend = _f(row.get("spend_usd")) or 0.0
            volume = (row.get("prompt_tokens") or 0) + (row.get("completion_tokens") or 0)
            author_spend[author] += spend
            author_tokens[author] += volume
        author_price = {a: author_spend[a] / author_tokens[a] * 1e6
                        for a in author_tokens if author_tokens[a] > 0}

        # 结构效应：今天的价 × 历史的份额。只有这一个变量在动，所以它测的就是
        # 「需求往贵的还是便宜的厂商挪」，与真实降价无关。
        effect: Dict[str, float] = {}
        priced_cover: Dict[str, float] = {}
        for week in ordered:
            values = weeks[week]
            total = totals[week] or 0.0
            covered = sum(v for a, v in values.items() if a in author_price)
            priced_cover[week] = round(covered / total, 4) if total else 0.0
            if not covered:
                continue
            # 在有价厂商内部归一化，避免"没价的厂商"份额变动被误读成结构效应
            effect[week] = sum(values[a] / covered * author_price[a]
                               for a in values if a in author_price)
        min_cover = 0.70
        usable_weeks = [w for w in ordered
                        if w in effect and priced_cover[w] >= min_cover]
        effect_index: Dict[str, float] = {}
        if usable_weeks:
            base_week = usable_weeks[0]
            base_value = effect[base_week]
            if base_value:
                effect_index = {w: effect[w] / base_value * 100.0 for w in usable_weeks}

        total_index = {}
        if ordered and totals[ordered[0]]:
            total_index = {w: totals[w] / totals[ordered[0]] * 100.0 for w in ordered}

        def growth(weeks_back: int) -> Dict[str, Any]:
            if len(ordered) <= weeks_back:
                return {"pct": None,
                        "reason": f"历史只有 {len(ordered)} 周，不足 {weeks_back} 周"}
            new, old = totals[ordered[-1]], totals[ordered[-1 - weeks_back]]
            return {"pct": round(pct_change(new, old), 4),
                    "from_week": ordered[-1 - weeks_back], "to_week": ordered[-1]}

        latest_week = ordered[-1]
        shares = {a: round(v / totals[latest_week], 4)
                  for a, v in sorted(weeks[latest_week].items(), key=lambda kv: -kv[1])
                  } if totals[latest_week] else {}
        first_shares = {a: round(v / totals[ordered[0]], 4)
                        for a, v in weeks[ordered[0]].items()} if totals[ordered[0]] else {}

        # 桥接比值：日度序列盖满某个已结算周之后才算得出来，算不出来就明说
        daily_weeks: Dict[str, float] = defaultdict(float)
        daily_days: Dict[str, set] = defaultdict(set)
        for day, block in daily.items():
            monday = (date.fromisoformat(day)
                      - timedelta(days=date.fromisoformat(day).weekday())).isoformat()
            daily_weeks[monday] += float(block["total_tokens"])
            daily_days[monday].add(day)
        bridge = {"usable": False,
                  "reason": "日度序列还没盖满任何一个已结算周，桥接比值算不出来"}
        for week in reversed(ordered):
            if len(daily_days.get(week, ())) == 7 and totals[week]:
                bridge = {
                    "usable": True, "week": week,
                    "history_over_daily": round(totals[week] / daily_weeks[week], 4),
                    "note": ("两条序列在同一周上的比值。稳定下来之后才谈得上换算，"
                             "在那之前它们只能各读各的"),
                }
                break

        return {
            "usable": True,
            "grain": "author_weekly",
            "unit_basis": sorted(u for u in unit_bases if u),
            "source_dataset": "openrouter/market-share",
            "first_week": ordered[0], "last_week": latest_week, "weeks": len(ordered),
            "caveat": ("与日度模型级序列口径不同（实测同日各厂商比值 1.42–1.75，"
                       "非常数倍），只能看份额与增速，不能读绝对水平；"
                       "最新那个未结算的点在采集时就已丢弃"),
            "volume_index": {
                "base_week": ordered[0], "base": 100,
                "series": [{"date": w, "value": round(v, 4)}
                           for w, v in sorted(total_index.items())],
                "growth_4w": growth(4), "growth_13w": growth(13),
                "growth_52w": growth(52),
            },
            "author_shares": {
                "latest_week": latest_week, "latest": shares,
                "first_week": ordered[0], "first": first_shares,
            },
            "structure_effect_index": {
                "usable": bool(effect_index),
                "reason": (None if effect_index else
                           "没有任何一周的有价厂商覆盖率达到门槛"),
                "base_week": usable_weeks[0] if usable_weeks else None,
                "base": 100,
                "definition": ("今天的厂商均价 × 历史的厂商份额。只有份额在动，"
                               "所以它测的就是购买结构迁移，不含任何真实降价"),
                "blind_spot": "厂商级粒度：厂商内部的模型迁移看不见",
                "min_priced_coverage": min_cover,
                "priced_coverage_latest": priced_cover.get(latest_week),
                "series": [{"date": w, "value": round(v, 4)}
                           for w, v in sorted(effect_index.items())],
            },
            "unit_bridge": bridge,
        }

    def token_market(self, gpu_anchor: Optional[str]) -> Dict[str, Any]:
        guards = (self.basket_cfg.get("guards") or {})
        cache_cfg = (self.basket_cfg.get("cache_sensitivity") or {})
        daily = self.token_daily()
        if not daily:
            return {"usable": False,
                    "reason": "库里没有任何 token 观测（openrouter 源未采或采集失败）",
                    "cost_floor": _not_enabled(), "margin_pool": _not_enabled()}
        anchor = max(daily)
        latest = daily[anchor]
        basket = self.token_basket(daily)
        laspeyres = self.token_laspeyres(daily, basket)

        paid_series = {d: float(v["paid_tokens"]) for d, v in daily.items()
                       if v["paid_tokens"]}
        spend_series = {d: v["spend_usd"] for d, v in daily.items() if v["spend_usd"]}
        blended_series = {d: v["blended_usd_per_mtok"] for d, v in daily.items()
                          if v["blended_usd_per_mtok"]}
        free_series = {d: float(v["free_variant_tokens"]) for d, v in daily.items()}
        zero_series = {d: float(v["zero_priced_standard_tokens"])
                       for d, v in daily.items()}
        tpr_series = {d: v["tokens_per_request"] for d, v in daily.items()
                      if v["tokens_per_request"]}

        unmatched_share = latest["unmatched_share"]
        max_unmatched = float(guards.get("max_unmatched_token_share", 0.03))
        coverage_ok = unmatched_share is not None and unmatched_share <= max_unmatched
        coverage_reason = (None if coverage_ok else
                           f"未匹配 token 占比 {(unmatched_share or 0) * 100:.2f}%"
                           f" 超过 {max_unmatched * 100:.0f}% 的守卫线，篮子残缺")

        blended = self.changes(blended_series, anchor_date=anchor)
        lasp = self.changes(laspeyres["points"], anchor_date=anchor)
        volume = self.changes(paid_series, anchor_date=anchor)
        spend = self.changes(spend_series, anchor_date=anchor)

        mix: Dict[str, Any] = {}
        decomposition: Dict[str, Any] = {}
        for label, _days in CHANGE_WINDOWS:
            g_blended = _usable_pct(blended.get("changes", {}).get(label))
            g_lasp = _usable_pct(lasp.get("changes", {}).get(label))
            g_volume = _usable_pct(volume.get("changes", {}).get(label))
            g_spend = _usable_pct(spend.get("changes", {}).get(label))
            if g_blended is None or g_lasp is None:
                mix[label] = {"pct_points": None,
                              "reason": "混合价或固定篮子指数有一头不可用"}
            else:
                mix[label] = {
                    "pct_points": round(g_blended - g_lasp, 4),
                    "blended_pct": g_blended,
                    "laspeyres_pct": g_lasp,
                    "meaning": ("负值 = 需求在往更便宜的模型迁移；"
                                "正值 = 在往更贵的模型迁移"),
                }
            if None in (g_spend, g_volume, g_lasp):
                decomposition[label] = {"usable": False,
                                        "reason": "三项里有一项不可用，不做分解"}
            else:
                mix_part = g_spend - g_volume - g_lasp
                decomposition[label] = {
                    "usable": True,
                    "spend_pct": g_spend,
                    "volume_pct": g_volume,
                    "true_price_pct": g_lasp,
                    "mix_residual_pct": round(mix_part, 4),
                    "note": "近似式 g_spend ≈ g_volume + g_真价格 + g_结构；残差即结构项",
                }

        hit_rates = cache_cfg.get("assumed_hit_rates") or []
        ratio = float(cache_cfg.get("cache_read_price_ratio", 0.119))
        nominal = latest["spend_usd"]
        sensitivity = []
        for rate in hit_rates:
            rate = float(rate)
            actual = (latest["prompt_spend_usd"] * (1 - rate)
                      + latest["prompt_spend_usd"] * rate * ratio
                      + latest["completion_spend_usd"])
            sensitivity.append({
                "assumed_cache_hit_rate": rate,
                "implied_actual_spend_usd": round(actual, 2),
                "nominal_overstatement_pct": (round((nominal / actual - 1) * 100, 2)
                                              if actual else None),
            })

        concentration_line = float(guards.get("concentration_warn_share", 0.25))
        top_share = latest["top_model_share"]
        return {
            "usable": True,
            "anchor_date": anchor,
            "anchor_lag_days": (date.fromisoformat(self.asof)
                                - date.fromisoformat(anchor)).days,
            "gpu_anchor_date": gpu_anchor,
            "alignment_lag_days": ((date.fromisoformat(gpu_anchor)
                                    - date.fromisoformat(anchor)).days
                                   if gpu_anchor else None),
            "series_start": min(daily),
            "series_days": len(daily),
            "coverage": {
                "matched_token_share": (round(1 - unmatched_share, 4)
                                        if unmatched_share is not None else None),
                "unmatched_token_share": (round(unmatched_share, 4)
                                          if unmatched_share is not None else None),
                "guard_max_unmatched_share": max_unmatched,
                "usable": coverage_ok,
                "reason": coverage_reason,
                "row_count": latest["row_count"],
            },
            "concentration": {
                "top_model": latest["top_model"],
                "top_model_share": (round(top_share, 4)
                                    if top_share is not None else None),
                "warn_line": concentration_line,
                "concentration_warning": bool(top_share and top_share > concentration_line),
                "meaning": ("单模型份额过高时，它进出榜单会让总量序列直接跳一大截，"
                            "总量线只能当背景，不参与变化率结论"),
            },
            "volume": {
                "paid": {**volume,
                         "series": _series_list(paid_series, self.asof, None),
                         "series_scope": FULL_HISTORY_SCOPE},
                "free_variant": {**self.changes(free_series, anchor_date=anchor),
                                 "series": _series_list(free_series, self.asof, None),
                                 "series_scope": FULL_HISTORY_SCOPE},
                "zero_priced_standard": {
                    **self.changes(zero_series, anchor_date=anchor),
                    "series": _series_list(zero_series, self.asof, None),
                    "series_scope": FULL_HISTORY_SCOPE,
                    "note": "零价的 standard 变体，主体是匿名 stealth 模型在免费放量",
                },
                "total_tokens_latest": latest["total_tokens"],
                "prompt_share": (round(latest["prompt_tokens"] / latest["paid_tokens"], 4)
                                 if latest["paid_tokens"] else None),
                "requests_latest": latest["requests"],
                "tokens_per_request": {
                    **self.changes(tpr_series, anchor_date=anchor),
                    "note": ("reasoning / cached / tool_calls 三个字段源侧全为 0，"
                             "token 通胀拆不了，这个比值是仅有的代理"),
                },
            },
            "price": {
                "blended": {**blended, "unit": "USD/Mtok",
                            "usable": coverage_ok,
                            "reason": coverage_reason,
                            "series": _series_list(blended_series, self.asof, None),
                            "series_scope": FULL_HISTORY_SCOPE,
                            "note": "当期权重，会被购买结构迁移污染，单看没有意义"},
                "laspeyres": {**lasp, "unit": "index, base=100",
                              "usable_index": laspeyres["usable"],
                              "reason": laspeyres.get("reason"),
                              "in_basket_weight": laspeyres.get("in_basket_weight"),
                              "series": _series_list(laspeyres["points"], self.asof, None),
                              "series_scope": FULL_HISTORY_SCOPE,
                              "note": "锁基期家族篮子权重与输入输出结构，只让单价动"},
                "mix_shift": mix,
                "basket": {k: v for k, v in basket.items() if k != "members"},
                "basket_members_top": _top_members(basket),
                "provider_band": latest["provider_band"],
            },
            "spend": {
                "nominal_usd_per_day": {
                    **spend,
                    "series": _series_list(spend_series, self.asof, None),
                    "series_scope": FULL_HISTORY_SCOPE,
                    "usable": coverage_ok,
                    "reason": coverage_reason,
                    "definition": ("按挂牌价计的名义支出，不是实际账单："
                                   "缓存命中只按约 12% 计费，而命中率不可观测"),
                },
                "prompt_spend_share": (round(latest["prompt_spend_usd"] / nominal, 4)
                                       if nominal else None),
                "cache_sensitivity": sensitivity,
                "decomposition": decomposition,
            },
            "composition": self.token_composition(anchor),
            "apps": self.token_apps(anchor, latest.get("total_tokens")),
            "history": self.token_history(daily, anchor),
            "cost_floor": _not_enabled(),
            "margin_pool": _not_enabled(),
            "sources": _token_source_view(self.tokens, anchor),
        }

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

        # GPU 侧的锚定日：三个主力 SKU 里最新的那个。token 侧要报出与它的差，
        # 两端不同日却当成同一天读，是跨维度比较最容易犯的错。
        gpu_anchor_dates = [c.get("anchor_date") for c in medians.values()
                            if c.get("anchor_date")]
        gpu_anchor = max(gpu_anchor_dates) if gpu_anchor_dates else None

        return {
            "schema": "gpu-compute-monitor/evidence/1.1",
            "asof": self.asof,
            "window_days": self.window,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "models": per_model,
            "reference_models": self.reference_models(),
            "generation_premium": self.generation_premium(medians),
            "token_market": self.token_market(gpu_anchor),
            "source_health": self.freshness(),
            "config": {
                "core_price_types": list(CORE_PRICE_TYPES),
                "scoring": self.thresholds["scoring"],
                "alert_mode": self.thresholds["alerts"]["mode"],
                "alert_direction": self.thresholds["alerts"]["direction"],
                "confirmation": self.thresholds["confirmation"],
                "token_basket": {k: v for k, v in (self.basket_cfg.get("basket") or {}).items()
                                 if k != "chain_links"},
                "token_guards": self.basket_cfg.get("guards") or {},
            },
        }


def persist_alerts(evidence: Dict[str, Any]) -> int:
    """把当天触发的告警写进 gpu_alerts（PRD §6.1 步骤 8）。

    先清掉当天的旧行再写：阈值调过之后，昨天触发、今天不该触发的那条
    否则会永远留在库里，把后面的连续性判断带偏。
    """
    asof = evidence["asof"]
    models = list((evidence.get("models") or {}).keys())
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
    # metrics.py 可以独立运行；升级后的旧库可能还没有 gpu_alerts。
    db_adapter.init_schema()
    return db_adapter.replace_alerts(asof, models, rows)


def _unit_price_per_mtok(spend_usd: float,
                         priced_tokens: int) -> Optional[float]:
    """一条带子的挂牌单价，USD/Mtok。分母是配到价的 token，不是总 token。

    没有任何 token 配上价时返回 None——那是"不知道"，不是"零"。零价模型
    （pricing 明写 0）是配上了价的，会正常算出 0.0，两者必须分得开。
    """
    if not priced_tokens:
        return None
    return round(float(spend_usd) / priced_tokens * 1e6, 4)


def _json_list(value: Any) -> Optional[List[str]]:
    """JSON 列在 PG 里回来是 list，在 SQLite 里回来是字符串。两种都要认。"""
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return [str(v) for v in parsed] if isinstance(parsed, list) else None
    return None


def _usable_pct(change: Optional[Dict[str, Any]]) -> Optional[float]:
    """只有基准点落在容差内的变化率才拿来用。"""
    if not change or change.get("pct") is None or not change.get("usable"):
        return None
    return float(change["pct"])


def _series_list(series: Dict[str, float], asof: str,
                 window: Optional[int]) -> List[Dict[str, Any]]:
    """把日度字典摊成序列。`window=None` 表示不裁剪，出全部历史。

    补不回去的序列（供给、token）传 None：见 FULL_HISTORY_START。
    """
    start = ("" if window is None
             else (date.fromisoformat(asof) - timedelta(days=window)).isoformat())
    return [{"date": k, "value": round(float(v), 6)}
            for k, v in sorted(series.items()) if start <= k <= asof]


def _unit_price(prices: Dict[str, Any], weights: Dict[str, Any]) -> Optional[float]:
    """一个家族「一个 token」的单价，输入输出比例由 weights 决定。

    基期算篮子时 weights 就是它自己（当期结构）；之后每天都拿基期的结构去乘
    当期的单价——这样连"输入输出比例变了"这层 mix 也被锁住，剩下的变动
    才是纯粹的挂牌价变动。
    """
    p_prompt = prices.get("price_prompt")
    p_completion = prices.get("price_completion")
    if p_prompt is None and p_completion is None:
        return None
    if p_prompt is None:
        p_prompt = p_completion
    if p_completion is None:
        p_completion = p_prompt
    total = weights.get("paid_tokens") or 0
    raw_prompt = weights.get("prompt_tokens")
    if not total or raw_prompt is None:
        return None
    share = float(raw_prompt) / float(total)
    if share < 0 or share > 1:
        return None
    return share * float(p_prompt) + (1 - share) * float(p_completion)


def _basket_fingerprint(base_date: str, members: Dict[str, Any]) -> str:
    """篮子指纹。成员或基期变了，指纹就变，两段 Laspeyres 不许直接相减。"""
    blob = json.dumps({"base": base_date, "members": sorted(members)},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _top_members(basket: Dict[str, Any], limit: int = 8) -> List[Dict[str, Any]]:
    members = basket.get("members") or {}
    ranked = sorted(members.items(), key=lambda kv: -(kv[1].get("weight") or 0))
    return [{"member": k, "weight": round(v.get("weight") or 0, 4),
             "base_unit_price_usd_per_mtok": round(
                 v.get("base_unit_price_usd_per_mtok") or 0, 4)}
            for k, v in ranked[:limit]]


def _not_enabled() -> Dict[str, Any]:
    """成本地板与毛利池的插槽：键位现在就定好，值恒为不可用。

    留形不留值是刻意的——将来补上人工吞吐表时不用改 schema，
    也不会有人误以为现在这两个数已经能读。
    """
    return {"usable": False, "reason": "未启用（P1）",
            "note": "需要人工核对的 tokens/sec/GPU 吞吐表，本轮范围之外"}


def _token_source_view(rows: List[Dict[str, Any]], anchor: str) -> Dict[str, Any]:
    """按源分开的原始视图。将来接第二个源时，这里是它落脚的地方，
    而主指数仍然只由能同时给量和价的那个源建。"""
    out: Dict[str, Any] = {}
    for row in rows:
        if _d(row["obs_date"]) != anchor:
            continue
        block = out.setdefault(row["source"], {
            "rows": 0, "coverage_scope": row.get("coverage_scope"),
            "price_basis": row.get("price_basis"), "fingerprints": set(),
        })
        block["rows"] += 1
        if row.get("query_fingerprint"):
            block["fingerprints"].add(row["query_fingerprint"])
    for block in out.values():
        block["fingerprints"] = sorted(block["fingerprints"])
        block["fingerprint_stable"] = len(block["fingerprints"]) <= 1
    return out


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
