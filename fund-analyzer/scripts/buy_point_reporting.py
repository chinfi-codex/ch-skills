#!/usr/bin/env python3
"""
Reporting helpers for fund buy-point analysis.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from ai_client import call_ai, clean_ai_output
from buy_point_rules import fmt_float


def build_ai_analysis(fetcher: Any, nav_df: pd.DataFrame, analysis: Dict[str, Any]) -> str:
    fund_info = analysis.get("fund_info", {})
    nav_data = analysis.get("nav_data", {})
    market_data = analysis.get("market_data", {})
    left_mode = analysis.get("buy_signals", {}).get("left_mode", {})
    right_mode = analysis.get("buy_signals", {}).get("right_mode", {})
    overall = analysis.get("buy_signals", {}).get("overall", {})
    risk_levels = analysis.get("risk_levels", {})

    prompt = f"""你是一位专业的基金投资分析师，请根据以下已计算好的规则结果，对【{fund_info.get('name', '未知基金')}】进行双模式买点分析总结。

## 基金信息
- 基金代码：{analysis.get('fund_code', '')}
- 基金名称：{fund_info.get('name', '未知基金')}
- 管理人：{fund_info.get('management', '未知')}
- 托管人：{fund_info.get('custodian', '未知')}

## 当前净值位置
- 当前净值：{nav_data.get('current_nav', 0):.4f}
- 近期高点：{nav_data.get('recent_high', 0):.4f}
- 当前折扣率：{nav_data.get('decline_ratio', 0):.2f}
- 距高点回撤：{nav_data.get('decline_pct', 0):+.2f}%
- 回撤交易日：{nav_data.get('decline_days', 0)}
- MA5/MA20/MA60/MA200：{fmt_float(nav_data.get('ma5'))}/{fmt_float(nav_data.get('ma20'))}/{fmt_float(nav_data.get('ma60'))}/{fmt_float(nav_data.get('ma200'))}
- 20月线：{fmt_float(nav_data.get('ma20_monthly'))}

## 市场环境
- 上证收盘：{market_data.get('current_close', 0):.2f}
- 上证 MA20 / MA200：{fmt_float(market_data.get('ma20'))} / {fmt_float(market_data.get('ma200'))}
- 成交量收缩比：{market_data.get('contraction_ratio', 0):.2f}
- 5日量能比：{market_data.get('vol_ratio_5', 0):.2f}
- RSI(14)：{fmt_float(market_data.get('rsi14'))}
- MACD柱：{fmt_float(market_data.get('macd_hist'))}
- 市场形态：{market_data.get('stabilization_k', '其他')}

## 左侧模式结果
- 阶段：{left_mode.get('stage', '条件不足')}
- 概要：{left_mode.get('summary', '')}
- 通过条件数：{left_mode.get('passed_count', 0)}

## 右侧模式结果
- 阶段：{right_mode.get('stage', '条件不足')}
- 概要：{right_mode.get('summary', '')}
- 通过条件数：{right_mode.get('passed_count', 0)}

## 综合判断
- 主导模式：{overall.get('dominant_mode', '双模式均不成立')}
- 操作建议：{overall.get('action', '放弃')}
- 仓位参考：{overall.get('position_hint', '0%')}
- 理由：{overall.get('rationale', '')}

## 风险位
- 左侧止损：{fmt_float(risk_levels.get('left_stop'))}
- 右侧止损：{fmt_float(risk_levels.get('right_stop'))}
- 参考止盈：{fmt_float(risk_levels.get('take_profit'))}

## 最近20个交易日净值
"""
    nav_series = pd.to_numeric(nav_df["nav"], errors="coerce").dropna().tail(20)
    for idx, value in nav_series.items():
        prompt += f"{idx.strftime('%Y-%m-%d')}: {value:.4f}\n"

    prompt += """
