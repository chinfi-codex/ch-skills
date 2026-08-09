#!/usr/bin/env python3
"""极简测试跑法：本机没装 pytest，用它跑 test_*.py 里的 test_* 函数。

    python3 tests/run_tests.py            # 跑全部
    python3 tests/run_tests.py trend      # 只跑文件名含 trend 的
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(p for p in ROOT.glob("test_*.py") if pattern in p.name)
    passed, failed = 0, []
    for path in files:
        try:
            module = load(path)
        except Exception:
            failed.append((path.name, "<import>", traceback.format_exc()))
            continue
        for name in dir(module):
            if not name.startswith("test_"):
                continue
            fn = getattr(module, name)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
            except Exception:
                failed.append((path.name, name, traceback.format_exc()))
    for file_name, test_name, tb in failed:
        print(f"\n=== FAIL {file_name}::{test_name} ===\n{tb}")
    print(f"\n{passed} passed, {len(failed)} failed  ({len(files)} files)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
