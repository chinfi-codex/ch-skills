from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import env_doctor  # noqa: E402


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self.tmp.name, "config.env")
        self.env = patch.dict(os.environ, {"CH_SKILLS_CONFIG": self.cfg}, clear=False)
        self.env.start()
        for key in ("TUSHARE_TOKEN", "ALPHA_DB_BACKEND", "ALPHA_SQLITE_DIR"):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, *argv: str) -> dict:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(env_doctor.main(list(argv)), 0)
        return json.loads(buf.getvalue())

    def test_setup_defaults_to_sqlite_and_keeps_token_private(self) -> None:
        out = self._run("setup", "--tushare-token", "abcdef0123456789abcdef")
        cfg = env_doctor.read_config(self.cfg)
        self.assertEqual(cfg["ALPHA_DB_BACKEND"], "sqlite")
        self.assertEqual(cfg["TUSHARE_TOKEN"], "abcdef0123456789abcdef")
        self.assertEqual(out["values"]["TUSHARE_TOKEN"], "abcd…cdef")
        if os.name == "posix":
            self.assertEqual(os.stat(self.cfg).st_mode & 0o777, 0o600)

    def test_setup_preserves_existing_values(self) -> None:
        self._run("setup", "--tushare-token", "abcdef0123456789abcdef")
        self._run("setup", "--sqlite-dir", "/data/alpha")
        cfg = env_doctor.read_config(self.cfg)
        self.assertEqual(cfg["TUSHARE_TOKEN"], "abcdef0123456789abcdef")
        self.assertEqual(cfg["ALPHA_SQLITE_DIR"], "/data/alpha")

    def test_environment_beats_config_file(self) -> None:
        self._run("setup", "--tushare-token", "from-file-0123456789")
        with patch.dict(os.environ, {"TUSHARE_TOKEN": "from-env"}):
            self.assertEqual(env_doctor.effective("TUSHARE_TOKEN", env_doctor.read_config(self.cfg)),
                             ("from-env", "env"))


class InterpreterTests(unittest.TestCase):
    def test_pip_uses_tsinghua_mirror_by_default(self) -> None:
        cmd = env_doctor.pip_command("/venv/bin/python", ["pandas"])
        self.assertEqual(cmd[:5], ["/venv/bin/python", "-m", "pip", "install", "-i"])
        self.assertEqual(cmd[5], "https://pypi.tuna.tsinghua.edu.cn/simple")

    def test_all_packages_cover_every_skill_import(self) -> None:
        pkgs = env_doctor.all_packages()
        for name in ("tushare", "pandas", "requests", "PyMuPDF", "PyPDF2"):
            self.assertIn(name, pkgs)
        self.assertNotIn("psycopg2-binary", pkgs)
        self.assertIn("psycopg2-binary", env_doctor.all_packages("postgresql"))

    def test_configured_interpreter_wins_over_running_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "python")
            open(fake, "w").close()
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CH_SKILLS_PYTHON", None)
                self.assertEqual(env_doctor.target_python({"CH_SKILLS_PYTHON": fake}), (fake, "config_file"))
                self.assertEqual(env_doctor.target_python({"CH_SKILLS_PYTHON": fake + "x"})[1],
                                 "running_interpreter")

    def test_skill_names_match_packaged_names(self) -> None:
        self.assertIn("cninfo-announcement-search", env_doctor.SKILLS)
        self.assertNotIn("chstock-cninfo-announcement", env_doctor.SKILLS)


class CapabilityTests(unittest.TestCase):
    ok_db = {"ok": True}
    imports = {s: {"missing": []} for s in env_doctor.SKILLS}
    network = {"cninfo": {"ok": True}, "p5w": {"ok": True}}

    def test_forecast_without_token_is_cninfo_only(self) -> None:
        cap = env_doctor.skill_capability("a-stock-earnings-forecast", self.imports, self.ok_db,
                                          {"token_present": False, "endpoints": {}}, self.network)
        self.assertEqual(cap["level"], "cninfo_only")
        self.assertIn("income", cap["tushare_unavailable_features"])

    def test_quarterly_needs_statement_endpoints(self) -> None:
        endpoints = {e: {"status": "ok"} for e in env_doctor.SKILLS["a-stock-quarterly-report"]["endpoints"]}
        endpoints["cashflow"] = {"status": "no_permission"}
        cap = env_doctor.skill_capability("a-stock-quarterly-report", self.imports, self.ok_db,
                                          {"token_present": True, "token_valid": True,
                                           "endpoints": endpoints}, self.network)
        self.assertEqual(cap["level"], "blocked")
        self.assertIn("tushare_required_endpoints:cashflow", cap["blockers"])

    def test_optional_endpoint_gap_is_partial(self) -> None:
        endpoints = {e: {"status": "ok"} for e in env_doctor.SKILLS["a-stock-earnings-forecast"]["endpoints"]}
        endpoints["daily_basic"] = {"status": "no_permission"}
        cap = env_doctor.skill_capability("a-stock-earnings-forecast", self.imports, self.ok_db,
                                          {"token_present": True, "token_valid": True,
                                           "endpoints": endpoints}, self.network)
        self.assertEqual(cap["level"], "partial")
        self.assertEqual(list(cap["tushare_unavailable_features"]), ["daily_basic"])

    def test_error_classification(self) -> None:
        self.assertEqual(env_doctor._classify_error("抱歉，您没有接口(income)访问权限"), "no_permission")
        self.assertEqual(env_doctor._classify_error("您的token不对，请确认。"), "invalid_token")
        self.assertEqual(env_doctor._classify_error("抱歉，您每分钟最多访问该接口50次"), "rate_limited")


if __name__ == "__main__":
    unittest.main()
