from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = "import json, db_core; print(json.dumps(db_core.config_snapshot()))"


def _snapshot(config_text: str | None, **env: str) -> dict:
    """db_core reads its config at import time, so probe it in a fresh interpreter."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.env"
        if config_text is not None:
            cfg.write_text(config_text, encoding="utf-8")
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith("ALPHA_") and k != "DATABASE_URL"}
        clean.update({"CH_SKILLS_CONFIG": str(cfg), **env})
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=HERE, env=clean,
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)


class UserConfigTests(unittest.TestCase):
    def test_no_config_keeps_postgresql_default(self) -> None:
        self.assertEqual(_snapshot(None)["backend"], "postgresql")

    def test_config_file_selects_sqlite(self) -> None:
        snap = _snapshot("ALPHA_DB_BACKEND=sqlite\nALPHA_SQLITE_DIR=/data/alpha\n")
        self.assertEqual((snap["backend"], snap["sqlite_dir"]), ("sqlite", "/data/alpha"))

    def test_environment_overrides_config_file(self) -> None:
        snap = _snapshot("ALPHA_DB_BACKEND=sqlite\n", ALPHA_DB_BACKEND="postgresql")
        self.assertEqual(snap["backend"], "postgresql")

    def test_non_db_keys_are_not_exported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.env"
            cfg.write_text("TUSHARE_TOKEN=secret\nALPHA_DB_BACKEND=sqlite\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "TUSHARE_TOKEN"}
            env["CH_SKILLS_CONFIG"] = str(cfg)
            out = subprocess.run(
                [sys.executable, "-c", "import os, db_core; print(os.getenv('TUSHARE_TOKEN', ''))"],
                cwd=HERE, env=env, capture_output=True, text=True, check=True)
            self.assertEqual(out.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
