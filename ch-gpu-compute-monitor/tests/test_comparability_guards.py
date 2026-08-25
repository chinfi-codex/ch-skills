"""指标层可比性守卫的回归测试 —— 本项目最容易出的假信号都在这里钉死。

这些不是形式化的边界测试。每一条都对应一个实测踩过的坑：
接入新数据源那天跨平台中枢凭空"跌"25%、Ornn T-1 结算让锚点落在空口径上、
冷启动期评分被算出一个看起来很像样的数字。
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import yaml  # noqa: E402

from metrics import Evidence, _usable_pct, pct_change  # noqa: E402


def make_evidence(asof: str = "2026-08-25", window: int = 90) -> Evidence:
    """不碰数据库地造一个 Evidence —— 这些守卫是纯函数，不需要真数据。"""
    ev = Evidence.__new__(Evidence)
    ev.asof = asof
    ev.window = window
    ev.prices = []
    ev.supply = []
    ev.runs = []
    ev.thresholds = yaml.safe_load(
        (SKILL_ROOT / "config" / "thresholds.yaml").read_text(encoding="utf-8"))
    return ev


class TestBasisGuard:
    def test_source_set_change_makes_change_unusable(self):
        """新接一个源的那天，中枢会跳。那不是价格动了，是口径动了。"""
        ev = make_evidence()
        series = {"2026-08-18": 3.04, "2026-08-25": 2.24}
        basis = {"2026-08-18": "ornn", "2026-08-25": "ornn+runpod+vast"}
        out = ev.changes(series, basis=basis)
        change = out["changes"]["7d"]
        assert change["pct"] is not None            # 数字照算，供追溯
        assert change["usable"] is False            # 但不许拿来下结论
        assert change["basis_match"] is False
        assert "口径" in change["reason"]
        assert _usable_pct(change) is None          # 下游一律读不到它

    def test_same_source_set_is_usable(self):
        ev = make_evidence()
        series = {"2026-08-18": 3.04, "2026-08-25": 3.30}
        basis = {"2026-08-18": "ornn", "2026-08-25": "ornn"}
        change = ev.changes(series, basis=basis)["changes"]["7d"]
        assert change["usable"] is True
        assert change["basis_match"] is True
        assert _usable_pct(change) is not None

    def test_no_basis_means_no_basis_check(self):
        """单源序列本来就同口径，不该被口径规则误伤。"""
        ev = make_evidence()
        change = ev.changes({"2026-08-18": 3.0, "2026-08-25": 3.3})["changes"]["7d"]
        assert change["usable"] is True
        assert "basis_match" not in change


class TestDateDrift:
    def test_far_base_point_is_unusable(self):
        """窗口起点附近整周没数据时，找到的"7 日前"其实是 20 天前。"""
        ev = make_evidence()
        change = ev.changes({"2026-08-05": 3.0, "2026-08-25": 3.3})["changes"]["7d"]
        assert change["usable"] is False
        assert change["date_drift_days"] > 2

    def test_small_drift_is_tolerated(self):
        """容忍单日缺采，否则一次采集失败会连累整周的指标。"""
        ev = make_evidence()
        change = ev.changes({"2026-08-17": 3.0, "2026-08-25": 3.3})["changes"]["7d"]
        assert change["usable"] is True

    def test_missing_window_start_returns_reason_not_zero(self):
        ev = make_evidence()
        change = ev.changes({"2026-08-25": 3.3})["changes"]["30d"]
        assert change["pct"] is None
        assert "无观测" in change["reason"]


class TestModalBasisAnchor:
    def test_anchor_skips_the_partial_latest_day(self):
        """Ornn T-1 结算 → 今天的中枢只剩 runpod+vast，锚点必须退回昨天。"""
        ev = make_evidence()
        basis = {f"2026-08-{d:02d}": "ornn" for d in range(12, 25)}
        basis["2026-08-25"] = "runpod+vast"
        anchor, modal = ev._modal_basis_anchor(basis)
        assert modal == "ornn"
        assert anchor == "2026-08-24"

    def test_empty_basis_is_handled(self):
        assert make_evidence()._modal_basis_anchor({}) == (None, None)


class TestScoreRefusal:
    def _blank_supply(self):
        empty = {"latest": None, "changes": {}, "series": []}
        return {"offer_share": empty, "available_gpu_count": empty,
                "available_region_count": empty,
                "stock_status": {"latest": None, "rank": None,
                                 "consecutive_days_at_latest": 0, "history": []}}

    def test_cold_start_refuses_to_score(self):
        """历史不够就不出分。出一个"近似分"比不出分危险得多。"""
        ev = make_evidence()
        price = {"changes": {"7d": {"pct": -12.0, "usable": True},
                             "30d": {"pct": -20.0, "usable": True}},
                 "window_points": 3, "source_set": ["ornn"]}
        score = ev.score("H100 SXM", price, self._blank_supply(), [])
        assert score["usable"] is False
        assert score["value"] is None
        assert any("门槛" in b for b in score["blockers"])

    def test_components_are_exposed_even_when_refusing(self):
        """拒绝出分时仍要交出原料，模型才能自己判断。"""
        ev = make_evidence()
        price = {"changes": {"7d": {"pct": -12.0, "usable": True}},
                 "window_points": 3, "source_set": ["ornn"]}
        score = ev.score("H100 SXM", price, self._blank_supply(), [])
        raw = {c["name"]: c["raw_pct"] for c in score["components"]}
        assert raw["cross_platform_median_7d"] == -12.0
        assert raw["offer_share_7d"] is None

    def test_price_drop_scores_negative_when_inputs_are_complete(self):
        """价格跌 + 供给放量 = 偏松 = 负分。符号方向不能反。"""
        ev = make_evidence()
        price = {"changes": {"7d": {"pct": -12.0, "usable": True},
                             "30d": {"pct": -20.0, "usable": True}},
                 "window_points": 60, "source_set": ["ornn", "runpod"]}
        supply = self._blank_supply()
        supply["offer_share"] = {"changes": {"7d": {"pct": 40.0, "usable": True}}}
        supply["available_gpu_count"] = {"changes": {"7d": {"pct": 60.0, "usable": True}}}
        score = ev.score("H100 SXM", price, supply, [])
        assert score["usable"] is True
        assert score["value"] < 0

    def test_source_set_change_marks_score_incomparable(self):
        ev = make_evidence()
        price = {"changes": {"7d": {"pct": -12.0, "usable": True},
                             "30d": {"pct": -20.0, "usable": True}},
                 "window_points": 60, "source_set": ["ornn", "runpod"],
                 "source_set_changed_vs_prev_day": True}
        supply = self._blank_supply()
        supply["offer_share"] = {"changes": {"7d": {"pct": 40.0, "usable": True}}}
        score = ev.score("H100 SXM", price, supply, [])
        assert score["comparable_across_days"] is False


class TestAlerts:
    def _supply(self):
        empty = {"latest": None, "changes": {}, "series": []}
        return {"offer_share": empty, "available_gpu_count": empty,
                "available_region_count": empty,
                "stock_status": {"latest": None, "rank": None,
                                 "consecutive_days_at_latest": 0, "history": []}}

    def test_unusable_change_never_fires_an_alert(self):
        """口径变动造成的 -26% 绝不能触发"快速降价"。"""
        ev = make_evidence()
        price = {"changes": {"7d": {"pct": -26.0, "usable": False,
                                    "basis_match": False}}}
        assert ev.alerts("H100 SXM", price, self._supply(), []) == []

    def test_real_drop_fires(self):
        ev = make_evidence()
        price = {"changes": {"7d": {"pct": -13.4, "usable": True}}}
        fired = ev.alerts("H200 SXM", price, self._supply(), [])
        assert [a["id"] for a in fired] == ["fast_price_drop"]
        assert fired[0]["mode"] == "record_only"

    def test_alerts_are_two_sided(self):
        """实测三个月 OCPI 方向是收紧；只有宽松侧的告警会长期零触发。"""
        ev = make_evidence()
        price = {"changes": {"7d": {"pct": 11.0, "usable": True}}}
        fired = ev.alerts("H100 SXM", price, self._supply(), [])
        assert [a["id"] for a in fired] == ["fast_price_spike"]


class TestPctChange:
    def test_none_inputs_return_none(self):
        assert pct_change(None, 3.0) is None
        assert pct_change(3.0, None) is None

    def test_zero_base_returns_none_not_infinity(self):
        assert pct_change(3.0, 0) is None


def _run() -> int:
    failures, total = [], 0
    for name, obj in sorted(globals().items()):
        if not (name.startswith("Test") and isinstance(obj, type)):
            continue
        instance = obj()
        for attr in sorted(dir(instance)):
            if not attr.startswith("test_"):
                continue
            total += 1
            try:
                getattr(instance, attr)()
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}.{attr}: {type(exc).__name__}: {exc}")
    for line in failures:
        print("FAIL", line)
    print(f"{total - len(failures)}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run())
