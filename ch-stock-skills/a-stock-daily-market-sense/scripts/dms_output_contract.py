"""Deterministic Markdown-side checks for the DMS output contract.

The HTML section contract proves that anchors exist.  This module checks the
parts that only exist before rendering: declared degradations, their evidence,
dynamic 3.2 headings, highlighted judgment paragraphs and numeric provenance.
It returns an audit payload suitable for embedding in the render manifest and
raises ``ContractError`` for hard failures.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

SCRIPT_ROOT = Path(__file__).resolve().parent
_BUNDLED_SHARED = SCRIPT_ROOT / "_shared"
_DEV_SHARED = SCRIPT_ROOT.parents[2] / "shared"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

from html_report.contract import ContractError, SectionContract, SectionSpec, strip_numbering
from html_report.markdown_engine import render_markdown


_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*$")
_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?%?")
_FORBIDDEN_ADVICE_PATTERNS = {
    "actionable_trade_instruction": re.compile(
        r"(?:建议读者|建议投资者|我们建议|操作建议|交易建议|可考虑|应当|应该|宜|请|务必)"
        r"[^。；\n]{0,24}(?:买入|卖出|加仓|止损)"
    ),
    "direct_trade_instruction": re.compile(
        r"^(?:[-*]\s*)?(?:买入|卖出|加仓|止损)(?:[：:\s]|$)",
        re.MULTILINE,
    ),
    "standalone_price_target": re.compile(
        r"^(?:[-*]\s*)?(?:目标价|止损价)\s*(?:为|设为|看到|上看|下看|[:：])?"
        r"\s*(?:人民币|[¥￥$])?\s*\d",
        re.MULTILINE,
    ),
}
# 前瞻轴输出的是条件分布，不是预测。这几种写法把「历史上 N 次同类日之后……」
# 压缩成了一句去掉样本与基准的断言，正是 forward_odds.md 明令禁止的。
_FORBIDDEN_FORECAST_PATTERNS = {
    "bare_probability_claim": re.compile(
        r"(?:大概率|多半|基本上?)(?:会|将|要)?(?:上涨|反弹|回升|走强|下跌|回落|走弱)"
    ),
    "imminent_move_claim": re.compile(r"(?:反弹|下跌|回调|拐点|见底|见顶)(?:在即|已至|确认了?)"),
}
_NUMERIC_EVIDENCE_KEYS = {
    "amount_concentration",
    "market_trend",
    "money_effect_samples",
    "volume_decline_samples",
    "feature_group_analysis_samples",
}
# 数值出处不止 evidence 一处：主题级统计由 theme_group_stats.py 单独算进
# module_context_<日期>/，M3/M4 交叉检查也一样。它们同样是脚本产物，却曾因为
# 不在取数集里而被判成「编造」——2026-08-19 的 27.45%、5.43% 就是这么被从
# 表格里赶进正文的。取数集按文件名前缀扩容，未知文件不自动纳入。
_AUX_PROVENANCE_GLOBS = ("module3_theme_stats.json", "module3_theme_map.json", "assembled_checks.json")
# 模型自建分组的派生聚合列：3.1 的风险类型是模型当场分的组，模板要求填组内
# 中位数，而偶数样本的中位数按定义要取中间两值的平均——这个数 evidence 里本来
# 就不会有。硬判会逼出「3.85 / 4.25」这种并列写法，所以只在这一节降为软告警。
_DERIVED_AGGREGATE_SECTIONS = ("m4_risk_types",)
# 数字限流针对的是判断段里的读数密度，不是模板强制的结构块。以下几类整段跳过：
# frontmatter、正文前的元信息、1.1 那张必须逐项照抄的状态卡、以及判据/口径声明。
_DEFINITION_PREFIXES = (
    "判据", "判定规则", "数据源", "数据基础", "分层口径", "口径", "字段口径",
    "时间口径", "来源", "注", "说明", "预筛", "前高折扣",
)
_CARD_BULLET_RE = re.compile(r"^[-*]\s*\*\*([^*]+)\*\*\s*[：:]")
_STATE_CARD_LABELS = {
    "趋势状态", "极值轴", "计数器", "当日阈值", "相位证据", "升档说明",
    "近 5 日轨迹", "解除进度",
}
_BULLET_RE = re.compile(r"^[-*+]\s+|^\d+[.、)]\s+")
_THRESHOLD_RE = re.compile(r"[<>≤≥]")
# 窗口标签与时间戳不是读数：「5 日均」「20 日线」「120 日高点」「09:31」
# 「2026-08-19」都只是坐标，计进限流会让每段都超标。
_DATE_TOKEN_RE = re.compile(r"\d{4}-\d{2}-\d{2}|(?<!\d)\d{1,2}-\d{2}(?!\d)")
_CLOCK_TOKEN_RE = re.compile(r"(?<!\d)\d{1,2}:\d{2}")
_WINDOW_TOKEN_RE = re.compile(
    r"(?<![\d.])\d+(?:\.\d+)?\s*(?:个交易日|个月|个季度|日线|日均|日|天|周|月|年|季度)"
)
# 指数名里的数字也不是读数：科创50、沪深300、中证1000、国证2000、300成长。
# 判据是「紧贴中文、没有空格、后面不接单位」——「跌停 137 家」有空格，不受影响。
_UNIT_CHARS = "%亿万倍家只个日月年天周次条股元笔pctbp"
_NAME_NUMBER_RE = re.compile(
    rf"(?<=[\u4e00-\u9fff])\d+(?:\.\d+)?(?![.\d])(?![{_UNIT_CHARS}])"
    rf"|(?<![\d.%])\d+(?![{_UNIT_CHARS}])(?=[\u4e00-\u9fff])"
)


@dataclass(frozen=True)
class MarkdownSection:
    level: int
    title: str
    stripped: str
    body: str


def validate_dms_content(
    markdown_text: str,
    evidence: Mapping[str, Any],
    contract: SectionContract,
    aux_payloads: Sequence[Tuple[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Validate DMS Markdown and return the content-contract audit payload.

    ``aux_payloads`` are ``(label, payload)`` pairs of *other* deterministic
    script outputs whose numbers are just as legitimate as evidence's — see
    ``_AUX_PROVENANCE_GLOBS``.  Passing none keeps the old evidence-only scope.
    """
    # Reuse the same renderer and resolver as the eventual HTML build.  This
    # makes heading existence/order/level failures part of one hard verdict.
    contract.stamp(render_markdown(markdown_text))
    sections = _resolve_markdown_sections(markdown_text, contract.sections)
    problems: List[str] = []
    warnings: List[Dict[str, Any]] = []
    degraded: List[Dict[str, Any]] = []

    rated_themes = _rated_theme_count(sections["m3_mainline"].body)
    three_star = _rated_theme_count(sections["m3_mainline"].body, cell_pattern=r"★★★")
    dynamic_titles = _validate_dynamic_catalyst(sections, three_star, problems)

    for spec in contract.sections:
        section = sections.get(spec.key)
        if not spec.degraded_patterns or section is None:
            continue
        matched = _matches_degradation(section.body, spec)
        if not matched:
            continue
        ok, evidence_detail = _degradation_supported(
            spec.key,
            section.body,
            evidence,
            rated_themes=rated_themes,
        )
        row = {
            "section": spec.key,
            "heading": section.title,
            "pattern": matched,
            "evidence": evidence_detail,
        }
        degraded.append(row)
        if not ok:
            problems.append(
                f"[{spec.key}] declared degradation is not supported by evidence: {evidence_detail}"
            )

    highlight_detail = _validate_highlights(sections, markdown_text, problems)

    forbidden = [
        name
        for name, pattern in _FORBIDDEN_ADVICE_PATTERNS.items()
        if pattern.search(markdown_text)
    ]
    if forbidden:
        problems.append(f"forbidden trading-advice terms present: {forbidden}")

    forbidden_forecast = [
        name
        for name, pattern in _FORBIDDEN_FORECAST_PATTERNS.items()
        if pattern.search(markdown_text)
    ]
    if forbidden_forecast:
        problems.append(
            "forward-odds must stay a conditional distribution, not a forecast; "
            f"offending phrasing: {forbidden_forecast}"
        )

    forward_detail = _validate_forward_axis(sections, evidence, problems)

    provenance = _NumberProvenance(evidence, aux_payloads)
    derived_only = _derived_only_tokens(markdown_text, sections)
    table_numeric = _validate_table_numbers(
        markdown_text, evidence, problems, warnings,
        provenance=provenance, derived_only_tokens=derived_only,
    )
    paragraph_detail = _paragraph_warnings(markdown_text, provenance, table_numeric["tokens"])
    warnings.extend(paragraph_detail["warnings"])

    audit = {
        "status": "error" if problems else "ok",
        "contract_version": contract.version,
        "section_count": len(contract.sections),
        "order": contract.order,
        "degraded": degraded,
        "warnings": warnings,
        "detail": {
            "dynamic_sections": {
                "rated_theme_rows": rated_themes,
                "three_star_rows": three_star,
                "expected": three_star,
                "matched": len(dynamic_titles),
                "titles": dynamic_titles,
                "section_present": "m3_catalyst" in sections,
            },
            "highlights": highlight_detail,
            "forward_axis": forward_detail,
            "table_numbers": {key: value for key, value in table_numeric.items() if key != "tokens"},
            "paragraph_discipline": paragraph_detail["detail"],
            "forbidden_terms": forbidden,
            "forbidden_forecast_terms": forbidden_forecast,
        },
    }
    if problems:
        raise ContractError(
            f"DMS content contract {contract.version} failed:\n"
            + "\n".join(f"  - {problem}" for problem in problems)
        )
    return audit


