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
_NUMERIC_EVIDENCE_KEYS = {
    "amount_concentration",
    "market_trend",
    "money_effect_samples",
    "volume_decline_samples",
    "feature_group_analysis_samples",
}


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
) -> Dict[str, Any]:
    """Validate DMS Markdown and return the content-contract audit payload."""
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

    table_numeric = _validate_table_numbers(markdown_text, evidence, problems, warnings)
    paragraph_detail = _paragraph_warnings(markdown_text, evidence, table_numeric["tokens"])
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
            "table_numbers": {key: value for key, value in table_numeric.items() if key != "tokens"},
            "paragraph_discipline": paragraph_detail["detail"],
            "forbidden_terms": forbidden,
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
) -> Dict[str, Any]:
    rows = _table_rows(markdown_text)
    tokens: List[str] = []
    for cells in rows:
        for cell in cells[1:]:  # first column is a label, stock name or ordinal
            if re.search(r"[<>≤≥~～]", cell):
                continue  # methodology/reference thresholds are not daily evidence
            tokens.extend(_NUMBER_RE.findall(cell))
    unique_tokens = sorted(set(tokens))
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

    evidence_forms = _evidence_number_forms(evidence)
    unmatched = [token for token in unique_tokens if _canonical_number(token) not in evidence_forms]
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
        "tokens": unique_tokens,
    }


def _paragraph_warnings(
    markdown_text: str,
    evidence: Mapping[str, Any],
    table_tokens: Sequence[str],
) -> Dict[str, Any]:
    table_token_set = {_canonical_number(token) for token in table_tokens}
    evidence_forms = _evidence_number_forms(evidence)
    prose_lines = [
        line
        for line in markdown_text.splitlines()
        if not line.lstrip().startswith(("|", "#"))
        and not _TABLE_SEPARATOR_RE.match(line)
    ]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", "\n".join(prose_lines)) if part.strip()]
    warnings: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    for index, paragraph in enumerate(paragraphs, start=1):
        plain = re.sub(r"^[\s=*`>-]+", "", paragraph)
        if re.match(r"\d{2,}", plain):
            warnings.append({
                "rule": "judgment_starts_with_digits",
                "paragraph": index,
                "text": plain[:100],
            })
        numbers = _NUMBER_RE.findall(paragraph)
        if len(numbers) > 3:
            warnings.append({
                "rule": "paragraph_numeric_limit",
                "paragraph": index,
                "count": len(numbers),
                "text": plain[:100],
            })
        absent = sorted({
            token for token in numbers
            if _canonical_number(token) not in evidence_forms
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
            "paragraphs_checked": len(paragraphs),
            "unknown_numeric_paragraphs": len(unknown),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI adapter used by the compiled output gate."""
    parser = argparse.ArgumentParser(description="Validate a staged DMS Markdown report.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        # The renderer owns the one canonical SectionContract.  Importing it
        # here prevents a second declaration from drifting independently.
        from render_report_html import DMS_CONTRACT

        audit = validate_dms_content(
            args.input.read_text(encoding="utf-8"), evidence, DMS_CONTRACT
        )
    except Exception as exc:  # noqa: BLE001 - deterministic validator boundary
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
