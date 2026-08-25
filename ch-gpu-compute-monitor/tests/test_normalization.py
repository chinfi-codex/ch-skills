"""标准化与可比性守卫的回归测试。

这些测试盯的是本项目最容易出、又最难在成品里看出来的四类错：
单卡换算、样本量门槛、源集合变动造成的假变化率、别名误配。
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from collectors.base import percentile, query_fingerprint  # noqa: E402
from collectors.runpod import stock_rank  # noqa: E402
from collectors.vast import _keep, _unit_price  # noqa: E402
from gpu_catalog import load_catalog  # noqa: E402


def approx(expected: float, tol: float = 1e-6):
    """本地实现的浮点近似比较，免得测试硬依赖 pytest（仓库环境里未必装了）。"""
    class _Approx:
        def __eq__(self, other):
            return other is not None and abs(float(other) - expected) <= tol

        def __repr__(self):
            return f"approx({expected})"

    return _Approx()


class TestUnitConversion:
    def test_offer_price_is_divided_by_gpu_count(self):
        """8 卡机报 40 美元，单卡价必须是 5 —— 不除就整整差 8 倍。"""
        offer = {"num_gpus": 8, "search": {"gpuCostPerHour": 40.0}}
        assert _unit_price(offer, "gpu_only") == approx(5.0)

    def test_gpu_only_excludes_disk_and_sla(self):
        offer = {"num_gpus": 1, "dph_total": 5.32,
                 "search": {"gpuCostPerHour": 5.3125, "totalHour": 5.3146}}
        assert _unit_price(offer, "gpu_only") == approx(5.3125)
        assert _unit_price(offer, "bundled") == approx(5.3146)

    def test_missing_gpu_count_returns_none_not_a_guess(self):
        assert _unit_price({"search": {"gpuCostPerHour": 40.0}}, "gpu_only") is None
        assert _unit_price({"num_gpus": 0, "dph_base": 4.0}, "gpu_only") is None

    def test_zero_or_negative_price_is_rejected(self):
        """价格 0 通常是解析失败，不是免费——绝不能入库。"""
        assert _unit_price({"num_gpus": 1, "search": {"gpuCostPerHour": 0}}, "gpu_only") is None


class TestQualityFilter:
    def test_deverified_machine_is_dropped(self):
        offer = {"verification": "deverified", "reliability2": 0.99}
        assert _keep(offer, {"exclude_deverified": True, "min_reliability": 0.9}) is False

    def test_low_reliability_is_dropped(self):
        offer = {"verification": "verified", "reliability2": 0.5}
        assert _keep(offer, {"exclude_deverified": True, "min_reliability": 0.9}) is False

    def test_missing_reliability_is_dropped_not_assumed_good(self):
        assert _keep({"verification": "verified"}, {"min_reliability": 0.9}) is False


class TestPercentile:
    def test_empty_returns_none_never_zero(self):
        assert percentile([], 0.5) is None

    def test_interpolates(self):
        assert percentile([1, 2, 3, 4], 0.25) == approx(1.75)

    def test_single_sample(self):
        assert percentile([2.5], 0.75) == approx(2.5)


class TestCatalog:
    def test_sku_level_ids_do_not_collapse_h100_variants(self):
        """H100 SXM / NVL / PCIe 同日实测差 35%，合并会污染价格中枢。"""
        catalog = load_catalog()
        ids = {catalog.resolve("runpod", raw) for raw in
               ("NVIDIA H100 80GB HBM3", "NVIDIA H100 NVL", "NVIDIA H100 PCIe")}
        assert ids == {"H100 SXM", "H100 NVL", "H100 PCIe"}

    def test_unknown_alias_returns_none_rather_than_fuzzy_match(self):
        catalog = load_catalog()
        assert catalog.resolve("runpod", "NVIDIA H100") is None
        assert catalog.resolve("runpod", "H100 SXM") is None  # 源不对也不该命中

    def test_source_aliases_keep_platform_casing(self):
        catalog = load_catalog()
        assert "NVIDIA H100 80GB HBM3" in catalog.source_aliases("runpod")
        assert catalog.source_aliases("vast", primary_only=True) == [
            "H100 SXM", "H200", "B200"]


class TestStockRank:
    def test_ordering(self):
        assert stock_rank("None") < stock_rank("Low") < stock_rank("Medium") < stock_rank("High")

    def test_unknown_status_is_none_not_zero(self):
        """未知档位当成 0（=None 无货）会把采集异常读成断货。"""
        assert stock_rank("Unknown") is None
        assert stock_rank(None) is None


class TestQueryFingerprint:
    def test_same_query_same_fingerprint_regardless_of_key_order(self):
        a = query_fingerprint({"limit": 100, "rentable": True})
        b = query_fingerprint({"rentable": True, "limit": 100})
        assert a == b

    def test_changed_query_changes_fingerprint(self):
        """口径变了指纹必须变，否则不可比的两段会被当成一条序列。"""
        assert query_fingerprint({"limit": 100}) != query_fingerprint({"limit": 500})


def _run() -> int:
    """无 pytest 时的兜底跑法：`python3 tests/test_normalization.py`。"""
    failures = []
    total = 0
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
