#!/usr/bin/env python3
"""Offline regression tests for the trend-axis state transitions.

2026-08 起阈值改为占比 / bp 口径，所以这些行都显式带上 total（全市场家数），
由 `row()` 顺手把 rise_share / limit_down_share 算好——测试直接调
`run_state_machine`，不经过 `derive`，两者的口径必须在这里对齐。
"""

from __future__ import annotations

import math
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

import trend_state_card as t  # noqa: E402


def row(offset: int, *, rise: int = 2000, fall: int = 2000, flat: int = 200,
        limit_up: int = 50, limit_down: int = 0, margin: float = 0.0,
        groups: list | None = None, shrink: bool = False,
        ma20_gap: float | None = None) -> t.DayRow:
    day = date(2026, 7, 1) + timedelta(days=offset)
    total = rise + fall + flat
    r = t.DayRow(
        d=day, rise=rise, fall=fall, limit_up=limit_up, limit_down=limit_down,
        amount_qianyuan=100.0, margin_yi=margin, total=total,
        margin_data_date=day - timedelta(days=1),
        groups_hit=list(groups or []), shrink=shrink, ma20_gap=ma20_gap,
    )
    r.rise_share = rise / total
    r.limit_down_share = limit_down / total
    return r


def panic_ld(total: int = 4200) -> int:
    """刚好越过恐慌线的跌停家数。"""
    return math.ceil(total * t.PANIC_LIMIT_DOWN_SHARE)


def calm_ld(total: int = 4200) -> int:
    """刚好落在冰点解除线以下的跌停家数。"""
    return math.floor(total * t.ICE_EXIT_LIMIT_DOWN_SHARE) - 1


ALL_THREE = ["破位", "融资", "缩量"]


def test_panic_ice_and_clear_path() -> None:
    days = [
        row(0),
        row(1, limit_down=panic_ld()),
        row(2, limit_down=panic_ld()),
        row(3, rise=3000, fall=1000, limit_down=calm_ld(4200)),
    ]
    t.run_state_machine(days)
    assert [d.state for d in days] == ["标准", "退潮", "冰点", "退潮"]
    assert days[-1].phase == "修复中"


def test_panic_line_scales_with_universe_size() -> None:
    """同样 190 家跌停，在 4200 家的市场是恐慌，在 6000 家的市场不是。

    这条正是本轮口径切换要解决的问题：写死 200 家时，同一个绝对数在上市家数
    从 4050 涨到 5493 的五年里代表的含义一路漂移。
    """
    small = row(0, rise=2000, fall=2000, flat=200, limit_down=190)
    big = row(0, rise=2900, fall=2900, flat=200, limit_down=190)
    assert t.is_panic(small)
    assert not t.is_panic(big)


def test_legacy_count_fallback_when_universe_unknown() -> None:
    """market_history 缺 flat 列时 total=0，退回旧的绝对家数口径而不是恒不触发。"""
    legacy = t.DayRow(d=date(2026, 7, 1), rise=1000, fall=4000, limit_up=5,
                      limit_down=t.LEGACY_PANIC_LIMIT_DOWN, amount_qianyuan=100.0,
                      margin_yi=0.0)
    assert legacy.total == 0
    assert t.is_panic(legacy)


def test_margin_cumulative_trigger_enters_caution() -> None:
    day = row(0, margin=-50.0)
    day.margin_neg_cum_yi = t.MARGIN_NEG_CUM_YI
    t.run_state_machine([day])
    assert day.state == "谨慎"


def test_margin_cumulative_uses_bp_when_balance_known() -> None:
    """有融资余额时按 bp 判：同样 -300 亿，在 2.6 万亿的余额上不该再算连负累计。"""
    day = row(0, margin=-300.0)
    day.margin_bp = -300.0 / 26000 * 1e4      # ≈ -115bp，未越 -180bp 线
    day.margin_neg_cum_bp = day.margin_bp
    day.margin_neg_cum_yi = -300.0
    t.run_state_machine([day])
    assert day.state == "标准", day.state

    deep = row(0, margin=-300.0)
    deep.margin_bp = -300.0 / 14000 * 1e4     # ≈ -214bp，越线
    deep.margin_neg_cum_bp = deep.margin_bp
    deep.margin_neg_cum_yi = -300.0
    t.run_state_machine([deep])
    assert deep.state == "谨慎", deep.state