请按以下要求输出：
1. 先给一句话结论，明确当前更接近左侧、右侧还是继续等待。
2. 分别解释左侧和右侧成立或不成立的关键原因。
3. 用审慎措辞概括接下来最值得观察的信号。
4. 不要虚构未给出的数据，不要用“稳赚”“强烈建议满仓”之类表述。
"""
    return clean_ai_output(call_ai(prompt))


def format_buy_point_report(fetcher: Any, analysis: Dict[str, Any]) -> str:
    if not analysis.get("success", False):
        return f"分析失败: {analysis.get('message', '未知错误')}"

    fund_info = analysis.get("fund_info", {})
    nav_data = analysis.get("nav_data", {})
    market_data = analysis.get("market_data", {})
    buy_signals = analysis.get("buy_signals", {})
    left_mode = buy_signals.get("left_mode", {})
    right_mode = buy_signals.get("right_mode", {})
    overall = buy_signals.get("overall", {})
    risk_levels = analysis.get("risk_levels", {})
    suggestion = analysis.get("suggestion", {})

    fund_name = fund_info.get("name", "未知基金")
    ts_code = fund_info.get("ts_code", "")
    lines: List[str] = [f"【基金买点分析】{fund_name}", "=" * 60]

    dominant_mode = overall.get("dominant_mode", "双模式均不成立")
    stage = resolve_stage(left_mode, right_mode, overall)
    lines.append("")
    lines.append(f"一句话判断：当前更接近“{stage}”阶段，主导逻辑为“{dominant_mode}”，原因是：{overall.get('rationale', '暂无')}")
    lines.append("")
    lines.append("## 基本信息")
    lines.append(f"- 基金代码：{ts_code}")
    aum = fetcher.get_fund_aum(ts_code)
    if aum is not None:
        lines.append(f"- 基金规模：{aum:.2f} 亿元")
    manager_df = fetcher.get_current_fund_managers(ts_code)
    if manager_df is not None and not manager_df.empty:
        manager = manager_df.iloc[0]
        manager_name = manager.get("name", "未知")
        tenure = manager_tenure_years(manager)
        lines.append(f"- 基金经理：{manager_name}（任职 {tenure:.1f} 年）")
        background = get_manager_background(fetcher, ts_code, manager_name, fund_info.get("management", ""))
        if background:
            lines.append(f"- 管理人背景：{background}")

    lines.append("")
    lines.append("## 当前市场位置")
    lines.append(f"- 当前净值：{nav_data.get('current_nav', 0):.4f}")
    lines.append(f"- 前高净值：{nav_data.get('recent_high', 0):.4f}")
    lines.append(f"- 当前折扣率：{nav_data.get('decline_ratio', 0):.2f}")
    lines.append(f"- 回撤幅度：{nav_data.get('decline_pct', 0):+.2f}%")
    lines.append(f"- 回撤交易日：{nav_data.get('decline_days', 0)}")
    lines.append(f"- MA20 / MA60 / MA200：{fmt_float(nav_data.get('ma20'))} / {fmt_float(nav_data.get('ma60'))} / {fmt_float(nav_data.get('ma200'))}")
    lines.append(f"- 20月线：{fmt_float(nav_data.get('ma20_monthly'))}")
    lines.append(f"- 上证位置：{market_data.get('current_close', 0):.2f}，MA20 {fmt_float(market_data.get('ma20'))}，MA200 {fmt_float(market_data.get('ma200'))}")
    lines.append(f"- 市场量能：收缩比 {market_data.get('contraction_ratio', 0):.2f}，5日量能比 {market_data.get('vol_ratio_5', 0):.2f}")
    lines.append(f"- 市场指标：RSI {fmt_float(market_data.get('rsi14'))}，MACD柱 {fmt_float(market_data.get('macd_hist'))}，形态 {market_data.get('stabilization_k', '其他')}")

    lines.append("")
    lines.append("## 模式一：左侧折扣买点判断")
    lines.extend(render_mode_checks(left_mode, [
        ("space_discount", "空间折扣"),
        ("time_adjustment", "时间调整"),
        ("slope_repair", "净值斜率"),
        ("market_below_ma200", "大盘位置"),
        ("market_volume_contraction", "大盘量能收缩"),
        ("market_stabilization", "大盘企稳形态"),
        ("market_rsi", "大盘 RSI"),
    ]))
    lines.append(f"- 左侧结论：{left_mode.get('stage', '条件不足')}")
    lines.append(f"- 解释：{left_mode.get('summary', '')}")

    lines.append("")
    lines.append("## 模式二：右侧突破回踩买点判断")
    lines.extend(render_mode_checks(right_mode, [
        ("trend_breakout", "趋势突破"),
        ("multi_ma_alignment", "均线多头排列"),
        ("pullback_range", "回踩幅度"),
        ("pullback_support", "回踩支撑"),
        ("slope_maintained", "斜率维持"),
        ("market_trend_background", "大盘趋势背景"),
        ("pullback_window", "回踩时间窗"),
        ("market_stabilization", "大盘止跌形态"),
        ("market_support_valid", "市场支撑有效"),
        ("washout_volume", "缩量洗盘"),
        ("market_rsi", "大盘 RSI"),
    ]))
    lines.append(f"- 右侧结论：{right_mode.get('stage', '条件不足')}")
    lines.append(f"- 解释：{right_mode.get('summary', '')}")

    lines.append("")
    lines.append("## 综合判断")
    lines.append(f"- 当前主导模式：{dominant_mode}")
    lines.append(f"- 当前操作建议：{overall.get('action', '放弃')}")
    lines.append(f"- 仓位参考：{overall.get('position_hint', '0%')}")
    lines.append(f"- 触发原因：{overall.get('rationale', '')}")

    lines.append("")
    lines.append("## 操作建议")
    action = overall.get("action")
    if action == "分批建仓":
        lines.append("- 建仓方式：分批执行，不鼓励一次性重仓。")
    elif action == "试仓":
        lines.append("- 试仓方式：仅用小仓位试探，前提是市场企稳信号不被破坏。")
    elif action in {"等待", "跟踪"}:
        lines.append("- 当前以等待和跟踪为主，重点观察以下信号：")
    else:
        lines.append("- 当前以放弃为主，直到关键条件重新建立。")
    for item in suggestion.get("waiting_for", [])[:4]:
        lines.append(f"- {item}")

    lines.append("")
    lines.append("## 风险与退出")
    lines.append(f"- 左侧止损：{fmt_float(risk_levels.get('left_stop'))}，或长时间未修复。")
    lines.append(f"- 右侧止损：{fmt_float(risk_levels.get('right_stop'))}。")
    lines.append(f"- 止盈思路：参考位 {fmt_float(risk_levels.get('take_profit'))}，或跌破 {fmt_float(risk_levels.get('trailing_take_profit'))} 后移动止盈。")
    lines.append("- 风险提示：历史净值不代表未来表现，行业与市场风格切换会显著影响判断。")

    ai_analysis = analysis.get("ai_analysis", "").strip()
    if ai_analysis and not ai_analysis.startswith("[未配置AI模型]"):
        lines.append("")
        lines.append("## AI 分析补充")
        lines.append(ai_analysis)

    lines.append("")
    lines.append("=" * 60)
    lines.append("⚠️ 仅供研究参考，不构成投资建议。基金投资有风险，决策需谨慎。")
    return "\n".join(lines)


def render_mode_checks(mode_payload: Dict[str, Any], ordered_keys: List[tuple]) -> List[str]:
    checks = mode_payload.get("checks", {})
    return [
        f"- {label}：{'通过' if checks.get(key, {}).get('passed') else '未通过'}；{checks.get(key, {}).get('detail', '无')}"
        for key, label in ordered_keys
    ]


def resolve_stage(left_mode: Dict[str, Any], right_mode: Dict[str, Any], overall: Dict[str, Any]) -> str:
    dominant_mode = overall.get("dominant_mode", "双模式均不成立")
    if dominant_mode.startswith("左侧"):
        return left_mode.get("stage", "继续等待")
    if dominant_mode == "右侧":
        return right_mode.get("stage", "继续等待")
    return overall.get("action", "继续等待")


def get_manager_background(fetcher: Any, ts_code: str, manager_name: str, management: str = "") -> str:
    try:
        manager_df = fetcher.get_current_fund_managers(ts_code)
        if manager_df is None or manager_df.empty:
            return ""
        manager = manager_df.iloc[0]
        actual_name = str(manager.get("name", "") or "").strip() or manager_name or "未知"
        company = management or "该基金管理人旗下"
        tenure = manager_tenure_years(manager)
        summary = summarize_manager_resume(str(manager.get("resume", "") or "").strip())
        profile = f"{company}{actual_name}，任职 {tenure:.1f} 年"
        if summary:
            profile += f"。履历上，{summary}"
        return profile
    except Exception:
        return ""


def summarize_manager_resume(resume: str, max_length: int = 140) -> str:
    text = " ".join(resume.replace("\r", " ").replace("\n", " ").split())
    if not text or text.lower() == "nan":
        return ""
    text = text.replace("；", "。").replace(";", "。")
    parts = [part.strip(" ，,") for part in text.split("。") if part.strip(" ，,")]
    summary = "；".join(parts[:2])
    if len(summary) > max_length:
        summary = summary[: max_length - 3].rstrip("；，, ") + "..."
    return summary


def manager_tenure_years(manager_row: pd.Series) -> float:
    begin_date = manager_row.get("begin_date")
    if pd.isna(begin_date):
        return 0.0
    begin_dt = pd.to_datetime(begin_date)
    return max((pd.Timestamp.now() - begin_dt).days / 365.25, 0.0)
