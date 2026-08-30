#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信用梯级：档位聚合、跨档距离、离散度、长短端分化。

单看「ORCL 205bp」读不出任何东西。有意义的是它在梯级里的位置，以及这个位置
在移动。这个模块把发行人层的曲线摊成一把标尺，并算出方法论判据 2 和判据 4
要的那几个数。

**档位归属由配置维护，脚本不自动调整。** 标尺本身会漂，重新分档是判断不是计算。
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Sequence


def _cm(issuer_block: Dict[str, Any], tenor: str) -> Optional[float]:
    return ((issuer_block or {}).get("constant_maturity_bp") or {}).get(tenor)


def _bucket(issuer_block: Dict[str, Any], bucket: str) -> Optional[float]:
    return ((issuer_block or {}).get("buckets") or {}).get(bucket, {}).get("mean_bp")


def build_rungs(curves: Dict[str, Dict[str, Any]],
                issuers_cfg: Dict[str, Any],
                rungs_cfg: Dict[Any, Any],
                *,
                index_oas_bp: Optional[Dict[str, float]] = None,
                anchor_index_oas_bp: Optional[Dict[str, float]] = None,
                default_bench: str = "ig") -> List[Dict[str, Any]]:
    """把发行人按档位摊开，每档给一个代表读数（5–10Y 桶均值的中位数）。

    每档同时给**两个口径的锚点漂移**，因为它们回答的不是同一个问题：

    * `drift_vs_anchor_bp` 是 G-spread 口径。G-spread 只剥了利率 beta
      （它的定义式就是收益率减插值国债），信用市场 beta 还在里面。IG 指数 OAS
      从 81 走到 95，七个档的这一列会集体 +14bp，读起来像「AI 信用全面恶化」，
      其实是整个投资级市场在走宽。
    * `drift_vs_anchor_excess_bp` 是超额口径：先减掉对应指数 OAS 再跟锚点比，
      等价于 `G-spread 口径漂移 − 指数 OAS 自身的漂移`。**它才是「这一档相对
      市场额外走了多少」。**

    两个都留着是有意的：前者回答「现在的绝对补偿是多少」，后者回答「这是不是
    AI 自己的事」。**只看后者会漏掉整体走宽本身就是风险这件事。**

    `index_oas_bp` 是当日指数 OAS（{"ig": 81.0, "hy": 270.0}），
    `anchor_index_oas_bp` 是锚点那天的同一组数。缺任何一个，超额口径出 None
    并在 `excess_quality` 里说明缺在哪，**不拿别的日期顶替**——用错日期的指数值
    算出来的「超额漂移」比没有更坏。

    档位对哪条指数：`rungs_cfg[rung]["bench"]`，缺省 ig。纯算力商挂 hy，
    这跟 attribution 里市场 beta 的分段口径必须一致。
    """
    index_oas_bp = index_oas_bp or {}
    anchor_index_oas_bp = anchor_index_oas_bp or {}

    grouped: Dict[int, List[str]] = {}
    for key, meta in issuers_cfg.items():
        rung = meta.get("rung")
        if rung is None:
            continue
        grouped.setdefault(int(rung), []).append(key)

    out: List[Dict[str, Any]] = []
    for rung in sorted(grouped):
        members = sorted(grouped[rung])
        readings = {m: _bucket(curves.get(m, {}), "5-10y") for m in members}
        live = [v for v in readings.values() if v is not None]
        meta = rungs_cfg.get(rung) or rungs_cfg.get(str(rung)) or {}
        median = round(statistics.median(live), 1) if live else None
        anchor = meta.get("anchor_5_10y")

        bench = meta.get("bench") or default_bench
        bench_bp = index_oas_bp.get(bench)
        anchor_bench_bp = anchor_index_oas_bp.get(bench)
        excess = (None if median is None or bench_bp is None
                  else round(median - bench_bp, 1))
        anchor_excess = (None if anchor is None or anchor_bench_bp is None
                         else round(anchor - anchor_bench_bp, 1))
        if median is None:
            excess_quality = "no_reading"
        elif bench_bp is None:
            excess_quality = "no_index_oas"
        elif anchor is None or anchor_bench_bp is None:
            excess_quality = "no_anchor_index_oas"
        else:
            excess_quality = "ok"

        out.append({
            "rung": rung,
            "name": meta.get("name", f"档 {rung}"),
            "role": meta.get("role"),
            "anchor_5_10y": anchor,
            "members": members,
            "readings_5_10y": readings,
            "median_5_10y": median,
            "drift_vs_anchor_bp": (None if median is None or anchor is None
                                   else round(median - anchor, 1)),
            # 超额口径：剔掉对应指数 OAS 之后的水平、锚点与漂移。
            "bench": bench,
            "bench_bp": bench_bp,
            "anchor_bench_bp": anchor_bench_bp,
            "excess_5_10y": excess,
            "anchor_excess_5_10y": anchor_excess,
            "drift_vs_anchor_excess_bp": (
                None if excess is None or anchor_excess is None
                else round(excess - anchor_excess, 1)),
            "excess_quality": excess_quality,
        })
    return out


