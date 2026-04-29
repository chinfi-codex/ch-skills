#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Q1 季报数据获取工具（同比环比）。
职责边界：仅做数据获取、同比环比计算与单位转换，不做业绩分类、归因分析或报告生成。
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def fetch_disclosure_list(pro, start_date: str, end_date: str, report_period: str) -> pd.DataFrame:
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
                df = df[df['end_date'] == report_period]
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


def fetch_income_for_code(pro, ts_code: str, periods: list):
    result = {}
    for period in periods:
        try:
            df = pro.income(
                ts_code=ts_code,
                period=period,
                fields='ts_code,ann_date,end_date,total_revenue,revenue,total_profit,n_income_attr_p'
            )
            if df is not None and not df.empty:
                result[period] = df.iloc[0]
        except Exception:
            pass
        time.sleep(0.08)
    return result


def fetch_stock_basic(pro, ts_codes: list) -> pd.DataFrame:
    all_data = []
    batch_size = 80
    for i in range(0, len(ts_codes), batch_size):
        batch = ",".join(ts_codes[i:i+batch_size])
        try:
            df = pro.stock_basic(
                ts_code=batch,
                fields='ts_code,name,industry,market'
            )
            if df is not None and not df.empty:
                all_data.append(df)
        except Exception as e:
            print(f"  获取基础信息失败: {e}")
        time.sleep(0.15)

    if not all_data:
        return pd.DataFrame()
    return pd.concat(all_data, ignore_index=True)


def fetch_income_batch(pro, ts_codes: list, periods: list, max_workers: int = 3) -> pd.DataFrame:
    records = []

    print(f"\n开始获取 {len(ts_codes)} 只股票的 income 数据...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_code = {
            executor.submit(fetch_income_for_code, pro, code, periods): code
            for code in ts_codes
        }

        completed = 0
        for future in as_completed(future_to_code):
            code = future_to_code[future]
            completed += 1
            if completed % 50 == 0:
                print(f"  进度: {completed}/{len(ts_codes)}")

            try:
                data = future.result(timeout=10)
                if periods[0] not in data:
                    continue

                rec = {'ts_code': code}
                q1 = data[periods[0]]
                rec['ann_date'] = q1.get('ann_date')
                rec['revenue_q1'] = q1.get('total_revenue')
                rec['profit_q1'] = q1.get('n_income_attr_p')

                if periods[1] in data:
                    q1_last = data[periods[1]]
                    rec['revenue_q1_last'] = q1_last.get('total_revenue')
                    rec['profit_q1_last'] = q1_last.get('n_income_attr_p')

                if len(periods) >= 4 and periods[2] in data and periods[3] in data:
                    q4_cum = data[periods[2]].get('total_revenue')
                    q3_cum = data[periods[3]].get('total_revenue')
                    q4_prof_cum = data[periods[2]].get('n_income_attr_p')
                    q3_prof_cum = data[periods[3]].get('n_income_attr_p')

                    if pd.notna(q4_cum) and pd.notna(q3_cum) and q4_cum is not None and q3_cum is not None:
                        rec['revenue_q4'] = q4_cum - q3_cum
                    if pd.notna(q4_prof_cum) and pd.notna(q3_prof_cum) and q4_prof_cum is not None and q3_prof_cum is not None:
                        rec['profit_q4'] = q4_prof_cum - q3_prof_cum

                records.append(rec)
            except Exception:
                pass

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    if 'revenue_q1_last' in df.columns:
        df['revenue_yoy'] = (df['revenue_q1'] - df['revenue_q1_last']) / df['revenue_q1_last'] * 100
    if 'profit_q1_last' in df.columns:
        df['profit_yoy'] = (df['profit_q1'] - df['profit_q1_last']) / df['profit_q1_last'] * 100

    if 'revenue_q4' in df.columns:
        df['revenue_qoq'] = (df['revenue_q1'] - df['revenue_q4']) / df['revenue_q4'] * 100
    if 'profit_q4' in df.columns:
        df['profit_qoq'] = (df['profit_q1'] - df['profit_q4']) / df['profit_q4'] * 100

    df['profit_q1_yi'] = df['profit_q1'] / 1e8
    df['revenue_q1_yi'] = df['revenue_q1'] / 1e8

    return df


def main():
    parser = argparse.ArgumentParser(description='Q1 季报数据获取（同比环比）')
    parser.add_argument('--start-date', type=str, required=True,
                        help='起始日期，格式 YYYYMMDD')
    parser.add_argument('--end-date', type=str, required=True,
                        help='结束日期，格式 YYYYMMDD')
    parser.add_argument('--period', type=str, default='20260331',
                        help='报告期，默认 20260331（2026Q1）')
    parser.add_argument('--output', type=str, default=None,
                        help='输出 CSV 文件路径')
    parser.add_argument('--max-workers', type=int, default=3,
                        help='并发线程数，默认 3')

    args = parser.parse_args()

    year = int(args.period[:4])
    periods = [
        args.period,
        f"{year-1}{args.period[4:]}",
        f"{year-1}1231",
        f"{year-1}0930"
    ]

    print(f"{'='*60}")
    print(f"Q1 季报数据获取")
    print(f"披露日期范围: {args.start_date} - {args.end_date}")
    print(f"报告期: {args.period}")
    print(f"同比: {periods[1]}, 环比辅助: {periods[2]} / {periods[3]}")
    print(f"{'='*60}\n")

    pro = init_pro()

    print("[1/3] 获取披露公司列表...")
    disclosure_df = fetch_disclosure_list(pro, args.start_date, args.end_date, args.period)
    if disclosure_df.empty:
        print(f"日期范围 {args.start_date}-{args.end_date} 内无 {args.period} 报告期披露数据。")
        sys.exit(0)

    ts_codes = disclosure_df['ts_code'].tolist()
    print(f"共 {len(ts_codes)} 家公司需要获取数据\n")

    print("[2/3] 批量获取 income 数据...")
    income_df = fetch_income_batch(pro, ts_codes, periods, max_workers=args.max_workers)
    if income_df.empty:
        print("未能获取到有效的 income 数据。")
        sys.exit(0)

    print(f"成功获取 {len(income_df)} 家公司的 income 数据\n")

    print("[3/3] 获取股票基础信息并合并...")
    basic_df = fetch_stock_basic(pro, income_df['ts_code'].tolist())
    if not basic_df.empty:
        income_df = income_df.merge(basic_df, on='ts_code', how='left')

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = DATA_DIR / f"q1_income_{args.start_date}_{args.end_date}.csv"

    income_df.to_csv(output_path, index=False, encoding='utf-8-sig')
    print(f"\n{'='*60}")
    print(f"数据已保存: {output_path}")
    print(f"总样本: {len(income_df)} 家")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
