#!/usr/bin/env python3
"""Pure-function and CLI tests for theme_group_stats.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent
SCRIPT = SKILL_ROOT / "scripts" / "theme_group_stats.py"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import theme_group_stats as tgs


def _context() -> dict:
    return {
        "money_effect": {
            "theme_grouping_aid": {
                "records": [
                    {
                        "ts_code": "A.SZ",
                        "name": "甲",
                        "amount_100m_yuan": 10,
                        "pct_chg": 10,
                        "ret_5d": 20,
                        "rel_ret_5d": 12,
                        "sustained_volume_days_5": 3,
                    },
                    {
                        "ts_code": "B.SH",
                        "name": "乙",
                        "amount_100m_yuan": 6,
                        "pct_chg": 8,
                        "ret_5d": None,
                        "rel_ret_5d": 4,
                        "sustained_volume_days_5": 1,
                    },
                    {
                        "ts_code": "C.SZ",
                        "name": "丙",
                        "amount_100m_yuan": 4,
                        "pct_chg": 7,
                        "ret_5d": 2,
                        "rel_ret_5d": None,
                        "sustained_volume_days_5": None,
                    },
                    {
                        "ts_code": "D.SH",
                        "name": "丁",
                        "amount_100m_yuan": None,
                        "pct_chg": None,
                        "ret_5d": None,
                        "rel_ret_5d": None,
                        "sustained_volume_days_5": None,
                    },
                ]
            }
        }
    }


def _mapping() -> dict:
    return {
        "themes": [
            {
                "name": "半导体",
                "members": ["A.SZ", "B.SH", "D.SH"],
                "sublines": [
                    {"name": "存储材料", "members": ["B.SH", "D.SH"]},
                    {"name": "HBM材料", "members": []},
                ],
            },
            {
                "name": "航天",
                "members": ["C.SZ"],
                "sublines": [],
            },
        ]
    }


def _expect_validation(context: dict, mapping: dict, expected: str) -> None:
    try:
        tgs.build_theme_group_stats(context, mapping)
        raise AssertionError("expected ValidationError")
    except tgs.ValidationError as exc:
        assert expected in str(exc), str(exc)


def test_aggregation_and_null_handling() -> None:
    result = tgs.build_theme_group_stats(_context(), _mapping())
    assert result["money_pool"] == {"member_count": 4, "total_amount_100m_yuan": 20.0}

    semiconductor = result["themes"][0]
    assert semiconductor["member_count"] == 3
    assert semiconductor["total_amount_100m_yuan"] == 16.0
    assert semiconductor["share_of_money_pool_pct"] == 80.0
    assert semiconductor["share_of_parent_theme_pct"] == 100.0
    assert semiconductor["median_pct_chg"] == 9.0
    assert semiconductor["median_ret_5d"] == 20.0
    assert semiconductor["median_rel_ret_5d"] == 8.0
    assert semiconductor["median_sustained_volume_days_5"] == 2.0
    assert [item["ts_code"] for item in semiconductor["leaders"]] == ["A.SZ", "B.SH", "D.SH"]

    storage = semiconductor["sublines"][0]
    assert storage["member_count"] == 2
    assert storage["total_amount_100m_yuan"] == 6.0
    assert storage["share_of_money_pool_pct"] == 30.0
    assert storage["share_of_parent_theme_pct"] == 37.5
    assert storage["median_pct_chg"] == 8.0
    assert storage["median_ret_5d"] is None
    assert storage["median_rel_ret_5d"] == 4.0
    assert storage["median_sustained_volume_days_5"] == 1.0

    empty_subline = semiconductor["sublines"][1]
    assert empty_subline["member_count"] == 0
    assert empty_subline["total_amount_100m_yuan"] == 0.0
    assert empty_subline["share_of_parent_theme_pct"] == 0.0
    assert empty_subline["median_pct_chg"] is None
    assert empty_subline["leaders"] == []


def test_rejects_unknown_member_with_path() -> None:
    mapping = _mapping()
    mapping["themes"][0]["members"].append("UNKNOWN.SZ")
    _expect_validation(_context(), mapping, "mapping.themes[0].members[3]")
    _expect_validation(_context(), mapping, "不存在的代码: UNKNOWN.SZ")


def test_rejects_subline_outside_parent() -> None:
    mapping = _mapping()
    mapping["themes"][0]["sublines"][0]["members"] = ["C.SZ"]
    _expect_validation(_context(), mapping, "必须是父主题 '半导体' 的子集")
    _expect_validation(_context(), mapping, "越界代码: C.SZ")


def test_rejects_member_reused_across_parent_themes() -> None:
    mapping = _mapping()
    mapping["themes"][1]["members"] = ["A.SZ", "C.SZ"]
    _expect_validation(_context(), mapping, "A.SZ 已归入父主题 '半导体'")
    _expect_validation(_context(), mapping, "不得跨父主题重复归类")


def test_rejects_invalid_names_lists_and_duplicates() -> None:
    bad_name = _mapping()
    bad_name["themes"][0]["name"] = "  "
    _expect_validation(_context(), bad_name, "mapping.themes[0].name 必须是非空字符串")

    bad_list = _mapping()
    bad_list["themes"][0]["sublines"] = None
    _expect_validation(_context(), bad_list, "mapping.themes[0].sublines 必须是 JSON array")

    duplicate = _mapping()
    duplicate["themes"][0]["members"] = ["A.SZ", "A.SZ"]
    _expect_validation(_context(), duplicate, "重复成员: A.SZ")


def test_rejects_invalid_record_values() -> None:
    context = _context()
    context["money_effect"]["theme_grouping_aid"]["records"][0]["pct_chg"] = "not-number"
    _expect_validation(context, _mapping(), "records[0].pct_chg 必须是数值或 null")

    negative = _context()
    negative["money_effect"]["theme_grouping_aid"]["records"][0]["amount_100m_yuan"] = -1
    _expect_validation(negative, _mapping(), "amount_100m_yuan 不能为负数")


def test_zero_amount_denominators_are_zero() -> None:
    context = _context()
    for record in context["money_effect"]["theme_grouping_aid"]["records"]:
        record["amount_100m_yuan"] = None
    result = tgs.build_theme_group_stats(context, _mapping())
    assert result["money_pool"]["total_amount_100m_yuan"] == 0.0
    assert result["themes"][0]["share_of_money_pool_pct"] == 0.0
    assert result["themes"][0]["share_of_parent_theme_pct"] == 0.0
    assert result["themes"][0]["sublines"][0]["share_of_parent_theme_pct"] == 0.0


def test_star_field_never_affects_deterministic_stats() -> None:
    provisional = _mapping()
    locked = _mapping()
    for theme in provisional["themes"]:
        theme["stars"] = None
    for index, theme in enumerate(locked["themes"]):
        theme["stars"] = 3 - index
    assert tgs.build_theme_group_stats(_context(), provisional) == tgs.build_theme_group_stats(
        _context(), locked
    )


def test_cli_writes_output() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        context_path = root / "module3_money_effect.json"
        mapping_path = root / "module3_theme_map.json"
        output_path = root / "nested" / "module3_theme_stats.json"
        context_path.write_text(json.dumps(_context(), ensure_ascii=False), encoding="utf-8")
        mapping_path.write_text(json.dumps(_mapping(), ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--context",
                str(context_path),
                "--mapping",
                str(mapping_path),
                "--output",
                str(output_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert "已写入主题确定性统计" in proc.stdout
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        assert payload["themes"][0]["name"] == "半导体"
        serialized = output_path.read_text(encoding="utf-8")
        assert "产业逻辑" not in serialized and "盘面确认" not in serialized


def main() -> int:
    tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = []
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - small standalone test runner
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
