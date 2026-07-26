#!/usr/bin/env python3
"""Offline regression tests for the trend-axis state transitions."""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

import trend_state_card as t  # noqa: E402


def row(offset: int, *, rise: int = 2000, fall: int = 2000,
        limit_down: int = 0, margin: float = 0.0) -> t.DayRow:
    return t.DayRow(
        d=date(2026, 7, 1) + timedelta(days=offset),
        rise=rise, fall=fall, limit_up=50, limit_down=limit_down,
        amount_qianyuan=100.0, margin_yi=margin,
    )


def test_panic_ice_and_clear_path() -> None:
    days = [
        row(0),
        row(1, limit_down=t.PANIC_LIMIT_DOWN),
        row(2, limit_down=t.PANIC_LIMIT_DOWN),
        row(3, rise=3000, fall=1000, limit_down=t.ICE_EXIT_LIMIT_DOWN - 1),
    ]
    t.run_state_machine(days)
    assert [d.state for d in days] == ["标准", "退潮", "冰点", "退潮"]
    assert days[-1].phase == "修复中"


def test_margin_cumulative_trigger_enters_caution() -> None:
    day = row(0, margin=-50.0)
    day.margin_neg_cum_yi = t.MARGIN_NEG_CUM_YI
    t.run_state_machine([day])
    assert day.state == "谨慎"


def test_panic_next_day_ignores_margin_leg_but_keeps_volume_leg() -> None:
    panic = row(0, limit_down=t.PANIC_LIMIT_DOWN)
    rebound = row(1, rise=t.BIGUP_RISE + 1, margin=t.QC_MARGIN_YI - 1)
    rebound.amt_ma20_prev = 100.0
    rebound.amount_qianyuan = 100.0
    t.derive([panic, rebound])
    assert rebound.big_up and not rebound.rebound_stamp

    rebound.amount_qianyuan = 80.0
    t.derive([panic, rebound])
    assert rebound.rebound_stamp


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
