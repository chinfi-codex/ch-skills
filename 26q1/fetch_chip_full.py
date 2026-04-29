#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获取国产芯片/昇腾链重点公司的完整财报数据
"""

import os
import sys
import time
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


COMPANIES = {
    'GPU': {'寒武纪': '688256.SH'},
    '昇腾链': {
        '伟测科技': '688372.SH',
        '强瑞技术': '301128.SZ',
        '华丰科技': '688629.SH',
        '泰嘉股份': '002843.SZ',
        '意华股份': '002897.SZ',
    }
}

PERIODS = ['20241231', '20250331', '20250630', '20250930', '20251231', '20260331']


def safe_get(pro, func, **kwargs):
    """安全调用Tushare API"""
    for attempt in range(3):
        try:
            df = func(**kwargs)
            if df is not None and not df.empty:
                return df
            return None
        except Exception as e:
            if attempt < 2:
                time.sleep(0.5)
            else:
                return None
    return None


def fetch_company_period(pro, name, ts_code, period):
    """获取单个公司单个报告期的完整数据"""
    result = {
        '分类': None,
        '公司名称': name,
        '股票代码': ts_code,
        '报告期': period,
    }
    
    # 利润表
    income = safe_get(pro, pro.income, ts_code=ts_code, period=period,
                      fields='ts_code,ann_date,end_date,total_revenue,revenue,total_profit,n_income_attr_p')
    if income is not None:
        row = income.iloc[0]
        result['公告日期'] = row.get('ann_date')
        result['营业收入'] = row.get('total_revenue')
        result['净利润'] = row.get('n_income_attr_p')
    else:
        result['公告日期'] = None
        result['营业收入'] = None
        result['净利润'] = None
    
    # 资产负债表
    balance = safe_get(pro, pro.balancesheet, ts_code=ts_code, period=period,
                       fields='ts_code,ann_date,end_date,inventories,prepayment,adv_receipts,deferred_inc')
    if balance is not None:
        row = balance.iloc[0]
        result['存货'] = row.get('inventories')
        result['预付款项'] = row.get('prepayment')
        result['预收款项'] = row.get('adv_receipts')
        result['递延收益'] = row.get('deferred_inc')
    else:
        result['存货'] = None
        result['预付款项'] = None
        result['预收款项'] = None
        result['递延收益'] = None
    
    time.sleep(0.15)
    return result


def main():
    pro = init_pro()
    
    print("=" * 70)
    print("国产芯片/昇腾链重点公司完整财报数据获取")
    print("=" * 70)
    
    all_records = []
    
    for category, companies in COMPANIES.items():
        print(f"\n【{category}】")
        for name, ts_code in companies.items():
            print(f"  {name} ({ts_code}):")
            for period in PERIODS:
                print(f"    {period}...", end=" ")
                rec = fetch_company_period(pro, name, ts_code, period)
                rec['分类'] = category
                all_records.append(rec)
                if rec['营业收入'] is not None:
                    print(f"营收={rec['营业收入']/1e8:.2f}亿")
                else:
                    print("无数据")
    
    df = pd.DataFrame(all_records)
    
    # 转换单位
    for col in ['营业收入', '净利润', '存货', '预付款项', '预收款项', '递延收益']:
        df[f'{col}(亿元)'] = df[col] / 1e8
    
    # 保存完整数据
    df.to_csv("chip_companies_full.csv", index=False, encoding='utf-8-sig')
    print(f"\n完整数据已保存: chip_companies_full.csv")
    
    # 生成透视表
    pivot_revenue = df.pivot_table(index=['分类', '公司名称', '股票代码'],
                                    columns='报告期', values='营业收入(亿元)')
    pivot_profit = df.pivot_table(index=['分类', '公司名称', '股票代码'],
                                   columns='报告期', values='净利润(亿元)')
    pivot_inventory = df.pivot_table(index=['分类', '公司名称', '股票代码'],
                                      columns='报告期', values='存货(亿元)')
    pivot_prepay = df.pivot_table(index=['分类', '公司名称', '股票代码'],
                                   columns='报告期', values='预付款项(亿元)')
    
    print("\n" + "=" * 70)
    print("营业收入(亿元) 透视")
    print("=" * 70)
    print(pivot_revenue.to_string())
    
    print("\n" + "=" * 70)
    print("净利润(亿元) 透视")
    print("=" * 70)
    print(pivot_profit.to_string())
    
    print("\n" + "=" * 70)
    print("存货(亿元) 透视")
    print("=" * 70)
    print(pivot_inventory.to_string())
    
    print("\n" + "=" * 70)
    print("预付款项(亿元) 透视")
    print("=" * 70)
    print(pivot_prepay.to_string())
    
    # 计算2026Q1同比（如果有数据）
    print("\n" + "=" * 70)
    print("2026Q1 vs 2025Q1 同比分析")
    print("=" * 70)
    
    q1_2026 = df[df['报告期'] == '20260331'][['分类', '公司名称', '股票代码', '营业收入(亿元)', '净利润(亿元)', '存货(亿元)', '预付款项(亿元)']].copy()
    q1_2025 = df[df['报告期'] == '20250331'][['股票代码', '营业收入(亿元)', '净利润(亿元)', '存货(亿元)', '预付款项(亿元)']].copy()
    
    if not q1_2026.empty and not q1_2025.empty:
        yoy = q1_2026.merge(q1_2025, on='股票代码', suffixes=('_2026Q1', '_2025Q1'))
        for metric in ['营业收入', '净利润', '存货', '预付款项']:
            c1 = f'{metric}(亿元)_2026Q1'
            c2 = f'{metric}(亿元)_2025Q1'
            yoy[f'{metric}同比(%)'] = ((yoy[c1] - yoy[c2]) / yoy[c2].abs() * 100).round(1)
        
        print(yoy[['分类', '公司名称', '营业收入同比(%)', '净利润同比(%)', '存货同比(%)', '预付款项同比(%)']].to_string(index=False))


if __name__ == '__main__':
    main()
