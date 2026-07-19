#!/usr/bin/env python3
"""CNInfo announcement parser for the earnings-forecast primary-source chain.

The full-sample path is driven by forecast_scan.py, which discovers CNInfo
announcements first and invokes this parser for every new PDF. This CLI remains
available for targeted re-parsing or debugging of selected codes.

脑/手 boundary: the script extracts text and offers best-effort parsed numbers
WITH their raw spans + confidence. Forecast announcements are short, so the FULL
text is returned — the model reads it, verifies the parsed numbers, and judges
sustainability. It does not decide "excellent" here.

Usage:
    python3 scripts/cninfo_enrich.py --period 20260630 --codes 600872.SH,601005.SH,002648.SZ
    python3 scripts/cninfo_enrich.py --period 20260630 --from reports/forecast_scan_20260630.json --positive --top 15
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from cninfo_client import (
    download_pdf,
    find_forecast_announcement,
    normalize_title,
    pdf_to_text,
    resolve_orgid,
)
from store import Store

_QUARTER_MMDD = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}
_MMDD_QUARTER = {v: k for k, v in _QUARTER_MMDD.items()}
UNIT_TO_YI = {"亿元": 1.0, "亿": 1.0, "万元": 1e-4, "万": 1e-4, "元": 1e-8}

KF_LABELS = ("扣除非经常性损益后的净利润", "扣除非经常性损益的净利润", "扣非后净利润", "扣非净利润")
PARENT_LABELS = ("归属于上市公司股东的净利润", "归属于母公司所有者的净利润", "归属于母公司股东的净利润")
REVENUE_LABELS = ("营业总收入", "营业收入")

# A number token: thousand-separated (requires a comma group, so it stops at a
# cell boundary like "668,700289,600" -> "668,700") OR a plain number.
_NUM = r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?"
_UNIT = r"亿元|万元|元"
_RANGE = r"[至到~～–—─\-]"
_PCT_NUM = r"-?\d+(?:\.\d+)?"

POSITIVE_WORDS = ("预增", "略增", "续盈", "扭亏", "减亏")
NEGATIVE_WORDS = ("预减", "略减", "首亏", "续亏", "增亏")


def quarter_of(period: str) -> int:
    mmdd = period[4:]
    if mmdd not in _MMDD_QUARTER:
        raise ValueError(f"{period} 不是季度末(0331/0630/0930/1231)。")
    return _MMDD_QUARTER[mmdd]


def period_label(period: str) -> str:
    q = quarter_of(period)
    return f"{period[:4]}H1" if q == 2 else f"{period[:4]}Q{q}"


def latest_quarter_end(today: dt.date) -> str:
    ymd = today.strftime("%Y%m%d")
    for c in sorted([f"{today.year}0331", f"{today.year}0630", f"{today.year}0930",
                     f"{today.year}1231", f"{today.year - 1}1231"], reverse=True):
        if c <= ymd:
            return c
    return f"{today.year - 1}1231"


def norm_code(raw: str) -> str:
    """'600872.SH' / '600872' / 'sh600872' -> '600872'."""
    digits = re.sub(r"\D", "", raw)
    return digits[-6:] if len(digits) >= 6 else digits


def to_ts_code(code: str) -> str:
    if code.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return f"{code}.SH"
    if code.startswith(("4", "8", "920", "430")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def detect_doc_unit(flat: str) -> Optional[str]:
    """Table-format forecasts put the unit in a header cell ('单位：万元')."""
    m = re.search(r"(?:金额)?单位[：:][^。\n]{0,10}?(亿元|万元|元)", flat)
    return m.group(1) if m else None


def _mk(val_lo, val_hi, unit, raw, conf):
    if val_hi is None:
        return {"low": None, "high": None, "point": round(val_lo, 4), "unit": unit,
                "raw": raw[:80], "confidence": conf}
    lo, hi = sorted((val_lo, val_hi))
    return {"low": round(lo, 4), "high": round(hi, 4), "point": None, "unit": unit,
            "raw": raw[:80], "confidence": conf}


def parse_amount(flat: str, labels: tuple, doc_unit: Optional[str]) -> Optional[Dict[str, Any]]:
    """Best-effort amount after a label. Returns 亿元 low/high/point + raw span.

    Emits a number only when a unit is known (adjacent or document-level);
    otherwise returns None so the model reads the raw text instead of trusting a
    fabricated figure. Confidence: high = unit adjacent, med = document unit.
    """
    for lab in labels:
        idx = flat.find(lab)
        if idx < 0:
            continue
        tail = re.split(r"[。；;]", flat[idx + len(lab): idx + len(lab) + 100])[0]
        # Range: NUM [unit?] rangechar NUM [unit?]
        rng = re.search(rf"({_NUM})\s*({_UNIT})?\s*{_RANGE}\s*({_NUM})\s*({_UNIT})?", tail)
        if rng:
            unit = rng.group(4) or rng.group(2) or doc_unit
            if unit:
                f = UNIT_TO_YI[unit]
                conf = "high" if (rng.group(2) or rng.group(4)) else "med"
                lo = float(rng.group(1).replace(",", "")) * f
                hi = float(rng.group(3).replace(",", "")) * f
                # Chinese forecast tables commonly write “亏损：2,500万元至
                # 3,000万元” without a minus sign.  Preserve the economic sign
                # from the cell prefix instead of treating the unsigned amounts
                # as profit.
                if re.search(r"亏损|为负|负值", tail[:rng.start()]):
                    lo, hi = -abs(lo), -abs(hi)
                return _mk(lo, hi, unit, tail, conf)
        # Point: NUM unit
        pt = re.search(rf"({_NUM})\s*({_UNIT})", tail)
        if pt:
            f = UNIT_TO_YI[pt.group(2)]
            value = float(pt.group(1).replace(",", "")) * f
            if re.search(r"亏损|为负|负值", tail[:pt.start()]):
                value = -abs(value)
            return _mk(value, None, pt.group(2), tail, "high")
        # Point via document unit (no adjacent unit — table row where the label
        # is directly followed by the value). Anchor at tail start and reject a
        # trailing % so prose like "营业收入同比增长约60%" is not mistaken for an
        # amount (revenue is usually quoted only as a growth rate, if at all).
        pt2 = re.match(rf"({_NUM})(?![%％])", tail)
        if pt2 and doc_unit:
            f = UNIT_TO_YI[doc_unit]
            value = float(pt2.group(1).replace(",", "")) * f
            if re.search(r"亏损|为负|负值", tail[:pt2.start()]):
                value = -abs(value)
            return _mk(value, None, doc_unit, tail, "med")
    return None


def parse_parent_yoy(flat: str) -> Optional[Dict[str, Any]]:
    """Parse the disclosed parent-profit YoY range near the parent-profit row."""
    for label in PARENT_LABELS:
        idx = flat.find(label)
        if idx < 0:
            continue
        span = flat[idx: idx + 360]
        for match in re.finditer(
            rf"(?:比上年同期|同比)(增长|上升|增加|下降|减少|降低)?[：:]?"
            rf"({_PCT_NUM})[%％](?:\s*{_RANGE}\s*({_PCT_NUM})[%％])?",
            span,
        ):
            direction = match.group(1) or ""
            lo = float(match.group(2))
            hi = float(match.group(3)) if match.group(3) is not None else lo
            if direction in ("下降", "减少", "降低"):
                lo, hi = -abs(lo), -abs(hi)
            lo, hi = sorted((lo, hi))
            return {"low": round(lo, 2), "high": round(hi, 2), "raw": match.group(0)[:80],
                    "confidence": "high"}
    return None


def infer_forecast_type(title: str, flat: str) -> str:
    # Only explicit title wording is stable enough for the categorical label.
    # Body text often mentions prior-period "扭亏/减亏" in a different context;
    # scanning it would misclassify otherwise generic "业绩预告" titles.
    haystack = normalize_title(title)
    for token in (*POSITIVE_WORDS, *NEGATIVE_WORDS):
        if token in haystack:
            return token
    if "预计扭亏" in haystack or "扭亏为盈" in haystack:
        return "扭亏"
    return ""


def extract_change_reason(text: str) -> str:
    """Extract the company's deterministic reason section without judging it."""
    flat = re.sub(r"[ \t]+", "", text or "")
    patterns = (
        r"(?:业绩变动原因说明|业绩变动的主要原因|业绩变化的主要原因|变动原因)[：:]?\s*(.+?)(?=\n\s*(?:三、|四、|其他相关说明|风险提示|特此公告)|$)",
        r"(?:主要原因如下|主要原因系)[：:]?\s*(.+?)(?=\n\s*(?:三、|四、|其他相关说明|风险提示|特此公告)|$)",
    )
    for pattern in patterns:
        m = re.search(pattern, flat, flags=re.S)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()[:1200]
    return ""


