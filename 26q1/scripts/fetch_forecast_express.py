#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获取业绩预告与快报原始数据，合并多源信息，输出结构化数据表。
职责边界：仅做数据获取、合并与单位转换，不做业绩分类、归因分析或报告生成。
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

try:
    import tushare as ts
except ImportError:
    print("错误：未安装 tushare 包。请运行：pip install tushare")
    sys.exit(1)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data" / "raw"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def init_pro():
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        print("错误：未设置 TUSHARE_TOKEN 环境变量。")
        sys.exit(1)
    return ts.pro_api(token)


def fetch_forecast(pro, ann_date: str) -> pd.DataFrame:
    """获取业绩预告数据"""
    try:
        df = pro.forecast(
            ann_date=ann_date,
            fields='ts_code,ann_date,end_date,type,p_change_min,p_change_max,'
                   'net_profit_min,net_profit_max,last_parent_net,first_ann_date,'
                   'summary,change_reason'
        )
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        print(f"获取业绩预告失败: {e}")
        return pd.DataFrame()


def fetch_express(pro, ann_date: str) -> pd.DataFrame:
    """获取业绩快报数据"""
    try:
        df = pro.express(
            ann_date=ann_date,
            fields='ts_code,ann_date,end_date,revenue,operate_profit,total_profit,'
                   'n_income,total_assets,total_hldr_eqy_exc_min_int,'
                   'diluted_eps,diluted_roe,yoy_net_profit,bps,perf_summary'
        )
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        print(f"获取业绩快报失败: {e}")
        return pd.DataFrame()


def fetch_stock_basic(pro) -> pd.DataFrame:
    """获取股票基础信息"""
    try:
        df = pro.stock_basic(
            exchange='', list_status='L',
            fields='ts_code,name,industry,market'
        )
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        print(f"获取股票基础信息失败: {e}")
        return pd.DataFrame()


def fetch_daily_basic(pro, trade_date: str) -> pd.DataFrame:
    """获取每日指标（市值等）"""
    try:
        df = pro.daily_basic(
            trade_date=trade_date,
            fields='ts_code,circ_mv,total_mv'
        )
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        print(f"获取每日指标失败: {e}")
        return pd.DataFrame()


