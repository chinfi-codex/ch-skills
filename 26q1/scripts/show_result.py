#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 CSV 数据转换为 Markdown 表格。
职责边界：仅做格式转换，不添加分析结论、标题或统计摘要。
"""

import argparse
import pandas as pd


def csv_to_markdown_table(csv_path: str, output_path: str = None):
    df = pd.read_csv(csv_path, encoding='utf-8-sig')

    lines = []
    header = "| " + " | ".join(df.columns) + " |"
    lines.append(header)
    lines.append("|" + "|".join(["---"] * len(df.columns)) + "|")

    for _, row in df.iterrows():
        vals = []
        for col in df.columns:
            v = row[col]
            if isinstance(v, float):
                if '增速' in col or '同比' in col or '环比' in col or 'yoy' in col.lower() or 'qoq' in col.lower():
                    vals.append(f"{v:.1f}")
                elif '亿元' in col or 'yi' in col.lower():
                    vals.append(f"{v:.2f}")
                else:
                    vals.append(f"{v:.2f}")
            else:
                vals.append(str(v) if pd.notna(v) else '')
        lines.append("| " + " | ".join(vals) + " |")

    md = "\n".join(lines)

    if output_path:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(md)
        print(f"Markdown table saved to {output_path}")
    else:
        print(md)

    return md


def main():
    parser = argparse.ArgumentParser(description='CSV 转 Markdown 表格')
    parser.add_argument('csv', help='输入 CSV 文件路径')
    parser.add_argument('-o', '--output', default=None, help='输出 Markdown 文件路径')
    args = parser.parse_args()

    csv_to_markdown_table(args.csv, args.output)


if __name__ == '__main__':
    main()
