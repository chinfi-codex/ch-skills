"""双段 Hermes 安装契约测试；不创建任务、不写真实配置。迁自 AlphaVault system/tools/tests。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import install_hermes as installer  # noqa: E402


class InstallHermesTest(unittest.TestCase):
    def test_dry_run_has_no_side_effects_even_for_live_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HERMES_HOME": tmp}, clear=False), \
                    patch.object(sys, "argv", ["install_hermes.py", "--enable-live"]):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(installer.main(), 0)
                result = json.loads(output.getvalue())
                self.assertTrue(result["dry_run"])
                self.assertEqual(result["delivery"]["deliver"], "weixin")
                self.assertFalse((Path(tmp) / "scripts" / installer.EVALUATION_WRAPPER).exists())
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_evaluator_has_no_tools_and_never_delivers_externally(self):
        home = Path("/tmp/hermes-home")
        job = installer.evaluation_config(home, Path("/usr/bin/python3"), True)
        self.assertEqual(job["workdir"], str(installer.RUNTIME))
        self.assertEqual(job["deliver"], "local")
        self.assertEqual(job["failure_deliver"], "local")
        self.assertEqual(job["skills"], ["ch-proactive-agent"])
        self.assertEqual(job["enabled_toolsets"], [])
        self.assertFalse(job["no_agent"])
        self.assertFalse(job["attach_to_session"])
        self.assertEqual(job["context_from"], [])
        self.assertEqual(job["interpreter"], "/usr/bin/python3")
        self.assertEqual(job["schedule"], "*/30 * * * *")

    def test_delivery_never_starts_in_the_same_minute_as_scan(self):
        home = Path("/tmp/hermes-home")
        scan = installer.evaluation_config(home, Path("/usr/bin/python3"), False)["schedule"].split()[0]
        delivery = installer.delivery_config(home, Path("/usr/bin/python3"), True, False)["schedule"].split()[0]
        scan_minutes = set(range(0, 60, 30)) if scan == "*/30" else {int(m) for m in scan.split(",")}
        self.assertFalse(scan_minutes & {int(m) for m in delivery.split(",")})

    def test_job_prompt_defers_rules_to_skill(self):
        self.assertIn(installer.GOAL_ID, installer.PROMPT)
        self.assertNotIn("27-主动目标协议", installer.PROMPT)
        self.assertLess(len(installer.PROMPT), 200)

    def test_only_deterministic_delivery_can_opt_into_weixin(self):
        home = Path("/tmp/hermes-home")
        job = installer.delivery_config(home, Path("/usr/bin/python3"), True, False)
        self.assertTrue(job["no_agent"])
        self.assertEqual(job["enabled_toolsets"], [])
        self.assertEqual(job["deliver"], "weixin")
        self.assertTrue(job["attach_to_session"])
        self.assertEqual(job["failure_deliver"], "local")
        self.assertEqual(job["schedule"], "5,35 * * * *")
        shadow = installer.delivery_config(home, Path("/usr/bin/python3"), False, False)
        self.assertEqual(shadow["deliver"], "local")
        self.assertFalse(shadow["attach_to_session"])

    def test_wrappers_run_synced_skill_with_fixed_arguments(self):
        home = Path("/tmp/hermes-home")
        evaluation = installer.wrapper_source("evaluation", home)
        delivery = installer.wrapper_source("delivery", home, "a" * 12, "local")
        self.assertIn('"scan"', evaluation)
        self.assertIn('"dispatch"', delivery)
        self.assertIn("a" * 12, delivery)
        self.assertIn(str(home / "skills" / "ch-proactive-agent"), evaluation)
        self.assertIn(str(installer.RUNTIME), evaluation)
        self.assertNotIn("system/tools", evaluation + delivery)
        self.assertNotIn("subprocess", evaluation + delivery)
        self.assertNotIn("send_message", evaluation + delivery)
        self.assertNotIn("hermes send", evaluation + delivery)

    def test_old_python_uses_hermes_managed_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            python = home / "installs" / "x" / "environments" / "y" / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(installer.sys, "version_info", (3, 9)), \
                    patch.dict(os.environ, {}, clear=False), \
                    patch.object(installer.os, "execv", side_effect=RuntimeError("exec-called")) as execv:
                with self.assertRaisesRegex(RuntimeError, "exec-called"):
                    installer.ensure_hermes_python(home)
                snapshot = json.loads(os.environ[installer.SETTINGS_ENV])
            execv.assert_called_once()
            self.assertEqual(snapshot["goals"]["goal_id"], installer.GOAL_ID)

    def test_settings_snapshot_loads_without_yaml(self):
        snapshot = installer.settings_snapshot()
        with patch.dict(os.environ, {installer.SETTINGS_ENV: snapshot}, clear=False), \
                patch.dict(sys.modules, {"yaml": None}):
            settings = installer.load_settings()
        self.assertEqual(settings.goal_id, installer.GOAL_ID)
        self.assertEqual(settings.policy, installer.SETTINGS.policy)

    def test_wrapper_channel_switch_is_limited_to_managed_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "scripts" / installer.DELIVERY_WRAPPER
            local = installer.wrapper_source("delivery", home, "a" * 12, "local")
            live = installer.wrapper_source("delivery", home, "a" * 12, "weixin")
            installer.write_wrapper(path, local)
            installer.write_wrapper(path, live, managed_variants=(local,))
            self.assertEqual(path.read_text(), live)
            path.write_text("operator-owned custom wrapper")
            with self.assertRaises(ValueError):
                installer.write_wrapper(path, local, managed_variants=(live,))
            self.assertEqual(path.read_text(), "operator-owned custom wrapper")

    def test_legacy_vault_wrapper_can_be_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "scripts" / installer.DELIVERY_WRAPPER
            path.parent.mkdir(parents=True)
            path.write_text(installer.legacy_wrapper_source("delivery", home, "a" * 12, "weixin"), encoding="utf-8")
            new = installer.wrapper_source("delivery", home, "a" * 12, "weixin")
            installer.write_wrapper(path, new, managed_variants=installer.managed_variants("delivery", home, "a" * 12))
            self.assertEqual(path.read_text(encoding="utf-8"), new)

    def test_preflight_refuses_before_runtime_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            skill = home / "skills" / "ch-proactive-agent"
            for rel in ("SKILL.md", "scripts/proactive_goal.py", "scripts/proactive_config.py", "config/goals.yaml"):
                (skill / rel).parent.mkdir(parents=True, exist_ok=True)
                (skill / rel).write_text("x", encoding="utf-8")
            with patch.object(installer, "RUNTIME", home / "runtime"):
                with self.assertRaisesRegex(ValueError, "migrate_from_vault"):
                    installer.preflight(Path(sys.executable), home)


if __name__ == "__main__":
    unittest.main()
