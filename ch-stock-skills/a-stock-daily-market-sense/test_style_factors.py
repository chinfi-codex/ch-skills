"""市场风格因子的纯函数单测（不依赖网络与 PostgreSQL）。

覆盖的失败模式：
- 比值涨跌被写成收益率相减（+10% / +5% 必须是 4.7619%，不是 5）；
- 历史分位偷看未来、或样本不足 252 仍出分位；
- 接口返回成功但数据不可用：末日滞后、缺交易日、重复日期、非正价格、静默截断；
- 换源拼接：Tushare 核验不过时必须整条换 Baostock，而不是补尾巴；
- 可选全收益组缺一条仍被当成完整核验；
- 契约：读数可用却没写「风格因子」表、没有 20/60 日反向却写「风格轮动」；
- 渲染：曲线文件日期对不上读数时必须失败，而不是配错图。

跑法：python3 tests/run_tests.py style
"""
from __future__ import annotations

import sys
import tempfile
import json
from pathlib import Path

import numpy as np
import pandas as pd

SKILL_ROOT = Path(__file__).resolve().parent
SHARED = SKILL_ROOT.parents[1] / "shared"
sys.path.insert(0, str(SHARED))
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import style_factors as sf  # noqa: E402
from dms_output_contract import (  # noqa: E402
    INDUSTRY_SWING_CONTRACT,
    ContractError,
    validate_dms_content,
)

ASOF = "20260924"
CAL = pd.bdate_range("2002-01-04", "2026-09-24")
CAL_STR = [d.strftime("%Y%m%d") for d in CAL]


