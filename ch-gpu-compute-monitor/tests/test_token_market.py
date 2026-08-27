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
    _required_nonnegative_int,
    build_app_rows,
    build_price_index,
    lookup,
    parse_app_rows,
    settled_points,
    split_variant,
    strip_date,
)
from metrics import Evidence, _unit_price  # noqa: E402
import render_report_html as R  # noqa: E402
from collectors import openrouter as openrouter_collector  # noqa: E402
from collectors.base import CollectorError  # noqa: E402


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


def _collect_with(rankings, models):
    """给 collector 喂固定响应，不碰网络和 raw 目录。"""
    payloads = iter(({"data": rankings}, {"data": models}))
    original_request = openrouter_collector.request_json
    original_save_raw = openrouter_collector.save_raw
    openrouter_collector.request_json = lambda *args, **kwargs: next(payloads)
    openrouter_collector.save_raw = lambda *args, **kwargs: "raw/test.json"
    try:
        return openrouter_collector.collect({
            "base_url": "https://example.invalid",
            "endpoints": {
                "rankings": "/rankings",
                "models": "/models",
                "endpoints_of": "/models/{model}/endpoints",
            },
            "query": {"view": "day", "provider_probe_top_n": 0},
            "coverage_scope": "gateway",
            "price_basis": "list",
        }, None, "2026-08-25")
    finally:
        openrouter_collector.request_json = original_request
        openrouter_collector.save_raw = original_save_raw


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
    def test_missing_required_token_field_fails_instead_of_becoming_zero(self):
        rankings = [{
            "date": "2026-08-24", "model_permaslug": "vendor/model",
            "variant": "standard", "count": 10,
            # 模拟接口改版：total_prompt_tokens 消失。
            "total_completion_tokens": 100,
        }]
        models = [{
            "id": "vendor/model", "canonical_slug": "vendor/model",
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        }]
        try:
            _collect_with(rankings, models)
        except CollectorError as exc:
            assert "total_prompt_tokens" in str(exc)
        else:
            assert False, "缺少必需 token 字段时必须显式失败"

    def test_missing_price_for_nonzero_token_side_fails(self):
        rankings = [{
            "date": "2026-08-24", "model_permaslug": "vendor/model",
            "variant": "standard", "count": 10,
            "total_prompt_tokens": 1_000_000, "total_completion_tokens": 100_000,
        }]
        models = [{
            "id": "vendor/model", "canonical_slug": "vendor/model",
            "pricing": {"completion": "0.000002"},
        }]
        try:
            _collect_with(rankings, models)
        except CollectorError as exc:
            assert "prompt" in str(exc)
        else:
            assert False, "有 prompt token 却缺 prompt 价格时必须显式失败"

    def test_missing_price_for_zero_token_side_is_allowed(self):
        rankings = [{
            "date": "2026-08-24", "model_permaslug": "vendor/model",
            "variant": "standard", "count": 10,
            "total_prompt_tokens": 0, "total_completion_tokens": 100_000,
        }]
        models = [{
            "id": "vendor/model", "canonical_slug": "vendor/model",
            "pricing": {"completion": "0.000002"},
        }]
        result = _collect_with(rankings, models)
        assert result.tokens[0]["spend_usd"] == approx(0.2)

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


def _app_row(day, app_id, tokens, requests, rank=None, title=None, cats=None):
    """应用榜落库后的一行（源侧的字符串在采集时已经 cast 成 bigint）。"""
    return {"obs_date": day, "source": "openrouter", "app_id": str(app_id),
            "app_title": title or f"App {app_id}", "app_slug": f"app-{app_id}",
            "app_url": None, "categories": cats, "rank": rank,
            "total_tokens": tokens, "total_requests": requests,
            "quality_flag": "ok"}


def _fake_apps(rows, **composition):
    ev = Evidence.__new__(Evidence)
    ev.apps = rows
    ev.basket_cfg = {"composition": {"apps_top_n": 3, **composition}}
    return ev


