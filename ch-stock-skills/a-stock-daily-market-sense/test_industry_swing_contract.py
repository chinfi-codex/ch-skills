"""产业趋势波段(2.x) 契约路径的回归覆盖。

锁定的失败模式（都是 2.x 开发中真实发生过的）：

- **注册表漂移**：outputs.yaml 与 INDUSTRY_SWING_CONTRACT 在两处声明同一份章节集，
  只改一侧在本地全绿，直到某份报告撞上另一侧。yaml 侧刻意 required:false
  （同一 glob 服务两代报告），必选性由契约侧硬判——所以守护断言的是键/层级对齐，
  不是 requiredness。
- **指纹回退**：2.x 报告意外丢掉全部三个指纹标题时会静默回退 legacy 契约，
  再报一墙 legacy 章节错误。测试钉死指纹键。
- **缺章必须点名**：2.x 章节是必选 spec，丢掉仓位管理备忘要被点名，不许放行。

跑法：python3 tests/run_tests.py（本文件在技能根目录，会被自动发现）
"""
from __future__ import annotations

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent
SHARED = SKILL_ROOT.parents[1] / "shared"
sys.path.insert(0, str(SHARED))
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import yaml

from dms_output_contract import (
    INDUSTRY_SWING_CONTRACT,
    ContractError,
    select_contract,
    validate_dms_content,
)
from html_report.contract import SectionContract, SectionSpec


def _fake_legacy_contract() -> SectionContract:
    return SectionContract(
        version="dms/1.5.0",
        sections=[SectionSpec("sentiment_trend", [r"^情绪趋势$"], level=3)],
        order="strict",
    )


SWING_MARKDOWN = """# 报告

## 一句话盘面判断

==测试判断句。==

# 1. 环境与仓位总闸门

闸门正文。

# 2. 大盘温度与风格

温度正文。

# 3. 产业趋势主线总览

主线正文。

# 4. 主线内关注个股（多维筛选 · 并集）

并集正文。

# 5. 亏钱效应（爆量下跌）

亏钱正文。

# 6. 仓位管理备忘

备忘正文。
"""

# 命中指纹三键之一即可选型——这是 select_contract 的合同。
FINGERPRINT_KEYS = ("环境与仓位总闸门", "仓位管理备忘", "产业趋势主线总览")

EXPECTED_SECTIONS = [
    ("hero_verdict", 3),
    ("pos_gate", 2),
    ("temp_macro", 2),
    ("industry_mainline", 2),
    ("mainline_screening", 2),
    ("m4_decline", 2),
    ("position_memo", 2),
]


def _minimal_evidence() -> dict:
    # 前瞻轴/机判卡全部不可用 → 轴类校验跳过，聚焦章节契约本身。
    return {
        "metadata": {},
        "forward_odds": {"available": False, "pulse": {"available": False}},
    }


def test_fingerprint_selects_swing():
    legacy = _fake_legacy_contract()
    for key in FINGERPRINT_KEYS:
        assert select_contract(f"# 章节含 {key}", legacy) is INDUSTRY_SWING_CONTRACT, key


def test_legacy_text_keeps_legacy():
    legacy = _fake_legacy_contract()
    assert select_contract("## 1.2 情绪趋势\n正文", legacy) is legacy


def test_section_keys_and_levels():
    got = [(spec.key, spec.level) for spec in INDUSTRY_SWING_CONTRACT.sections]
    assert got == EXPECTED_SECTIONS, got


def test_all_sections_required():
    optional = [spec.key for spec in INDUSTRY_SWING_CONTRACT.sections if not spec.required]
    assert optional == [], f"2.x 章节必选性由本契约硬判，不应有可选节: {optional}"


def test_outputs_yaml_sections_match_contract():
    """yaml 的 markdown level + 1 == 契约的渲染 level；键序列必须一致。"""
    outputs = yaml.safe_load((SKILL_ROOT / "outputs.yaml").read_text(encoding="utf-8"))
    sections = outputs["outputs"]["dms-markdown"]["contract"]["sections"]
    yaml_keys = [item["key"] for item in sections]
    contract_keys = [spec.key for spec in INDUSTRY_SWING_CONTRACT.sections]
    assert yaml_keys == contract_keys, f"章节键集合漂移: {yaml_keys} vs {contract_keys}"
    for item in sections:
        spec = next(s for s in INDUSTRY_SWING_CONTRACT.sections if s.key == item["key"])
        assert item["level"] + 1 == spec.level, (
            f"[{item['key']}] 层级漂移: yaml {item['level']} 对应契约 {spec.level}"
        )


def test_full_skeleton_passes():
    audit = validate_dms_content(SWING_MARKDOWN, _minimal_evidence(), INDUSTRY_SWING_CONTRACT)
    assert audit["status"] == "ok", audit
    assert audit["contract_version"] == INDUSTRY_SWING_CONTRACT.version


def test_missing_chapter_is_named():
    broken = SWING_MARKDOWN.replace("# 6. 仓位管理备忘\n\n备忘正文。\n", "")
    try:
        validate_dms_content(broken, _minimal_evidence(), INDUSTRY_SWING_CONTRACT)
    except ContractError as exc:
        assert "position_memo" in str(exc), str(exc)
    else:
        raise AssertionError("缺 position_memo 章节居然通过了契约校验")


def test_swing_risk_type_medians_are_soft_derived():
    """2.x 的「风险类型归纳」是小节不入契约，分组中位数仍按派生值软放行。"""
    from dms_output_contract import _derived_only_tokens, _resolve_markdown_sections

    text = SWING_MARKDOWN.replace(
        "亏钱正文。\n",
        "亏钱正文。\n\n## 5.1 风险类型归纳\n\n| 风险类型 | 跌幅中位 |\n|---|---:|\n| 软件 | -8.12% |\n\n"
        "## 5.2 高强度爆量下跌个股明细\n\n| 股票 | 当日跌幅 |\n|---|---:|\n| 甲 | -9.88% |\n",
    )
    sections = _resolve_markdown_sections(text, INDUSTRY_SWING_CONTRACT.sections)
    derived = _derived_only_tokens(text, sections)
    assert "-8.12%" in derived
    assert "-9.88%" not in derived   # 明细表不在豁免范围


def test_legacy_2_0_chapter_name_still_resolves():
    """2.0 报告的第 2 章叫「大盘温度与宏观」，2.1 起改名后仍要能重渲染旧稿。"""
    old = SWING_MARKDOWN.replace("# 2. 大盘温度与风格", "# 2. 大盘温度与宏观")
    audit = validate_dms_content(old, _minimal_evidence(), INDUSTRY_SWING_CONTRACT)
    assert audit["status"] == "ok", audit
