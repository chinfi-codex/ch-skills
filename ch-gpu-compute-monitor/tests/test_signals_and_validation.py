"""确认型拐点、分散度、供给广度、跨日离群检测的回归测试。

对应补上的 PRD 缺口：§4.3/§8 的确认型拐点、§4.2 的 Quote Dispersion 与
Supply Breadth、§6.1 步骤 4 的异常值与单位错误检测。
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import yaml  # noqa: E402

from metrics import Evidence  # noqa: E402
from validate import flag_outliers  # noqa: E402


def make_evidence(asof: str = "2026-08-25", window: int = 90) -> Evidence:
    ev = Evidence.__new__(Evidence)
    ev.asof, ev.window = asof, window
    ev.prices, ev.supply, ev.runs = [], [], []
    ev.thresholds = yaml.safe_load(
        (SKILL_ROOT / "config" / "thresholds.yaml").read_text(encoding="utf-8"))
    return ev


def price_row(day, source, ptype, value, model="H100 SXM"):
    return {"obs_date": day, "source": source, "gpu_model": model,
            "price_type": ptype, "market_segment": "default", "region": "global",
            "price_usd_gpu_hour": value, "quality_flag": "ok", "sample_count": 20,
            "query_fingerprint": "f"}


def supply_row(day, source, model="H100 SXM", **kw):
    base = {"obs_date": day, "source": source, "gpu_model": model,
            "market_segment": "default", "offer_share": None,
            "available_gpu_count": None, "available_region_count": None,
            "stock_status": None, "offer_count": None, "query_fingerprint": "f"}
    base.update(kw)
    return base


def _days(n, start_day=1):
    return [f"2026-08-{d:02d}" for d in range(start_day, start_day + n)]


class TestConfirmation:
    """PRD §4.3：价格类 ≥3 + 供给类 ≥2，连续 ≥10 个采集日。"""

    def _loosening_fixture(self, days, price_sources=3, supply_sources=2):
        """造一批持续下跌的价格 + 持续放量的供给。"""
        prices, supply = [], []
        for i, day in enumerate(days):
            for k in range(price_sources):
                # 每天跌一点，7 日方向必然为负
                prices.append(price_row(day, f"src{k}", "on_demand", 10.0 - i * 0.3))
            for k in range(supply_sources):
                supply.append(supply_row(day, f"sup{k}",
                                         offer_share=0.1 + i * 0.02,
                                         available_gpu_count=10 + i * 3))
        return prices, supply

    def test_confirms_loosening_after_enough_consecutive_days(self):
        days = _days(25)
        ev = make_evidence(asof=days[-1])
        ev.prices, ev.supply = self._loosening_fixture(days)
        out = ev.confirmation("H100 SXM")
        assert out["verdict"] == "loosening"
        assert out["loosening"]["streak_days"] >= 10
        assert out["blockers"] == []

    def test_not_enough_days_is_not_confirmed(self):
        days = _days(12)          # 前 7 天算不出 7 日方向，实际连续不足 10
        ev = make_evidence(asof=days[-1])
        ev.prices, ev.supply = self._loosening_fixture(days)
        out = ev.confirmation("H100 SXM")
        assert out["verdict"] == "none"
        assert out["loosening"]["streak_days"] < 10

    def test_too_few_price_signals_blocks_judgement(self):
        """只有一个源有历史时，凑不满 3 类价格信号——这正是本项目的现状。"""
        days = _days(25)
        ev = make_evidence(asof=days[-1])
        ev.prices, ev.supply = self._loosening_fixture(days, price_sources=1)
        out = ev.confirmation("H100 SXM")
        assert out["verdict"] == "none"
        assert any("价格序列" in b for b in out["blockers"])

    def test_gap_in_collection_restarts_the_streak(self):
        """allow_gap_days=0：缺采一天就重新起算，不许跨缺口连续计数。"""
        days = _days(11) + _days(11, start_day=13)   # 08-12 缺采
        ev = make_evidence(asof=days[-1])
        ev.prices, ev.supply = self._loosening_fixture(days)
        out = ev.confirmation("H100 SXM")
        span = out["loosening"]["span"]
        if span:
            assert not (span[0] <= "2026-08-12" <= span[1] and
                        out["loosening"]["streak_days"] > 11)

    def test_direction_is_symmetric(self):
        """涨的时候要能确认收紧，不能只有宽松一个方向。"""
        days = _days(25)
        prices, supply = [], []
        for i, day in enumerate(days):
            for k in range(3):
                prices.append(price_row(day, f"src{k}", "on_demand", 3.0 + i * 0.2))
            for k in range(2):
                supply.append(supply_row(day, f"sup{k}",
                                         offer_share=0.5 - i * 0.01,
                                         available_gpu_count=100 - i * 3))
        ev = make_evidence(asof=days[-1])
        ev.prices, ev.supply = prices, supply
        out = ev.confirmation("H100 SXM")
        assert out["verdict"] == "tightening"

    def test_reference_day_skips_the_empty_latest_day(self):
        """Ornn T-1 结算：最后一天价格序列必然为空，参考日要退回前一天。"""
        days = _days(20)
        ev = make_evidence(asof="2026-08-21")
        ev.prices, ev.supply = self._loosening_fixture(days)
        out = ev.confirmation("H100 SXM")
        assert out["reference_date"] == "2026-08-20"

    def test_segment_series_are_aggregated_without_order_dependent_overwrite(self):
        """同一来源/类型的多个档位只算一票，且方向不依赖数据库返回顺序。"""
        days = _days(25)
        prices = []
        for i, day in enumerate(days):
            for segment, value in (("community", 12.0 - i * 0.3),
                                   ("secure", 10.0 - i * 0.2),
                                   ("lowest", 6.0 + i * 0.05)):
                row = price_row(day, "runpod", "on_demand", value)
                row["market_segment"] = segment
                prices.append(row)
        tallies = []
        for ordered in (prices, list(reversed(prices))):
            ev = make_evidence(asof=days[-1])
            ev.prices = ordered
            tallies.append(ev._daily_signal_tally("H100 SXM")[days[-1]])
        assert tallies[0] == tallies[1]
        assert tallies[0]["price_series_live"] == 1
        assert tallies[0]["price_down"] == 1

    def test_fingerprint_change_breaks_confirmation_streak(self):
        """查询口径变化前后的值不可比较，也不能拼成连续确认。"""
        days = _days(25)
        ev = make_evidence(asof=days[-1])
        ev.prices, ev.supply = self._loosening_fixture(days)
        for row in ev.prices + ev.supply:
            row["query_fingerprint"] = "old" if row["obs_date"] < "2026-08-16" else "new"
        out = ev.confirmation("H100 SXM")
        assert out["verdict"] == "none"
        assert out["loosening"]["streak_days"] < 10


class TestQuoteDispersion:
    def test_spread_and_relative_spread(self):
        ev = make_evidence()
        ev.prices = [
            price_row("2026-08-25", "vast", "offer_p25", 6.0),
            price_row("2026-08-25", "vast", "offer_median", 7.0),
            price_row("2026-08-25", "vast", "offer_p75", 8.0),
        ]
        for r in ev.prices:
            r["market_segment"] = "on_demand"
        out = ev.quote_dispersion("H100 SXM")[0]
        assert out["spread"] == 2.0
        assert out["spread_pct_of_median"] == 28.57

    def test_missing_quantiles_reports_reason_not_zero(self):
        ev = make_evidence()
        ev.prices = [price_row("2026-08-25", "vast", "offer_median", 7.0)]
        ev.prices[0]["market_segment"] = "on_demand"
        out = ev.quote_dispersion("H100 SXM")[0]
        assert out["spread"] is None
        assert "样本不足" in out["reason"]


class TestSupplyBreadth:
    def test_counts_only_sources_that_actually_reported(self):
        """没表态的源不进分母——否则「没数据」会被读成「没货」。"""
        ev = make_evidence()
        ev.supply = [
            supply_row("2026-08-25", "vast", offer_count=12),
            supply_row("2026-08-25", "runpod", stock_status="Low"),
            supply_row("2026-08-25", "quiet"),          # 什么都没报
        ]
        out = ev.supply_breadth("H100 SXM")
        assert out["reporting"] == 2
        assert out["with_stock"] == 2
        assert out["breadth"] == 1.0

    def test_zero_offers_counts_as_no_stock(self):
        ev = make_evidence()
        ev.supply = [
            supply_row("2026-08-25", "vast", offer_count=0),
            supply_row("2026-08-25", "runpod", stock_status="High"),
        ]
        out = ev.supply_breadth("H100 SXM")
        assert out["with_stock"] == 1 and out["reporting"] == 2
        assert out["breadth"] == 0.5

    def test_explicit_no_stock_stays_in_denominator(self):
        ev = make_evidence()
        missing = supply_row("2026-08-25", "runpod")
        missing["quality_flag"] = "no_stock"
        ev.supply = [supply_row("2026-08-25", "vast", offer_count=5), missing]
        out = ev.supply_breadth("H100 SXM")
        assert out["reporting"] == 2 and out["with_stock"] == 1
        assert out["breadth"] == 0.5

    def test_unknown_stock_status_is_not_treated_as_available(self):
        ev = make_evidence()
        ev.supply = [supply_row("2026-08-25", "runpod", stock_status="Unknown")]
        out = ev.supply_breadth("H100 SXM")
        assert out["reporting"] == 0 and out["breadth"] is None

    def test_no_observations_reports_reason(self):
        out = make_evidence().supply_breadth("H100 SXM")
        assert out["breadth"] is None and "没有任何供给观测" in out["reason"]


class TestFullHistorySeries:
    """补不回去的序列不许被 90 天窗口裁掉——掐掉的那一段永久丢失。"""

    def test_supply_series_ignores_window(self):
        ev = make_evidence(asof="2026-08-25", window=7)
        # 窗口起点是 08-18，但 08-01 那个点必须还在
        ev.supply = [supply_row("2026-08-01", "vast", offer_count=10,
                                offer_share=0.4, available_gpu_count=100),
                     supply_row("2026-08-25", "vast", offer_count=12,
                                offer_share=0.5, available_gpu_count=140)]
        out = ev.supply_view("H100 SXM")
        for key in ("offer_share", "available_gpu_count"):
            days = [p["date"] for p in out[key]["series"]]
            assert days == ["2026-08-01", "2026-08-25"], key
            assert out[key]["series_scope"] == "full_history"

    def test_series_list_without_window_keeps_everything(self):
        from metrics import _series_list

        series = {"2026-01-01": 1.0, "2026-08-01": 2.0, "2026-08-25": 3.0}
        clipped = [p["date"] for p in _series_list(series, "2026-08-25", 7)]
        assert clipped == ["2026-08-25"]
        full = [p["date"] for p in _series_list(series, "2026-08-25", None)]
        assert full == ["2026-01-01", "2026-08-01", "2026-08-25"]


class TestOutlierValidation:
    CFG = {"enabled": True, "lookback_days": 30, "min_history_points": 5,
           "max_jump_pct": 60, "unit_error_tolerance": 0.12}
    KEY = ("coreweave", "H100 SXM", "on_demand", "default", "global")

    HIST = {"2026-08-%02d" % d: v for d, v in
            zip(range(18, 24), [6.1, 6.2, 6.15, 6.16, 6.1, 6.12])}

    def _row(self, value, day="2026-08-25"):
        return {"source": "coreweave", "gpu_model": "H100 SXM", "obs_date": day,
                "price_type": "on_demand", "market_segment": "default",
                "region": "global", "price_usd_gpu_hour": value, "quality_flag": "ok"}

    def test_eightfold_jump_is_named_as_unit_error(self):
        """整机价忘了除以 8 —— 成因明确，值得单独点名。"""
        hits = flag_outliers([self._row(49.24)], self.CFG,
                             history={self.KEY: self.HIST})
        assert hits[0]["reason"] == "unit_error"
        assert "8×" in hits[0]["detail"]

    def test_divided_twice_is_also_caught(self):
        hits = flag_outliers([self._row(0.77)], self.CFG,
                             history={self.KEY: self.HIST})
        assert hits[0]["reason"] == "unit_error"
        assert "1/8" in hits[0]["detail"]

    def test_plain_jump_is_flagged_without_unit_hint(self):
        hits = flag_outliers([self._row(15.0)], self.CFG,
                             history={self.KEY: self.HIST})
        assert hits[0]["reason"] == "jump"

    def test_normal_value_is_untouched(self):
        row = self._row(6.3)
        assert flag_outliers([row], self.CFG,
                             history={self.KEY: self.HIST}) == []
        assert row["quality_flag"] == "ok"

    def test_trend_is_not_mistaken_for_an_outlier(self):
        """实测踩过：Ornn 每次带回 90 天历史，拿三个月前的点比最近 30 天中位数，
        会把 RTX 5090 一段真实的 −38% 下跌整段标成可疑（一次误报 17 条）。
        基准必须取被检查那一行自己日期之前的窗口。"""
        key = ("ornn", "RTX 5090", "transaction_index", "default", "global")
        # 三个月缓慢下行：任何一天相对前 30 天中位数的偏离都不大
        series = {f"2026-{m:02d}-{d:02d}": round(1.33 - 0.009 * i, 3)
                  for i, (m, d) in enumerate(
                      [(6, x) for x in range(1, 31)] + [(7, x) for x in range(1, 31)]
                      + [(8, x) for x in range(1, 25)])}
        rows = [{"source": "ornn", "gpu_model": "RTX 5090", "obs_date": day,
                 "price_type": "transaction_index", "market_segment": "default",
                 "region": "global", "price_usd_gpu_hour": value, "quality_flag": "ok"}
                for day, value in sorted(series.items())]
        hits = flag_outliers(rows, self.CFG, history={key: {}})
        assert hits == [], f"真实趋势被误报 {len(hits)} 条"
        assert all(r["quality_flag"] == "ok" for r in rows)

    def test_spike_inside_a_backfilled_series_is_still_caught(self):
        """趋势不报，但序列内部的突刺要报——本批自己的前序点也进基准池。"""
        key = ("ornn", "RTX 5090", "transaction_index", "default", "global")
        rows = [{"source": "ornn", "gpu_model": "RTX 5090",
                 "obs_date": f"2026-08-{d:02d}", "price_type": "transaction_index",
                 "market_segment": "default", "region": "global",
                 "price_usd_gpu_hour": (9.9 if d == 20 else 0.5), "quality_flag": "ok"}
                for d in range(1, 25)]
        hits = flag_outliers(rows, self.CFG, history={key: {}})
        assert len(hits) == 1 and hits[0]["value"] == 9.9

    def test_cold_start_never_flags(self):
        """历史点不足就不判——宁可漏也不要冤枉真实行情。"""
        row = self._row(49.24)
        assert flag_outliers([row], self.CFG, history={self.KEY: {"2026-08-23": 6.1, "2026-08-24": 6.2}}) == []
        assert row["quality_flag"] == "ok"

    def test_flagged_row_is_marked_not_dropped(self):
        """打标不丢弃：丢了就没法事后判断是真跳价还是采错了。"""
        row = self._row(49.24)
        flag_outliers([row], self.CFG, history={self.KEY: self.HIST})
        assert row["quality_flag"] == "suspicious"
        assert row["price_usd_gpu_hour"] == 49.24

    def test_disabled_config_is_a_no_op(self):
        row = self._row(49.24)
        assert flag_outliers([row], {"enabled": False},
                             history={self.KEY: self.HIST}) == []
        assert row["quality_flag"] == "ok"


class TestAlertPersistence:
    def test_alert_table_columns_cover_the_rule_shape(self):
        import db_adapter
        for field in ("obs_date", "gpu_model", "rule_id", "direction",
                      "observed", "threshold", "op", "mode"):
            assert field in db_adapter.ALERT_COLUMNS
        assert db_adapter.ALERT_KEY == ["obs_date", "gpu_model", "rule_id"]

    def test_every_rule_declares_a_direction(self):
        """确认型拐点要按方向归类，规则没写 direction 就归不了。"""
        cfg = yaml.safe_load(
            (SKILL_ROOT / "config" / "thresholds.yaml").read_text(encoding="utf-8"))
        for rule in cfg["alerts"]["rules"]:
            assert rule.get("direction") in ("loosening", "tightening"), rule["id"]


class TestCollectDryRun:
    def test_dry_run_uses_empty_history_instead_of_querying_database(self):
        """回归保护：dry-run 不建表时不能再由离群检测隐式读库。"""
        import collect

        source_cfg = {"enabled": True}
        result = type("Result", (), {
            # 形状要跟 CollectResult 保持一致：v1.1 起多了 tokens（需求端量价），
            # v1.3 起多了 apps（调用方维度）
            "prices": [], "supply": [], "tokens": [], "apps": [], "notes": [],
            "unmapped": [], "raw_path": None,
        })()
        original_sources = collect.load_sources
        original_catalog = collect.load_catalog
        original_run_one = collect.run_one
        original_history = collect.validate.trailing_history
        original_argv = sys.argv
        try:
            collect.load_sources = lambda: {"defaults": {}, "sources": {"fake": source_cfg}}
            collect.load_catalog = lambda: object()
            collect.run_one = lambda *args, **kwargs: result
            collect.validate.trailing_history = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("dry-run 不应查询历史数据库"))
            sys.argv = ["collect.py", "--dry-run", "--date", "2026-08-25"]
            assert collect.main() == 0  # empty 是成功降级，但不得访问数据库
        finally:
            collect.load_sources = original_sources
            collect.load_catalog = original_catalog
            collect.run_one = original_run_one
            collect.validate.trailing_history = original_history
            sys.argv = original_argv


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