def parse_forecast_text(text: str, title: str = "") -> Dict[str, Any]:
    flat = re.sub(r"\s+", "", text or "")
    doc_unit = detect_doc_unit(flat)
    return {
        "kf_net_profit_yi": parse_amount(flat, KF_LABELS, doc_unit),
        "revenue_yi": parse_amount(flat, REVENUE_LABELS, doc_unit),
        "parent_net_yi": parse_amount(flat, PARENT_LABELS, doc_unit),
        "parent_yoy_pct": parse_parent_yoy(flat),
        "forecast_type": infer_forecast_type(title, flat),
        "change_reason": extract_change_reason(text),
        "revenue_disclosed": parse_amount(flat, REVENUE_LABELS, doc_unit) is not None,
    }


def enrich_announcement(ann: Dict[str, Any], period: str) -> Dict[str, Any]:
    """Download and parse one already-discovered CNInfo announcement."""
    ts_code = str(ann.get("ts_code") or to_ts_code(norm_code(str(ann.get("code") or ""))))
    rec: Dict[str, Any] = {"ts_code": ts_code, "code": norm_code(ts_code), "found": False}
    announcement = {
        "announcement_id": str(ann.get("announcement_id") or ""),
        "title": normalize_title(str(ann.get("title") or "")),
        "ann_date": str(ann.get("ann_date") or ""),
        "url": str(ann.get("url") or ""),
    }
    rec["announcement"] = announcement
    try:
        raw = download_pdf(announcement["url"])
        text = pdf_to_text(raw) if raw else ""
    except Exception as exc:  # noqa: BLE001
        rec["note"] = f"PDF 下载/解析失败: {str(exc)[:60]}"
        return rec
    if not text.strip():
        rec["note"] = "PDF 文本为空(可能为扫描件)"
        return rec
    parsed = parse_forecast_text(text, announcement["title"])
    rec.update({"found": True, "parsed": parsed, "text": text.strip()[:3000], "notes": []})
    if parsed.get("revenue_yi") is None:
        rec["notes"].append("公告未披露营业收入(业绩预告常态)")
    if parsed.get("kf_net_profit_yi") is None:
        rec["notes"].append("未检出扣非净利(部分模板式预告不列)")
    if parsed.get("parent_net_yi") is None:
        rec["notes"].append("未稳定解析归母净利区间，结构化字段需使用差异校验源")
    return rec


