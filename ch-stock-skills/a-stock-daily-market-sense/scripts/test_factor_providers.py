# -*- coding: utf-8 -*-
"""跨技能因子 provider 的 as-of 防未来函数单测（Phase 4）。

用 monkeypatch 注入合成 forecast/theme 数据，不依赖实时 DB，断言：
  1. first_ann_date > signal_date 的预告绝不被 join（否则就是用了未来信息）；
  2. 多版本预告只取 ≤T 的最新一版（更晚的修订不泄漏）；
  3. theme 台账覆盖窗口外的信号一律 null（不臆造 0）；窗口内命中=1、未命中=0。

可直接 `python3 scripts/test_factor_providers.py` 跑，也兼容 pytest。
本文件是仓库内测试，不随 skill 发布（发布边界排除 test_*.py）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import factor_providers as fp

CALENDAR = ["20251201", "20260102", "20260315", "20260420",
            "20260424", "20260601", "20260610", "20260703", "20260801"]


def _fake_forecast():
    # AAA 两版预告（0102 与 0601）；BBB 只有一版且在 0601（晚于其信号）。
    return {
        "AAA.SZ": [("20260102", 50.0), ("20260601", 80.0)],
        "BBB.SZ": [("20260601", 80.0)],
    }


def _fake_theme():
    # 覆盖窗口 [0424, 0703]；仅 0610 有"强势在场"主线，成员含"名A"。
    return {"20260610": {"名A"}}, "20260424", "20260703"


def _run(sig_rows):
    fp._load_forecast = _fake_forecast          # type: ignore[assignment]
    fp._load_theme = _fake_theme                # type: ignore[assignment]
    df = pd.DataFrame(sig_rows)
    out, cov = fp.compute_providers(df, CALENDAR)
    return out, cov


def test_forecast_no_future_leak():
    """BBB 的唯一预告 first_ann=0601 晚于信号 0315 → 不得 join（days/yoy 均 null）。"""
    out, _ = _run([{"ts_code": "BBB.SZ", "name": "乙公司", "signal_date": "20260315"}])
    row = out.iloc[0]
    assert pd.isna(row["days_since_forecast_ann"]), "未来预告被泄漏进 days_since"
    assert pd.isna(row["forecast_cum_yoy_med"]), "未来预告被泄漏进 cum_yoy"
    print("[pass] forecast 未来版本不泄漏")


def test_forecast_picks_latest_not_future():
    """AAA 在 0315 应取 ≤0315 的最新版 0102(yoy 50)，而非更晚的 0601(yoy 80)。"""
    out, _ = _run([{"ts_code": "AAA.SZ", "name": "甲公司", "signal_date": "20260315"}])
    row = out.iloc[0]
    assert row["forecast_cum_yoy_med"] == 50.0, f"应取 0102 版 yoy=50，实际 {row['forecast_cum_yoy_med']}"
    exp_days = CALENDAR.index("20260315") - CALENDAR.index("20260102")
    assert row["days_since_forecast_ann"] == exp_days, "days_since 计算错"
    print("[pass] forecast 只取 ≤T 的最新一版")


def test_theme_outside_window_is_null():
    """0420 在覆盖窗口 [0424,0703] 之前 → in_active_theme 必须 null（不臆造 0）。"""
    out, _ = _run([{"ts_code": "AAA.SZ", "name": "名A", "signal_date": "20260420"}])
    assert pd.isna(out.iloc[0]["in_active_theme"]), "窗口外应为 null"
    print("[pass] theme 覆盖窗口外为 null")


def test_theme_in_window_hit_and_miss():
    """窗口内：0610 成员'名A'→1；同日'名B'非成员→0。"""
    out, _ = _run([
        {"ts_code": "AAA.SZ", "name": "名A", "signal_date": "20260610"},
        {"ts_code": "CCC.SZ", "name": "名B", "signal_date": "20260610"},
    ])
    assert out.iloc[0]["in_active_theme"] == 1, "在场成员应为 1"
    assert out.iloc[1]["in_active_theme"] == 0, "非成员应为 0"
    print("[pass] theme 窗口内命中=1、未命中=0")


def test_coverage_gate_reported():
    """覆盖率与 enters_overlay 闸门要如实报出。"""
    out, cov = _run([
        {"ts_code": "AAA.SZ", "name": "名A", "signal_date": "20260610"},
        {"ts_code": "BBB.SZ", "name": "乙", "signal_date": "20260315"},
    ])
    assert "in_active_theme" in cov and "null_frac" in cov["in_active_theme"]
    assert isinstance(cov["in_active_theme"]["enters_overlay"], bool)
    print("[pass] factor_coverage 闸门如实报出")


def main() -> int:
    tests = [test_forecast_no_future_leak, test_forecast_picks_latest_not_future,
             test_theme_outside_window_is_null, test_theme_in_window_hit_and_miss,
             test_coverage_gate_reported]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 条 as-of 防泄漏单测通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