def test_panic_next_day_ignores_margin_leg_but_keeps_volume_leg() -> None:
    panic = row(0, limit_down=panic_ld())
    # 上涨占比越过大涨线；融资腿用亿元兜底口径（margin_bp 未知）
    rebound = row(1, rise=3600, fall=400, flat=200,
                  margin=t.QC_MARGIN_BP / 1e4 * 15000.0 - 1)
    rebound.amt_ma20_prev = 100.0
    rebound.amount_qianyuan = 100.0
    t.derive([panic, rebound])
    assert rebound.big_up and not rebound.rebound_stamp

    rebound.amount_qianyuan = 80.0
    t.derive([panic, rebound])
    assert rebound.rebound_stamp


def test_bigup_leg_is_share_based() -> None:
    """3500 家上涨在 4200 家的市场是大涨日，在 5500 家的市场不是。"""
    small = row(0, rise=3500, fall=500, flat=200)
    big = row(0, rise=3500, fall=1800, flat=200)
    t.derive([small])
    t.derive([big])
    assert small.big_up
    assert not big.big_up


def test_stale_margin_defers_deep_escalation_on_breadth_reversal() -> None:
    """三组全中但第三组「融资」是 T-1 读数，当日广度反转时推迟一日再升深度退潮。"""
    day = row(0, rise=5193, fall=286, limit_up=125, limit_down=7,
              margin=-222.2, groups=ALL_THREE, shrink=True)
    t.run_state_machine([day])
    assert day.state == "退潮", day.state
    assert day.deep_escalation_deferred
    assert "推迟一日" in day.state_note


def test_deep_escalation_proceeds_when_breadth_not_reversed() -> None:
    """广度未反转时守卫不介入，三组全中照记深度退潮。"""
    day = row(0, rise=1500, fall=3800, limit_up=20, limit_down=60,
              margin=-222.2, groups=ALL_THREE, shrink=True)
    t.run_state_machine([day])
    assert day.state == "深度退潮"
    assert not day.deep_escalation_deferred


def test_guard_never_downgrades_an_existing_deep_state() -> None:
    """守卫只拦升档：已在深度退潮的日子即使广度反转也不因此降回退潮。"""
    days = [
        row(0, rise=1500, fall=3800, limit_up=20, limit_down=60,
            margin=-222.2, groups=ALL_THREE, shrink=True),
        row(1, rise=4000, fall=1200, limit_up=100, limit_down=5,
            margin=-150.0, groups=ALL_THREE, shrink=True),
    ]
    t.run_state_machine(days)
    assert [d.state for d in days] == ["深度退潮", "深度退潮"]
    assert not days[1].deep_escalation_deferred


def test_phase_repair_counts_ma20_gap_narrowing_under_break() -> None:
    """破位期间旧口径的「收复双指数 20 日线」恒为假，改用缺口收窄后修复腿仍可计分。"""
    days = [
        row(0, rise=1000, fall=4000, limit_down=25, margin=-97.2,
            groups=["破位", "缩量"], shrink=True, ma20_gap=0.052),
        row(1, rise=5193, fall=286, limit_up=125, limit_down=7, margin=-222.2,
            groups=["破位", "缩量"], shrink=True, ma20_gap=0.038),
    ]
    t.run_state_machine(days)
    today = days[1]
    assert today.state == "退潮"
    assert today.phase == "修复中", (today.phase, today.phase_repair, today.phase_worsen)
    assert "20日线缺口收窄" in today.phase_repair
    assert "跌停收敛" in today.phase_repair
    # 滞后融资仍计一票，但不再能单独把方向定成恶化
    assert any(h.startswith("融资大卖") for h in today.phase_worsen)


def test_phase_worsen_counts_breadth_deterioration() -> None:
    """旧口径恶化侧没有广度腿，5000 家下跌也只记企稳；新口径必须记恶化中。"""
    days = [
        row(0, rise=3000, fall=2000, groups=["破位"], ma20_gap=0.030),
        row(1, rise=554, fall=4940, limit_up=43, limit_down=125, margin=2.2,
            groups=["破位", "缩量"], shrink=True, ma20_gap=0.048),
    ]
    t.run_state_machine(days)
    today = days[1]
    assert today.phase == "恶化中", (today.phase, today.phase_repair, today.phase_worsen)
    assert "广度恶化" in today.phase_worsen
    assert "20日线缺口扩大" in today.phase_worsen


def test_margin_data_date_labels_the_lagged_leg() -> None:
    """相位里的融资腿必须带出实际数据日，卡面才能标注它不是当日读数。"""
    day = row(0, rise=1000, fall=4000, margin=-250.0, groups=["破位", "缩量"], shrink=True)
    t.run_state_machine([day])
    assert f"融资大卖（{day.margin_data_date.strftime('%m-%d')}）" in day.phase_worsen


def run() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
