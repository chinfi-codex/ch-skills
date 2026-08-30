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
import metrics as metrics_mod                   # noqa: E402
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
# --- 6. 把市场 beta 当成 AI 自己的事 ---------------------------------------
_RUNGS_CFG = {1: {"name": "AAA 超大厂", "anchor_5_10y": 39, "bench": "ig"},
              7: {"name": "纯算力商", "anchor_5_10y": 775, "bench": "hy"}}
_ISSUERS_CFG = {"MSFT": {"rung": 1}, "CRWV": {"rung": 7}}


def _curves(msft: float, crwv: float):
    return {"MSFT": {"buckets": {"5-10y": {"mean_bp": msft, "n": 4}}},
            "CRWV": {"buckets": {"5-10y": {"mean_bp": crwv, "n": 4}}}}


def test_excess_basis_cancels_market_beta():
    """整个信用市场走宽 14bp，G-spread 口径的漂移会集体 +14——读起来像
    「AI 信用全面恶化」，其实标尺自己在动。超额口径必须把这 14bp 减掉。"""
    anchor_oas = {"ig": 81.0, "hy": 270.0}
    calm = ladder_mod.build_rungs(_curves(39.0, 775.0), _ISSUERS_CFG, _RUNGS_CFG,
                                  index_oas_bp=anchor_oas,
                                  anchor_index_oas_bp=anchor_oas)
    assert [r["drift_vs_anchor_bp"] for r in calm] == [0.0, 0.0]
    assert [r["drift_vs_anchor_excess_bp"] for r in calm] == [0.0, 0.0]

    # 只有市场在动：两档跟着各自的指数等量走宽，自己什么都没发生。
    wide = ladder_mod.build_rungs(_curves(53.0, 789.0), _ISSUERS_CFG, _RUNGS_CFG,
                                  index_oas_bp={"ig": 95.0, "hy": 284.0},
                                  anchor_index_oas_bp=anchor_oas)
    assert [r["drift_vs_anchor_bp"] for r in wide] == [14.0, 14.0]
    assert [r["drift_vs_anchor_excess_bp"] for r in wide] == [0.0, 0.0]


def test_excess_drift_equals_gspread_drift_minus_index_drift():
    """两个口径的差必须恰好等于指数 OAS 自己的漂移——这是这一列的定义式，
    也是「两列并排」这个读法唯一成立的前提。"""
    rows = ladder_mod.build_rungs(_curves(48.0, 800.0), _ISSUERS_CFG, _RUNGS_CFG,
                                  index_oas_bp={"ig": 79.0, "hy": 263.0},
                                  anchor_index_oas_bp={"ig": 81.0, "hy": 270.0})
    index_drift = {"ig": 79.0 - 81.0, "hy": 263.0 - 270.0}
    for r in rows:
        assert abs(r["drift_vs_anchor_bp"] - r["drift_vs_anchor_excess_bp"]
                   - index_drift[r["bench"]]) < 1e-9


def test_pure_neocloud_benches_against_high_yield():
    """纯算力商 766bp 拿 IG 指数去剔是错的段。分段必须与 attribution 的
    市场 beta 一致（那边 rung 7 → hy），否则同一个档的「剔市场」两处不同义。"""
    rows = ladder_mod.build_rungs(_curves(39.0, 766.0), _ISSUERS_CFG, _RUNGS_CFG,
                                  index_oas_bp={"ig": 79.0, "hy": 263.0},
                                  anchor_index_oas_bp={"ig": 81.0, "hy": 270.0})
    crwv = next(r for r in rows if r["rung"] == 7)
    assert crwv["bench"] == "hy"
    assert crwv["excess_5_10y"] == 766.0 - 263.0


