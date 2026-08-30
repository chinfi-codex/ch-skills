#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""守卫的回归测试。

盯的是本项目最容易出、又最难在成品里看出来的五类错：
把股票当成债、把发行人当成关联键、把一次性披露画成序列、
把噪音当成事件、把不可比的两段序列接起来。
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import attribution                              # noqa: E402
import ladder as ladder_mod                     # noqa: E402
import render_report_html as renderer           # noqa: E402
from collectors.base import fingerprint         # noqa: E402
from collectors.spdr import _match_issuer, _parse_maturity   # noqa: E402


# --- 1. 把股票当成债 -------------------------------------------------------
def test_mandatory_convert_has_no_maturity():
    """CWB 里混着强制转股优先股，maturity 是 '-'，par/MV 比算出来四位数
    （实测 Alphabet 4922、Oracle 4567）。必须剔掉。"""
    assert _parse_maturity("-") is None
    assert _parse_maturity("") is None
    assert _parse_maturity("11/15/2032") is not None


# --- 2. 发行人匹配与排除 ---------------------------------------------------
def test_toronto_dominion_is_not_dominion_energy():
    """Dominion Energy 的匹配词会误伤 Toronto Dominion Bank——
    实测 SPBO 里有 20 多只多伦多道明的债。"""
    issuers = {
        "D": {"match": ["DOMINION ENERGY", "VIRGINIA ELECTRIC"],
              "exclude": ["TORONTO DOMINION"]},
    }
    assert _match_issuer("DOMINION ENERGY INC JR SUBORDINA 02/56", issuers) == "D"
    assert _match_issuer("TORONTO DOMINION BANK SR UNSECURED 04/33", issuers) is None


def test_utility_opco_rolls_up_to_parent():
    """opco 一按揭债比母公司无担保债更贴近数据中心负荷的担保结构，
    但聚合时要上卷到母公司。"""
    issuers = {"AEP": {"match": ["APPALACHIAN POWER", "AEP TEXAS"]}}
    assert _match_issuer("APPALACHIAN POWER CO SR UNSECURED 04/38", issuers) == "AEP"
    assert _match_issuer("AEP TEXAS INC SR UNSECURED 06/33", issuers) == "AEP"


# --- 3. 一次性披露不许画成序列 ---------------------------------------------
def test_disclosure_once_refuses_timeseries():
    """Beignet 只在 Blue Owl 2025Q3 10-Q 的期后事项附注里出现过一次，
    之后并入 FVO 合计行。把一个点画成一条线就是造假。"""
    assert renderer.refuses_timeseries("disclosure_once") is True
    assert renderer.refuses_timeseries("ok") is False
    assert renderer.refuses_timeseries(None) is False


def test_compact_html_prioritizes_watchlist_and_groups_hyperscalers():
    """精简页先看异动；超大厂合成一个类别指数并逐行展开成员。"""
    ev = {
        "asof": "2026-08-27",
        "dials": [
            {"group": "梯级", "name": "档1", "rung": 1, "value": 38.3,
             "anchor": 39, "d1": -2.2, "d7": None, "d30": None},
            {"group": "梯级", "name": "档2", "rung": 2, "value": 68.3,
             "anchor": 68, "d1": -1.8, "d7": None, "d30": None},
            {"group": "梯级", "name": "档4", "rung": 4, "value": 92.9,
             "anchor": 97, "d1": -3.7, "d7": None, "d30": None},
            {"group": "梯级", "name": "档6", "rung": 6, "value": 198.4,
             "anchor": 205, "d1": -5.3, "d7": None, "d30": None},
        ],
        "ladder": [
            {"rung": 1, "members": ["MSFT"], "readings_5_10y": {"MSFT": 38.3}},
            {"rung": 2, "members": ["AMZN", "GOOGL"],
             "readings_5_10y": {"AMZN": 67.9, "GOOGL": 68.7}},
            {"rung": 4, "members": ["META"], "readings_5_10y": {"META": 92.9}},
            {"rung": 6, "members": ["ORCL"], "readings_5_10y": {"ORCL": 198.4}},
        ],
        "issuers": {
            "MSFT": {"buckets": {"5-10y": {"mean_bp": 38.3, "n": 1}}},
            "AMZN": {"buckets": {"5-10y": {"mean_bp": 67.9, "n": 8}}},
            "GOOGL": {"buckets": {"5-10y": {"mean_bp": 68.7, "n": 6}}},
            "META": {"buckets": {"5-10y": {"mean_bp": 92.9, "n": 7}}},
            "ORCL": {"buckets": {"5-10y": {"mean_bp": 198.4, "n": 12}}},
        },
    }
    page = renderer.build_compact_html(ev, {})
    assert page.index("今天值得看的") < page.index("核心刻度")
    assert "超大厂指数" in page
    assert "档1指数" not in page and "档6指数" not in page
    assert page.count('class="category-member"') == 5
    assert "+0.3" in page
    assert "AMZN" in page and "8 只样本" in page
    assert "GOOGL" in page and "6 只样本" in page


def test_compact_html_omits_daily_status_and_caveat_block():
    """用户指定删除的日频底部状态/口径段不应再进入精简 HTML。"""
    page = renderer.build_compact_html({"asof": "2026-08-27"}, {})
    for removed in ("持仓 As of", "国债曲线", "只有利差", "降级源：",
                    "G-spread 不是 OAS", "需要时用"):
        assert removed not in page


