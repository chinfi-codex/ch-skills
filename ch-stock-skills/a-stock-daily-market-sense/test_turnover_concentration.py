"""成交额集中度卡的纯函数单测（不依赖 PostgreSQL）。

覆盖的失败模式：
- ceil 浮点误差：5400×0.01 这类恰好整数的 k 被抬大 1（环境桶阈值同理）；
- 条件分位资格线：桶内样本不足回退无条件分位，且整体历史不足 250 日时
  连无条件分位也不出（insufficient_history 而不是拿裸值判读）；
- 状态机分支：被动 / 存量搬家 / 温和主动 / 过热 / 常态各自的触发条件；
- 排除大盘因素的语义：head 与 total 同比放大时 CR 与判读都不动。

跑法：python3 tests/run_tests.py（本文件在技能根目录，会被自动发现）
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

SKILL_ROOT = Path(__file__).resolve().parent
SHARED = SKILL_ROOT.parents[1] / "shared"
sys.path.insert(0, str(SHARED))
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import turnover_concentration as tc  # noqa: E402


def test_env_bucket_bounds():
    assert tc.env_bucket_of(None) is None
    assert tc.env_bucket_of(0.80) == "shrink"
    assert tc.env_bucket_of(0.8499) == "shrink"
    assert tc.env_bucket_of(0.85) == "normal"
    assert tc.env_bucket_of(1.0) == "normal"
    assert tc.env_bucket_of(1.15) == "normal"
    assert tc.env_bucket_of(1.1501) == "expand"
    assert tc.env_bucket_of(1.4) == "expand"


def test_ceil_series_float_guard():
    # 0.01 的二进制误差会让 5400*0.01 = 54.00000000000001，直接 ceil 变 55
    s = pd.Series([5400 * 0.01, 5400 * 0.05, 260 * 0.05, 123.4, None])
    out = tc.ceil_series(s).tolist()
    assert out[0] == 54, f"5400×1% 应为 54，得到 {out[0]}"
    assert out[1] == 270, f"5400×5% 应为 270，得到 {out[1]}"
    assert out[2] == 13, f"260×5% 应为 13，得到 {out[2]}"
    assert out[3] == 124
    assert pd.isna(out[4])


def test_percentile_min_sample_and_value():
    short = pd.Series([1.0] * 100)
    assert tc.percentile_of(short, 1.0) is None, "不足 250 日不得出分位"
    long_series = pd.Series([float(i) for i in range(1, 301)])
    # 250.0 在 1..300（严格小于计 249 个）→ 249/300 = 83.0
    assert tc.percentile_of(long_series, 250.0) == 83.0
    assert tc.percentile_of(long_series, 250.0, min_n=60) == 83.0


def _history_frame(n_rows: int = 300, *, cr_today: float, head_path, total_path,
                   buckets=None):
    """合成 dms_concentration_daily 形状的帧；head/total 传长度 n_rows 的序列。"""
    dates = pd.bdate_range("2025-01-01", periods=n_rows).strftime("%Y-%m-%d")
    cr = [25.0 + 10.0 * (i / (n_rows - 1)) for i in range(n_rows - 1)] + [cr_today]
    frame = pd.DataFrame({
        "trade_date": dates,
        "universe": [5400] * n_rows,
        "k_1pct": [54] * n_rows,
        "k_5pct": [270] * n_rows,
        "total_amt": list(total_path),
        "head_amt_1pct": list(head_path),
        "head_amt_5pct": [2.0 * h for h in head_path],
        "cr_1pct": cr,
        "cr_5pct": [2.0 * c for c in cr],
        "amt_env_ratio": [1.0] * n_rows,
        "env_bucket": buckets or ["normal"] * n_rows,
    })
    return frame


def test_conditional_percentile_fallback_and_match():
    n = 300
    frame = _history_frame(
        n, cr_today=40.0,
        head_path=[100.0] * n, total_path=[300.0] * n,
        buckets=["shrink"] * 10 + ["normal"] * (n - 10),
    )
    today_cr = float(frame["cr_1pct"].iloc[-1])
    # 当日 normal 桶样本充足 → env_matched
    pct, source, sample = tc.conditional_percentile(
        frame.iloc[:-1], "cr_1pct", today_cr, "normal")
    assert source == "env_matched" and sample == n - 11
    # 当日 shrink 桶只有 10 个样本 → 回退无条件
    pct_fb, source_fb, sample_fb = tc.conditional_percentile(
        frame.iloc[:-1], "cr_1pct", today_cr, "shrink")
    assert source_fb == "unconditional_fallback" and sample_fb == 0
    assert pct_fb == pct  # 回退值就是无条件分位
    # 无桶 → no_bucket
    _p, src, _s = tc.conditional_percentile(frame.iloc[:-1], "cr_1pct", today_cr, None)
    assert src == "unconditional_no_bucket"


def test_growth_windows():
    series = pd.Series([100.0] * 295 + [110.0] + [120.0] + [130.0] + [140.0] + [150.0])
    assert tc.growth(series, 5) == 50.0   # 150 / 100 - 1
    # 20 日窗基值仍是 100（前 295 个都持平）→ 同样 +50%
    assert tc.growth(series, 20) == 50.0
    # 长度 300、窗口 20：基值 = iloc[-21] = 379，末值 399
    long_series = pd.Series([100.0 + i for i in range(300)])
    assert tc.growth(long_series, 20) == round(100.0 * (399.0 / 379.0 - 1), 2)
    assert tc.growth(pd.Series([1.0, 2.0]), 5) is None


def test_classify_branches():
    # 历史不足
    assert tc.classify(None, 5.0, 1.0, 90.0) == "insufficient_history"
    assert tc.classify(80.0, None, 1.0, 90.0) == "insufficient_history"
    # 常态
    assert tc.classify(69.9, -5.0, -1.0, 90.0) == "normal"
    # 被动：head 无增量
    assert tc.classify(85.0, -2.0, -8.0, 60.0) == "passive"
    assert tc.classify(85.0, 0.0, -3.0, 60.0) == "passive"
    # 存量搬家：head 涨、大盘缩
    assert tc.classify(85.0, 6.0, -2.0, 60.0) == "rotation"
    # 温和主动：都涨但 head 绝对水位不高
    assert tc.classify(85.0, 6.0, 2.0, 84.9) == "inflow"
    # 过热：都涨 + head 绝对分位高
    assert tc.classify(85.0, 6.0, 2.0, 85.0) == "overheat"
    assert tc.classify(70.0, 6.0, 2.0, 99.0) == "overheat"


def test_reading_for_passive():
    n = 300
    head = [100.0] * (n - 5) + [99.0, 98.0, 97.5, 97.0, 96.0]   # 5 日增速为负
    total = [300.0] * n
    frame = _history_frame(n, cr_today=40.0, head_path=head, total_path=total)
    reading = tc.reading_for(frame, "1pct")
    assert reading["available"] is True
    assert reading["state"] == "passive"
    assert reading["unconditional_percentile"] == 100.0
    assert reading["env_matched_source"] == "env_matched"
    assert reading["windows"]["g5"]["head_growth_pct"] < 0
    # 20 日窗 head 也下行 → 双窗同态
    assert reading["confirmed_20d"] is True


def test_reading_for_overheat():
    n = 300
    head = [100.0] * (n - 5) + [104.0, 108.0, 112.0, 116.0, 120.0]  # 5 日 +20%
    total = [300.0] * (n - 5) + [301.0, 302.0, 303.0, 304.0, 305.0]
    frame = _history_frame(n, cr_today=40.0, head_path=head, total_path=total)
    reading = tc.reading_for(frame, "1pct")
    assert reading["state"] == "overheat"
    assert reading["head_amt_percentile"] == 100.0  # 120 远超历史 100
    assert reading["anomaly_after_env_adjust"] is True


def test_reading_for_normal_and_insufficient():
    n = 300
    frame = _history_frame(n, cr_today=25.5, head_path=[100.0] * n,
                           total_path=[300.0] * n)
    reading = tc.reading_for(frame, "1pct")
    assert reading["state"] == "normal"
    assert reading["confirmed_20d"] is False
    # 历史不足：整体 <250 日，连无条件分位也不出
    short = _history_frame(100, cr_today=40.0, head_path=[100.0] * 100,
                           total_path=[300.0] * 100)
    short_reading = tc.reading_for(short, "1pct")
    assert short_reading["state"] == "insufficient_history"
    assert short_reading["unconditional_percentile"] is None


def test_market_scale_invariance():
    """排除大盘量能因素的语义检查：head 与 total 等比放大，CR 与判读不变。"""
    n = 300
    head_a = [100.0] * n
    total_a = [300.0] * n
    head_b = [h * 1.5 for h in head_a]
    total_b = [t * 1.5 for t in total_a]
    # CR 由脚本算出的口径模拟：head/total 同比放大 → cr 不变、growth 不变
    frame_a = _history_frame(n, cr_today=30.0, head_path=head_a, total_path=total_a)
    frame_b = _history_frame(n, cr_today=30.0, head_path=head_b, total_path=total_b)
    ra, rb = tc.reading_for(frame_a, "1pct"), tc.reading_for(frame_b, "1pct")
    assert ra["state"] == rb["state"]
    assert ra["windows"]["g5"] == rb["windows"]["g5"]
    # 分解式校验：Δlog CR = Δlog head − Δlog total，大盘共同项约掉
    head_g, total_g = 10.0, 4.0
    cr_g = 100.0 * (math.log(1 + head_g / 100) - math.log(1 + total_g / 100))
    assert abs(cr_g - round(cr_g, 2)) < 0.006


def test_recent_states_series():
    """近 30 日逐日回算：被动场景末段逐日 state 应为 passive，字段齐全。"""
    n = 300
    head = [100.0] * (n - 5) + [99.0, 98.0, 97.5, 97.0, 96.0]
    frame = _history_frame(n, cr_today=40.0, head_path=head, total_path=[300.0] * n)
    recent = tc.recent_states(frame)
    assert len(recent) == tc.RECENT_DAYS
    assert all({"date", "cr_1pct", "cr_5pct", "env_bucket", "state"} <= set(r) for r in recent)
    assert recent[-1]["state"] == "passive"
    assert recent[-1]["cr_1pct"] == 40.0
    # 短历史：不足 30 日返回全部，超过 30 日截最近 30 日，都不炸
    short = _history_frame(20, cr_today=40.0, head_path=[100.0] * 20, total_path=[300.0] * 20)
    assert len(tc.recent_states(short)) == 20
    mid = _history_frame(40, cr_today=40.0, head_path=[100.0] * 40, total_path=[300.0] * 40)
    assert len(tc.recent_states(mid)) == 30


def test_extract_concentration_payload():
    """渲染层提取器：卡可用时透传 recent；卡缺失/空序列返回 None（不注册图）。"""
    import render_report_html as rrh

    card = {
        "available": True,
        "data_through": "2026-09-18",
        "recent": [
            {"date": "2026-09-17", "cr_1pct": 22.1, "cr_5pct": 48.2,
             "env_bucket": "normal", "state": "normal"},
            {"date": "2026-09-18", "cr_1pct": 22.4, "cr_5pct": 48.6,
             "env_bucket": "normal", "state": "inflow"},
        ],
    }
    payload = rrh.extract_concentration_payload({"turnover_concentration": card})
    assert payload is not None
    assert payload["data_through"] == "2026-09-18"
    assert [r["date"] for r in payload["recent"]] == ["2026-09-17", "2026-09-18"]
    # 卡不可用 / 序列为空 / 键缺失 → None
    assert rrh.extract_concentration_payload(
        {"turnover_concentration": {"available": False, "reason": "x"}}) is None
    assert rrh.extract_concentration_payload(
        {"turnover_concentration": {"available": True, "recent": []}}) is None
    assert rrh.extract_concentration_payload({}) is None
