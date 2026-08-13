#!/usr/bin/env python3
"""Offline regression tests for the deterministic vault and macro extractors."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import macro_snapshot as macro
import vault_delta as vault


def test_conclusion_rows_keep_the_source_text() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        page = root / "wiki" / "03-产业链图谱" / "AI基建-存储.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            "# AI基建-存储\n\n"
            "## 可产出结论\n"
            "- **C-存储-01 ｜ HBM 供给约束**（🟢 高置信｜已上修｜2026-08-07，GS）："
            "2027 年供给仍紧，合约价格锚上移。\n\n"
            "## 其他章节\n正文\n",
            encoding="utf-8",
        )

        result = vault.extract_conclusions(root, "2026-08-01", "2026-08-10")

        assert result["count"] == 1
        row = result["rows"][0]
        assert row["chain"] == "AI基建-存储"
        assert "2027 年供给仍紧" in row["text"]
        assert row["chars"] == len(row["text"])


def test_vault_default_window_starts_on_monday() -> None:
    args = argparse.Namespace(until="2026-08-13", since=None, days=None, week=False)
    assert vault.resolve_window(args) == ("2026-08-10", "2026-08-13")


def test_macro_default_window_starts_on_monday() -> None:
    args = argparse.Namespace(end="2026-08-13", start=None, days=None)
    assert macro.resolve_window(args) == ("2026-08-10", "2026-08-13")


def test_explicit_start_must_not_exceed_end() -> None:
    args = argparse.Namespace(end="2026-08-10", start="2026-08-11", days=None)
    try:
        macro.resolve_window(args)
    except ValueError:
        pass
    else:
        raise AssertionError("reversed macro window must fail")


def run() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