def forecast_row_from_enrich(rec: Dict[str, Any], period: str) -> Optional[Dict[str, Any]]:
    """Convert authoritative CNInfo PDF facts to the existing forecast row schema."""
    parsed = rec.get("parsed") or {}
    parent = parsed.get("parent_net_yi") or {}
    vals = [parent.get("low"), parent.get("high"), parent.get("point")]
    vals = [float(v) for v in vals if v is not None]
    if not vals:
        return None
    lo_yi, hi_yi = min(vals), max(vals)
    yoy = parsed.get("parent_yoy_pct") or {}
    yvals = [yoy.get("low"), yoy.get("high")]
    yvals = [float(v) for v in yvals if v is not None]
    ann = rec.get("announcement") or {}
    ann_date = re.sub(r"\D", "", str(ann.get("ann_date") or ""))[:8]
    title = normalize_title(str(ann.get("title") or ""))
    ftype = infer_forecast_type(title, "")
    return {
        "ts_code": rec["ts_code"], "end_date": period, "ann_date": ann_date,
        "type": ftype,
        "p_change_min": min(yvals) if yvals else None,
        "p_change_max": max(yvals) if yvals else None,
        "net_profit_min": lo_yi * 10000.0,
        "net_profit_max": hi_yi * 10000.0,
        "last_parent_net": None,
        "first_ann_date": ann_date,
        "summary": f"CNInfo公告：归母净利润预计{lo_yi:.4f}-{hi_yi:.4f}亿元",
        "change_reason": str(parsed.get("change_reason") or ""),
        "update_flag": "1" if any(t in title for t in ("修正", "更正")) else "0",
    }


