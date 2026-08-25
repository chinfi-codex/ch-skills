#!/usr/bin/env python3
"""跨日离群检测 —— PRD §6.1 步骤 4 里「异常值 / 单位错误」那一半。

采集器里已经有 28 处守卫拦住了空值、结构变化、算不出单卡价、低质供给。
它们都是「这一条数据本身不对」。这里补的是另一类：**数据本身长得很正常，
只是跟自己的历史对不上。** 页面改版、平台换实例规格、别名映射错位，
都会以「某天某个源的价格突然跳一大截」的形式出现，单看那一条看不出问题。

处理方式是打标不是丢弃：命中的行 `quality_flag='suspicious'`，指标层会把它
排除出跨平台中枢与评分，但行还在库里可以追溯。丢掉的话就没法事后判断
到底是真跳价还是采错了。

两类命中：
  * jump —— 相对滚动中位数的偏离超过阈值。
  * unit_error —— 偏离倍数正好落在常见 GPU 数（2/4/8）附近。这是单位换算
    出错的签名：整机价没除以卡数，或者除了两遍。它比普通跳价更值得单独点名，
    因为成因明确、且几乎一定是 bug 而不是市场。

被标记的只是「可疑」，不是「错」。真实市场也会跳——所以阈值配置化，
且冷启动期（历史点不足）一律不标。

比对基准必须取**这一行自己日期之前**的那段，不能拿全窗口中位数一把梭。
实测踩过：Ornn 每次返回 90 天历史，拿三个月前的 RTX 5090（1.33）去比最近
30 天的中位数（0.49），会把一段真实的 −38% 下跌整段标成可疑，一次误报 17 条。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import db_adapter  # noqa: E402
from collectors.base import percentile  # noqa: E402

# 单位换算出错时最常见的倍数。
# 4/8/16 倍这种幅度真实市场几乎不会一天走出来，所以不管有没有超过通用跳变
# 阈值都查——「多除了一遍」的 1/8 只偏离 87.5%，通用阈值反而盖不住它。
# 2 倍不一样：真实行情一个月翻倍是可能的，所以只在已经超过通用阈值时才认。
UNIT_FACTORS_ALWAYS = (4, 8, 16)
UNIT_FACTORS_ON_JUMP = (2,)


def _key(row: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
    return (str(row.get("source")), str(row.get("gpu_model")),
            str(row.get("price_type")), str(row.get("market_segment", "default")),
            str(row.get("region", "global")))


def _d(value: Any) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def _unit_error_factor(ratio: float, tolerance: float,
                       factors: Iterable[int]) -> Optional[str]:
    """倍数是否贴着常见 GPU 数。返回可读的成因描述，否则 None。"""
    for count in factors:
        if abs(ratio - count) / count <= tolerance:
            return f"约 {count}× —— 整机价可能没除以 node_gpu_count"
        if abs(ratio - 1.0 / count) * count <= tolerance:
            return f"约 1/{count} —— 单卡价可能被多除了一次"
    return None


def flag_outliers(rows: List[Dict[str, Any]], cfg: Dict[str, Any],
                  history: Optional[Dict[Tuple, Dict[str, float]]] = None,
                  obs_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """给本次采集的价格行打 quality_flag，返回命中的说明列表。

    直接修改 rows（原地打标），返回的是给运行摘要看的命中清单。
    每一行都跟**自己日期之前** lookback 天内的中位数比，不是跟全窗口比。
    """
    if not cfg.get("enabled", True) or not rows:
        return []
    lookback = int(cfg.get("lookback_days", 30))
    min_points = int(cfg.get("min_history_points", 5))
    max_jump = float(cfg.get("max_jump_pct", 60)) / 100.0
    unit_tol = float(cfg.get("unit_error_tolerance", 0.12))

    if history is None:
        asof = obs_date or _d(rows[0].get("obs_date"))
        start = (date.fromisoformat(asof) - timedelta(days=lookback)).isoformat()
        history = trailing_history(start, asof)

    # 本批自己的行也进基准池：一次回填 90 天时，序列内部的突刺得能被自己的
    # 前序点照出来，否则整段历史都没有基准可比。
    pool: Dict[Tuple, Dict[str, float]] = {k: dict(v) for k, v in history.items()}
    for row in rows:
        value = row.get("price_usd_gpu_hour")
        if value is not None:
            pool.setdefault(_key(row), {}).setdefault(_d(row["obs_date"]), float(value))

    hits: List[Dict[str, Any]] = []
    for row in rows:
        value = row.get("price_usd_gpu_hour")
        if value is None or row.get("quality_flag") not in (None, "ok"):
            continue
        row_day = _d(row["obs_date"])
        window_start = (date.fromisoformat(row_day)
                        - timedelta(days=lookback)).isoformat()
        series = pool.get(_key(row)) or {}
        past = [v for d, v in series.items() if window_start <= d < row_day]
        if len(past) < min_points:
            # 冷启动期没有可比基准，一律不标——宁可漏也不要冤枉真实行情
            continue
        baseline = percentile(sorted(past), 0.5)
        if not baseline:
            continue
        ratio = float(value) / baseline
        deviation = abs(ratio - 1.0)
        # 先查单位换算签名（与幅度无关），再查通用跳变
        unit_hint = _unit_error_factor(ratio, unit_tol, UNIT_FACTORS_ALWAYS)
        if unit_hint is None:
            if deviation <= max_jump:
                continue
            unit_hint = _unit_error_factor(ratio, unit_tol, UNIT_FACTORS_ON_JUMP)
        row["quality_flag"] = "suspicious"
        hits.append({
            "source": row.get("source"), "gpu_model": row.get("gpu_model"),
            "price_type": row.get("price_type"),
            "market_segment": row.get("market_segment"),
            "value": round(float(value), 6),
            "baseline_median": round(baseline, 6),
            "ratio": round(ratio, 4),
            "reason": "unit_error" if unit_hint else "jump",
            "detail": unit_hint or (
                f"相对近 {lookback} 天中位数偏离 {deviation * 100:.0f}%，"
                f"超过 {max_jump * 100:.0f}% 阈值"),
            "history_points": len(past),
        })
    return hits


def trailing_history(start_date: str, end_date: str) -> Dict[Tuple, Dict[str, float]]:
    """按 (source, gpu, price_type, segment, region) 归集历史价，按日期存。

    保留日期而不是压成一个列表，是因为基准必须按「被检查那一行的日期」
    往前取窗口；丢掉日期就只能拿全窗口中位数比，那会把真实趋势判成异常。

    只取 quality_flag='ok' 的行——拿一个可疑值当基准，会把后面正常的值
    反过来判成可疑。
    """
    out: Dict[Tuple, Dict[str, float]] = {}
    for row in db_adapter.read_prices(start_date, end_date):
        if row.get("quality_flag") != "ok":
            continue
        value = row.get("price_usd_gpu_hour")
        if value is None:
            continue
        out.setdefault(_key(row), {})[_d(row["obs_date"])] = float(value)
    return out
