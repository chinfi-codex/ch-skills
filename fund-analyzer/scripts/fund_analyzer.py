#!/usr/bin/env python3
"""
公募基金智能分析助手
支持指定基金买点和优质基金推荐
"""

import sys
import os
from typing import Optional, Dict, Any, List

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fund_data_fetcher import FundDataFetcher
from buy_point_analyzer import BuyPointAnalyzer
from industry_compare_analyzer import IndustryCompareAnalyzer


def search_and_confirm_fund(
    fetcher: FundDataFetcher, query: str
) -> Optional[Dict[str, Any]]:
    """搜索并确认基金"""
    print(f"[搜索] 正在搜索基金: {query}...")

    results = fetcher.search_fund(query)

    if not results:
        return None

    if len(results) == 1:
        return results[0]

    print(f"[提示] 找到多个匹配的基金，请选择：")
    for i, r in enumerate(results, 1):
        print(
            f"  {i}. {r.get('name', '未知')} ({r.get('ts_code', '')})"
        )

    while True:
        try:
            choice = input("请输入序号选择基金（直接回车退出）: ").strip()
            if not choice:
                return None
            idx = int(choice) - 1
            if 0 <= idx < len(results):
                return results[idx]
            print("无效序号，请重新输入")
        except ValueError:
            print("请输入有效序号")


def recommend_funds(fetcher: FundDataFetcher) -> str:
    """推荐优质基金"""
    print("[推荐] 正在筛选优质基金...")

    all_funds = fetcher.get_all_funds()
    if all_funds.empty:
        return "获取基金列表失败"

    candidate_df = all_funds.copy()
    candidate_df = candidate_df[
        candidate_df["fund_type"].isin(["股票型", "混合型"])
        | candidate_df["invest_type"].isin(["偏股混合型", "灵活配置型", "混合型"])
    ]
    candidate_df = candidate_df[~candidate_df["invest_type"].astype(str).str.contains("指数", na=False)]
    candidate_df["found_date"] = pd.to_datetime(candidate_df["found_date"], format="%Y%m%d", errors="coerce")
    candidate_df = candidate_df[candidate_df["found_date"].notna()]
    candidate_df = candidate_df[candidate_df["found_date"] <= (pd.Timestamp.now() - pd.Timedelta(days=365))]
    candidate_df = candidate_df[candidate_df["issue_amount"].notna()]
    candidate_df = candidate_df[(candidate_df["issue_amount"] >= 2.0) & (candidate_df["issue_amount"] <= 50.0)]
    candidate_df = candidate_df.sort_values(["issue_amount", "found_date"], ascending=[False, True]).head(120)

    recommended = []

    for _, fund in candidate_df.iterrows():
        ts_code = fund.get("ts_code", "")
        if not ts_code:
            continue

        try:
            issue_amount = fund.get("issue_amount", 0)
            try:
                total_asset_yi = float(issue_amount)
            except (TypeError, ValueError):
                continue
            if total_asset_yi < 2 or total_asset_yi > 50:
                continue

            manager_df = fetcher.get_fund_manager(ts_code)
            if manager_df is None or manager_df.empty:
                continue

            manager = manager_df.iloc[0]
            manager_name = manager.get("name", "")
            work_time = manager.get("work_time", 0)
            try:
                manager_tenure = float(work_time)
            except (TypeError, ValueError):
                manager_tenure = 0.0
            if manager_tenure < 1:
                continue

            nav_df = fetcher.get_fund_nav(ts_code, count=500)
            if nav_df is None or nav_df.empty or len(nav_df) < 250:
                continue

            nav_series = nav_df["nav"].dropna()
            if nav_series.empty:
                continue

            current_nav = nav_series.iloc[-1]
            year_ago_nav = (
                nav_series.iloc[-250] if len(nav_series) >= 250 else nav_series.iloc[0]
            )
            three_years_ago_nav = (
                nav_series.iloc[0] if len(nav_series) >= 750 else nav_series.iloc[0]
            )

            annual_return_1y = (
                (current_nav / year_ago_nav - 1) * 100 if year_ago_nav > 0 else 0
            )

            nav_df_3y = fetcher.get_fund_nav(ts_code, count=750)
            if nav_df_3y is not None and not nav_df_3y.empty:
                three_years_ago_nav = nav_df_3y["nav"].iloc[0]
                annual_return_3y = (
                    (current_nav / three_years_ago_nav - 1) / 3 * 100
                    if three_years_ago_nav > 0
                    else 0
                )
            else:
                annual_return_3y = 0

            returns = [annual_return_1y, annual_return_3y]
            if all(r > 0 for r in returns):
                recommended.append(
                    {
                        "ts_code": ts_code,
                        "name": fund.get("name", ""),
                        "manager_name": manager_name,
                        "manager_tenure": manager_tenure,
                        "scale": total_asset_yi,
                        "annual_return_1y": annual_return_1y,
                        "annual_return_3y": annual_return_3y,
                        "current_nav": current_nav,
                    }
                )

        except Exception as e:
            continue

    recommended.sort(
        key=lambda x: (x["annual_return_1y"] + x["annual_return_3y"]) / 2, reverse=True
    )
    recommended = recommended[:20]

    if not recommended:
        return "未找到符合条件的优质基金"

    lines = []
    lines.append("【优质基金推荐】")
    lines.append("=" * 70)
    lines.append("\n筛选标准：")
    lines.append("- 基金规模：2亿-50亿元")
    lines.append("- 基金经理：任期≥1年")
    lines.append("- 业绩要求：近1年、3年收益均大于0")
    lines.append("\n")
    lines.append(
        f"{'基金名称':<20} {'基金经理':<8} {'任期':>5} {'规模':>8} {'1年收益':>8} {'3年收益':>8}"
    )
    lines.append("-" * 70)

    for fund in recommended:
        lines.append(
            f"{fund['name']:<20} {fund['manager_name']:<8} "
            f"{fund['manager_tenure']:>3}年 {fund['scale']:>7.1f}亿 "
            f"{fund['annual_return_1y']:>7.1f}% {fund['annual_return_3y']:>7.1f}%"
        )

    lines.append("\n" + "=" * 70)
    lines.append("⚠️ 仅供参考，不构成投资建议。基金投资有风险，入市需谨慎。")

    return "\n".join(lines)


