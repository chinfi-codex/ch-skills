from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from html_report.assets import verify_json_asset_bundle, write_json_asset_bundle  # noqa: E402


class JsonAssetBundleTests(unittest.TestCase):
    def test_version_tracks_content_and_manifest_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = write_json_asset_bundle(
                root,
                {"00.json": {"A": [["20260806", 1]]}},
                schema_version="test/v1",
                generated_at="2026-08-06T20:00:00+08:00",
            )
            same = write_json_asset_bundle(
                root,
                {"00.json": {"A": [["20260806", 1]]}},
                schema_version="test/v1",
                generated_at="2026-08-07T20:00:00+08:00",
            )
            changed = write_json_asset_bundle(
                root,
                {"00.json": {"A": [["20260807", 2]]}},
                schema_version="test/v1",
                generated_at="2026-08-07T20:00:00+08:00",
            )
            self.assertEqual(first["asset_version"], same["asset_version"])
            self.assertNotEqual(same["asset_version"], changed["asset_version"])
            self.assertTrue(verify_json_asset_bundle(root)["ok"])

    def test_stale_json_is_pruned_and_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "stale.json").write_text("{}", encoding="utf-8")
            write_json_asset_bundle(
                root,
                {"01.json": {"A": 1}},
                schema_version="test/v1",
            )
            self.assertFalse((root / "stale.json").exists())
            (root / "01.json").write_text(json.dumps({"A": 2}), encoding="utf-8")
            result = verify_json_asset_bundle(root)
            self.assertFalse(result["ok"])
            self.assertIn("sha256:01.json", result["problems"])


if __name__ == "__main__":
    unittest.main()