def rung_gaps(curves: Dict[str, Dict[str, Any]],
              gaps_cfg: Sequence[Dict[str, Any]],
              *, utility_members: Sequence[str],
              index_oas_bp: Dict[str, float],
              anchor_index_oas_bp: Optional[Dict[str, float]] = None,
              issuers_cfg: Optional[Dict[str, Any]] = None,
              rungs_cfg: Optional[Dict[Any, Any]] = None,
              tenor_bucket: str = "5-10y") -> List[Dict[str, Any]]:
    """跨档距离 —— 判据 2 盯的那几个数。

    两个特殊主体：UTIL_MEDIAN 是受监管公用事业的中位数，IG_INDEX 是指数 OAS。
    它们俩的差是**反向证伪器**：显著转正说明 AI 故事进了受监管电力定价，
    框架要重写。
    """
    anchor_index_oas_bp = anchor_index_oas_bp or {}
    issuers_cfg = issuers_cfg or {}
    rungs_cfg = rungs_cfg or {}

    def value_of(token: str) -> Optional[float]:
        if token == "UTIL_MEDIAN":
            vals = [_bucket(curves.get(m, {}), tenor_bucket) for m in utility_members]
            vals = [v for v in vals if v is not None]
            return round(statistics.median(vals), 1) if vals else None
        if token == "IG_INDEX":
            return index_oas_bp.get("ig")
        if token == "HY_INDEX":
            return index_oas_bp.get("hy")
        return _bucket(curves.get(token, {}), tenor_bucket)

    def bench_of(token: str) -> Optional[str]:
        if token in ("UTIL_MEDIAN", "IG_INDEX"):
            return "ig"
        if token == "HY_INDEX":
            return "hy"
        issuer = issuers_cfg.get(token) or {}
        rung = issuer.get("rung")
        rung_meta = rungs_cfg.get(rung) or rungs_cfg.get(str(rung)) or {}
        return rung_meta.get("bench")

    out: List[Dict[str, Any]] = []
    for gap in gaps_cfg:
        a, b = value_of(gap["a"]), value_of(gap["b"])
        observed = None if (a is None or b is None) else round(a - b, 1)
        anchor = gap.get("anchor_bp")
        bench_a, bench_b = bench_of(gap["a"]), bench_of(gap["b"])

        # 同段 gap 里的指数 OAS 会在相减时自动抵掉；跨段 gap 不会。典型例子是
        # CRWV(HY) − ORCL(IG)：HY−IG 基差即使只因市场变化，也会原样进入 raw gap。
        # 所以只给跨段 gap 增加超额口径，避免重复展示同段完全相同的两列。
        cross_bench = bool(bench_a and bench_b and bench_a != bench_b)
        market_basis = (None if not cross_bench
                        else (None if index_oas_bp.get(bench_a) is None
                              or index_oas_bp.get(bench_b) is None
                              else round(index_oas_bp[bench_a] - index_oas_bp[bench_b], 1)))
        anchor_market_basis = (None if not cross_bench
                               else (None if anchor_index_oas_bp.get(bench_a) is None
                                     or anchor_index_oas_bp.get(bench_b) is None
                                     else round(anchor_index_oas_bp[bench_a]
                                                - anchor_index_oas_bp[bench_b], 1)))
        excess = (None if observed is None or market_basis is None
                  else round(observed - market_basis, 1))
        anchor_excess = (None if anchor is None or anchor_market_basis is None
                         else round(anchor - anchor_market_basis, 1))
        if not cross_bench:
            excess_quality = "same_bench_cancelled"
        elif observed is None:
            excess_quality = "regime_na"
        elif market_basis is None:
            excess_quality = "no_index_oas"
        elif anchor_excess is None:
            excess_quality = "no_anchor_index_oas"
        else:
            excess_quality = "ok"
        out.append({
            "id": gap["id"],
            "a": gap["a"], "b": gap["b"],
            "a_bp": a, "b_bp": b,
            "observed_bp": observed,
            "anchor_bp": anchor,
            "drift_bp": (None if observed is None or anchor is None
                         else round(observed - anchor, 1)),
            "bench_a": bench_a, "bench_b": bench_b,
            "market_basis_bp": market_basis,
            "anchor_market_basis_bp": anchor_market_basis,
            "observed_excess_bp": excess,
            "anchor_excess_bp": anchor_excess,
            "drift_excess_bp": (None if excess is None or anchor_excess is None
                                else round(excess - anchor_excess, 1)),
            "excess_quality": excess_quality,
            "means": gap.get("means"),
            "quality": "ok" if observed is not None else "regime_na",
        })
    return out