def _resolve_markdown_sections(
    markdown_text: str,
    specs: Sequence[SectionSpec],
) -> Dict[str, MarkdownSection]:
    matches = list(_MD_HEADING_RE.finditer(markdown_text))
    parsed: List[MarkdownSection] = []
    for index, match in enumerate(matches):
        level = len(match.group(1))
        end = len(markdown_text)
        for later in matches[index + 1 :]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        title = match.group(2).strip()
        parsed.append(
            MarkdownSection(
                level=level,
                title=title,
                stripped=strip_numbering(title),
                body=markdown_text[match.end() : end].strip(),
            )
        )

    resolved: Dict[str, MarkdownSection] = {}
    for spec in specs:
        hits: List[MarkdownSection] = []
        for pattern in spec.patterns:
            compiled = re.compile(pattern)
            hits = [
                section
                for section in parsed
                if compiled.search(section.stripped) or compiled.search(section.title)
            ]
            if hits:
                break
        if len(hits) == 1:
            resolved[spec.key] = hits[0]

    # ``contract.stamp`` already resolved every spec against the *rendered*
    # headings, so a miss here means the raw Markdown heading reads differently
    # from its rendered form — inline markup (``## 1.4 **市场风格**``) is the
    # usual cause, since rendering strips the tags and this scan does not.
    # Without this guard the callers below index ``sections[key]`` straight into
    # a KeyError, which reads like a crash rather than the contract failure it is.
    unresolved = [spec.key for spec in specs if spec.required and spec.key not in resolved]
    if unresolved:
        raise ContractError(
            "DMS content contract: these sections resolved in the rendered HTML but "
            f"not in the raw Markdown: {unresolved}. Check the headings for inline "
            "markup or duplicate titles."
        )
    return resolved


