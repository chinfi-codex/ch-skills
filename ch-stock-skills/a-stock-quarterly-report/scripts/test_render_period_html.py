from __future__ import annotations

import json
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location("qreport_render_period_html", SCRIPT_DIR / "render_period_html.py")
assert SPEC and SPEC.loader
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


def _view() -> dict:
    return {
        "period": "20260630",
        "period_label": "2026 半年报",
        "updated_at": "2026-08-08",
        "ann_cutoff": "20260809",
        "ann_cutoff_stock_count": 0,
        "disclosure_progress_pct": None,
        "theme_registry_empty": False,
        "trend_net": 2,
        "pe_buckets": [],
        "industries": [],
        "stocks": [],
        "theme_trends": {},
        "klines": {"688018.SH": [["20260807", 110, 113, 109, 112.06, 1000]]},
    }


class LazyKlineAssetTests(unittest.TestCase):
    def test_page_uses_versioned_revalidated_kline_request(self) -> None:
        html = renderer.render_html(_view())
        self.assertIn('"kline_asset_version": "preview"', html)
        self.assertIn("url.searchParams.set('v',DATA.kline_asset_version)", html)
        self.assertIn("fetch(url,{cache:'no-cache'})", html)
        self.assertNotIn("force-cache", html)

    def test_manifest_records_content_version_and_latest_trade_date(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "qreport_20260630.html"
            assets = renderer.write_kline_assets(
                str(out),
                _view()["klines"],
                shard_count=4,
                generated_at="2026-08-08T08:00:00+08:00",
            )
            manifest = json.loads(
                (Path(assets["asset_dir"]) / "_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["schema_version"], "qreport-kline-shards/v2")
            self.assertEqual(manifest["asset_version"], assets["asset_version"])
            self.assertEqual(manifest["latest_trade_date"], "20260807")
            self.assertEqual(manifest["file_count"], 1)


if __name__ == "__main__":
    unittest.main()
