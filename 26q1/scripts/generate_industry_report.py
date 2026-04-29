#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读取 week_filter 输出的 CSV，按行业分类生成 Markdown 报告。
职责边界：仅做数据筛选与表格格式化，不添加分析结论。
"""

import argparse
import pandas as pd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv', help='输入 CSV 文件路径')
    parser.add_argument('--min-profit', type=float, default=0.2,
                        help='最低归母净利润（亿元），默认 0.2')
    parser.add_argument('-o', '--output', default='week_result_by_industry.md',
                        help='输出 Markdown 文件路径')
    args = parser.parse_args()

    df = pd.read_csv(args.csv, encoding='utf-8-sig')
    filtered = df[df['归母净利润(亿元)'] > args.min_profit].copy()
    filtered = filtered.sort_values(['所属行业', '利润同比增速(%)'], ascending=[True, False])

    lines = []
    lines.append('# 本周（2026-04-20 至 2026-04-26）Q1 财报筛选结果')
    lines.append('')
    lines.append(f'**筛选条件**：收入同比增速 > 20%、利润同比增速 > 20%、归母净利润 > {args.min_profit} 亿元（{int(args.min_profit*10000)}万）')
    lines.append(f'**满足条件公司数**：{len(filtered)} 家（原筛选 {len(df)} 家）')
    lines.append('')

    lines.append('## 行业分布')
    lines.append('')
    industry_counts = filtered['所属行业'].value_counts()
    for industry, count in industry_counts.items():
        lines.append(f'- {industry}：{count} 家')
    lines.append('')

    lines.append('## 按行业分类详细列表')
    lines.append('')

    for industry, group in filtered.groupby('所属行业', sort=False):
        lines.append(f'### {industry}（{len(group)} 家）')
        lines.append('')
        lines.append('| 公司名称 | 股票代码 | 收入同比 | 利润同比 | 营业收入(亿元) | 归母净利润(亿元) | 公告日期 |')
        lines.append('|----------|----------|----------|----------|----------------|------------------|----------|')
        for _, row in group.iterrows():
            lines.append(
                f"| {row['公司名称']} | {row['股票代码']} | "
                f"{row['收入同比增速(%)']:.1f}% | {row['利润同比增速(%)']:.1f}% | "
                f"{row['营业收入(亿元)']:.2f} | {row['归母净利润(亿元)']:.2f} | {row['公告日期']} |"
            )
        lines.append('')

    with open(args.output, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f'报告已保存: {args.output}')
    print(f'总计: {len(filtered)} 家')


if __name__ == '__main__':
    main()