def _matches_degradation(body: str, spec: SectionSpec) -> str:
    for pattern in spec.degraded_patterns:
        if re.search(pattern, body, re.IGNORECASE | re.DOTALL):
            return pattern
    return ""


def _rated_theme_count(body: str, cell_pattern: str = r"★★★|★★") -> int:
    count = 0
    for line in body.splitlines():
        if not line.lstrip().startswith("|") or _TABLE_SEPARATOR_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if any(re.fullmatch(cell_pattern, cell) for cell in cells):
            count += 1
    return count


def _dynamic_theme_titles(body: str) -> List[str]:
    return [
        match.group(2).strip()
        for match in _MD_HEADING_RE.finditer(body)
        if len(match.group(1)) == 3 and re.search(r"★★★", match.group(2))
    ]


def _validate_dynamic_catalyst(
    sections: Mapping[str, MarkdownSection],
    three_star: int,
    problems: List[str],
) -> List[str]:
    """3.2 is the one section allowed to vanish, and its presence is not a choice.

    ``references/template/section3.md`` gates 3.2 on ★★★ alone: with no
    three-star mainline the whole section goes — heading and fallback sentence
    included — and with one or more, every one of them gets its own ``###``.
    So both directions are failures: a 3.2 that lingers with nothing to say, and
    a 3.2 that is missing while 3.1 named three-star mainlines.
    """
    section = sections.get("m3_catalyst")
    if three_star == 0:
        if section is not None:
            problems.append(
                "[m3_catalyst] 3.1 has no ★★★ row, so 3.2 must be omitted entirely "
                "(heading and fallback sentence included), but the section is present"
            )
        return []
    if section is None:
        problems.append(
            f"[m3_catalyst] 3.1 has {three_star} ★★★ row(s) but section 3.2 is missing"
        )
        return []
    titles = _dynamic_theme_titles(section.body)
    if len(titles) != three_star:
        problems.append(
            "[m3_catalyst] dynamic heading count mismatch: "
            f"3.1 has {three_star} ★★★ row(s), found {len(titles)} theme heading(s) "
            f"under 3.2 ({titles})"
        )
    return titles