def test_neocloud_attribution_uses_hy_market_beta():
    """CRWV 完全跟随 HY 走宽时 alpha 必须为零；若误用 IG，会凭空多出 14bp。"""
    series = metrics_mod._market_index_series(
        {"2026-08-25": {"ig": 81.0, "hy": 270.0},
         "2026-08-26": {"ig": 81.0, "hy": 284.0}},
        "hy", "2026-08-25")
    block = attribution.decompose(
        "CRWV",
        issuer_series=_series([("2026-08-25", 775.0), ("2026-08-26", 789.0)]),
        peer_series={}, index_series=series,
        start="2026-08-25", end="2026-08-26")
    assert block["beta_market_bp"] == 14.0
    assert block["alpha_bp"] == 0.0


def test_cross_segment_gap_strips_hy_minus_ig_basis():
    """CRWV(HY)−ORCL(IG) 不是同段的差；只有 HY 市场走宽时 raw gap 会动，
    剔除 HY−IG 基差后的发行人超额 gap 应保持不变。"""
    curves = {
        "CRWV": {"buckets": {"5-10y": {"mean_bp": 789.0}}},
        "ORCL": {"buckets": {"5-10y": {"mean_bp": 205.0}}},
    }
    gaps = ladder_mod.rung_gaps(
        curves,
        [{"id": "neocloud_premium", "a": "CRWV", "b": "ORCL",
          "anchor_bp": 570.0}],
        utility_members=[],
        index_oas_bp={"ig": 81.0, "hy": 284.0},
        anchor_index_oas_bp={"ig": 81.0, "hy": 270.0},
        issuers_cfg={"CRWV": {"rung": 7}, "ORCL": {"rung": 6}},
        rungs_cfg={6: {"bench": "ig"}, 7: {"bench": "hy"}})
    gap = gaps[0]
    assert gap["drift_bp"] == 14.0
    assert gap["bench_a"] == "hy" and gap["bench_b"] == "ig"
    assert gap["drift_excess_bp"] == 0.0
    assert gap["excess_quality"] == "ok"
    full_html = renderer.render_gaps({"gaps": gaps}, {})
    assert "剔市场锚点" in full_html and "hy−ig 基差" in full_html


def test_excess_basis_refuses_to_substitute_another_day():
    """锚点那天的指数 OAS 取不到就整列出 None。拿今天的顶替算出来的
    又是 G-spread 口径的漂移，白做一遍还看不出来。"""
    rows = ladder_mod.build_rungs(_curves(39.0, 775.0), _ISSUERS_CFG, _RUNGS_CFG,
                                  index_oas_bp={"ig": 79.0, "hy": 263.0},
                                  anchor_index_oas_bp={})
    assert all(r["drift_vs_anchor_excess_bp"] is None for r in rows)
    assert all(r["excess_quality"] == "no_anchor_index_oas" for r in rows)
    # G-spread 口径不受影响——超额缺数不该把原来能出的数也拖掉。
    assert all(r["drift_vs_anchor_bp"] is not None for r in rows)

    no_index = ladder_mod.build_rungs(_curves(39.0, 775.0), _ISSUERS_CFG, _RUNGS_CFG,
                                      index_oas_bp={},
                                      anchor_index_oas_bp={"ig": 81.0, "hy": 270.0})
    assert all(r["excess_quality"] == "no_index_oas" for r in no_index)