class TestAppRankings:
    """调用方维度：份额分母、名次缺口、没有 spend 这三件事不能弄错。"""

    ROWS = [
        _app_row("2026-08-25", 1, 500, 5, rank=1, cats=["cli-agent"]),
        _app_row("2026-08-25", 2, 300, 30, rank=5),
        _app_row("2026-08-25", 3, 150, 3, rank=6),
        _app_row("2026-08-25", 4, 50, 2, rank=9),
    ]

    DAILY = {"2026-08-25": {"total_tokens": 2000}}

    def test_share_denominator_is_the_listed_set_not_the_site(self):
        """全站总量当分母算出的百分比两头不靠：既不是占全站也不是占应用侧。"""
        out = _fake_apps(self.ROWS).token_apps("2026-08-25", self.DAILY)
        assert out["listed_tokens_anchor"] == 1000
        assert out["bands"][0]["share"] == 0.5          # 500 / 1000，不是 500 / 2000
        assert out["listed_share_of_site"] == 0.5       # 榜上合计占全站，单独一个数

    def test_other_is_the_remainder_of_the_listed_set(self):
        out = _fake_apps(self.ROWS).token_apps("2026-08-25", self.DAILY)
        other = out["bands"][-1]
        assert other["key"] == "__other__"
        assert other["tokens"] == 50 and other["app_count"] == 1
        assert sum(b["tokens"] for b in out["bands"]) == out["listed_tokens_anchor"]

    def test_rank_gaps_are_reported(self):
        """返回 4 行、最大名次 9 —— 这不是前 4 名，合计只是下界。"""
        out = _fake_apps(self.ROWS).token_apps("2026-08-25", self.DAILY)
        assert out["max_rank"] == 9 and out["hidden_ranks"] == 5

    def test_no_spend_column_is_produced(self):
        """应用榜不拆模型，配不上价。凭空补一列 spend 就是编。"""
        out = _fake_apps(self.ROWS).token_apps("2026-08-25", self.DAILY)
        assert all("spend_usd" not in b for b in out["bands"])
        assert out["no_spend_reason"]
        assert "spend" not in R.apps_table(out)

    def test_missing_site_total_leaves_share_none_not_zero(self):
        out = _fake_apps(self.ROWS).token_apps("2026-08-25", {})
        assert out["listed_share_of_site"] is None

    def test_fallback_day_does_not_borrow_another_days_site_total(self):
        """应用榜退到 8-25、token 锚点却是 8-26 时，占比会变成跨日相除。

        分母必须跟着应用锚定日走；那天没有模型侧观测就不出这个数，
        而不是拿 8-26 的全站量去除 8-25 的应用量。
        """
        daily = {"2026-08-25": {"total_tokens": 2000},
                 "2026-08-26": {"total_tokens": 9999}}
        out = _fake_apps(self.ROWS).token_apps("2026-08-26", daily)
        assert out["anchor_date"] == "2026-08-25"
        assert out["anchor_fallback_from"] == "2026-08-26"
        assert out["site_tokens_anchor"] == 2000          # 不是 9999
        assert out["listed_share_of_site"] == 0.5
        assert out["site_tokens_date"] == "2026-08-25"

    def test_fallback_without_that_days_site_total_drops_the_ratio(self):
        out = _fake_apps(self.ROWS).token_apps(
            "2026-08-26", {"2026-08-26": {"total_tokens": 9999}})
        assert out["anchor_date"] == "2026-08-25"
        assert out["listed_share_of_site"] is None
        assert "2026-08-25" in out["share_of_site_reason"]

    def test_no_observations_reports_reason(self):
        out = _fake_apps([]).token_apps("2026-08-25", self.DAILY)
        assert out["usable"] is False and "应用榜" in out["reason"]


