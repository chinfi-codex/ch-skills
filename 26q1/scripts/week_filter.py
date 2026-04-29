#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获取指定日期范围内披露的季报公司，并按增速筛选。
职责边界：仅做数据获取、增速计算与数值筛选，不做业绩分类或报告生成。
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

try:
    import tushare as ts
except ImportError:
    print("错误：未安装 tushare 包。请运行：pip install tushare")
    sys.exit(1)


def init_pro():
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        print("错误：未设置 TUSHARE_TOKEN 环境变量。")
        sys.exit(1)
    return ts.pro_api(token)


def fetch_week_disclosure(pro, start_date: str, end_date: str, period: str) -> pd.DataFrame:
    all_data = []
    current = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")

    while current <= end:
        date_str = current.strftime("%Y%m%d")
        try:
            df = pro.disclosure_date(
                actual_date=date_str,
                fields='ts_code,ann_date,end_date,actual_date'
            )
            if df is not None and not df.empty:
                df = df[df['end_date'] == period]
                if not df.empty:
                    all_data.append(df)
        except Exception as e:
            print(f"  {date_str}: 获取失败 - {e}")

        current = pd.Timestamp(current) + pd.Timedelta(days=1)
        current = current.to_pydatetime()
        time.sleep(0.15)

    if not all_data:
        return pd.DataFrame()

    result = pd.concat(all_data, ignore_index=True)
    result = result.drop_duplicates(subset=['ts_code'])
    return result


def fetch_income_for_code(pro, ts_code: str, period: str):
    try:
        df = pro.income(
            ts_code=ts_code,
            period=period,
            fields='ts_code,ann_date,end_date,total_revenue,n_income_attr_p'
        )
        if df is not None and not df.empty:
            return df.iloc[0]
    except Exception:
        pass
    time.sleep(0.08)
    return None


def fetch_stock_basic(pro, ts_codes: list) -> pd.DataFrame:
    all_data = []
    batch_size = 80
    for i in range(0, len(ts_codes), batch_size):
        batch = ",".join(ts_codes[i:i+batch_size])
        try:
            df = pro.stock_basic(
                ts_code=batch,
                fields='ts_code,name,industry'
            )
            if df is not None and not df.empty:
                all_data.append(df)
        except Exception as e:
            print(f"  获取基础信息失败: {e}")
        time.sleep(0.15)

    if not all_data:
        return pd.DataFrame()
    return pd.concat(all_data, ignore_index=True)