def _series_frame(start: str, seed: int, drift: float = 0.0002, end: str = ASOF, drop=()) -> pd.DataFrame:
    days = [d for d in CAL if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    rng = np.random.default_rng(seed)
    closes = 1000 * np.exp(np.cumsum(rng.normal(drift, 0.012, len(days))))
    df = pd.DataFrame({"trade_date": [d.strftime("%Y%m%d") for d in days], "close": closes})
    if drop:
        df = df.loc[~df["trade_date"].isin(drop)]
    return df.reset_index(drop=True)


def _all_frames(overrides=None) -> dict:
    frames = {}
    for i, (key, spec) in enumerate({**sf.CORE_SERIES, **sf.TOTAL_RETURN_SERIES}.items()):
        frames[spec["ts_code"]] = _series_frame(spec["fetch_start"], seed=i)
    frames.update(overrides or {})
    return frames


def _loaders(tushare_frames, baostock_frames=None):
    calls = []

    def make(source, table):
        def load(code, start, end):
            calls.append((source, code))
            if code not in table:
                raise RuntimeError(f"{source} has no {code}")
            return table[code]
        return load

    loaders = {"tushare": make("tushare", tushare_frames)}
    if baostock_frames is not None:
        loaders["baostock"] = make("baostock", baostock_frames)
    return loaders, calls


# --------------------------------------------------------------------------- #
# 公式
# --------------------------------------------------------------------------- #
def test_ratio_change_is_not_return_difference():
    idx = pd.bdate_range("2026-01-01", periods=2)
    prices = pd.DataFrame({"a": [100.0, 110.0], "b": [100.0, 105.0]}, index=idx)
    logq = sf.log_contrast(prices, {"a": 1.0, "b": -1.0})
    rel = float(np.expm1(logq.iloc[-1] - logq.iloc[0]) * 100)
    assert abs(rel - 4.7619) < 1e-3, rel


def test_size_weights_are_geometric_means():
    idx = pd.bdate_range("2026-01-01", periods=2)
    cols = {k: [100.0, 100.0] for k in sf.WEIGHTS["size"]}
    cols["sz399376"] = [100.0, 121.0]   # 小盘成长 +21%，小盘价值不动
    logq = sf.log_contrast(pd.DataFrame(cols, index=idx), sf.WEIGHTS["size"])
    # √(1.21 × 1) / √(1 × 1) = 1.1
    assert abs(float(np.exp(logq.iloc[-1] - logq.iloc[0])) - 1.1) < 1e-9


def test_rotation_codes():
    assert sf.rotation_of([1, 1, 1])[0] == "aligned"
    assert sf.rotation_of([1, -1, -1])[0] == "reversal_20v60"
    assert sf.rotation_of([1, 1, -1])[0] == "shift_10v20"
    assert sf.rotation_of([0, 0, 0])[0] == "mixed"


def test_percentile_prior_has_no_lookahead_and_needs_252():
    idx = pd.bdate_range("2010-01-01", periods=400)
    s = pd.Series(np.arange(400, dtype=float), index=idx)
    ranks = sf.percentile_prior(s, idx[0])
    assert ranks.iloc[:252].isna().all()
    # 严格递增序列：每天都比所有过去值大 → 100 分位；要是偷看了未来就会小于 100
    assert (ranks.iloc[252:] == 100).all()
    # rank_start 之前的样本不进历史
    late = sf.percentile_prior(s, idx[100])
    assert late.iloc[:352].isna().all() and late.iloc[352] == 100


# --------------------------------------------------------------------------- #
# 逐序列核验
# --------------------------------------------------------------------------- #
def _validate(frame, spec=None, core=True):
    spec = spec or {"latest_start": "20030102"}
    return sf.validate_series(frame, CAL, pd.Timestamp(ASOF), spec, core)


def test_validate_rejects_stale_missing_duplicate_nonpositive_truncated():
    ok, qa = _validate(_series_frame("20021231", 1))
    assert ok is not None and qa["ok"] and qa["missing_trade_days"] == 0

    _, qa = _validate(_series_frame("20021231", 1, end="20260922"))
    assert "!= asof" in qa["error"]

    _, qa = _validate(_series_frame("20021231", 1, drop=("20150605",)))
    assert "missing trade days" in qa["error"]

    dup = _series_frame("20021231", 1)
    _, qa = _validate(pd.concat([dup, dup.tail(1)]))
    assert qa["error"] == "duplicate dates"

    bad = _series_frame("20021231", 1)
    bad.loc[10, "close"] = 0
    _, qa = _validate(bad)
    assert "invalid prices" in qa["error"]

    _, qa = _validate(_series_frame("20080102", 1))
    assert "silent truncation" in qa["error"]


def test_valid_from_skips_known_gap_and_trims():
    frame = _series_frame("20041231", 3, drop=("20110802",))
    series, qa = _validate(frame, {"valid_from": "20110803", "valid_note": "x"}, core=False)
    assert qa["ok"] and series.index.min() == pd.Timestamp("20110803")


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def test_build_block_full():
    loaders, _ = _loaders(_all_frames())
    block, display = sf.build_block(ASOF, CAL_STR, loaders)
    assert block["available"], block.get("reason")
    assert block["order"] == list(sf.FACTOR_KEYS)
    size = block["metrics"]["size"]
    assert size["rotation_code"] in {"aligned", "reversal_20v60", "shift_10v20", "mixed"}
    assert size["percentile"] is None or 0 <= size["percentile"] <= 100
    assert size["rank_start"] > "2010-01-04"
    assert {r["key"] for r in block["sensitivity_total_return"]} == {"size", "growth", "dividend"}
    assert block["total_return_status"] == {"size_growth": "complete", "dividend": "complete"}
    assert set(block["views"]) == {"all", "3y", "1y", "60d"}
    assert block["views"]["60d"]["size"]["return_intervals"] == 60
    assert block["views"]["all"]["size"]["unit"] == "%"
    assert block["views"]["all"]["volatility"]["unit"] == "个百分点"
    # evidence 里不能有 NaN（JSON 非法），曲线只在旁路载荷里
    json.dumps(block, allow_nan=False)
    assert "dates" not in block and display["as_of"] == block["as_of"]
    assert len(display["factors"]["size"]["level"]) == len(display["dates"])
    assert "tr_relative_60d" in display["factors"]["dividend"]


def test_optional_total_return_group_disabled_when_one_missing():
    frames = _all_frames()
    frames.pop("CN2375.CNI")
    loaders, _ = _loaders(frames)
    block, display = sf.build_block(ASOF, CAL_STR, loaders)
    assert block["available"]
    assert block["total_return_status"]["size_growth"].startswith("unavailable")
    assert [r["key"] for r in block["sensitivity_total_return"]] == ["dividend"]
    assert "tr_relative_60d" not in display["factors"]["size"]


def test_core_failure_makes_block_unavailable():
    frames = _all_frames({"000985.CSI": _series_frame("20041231", 9, end="20260922")})
    loaders, _ = _loaders(frames)
    block, display = sf.build_block(ASOF, CAL_STR, loaders)
    assert not block["available"] and display is None
    assert "中证全指" in block["reason"]


def test_stale_tushare_swaps_whole_series_to_baostock():
    stale = _series_frame("20021231", 0, end="20260922")
    frames = _all_frames({"399372.SZ": stale})
    fresh = _series_frame("20021231", 0)
    loaders, calls = _loaders(frames, {"sz.399372": fresh})
    block, _ = sf.build_block(ASOF, CAL_STR, loaders)
    assert block["available"], block.get("reason")
    entry = next(e for e in block["qa"] if e["symbol"] == "sz399372")
    assert entry["source"] == "baostock"
    assert [a["source"] for a in entry["attempts"]] == ["tushare", "baostock"]
    assert ("baostock", "sz.399372") in calls


def test_loader_exception_is_recorded_not_none():
    frames = _all_frames()
    frames.pop("000985.CSI")
    loaders, _ = _loaders(frames)
    block, _ = sf.build_block(ASOF, CAL_STR, loaders)
    entry = next(e for e in block["qa"] if e["symbol"] == "sh000985")
    assert not entry["ok"] and "tushare" in entry["error"]


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #
SWING = """# 报告

## 一句话盘面判断

==测试判断句。==

# 1. 环境与仓位总闸门

闸门正文。

# 2. 大盘温度与风格

温度正文。

{style}

# 3. 产业趋势主线总览

主线正文。

# 4. 主线内关注个股（多维筛选 · 并集）

并集正文。

# 5. 亏钱效应（爆量下跌）

亏钱正文。

# 6. 仓位管理备忘

备忘正文。
"""
TABLE = """| 风格因子 | 60日 |
|---|---:|
| 规模风格 | 大盘 |
"""


def _evidence(rotation="aligned"):
    metrics = {k: {"rotation_code": rotation} for k in ("size", "growth", "small_tail", "dividend")}
    return {
        "metadata": {},
        "forward_odds": {"available": False, "pulse": {"available": False}},
        "market_trend": {"market_style": {"available": True, "style_factors": {"available": True, "metrics": metrics}}},
    }


def test_contract_requires_style_table_when_readings_available():
    try:
        validate_dms_content(SWING.format(style="风格正文。"), _evidence(), INDUSTRY_SWING_CONTRACT)
    except ContractError as exc:
        assert "风格因子 table is missing" in str(exc)
    else:
        raise AssertionError("missing table must fail")
    audit = validate_dms_content(SWING.format(style=TABLE), _evidence(), INDUSTRY_SWING_CONTRACT)
    assert audit["detail"]["style_factors"]["table_present"]


def test_contract_rejects_unsupported_rotation_claim():
    text = SWING.format(style=TABLE + "\n==市场风格判断：风格发生轮动。==\n")
    try:
        validate_dms_content(text, _evidence("shift_10v20"), INDUSTRY_SWING_CONTRACT)
    except ContractError as exc:
        assert "风格发生轮动" in str(exc)
    else:
        raise AssertionError("rotation claim without reversal_20v60 must fail")
    audit = validate_dms_content(text, _evidence("reversal_20v60"), INDUSTRY_SWING_CONTRACT)
    assert audit["detail"]["style_factors"]["reversal_factors"]


def test_contract_skips_when_readings_unavailable():
    ev = _evidence()
    ev["market_trend"]["market_style"]["style_factors"] = {"available": False}
    audit = validate_dms_content(SWING.format(style="风格正文。"), ev, INDUSTRY_SWING_CONTRACT)
    assert audit["detail"]["style_factors"]["checked"] is False


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def test_renderer_target_section_and_freshness_gate():
    import render_report_html as rrh

    assert rrh.style_factor_target_section(SWING.format(style=TABLE), INDUSTRY_SWING_CONTRACT) == "temp_macro"
    assert rrh.style_factor_target_section(SWING.format(style="无表"), INDUSTRY_SWING_CONTRACT) is None

    loaders, _ = _loaders(_all_frames())
    block, display = sf.build_block(ASOF, CAL_STR, loaders)
    evidence = {"market_trend": {"market_style": {"style_factors": block}}}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "style_factors_20260924.json"
        path.write_text(json.dumps(display), encoding="utf-8")
        payload = rrh.extract_style_factor_payload(evidence, path, "pos_gate")
        assert payload["target_sec"] == "pos_gate" and payload["dates"]
        path.write_text(json.dumps({**display, "as_of": "2026-09-18"}), encoding="utf-8")
        try:
            rrh.extract_style_factor_payload(evidence, path, "pos_gate")
        except RuntimeError as exc:
            assert "freshness" in str(exc)
        else:
            raise AssertionError("mismatched series must fail")