def merge_data(
    forecast_df: pd.DataFrame,
    express_df: pd.DataFrame,
    stock_df: pd.DataFrame,
    daily_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    合并多源数据，构建统一数据表。
    优先级：快报 > 预告（快报数字更准）
    """
    codes_forecast = set(forecast_df['ts_code']) if not forecast_df.empty else set()
    codes_express = set(express_df['ts_code']) if not express_df.empty else set()
    all_codes = list(codes_forecast | codes_express)

    if not all_codes:
        return pd.DataFrame()

    records = []
    for code in all_codes:
        rec = {'ts_code': code}

        if code in codes_forecast:
            row = forecast_df[forecast_df['ts_code'] == code].iloc[0]
            rec['ann_date'] = row.get('ann_date')
            rec['end_date'] = row.get('end_date')
            rec['type'] = row.get('type')
            rec['p_change_min'] = row.get('p_change_min')
            rec['p_change_max'] = row.get('p_change_max')
            rec['net_profit_min'] = row.get('net_profit_min')
            rec['net_profit_max'] = row.get('net_profit_max')
            rec['last_parent_net'] = row.get('last_parent_net')
            rec['summary'] = row.get('summary')
            rec['change_reason'] = row.get('change_reason')
            rec['data_source'] = 'forecast'

        if code in codes_express:
            row = express_df[express_df['ts_code'] == code].iloc[0]
            rec['ann_date'] = row.get('ann_date')
            rec['end_date'] = row.get('end_date')
            rec['revenue'] = row.get('revenue')
            rec['n_income'] = row.get('n_income')
            rec['diluted_roe'] = row.get('diluted_roe')
            rec['yoy_net_profit'] = row.get('yoy_net_profit')
            rec['perf_summary'] = row.get('perf_summary')
            rec['data_source'] = 'express'
            if code in codes_forecast:
                rec['data_source'] = 'express+forecast'

        records.append(rec)

    df = pd.DataFrame(records)

    if not stock_df.empty:
        df = df.merge(stock_df, on='ts_code', how='left')

    if not daily_df.empty:
        df = df.merge(daily_df, on='ts_code', how='left')
        df['total_mv_yi'] = df['total_mv'] / 1e4

    # 计算统一净利润增速（优先快报，否则用预报中值）
    df['profit_yoy'] = df.get('yoy_net_profit')
    mask = df['profit_yoy'].isna() & df['p_change_min'].notna() & df['p_change_max'].notna()
    df.loc[mask, 'profit_yoy'] = (df.loc[mask, 'p_change_min'] + df.loc[mask, 'p_change_max']) / 2

    # 单位转换：元 -> 亿元
    for col in ['net_profit_min', 'net_profit_max', 'last_parent_net', 'n_income']:
        if col in df.columns:
            df[f'{col}_yi'] = df[col] / 1e8

    if 'revenue' in df.columns:
        df['revenue_yi'] = df['revenue'] / 1e8

    return df


def load_baseline_data() -> pd.DataFrame:
    baseline_path = DATA_DIR / "baseline.json"
    if baseline_path.exists():
        with open(baseline_path, 'r', encoding='utf-8') as f:
            records = json.load(f)
        return pd.DataFrame(records)
    return pd.DataFrame()


def save_baseline_data(df: pd.DataFrame):
    baseline_path = DATA_DIR / "baseline.json"
    core_cols = ['ts_code', 'ann_date', 'end_date', 'name', 'industry',
                 'type', 'change_reason', 'summary', 'perf_summary',
                 'profit_yoy', 'p_change_min', 'p_change_max',
                 'data_source', 'total_mv_yi', 'revenue_yi', 'n_income_yi']
    save_df = df[[c for c in core_cols if c in df.columns]].copy()
    save_df = save_df.where(pd.notna(save_df), None)
    records = save_df.to_dict('records')
    with open(baseline_path, 'w', encoding='utf-8') as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def merge_daily_to_baseline(daily_df: pd.DataFrame) -> pd.DataFrame:
    baseline_df = load_baseline_data()
    if baseline_df.empty:
        return daily_df.copy()
    combined = pd.concat([baseline_df, daily_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=['ts_code', 'ann_date'], keep='last')
    return combined


def main():
    parser = argparse.ArgumentParser(description='获取业绩预告与快报原始数据')
    parser.add_argument('--date', type=str, default=datetime.now().strftime('%Y%m%d'),
                        help='披露日期，格式 YYYYMMDD，默认今日')
    parser.add_argument('--period', type=str, default='20260331',
                        help='报告期，默认 2026Q1')
    parser.add_argument('--output', type=str, default=None,
                        help='输出 CSV 文件路径，默认保存到 data/raw/')
    parser.add_argument('--merge-to-baseline', action='store_true',
                        help='合并到 baseline.json')
    parser.add_argument('--baseline-only', action='store_true',
                        help='仅合并到 baseline，不输出单独 CSV')

    args = parser.parse_args()

    print(f"{'='*50}")
    print(f"获取业绩预告与快报 | 日期: {args.date}")
    print(f"{'='*50}\n")

    pro = init_pro()

    print("[1/4] 获取业绩预告...")
    forecast_df = fetch_forecast(pro, args.date)
    print(f"      预告数量: {len(forecast_df)}")

    print("[2/4] 获取业绩快报...")
    express_df = fetch_express(pro, args.date)
    print(f"      快报数量: {len(express_df)}")

    print("[3/4] 获取股票基础信息...")
    stock_df = fetch_stock_basic(pro)

    print("[4/4] 获取市值数据...")
    daily_df = fetch_daily_basic(pro, args.date)

    merged = merge_data(forecast_df, express_df, stock_df, daily_df)
    if merged.empty:
        print(f"\n日期 {args.date} 无新披露数据。")
        return

    print(f"\n合并后数据: {len(merged)} 条")

    if not args.baseline_only:
        if args.output:
            csv_path = Path(args.output)
        else:
            csv_path = DATA_DIR / f"forecast_express_{args.date}.csv"
        merged.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"数据已保存: {csv_path}")

    if args.merge_to_baseline or args.baseline_only:
        print("[合并] 更新 baseline.json...")
        new_baseline = merge_daily_to_baseline(merged)
        save_baseline_data(new_baseline)
        print(f"      baseline 累计: {len(new_baseline)} 条")

    print(f"\n{'='*50}")
    print(f"完成 | 当日数据: {len(merged)} 条")
    print(f"{'='*50}\n")


if __name__ == '__main__':
    main()
