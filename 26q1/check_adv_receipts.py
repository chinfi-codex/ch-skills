import os
import tushare as ts
pro = ts.pro_api(os.environ.get('TUSHARE_TOKEN', ''))

# 检查寒武纪的预收款项和递延收益
code = '688256.SH'
period = '20251231'

df = pro.balancesheet(ts_code=code, period=period, fields='ts_code,adv_receipts,deferred_inc,contract_liabilities')
print("balancesheet subset:")
print(df.to_string())

# 检查fina_indicator是否有合同负债
df2 = pro.fina_indicator(ts_code=code, period=period)
if df2 is not None and not df2.empty:
    print("\nfina_indicator fields:", [c for c in df2.columns if 'contract' in c.lower() or 'receipt' in c.lower() or 'adv' in c.lower()])
