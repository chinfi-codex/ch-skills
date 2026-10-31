"""项目配置与契约测试：配置可被环境变量覆盖、数据源登记完整、输出契约与 SKILL.md 一致。"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

from helpers import NOW, SCRIPTS, parse_time
from proactive import store
from proactive.context import CTX, KINDS

import proactive_config

SKILL_DIR = SCRIPTS.parent


class ProjectConfigTest(unittest.TestCase):
    def test_environment_overrides_configured_paths(self):
        with patch.dict(os.environ, {"ALPHAVAULT_ROOT": "/tmp/v", "PROACTIVE_HOME": "/tmp/r"}, clear=False):
            settings = proactive_config.load_settings()
        self.assertEqual(settings.vault_root, Path("/tmp/v"))
        self.assertEqual(settings.runtime_home, Path("/tmp/r"))

    def test_protocol_is_not_watched_as_vault_methodology(self):
        self.assertNotIn("system/frameworks/27-主动目标协议.md", proactive_config.load_settings().framework_files)

    def test_every_candidate_kind_is_registered_with_cooldown(self):
        settings = proactive_config.load_settings()
        self.assertEqual(set(settings.sources), set(KINDS))
        for kind, row in settings.sources.items():
            self.assertGreater(float(row.get("cooldown_hours") or 0), 0, kind)
        state_schema = json.loads(CTX.schema.read_text(encoding="utf-8"))
        schema_kinds = set(state_schema["properties"]["candidates"]["additionalProperties"]["properties"]["kind"]["enum"])
        self.assertEqual(schema_kinds, set(KINDS))

    def test_policy_defaults_feed_new_state(self):
        state = store.default_state(parse_time(NOW))
        self.assertEqual(state["policy"]["max_notifications_per_day"], 4)
        self.assertEqual(state["policy"]["evaluation_lease_minutes"], 30)
        self.assertEqual(state["policy"]["scheduled_reviews"], ["08:35", "15:40"])

    def test_decision_schema_matches_skill_contract(self):
        schema = json.loads((SKILL_DIR / "config/schemas/decision.schema.json").read_text(encoding="utf-8"))
        good = {"decisions": [{
            "candidate_id": "PG-0123456789abcdef", "evidence_hash": "a" * 64, "action": "needs_user",
            "priority": "P3", "reason": "体检到期", "review_after": None, "message": None,
            "brief": {"headline": "框架该体检了", "what_changed": "版本停在 08-16。", "so_what": "安排体检。",
                      "falsifier": "已体检。", "source": "框架约定", "ask": "1 安排；2 推迟"},
            "missing_inputs": [], "falsifier": "已体检"}]}
        jsonschema.validate(good, schema)
        no_ask = json.loads(json.dumps(good))
        del no_ask["decisions"][0]["brief"]["ask"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(no_ask, schema)
        empty = json.loads(json.dumps(good))
        empty["decisions"][0]["brief"] = None
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(empty, schema)
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        for field in schema["properties"]["decisions"]["items"]["properties"]:
            self.assertIn(f'"{field}"', skill)
        for field in schema["properties"]["decisions"]["items"]["properties"]["brief"]["anyOf"][1]["properties"]:
            self.assertIn(f"`{field}`", skill)


if __name__ == "__main__":
    unittest.main()
