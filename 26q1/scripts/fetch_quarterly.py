#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare 季报数据获取与筛选工具
支持按披露日期、营收规模、净利润、毛利率、净利率、ROE 等多维度筛选
"""

import argparse
import os
import sys
from datetime import datetime
from typing import Optional

import pandas as pd

try:
    import tushare as ts
except ImportError:
    print("错误：未安装 tushare 包。请运行：pip install tushare")
    sys.exit(1)


def init_pro():
    """初始化 Tushare Pro API"""
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        print("错误：未设置 TUSHARE_TOKEN 环境变量。")
        print("请设置环境变量：export TUSHARE_TOKEN=your_token")
        sys.exit(1)
    pro = ts.pro_api(token)
    return pro


def fetch_disclosure_dates(pro, trade_date: str) -> pd.DataFrame:
    """
    获取指定日期的财报披露信息
    
    Args:
        pro: Tushare Pro API 实例
        trade_date: 日期，格式 YYYYMMDD
    
    Returns:
        DataFrame，包含当日披露季报的公司列表
    """
    try:
        df = pro.disclosure_date(
            actual_date=trade_date,
            fields='ts_code,ann_date,end_date,actual_date,modify_date'
        )
        if df is None or df.empty:
            print(f"日期 {trade_date} 无财报披露数据（可能为非交易日或无公司披露）")
            return pd.DataFrame()
        return df
    except Exception as e:
        print(f"获取披露日期数据失败: {e}")
        return pd.DataFrame()


def fetch_income_data(pro, ts_codes: list, end_date: str) -> pd.DataFrame:
    """
    获取利润表数据
    
    Args:
        pro: Tushare Pro API 实例
        ts_codes: 股票代码列表
        end_date: 报告期，格式 YYYYMMDD（如 20260331 表示 2026Q1）
    
    Returns:
        DataFrame，包含营业收入、净利润等数据
    """
    all_data = []
    batch_size = 80
    
    for i in range(0, len(ts_codes), batch_size):
        batch = ts_codes[i:i + batch_size]
        codes_str = ",".join(batch)
        try:
            df = pro.income(
                ts_code=codes_str,
                period=end_date,
                fields='ts_code,ann_date,end_date,total_revenue,revenue,total_profit,net_profit'
            )
            if df is not None and not df.empty:
                all_data.append(df)
        except Exception as e:
            print(f"获取利润表数据失败（批次 {i // batch_size + 1}）: {e}")
    
    if not all_data:
        return pd.DataFrame()
    
    result = pd.concat(all_data, ignore_index=True)
    result = result.drop_duplicates(subset=['ts_code'])
    return result


def fetch_fina_indicators(pro, ts_codes: list, end_date: str) -> pd.DataFrame:
    """
    获取财务指标数据（毛利率、净利率、ROE 等）
    
    Args:
        pro: Tushare Pro API 实例
        ts_codes: 股票代码列表
        end_date: 报告期，格式 YYYYMMDD
    
    Returns:
        DataFrame，包含财务指标数据
    """
    all_data = []
    batch_size = 80
    
    for i in range(0, len(ts_codes), batch_size):
        batch = ts_codes[i:i + batch_size]
        codes_str = ",".join(batch)
        try:
            df = pro.fina_indicator(
                ts_code=codes_str,
                period=end_date,
                fields='ts_code,ann_date,end_date,grossprofit_margin,netprofit_margin,roe,roe_dt,roe_yearly,roe_avg'
            )
            if df is not None and not df.empty:
                all_data.append(df)
        except Exception as e:
            print(f"获取财务指标数据失败（批次 {i // batch_size + 1}）: {e}")
    
    if not all_data:
        return pd.DataFrame()
    
    result = pd.concat(all_data, ignore_index=True)
    result = result.drop_duplicates(subset=['ts_code'])
    return result


def merge_and_filter(
    disclosure_df: pd.DataFrame,
    income_df: pd.DataFrame,
    fina_df: pd.DataFrame,
    min_revenue: Optional[float],
    max_revenue: Optional[float],
    min_net_profit: Optional[float],
    max_net_profit: Optional[float],
    min_gross_margin: Optional[float],
    min_net_margin: Optional[float],
    min_roe: Optional[float],
) -> pd.DataFrame:
    """
    合并数据并按条件筛选
    
    Args:
        disclosure_df: 披露日期数据
        income_df: 利润表数据
        fina_df: 财务指标数据
        min_revenue: 最小营业收入（亿元）
        max_revenue: 最大营业收入（亿元）
        min_net_profit: 最小净利润（亿元）
        max_net_profit: 最大净利润（亿元）
        min_gross_margin: 最小毛利率（%）
        min_net_margin: 最小净利率（%）
        min_roe: 最小 ROE（%）
    
    Returns:
        筛选后的 DataFrame
    """
    merged = disclosure_df[['ts_code', 'ann_date', 'end_date', 'actual_date']].copy()
    
    # 合并利润表数据
    if not income_df.empty:
        merged = merged.merge(income_df, on='ts_code', how='left', suffixes=('', '_income'))
    
    # 合并财务指标数据
    if not fina_df.empty:
        merged = merged.merge(fina_df, on='ts_code', how='left', suffixes=('', '_fina'))
    
    # 单位转换：原始数据为元，转换为亿元便于阅读
    if 'total_revenue' in merged.columns:
        merged['total_revenue_yi'] = merged['total_revenue'] / 1e8
    if 'net_profit' in merged.columns:
        merged['net_profit_yi'] = merged['net_profit'] / 1e8
    
    # 应用筛选条件
    before_count = len(merged)
    
    if min_revenue is not None and 'total_revenue_yi' in merged.columns:
        merged = merged[merged['total_revenue_yi'] >= min_revenue]
    
    if max_revenue is not None and 'total_revenue_yi' in merged.columns:
        merged = merged[merged['total_revenue_yi'] <= max_revenue]
    
    if min_net_profit is not None and 'net_profit_yi' in merged.columns:
        merged = merged[merged['net_profit_yi'] >= min_net_profit]
    
    if max_net_profit is not None and 'net_profit_yi' in merged.columns:
        merged = merged[merged['net_profit_yi'] <= max_net_profit]
    
    if min_gross_margin is not None and 'grossprofit_margin' in merged.columns:
        merged = merged[merged['grossprofit_margin'] >= min_gross_margin]
    
    if min_net_margin is not None and 'netprofit_margin' in merged.columns:
        merged = merged[merged['netprofit_margin'] >= min_net_margin]
    
    if min_roe is not None and 'roe' in merged.columns:
        merged = merged[merged['roe'] >= min_roe]
    
    after_count = len(merged)
    print(f"筛选前: {before_count} 条，筛选后: {after_count} 条")
    
    return merged


def format_output(df: pd.DataFrame) -> pd.DataFrame:
    """格式化输出字段，保留核心可读列"""
    core_cols = [
        'ts_code', 'ann_date', 'end_date',
        'total_revenue_yi', 'net_profit_yi',
        'grossprofit_margin', 'netprofit_margin', 'roe'
    ]
    
    available_cols = [c for c in core_cols if c in df.columns]
    out = df[available_cols].copy()
    
    rename_map = {
        'ts_code': '股票代码',
        'ann_date': '公告日期',
        'end_date': '报告期',
        'total_revenue_yi': '营业收入(亿元)',
        'net_profit_yi': '净利润(亿元)',
        'grossprofit_margin': '毛利率(%)',
        'netprofit_margin': '净利率(%)',
        'roe': 'ROE(%)',
    }
    out = out.rename(columns={k: v for k, v in rename_map.items() if k in out.columns})
    
    # 数值格式化
    for col in out.columns:
        if '亿元' in col:
            out[col] = out[col].round(2)
        elif '%' in col:
            out[col] = out[col].round(2)
    
    return out


def main():
    parser = argparse.ArgumentParser(description='Tushare 季报数据获取与筛选')
    parser.add_argument('--date', type=str, default=datetime.now().strftime('%Y%m%d'),
                        help='披露日期，格式 YYYYMMDD，默认今日')
    parser.add_argument('--period', type=str, default='20260331',
                        help='报告期，格式 YYYYMMDD，默认 2026Q1（20260331）')
    parser.add_argument('--min-revenue', type=float, default=None,
                        help='最小营业收入（亿元）')
    parser.add_argument('--max-revenue', type=float, default=None,
                        help='最大营业收入（亿元）')
    parser.add_argument('--min-net-profit', type=float, default=None,
                        help='最小净利润（亿元）')
    parser.add_argument('--max-net-profit', type=float, default=None,
                        help='最大净利润（亿元）')
    parser.add_argument('--min-gross-margin', type=float, default=None,
                        help='最小毛利率（%%）')
    parser.add_argument('--min-net-margin', type=float, default=None,
                        help='最小净利率（%%）')
    parser.add_argument('--min-roe', type=float, default=None,
                        help='最小 ROE（%%）')
    parser.add_argument('--output', type=str, default=None,
                        help='输出 CSV 文件路径')
    parser.add_argument('--all-codes', action='store_true',
                        help='不限制披露日期，拉取全市场该报告期数据（注意积分消耗）')
    
    args = parser.parse_args()
    
    print(f"=" * 50)
    print(f"Tushare 季报筛选工具")
    print(f"披露日期: {args.date}")
    print(f"报告期: {args.period}")
    print(f"=" * 50)
    
    pro = init_pro()
    
    if args.all_codes:
        # 获取全市场股票列表
        print("获取全市场股票列表...")
        stock_basic = pro.stock_basic(exchange='', list_status='L',
                                       fields='ts_code,name,industry')
        if stock_basic is None or stock_basic.empty:
            print("获取股票列表失败")
            sys.exit(1)
        ts_codes = stock_basic['ts_code'].tolist()
        disclosure_df = stock_basic.copy()
        disclosure_df['ann_date'] = None
        disclosure_df['end_date'] = args.period
        disclosure_df['actual_date'] = None
        print(f"全市场股票数: {len(ts_codes)}")
    else:
        # 按披露日期获取
        disclosure_df = fetch_disclosure_dates(pro, args.date)
        if disclosure_df.empty:
            sys.exit(0)
        
        # 筛选出季度报告（end_date 为季度末）
        disclosure_df = disclosure_df[disclosure_df['end_date'] == args.period]
        if disclosure_df.empty:
            print(f"日期 {args.date} 无 {args.period} 报告期的季报披露")
            sys.exit(0)
        
        ts_codes = disclosure_df['ts_code'].tolist()
        print(f"当日披露 {args.period} 季报的公司数: {len(ts_codes)}")
    
    # 获取利润表数据
    print("获取利润表数据...")
    income_df = fetch_income_data(pro, ts_codes, args.period)
    print(f"利润表数据: {len(income_df)} 条")
    
    # 获取财务指标数据
    print("获取财务指标数据...")
    fina_df = fetch_fina_indicators(pro, ts_codes, args.period)
    print(f"财务指标数据: {len(fina_df)} 条")
    
    # 合并与筛选
    filtered = merge_and_filter(
        disclosure_df, income_df, fina_df,
        min_revenue=args.min_revenue,
        max_revenue=args.max_revenue,
        min_net_profit=args.min_net_profit,
        max_net_profit=args.max_net_profit,
        min_gross_margin=args.min_gross_margin,
        min_net_margin=args.min_net_margin,
        min_roe=args.min_roe,
    )
    
    if filtered.empty:
        print("筛选后无数据。")
        sys.exit(0)
    
    # 格式化输出
    out_df = format_output(filtered)
    
    print("\n筛选结果预览：")
    print(out_df.to_string(index=False))
    
    # 输出到文件
    if args.output:
        out_df.to_csv(args.output, index=False, encoding='utf-8-sig')
        print(f"\n结果已保存至: {args.output}")
    else:
        default_name = f"quarterly_{args.period}_{args.date}.csv"
        out_df.to_csv(default_name, index=False, encoding='utf-8-sig')
        print(f"\n结果已保存至: {default_name}")
    
    # 输出统计摘要
    print("\n统计摘要：")
    if '营业收入(亿元)' in out_df.columns:
        print(f"  营业收入区间: {out_df['营业收入(亿元)'].min():.2f} - {out_df['营业收入(亿元)'].max():.2f} 亿元")
    if '净利润(亿元)' in out_df.columns:
        print(f"  净利润区间: {out_df['净利润(亿元)'].min():.2f} - {out_df['净利润(亿元)'].max():.2f} 亿元")
    if '毛利率(%)' in out_df.columns:
        print(f"  毛利率区间: {out_df['毛利率(%)'].min():.2f}% - {out_df['毛利率(%)'].max():.2f}%")
    if '净利率(%)' in out_df.columns:
        print(f"  净利率区间: {out_df['净利率(%)'].min():.2f}% - {out_df['净利率(%)'].max():.2f}%")
    if 'ROE(%)' in out_df.columns:
        print(f"  ROE区间: {out_df['ROE(%)'].min():.2f}% - {out_df['ROE(%)'].max():.2f}%")


if __name__ == '__main__':
    main()
