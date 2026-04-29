import os
import tushare as ts
import pandas as pd

pro = ts.pro_api(os.environ.get('TUSHARE_TOKEN', ''))

# 1. 检查balancesheet的所有字段
print("=== balancesheet 可用字段 ===")
try:
    df = pro.balancesheet(ts_code='688256.SH', period='20251231')
    if df is not None and not df.empty:
        print("字段列表:", df.columns.tolist())
        print("\n寒武纪2025年报资产负债表:")
        print(df.to_string())
except Exception as e:
    print(f"Error: {e}")

# 2. 检查意华股份
print("\n=== 意华股份检查 ===")
try:
    df = pro.stock_basic(ts_code='002897.SZ', fields='ts_code,name,list_status')
    print(df.to_string())
except Exception as e:
    print(f"Error: {e}")

# 尝试获取意华股份income
try:
    df = pro.income(ts_code='002897.SZ', period='20251231')
    if df is not None and not df.empty:
        print("意华股份income:\n", df.to_string())
    else:
        print("意华股份income: 无数据")
except Exception as e:
    print(f"意华股份income error: {e}")

# 尝试balancesheet
try:
    df = pro.balancesheet(ts_code='002897.SZ', period='20251231')
    if df is not None and not df.empty:
        print("意华股份balancesheet:\n", df.to_string())
    else:
        print("意华股份balancesheet: 无数据")
except Exception as e:
    print(f"意华股份balancesheet error: {e}")
