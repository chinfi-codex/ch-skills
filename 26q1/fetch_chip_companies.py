#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获取国产芯片/昇腾链重点公司的财报数据
包含利润表和资产负债表关键科目
"""

import os
import sys
import pandas as pd
from datetime import datetime

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


# 公司列表
COMPANIES = {
    'GPU': {
        '寒武纪': '688256.SH',
    },
    '昇腾链': {
        '伟测科技': '688372.SH',
        '强瑞技术': '301128.SZ',
        '华丰科技': '688629.SH',
        '泰嘉股份': '002843.SZ',
        '意华股份': '002897.SZ',
    }
}

# 未上市公司（无法通过Tushare获取）
UNLISTED = ['沐曦', '摩尔线程']


def fetch_income(pro, ts_code, period):
    """获取利润表数据"""
    try:
        df = pro.income(
            ts_code=ts_code,
            period=period,
            fields='ts_code,ann_date,end_date,total_revenue,revenue,total_profit,n_income_attr_p'
        )
        if df is not None and not df.empty:
            return df.iloc[0]
    except Exception as e:
        print(f"  获取利润表失败 {ts_code} {period}: {e}")
    return None


def fetch_balance_sheet(pro, ts_code, period):
    """获取资产负债表数据"""
    try:
        df = pro.balancesheet(
            ts_code=ts_code,
            period=period,
            fields='ts_code,ann_date,end_date,inventories,contract_liabilities,prepayment'
        )
        if df is not None and not df.empty:
            return df.iloc[0]
    except Exception as e:
        print(f"  获取资产负债表失败 {ts_code} {period}: {e}")
    return None


def fetch_company_data(pro, name, ts_code, period):
    """获取单个公司单个报告期的数据"""
    income = fetch_income(pro, ts_code, period)
    balance = fetch_balance_sheet(pro, ts_code, period)
    
    result = {
        '公司名称': name,
        '股票代码': ts_code,
        '报告期': period,
    }
    
    if income is not None:
        result['营业收入(元)'] = income.get('total_revenue')
        result['净利润(元)'] = income.get('n_income_attr_p')
        result['公告日期'] = income.get('ann_date')
    else:
        result['营业收入(元)'] = None
        result['净利润(元)'] = None
        result['公告日期'] = None
    
    if balance is not None:
        result['存货(元)'] = balance.get('inventories')
        result['合同负债(元)'] = balance.get('contract_liabilities')
        result['预付款项(元)'] = balance.get('prepayment')
    else:
        result['存货(元)'] = None
        result['合同负债(元)'] = None
        result['预付款项(元)'] = None
    
    return result


def main():
    pro = init_pro()
    
    periods = ['20251231', '20260331']
    
    print("=" * 60)
    print("国产芯片/昇腾链重点公司财报数据获取")
    print("=" * 60)
    print(f"报告期: {periods}")
    print(f"注：沐曦、摩尔线程未上市，无法通过Tushare获取数据")
    print("=" * 60)
    
    all_records = []
    
    for category, companies in COMPANIES.items():
        print(f"\n【{category}】")
        for name, ts_code in companies.items():
            print(f"  获取 {name} ({ts_code})...")
            for period in periods:
                data = fetch_company_data(pro, name, ts_code, period)
                data['分类'] = category
                all_records.append(data)
                # 避免触发限流
                import time
                time.sleep(0.2)
    
    df = pd.DataFrame(all_records)
    
    # 转换单位：元 -> 亿元
    for col in ['营业收入(元)', '净利润(元)', '存货(元)', '合同负债(元)', '预付款项(元)']:
        yi_col = col.replace('(元)', '(亿元)')
        df[yi_col] = df[col] / 1e8
    
    # 排序
    df = df.sort_values(['分类', '公司名称', '报告期'])
    
    # 保存原始数据
    output_raw = "chip_companies_raw.csv"
    df.to_csv(output_raw, index=False, encoding='utf-8-sig')
    print(f"\n原始数据已保存: {output_raw}")
    
    # 生成可读表格
    display_cols = ['分类', '公司名称', '股票代码', '报告期', '公告日期',
                    '营业收入(亿元)', '净利润(亿元)', '存货(亿元)', 
                    '合同负债(亿元)', '预付款项(亿元)']
    display_df = df[display_cols].copy()
    
    # 数值格式化
    for col in display_cols:
        if '(亿元)' in col:
            display_df[col] = display_df[col].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    
    output_display = "chip_companies_display.csv"
    display_df.to_csv(output_display, index=False, encoding='utf-8-sig')
    print(f"可读表格已保存: {output_display}")
    
    # 打印结果
    print("\n" + "=" * 60)
    print("数据汇总")
    print("=" * 60)
    print(display_df.to_string(index=False))
    
    # 生成同比分析（仅适用于2026Q1 vs 2025Q1）
    print("\n" + "=" * 60)
    print("2026Q1 vs 2025Q1 同比数据")
    print("=" * 60)
    
    # 需要获取2025Q1数据
    q1_2025_period = '20250331'
    q1_records = []
    
    for category, companies in COMPANIES.items():
        for name, ts_code in companies.items():
            data = fetch_company_data(pro, name, ts_code, q1_2025_period)
            data['分类'] = category
            q1_records.append(data)
            import time
            time.sleep(0.2)
    
    q1_df = pd.DataFrame(q1_records)
    for col in ['营业收入(元)', '净利润(元)', '存货(元)', '合同负债(元)', '预付款项(元)']:
        yi_col = col.replace('(元)', '(亿元)')
        q1_df[yi_col] = q1_df[col] / 1e8
    
    # 合并2026Q1和2025Q1进行同比计算
    q1_2026 = df[df['报告期'] == '20260331'].copy()
    q1_2025 = q1_df[q1_df['报告期'] == '20250331'].copy()
    
    if not q1_2026.empty and not q1_2025.empty:
        merge_cols = ['公司名称', '股票代码', '分类']
        yoy_df = q1_2026.merge(q1_2025, on=merge_cols, suffixes=('_2026Q1', '_2025Q1'))
        
        for metric in ['营业收入', '净利润', '存货', '合同负债', '预付款项']:
            col_2026 = f'{metric}(亿元)_2026Q1'
            col_2025 = f'{metric}(亿元)_2025Q1'
            yoy_col = f'{metric}同比(%)'
            yoy_df[yoy_col] = ((yoy_df[col_2026] - yoy_df[col_2025]) / yoy_df[col_2025].abs() * 100).round(1)
        
        yoy_display = yoy_df[['分类', '公司名称', '股票代码',
                              '营业收入同比(%)', '净利润同比(%)', 
                              '存货同比(%)', '合同负债同比(%)', '预付款项同比(%)']]
        print(yoy_display.to_string(index=False))
        yoy_display.to_csv("chip_companies_yoy_q1.csv", index=False, encoding='utf-8-sig')
        print("\n同比数据已保存: chip_companies_yoy_q1.csv")


if __name__ == '__main__':
    main()
