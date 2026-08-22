# -*- coding: utf-8 -*-
"""前瞻轴的纯函数回归：不连库，用构造帧覆盖脉冲门槛、去重、发布门槛与措辞纪律。"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import forward_odds as fo  # noqa: E402


def _frame(n: int = 900, seed: int = 7) -> pd.DataFrame:
    """造一段行为温和的市场帧，再由各用例往里打洞。"""
    rng = np.random.default_rng(seed)
    d0 = date(2021, 1, 4)
    dates = [d0 + timedelta(days=i) for i in range(n)]
    total = 5000
    rise = rng.integers(1800, 3200, n)
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "rise": rise,
        "fall": total - rise - 100,
        "flat": np.full(n, 100),
        "limit_up": rng.integers(30, 90, n),
        "limit_down": rng.integers(3, 20, n),
        "amount": rng.normal(2.0e9, 4e7, n),
        "bench": 6000 + np.cumsum(rng.normal(0.0, 8.0, n)),
        "ref": 3500 + np.cumsum(rng.normal(0.0, 4.0, n)),
        "rzye": 2.0e12 + np.cumsum(rng.normal(0.0, 1e9, n)),
    })


class PulseGateTest(unittest.TestCase):
    def test_gate_requires_all_four_legs(self):
        raw = _frame()
        # 只把「上涨占比 + 跌停占比」打到极值，另两条腿不给：门槛必须不开
        for i in (700, 701):
            raw.loc[i, "rise"] = 300
            raw.loc[i, "fall"] = 4600
            raw.loc[i, "limit_down"] = 200
            raw.loc[i, "limit_up"] = 3000       # 涨跌停比远高于低位分位 → s3 不中
            raw.loc[i, "amount"] = 1.0e9        # 缩量 → s4 不中
        df = fo.add_features(raw)
        self.assertTrue(bool(df.at[700, "s1"]))
        self.assertTrue(bool(df.at[700, "s2"]))
        self.assertFalse(bool(df.at[700, "s3"]))
        self.assertFalse(bool(df.at[700, "s4"]))
        self.assertFalse(bool(df.at[700, "pulse_gate"]),
                         "四腿缺两条时门槛不得开——pulse 是合取，不是打分")

    def test_gate_opens_when_all_four_hit(self):
        raw = _frame()
        i = 700
        raw.loc[i, "rise"] = 300
        raw.loc[i, "fall"] = 4600
        raw.loc[i, "limit_down"] = 200
        raw.loc[i, "limit_up"] = 2
        raw.loc[i, "amount"] = 3.0e9
        df = fo.add_features(raw)
        for leg in ("s1", "s2", "s3", "s4"):
            self.assertTrue(bool(df.at[i, leg]), f"{leg} 应命中")
        self.assertTrue(bool(df.at[i, "pulse_gate"]))
        self.assertEqual(int(df.at[i, "pulse_legs"]), 4)

    def test_pulse_not_ready_before_rolling_window(self):
        df = fo.add_features(_frame())
        self.assertFalse(bool(df.at[10, "pulse_ready"]),
                         "滚动分位没攒满时不得给出脉冲读数")

    def test_rolling_percentile_excludes_today(self):
        """分位必须只用信号日之前的历史——含当日就是前视。"""
        s = pd.Series(list(range(fo.ROLL_MIN + 1)), dtype=float)
        pct = fo._rolling_pct_rank(s)
        # 末位是历史最大值，排除当日后分位应为 1.0（严格大于此前所有值）
        self.assertEqual(pct.iloc[-1], 1.0)

    def test_limit_down_zero_does_not_poison_window(self):
        raw = _frame()
        raw.loc[100:400, "limit_down"] = 0
        df = fo.add_features(raw)
        self.assertTrue(df["lu_ld_ratio"].notna().all(), "跌停为 0 不得产生 NaN 比值")
        self.assertTrue(bool(df.at[700, "pulse_ready"]))


class DedupTest(unittest.TestCase):
    def test_cluster_collapses_to_first_day(self):
        self.assertEqual(fo.dedup_events([10, 11, 12, 30, 31]), [10, 30])

    def test_gap_boundary_is_inclusive(self):
        self.assertEqual(fo.dedup_events([0, fo.DEDUP_GAP]), [0, fo.DEDUP_GAP])
        self.assertEqual(fo.dedup_events([0, fo.DEDUP_GAP - 1]), [0])


class PublishGateTest(unittest.TestCase):
    @staticmethod
    def _entry(events: int, p_mean: float, means: tuple) -> dict:
        return {
            "sample": {"events": events},
            "horizons": [
                {"horizon_days": n, "permutation": {"p_mean": p_mean}}
                for n in fo.HORIZONS
            ],
            "subsample": [
                {"label": lbl, "events": 5,
                 "horizons": [{"horizon_days": n, "n": 5, "mean_pct": m}
                              for n, m in zip(fo.HORIZONS, group)]}
                for lbl, group in zip(("~2023", "2024~"), means)
            ],
        }

    def test_all_three_gates_pass(self):
        ok, detail = fo._publish_gate(
            self._entry(14, 0.001, ((1.0, 2.0, 3.0, 2.5), (1.2, 1.9, 3.5, 2.3))))
        self.assertTrue(ok)
        self.assertTrue(detail["min_events"]["pass"])

    def test_thin_sample_blocks_publication(self):
        ok, detail = fo._publish_gate(
            self._entry(5, 0.001, ((1.0, 2.0, 3.0, 2.5), (1.2, 1.9, 3.5, 2.3))))
        self.assertFalse(ok)
        self.assertFalse(detail["min_events"]["pass"])

    def test_subsample_disagreement_blocks_publication(self):
        ok, detail = fo._publish_gate(
            self._entry(14, 0.001, ((-1.0, -2.0, -3.0, -2.5), (1.2, 1.9, 3.5, 2.3))))
        self.assertFalse(ok)
        self.assertEqual(detail["subsample_consistent"]["horizons"], [])

    def test_weak_permutation_blocks_publication(self):
        ok, detail = fo._publish_gate(
            self._entry(14, 0.42, ((1.0, 2.0, 3.0, 2.5), (1.2, 1.9, 3.5, 2.3))))
        self.assertFalse(ok)
        self.assertFalse(detail["permutation"]["pass"])

    def test_only_consistent_horizons_are_quotable(self):
        """+1 日在子样本里翻负时不得进可引用视窗清单。"""
        ok, detail = fo._publish_gate(
            self._entry(14, 0.001, ((-0.1, 2.0, 3.0, 2.5), (1.4, 1.9, 3.5, 2.3))))
        self.assertTrue(ok)
        self.assertNotIn(1, detail["subsample_consistent"]["horizons"])
        self.assertEqual(detail["subsample_consistent"]["horizons"], [2, 3, 5])


class TopSideTest(unittest.TestCase):
    def test_top_signals_are_never_publishable(self):
        raw = _frame()
        df = fo.add_features(raw)
        valid = df["pulse_ready"]
        rng = np.random.default_rng(1)
        specs = [s for s in fo._signal_masks(df) if s["side"] == "top"]
        self.assertTrue(specs, "顶部侧信号不得为空")
        for spec in specs:
            entry = fo.evaluate_signal(df, spec, valid, rng, full=False)
            if not entry.get("available"):
                continue
            self.assertFalse(entry["publishable"],
                             f"{spec['key']} 顶部侧一律不可发布")
            self.assertNotIn("horizons", entry, "顶部侧不得输出方向分布")
            self.assertIn("drawdown", entry, "顶部侧必须输出回撤风险分布")
            self.assertIn("direction_note", entry)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 契约层：前瞻轴的三条硬纪律必须被机器拦住，靠 review 抓不稳
# ---------------------------------------------------------------------------
sys.path.insert(0, str(HERE.parents[1] / "shared"))

import dms_output_contract as doc  # noqa: E402


def _sections(sentiment_body: str) -> dict:
    return {"sentiment_trend": doc.MarkdownSection(
        level=3, title="情绪趋势", stripped="情绪趋势", body=sentiment_body)}


def _evidence(gate: bool, publishable: bool, events: int = 14) -> dict:
    return {
        "forward_odds": {
            "available": True,
            "pulse": {"available": True, "gate": gate, "legs_hit": 4 if gate else 2},
            "signals": [{
                "key": "pulse_gate",
                "publishable": publishable,
                "sample": {"events": events},
            }],
        }
    }


class ForwardAxisContractTest(unittest.TestCase):
    def test_missing_axis_line_is_caught(self):
        problems: list = []
        doc._validate_forward_axis(_sections("今日缩量下跌。"), _evidence(False, False), problems)
        self.assertTrue(any("情绪脉冲 line is missing" in p for p in problems))

    def test_unsupported_trigger_claim_is_caught(self):
        problems: list = []
        body = "- **前瞻轴**：情绪脉冲 未全中 2/4\n\n==趋势判断：情绪脉冲触发。=="
        doc._validate_forward_axis(_sections(body), _evidence(False, False), problems)
        self.assertTrue(any("情绪脉冲触发" in p for p in problems),
                        "门槛没开却写了判断词，必须拦住")

    def test_probability_without_sample_size_is_caught(self):
        problems: list = []
        body = "- **前瞻轴**：情绪脉冲 四腿全中\n\n==趋势判断：情绪脉冲触发，+3 日 84.6% 收涨。=="
        doc._validate_forward_axis(_sections(body), _evidence(True, True, events=14), problems)
        self.assertTrue(any("sample size" in p for p in problems),
                        "概率不带样本量就是断言不是证据")

    def test_well_formed_reading_passes(self):
        problems: list = []
        body = ("- **前瞻轴**：情绪脉冲 四腿全中\n\n"
                "==趋势判断：情绪脉冲触发——历史上 14 次同类日之后，+3 日 84.6% 收涨、"
                "均值 +3.40%，全样本基准 52.5% / +0.06%。==")
        detail = doc._validate_forward_axis(_sections(body), _evidence(True, True, 14), problems)
        self.assertEqual(problems, [])
        self.assertTrue(detail["sample_size_cited"])

    def test_card_absent_skips_check(self):
        problems: list = []
        detail = doc._validate_forward_axis(_sections("今日缩量下跌。"), {}, problems)
        self.assertEqual(problems, [], "没有前瞻轴读数的旧报告不得因此失败")
        self.assertFalse(detail["checked"])


class ForecastPhrasingTest(unittest.TestCase):
    def test_bare_probability_claims_are_forbidden(self):
        for text in ("后市大概率反弹", "多半会上涨", "基本上要走强"):
            self.assertTrue(
                doc._FORBIDDEN_FORECAST_PATTERNS["bare_probability_claim"].search(text),
                f"{text!r} 应被拦截")

    def test_imminent_move_claims_are_forbidden(self):
        for text in ("反弹在即", "见底确认", "拐点已至"):
            self.assertTrue(
                doc._FORBIDDEN_FORECAST_PATTERNS["imminent_move_claim"].search(text),
                f"{text!r} 应被拦截")

    def test_conditional_distribution_phrasing_passes(self):
        text = "历史上 14 次同类日之后，+3 日有 84.6% 收涨、均值 +3.40%（全样本基准 52.5%）。"
        for pattern in doc._FORBIDDEN_FORECAST_PATTERNS.values():
            self.assertIsNone(pattern.search(text), "合规的条件分布写法不得被误伤")


# ---------------------------------------------------------------------------
# 图表层：三根轴合并到一条日期轴，缺轴要能少画一行而不是整张图塌掉
# ---------------------------------------------------------------------------
sys.path.insert(0, str(HERE.parents[0] / "scripts"))

import render_report_html as rr  # noqa: E402


def _timeline_evidence(with_pulse: bool = True) -> dict:
    ev = {
        "trend_state_card": {
            "available": True, "data_through": "2026-08-21",
            "history": [{"date": f"2026-08-{d:02d}", "state": "谨慎"} for d in (19, 20, 21)],
        },
        "extreme_state": {
            "available": True, "washout": {"max_score": 6}, "top": {"max_score": 5},
            "recent": [{"date": f"2026-08-{d:02d}", "washout": 2, "top": 0} for d in (19, 20, 21)],
        },
    }
    if with_pulse:
        ev["forward_odds"] = {
            "available": True, "max_legs": 4, "data_through": "2026-08-21",
            "pulse_history": [
                {"date": "2026-08-19", "legs": 4, "gate": True},
                {"date": "2026-08-20", "legs": 0, "gate": False},
                {"date": "2026-08-21", "legs": 0, "gate": False},
            ],
        }
    return ev


class StateTimelinePayloadTest(unittest.TestCase):
    def test_pulse_axis_joins_the_shared_date_axis(self):
        p = rr.extract_state_timeline_payload(_timeline_evidence())
        self.assertEqual(len(p["pulse"]), 3)
        self.assertEqual(p["pulse_max"], 4)
        self.assertEqual([x["date"] for x in p["pulse"] if x["gate"]], ["2026-08-19"])

    def test_missing_forward_axis_degrades_to_two_axes(self):
        p = rr.extract_state_timeline_payload(_timeline_evidence(with_pulse=False))
        self.assertEqual(p["pulse"], [], "没有前瞻轴时只少画一行，不得整张图返回 None")
        self.assertTrue(p["states"] and p["scores"])

    def test_pulse_only_evidence_still_charts(self):
        ev = {"forward_odds": _timeline_evidence()["forward_odds"]}
        p = rr.extract_state_timeline_payload(ev)
        self.assertIsNotNone(p)
        self.assertEqual(len(p["pulse"]), 3)
        self.assertEqual(p["states"], [])

    def test_all_axes_absent_returns_none(self):
        self.assertIsNone(rr.extract_state_timeline_payload({}))

    def test_unavailable_forward_card_is_ignored(self):
        ev = _timeline_evidence()
        ev["forward_odds"]["available"] = False
        p = rr.extract_state_timeline_payload(ev)
        self.assertEqual(p["pulse"], [])


class HistoryWindowTest(unittest.TestCase):
    def test_all_three_axes_share_the_same_window_length(self):
        """三根轴画在同一条日期轴上，窗口长度必须一致，否则图会左右错位。"""
        import extreme_state_card as esc
        import trend_state_card as tsc
        self.assertEqual(fo.HISTORY_DAYS, 30)
        self.assertEqual(esc.HISTORY_DAYS, fo.HISTORY_DAYS)
        self.assertEqual(tsc.HISTORY_DAYS, fo.HISTORY_DAYS)
