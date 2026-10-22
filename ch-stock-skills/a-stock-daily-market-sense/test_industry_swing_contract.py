"""产业趋势波段(2.x) 契约路径的回归覆盖。

锁定的失败模式（都是 2.x 开发中真实发生过的）：

- **注册表漂移**：outputs.yaml 与 INDUSTRY_SWING_CONTRACT 在两处声明同一份章节集，
  只改一侧在本地全绿，直到某份报告撞上另一侧。yaml 侧刻意 required:false
  （同一 glob 服务两代报告），必选性由契约侧硬判——所以守护断言的是键/层级对齐，
  不是 requiredness。
- **指纹回退**：2.x 报告意外丢掉全部两个指纹标题时会静默回退 legacy 契约，
  再报一墙 legacy 章节错误。测试钉死指纹键。
- **缺章必须点名**：2.x 章节是必选 spec，丢掉亏钱效应要被点名，不许放行。

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

## 2.1 情绪趋势

| 指标 | 今日 | 近 5/20 日参照 | 趋势提示 |
|---|---:|---:|---|
| 成交额（万亿） |  |  |  |

==趋势判断：测试用判断句。==

## 2.2 指数趋势

| 指数 | 今日表现 | 5/20 日表现 | 位置与量价 | 趋势提示 |
|---|---:|---:|---|---|
| 上证指数 |  |  |  |  |

==指数趋势判断：测试用判断句。==

## 2.3 风格因子

风格因子正文。

==市场风格判断：测试用判断句。==

# 3. 产业趋势主线总览

主线正文。

# 4. 主线内关注个股（多维筛选 · 并集）

并集正文。

# 5. 亏钱效应（爆量下跌）

亏钱正文。
"""

# 命中指纹两键之一即可选型——这是 select_contract 的合同。
FINGERPRINT_KEYS = ("环境与仓位总闸门", "产业趋势主线总览")

EXPECTED_SECTIONS = [
    ("hero_verdict", 3),
    ("pos_gate", 2),
    ("temp_macro", 2),
    ("sentiment_trend", 3),
    ("index_trend", 3),
    ("market_style", 3),
    ("industry_mainline", 2),
    ("mainline_screening", 2),
    ("m4_decline", 2),
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
    broken = SWING_MARKDOWN.replace("# 5. 亏钱效应（爆量下跌）\n\n亏钱正文。\n", "")
    try:
        validate_dms_content(broken, _minimal_evidence(), INDUSTRY_SWING_CONTRACT)
    except ContractError as exc:
        assert "m4_decline" in str(exc), str(exc)
    else:
        raise AssertionError("缺 m4_decline 章节居然通过了契约校验")


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
    """2.0 报告的第 2 章叫「大盘温度与宏观」，章名正则兼认旧名；但三个小节必须有。"""
    old = SWING_MARKDOWN.replace("# 2. 大盘温度与风格", "# 2. 大盘温度与宏观")
    audit = validate_dms_content(old, _minimal_evidence(), INDUSTRY_SWING_CONTRACT)
    assert audit["status"] == "ok", audit


def test_flat_chapter2_fails_and_names_missing_subsections():
    """09-29 起的平铺第 2 章（无 h3 小节）必须被点名拦下——这是 2.2.0 的动机。"""
    flat = SWING_MARKDOWN.replace("""## 2.1 情绪趋势

| 指标 | 今日 | 近 5/20 日参照 | 趋势提示 |
|---|---:|---:|---|
| 成交额（万亿） |  |  |  |

==趋势判断：测试用判断句。==

## 2.2 指数趋势

| 指数 | 今日表现 | 5/20 日表现 | 位置与量价 | 趋势提示 |
|---|---:|---:|---|---|
| 上证指数 |  |  |  |  |

==指数趋势判断：测试用判断句。==

## 2.3 风格因子

风格因子正文。

==市场风格判断：测试用判断句。==
""", "平铺正文，没有任何小节。\n")
    try:
        validate_dms_content(flat, _minimal_evidence(), INDUSTRY_SWING_CONTRACT)
    except ContractError as exc:
        message = str(exc)
        for key in ("sentiment_trend", "index_trend", "market_style"):
            assert key in message, f"缺少对 {key} 的点名: {message}"
    else:
        raise AssertionError("平铺第 2 章居然通过了契约校验")


def test_missing_index_table_fails():
    """指数表（首列表头「指数」）是 K 线与创业板 PE 图的锚点，降级为散文要硬拦。"""
    broken = SWING_MARKDOWN.replace(
        "| 指数 | 今日表现 | 5/20 日表现 | 位置与量价 | 趋势提示 |\n|---|---:|---:|---|---|\n| 上证指数 |  |  |  |  |\n",
        "指数表现写成散文，没有表格。\n",
    )
    try:
        validate_dms_content(broken, _minimal_evidence(), INDUSTRY_SWING_CONTRACT)
    except ContractError as exc:
        assert "index_trend" in str(exc) and "指数表" in str(exc), str(exc)
    else:
        raise AssertionError("缺指数表居然通过了契约校验")


def test_missing_emotion_table_fails():
    """情绪指标表（首列表头「指标」）是情绪趋势五图的锚点。"""
    broken = SWING_MARKDOWN.replace(
        "| 指标 | 今日 | 近 5/20 日参照 | 趋势提示 |\n|---|---:|---:|---|\n| 成交额（万亿） |  |  |  |\n",
        "情绪读数写成散文，没有表格。\n",
    )
    try:
        validate_dms_content(broken, _minimal_evidence(), INDUSTRY_SWING_CONTRACT)
    except ContractError as exc:
        assert "sentiment_trend" in str(exc) and "情绪指标表" in str(exc), str(exc)
    else:
        raise AssertionError("缺情绪指标表居然通过了契约校验")


def test_style_subsection_legacy_name_still_resolves():
    """2.3 小节接受旧名「市场风格」——2.1.0 时期的合规稿件仍能重渲染。"""
    old = SWING_MARKDOWN.replace("## 2.3 风格因子", "## 2.3 市场风格")
    audit = validate_dms_content(old, _minimal_evidence(), INDUSTRY_SWING_CONTRACT)
    assert audit["status"] == "ok", audit
