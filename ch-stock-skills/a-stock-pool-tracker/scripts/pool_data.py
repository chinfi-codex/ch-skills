#!/usr/bin/env python3
"""Atomic data access for the pool tracker: Tushare first, local DB for bars.

Nothing here filters or judges — it only returns rows.  Dates in / out are
ISO 'YYYY-MM-DD'; Tushare's 'YYYYMMDD' is converted at the boundary.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = SCRIPT_DIR / "_shared"
_DEV_SHARED = SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

from db_core import get_connection, placeholder, table_exists  # noqa: E402

JRJ_LIMIT_UP_URL = "https://gateway.jrj.com/quot-dc/zdt/v1/record"


def ts_date(iso: str) -> str:
    return str(iso).replace("-", "")[:8]


def iso_date(raw: Any) -> str:
    if hasattr(raw, "isoformat"):
        return raw.isoformat()[:10]
    s = str(raw).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _records(df: Any) -> list[dict]:
    if df is None or getattr(df, "empty", True):
        return []
    return df.where(df.notna(), None).to_dict(orient="records")


# --------------------------------------------------------------------------- #
# Tushare
# --------------------------------------------------------------------------- #
_PRO = None


def _token() -> str:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip() == "TUSHARE_TOKEN":
                return value.strip().strip('"').strip("'")
    return ""


def pro():
    global _PRO
    if _PRO is None:
        import tushare as ts

        token = _token()
        if not token:
            raise RuntimeError("缺少 TUSHARE_TOKEN（环境变量或当前目录 .env）")
        _PRO = ts.pro_api(token)
    return _PRO


def call(endpoint: str, attempts: int = 3, **kwargs: Any) -> list[dict]:
    """Call a Tushare endpoint with backoff on transient / rate-limit errors."""
    last: Optional[Exception] = None
    for k in range(attempts):
        try:
            return _records(getattr(pro(), endpoint)(**kwargs))
        except Exception as exc:  # noqa: BLE001 — Tushare raises bare Exception
            msg = str(exc).lower()
            if any(w in msg for w in ("token", "权限", "permission", "积分")):
                raise
            last = exc
            time.sleep(1.5 * (2 ** k))
    raise RuntimeError(f"Tushare {endpoint} 失败: {last}")


def trade_dates(start: str, end: str) -> list[str]:
    rows = call("trade_cal", exchange="SSE", start_date=ts_date(start), end_date=ts_date(end), is_open="1")
    return sorted(iso_date(r["cal_date"]) for r in rows)


def latest_trade_date(asof: Optional[str] = None) -> str:
    end = asof or date.today().isoformat()
    start = (date.fromisoformat(end) - timedelta(days=20)).isoformat()
    days = trade_dates(start, end)
    if not days:
        raise RuntimeError(f"{end} 之前 20 天内没有交易日")
    return days[-1]


def market_day(trade_date: str) -> dict:
    """Whole-market rows for one day: daily ⨝ stk_limit ⨝ daily_basic ⨝ stock_basic."""
    td = ts_date(trade_date)
    daily = call("daily", trade_date=td)
    notes: list[str] = []
    try:
        limits = {r["ts_code"]: r for r in call("stk_limit", trade_date=td)}
    except Exception as exc:  # noqa: BLE001
        limits = {}
        notes.append(f"stk_limit 不可用，涨停价按板制规则推算: {exc}")
    basic = {r["ts_code"]: r for r in call("daily_basic", trade_date=td,
                                            fields="ts_code,turnover_rate,turnover_rate_f,total_mv,circ_mv")}
    names = {r["ts_code"]: r for r in call("stock_basic", list_status="L", fields="ts_code,name,list_date,industry")}
    rows = []
    for r in daily:
        code = r["ts_code"]
        row = dict(r)
        row["up_limit"] = (limits.get(code) or {}).get("up_limit")
        row.update({k: v for k, v in (basic.get(code) or {}).items() if k != "ts_code"})
        row.update({k: v for k, v in (names.get(code) or {}).items() if k != "ts_code"})
        rows.append(row)
    return {"rows": rows, "notes": notes}


def prior_days(trade_date: str, n: int = 4) -> dict:
    """{ts_code: [前 n 个交易日的 daily 行（升序）]}，前一交易日附 up_limit。供首板与 5 日涨幅判定。"""
    start = (date.fromisoformat(trade_date) - timedelta(days=30)).isoformat()
    days = [d for d in trade_dates(start, trade_date) if d < trade_date][-n:]
    out: dict[str, list[dict]] = {}
    for d in days:
        for r in call("daily", trade_date=ts_date(d), fields="ts_code,trade_date,close,pre_close,pct_chg"):
            out.setdefault(r["ts_code"], []).append(dict(r, trade_date=d))
    if days:
        try:
            limits = {r["ts_code"]: r.get("up_limit") for r in call("stk_limit", trade_date=ts_date(days[-1]))}
        except Exception:  # noqa: BLE001 — 退回板制规则推算
            limits = {}
        for code, rows in out.items():
            if rows[-1]["trade_date"] == days[-1]:
                rows[-1]["up_limit"] = limits.get(code)
    # 前一交易日没有数据（停牌）的票，最后一行不是前一交易日；交给规则判 history_missing
    return {code: rows for code, rows in out.items() if days and rows[-1]["trade_date"] == days[-1]}


def first_limit_times(trade_date: str) -> dict:
    """{ts_code: 'HHMMSS'} 首次封板时间。Tushare limit_list_d 优先，JRJ 涨停池兜底。"""
    errors = []
    try:
        rows = call("limit_list_d", trade_date=ts_date(trade_date), limit_type="U",
                    fields="ts_code,first_time,open_times,limit_times")
        if rows:
            return {"source": "tushare.limit_list_d", "errors": errors,
                    "times": {r["ts_code"]: str(r.get("first_time") or "").replace(":", "").zfill(6)
                              for r in rows if r.get("first_time")},
                    "extra": {r["ts_code"]: {"open_times": r.get("open_times"), "limit_times": r.get("limit_times")}
                              for r in rows}}
        errors.append("limit_list_d 返回空")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"limit_list_d: {exc}")
    try:
        times, extra = _jrj_first_times(trade_date)
        if times:
            return {"source": "jrj.zdt", "errors": errors, "times": times, "extra": extra}
        errors.append("JRJ 返回空")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"JRJ: {exc}")
    return {"source": None, "errors": errors, "times": {}, "extra": {}}


def _jrj_first_times(trade_date: str) -> tuple[dict, dict]:
    import requests

    headers = {
        "accept": "application/json, text/plain, */*",
        "deviceinfo": json.dumps({"productId": "6000021", "version": "1.0.0", "device": "Mozilla/5.0",
                                  "sysName": "Chrome", "sysVersion": ["chrome/145.0.0.0"]}),
        "origin": "https://summary.jrj.com.cn",
        "productid": "6000021",
        "referer": "https://summary.jrj.com.cn/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    times, extra = {}, {}
    for page in range(1, 11):
        resp = requests.post(JRJ_LIMIT_UP_URL, headers=headers, timeout=15,
                             json={"td": ts_date(trade_date), "zdtType": "zt", "pageNum": page, "pageSize": 100})
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 20000:
            raise RuntimeError(f"unexpected JRJ code {data.get('code')}")
        items = (data.get("data") or {}).get("list") or []
        for it in items:
            code = str(it.get("code") or "").strip()
            if not code:
                continue
            suffix = ".SH" if code.startswith(("6", "9")) else ".BJ" if code.startswith(("4", "8")) else ".SZ"
            ts_code = f"{code}{suffix}"
            if it.get("zdttm"):
                times[ts_code] = str(it["zdttm"]).zfill(6)
            extra[ts_code] = {"open_times": it.get("open_times")}
        if len(items) < 100:
            break
    return times, extra


# --------------------------------------------------------------------------- #
# Bars（本地 stock_daily 优先，缺最新一天再走 Tushare）
# --------------------------------------------------------------------------- #
_BAR_COLS = ("trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "amount")


def _bars_from_db(ts_code: str, start: str, end: str) -> list[dict]:
    ph = placeholder()
    with get_connection() as conn:
        if not table_exists(conn, "stock_daily"):
            return []
        has_adj = table_exists(conn, "stock_adj_factor")
        sql = (f"SELECT d.trade_date, d.open, d.high, d.low, d.close, d.pre_close, d.pct_chg, d.amount"
               + (", a.adj_factor" if has_adj else "")
               + " FROM stock_daily d"
               + (" LEFT JOIN stock_adj_factor a ON a.ts_code = d.ts_code AND a.trade_date = d.trade_date"
                  if has_adj else "")
               + f" WHERE d.ts_code = {ph} AND d.trade_date >= {ph} AND d.trade_date <= {ph} ORDER BY d.trade_date")
        cur = conn.cursor()
        cur.execute(sql, (ts_code, start, end))
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["trade_date"] = iso_date(r["trade_date"])
        for k in r:
            if k != "trade_date" and r[k] is not None:
                r[k] = float(r[k])
    if any(r.get("adj_factor") is None for r in rows):
        return []  # 复权因子不完整时不用本地数据，交由 Tushare 路径补齐
    return rows


def bars(ts_code: str, start: str, end: str) -> dict:
    rows = []
    try:
        rows = _bars_from_db(ts_code, start, end)
    except Exception:  # noqa: BLE001 — DB 不可用时退回 Tushare
        rows = []
    if rows and rows[-1]["trade_date"] == end:
        return {"source": "db", "rows": rows}
    daily = call("daily", ts_code=ts_code, start_date=ts_date(start), end_date=ts_date(end))
    adj = {iso_date(r["trade_date"]): r["adj_factor"]
           for r in call("adj_factor", ts_code=ts_code, start_date=ts_date(start), end_date=ts_date(end))}
    out = []
    for r in sorted(daily, key=lambda x: x["trade_date"]):
        d = iso_date(r["trade_date"])
        item = {k: (d if k == "trade_date" else r.get(k)) for k in _BAR_COLS}
        item["adj_factor"] = adj.get(d)
        out.append(item)
    return {"source": "tushare", "rows": out}