def buy_point_analysis(fetcher: FundDataFetcher, query: str) -> str:
    """执行买点分析"""
    fund = search_and_confirm_fund(fetcher, query)
    if fund is None:
        return f"❌ 未找到基金: {query}\n请检查基金名称或代码是否正确。"

    ts_code = fund.get("ts_code", "")
    fund_name = fund.get("name", "")

    print(f"[成功] 选择基金: {fund_name} ({ts_code})")
    print("[分析] 正在获取数据并分析买点...\n")

    analyzer = BuyPointAnalyzer(fetcher)
    result = analyzer.analyze_buy_point(ts_code)

    report = analyzer.format_buy_point_report(result)
    return report


def compare_industry_funds(fetcher: FundDataFetcher, industry_term: str) -> str:
    """Execute industry fund comparison."""
    print(f"[对比] 正在分析行业基金: {industry_term}...\n")
    analyzer = IndustryCompareAnalyzer(fetcher)
    return analyzer.compare(industry_term)


def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("""
╔══════════════════════════════════════════════════════════════╗
║           公募基金智能分析助手                                ║
╚══════════════════════════════════════════════════════════════╝

使用方法:
    python fund_analyzer.py analyze <基金名称/代码>   # 买点分析
    python fund_analyzer.py compare-industry <行业词> # 行业基金对比
    python fund_analyzer.py recommend                # 推荐优质基金

示例:
    python fund_analyzer.py analyze 景顺长城新兴成长混合
    python fund_analyzer.py analyze 260101
    python fund_analyzer.py compare-industry 医药
    python fund_analyzer.py recommend
""")
        sys.exit(1)

    command = sys.argv[1].lower()

    try:
        fetcher = FundDataFetcher()

        if command == "analyze" and len(sys.argv) >= 3:
            query = " ".join(sys.argv[2:])
            result = buy_point_analysis(fetcher, query)
            print(result)

        elif command in {"compare-industry", "industry-compare"} and len(sys.argv) >= 3:
            query = " ".join(sys.argv[2:])
            result = compare_industry_funds(fetcher, query)
            print(result)

        elif command == "recommend":
            result = recommend_funds(fetcher)
            print(result)

        else:
            print(f"❌ 未知的命令: {command}")
            print("支持: analyze, compare-industry, industry-compare, recommend")
            sys.exit(1)

    except RuntimeError as e:
        print(f"❌ 配置错误: {e}")
        print("请确保已设置 TUSHARE_TOKEN 环境变量或在 .env 文件中配置")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n已取消操作")
        sys.exit(0)
    except Exception as e:
        print(f"❌ 发生错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    import io
    import sys

    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    main()