class TestAppPayloadParsing:
    def test_reads_the_view_bucket_not_the_data_array(self):
        payload = {"data": {"day": [{"app_id": 1, "rank": 1}],
                            "week": [{"app_id": 1, "rank": 1},
                                     {"app_id": 2, "rank": 2}]}}
        rows, hidden = parse_app_rows(payload, "day")
        assert len(rows) == 1 and hidden == 0

    def test_rank_gap_is_counted(self):
        payload = {"data": {"day": [{"app_id": 1, "rank": 1},
                                    {"app_id": 2, "rank": 5}]}}
        _, hidden = parse_app_rows(payload, "day")
        assert hidden == 3

    def test_row_without_app_id_fails_loudly(self):
        payload = {"data": {"day": [{"total_tokens": "100"}]}}
        try:
            parse_app_rows(payload, "day")
        except CollectorError as exc:
            assert "app_id" in str(exc)
        else:
            raise AssertionError("缺 app_id 必须失败，不能静默丢")

    def test_string_bigint_is_accepted_as_a_count(self):
        """源侧 total_tokens 是字符串。直接相加会拼字符串，必须走整数守卫。"""
        assert _required_nonnegative_int(
            {"total_tokens": "2468214405615"}, "total_tokens", "x") == 2468214405615

    def test_float_string_is_not_a_count(self):
        try:
            _required_nonnegative_int({"total_tokens": "1.5"}, "total_tokens", "x")
        except CollectorError:
            pass
        else:
            raise AssertionError("不是整数的字符串必须失败，不能悄悄截断")

    def test_data_as_array_is_rejected(self):
        """模型榜的 data 是数组、应用榜的是对象。形状变了要当场炸，不能猜。"""
        try:
            parse_app_rows({"data": [{"app_id": 1}]}, "day")
        except CollectorError as exc:
            assert "data" in str(exc)
        else:
            raise AssertionError("data 不是对象时必须失败")


class TestAppRowBuilding:
    """应用榜是补强维度：它的脏数据不许把整个 OpenRouter 源判失败。"""

    GOOD = {"app_id": 1, "rank": 1, "total_tokens": "100", "total_requests": 5,
            "app": {"title": "A", "slug": "a", "categories": ["cli-agent"]}}

    def test_builds_rows_and_sums_listed_tokens(self):
        rows, listed = build_app_rows(
            [self.GOOD], settled="2026-08-25", coverage_scope="gateway",
            fingerprint="fp", raw_ref=None)
        assert listed == 100
        assert rows[0]["total_tokens"] == 100 and rows[0]["app_title"] == "A"
        assert rows[0]["categories"] == ["cli-agent"]

    def test_missing_total_requests_raises_instead_of_defaulting(self):
        bad = dict(self.GOOD)
        bad.pop("total_requests")
        try:
            build_app_rows([bad], settled="2026-08-25", coverage_scope="gateway",
                           fingerprint="fp", raw_ref=None)
        except CollectorError as exc:
            assert "total_requests" in str(exc)
        else:
            raise AssertionError("缺必需计数字段必须失败，不能补 0")

    def test_app_metadata_of_the_wrong_shape_raises_cleanly(self):
        """`app` 变成字符串时以前会抛 AttributeError，逃出降级路径。"""
        bad = dict(self.GOOD, app="not-an-object")
        try:
            build_app_rows([bad], settled="2026-08-25", coverage_scope="gateway",
                           fingerprint="fp", raw_ref=None)
        except CollectorError as exc:
            assert "app" in str(exc)
        else:
            raise AssertionError("app 不是对象时必须抛 CollectorError")

    def test_bad_app_row_does_not_kill_the_core_token_collection(self):
        """回归保护：脏应用行只该少一个维度，521 行模型观测必须照样落库。"""
        cfg = {"base_url": "https://x", "coverage_scope": "gateway",
               "endpoints": {"rankings": "/r", "models": "/m",
                             "endpoints_of": "/e/{model}", "apps": "/a"},
               "query": {"view": "day", "provider_probe_top_n": 0}}
        rankings = {"data": [{"date": "2026-08-25", "model_permaslug": "a/one",
                              "variant": "standard", "total_prompt_tokens": 100,
                              "total_completion_tokens": 10, "count": 3}]}
        models = {"data": [{"id": "a/one", "canonical_slug": "a/one",
                            "pricing": {"prompt": "0.000001",
                                        "completion": "0.000002"}}]}
        # total_requests 缺失：正是评审里那条最小复现
        apps = {"data": {"day": [{"app_id": 7, "rank": 1,
                                  "total_tokens": "100"}]}}

        def fake_request_json(url, **kwargs):
            return apps if url.endswith("/a") else (
                rankings if url.endswith("/r") else models)

        original_req = openrouter_collector.request_json
        original_save = openrouter_collector.save_raw
        try:
            openrouter_collector.request_json = fake_request_json
            openrouter_collector.save_raw = lambda *a, **k: "raw/fake.json"
            result = openrouter_collector.collect(cfg, None, "2026-08-25")
        finally:
            openrouter_collector.request_json = original_req
            openrouter_collector.save_raw = original_save

        assert len(result.tokens) == 1          # 核心观测没被拖下水
        assert result.apps == []                # 半截行也不许落库
        assert any("应用榜异常" in n for n in result.notes)
        assert any("total_requests" in n for n in result.notes)


