"""运行态迁移与项目配置测试；只在临时目录中构造金库与运行态。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import migrate_from_vault as migrate  # noqa: E402
import proactive_config  # noqa: E402
import proactive_goal as pg  # noqa: E402

LEGACY_PACKET = "ledgers/runs/proactive/packets/PG-0123456789abcdef.json"


def legacy_state() -> dict:
    state = pg.default_state(pg.parse_time("2026-10-27T10:00:00+08:00"))
    state["initialized"] = True
    state["framework_versions"] = {
        "system/frameworks/27-主动目标协议.md": "a" * 64,
        "wiki/04-交易方法论/仓位体系.md": "b" * 64,
    }
    state["candidates"]["PG-0123456789abcdef"] = {
        "evidence_hash": "c" * 64, "kind": "catalyst", "native_id": "CE-1", "attempts": 0,
        "last_action": "defer", "review_after": "2026-10-28T08:00:00+08:00", "packet_path": LEGACY_PACKET,
    }
    state["candidates"]["PG-fedcba9876543210"] = {
        "evidence_hash": "d" * 64, "kind": "catalyst", "native_id": "CE-2", "attempts": 0,
        "last_action": "baseline", "review_after": None,
    }
    return state


class MigrateFromVaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.vault = base / "vault"
        self.runtime = base / "runtime"
        (self.vault / LEGACY_PACKET).parent.mkdir(parents=True)
        (self.vault / LEGACY_PACKET).write_text(json.dumps({"candidate_id": "PG-0123456789abcdef"}), encoding="utf-8")
        (self.vault / migrate.LEGACY_RUNS_REL / "decisions.jsonl").write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
        self.state_path = self.vault / migrate.LEGACY_STATE_REL
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text(json.dumps(legacy_state(), ensure_ascii=False), encoding="utf-8")
        self.env = patch.dict(os.environ, {"HERMES_HOME": str(base / "hermes")}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_dry_run_reports_without_writing(self):
        result = migrate.plan(self.vault, self.runtime)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["packets"], 1)
        self.assertEqual(result["packets_missing"], [])
        self.assertEqual(result["audit_rows"], 2)
        self.assertFalse(self.runtime.exists())

    def test_apply_rewrites_paths_and_keeps_vault_originals(self):
        before = self.state_path.read_bytes()
        receipt = migrate.apply(self.vault, self.runtime)
        state = json.loads((self.runtime / "state.json").read_text(encoding="utf-8"))
        entry = state["candidates"]["PG-0123456789abcdef"]
        self.assertEqual(entry["packet_path"], "packets/PG-0123456789abcdef.json")
        self.assertTrue((self.runtime / entry["packet_path"]).is_file())
        self.assertNotIn("system/frameworks/27-主动目标协议.md", state["framework_versions"])
        self.assertIn("wiki/04-交易方法论/仓位体系.md", state["framework_versions"])
        self.assertEqual((self.runtime / "decisions.jsonl").read_text(encoding="utf-8").count("\n"), 2)
        self.assertEqual(receipt["packet_paths_rewritten"], 1)
        self.assertEqual(receipt["framework_versions_retired"], 1)
        self.assertTrue((self.runtime / "migration.json").is_file())
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertTrue((self.vault / LEGACY_PACKET).is_file())

    def test_migrated_state_satisfies_project_schema(self):
        migrate.apply(self.vault, self.runtime)
        with patch.object(pg, "STATE", self.runtime / "state.json"):
            state = pg.load_state()
        self.assertEqual(state["goal_id"], pg.GOAL_ID)

    def test_apply_refuses_to_overwrite_existing_runtime(self):
        migrate.apply(self.vault, self.runtime)
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            migrate.apply(self.vault, self.runtime)

    def test_unknown_packet_path_is_rejected(self):
        state = legacy_state()
        state["candidates"]["PG-0123456789abcdef"]["packet_path"] = "../outside.json"
        with self.assertRaisesRegex(ValueError, "无法识别"):
            migrate.transform(state)


class ProjectConfigTest(unittest.TestCase):
    def test_environment_overrides_configured_paths(self):
        with patch.dict(os.environ, {"ALPHAVAULT_ROOT": "/tmp/v", "PROACTIVE_HOME": "/tmp/r"}, clear=False):
            settings = proactive_config.load_settings()
        self.assertEqual(settings.vault_root, Path("/tmp/v"))
        self.assertEqual(settings.runtime_home, Path("/tmp/r"))

    def test_protocol_is_no_longer_watched_as_vault_methodology(self):
        settings = proactive_config.load_settings()
        self.assertNotIn("system/frameworks/27-主动目标协议.md", settings.framework_files)
        self.assertEqual(set(migrate.RETIRED_FRAMEWORK_FILES) & set(settings.framework_files), set())

    def test_every_candidate_kind_is_registered_as_a_source(self):
        settings = proactive_config.load_settings()
        kinds = {"trend_state", "catalyst", "framework_review", "framework_change", "scheduled_review"}
        self.assertEqual(set(settings.sources), kinds)
        state_schema = json.loads(pg.SCHEMA.read_text(encoding="utf-8"))
        schema_kinds = set(state_schema["properties"]["candidates"]["additionalProperties"]["properties"]["kind"]["enum"])
        self.assertEqual(schema_kinds, kinds)

    def test_policy_defaults_feed_new_state(self):
        state = pg.default_state(pg.parse_time("2026-10-27T10:00:00+08:00"))
        self.assertEqual(state["policy"]["max_notifications_per_day"], 4)
        self.assertEqual(state["policy"]["scheduled_reviews"], ["08:35", "15:40"])

    def test_decision_schema_matches_skill_contract(self):
        schema = json.loads((SKILL_DIR / "config/schemas/decision.schema.json").read_text(encoding="utf-8"))
        good = {"decisions": [{
            "candidate_id": "PG-0123456789abcdef", "evidence_hash": "a" * 64, "action": "notify",
            "priority": "P1", "topic_key": "trend:axis", "reason": "趋势档位迁移",
            "review_after": None, "message": "【P1 交易】退潮加深", "missing_inputs": [], "falsifier": "融资转正",
        }]}
        jsonschema.validate(good, schema)
        bad = json.loads(json.dumps(good))
        bad["decisions"][0]["message"] = None
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        for field in schema["properties"]["decisions"]["items"]["properties"]:
            self.assertIn(f'"{field}"', skill)


if __name__ == "__main__":
    unittest.main()
