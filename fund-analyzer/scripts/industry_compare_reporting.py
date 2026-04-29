#!/usr/bin/env python3
"""
Report formatting helpers for industry fund comparison.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from industry_compare_metrics import (
    fmt_aum_total,
    fmt_days,
    fmt_focus,
    fmt_num,
    fmt_pct,
    fmt_percentile,
    format_date,
)


def format_report(
    context: Any,
    payload: Dict[str, Any],
    prohibited_terms: List[str],
) -> str:
    funds = payload["funds"]
    rankings = payload["rankings"]
    date = payload["date"]

    lines: List[str] = []
    lines.append(f"## {context.canonical_name}基金对比报告（{date}）")
    lines.append("")
    lines.append("### 对比标的")
    lines.append("| 基金名称 | 代码 | 规模(亿) | 经理 | 任职日期 |")
    lines.append("|---------|------|---------|------|---------|")
    for fund in funds:
        manager_name = str(fund["manager"].get("name", "") or "样本不足")
        begin_date = format_date(fund["manager"].get("begin_date"))
        lines.append(
            f"| {fund['name']} | {fund['ts_code']} | {fund['aum']:.1f} | {manager_name} | {begin_date} |"
        )

    lines.extend(
        [
            "",
            "### 一、超额水平（近1年）",
            "| 指标 | " + " | ".join(fund["name"] for fund in funds) + " | 行业平均 | 简要评价 |",
            "|------|" + "-------|" * len(funds) + "---------|---------|",
            metric_row(
                "行业超额α",
                [fmt_pct(fund["industry_alpha"]) for fund in funds],
                "0%",
                best_comment(funds, "industry_alpha", True),
            ),
            metric_row(
                "大盘超额",
                [fmt_pct(fund["market_excess"]) for fund in funds],
                "0%",
                best_comment(funds, "market_excess", True),
            ),
            metric_row(
                "超额稳定性",
                [fmt_pct(fund["excess_stability"]) for fund in funds],
                "15%",
                best_comment(funds, "excess_stability", False),
            ),
            metric_row(
                "跑赢行业胜率",
                [fmt_pct(fund["industry_win_rate"]) for fund in funds],
                "50%",
                best_comment(funds, "industry_win_rate", True),
            ),
            "",
            "### 二、净值表现（近1年）",
            "| 指标 | " + " | ".join(fund["name"] for fund in funds) + " | 简要评价 |",
            "|------|" + "-------|" * len(funds) + "---------|",
            metric_row(
                "年化收益",
                [fmt_pct(fund["annual_return"]) for fund in funds],
                None,
                sorted_comment(funds, "annual_return"),
            ),
            metric_row(
                "夏普比率",
                [fmt_num(fund["sharpe_calc"]) for fund in funds],
                None,
                best_comment(funds, "sharpe_calc", True),
            ),
            metric_row(
                "卡玛比率",
                [fmt_num(fund["calmar"]) for fund in funds],
                None,
                best_comment(funds, "calmar", True),
            ),
            "",
            "### 三、回撤控制（近1年）",
            "| 指标 | " + " | ".join(fund["name"] for fund in funds) + " | 行业指数 | 简要评价 |",
            "|------|" + "-------|" * len(funds) + "---------|---------|",
            metric_row(
                "最大回撤",
                [fmt_pct(-fund["max_drawdown"]) for fund in funds],
                fmt_pct(-payload["industry_drawdown"]),
                best_comment(funds, "max_drawdown", False),
            ),
            metric_row(
                "回撤修复天数",
                [fmt_days(fund["repair_days"]) for fund in funds],
                fmt_days(payload["industry_repair"]),
                best_comment(funds, "repair_days", False),
            ),
            metric_row(
                "下跌保护",
                [fmt_pct(fund["downside_protection"]) for fund in funds],
                "0%",
                best_comment(funds, "downside_protection", True),
            ),
            "",
            "### 四、基金经理",
            "| 维度 | "
            + " | ".join(
                f"{fund['name']}({str(fund['manager'].get('name', '') or '样本不足')})"
                for fund in funds
            )
            + " | 评价 |",
            "|------|" + "------------|" * len(funds) + "------|",
            metric_row(
                "任职年限",
                [f"{fund['manager_metrics']['tenure_years']:.1f}年" for fund in funds],
                None,
                best_comment_by_nested(funds, "manager_metrics", "tenure_years", True),
            ),
            metric_row(
                "行业专注度",
                [fmt_focus(fund["manager_metrics"]["industry_focus_ratio"]) for fund in funds],
                None,
                best_comment_by_nested(funds, "manager_metrics", "industry_focus_ratio", True),
            ),
            metric_row(
                "历史业绩",
                [fmt_percentile(fund["manager_metrics"]["history_percentile"]) for fund in funds],
                None,
                best_comment_by_nested(funds, "manager_metrics", "history_percentile", True),
            ),
            metric_row(
                "管理规模",
                [fmt_aum_total(fund["manager_metrics"]["aum_total"]) for fund in funds],
                None,
                best_comment_by_nested(funds, "manager_metrics", "aum_total", True),
            ),
            "",
            "### 五、综合排序",
            "| 排名 | 基金 | 核心优势 | 主要劣势 |",
            "|------|------|---------|---------|",
        ]
    )
    for idx, item in enumerate(rankings, start=1):
        rank_label = f"**{idx}**" if idx == 1 else str(idx)
        fund_label = f"**{item['name']}**" if idx == 1 else item["name"]
        lines.append(f"| {rank_label} | {fund_label} | {item['strength']} | {item['weakness']} |")

    lines.extend(["", f"**数据截止**：{date}", "**免责声明**：本报告仅作客观数据对比，不构成投资建议。"])
    return sanitize_report("\n".join(lines), prohibited_terms)


def metric_row(label: str, values: List[str], baseline: str | None, comment: str) -> str:
    pieces = [label] + values
    if baseline is not None:
        pieces.append(baseline)
    pieces.append(comment)
    return "| " + " | ".join(pieces) + " |"


def best_comment(funds: List[Dict[str, Any]], key: str, positive: bool) -> str:
    values = [(fund["name"], fund.get(key)) for fund in funds if pd.notna(fund.get(key))]
    if not values:
        return "样本不足"
    values.sort(key=lambda item: item[1], reverse=positive)
    return f"{values[0][0]}最优"


def best_comment_by_nested(
    funds: List[Dict[str, Any]], outer: str, inner: str, positive: bool
) -> str:
    values = []
    for fund in funds:
        value = fund.get(outer, {}).get(inner)
        if value is not None and pd.notna(value):
            values.append((fund["name"], value))
    if not values:
        return "样本不足"
    values.sort(key=lambda item: item[1], reverse=positive)
    return f"{values[0][0]}更优"


def sorted_comment(funds: List[Dict[str, Any]], key: str) -> str:
    values = [(fund["name"], fund.get(key)) for fund in funds if pd.notna(fund.get(key))]
    if len(values) != len(funds):
        return "样本不足"
    values.sort(key=lambda item: item[1], reverse=True)
    return ">".join(item[0] for item in values)


def sanitize_report(report: str, prohibited_terms: List[str]) -> str:
    sanitized = report
    for term in prohibited_terms:
        sanitized = sanitized.replace(term, "客观对比")
    return sanitized