class TestMalformedRows:
    """单行脏数据不该判死整天，但"有量却无标识"必须失败。

    实测线上每天有 1 行 model_permaslug 为空、prompt/completion/requests 全是 0
    的占位行（token 占比 0.0000%）。为它把 530 行全废掉，代价远大于收益。
    """

    @staticmethod
    def _cfg():
        return {
            "base_url": "https://example.invalid",
            "endpoints": {"rankings": "/r", "models": "/m",
                          "endpoints_of": "/m/{model}/e"},
            "query": {"view": "day", "provider_probe_top_n": 0},
            "coverage_scope": "gateway", "price_basis": "list",
        }

    @staticmethod
    def _run(rows):
        import collectors.openrouter as O
        pricing = {"data": [{"id": "a/one", "canonical_slug": "a/one",
                             "pricing": {"prompt": "0.000001",
                                         "completion": "0.000002"}}]}
        calls = {"n": 0}

        def fake_request(url, **kwargs):
            calls["n"] += 1
            return {"data": rows} if calls["n"] == 1 else pricing

        original_request, original_save = O.request_json, O.save_raw
        O.request_json = fake_request
        O.save_raw = lambda *a, **k: "raw/test.json"
        try:
            return O.collect(TestMalformedRows._cfg(), None, "2026-08-25")
        finally:
            O.request_json, O.save_raw = original_request, original_save

    @staticmethod
    def _row(slug, prompt=10, completion=1, count=2):
        return {"date": "2026-08-24 00:00:00", "model_permaslug": slug,
                "variant": "standard", "total_prompt_tokens": prompt,
                "total_completion_tokens": completion, "count": count}

    def test_zero_volume_placeholder_is_skipped_not_fatal(self):
        result = self._run([self._row("a/one"), self._row("", 0, 0, 0)])
        assert len(result.tokens) == 1
        assert "<empty model_permaslug>" in result.unmapped
        assert any("占位行" in n for n in result.notes)

    def test_unlabelled_row_with_real_volume_fails_loudly(self):
        """有量却无从归属：静默丢会让总量凭空少一块，必须显式失败。"""
        import collectors.openrouter as O
        try:
            self._run([self._row("a/one"), self._row("", 5_000, 100, 3)])
        except O.CollectorError as exc:
            assert "无从归属" in str(exc)
        else:
            raise AssertionError("带量的无标识行必须抛 CollectorError")

    def test_all_rows_unlabelled_fails(self):
        import collectors.openrouter as O
        try:
            self._run([self._row("", 0, 0, 0)])
        except O.CollectorError:
            pass
        else:
            raise AssertionError("一行带标识的都没有，必须失败")


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