def test_member_charts_keep_an_issuer_with_no_curve_at_all():
    """配置里的成员即使完全没有曲线，也要留一张 no_reading 卡说明数据缺口。"""
    original_history = metrics_mod._history
    metrics_mod._history = lambda *args, **kwargs: {}
    try:
        curves = {"MSFT": {"buckets": {"5-10y": {"mean_bp": 39.0, "n": 1}}}}
        issuers = {
            "MSFT": {"name": "Microsoft", "rung": 1, "anchor_5_10y": 39},
            "MISSING": {"name": "Missing Co", "rung": 1, "anchor_5_10y": 39},
        }
        charts = metrics_mod._member_charts(
            curves, issuers,
            "2026-08-27", window_days=200, min_points=10)
    finally:
        metrics_mod._history = original_history
    missing = next(c for c in charts if c["issuer"] == "MISSING")
    assert len(charts) == 2
    assert missing["value"] is None and missing["quality"] == "no_reading"

    rungs = ladder_mod.build_rungs(
        curves, issuers, {1: {"name": "AAA 超大厂", "anchor_5_10y": 39}},
        index_oas_bp={"ig": 81.0}, anchor_index_oas_bp={"ig": 81.0})
    assert rungs[0]["members"] == ["MISSING", "MSFT"]
    assert rungs[0]["readings_5_10y"]["MISSING"] is None

    page = renderer.render_dials({
        "anchors": {}, "member_charts": charts, "issuers": curves,
        "ladder": rungs,
        "dials": [{"group": "梯级", "name": "档1", "rung": 1,
                   "value": 39.0, "anchor": 39.0, "vs_anchor": 0.0,
                   "excess": -42.0, "anchor_excess": -42.0,
                   "vs_anchor_excess": 0.0,
                   "d1": None, "d7": None, "d30": None}],
    }, {})
    assert "MISSING" in page and "桶里没有样本" in page


def test_disclosure_once_refuses_timeseries():
    """Beignet 只在 Blue Owl 2025Q3 10-Q 的期后事项附注里出现过一次，
    之后并入 FVO 合计行。把一个点画成一条线就是造假。"""
    assert renderer.refuses_timeseries("disclosure_once") is True
    assert renderer.refuses_timeseries("ok") is False
    assert renderer.refuses_timeseries(None) is False


_MEMBER_CHARTS = [
    {"issuer": "MSFT", "name": "Microsoft", "rung": 1, "value": 38.3, "anchor": 39,
     "drift_vs_anchor_bp": -0.7, "sample_n": 1, "window_days": 200,
     "series_days": 2, "min_points_for_line": 10,
     "series": [["2026-08-26", 40.5], ["2026-08-27", 38.3]],
     "quality": "insufficient_series"},
    {"issuer": "AMZN", "name": "Amazon.com", "rung": 2, "value": 67.9, "anchor": 68,
     "drift_vs_anchor_bp": -0.1, "sample_n": 8, "window_days": 200,
     "series_days": 40, "min_points_for_line": 10,
     "series": [[f"2026-0{6 + i // 30}-{i % 30 + 1:02d}", 68.0 + i * 0.1]
                for i in range(40)],
     "quality": "ok"},
    {"issuer": "GOOGL", "name": "Alphabet", "rung": 2, "value": 68.7, "anchor": 69,
     "drift_vs_anchor_bp": -0.3, "sample_n": 6, "window_days": 200,
     "series_days": 2, "min_points_for_line": 10,
     "series": [["2026-08-26", 69.4], ["2026-08-27", 68.7]],
     "quality": "insufficient_series"},
    {"issuer": "META", "name": "Meta Platforms", "rung": 4, "value": 92.9, "anchor": 97,
     "drift_vs_anchor_bp": -4.1, "sample_n": 7, "window_days": 200,
     "series_days": 2, "min_points_for_line": 10,
     "series": [["2026-08-26", 96.6], ["2026-08-27", 92.9]],
     "quality": "insufficient_series"},
    {"issuer": "ORCL", "name": "Oracle", "rung": 6, "value": 198.4, "anchor": 205,
     "drift_vs_anchor_bp": -6.6, "sample_n": 12, "window_days": 200,
     "series_days": 0, "min_points_for_line": 10, "series": [],
     "quality": "no_reading"},
]


def test_member_spark_refuses_a_line_it_cannot_draw():
    """一个点画不出线；序列够长才有 path。这是画图函数自己的下限，
    真正的门槛在 quality 上，见下一个测试。"""
    assert renderer.member_spark([["2026-08-27", 38.3]], 39, "#000") == ""
    assert renderer.member_spark([], 39, "#000") == ""
    drawn = renderer.member_spark([["2026-08-26", 40.0], ["2026-08-27", 38.3]], 39, "#000")
    assert "<path" in drawn and "锚 39" in drawn


