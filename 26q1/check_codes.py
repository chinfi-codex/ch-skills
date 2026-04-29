import os
import tushare as ts
pro = ts.pro_api(os.environ.get('TUSHARE_TOKEN', ''))
codes = ['688256.SH', '688372.SH', '688629.SH', '002843.SZ', '002897.SZ', '301128.SZ']
for code in codes:
    try:
        df = pro.stock_basic(ts_code=code, fields='ts_code,name,industry')
        if df is not None and not df.empty:
            print(f"{code}: {df.iloc[0]['name']} - {df.iloc[0]['industry']}")
    except Exception as e:
        print(f"{code}: error - {e}")
