#!/usr/bin/env python3
"""Daily bars, adjustment factors and valuation history for price cross-checks.

Two providers, tried in order:

- ``public`` (no token): Sina daily K-line + Sina 后复权 factors for prices,
  Eastmoney datacenter ``RPT_VALUEANALYSIS_DET`` for daily PE_TTM / PB / PS /
  PCF history and the board (industry) name.
- ``tushare`` (needs TUSHARE_TOKEN with daily / adj_factor / daily_basic /
  index_daily permission): used when the public endpoints fail, or forced
  with ``source="tushare"``.

Only fetching and normalising lives here. Every function returns plain dicts:
bars ``{"date": "YYYY-MM-DD", "open", "high", "low", "close", "vol", "adj"}``
where ``adj`` is a 后复权 multiplier (1.0 when unknown) — ratios of
``close * adj`` are split/dividend-safe returns.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
SINA_KLINE = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
SINA_HFQ = "https://finance.sina.com.cn/realstock/company/{symbol}/hfq.js"
EM_VALUATION = "https://datacenter-web.eastmoney.com/api/data/v1/get"
BENCHMARK = {"code": "000300.SH", "name": "沪深300"}


class DataUnavailable(RuntimeError):
    pass


def _get(url: str, params: Optional[Dict[str, Any]] = None, attempts: int = 3,
         timeout: float = 15.0) -> str:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            resp = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": UA})
            if resp.status_code == 200 and resp.text.strip():
                return resp.text
            last = DataUnavailable(f"HTTP {resp.status_code}")
        except requests.RequestException as exc:
            last = exc
        time.sleep(0.8 * (2 ** i))
    raise DataUnavailable(f"{url}: {last}")


def sina_symbol(ts_code: str) -> str:
    code, exch = ts_code.split(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}[exch.upper()] + code


def _iso(raw: str) -> str:
    raw = str(raw)[:10].replace("/", "-")
    return raw if "-" in raw else f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


# --------------------------------------------------------------------------- #
# public provider
# --------------------------------------------------------------------------- #
def _sina_bars(ts_code: str, count: int) -> List[Dict[str, Any]]:
    text = _get(SINA_KLINE, {"symbol": sina_symbol(ts_code), "scale": 240, "ma": "no",
                             "datalen": min(count, 1023)})
    rows = json.loads(text)
    if not isinstance(rows, list) or not rows:
        raise DataUnavailable(f"sina kline empty for {ts_code}")
    return [{"date": _iso(r["day"]), "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]),
             "vol": float(r.get("volume") or 0.0)} for r in rows]


def _sina_hfq(ts_code: str) -> List[Tuple[str, float]]:
    """[(ex_date, factor)] ascending; the factor applies from ex_date onward."""
    text = _get(SINA_HFQ.format(symbol=sina_symbol(ts_code)))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise DataUnavailable(f"sina hfq unparsable for {ts_code}")
    data = json.loads(m.group(0)).get("data") or []
    out = sorted((_iso(d["d"]), float(d["f"])) for d in data)
    if not out:
        raise DataUnavailable(f"sina hfq empty for {ts_code}")
    return out


def _apply_factors(bars: List[Dict[str, Any]], factors: List[Tuple[str, float]]) -> None:
    j = -1
    for bar in bars:  # bars ascending
        while j + 1 < len(factors) and factors[j + 1][0] <= bar["date"]:
            j += 1
        bar["adj"] = factors[j][1] if j >= 0 else factors[0][1]


def _em_valuation(ts_code: str) -> List[Dict[str, Any]]:
    code = ts_code.split(".")[0]
    text = _get(EM_VALUATION, {
        "reportName": "RPT_VALUEANALYSIS_DET",
        "columns": "TRADE_DATE,PE_TTM,PB_MRQ,PS_TTM,PCF_OCF_TTM,TOTAL_MARKET_CAP,BOARD_NAME,SECURITY_NAME_ABBR",
        "filter": f'(SECURITY_CODE="{code}")',
        "pageNumber": 1, "pageSize": 5000, "sortColumns": "TRADE_DATE", "sortTypes": -1,
    })
    payload = json.loads(text)
    rows = ((payload.get("result") or {}).get("data")) or []
    if not rows:
        raise DataUnavailable(f"eastmoney valuation empty for {ts_code}")
    out = [{
        "date": _iso(r["TRADE_DATE"]),
        "pe_ttm": r.get("PE_TTM"), "pb": r.get("PB_MRQ"), "ps_ttm": r.get("PS_TTM"),
        "pcf_ocf_ttm": r.get("PCF_OCF_TTM"),
        "total_mv_yi": (r["TOTAL_MARKET_CAP"] / 1e8) if r.get("TOTAL_MARKET_CAP") else None,
        "board": r.get("BOARD_NAME"), "name": r.get("SECURITY_NAME_ABBR"),
    } for r in rows]
    return sorted(out, key=lambda r: r["date"])


# --------------------------------------------------------------------------- #
# tushare provider
# --------------------------------------------------------------------------- #
def _tushare_token() -> str:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    paths = [os.path.join(os.getcwd(), ".env"), os.path.expanduser(
        os.environ.get("CH_SKILLS_CONFIG") or os.path.join("~", ".ch-skills", "config.env"))]
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.strip().partition("=")
                if key.strip() == "TUSHARE_TOKEN" and value.strip():
                    return value.strip().strip('"').strip("'")
    return ""


def _tushare_pro():
    token = _tushare_token()
    if not token:
        raise DataUnavailable("no TUSHARE_TOKEN")
    try:
        import tushare as ts  # type: ignore
    except ImportError as exc:
        raise DataUnavailable("tushare not installed") from exc
    return ts.pro_api(token)


def _ts_window(days: int) -> Tuple[str, str]:
    end = time.strftime("%Y%m%d")
    start = time.strftime("%Y%m%d", time.localtime(time.time() - days * 1.6 * 86400))
    return start, end


def _tushare_bars(ts_code: str, count: int, index: bool = False) -> List[Dict[str, Any]]:
    pro = _tushare_pro()
    start, end = _ts_window(count)
    try:
        df = (pro.index_daily if index else pro.daily)(ts_code=ts_code, start_date=start, end_date=end)
        adj = None if index else pro.adj_factor(ts_code=ts_code, start_date=start, end_date=end)
    except Exception as exc:  # noqa: BLE001
        raise DataUnavailable(f"tushare bars {ts_code}: {str(exc)[:80]}") from exc
    if df is None or df.empty:
        raise DataUnavailable(f"tushare bars empty for {ts_code}")
    factors = {} if adj is None or adj.empty else dict(zip(adj["trade_date"], adj["adj_factor"]))
    rows = [{"date": _iso(r["trade_date"]), "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]), "vol": float(r.get("vol") or 0.0),
             "adj": float(factors.get(r["trade_date"], 1.0) or 1.0)} for _, r in df.iterrows()]
    return sorted(rows, key=lambda r: r["date"])[-count:]


def _tushare_valuation(ts_code: str) -> List[Dict[str, Any]]:
    pro = _tushare_pro()
    start, end = _ts_window(1300)
    try:
        df = pro.daily_basic(ts_code=ts_code, start_date=start, end_date=end,
                             fields="trade_date,pe_ttm,pb,ps_ttm,total_mv")
    except Exception as exc:  # noqa: BLE001
        raise DataUnavailable(f"tushare daily_basic {ts_code}: {str(exc)[:80]}") from exc
    if df is None or df.empty:
        raise DataUnavailable(f"tushare daily_basic empty for {ts_code}")
    rows = [{"date": _iso(r["trade_date"]), "pe_ttm": r.get("pe_ttm"), "pb": r.get("pb"),
             "ps_ttm": r.get("ps_ttm"), "pcf_ocf_ttm": None,
             "total_mv_yi": (float(r["total_mv"]) / 1e4) if r.get("total_mv") else None,
             "board": None, "name": None} for _, r in df.iterrows()]
    return sorted(rows, key=lambda r: r["date"])


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def stock_bars(ts_code: str, count: int = 520, source: str = "auto") -> Dict[str, Any]:
    """{"source", "adjusted", "bars"}; tries public first unless source='tushare'."""
    errors: List[str] = []
    if source in ("auto", "public"):
        try:
            bars = _sina_bars(ts_code, count)
            try:
                _apply_factors(bars, _sina_hfq(ts_code))
                adjusted = True
            except DataUnavailable as exc:
                errors.append(str(exc))
                for bar in bars:
                    bar["adj"] = 1.0
                adjusted = False
            return {"source": "sina", "adjusted": adjusted, "bars": bars, "errors": errors}
        except DataUnavailable as exc:
            errors.append(str(exc))
    if source in ("auto", "tushare"):
        try:
            return {"source": "tushare", "adjusted": True,
                    "bars": _tushare_bars(ts_code, count), "errors": errors}
        except DataUnavailable as exc:
            errors.append(str(exc))
    raise DataUnavailable("; ".join(errors))


def index_bars(count: int = 520, source: str = "auto") -> Dict[str, Any]:
    errors: List[str] = []
    if source in ("auto", "public"):
        try:
            bars = _sina_bars(BENCHMARK["code"], count)
            for bar in bars:
                bar["adj"] = 1.0
            return {"source": "sina", "bars": bars, **BENCHMARK}
        except DataUnavailable as exc:
            errors.append(str(exc))
    if source in ("auto", "tushare"):
        try:
            return {"source": "tushare", "bars": _tushare_bars(BENCHMARK["code"], count, index=True),
                    **BENCHMARK}
        except DataUnavailable as exc:
            errors.append(str(exc))
    raise DataUnavailable("; ".join(errors))


def market_snapshot(max_lookback_days: int = 7) -> Dict[str, Any]:
    """Whole-market board (industry) / PE_TTM / market cap for the latest trading day.

    One Eastmoney call; used to fill industry and size when the Tushare path
    (stock_basic / daily_basic) is unavailable.
    """
    today = time.time()
    for back in range(max_lookback_days + 1):
        day = time.strftime("%Y-%m-%d", time.localtime(today - back * 86400))
        text = _get(EM_VALUATION, {
            "reportName": "RPT_VALUEANALYSIS_DET",
            "columns": "SECUCODE,SECURITY_NAME_ABBR,BOARD_NAME,PE_TTM,TOTAL_MARKET_CAP",
            "filter": f"(TRADE_DATE='{day}')",
            "pageNumber": 1, "pageSize": 6000, "sortColumns": "SECUCODE", "sortTypes": 1,
        })
        rows = ((json.loads(text).get("result") or {}).get("data")) or []
        if rows:
            return {"date": day, "source": "eastmoney", "stocks": {
                r["SECUCODE"]: {"name": r.get("SECURITY_NAME_ABBR"), "board": r.get("BOARD_NAME"),
                                "pe_ttm": r.get("PE_TTM"),
                                "total_mv_yi": (r["TOTAL_MARKET_CAP"] / 1e8) if r.get("TOTAL_MARKET_CAP") else None}
                for r in rows}}
    raise DataUnavailable("eastmoney market snapshot empty")


def valuation_history(ts_code: str, source: str = "auto") -> Dict[str, Any]:
    errors: List[str] = []
    if source in ("auto", "public"):
        try:
            return {"source": "eastmoney", "rows": _em_valuation(ts_code)}
        except (DataUnavailable, ValueError) as exc:
            errors.append(str(exc))
    if source in ("auto", "tushare"):
        try:
            return {"source": "tushare", "rows": _tushare_valuation(ts_code)}
        except DataUnavailable as exc:
            errors.append(str(exc))
    raise DataUnavailable("; ".join(errors))