def main():
    parser = argparse.ArgumentParser(description='季报数据筛选（同比）')
    parser.add_argument('--start-date', type=str, required=True,
                        help='起始日期，格式 YYYYMMDD')
    parser.add_argument('--end-date', type=str, required=True,
                        help='结束日期，格式 YYYYMMDD')
    parser.add_argument('--period', type=str, default='20260331',
                        help='报告期，默认 20260331')
    parser.add_argument('--period-last', type=str, default='20250331',
                        help='去年同期报告期，默认 20250331')
    parser.add_argument('--min-revenue-yoy', type=float, default=20,
                        help='最低收入同比增速(%%)，默认 20')
    parser.add_argument('--min-profit-yoy', type=float, default=20,
                        help='最低利润同比增速(%%)，默认 20')
    parser.add_argument('--output', type=str, default=None,
                        help='输出 CSV 文件路径')
    args = parser.parse_args()

    print(f"{'='*60}")
    print(f"季报筛选：收入增速 > {args.min_revenue_yoy}% 且 利润增速 > {args.min_profit_yoy}%")
    print(f"披露日期范围: {args.start_date} - {args.end_date}")
    print(f"报告期: {args.period}")
    print(f"{'='*60}\n")

    pro = init_pro()

    print("[1/3] 获取披露公司列表...")
    disclosure_df = fetch_week_disclosure(pro, args.start_date, args.end_date, args.period)
    if disclosure_df.empty:
        print(f"日期范围 {args.start_date}-{args.end_date} 内无 {args.period} 报告期披露数据。")
        sys.exit(0)

    ts_codes = disclosure_df['ts_code'].tolist()
    print(f"共 {len(ts_codes)} 家公司需要获取数据\n")

    print("[2/3] 并发获取 income 数据...")
    records = []

    with ThreadPoolExecutor(max_workers=3) as executor:
        future_map = {}
        for code in ts_codes:
            f1 = executor.submit(fetch_income_for_code, pro, code, args.period)
            f2 = executor.submit(fetch_income_for_code, pro, code, args.period_last)
            future_map[f1] = (code, 'q1')
            future_map[f2] = (code, 'q1_last')

        results = {code: {'q1': None, 'q1_last': None} for code in ts_codes}
        completed = 0
        total = len(future_map)

        for future in as_completed(future_map):
            code, period_key = future_map[future]
            completed += 1
            if completed % 200 == 0:
                print(f"  进度: {completed}/{total}")

            try:
                data = future.result(timeout=10)
                if data is not None:
                    results[code][period_key] = data
            except Exception:
                pass

    for code, res in results.items():
        q1 = res['q1']
        q1_last = res['q1_last']
        if q1 is None or q1_last is None:
            continue

        rec = {
            'ts_code': code,
            'ann_date': q1.get('ann_date'),
            'revenue_q1': q1.get('total_revenue'),
            'profit_q1': q1.get('n_income_attr_p'),
            'revenue_q1_last': q1_last.get('total_revenue'),
            'profit_q1_last': q1_last.get('n_income_attr_p'),
        }
        records.append(rec)

    if not records:
        print("未能获取到有效的同比数据。")
        sys.exit(0)

    merged = pd.DataFrame(records)
    merged['revenue_yoy'] = (merged['revenue_q1'] - merged['revenue_q1_last']) / merged['revenue_q1_last'] * 100
    merged['profit_yoy'] = (merged['profit_q1'] - merged['profit_q1_last']) / merged['profit_q1_last'] * 100
    merged['revenue_yi'] = merged['revenue_q1'] / 1e8
    merged['profit_yi'] = merged['profit_q1'] / 1e8

    filtered = merged[
        (merged['revenue_yoy'] > args.min_revenue_yoy) &
        (merged['profit_yoy'] > args.min_profit_yoy)
    ].copy()

    print(f"\n筛选前: {len(merged)} 家，筛选后: {len(filtered)} 家")

    if filtered.empty:
        print("\n无公司满足筛选条件。")
        sys.exit(0)

    print("\n获取股票名称和行业...")
    basic_df = fetch_stock_basic(pro, filtered['ts_code'].tolist())
    if not basic_df.empty:
        filtered = filtered.merge(basic_df, on='ts_code', how='left')

    out_df = filtered[['name', 'ts_code', 'industry', 'revenue_yoy', 'profit_yoy',
                       'revenue_yi', 'profit_yi', 'ann_date']].copy()
    out_df = out_df.rename(columns={
        'name': '公司名称',
        'ts_code': '股票代码',
        'industry': '所属行业',
        'revenue_yoy': '收入同比增速(%)',
        'profit_yoy': '利润同比增速(%)',
        'revenue_yi': '营业收入(亿元)',
        'profit_yi': '归母净利润(亿元)',
        'ann_date': '公告日期',
    })

    for col in out_df.columns:
        if '增速' in col:
            out_df[col] = out_df[col].round(1)
        elif '亿元' in col:
            out_df[col] = out_df[col].round(2)

    if '利润同比增速(%)' in out_df.columns:
        out_df = out_df.sort_values('利润同比增速(%)', ascending=False)

    print("\n" + "="*60)
    print("筛选结果：")
    print("="*60)
    print(out_df.to_string(index=False))

    if args.output:
        csv_path = args.output
    else:
        csv_path = f"week_{args.start_date}_{args.end_date}_filtered.csv"
    out_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    print(f"\n结果已保存至: {csv_path}")

    print("\n统计摘要：")
    print(f"  公司数量: {len(out_df)} 家")
    print(f"  收入增速区间: {out_df['收入同比增速(%)'].min():.1f}% - {out_df['收入同比增速(%)'].max():.1f}%")
    print(f"  利润增速区间: {out_df['利润同比增速(%)'].min():.1f}% - {out_df['利润同比增速(%)'].max():.1f}%")
    print(f"  营业收入区间: {out_df['营业收入(亿元)'].min():.2f} - {out_df['营业收入(亿元)'].max():.2f} 亿元")
    print(f"  净利润区间: {out_df['归母净利润(亿元)'].min():.2f} - {out_df['归母净利润(亿元)'].max():.2f} 亿元")


if __name__ == '__main__':
    main()
