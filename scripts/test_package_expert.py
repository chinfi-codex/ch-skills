from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import package_expert as pe  # noqa: E402

EXPERT = pe.REPO_ROOT / "experts" / "a-stock-earnings-season"


def _load() -> tuple[dict, dict]:
    plugin = json.loads((EXPERT / ".codebuddy-plugin" / "plugin.json").read_text(encoding="utf-8"))
    manifest = pe.yaml.safe_load((EXPERT / "expert.yaml").read_text(encoding="utf-8"))
    return plugin, manifest


class ValidationTests(unittest.TestCase):
    def test_shipped_expert_is_valid(self) -> None:
        plugin, manifest = _load()
        self.assertEqual(pe.validate_plugin(plugin, EXPERT, manifest), [])

    def test_platform_rules_are_enforced(self) -> None:
        plugin, manifest = _load()
        cases = {
            "defaultInitPrompt must equal quickPrompts[0]":
                lambda p: p["defaultInitPrompt"].update(zh="别的开场白"),
            "tags must be exactly 3":
                lambda p: p["tags"].pop(),
            "displayDescription.zh must be 40-50 chars":
                lambda p: p["displayDescription"].update(zh="太短"),
            "categoryId":
                lambda p: p.update(categoryId="99-Unknown"),
            "plugin must equal name":
                lambda p: p.update(plugin="other"),
            "plugin.json skills must list":
                lambda p: p["skills"].pop(),
        }
        for expected, mutate in cases.items():
            broken = copy.deepcopy(plugin)
            mutate(broken)
            errors = pe.validate_plugin(broken, EXPERT, manifest)
            self.assertTrue(any(expected in e for e in errors), (expected, errors))


class ReferenceAuditTests(unittest.TestCase):
    def test_strip_and_audit(self) -> None:
        _, manifest = _load()
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / "a.md").write_text("保留<!-- repo-only -->（用 `a-stock-analyzer`）<!-- /repo-only -->文本",
                                        encoding="utf-8")
            (stage / "b.md").write_text("请先运行 a-stock-daily-market-sense", encoding="utf-8")
            (stage / "c.py").write_text("# owned by a-stock-analyzer\nprint('see a-stock-analyzer')\n",
                                        encoding="utf-8")
            self.assertEqual(pe.strip_repo_only(stage), 1)
            self.assertEqual((stage / "a.md").read_text(encoding="utf-8"), "保留文本")
            problems = pe.audit_references(stage, manifest)
            self.assertTrue(any(p.startswith("b.md:1") for p in problems), problems)
            self.assertTrue(any(p.startswith("c.py:2") for p in problems), problems)  # comment on line 1 ignored
            self.assertFalse(any(p.startswith("a.md") for p in problems), problems)


class FileReferenceAuditTests(unittest.TestCase):
    def test_references_must_resolve_inside_the_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / "agents").mkdir()
            skill = stage / "skills" / "alpha"
            (skill / "scripts").mkdir(parents=True)
            (skill / "references").mkdir()
            (skill / "scripts" / "run.py").write_text("", encoding="utf-8")
            (skill / "references" / "guide.md").write_text(
                "跑 `run.py --x`，见 `references/guide.md`，输出 `reports/out_<P>.json`；"
                "另见 `references/other.md`", encoding="utf-8")
            (stage / "agents" / "a.md").write_text(
                "用 `run.py`；用 `skills/alpha/scripts/run.py --x`", encoding="utf-8")
            problems = pe.audit_file_refs(stage)
            self.assertEqual(len(problems), 2, problems)
            self.assertTrue(any("other.md" in p for p in problems))       # missing in the skill
            self.assertTrue(any(p.startswith("agents/a.md") and "'run.py'" in p for p in problems))


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.result = pe.build(EXPERT, Path(cls.tmp.name), "author@example.org", check_only=False)
        cls.zip = zipfile.ZipFile(cls.result["zip"])
        cls.names = cls.zip.namelist()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.zip.close()
        cls.tmp.cleanup()

    def test_single_top_folder_with_plugin_manifest(self) -> None:
        self.assertTrue(all(n.startswith("a-stock-earnings-season/") for n in self.names))
        plugin = json.loads(self.zip.read("a-stock-earnings-season/.codebuddy-plugin/plugin.json"))
        self.assertEqual(plugin["author"]["email"], "author@example.org")

    def test_skills_are_self_contained(self) -> None:
        for skill in ("a-stock-earnings-forecast", "a-stock-quarterly-report"):
            prefix = f"a-stock-earnings-season/skills/{skill}/scripts/_shared/"
            self.assertIn(prefix + "db_core.py", self.names)
            self.assertIn(prefix + "html_report/themes/default.css", self.names)

    def test_no_dev_artifacts_ship(self) -> None:
        for name in self.names:
            base = name.rsplit("/", 1)[-1]
            self.assertFalse(base.startswith("test_") or base == "conftest.py", name)
            self.assertNotIn("/tests/", name)
            self.assertNotIn("/reports/", name)
            self.assertNotIn("invest-autoresearch", name)
            self.assertNotIn("__pycache__", name)

    def test_repo_only_blocks_and_outside_skills_are_gone(self) -> None:
        for name in self.names:
            if not name.endswith((".md", ".json")):
                continue
            text = self.zip.read(name).decode("utf-8")
            self.assertNotIn("repo-only", text, name)
            for term in ("a-stock-analyzer", "daily-market-sense", "~/.zshrc", "crawler/"):
                self.assertNotIn(term, text, (name, term))

    def test_shared_bundles_ship_only_imported_files(self) -> None:
        shared = sorted(n.split("/scripts/_shared/", 1)[1] for n in self.names
                        if "/a-stock-earnings-forecast/scripts/_shared/" in n)
        self.assertEqual(shared, ["db_core.py", "html_report/assets.py",
                                  "html_report/safe_json.py", "html_report/themes/default.css"])

    def test_skill_frontmatter_has_workbuddy_fields(self) -> None:
        text = self.zip.read(
            "a-stock-earnings-season/skills/cninfo-announcement-search/SKILL.md").decode("utf-8")
        fm, body = pe.split_frontmatter(text)
        for key in ("name", "description", "description_zh", "description_en", "version", "author"):
            self.assertTrue(fm.get(key), key)
        self.assertEqual(fm["name"], "cninfo-announcement-search")
        self.assertTrue(body.lstrip().startswith("# "))


if __name__ == "__main__":
    unittest.main()
