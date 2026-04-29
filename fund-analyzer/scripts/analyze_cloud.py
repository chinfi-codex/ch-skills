#!/usr/bin/env python3
"""临时脚本：分析云计算ETF买点"""

import sys
import os
import io

# 处理Windows编码问题
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from fund_data_fetcher import FundDataFetcher
from buy_point_analyzer import BuyPointAnalyzer


def main():
    fetcher = FundDataFetcher()
    analyzer = BuyPointAnalyzer(fetcher)

    # 使用华夏云计算ETF (516630.SH)
    ts_code = "516630.SH"
    print(f"正在分析基金: {ts_code}")
    print("=" * 50)

    result = analyzer.analyze_buy_point(ts_code)

    if result["success"]:
        print("\n" + "=" * 50)
        print(result["ai_analysis"])
    else:
        print(f"分析失败: {result['message']}")


if __name__ == "__main__":
    main()
