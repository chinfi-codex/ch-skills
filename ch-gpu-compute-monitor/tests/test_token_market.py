"""需求端 token 量价的回归测试。

盯的是这一路最容易出、又最难在成品里看出来的四类错：
join 键少了 variant（标准流量被按折扣价计，实测 spend 差 63%）、
篮子成员按 slug 而不是家族（版本升级被误记成结构迁移）、
Laspeyres 没锁住输入输出结构（mix 从后门漏进"真价格"）、
覆盖率残缺时还照常报 spend。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from collectors.openrouter import (  # noqa: E402
    build_price_index,
    lookup,
    settled_points,
    split_variant,
    strip_date,
)
from metrics import Evidence, _unit_price  # noqa: E402
import render_report_html as R  # noqa: E402


def _fake_evidence(rows, **composition):
    """不碰数据库造一个 Evidence：只填 token_composition 用到的那几个属性。"""
    ev = Evidence.__new__(Evidence)
    ev.tokens = rows
    ev.basket_cfg = {"composition": {"top_n": 3, "rank_by": "tokens",
                                     "split_variant": True, **composition}}
    return ev


def _row(day, slug, variant, tokens, requests, priced=True, spend=0.0):
    return {"obs_date": day, "model_slug": slug, "variant": variant,
            "prompt_tokens": tokens, "completion_tokens": 0, "requests": requests,
            "is_priced": priced, "spend_usd": spend, "quality_flag": "ok"}


def approx(expected: float, tol: float = 1e-6):
    class _Approx:
        def __eq__(self, other):
            return other is not None and abs(float(other) - expected) <= tol

        def __repr__(self):
            return f"approx({expected})"

    return _Approx()


# 实测形状：变体条目与标准条目共用同一个 canonical_slug。
PRICING = [
    {"id": "anthropic/claude-opus-5",
     "canonical_slug": "anthropic/claude-opus-5-20260723",
     "pricing": {"prompt": "0.000005", "completion": "0.000025"}},
    {"id": "anthropic/claude-opus-5:batch",
     "canonical_slug": "anthropic/claude-opus-5-20260723",
     "pricing": {"prompt": "0.0000025", "completion": "0.0000125"}},
    {"id": "nvidia/nemotron-3.5-lightning",
     "canonical_slug": "nvidia/nemotron-3.5-lightning-20260807",
     "pricing": {"prompt": "0.00000008", "completion": "0.00000016"}},
    {"id": "nvidia/nemotron-3.5-lightning:free",
     "canonical_slug": "nvidia/nemotron-3.5-lightning-20260807",
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openai/gpt-4.1-nano-2025-04-14",
     "canonical_slug": "openai/gpt-4.1-nano-2025-04-14",
     "pricing": {"prompt": "0.0000001", "completion": "0.0000004"}},
]


class TestVariantAwareJoin:
    def test_standard_traffic_is_not_priced_at_batch_rate(self):
        """全案最大的坑：按 canonical_slug 建索引会让 :batch 覆盖标准条目。

        标准流量被按半价计，实测整站 spend 差 63%。
        """
        index = build_price_index(PRICING)
        entry, how = lookup(index, "anthropic/claude-opus-5-20260723", "standard")
        assert entry is not None
        assert entry["id"] == "anthropic/claude-opus-5"
        assert float(entry["pricing"]["prompt"]) == approx(0.000005)
        assert how in ("exact", "date_stripped")

    def test_batch_traffic_gets_the_batch_price(self):
        index = build_price_index(PRICING)
        entry, _ = lookup(index, "anthropic/claude-opus-5-20260723", "batch")
        assert entry["id"] == "anthropic/claude-opus-5:batch"
        assert float(entry["pricing"]["prompt"]) == approx(0.0000025)

    def test_standard_traffic_is_not_priced_at_zero_by_free_variant(self):
        """:free 条目同样会碰撞。撞上了，付费流量会被整片记成零价。"""
        index = build_price_index(PRICING)
        entry, _ = lookup(index, "nvidia/nemotron-3.5-lightning-20260807", "standard")
        assert float(entry["pricing"]["prompt"]) > 0

    def test_free_variant_stays_zero(self):
        index = build_price_index(PRICING)
        entry, _ = lookup(index, "nvidia/nemotron-3.5-lightning-20260807", "free")
        assert float(entry["pricing"]["prompt"]) == approx(0.0)

    def test_unmatched_is_reported_not_guessed(self):
        index = build_price_index(PRICING)
        entry, how = lookup(index, "baai/bge-m3-20251117", "standard")
        assert entry is None and how == "unmatched"


class TestSlugParsing:
    def test_date_suffix_is_stripped_to_family(self):
        assert strip_date("deepseek/deepseek-v4-flash-20260731") == "deepseek/deepseek-v4-flash"
        assert strip_date("deepseek/deepseek-v4-flash-20260423") == "deepseek/deepseek-v4-flash"

    def test_two_versions_collapse_into_one_family(self):
        """实测 deepseek-v4-flash 一家 13.8% 的 token 横跨两个日期版本。
        按 slug 当篮子成员，这次版本迭代会被整块记成结构迁移。"""
        a = strip_date("deepseek/deepseek-v4-flash-20260423")
        b = strip_date("deepseek/deepseek-v4-flash-20260731")
        assert a == b

    def test_non_date_version_suffix_is_left_alone(self):
        """gpt-4.1-nano-2025-04-14 不是 -20YYMMDD 的形状，不许误伤。"""
        assert strip_date("openai/gpt-4.1-nano-2025-04-14") == "openai/gpt-4.1-nano-2025-04-14"

    def test_variant_split(self):
        assert split_variant("anthropic/claude-opus-5:batch") == (
            "anthropic/claude-opus-5", "batch")
        assert split_variant("anthropic/claude-opus-5") == (
            "anthropic/claude-opus-5", "standard")


class TestUnitPrice:
    def test_uses_base_period_io_mix_not_current(self):
        """Laspeyres 必须锁住输入输出结构。

        单价没变、只有输入输出比例变了的时候，指数一动都不该动——
        否则 mix 就从后门漏进了"真价格"。
        """
        prices = {"price_prompt": 1.0, "price_completion": 10.0}
        base_weights = {"prompt_tokens": 0.9, "paid_tokens": 1.0}
        # 当期结构变成 50/50，但我们拿基期结构去乘
        assert _unit_price(prices, base_weights) == approx(0.9 * 1.0 + 0.1 * 10.0)

    def test_current_mix_would_give_a_different_number(self):
        prices = {"price_prompt": 1.0, "price_completion": 10.0}
        current = {"prompt_tokens": 5, "paid_tokens": 10}
        assert _unit_price(prices, current) == approx(5.5)

    def test_missing_price_side_falls_back_not_zero(self):
        prices = {"price_prompt": 2.0, "price_completion": None}
        assert _unit_price(prices, {"prompt_tokens": 1, "paid_tokens": 2}) == approx(2.0)

    def test_no_price_at_all_returns_none(self):
        assert _unit_price({"price_prompt": None, "price_completion": None},
                           {"prompt_tokens": 1, "paid_tokens": 2}) is None

    def test_missing_weights_returns_none_not_a_guess(self):
        assert _unit_price({"price_prompt": 1.0, "price_completion": 2.0},
                           {"prompt_tokens": None, "paid_tokens": 1.0}) is None


class TestSpendMath:
    def test_spend_uses_split_prompt_and_completion_prices(self):
        """prompt 与 completion 单价差好几倍，用一个平均价算 spend 会错。"""
        prompt_tokens, completion_tokens = 1_000_000, 100_000
        p_prompt, p_completion = 0.000005, 0.000025
        spend = prompt_tokens * p_prompt + completion_tokens * p_completion
        assert spend == approx(5.0 + 2.5)

    def test_cache_sensitivity_direction(self):
        """缓存命中率越高，名义 spend 相对真实账单高估得越多。"""
        prompt_spend, completion_spend, ratio = 91.0, 9.0, 0.119
        nominal = prompt_spend + completion_spend
        over = []
        for rate in (0.2, 0.4, 0.6):
            actual = prompt_spend * (1 - rate) + prompt_spend * rate * ratio + completion_spend
            over.append(nominal / actual - 1)
        assert over[0] < over[1] < over[2]
        assert over[0] > 0


class TestHistorySettlement:
    """回填那一路的两条命门：丢掉活的那个点、不与日度序列拼接。"""

    def test_last_point_is_dropped(self):
        """实测最后一点在几分钟内从 2.78443e13 降到 2.75664e13——
        会往下走说明是滑动窗口，存进去就是存了个随取数时刻变化的数。"""
        points = [{"x": "2026-08-03", "ys": {"a": 1}},
                  {"x": "2026-08-10", "ys": {"a": 2}},
                  {"x": "2026-08-17", "ys": {"a": 3}},
                  {"x": "2026-08-24", "ys": {"a": 4}}]
        settled, live = settled_points(points)
        assert [p["x"] for p in settled] == ["2026-08-03", "2026-08-10", "2026-08-17"]
        assert live["x"] == "2026-08-24"

    def test_points_are_sorted_before_dropping(self):
        """接口顺序不保证；不排序就可能把中间某个点当成最新的丢掉。"""
        points = [{"x": "2026-08-17", "ys": {}}, {"x": "2026-08-03", "ys": {}},
                  {"x": "2026-08-10", "ys": {}}]
        settled, live = settled_points(points)
        assert [p["x"] for p in settled] == ["2026-08-03", "2026-08-10"]
        assert live["x"] == "2026-08-17"

    def test_single_point_leaves_nothing_settled(self):
        settled, live = settled_points([{"x": "2026-08-24", "ys": {}}])
        assert settled == [] and live["x"] == "2026-08-24"

    def test_history_and_daily_ratio_is_not_constant(self):
        """回归保护：两条序列的比值按厂商差很多（实测 1.42–1.75），
        所以不存在"乘一个系数就能拼起来"这回事。谁要改成拼接，这条会先响。"""
        daily = {"xiaomi": 1.68e12, "google": 1.048e12, "deepseek": 3.091e12}
        history = {"xiaomi": 2.393e12, "google": 1.834e12, "deepseek": 4.526e12}
        ratios = [history[a] / daily[a] for a in daily]
        assert max(ratios) / min(ratios) > 1.15


class TestComposition:
    """构成图：条带必须由锚定日一次定死，「其他」必须是减出来的余数。"""

    ROWS = [
        _row("2026-08-24", "a/one", "standard", 100, 10),
        _row("2026-08-24", "b/two", "standard", 50, 90),
        _row("2026-08-24", "c/three", "standard", 30, 5),
        _row("2026-08-24", "d/four", "standard", 20, 4),
        _row("2026-08-24", "e/five", "standard", 10, 3),
        _row("2026-08-23", "a/one", "standard", 80, 8),
        _row("2026-08-23", "e/five", "standard", 70, 7),
    ]

    def test_other_is_the_remainder_not_a_recount(self):
        comp = _fake_evidence(self.ROWS).token_composition("2026-08-24")
        latest = comp["series"][-1]
        assert latest["total"] == 210
        assert sum(latest["values"].values()) == latest["total"]
        assert latest["values"]["__other__"] == 210 - (100 + 50 + 30)

    def test_bands_are_fixed_by_anchor_day(self):
        """每天各取前 N 名会让条带含义天天变，叠出来的面积图是假的。

        8-23 那天 e/five 是第二大，但它不在锚定日的前 3 名，所以那天它必须
        落进「其他」，而不是挤掉某条带子。
        """
        comp = _fake_evidence(self.ROWS).token_composition("2026-08-24")
        keys = [b["key"] for b in comp["bands"]]
        assert keys == ["a/one:standard", "b/two:standard", "c/three:standard",
                        "__other__"]
        earlier = comp["series"][0]
        assert earlier["date"] == "2026-08-23"
        assert set(earlier["values"]) == set(keys)
        assert earlier["values"]["a/one:standard"] == 80
        assert earlier["values"]["b/two:standard"] == 0      # 那天没出现，补 0 不补值
        assert earlier["values"]["__other__"] == 70          # e/five 落进其他

    def test_rank_by_requests_picks_a_different_set(self):
        """按调用次数排和按 token 量排是两套答案：b/two 调用最多、token 只排第二。"""
        comp = _fake_evidence(self.ROWS, rank_by="requests").token_composition(
            "2026-08-24")
        assert comp["ranked_by"] == "requests"
        assert comp["bands"][0]["key"] == "b/two:standard"

    def test_zero_priced_band_is_flagged(self):
        rows = list(self.ROWS) + [_row("2026-08-24", "z/stealth", "standard",
                                       999, 1, priced=False)]
        comp = _fake_evidence(rows).token_composition("2026-08-24")
        top = comp["bands"][0]
        assert top["key"] == "z/stealth:standard" and top["is_priced"] is False


class TestCompositionChart:
    def test_area_needs_two_days_and_falls_back_to_a_bar(self):
        """一天画不出面积。退成横条比显示"暂无数据"诚实——构成本身当天就成立。"""
        comp = _fake_evidence(TestComposition.ROWS).token_composition("2026-08-24")
        one_day = dict(comp, series=comp["series"][-1:])
        assert R.stacked_area(one_day) == ""
        bar = R.stacked_bar(one_day)
        assert "<rect" in bar and "svg" in bar
        assert "堆叠条" in R.render_composition(one_day)

    def test_area_draws_one_path_per_band(self):
        comp = _fake_evidence(TestComposition.ROWS).token_composition("2026-08-24")
        svg = R.stacked_area(comp)
        assert svg.count("<path") == len(comp["bands"])
        assert "NaN" not in svg and "Infinity" not in svg

    def test_bar_segments_fill_the_full_width(self):
        comp = _fake_evidence(TestComposition.ROWS).token_composition("2026-08-24")
        bar = R.stacked_bar(dict(comp, series=comp["series"][-1:]))
        widths = [float(w) for w in re.findall(r'width="([0-9.]+)"', bar)]
        assert abs(sum(widths) - 1440) < 1.0


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