def test_member_card_obeys_quality_not_point_count():
    """**两个点也能连出一条线，这正是要拦的东西。** 够不够画由证据包的 quality
    说了算，渲染层不自己数点——绕过去的话 min_points_for_line 形同虚设。"""
    short = next(c for c in _MEMBER_CHARTS if c["issuer"] == "MSFT")
    card = renderer._member_card(short, "#000")
    assert "<path" not in card
    assert "序列 2 天" in card and "还差 8 天" in card

    ok = next(c for c in _MEMBER_CHARTS if c["issuer"] == "AMZN")
    assert "<path" in renderer._member_card(ok, "#000")

    empty = next(c for c in _MEMBER_CHARTS if c["issuer"] == "ORCL")
    assert "没有样本" in renderer._member_card(empty, "#000")


def test_compact_html_prioritizes_watchlist_and_groups_hyperscalers():
    """精简页先看异动；超大厂合成一个类别指数，成分卡装在同一个盒子里。"""
    ev = {
        "asof": "2026-08-27",
        "member_charts": _MEMBER_CHARTS,
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
    assert page.count('class="mcard"') == 5
    assert "+0.3" in page
    assert "AMZN" in page and "8 只样本" in page
    assert "GOOGL" in page and "6 只样本" in page

    # 类别指数和它的成分必须在同一个盒子里。分开放过一版：指数在表里、卡片在页尾，
    # 读的时候得来回滚，「是谁把这个指数拉动的」反而变难了。
    block = page.split('class="cat"')[1]
    for issuer in ("MSFT", "AMZN", "GOOGL", "META", "ORCL"):
        assert issuer in block
    # 盒子里先有指数的读数，再有成分卡——顺序反了就不是「指数带着成分」了。
    assert block.index("成员加权档位中位数") < block.index('class="mcard"')


def test_compact_html_carries_both_anchor_columns():
    """核心刻度表要并排给两个口径的锚点漂移，并把那段市场 beta 写在表边上——
    只给一列的话，读者没法判断这次移动是不是标尺自己在动。"""
    ev = {
        "asof": "2026-08-27",
        "anchors": {"anchor_asof": "2026-08-25",
                    "index_oas_bp": {"ig": 79.0, "hy": 263.0},
                    "anchor_index_oas_bp": {"ig": 81.0, "hy": 270.0}},
        "dials": [
            {"group": "梯级", "name": "档7", "rung": 7, "value": 765.7,
             "anchor": 775, "excess": 502.7, "anchor_excess": 505.0,
             "bench": "hy", "d1": -10.3, "d7": None, "d30": None},
        ],
        "ladder": [{"rung": 7, "members": ["CRWV"],
                    "readings_5_10y": {"CRWV": 765.7}}],
        "issuers": {"CRWV": {"buckets": {"5-10y": {"mean_bp": 765.7, "n": 2}}}},
    }
    page = renderer.build_compact_html(ev, {})
    assert "剔市场" in page
    # G-spread 口径 -9.3 里有 7bp 是整个 HY 市场，自己只走了 -2.3。
    assert "-9.3" in page and "-2.3" in page
    assert "2026-08-25" in page


def test_compact_html_shows_excess_for_cross_segment_gap():
    """跨段 gap 的剔市场列必须显示计算结果，不能再统一写成自然对消的破折号。"""
    html = renderer.render_dials({
        "anchors": {},
        "dials": [{
            "group": "跨档距离", "name": "纯算力商溢价",
            "value": 584.0, "anchor": 570.0, "vs_anchor": 14.0,
            "excess": 381.0, "anchor_excess": 381.0,
            "vs_anchor_excess": 0.0, "excess_quality": "ok",
            "d1": None, "d7": None, "d30": None,
        }],
    }, {})
    assert "纯算力商溢价" in html
    assert '<td class="dial-d pct-flat">+0.0</td>' in html


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
