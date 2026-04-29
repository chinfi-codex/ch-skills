#!/usr/bin/env python3
"""
Fund buy-point analysis orchestrator.
"""

from typing import Any, Dict

import pandas as pd

from fund_data_fetcher import FundDataFetcher
from buy_point_reporting import build_ai_analysis, format_buy_point_report
from buy_point_rules import (
    analyze_buy_signals,
    build_suggestion,
    calculate_risk_levels,
    process_market_data,
    process_nav_data,
)


class BuyPointAnalyzer:
    """基金买点分析器"""

    def __init__(self, fetcher: FundDataFetcher):
        self.fetcher = fetcher

    def analyze_buy_point(self, ts_code: str) -> Dict[str, Any]:
        result = {
            "fund_code": ts_code,
            "success": False,
            "message": "",
            "fund_info": {},
            "nav_data": {},
            "market_data": {},
            "buy_signals": {},
            "risk_levels": {},
            "suggestion": {},
            "ai_analysis": "",
        }

        fund_info = self.fetcher.get_fund_info(ts_code)
        if fund_info is None:
            result["message"] = "获取基金信息失败"
            return result
        result["fund_info"] = fund_info

        nav_df = self.fetcher.get_fund_nav(ts_code, count=520)
        if nav_df is None or nav_df.empty or len(nav_df) < 260:
            result["message"] = "基金净值样本不足，无法完成双模式分析"
            return result

        market_df = self.fetcher.get_shanghai_index(count=520)
        if market_df is None or market_df.empty or len(market_df) < 260:
            result["message"] = "上证指数数据不足，无法完成双模式分析"
            return result

        result["nav_data"] = process_nav_data(nav_df)
        result["market_data"] = process_market_data(market_df)
        result["buy_signals"] = analyze_buy_signals(result["nav_data"], result["market_data"])
        result["risk_levels"] = calculate_risk_levels(result["nav_data"])
        result["suggestion"] = build_suggestion(result["buy_signals"])
        result["ai_analysis"] = build_ai_analysis(self.fetcher, nav_df, result)
        result["success"] = True
        result["message"] = "分析完成"
        return result

    def format_buy_point_report(self, analysis: Dict[str, Any]) -> str:
        return format_buy_point_report(self.fetcher, analysis)


if __name__ == "__main__":
    pass