def _dig(payload: Mapping[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _candidate_state(evidence: Mapping[str, Any], group_key: str) -> Tuple[bool, int, str]:
    group = _dig(evidence, "feature_group_analysis_samples", "groups", group_key)
    if not isinstance(group, Mapping):
        return False, 0, ""
    candidates = group.get("candidates")
    count = len(candidates) if isinstance(candidates, list) else 0
    return bool(group.get("available")), count, str(group.get("reason") or group.get("error") or "")


def _degradation_supported(
    key: str,
    body: str,
    evidence: Mapping[str, Any],
    *,
    rated_themes: int,
) -> Tuple[bool, str]:
    if key == "market_style":
        block = _dig(evidence, "market_trend", "market_style")
        available = bool(block.get("available")) if isinstance(block, Mapping) else False
        return not available, f"market_style.available={available}"
    if key == "m3_mainline":
        return rated_themes == 0, f"3.1 rated_theme_rows={rated_themes}"
    if key == "m3_leaders":
        candidates = _dig(evidence, "money_effect_samples", "candidates")
        candidate_count = len(candidates) if isinstance(candidates, list) else 0
        ok = rated_themes == 0 or candidate_count == 0
        return ok, f"rated_theme_rows={rated_themes}, money_effect_candidates={candidate_count}"
    if key in {
        "m5_capacity_up",
        "m5_monthly_base",
        "m5_early_limit",
        "m5_discount_relaunch",
    }:
        group_key = {
            "m5_capacity_up": "capacity_up",
            "m5_monthly_base": "monthly_base_breakout",
            "m5_early_limit": "early_limit_up_1030",
            "m5_discount_relaunch": "discount_relaunch",
        }[key]
        available, count, reason = _candidate_state(evidence, group_key)
        reason_ok = not reason or reason in body
        return ((not available or count == 0) and reason_ok,
                f"available={available}, candidates={count}, reason={reason or 'none'}")
    if key == "m5_overlap":
        hits = _dig(evidence, "feature_group_analysis_samples", "overlap_hits")
        count = len(hits) if isinstance(hits, list) else 0
        return count == 0, f"overlap_hits={count}"
    return False, "no evidence rule registered"


def _validate_forward_axis(
    sections: Mapping[str, MarkdownSection],
    evidence: Mapping[str, Any],
    problems: List[str],
) -> Dict[str, Any]:
    """前瞻轴的三条硬纪律，机器化在这里（规则见 forward_odds.md）。

    为什么要机器拦：这三条全是「读数在、但作者顺手写强了」的失败模式，靠
    review 抓不稳。凡治理层依赖的，机器层就得有校验。
    """
    card = evidence.get("forward_odds") or {}
    pulse = card.get("pulse") or {}
    body = sections["sentiment_trend"].body if "sentiment_trend" in sections else ""
    detail: Dict[str, Any] = {
        "card_available": bool(card.get("available")),
        "pulse_available": bool(pulse.get("available")),
        "gate": pulse.get("gate"),
    }
    if not card.get("available") or not pulse.get("available"):
        detail["checked"] = False
        return detail
    detail["checked"] = True

    # R1 读数在就必须出现在 1.1，不能整行吞掉
    detail["axis_line_present"] = "情绪脉冲" in body
    if not detail["axis_line_present"]:
        problems.append(
            "[sentiment_trend] forward axis reading is available but the 情绪脉冲 line is missing"
        )

    signal = next(
        (s for s in card.get("signals", []) if s.get("key") == "pulse_gate"), {}
    )
    gate_hit = bool(pulse.get("gate"))
    publishable = bool(signal.get("publishable"))

    # R2 没命中就不许出现「情绪脉冲触发」这个判断词
    claimed = "情绪脉冲触发" in body
    detail["claimed_trigger"] = claimed
    if claimed and not (gate_hit and publishable):
        problems.append(
            "[sentiment_trend] 「情绪脉冲触发」claimed but "
            f"gate={gate_hit} / publishable={publishable}"
        )

    # R3 命中且可发布时，条件分布必须带样本量——概率不带 n 就是断言不是证据
    if gate_hit and publishable:
        events = (signal.get("sample") or {}).get("events")
        horizon_rows = {
            int(row["horizon_days"]): row
            for row in signal.get("horizons", [])
            if row.get("horizon_days") is not None
        }
        mentioned_horizons = sorted({
            int(match.group(1))
            for match in re.finditer(
                r"(?:\+|T\s*\+|未来)\s*(\d+)\s*日", body, re.IGNORECASE
            )
        })
        unknown = [days for days in mentioned_horizons if days not in horizon_rows]
        detail["unknown_horizons"] = unknown
        if unknown:
            problems.append(
                "[sentiment_trend] forward-odds cited horizons absent from evidence: "
                f"{unknown}"
            )
        gate_detail = signal.get("gate_detail") or {}
        published = gate_detail.get("publishable_horizons")
        # 兼容修复前已落盘的 evidence；新证据以同视窗三门槛合取后的列表为准。
        if published is None:
            published = (gate_detail.get("subsample_consistent") or {}).get("horizons") or []
            detail["publishable_horizons_source"] = "legacy_subsample_fallback"
        else:
            detail["publishable_horizons_source"] = "publishable_horizons"
        publishable_horizons = set(published)
        unsupported = [
            days for days in mentioned_horizons
            if days in horizon_rows and days not in publishable_horizons
        ]
        detail["mentioned_horizons"] = mentioned_horizons
        detail["publishable_horizons"] = sorted(publishable_horizons)
        detail["unsupported_horizons"] = unsupported
        if unsupported:
            problems.append(
                "[sentiment_trend] forward-odds cited from non-publishable horizons: "
                f"{unsupported}"
            )

        required = {
            f"+{days}": horizon_rows[days].get("n")
            for days in mentioned_horizons if days in horizon_rows
        } or {"events": events}
        detail["required_sample_sizes"] = required
        unique_sample_sizes = {value for value in required.values() if isinstance(value, int)}
        # 保留旧 audit 字段；多个视窗 n 不同的时候不再伪造一个统一样本量。
        detail["required_sample_size"] = (
            next(iter(unique_sample_sizes)) if len(unique_sample_sizes) == 1 else None
        )
        missing = [
            label for label, sample_n in required.items()
            if not _forward_sample_size_cited(body, sample_n)
        ]
        detail["sample_size_cited"] = not missing
        if not detail["sample_size_cited"]:
            problems.append(
                "[sentiment_trend] forward-odds cited without horizon-specific sample size: "
                f"{missing} require {required}"
            )
    return detail


def _forward_sample_size_cited(body: str, sample_n: Any) -> bool:
    """只接受明确的 n=、样本量或「历史上 N 次同类日/样本/事件」引用。"""
    if not isinstance(sample_n, int) or isinstance(sample_n, bool) or sample_n <= 0:
        return False
    sample_patterns = (
        rf"(?<![A-Za-z0-9_])n\s*[=:：]\s*{sample_n}(?!\d)",
        rf"样本(?:量)?\s*(?:为|[=:：])?\s*{sample_n}(?!\d)",
        rf"历史上\s*{sample_n}\s*次(?:"
        rf"(?:已完成\s*(?:\+|T\s*\+|未来)?\s*\d+\s*日观察的\s*)?同类日|样本|事件)",
    )
    return any(re.search(pattern, body, re.IGNORECASE) for pattern in sample_patterns)


def _validate_highlights(
    sections: Mapping[str, MarkdownSection],
    markdown_text: str,
    problems: List[str],
) -> Dict[str, bool]:
    # output_discipline.md 点名的定性高亮。模块 1 的定性段自 2026-08 起就是
    # 1.1 末尾的「趋势判断」——原先承担这个角色的「市场状态与盘面定性」随 1.1
    # 一起移除了，所以高亮改落在 sentiment_trend 上。
    rules = {
        "sentiment_trend": r"趋势判断",
        "index_trend": r"指数趋势判断",
        "market_style": r"市场风格判断",
        "m3_leaders": r"主线\s*vs\s*资金轮动结论",
        "m4_decline_details": r"风险传导提示",
    }
    detail: Dict[str, bool] = {}
    for key, label_pattern in rules.items():
        blocks = re.findall(r"==(.+?)==", sections[key].body, re.DOTALL)
        present = any(re.search(label_pattern, block, re.IGNORECASE) for block in blocks)
        detail[key] = present
        if not present:
            problems.append(f"[{key}] required highlighted judgment paragraph is missing")
    m5_match = re.search(
        r"(?ms)^#\s+4\.\s*特征分组分析\s*$.*?^==.*?一句话判断.*?==\s*$",
        markdown_text,
    )
    detail["m5_verdict"] = bool(m5_match)
    if not m5_match:
        problems.append("[m5_verdict] required highlighted judgment paragraph is missing")
    return detail


def _numeric_forms(value: float) -> Iterable[str]:
    for scaled in (value, value * 100, value / 100, value * 10000, value / 10000):
        for digits in range(5):
            text = f"{scaled:.{digits}f}"
            yield _canonical_number(text)


def _canonical_number(token: str) -> str:
    text = token.strip().rstrip("%").replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return text
    if value == 0:
        value = 0.0
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _evidence_number_forms(payload: Any) -> set[str]:
    forms: set[str] = set()
    if isinstance(payload, Mapping):
        for value in payload.values():
            forms.update(_evidence_number_forms(value))
    elif isinstance(payload, list):
        for value in payload:
            forms.update(_evidence_number_forms(value))
    elif isinstance(payload, bool) or payload is None:
        pass
    elif isinstance(payload, (int, float)):
        forms.update(_numeric_forms(float(payload)))
    elif isinstance(payload, str):
        for token in _NUMBER_RE.findall(payload):
            forms.add(_canonical_number(token))
    return forms


class _NumberProvenance:
    """Every number a deterministic script produced for this report date.

    Evidence is the backbone; aux payloads (theme stats, cross checks) carry the
    rest.  Membership is by canonical form, so 5.43 and 5.430% are one value.
    """

    def __init__(
        self,
        evidence: Mapping[str, Any],
        aux_payloads: Sequence[Tuple[str, Any]] | None = None,
    ) -> None:
        self.forms = _evidence_number_forms(evidence)
        self.sources: List[str] = ["evidence"]
        for label, payload in aux_payloads or ():
            forms = _evidence_number_forms(payload)
            if not forms:
                continue
            self.forms |= forms
            self.sources.append(label)

    def __contains__(self, token: str) -> bool:
        return _canonical_number(token) in self.forms


def discover_aux_payloads(evidence_path: Path) -> List[Tuple[str, Any]]:
    """Sibling module-context files that also hold script-computed numbers.

    Layout is fixed by ``run_daily_panel.py``: ``reports/evidence_YYYYMMDD_utf8.json``
    next to ``reports/module_context_YYYYMMDD/``.  Missing or unreadable files are
    skipped — this widens provenance, it must never become a new way to fail.
    """
    match = re.search(r"(\d{8})", evidence_path.name)
    if not match:
        return []
    context_dir = evidence_path.parent / f"module_context_{match.group(1)}"
    payloads: List[Tuple[str, Any]] = []
    for filename in _AUX_PROVENANCE_GLOBS:
        candidate = context_dir / filename
        if not candidate.is_file():
            continue
        try:
            payloads.append((filename, json.loads(candidate.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue
    return payloads


def _table_tokens(markdown_text: str) -> set[str]:
    tokens: set[str] = set()
    for cells in _table_rows(markdown_text):
        for cell in cells[1:]:  # first column is a label, stock name or ordinal
            if _THRESHOLD_RE.search(cell) or re.search(r"[~～]", cell):
                continue  # methodology/reference thresholds are not daily evidence
            tokens.update(_NUMBER_RE.findall(cell))
    return tokens


def _derived_only_tokens(
    markdown_text: str,
    sections: Mapping[str, "MarkdownSection"],
) -> set[str]:
    """Tokens that live *only* in a model-built aggregate table (3.1 today).

    A value repeated outside that section is a claim like any other and stays
    hard-judged; the carve-out covers just the medians the model had to compute.
    """
    derived: set[str] = set()
    for key in _DERIVED_AGGREGATE_SECTIONS:
        section = sections.get(key)
        if section is None:
            continue
        elsewhere = {
            _canonical_number(token)
            for token in _table_tokens(markdown_text.replace(section.body, "", 1))
        }
        derived |= {
            token for token in _table_tokens(section.body)
            if _canonical_number(token) not in elsewhere
        }
    return derived


def _table_rows(markdown_text: str) -> List[List[str]]:
    rows: List[List[str]] = []
    in_table = False
    header_seen = False
    for line in markdown_text.splitlines():
        if line.lstrip().startswith("|"):
            if _TABLE_SEPARATOR_RE.match(line):
                in_table = True
                header_seen = True
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if in_table and header_seen:
                rows.append(cells)
            continue
        in_table = False
        header_seen = False
    return rows


def _validate_table_numbers(
    markdown_text: str,
    evidence: Mapping[str, Any],
    problems: List[str],
    warnings: List[Dict[str, Any]],
    provenance: "_NumberProvenance | None" = None,
    derived_only_tokens: Iterable[str] = (),
) -> Dict[str, Any]:
    unique_tokens = sorted(_table_tokens(markdown_text))
    complete = _NUMERIC_EVIDENCE_KEYS.issubset(evidence.keys())
    if not complete:
        missing = sorted(_NUMERIC_EVIDENCE_KEYS - set(evidence.keys()))
        warnings.append({
            "rule": "table_numeric_evidence",
            "message": f"skipped for incomplete/synthetic evidence; missing {missing}",
        })
        return {
            "status": "skipped_incomplete_evidence",
            "checked": 0,
            "unmatched": [],
            "tokens": unique_tokens,
        }

    if provenance is None:
        provenance = _NumberProvenance(evidence)
    derived_set = set(derived_only_tokens)
    absent = [token for token in unique_tokens if token not in provenance]
    derived = [token for token in absent if token in derived_set]
    unmatched = [token for token in absent if token not in derived_set]
    if derived:
        warnings.append({
            "rule": "derived_group_aggregate",
            "sections": list(_DERIVED_AGGREGATE_SECTIONS),
            "values": derived,
            "message": "模型自建分组的派生聚合值，evidence 里没有原值，按软告警记录",
        })
    if unmatched:
        problems.append(
            "table numeric cells contain values absent from evidence: "
            + ", ".join(unmatched[:30])
            + (f" (+{len(unmatched) - 30} more)" if len(unmatched) > 30 else "")
        )
    return {
        "status": "error" if unmatched else "ok",
        "checked": len(unique_tokens),
        "unmatched": unmatched,
        "derived": derived,
        "sources": provenance.sources,
        "tokens": unique_tokens,
    }


def _strip_structural_blocks(markdown_text: str) -> str:
    """Drop frontmatter, tables, headings and fenced code before segmenting."""
    text = re.sub(r"\A---\n.*?\n---\n", "", markdown_text, count=1, flags=re.DOTALL)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    lines = [
        line
        for line in text.splitlines()
        if not line.lstrip().startswith(("|", "#"))
        and not _TABLE_SEPARATOR_RE.match(line)
    ]
    return "\n".join(lines)


def _paragraph_kind(paragraph: str) -> str:
    """Classify a paragraph so the numeric-limit rule only judges prose.

    The limit exists to stop judgment paragraphs from turning into number
    soup.  Template-mandated structure is not prose: the 1.1 state card must
    copy every reading verbatim, and 判据 / 数据源 / 口径 blocks restate fixed
    thresholds.  Counting those guaranteed a daily wall of warnings — and a
    gate that cries wolf every day gets routed around within a week.
    """
    lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
    if not lines:
        return "empty"
    # 页眉元信息：日期、数据来源、生成时间、免责声明——每行都是「短标签：值」。
    if len(lines) >= 2 and all(re.match(r"^[^：:\s]{2,10}[：:]", line) for line in lines):
        return "metadata"
    bullets = [line for line in lines if _BULLET_RE.match(line)]
    if bullets and len(bullets) == len(lines):
        card_labels = [
            match.group(1).strip()
            for line in bullets
            if (match := _CARD_BULLET_RE.match(line))
            and match.group(1).strip() in _STATE_CARD_LABELS
        ]
        if len(card_labels) * 2 >= len(bullets):
            return "reading_card"
    plain = re.sub(r"^[\s=*`>-]+", "", lines[0])
    if plain.startswith(_DEFINITION_PREFIXES) and re.match(
        r"^[^\s]{1,6}\s*[：:=＝]", plain
    ):
        return "definition"
    if len(_THRESHOLD_RE.findall(paragraph)) >= 2:
        return "definition"
    return "prose"


def _reading_tokens(paragraph: str) -> List[str]:
    """Numbers that carry a reading — window labels and timestamps stripped."""
    text = _DATE_TOKEN_RE.sub(" ", paragraph)
    text = _CLOCK_TOKEN_RE.sub(" ", text)
    text = _NAME_NUMBER_RE.sub(" ", text)
    text = _WINDOW_TOKEN_RE.sub(" ", text)
    return _NUMBER_RE.findall(text)


def _paragraph_warnings(
    markdown_text: str,
    provenance: "_NumberProvenance",
    table_tokens: Sequence[str],
) -> Dict[str, Any]:
    table_token_set = {_canonical_number(token) for token in table_tokens}
    prose_text = _strip_structural_blocks(markdown_text)
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", prose_text) if part.strip()]
    warnings: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    skipped: Dict[str, int] = {}
    checked = 0
    for index, paragraph in enumerate(paragraphs, start=1):
        kind = _paragraph_kind(paragraph)
        if kind != "prose":
            skipped[kind] = skipped.get(kind, 0) + 1
            continue
        checked += 1
        plain = re.sub(r"^[\s=*`>-]+", "", paragraph)
        if re.match(r"\d{2,}", plain):
            warnings.append({
                "rule": "judgment_starts_with_digits",
                "paragraph": index,
                "text": plain[:100],
            })
        numbers = _reading_tokens(paragraph)
        if len(numbers) > 3:
            warnings.append({
                "rule": "paragraph_numeric_limit",
                "paragraph": index,
                "count": len(numbers),
                "text": plain[:100],
            })
        absent = sorted({
            token for token in numbers
            if token not in provenance
            and _canonical_number(token) not in table_token_set
        })
        if absent:
            unknown.append({"paragraph": index, "values": absent, "text": plain[:100]})
    if unknown:
        warnings.append({
            "rule": "paragraph_numbers_without_provenance",
            "count": len(unknown),
            "detail": unknown[:30],
        })
    return {
        "warnings": warnings,
        "detail": {
            "paragraphs_checked": checked,
            "paragraphs_skipped": skipped,
            "unknown_numeric_paragraphs": len(unknown),
            "provenance_sources": provenance.sources,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI adapter used by the compiled output gate."""
    parser = argparse.ArgumentParser(description="Validate a staged DMS Markdown report.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument(
        "--aux",
        type=Path,
        action="append",
        default=[],
        help="额外的脚本产物 JSON，其中的数字与 evidence 同等可信；"
             "默认已自动纳入同日 module_context_YYYYMMDD/ 下的主题统计与交叉检查。",
    )
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        aux_payloads = discover_aux_payloads(args.evidence)
        for path in args.aux:
            aux_payloads.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
        # The renderer owns the one canonical SectionContract.  Importing it
        # here prevents a second declaration from drifting independently.
        from render_report_html import DMS_CONTRACT

        audit = validate_dms_content(
            args.input.read_text(encoding="utf-8"), evidence, DMS_CONTRACT, aux_payloads
        )
    except Exception as exc:  # noqa: BLE001 - deterministic validator boundary
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
