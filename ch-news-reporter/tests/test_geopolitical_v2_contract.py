from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from framework_loader import load_framework  # noqa: E402
from prepare_report_data import emit_markdown  # noqa: E402
from profile_config import load_profile, render_config  # noqa: E402
from render_report_html import (  # noqa: E402
    PATH_PROB_JS,
    configured_probability_labels,
    load_probabilities,
)
from save_report_state import validate  # noqa: E402


V2_PATHS = ["W1", "W2", "W3", "W4", "De"]


class _WatchboardPath:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def exists(self) -> bool:
        return True

    def read_text(self, encoding: str = "utf-8") -> str:
        return json.dumps(self.payload, ensure_ascii=False)


def _item(item_id: str, status: str = "open", **extra) -> dict:
    item = {
        "id": item_id,
        "opened": "2026-08-01",
        "statement": item_id,
        "status": status,
        "expires_after": "2026-08-20",
    }
    item.update(extra)
    return item


def _payload(items: list[dict]) -> dict:
    return {
        "as_of": "2026-08-08",
        "regime": "test",
        "tracking_items": items,
        "next_nodes": [],
        "falsifiers": ["test falsifier"],
    }


class GeopoliticalV2ContractTests(unittest.TestCase):
    def test_framework_profile_and_probability_chart_use_the_same_paths(self) -> None:
        framework = load_framework("geopolitical_daily")
        self.assertIsNotNone(framework)
        self.assertEqual(framework["framework_version"], "geopolitics-v2-wartime")
        self.assertEqual(framework["frame_schema"]["path"]["enum"], V2_PATHS)

        profile = load_profile(
            "geopolitical_daily",
            config_path=ROOT / "config" / "report_profiles.yaml",
        )
        labels = configured_probability_labels(render_config(profile))
        self.assertEqual([key for key, _label in labels], V2_PATHS)

        watchboard = _WatchboardPath({
            "frame": {
                "path": "W2",
                "probabilities": {"W1": 30, "W2": 50, "W3": 13, "W4": 2, "De": 5},
            }
        })
        chart = load_probabilities(watchboard, labels)
        self.assertIsNotNone(chart)
        self.assertEqual(chart["path"], "W2")
        self.assertEqual(list(chart["probabilities"]), V2_PATHS)
        self.assertNotIn("charAt(0)", PATH_PROB_JS)

    def test_active_instructions_do_not_reintroduce_v1_paths(self) -> None:
        active_docs = [
            ROOT / "SKILL.md",
            ROOT / "config" / "report_profiles.yaml",
            ROOT / "references" / "reports" / "geopolitical_daily" / "template.md",
            ROOT / "references" / "reports" / "geopolitical_daily" / "template_brief.md",
            ROOT / "references" / "reports" / "geopolitical_daily" / "cross_asset_impact_framework.md",
        ]
        legacy = ("A 局部缓和", "B 可控摩擦", "C 扩散升级", "D 系统冲击", "A/B/C/D")
        for path in active_docs:
            text = path.read_text(encoding="utf-8")
            for token in legacy:
                with self.subTest(path=path.name, token=token):
                    self.assertNotIn(token, text)


class MotherTopicIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = {"title": "test", "state_enabled": True}
        self.prior = {
            "state_date_key": "2026-08-07",
            "watchboard": {"tracking_items": [_item("P", sub_items=[_item("C")])]},
        }

    def _errors(self, items: list[dict], prior=None) -> list[str]:
        errors, _warnings = validate(
            _payload(items),
            self.profile,
            self.prior if prior is None else prior,
            date_key="2026-08-08",
        )
        return errors

    def test_flattened_child_without_promotion_is_rejected(self) -> None:
        errors = self._errors([_item("P"), _item("C")])
        self.assertTrue(any("母题被拆平" in error for error in errors))

    def test_promotion_must_name_the_exact_parent(self) -> None:
        errors = self._errors([
            _item("P"),
            _item("C", promoted_from="WRONG", promote_reason="独立升级"),
        ])
        self.assertTrue(any("与上一期原母题" in error for error in errors))

    def test_promotion_requires_a_reason(self) -> None:
        errors = self._errors([_item("P"), _item("C", promoted_from="P")])
        self.assertTrue(any("promote_reason" in error for error in errors))

    def test_valid_promotion_passes(self) -> None:
        errors = self._errors([
            _item("P"),
            _item("C", promoted_from="P", promote_reason="子线已形成独立战线"),
        ])
        self.assertEqual(errors, [])

    def test_settled_parent_cannot_hide_an_open_child(self) -> None:
        parent = _item("P", "confirmed", resolution="parent settled", sub_items=[_item("C")])
        errors, _warnings = validate(
            _payload([parent]), self.profile, prior=None, date_key="2026-08-08"
        )
        self.assertTrue(any("子项 C 仍 open" in error for error in errors))

    def test_projection_exposes_historical_open_child_under_settled_parent(self) -> None:
        prior = {
            "state_date_key": "2026-08-07",
            "watchboard": {
                "regime": "test",
                "tracking_items": [
                    _item("P", "confirmed", resolution="old malformed row", sub_items=[_item("C")])
                ],
            },
        }
        packet = {
            "profile": "geopolitical_daily",
            "date_key": "2026-08-08",
            "generated_at": "2026-08-08T00:00:00",
            "sources": [],
            "items_count": 0,
            "enrichment_candidates": [],
            "state_enabled": True,
            "prior_state": prior,
            "items": [],
        }
        output = io.StringIO()
        with redirect_stdout(output):
            emit_markdown(packet)
        rendered = output.getvalue()
        self.assertIn("C (opened 2026-08-01，子项 of P)", rendered)
        self.assertIn("P (opened 2026-08-01, status confirmed)", rendered)


if __name__ == "__main__":
    unittest.main()