def dispersion(rungs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """梯级离散度 —— 各档代表读数的标准差。

    扩张 + 弱档走宽 = 质量分层；压缩 + 全档同向走宽 = 体系性；
    压缩 + 全档同向收窄 = 追逐收益。**三种读法完全不同，判别留给模型。**
    """
    vals = [r["median_5_10y"] for r in rungs if r.get("median_5_10y") is not None]
    if len(vals) < 3:
        return {"value": None, "n_rungs": len(vals), "quality": "insufficient_rungs"}
    return {
        "value": round(statistics.pstdev(vals), 1),
        "n_rungs": len(vals),
        "min_bp": min(vals),
        "max_bp": max(vals),
        "range_bp": round(max(vals) - min(vals), 1),
        "quality": "ok",
    }


def term_structure_split(curves: Dict[str, Dict[str, Any]],
                         a: str, b: str,
                         short_tenor: int, long_tenor: int) -> Dict[str, Any]:
    """判据 4 · 长短端分化：长端跨档差 − 短端跨档差。

    实测 META 对 GOOGL 在 5–10Y 差 28bp、在 20Y+ 差 72bp——同一个故事在长端
    定价了两倍多。这个差值收窄（短端追上来）= 担忧前移，是升级信号。
    """
    short_a, short_b = _cm(curves.get(a, {}), f"{short_tenor}y"), _cm(curves.get(b, {}), f"{short_tenor}y")
    long_a, long_b = _cm(curves.get(a, {}), f"{long_tenor}y"), _cm(curves.get(b, {}), f"{long_tenor}y")
    short_gap = None if short_a is None or short_b is None else round(short_a - short_b, 1)
    long_gap = None if long_a is None or long_b is None else round(long_a - long_b, 1)
    return {
        "pair": f"{a}-{b}",
        "short_tenor": short_tenor, "long_tenor": long_tenor,
        "short_gap_bp": short_gap, "long_gap_bp": long_gap,
        "split_bp": (None if short_gap is None or long_gap is None
                     else round(long_gap - short_gap, 1)),
        "quality": "ok" if short_gap is not None and long_gap is not None else "regime_na",
    }