def enrich_one(ts_code: str, period: str, se_date: str, notes: List[str]) -> Dict[str, Any]:
    code = norm_code(ts_code)
    rec: Dict[str, Any] = {"ts_code": to_ts_code(code), "code": code, "found": False}
    try:
        orgid = resolve_orgid(code)
    except Exception as exc:  # noqa: BLE001
        rec["note"] = f"orgId 解析失败: {str(exc)[:60]}"
        return rec
    if not orgid:
        rec["note"] = "未解析到 cninfo orgId"
        return rec
    try:
        ann = find_forecast_announcement(code, orgid, period, se_date)
    except Exception as exc:  # noqa: BLE001
        rec["note"] = f"公告查询失败: {str(exc)[:60]}"
        return rec
    if not ann:
        rec["note"] = "未找到该报告期业绩预告公告"
        return rec

    ann["ts_code"] = rec["ts_code"]
    direct = enrich_announcement(ann, period)
    return direct


def pick_codes_from_scan(path: str, top: int, positive: bool) -> List[str]:
    data = json.load(open(path, encoding="utf-8"))
    rows = data.get("stocks", [])
    if positive:
        rows = [r for r in rows if r.get("flags", {}).get("positive_type")]
    rows = rows[: top] if top else rows  # scan output is already sorted by cum_yoy desc
    return [r["ts_code"] for r in rows]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Enrich forecast candidates with cninfo 扣非净利/营收/原因全文.")
    ap.add_argument("--period", help="报告期末 YYYYMMDD。默认今天之前最近季度末。")
    ap.add_argument("--codes", help="逗号分隔的候选代码(600872.SH 或 600872)。")
    ap.add_argument("--from", dest="from_scan", help="从 forecast_scan JSON 取候选。")
    ap.add_argument("--top", type=int, default=15, help="配合 --from：取前 N(默认 15)。")
    ap.add_argument("--positive", action="store_true", help="配合 --from：仅向好类型。")
    ap.add_argument("--workers", type=int, default=6, help="并发抓取的线程数(默认 6；限流时调小)。")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，强制重下 PDF 重解析。")
    ap.add_argument("--no-cache", action="store_true", help="禁用 DB 缓存(每次都下载)。")
    ap.add_argument("--out", help="输出路径。默认 reports/cninfo_enrich_<period>.json")
    ap.add_argument("--stdout", action="store_true", help="同时打印完整 JSON。")
    args = ap.parse_args(argv)

    today = dt.date.today()
    period = args.period or latest_quarter_end(today)
    quarter_of(period)

    codes: List[str] = []
    if args.codes:
        codes += [c.strip() for c in args.codes.split(",") if c.strip()]
    if args.from_scan:
        codes += pick_codes_from_scan(args.from_scan, args.top, args.positive)
    # dedupe preserving order
    seen, ordered = set(), []
    for c in codes:
        key = norm_code(c)
        if key and key not in seen:
            seen.add(key)
            ordered.append(c)
    if not ordered:
        print(json.dumps({"error": "no codes: pass --codes or --from"}, ensure_ascii=False))
        return 2

    p_end = dt.datetime.strptime(period, "%Y%m%d").date()
    se_start = (p_end - dt.timedelta(days=60)).strftime("%Y-%m-%d")
    se_end = min(today, p_end + dt.timedelta(days=90)).strftime("%Y-%m-%d")
    se_date = f"{se_start}~{se_end}"

    notes = [
        "扣非净利(kf_net_profit)只在预告公告原文里有，Tushare 结构化预告没有——这是 cninfo 的主要增量。",
        "多数业绩预告不披露营业收入；revenue_disclosed=false 属正常。",
        "parsed 为最佳努力解析，附 raw 与 confidence；请对照 text 全文核对后再采用。",
        "已抓的公告原文/解析存入 DB(forecast_enrich_cache)，同一 code+period 默认复用缓存，不重复下载 PDF；--refresh 强制重取。",
    ]

    store = Store(enabled=not args.no_cache)
    ts_map = {c: to_ts_code(norm_code(c)) for c in ordered}
    cached_map = {} if args.refresh else store.get_enrich_many([ts_map[c] for c in ordered], period)
    to_fetch = [c for c in ordered if (cached_map.get(ts_map[c]) or {}).get("parsed") is None]

    # Uncached codes are independent → fetch (orgId + query + PDF) concurrently.
    fetched: Dict[str, Dict[str, Any]] = {}
    if to_fetch:
        workers = max(1, min(args.workers, len(to_fetch)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for c, rec in zip(to_fetch, ex.map(lambda c: enrich_one(c, period, se_date, notes), to_fetch)):
                fetched[c] = rec

    stocks: List[Dict[str, Any]] = []
    from_cache = 0
    upsert_records: List[Dict[str, Any]] = []
    for c in ordered:
        ts = ts_map[c]
        cc = cached_map.get(ts)
        if cc and cc.get("parsed") is not None:
            stocks.append({
                "ts_code": ts, "code": norm_code(c), "found": True,
                "announcement": cc["announcement"], "parsed": cc["parsed"],
                "text": cc["text"], "notes": [], "from_cache": True,
            })
            from_cache += 1
            continue
        rec = fetched[c]
        rec["from_cache"] = False
        if rec.get("found"):
            a = rec.get("announcement", {})
            upsert_records.append({
                "ts_code": ts, "period": period, "ann_date": a.get("ann_date", ""),
                "title": a.get("title", ""), "url": a.get("url", ""),
                "parsed": rec.get("parsed"), "text": rec.get("text", ""),
            })
        stocks.append(rec)
    store.upsert_enrich_many(upsert_records)

    payload = {
        "meta": {
            "period": period, "period_label": period_label(period), "se_date": se_date,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "requested": len(ordered),
            "found": sum(1 for s in stocks if s.get("found")),
            "from_cache": from_cache,
            "downloaded": len(ordered) - from_cache,
            "missing": [s["ts_code"] for s in stocks if not s.get("found")],
            "cache": "on" if store.available else f"off ({store.reason})",
            "notes": notes,
        },
        "stocks": stocks,
    }

    out_path = args.out or os.path.join("reports", f"cninfo_enrich_{period}.json")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    summary = {"period": period, "requested": len(ordered),
               "found": payload["meta"]["found"], "from_cache": from_cache,
               "downloaded": len(ordered) - from_cache, "out": out_path}
    print(json.dumps(payload if args.stdout else summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