def test_report_date_is_separate_from_evidence_asof_in_both_views():
    """精简页与完整版都用执行日做报告日，观测日只作为数据截止日。"""
    ev = {"asof": "2026-08-27", "window_days": 90,
          "source_health": [], "models": {}}
    report_date = renderer.resolve_report_date(
        {"date": "2026-08-30", "data_asof": "2026-08-27"})

    compact = renderer.build_compact_html(ev, {}, report_date)
    full = renderer.build_html(ev, {}, report_date)

    assert report_date == "2026-08-30"
    for page in (compact, full):
        assert "数据中心信用监控 · 2026-08-30" in page
        assert "报告日 2026-08-30 · 数据截止 2026-08-27" in page


# --- 4. alpha 分解必须闭合 -------------------------------------------------
def _series(pairs):
    return [{"date": d, "value": v} for d, v in pairs]


def test_attribution_closes_exactly():
    """Δ总 = 市场beta + 档位beta + alpha，残差必须为 0。"""
    block = attribution.decompose(
        "ORCL",
        issuer_series=_series([("2026-08-25", 200.0), ("2026-08-26", 240.0)]),
        peer_series={"GOOGL": _series([("2026-08-25", 70.0), ("2026-08-26", 82.0)])},
        index_series=_series([("2026-08-25", 81.0), ("2026-08-26", 86.0)]),
        start="2026-08-25", end="2026-08-26")
    assert block["total_bp"] == 40.0
    assert block["beta_market_bp"] == 5.0          # 指数走宽 5bp
    assert block["beta_tier_bp"] == 7.0            # 同档多走 12−5=7bp
    assert block["alpha_bp"] == 28.0               # 剩下的才是它自己
    assert abs(block["closure_residual_bp"]) < 1e-9


def test_single_member_rung_cannot_explain_itself():
    """单主体档没有同侪，档位 beta 必须置 0 并标 no_peer——
    不能拿自己的移动去解释自己。"""
    block = attribution.decompose(
        "ORCL",
        issuer_series=_series([("2026-08-25", 200.0), ("2026-08-26", 240.0)]),
        peer_series={},
        index_series=_series([("2026-08-25", 81.0), ("2026-08-26", 86.0)]),
        start="2026-08-25", end="2026-08-26")
    assert block["beta_tier_bp"] == 0.0
    assert "no_peer" in block["quality"]


def test_insufficient_history_returns_none_not_zero():
    """历史不足时必须不出数，不能填 0——0 会被读成「没变化」。"""
    cum = attribution.cumulative(
        "META", issuer_series=_series([("2026-08-26", 96.6)]),
        peer_series={}, index_series=_series([("2026-08-26", 81.0)]),
        windows=[5, 20], min_days=2)
    assert cum["quality"] == "insufficient_history"
    assert cum["windows"]["5d"]["value_bp"] is None
    assert cum["windows"]["20d"]["value_bp"] is None


def test_single_day_alpha_is_not_notable_by_itself():
    """单日 alpha 一律不定性：ETF 估值有粘滞，单日残差里混着估值噪音。
    只有累积 alpha 才带 notable 标记。"""
    cum = {"issuer": "CRWV", "windows": {
        "5d": {"value_bp": 12.0, "total_bp": 12.0},
        "20d": {"value_bp": 80.0, "total_bp": 90.0},
    }}
    events = attribution.notable_events(cum, segment="hy",
                                        thresholds={"ig": 15, "hy": 50})
    ids = [e["rule_id"] for e in events]
    assert "alpha_cum_20d" in ids       # 80bp 超过 HY 的 50bp 量级
    assert "alpha_cum_5d" not in ids    # 12bp 不到


# --- 5. 不可比的两段不许接起来 ---------------------------------------------
def test_basket_change_changes_fingerprint():
    """改 ETF 篮子会改指纹，历史序列从此断成两段——
    指标层不会拿不同指纹的观测相减。"""
    assert fingerprint(["spbo", "spib"]) != fingerprint(["spbo", "spib", "splb"])
    assert fingerprint(["spbo", "spib"]) == fingerprint(["spbo", "spib"])


def test_reverse_falsifier_uses_index_not_an_issuer():
    """utility_ai_check 的另一端是 IG 指数本身，不是某个发行人。
    它显著转正说明 AI 故事进了受监管电力定价，框架要重写。"""
    curves = {"D": {"buckets": {"5-10y": {"mean_bp": 79.2}}},
              "AEP": {"buckets": {"5-10y": {"mean_bp": 84.1}}},
              "ETR": {"buckets": {"5-10y": {"mean_bp": 76.7}}}}
    gaps = ladder_mod.rung_gaps(
        curves,
        [{"id": "utility_ai_check", "a": "UTIL_MEDIAN", "b": "IG_INDEX",
          "tenor": "5-10y", "anchor_bp": -3, "means": "反向证伪器"}],
        utility_members=["D", "AEP", "ETR"], index_oas_bp={"ig": 81.0})
    assert gaps[0]["a_bp"] == 79.2          # 三家的中位数
    assert gaps[0]["b_bp"] == 81.0
    assert abs(gaps[0]["observed_bp"] - (-1.8)) < 1e-9


def test_missing_leg_is_regime_na_not_zero():
    """跨档对缺一端就是 regime_na，不是 0。"""
    gaps = ladder_mod.rung_gaps(
        {"META": {"buckets": {"5-10y": {"mean_bp": 96.6}}}},
        [{"id": "x", "a": "META", "b": "GOOGL", "tenor": "5-10y", "anchor_bp": 28}],
        utility_members=[], index_oas_bp={})
    assert gaps[0]["observed_bp"] is None
    assert gaps[0]["quality"] == "regime_na"


def _run() -> int:
    failures, total = [], 0
    for name, obj in sorted(globals().items()):
        if not (name.startswith("test_") and callable(obj)):
            continue
        total += 1
        try:
            obj()
        except Exception as exc:                      # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    for line in failures:
        print("FAIL", line)
    print(f"{total - len(failures)}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run())
