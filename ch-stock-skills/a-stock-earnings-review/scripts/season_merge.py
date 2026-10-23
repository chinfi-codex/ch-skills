#!/usr/bin/env python3
"""Join one period's earnings forecasts and formal reports into a per-stock view.

Reads whatever the two scan skills already wrote to the working directory:

    reports/forecast_scan_<P>.json   (a-stock-earnings-forecast)
    reports/cninfo_enrich_<P>.json   (same skill; 扣非 parsed from the filing)
    reports/qreport_universe_<P>.json (a-stock-quarterly-report; every released stock)
    reports/qreport_scan_<P>.json     (same skill; meta: disclosure progress)

plus earlier verdicts from the shared ledger (forecast_verdict / qreport_verdict),
and writes ``reports/season_<P>.json``: one record per stock with

- ``mode``: ``deep`` once the formal report is out (四条线、质量、兑现度可查),
  ``inference`` while only the forecast exists (只有预告区间、扣非、原因);
- the forecast block and the formal block side by side, the headline growth
  taken from the formal report when present, and both announcement dates as
  anchors for the price cross-check (price_context.py).

Deterministic join only. Tiers, quality calls and the forecast→formal
reassessment are the model's (references/unified_framework.md).

    python3 scripts/season_merge.py --period 20260930
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

REASON_CHARS = 300


def load_snapshot() -> Optional[Dict[str, Any]]:
    try:
        sys.path.insert(0, str(_SCRIPT_DIR))
        import market_data  # noqa: E402

        return market_data.market_snapshot()
    except Exception:  # noqa: BLE001 - optional enrichment
        return None


def _load(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _mid(block: Optional[Dict[str, Any]]) -> Optional[float]:
    if not block:
        return None
    vals = [v for v in (block.get("low"), block.get("high"), block.get("point")) if v is not None]
    return round((min(vals) + max(vals)) / 2, 4) if vals else None


def load_verdicts(period: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """{"forecast": {code: row}, "formal": {code: row}} from the shared ledger; {} when absent."""
    out: Dict[str, Dict[str, Dict[str, Any]]] = {"forecast": {}, "formal": {}}
    try:
        from db_core import adapt_sql, get_connection, table_exists  # type: ignore
    except Exception:  # noqa: BLE001 - no DB layer: verdicts simply absent
        return out
    specs = {
        "forecast": ("forecast_verdict", "ts_code, tier, reason, caveat"),
        "formal": ("qreport_verdict", "ts_code, tier, reason, caveat, quality_call, fulfillment"),
    }
    try:
        with get_connection() as conn:
            for kind, (table, cols) in specs.items():
                if not table_exists(conn, table):
                    continue
                cur = conn.cursor()
                cur.execute(adapt_sql(f"SELECT {cols} FROM {table} WHERE period = ?"), (period,))
                names = [c[0] for c in cur.description]
                for row in cur.fetchall():
                    rec = dict(zip(names, row))
                    out[kind][str(rec["ts_code"])] = rec
    except Exception:  # noqa: BLE001
        return out
    return out


def forecast_block(s: Dict[str, Any], enrich: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    np_ = s.get("net_profit") or {}
    pg = s.get("profit_growth") or {}
    parsed = (enrich.get(s["ts_code"]) or {}).get("parsed") or {}
    kf = parsed.get("kf_net_profit_yi")
    rev = s.get("revenue_trailing") or {}
    reason = s.get("change_reason") or ""
    return {
        "ann_date": s.get("ann_date"),
        "first_ann_date": s.get("first_ann_date") or s.get("ann_date"),
        "type": s.get("type"),
        "np_min_yi": np_.get("min_yi"), "np_max_yi": np_.get("max_yi"),
        "np_median_yi": np_.get("median_yi"), "last_parent_yi": np_.get("last_parent_yi"),
        "base_source": (s.get("source") or {}).get("parent_base"),
        "cum_yoy_pct": pg.get("cum_yoy_pct"),
        "single_q_yoy_pct": pg.get("single_q_yoy_pct"), "single_q_note": pg.get("single_q_note"),
        "qoq_pct": pg.get("qoq_pct"),
        "accelerating": (s.get("flags") or {}).get("accelerating"),
        "kf_median_yi": _mid(kf), "kf_confidence": (kf or {}).get("confidence"),
        "revenue_trailing": {k: rev.get(k) for k in ("cum_yoy_pct", "single_q_yoy_pct", "qoq_pct")}
        if rev else None,
        "pe_annualized": (s.get("valuation") or {}).get("pe_annualized"),
        "change_reason": reason[:REASON_CHARS] + ("…" if len(reason) > REASON_CHARS else ""),
        "structured_source": (s.get("source") or {}).get("structured_source"),
    }


FORMAL_FIELDS = (
    "ann_date", "rev_cum_yi", "rev_cum_yoy_pct", "rev_single_yoy_pct", "np_cum_yi", "np_cum_yoy_pct",
    "np_single_yoy_pct", "np_qoq_pct", "dedt_cum_yi", "dedt_ratio_pct", "ocf_to_np_pct",
    "gross_margin_single_pct", "gross_margin_single_yoy_pp", "roe_annualized_pct",
    "contract_liab_yoy_pct", "pe_ttm_dedt", "total_mv_yi", "in_range", "hits", "rank_score",
    "shortlisted", "income_loaded",
)


def merge(period: str, forecast: Optional[Dict[str, Any]], enrich: Optional[Dict[str, Any]],
          universe: Optional[Dict[str, Any]], qscan: Optional[Dict[str, Any]],
          verdicts: Dict[str, Dict[str, Dict[str, Any]]],
          snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    snap = (snapshot or {}).get("stocks") or {}
    enrich_map = {e["ts_code"]: e for e in (enrich or {}).get("stocks", [])}
    f_map = {s["ts_code"]: s for s in (forecast or {}).get("stocks", [])}
    q_map = {u["ts_code"]: u for u in (universe or {}).get("universe", [])}
    codes = sorted(set(f_map) | set(q_map))
    stocks: List[Dict[str, Any]] = []
    for code in codes:
        f, q = f_map.get(code), q_map.get(code)
        fb = forecast_block(f, enrich_map) if f else None
        qb = {k: q.get(k) for k in FORMAL_FIELDS} if q else None
        deep = bool(qb and qb.get("income_loaded"))
        if deep:
            headline = {"np_cum_yoy_pct": qb.get("np_cum_yoy_pct"),
                        "np_single_q_yoy_pct": qb.get("np_single_yoy_pct"),
                        "np_qoq_pct": qb.get("np_qoq_pct"), "source": "formal"}
        elif fb:
            headline = {"np_cum_yoy_pct": fb.get("cum_yoy_pct"),
                        "np_single_q_yoy_pct": fb.get("single_q_yoy_pct"),
                        "np_qoq_pct": fb.get("qoq_pct"), "source": "forecast"}
        else:
            headline = {"np_cum_yoy_pct": None, "np_single_q_yoy_pct": None, "np_qoq_pct": None,
                        "source": "formal_incomplete"}
        fv, qv = verdicts["forecast"].get(code), verdicts["formal"].get(code)
        m = snap.get(code) or {}
        industry = (q or {}).get("industry") or (f or {}).get("industry") or None
        stocks.append({
            "ts_code": code,
            "name": (q or {}).get("name") or (f or {}).get("name") or m.get("name") or None,
            "industry": industry or m.get("board"),
            "industry_source": "tushare" if industry else ("eastmoney_board" if m.get("board") else None),
            "market": {"total_mv_yi": round(m["total_mv_yi"], 2) if m.get("total_mv_yi") else None,
                       "pe_ttm": round(m["pe_ttm"], 2) if m.get("pe_ttm") is not None else None,
                       "asof": (snapshot or {}).get("date")} if m else None,
            "mode": "deep" if deep else "inference",
            "has_forecast": fb is not None,
            "has_formal": qb is not None,
            "anchors": {k: v for k, v in (("forecast", (fb or {}).get("first_ann_date")),
                                          ("formal", (qb or {}).get("ann_date"))) if v},
            "headline": headline,
            "forecast": fb,
            "formal": qb,
            "verdicts": {
                "forecast": {k: fv.get(k) for k in ("tier", "reason", "caveat")} if fv else None,
                "formal": {k: qv.get(k) for k in ("tier", "quality_call", "fulfillment", "reason", "caveat")}
                if qv else None,
            },
        })

    def sort_key(s: Dict[str, Any]):
        v = s["headline"].get("np_single_q_yoy_pct")
        if v is None:
            v = s["headline"].get("np_cum_yoy_pct")
        return (s["mode"] != "deep", v is None, -(v or 0.0))

    stocks.sort(key=sort_key)
    return {"stocks": stocks, "industry_rollup": industry_rollup(stocks)}


def industry_rollup(stocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for s in stocks:
        groups.setdefault(s.get("industry") or "未归类", []).append(s)

    def med(vals: List[Optional[float]]) -> Optional[float]:
        clean = [v for v in vals if v is not None]
        return round(statistics.median(clean), 2) if clean else None

    rows = []
    for ind, members in groups.items():
        deep = [m for m in members if m["mode"] == "deep"]
        inf = [m for m in members if m["mode"] == "inference"]
        neg_types = {"预减", "略减", "首亏", "续亏", "增亏"}
        rows.append({
            "industry": ind, "n": len(members), "deep_n": len(deep), "inference_n": len(inf),
            "deep_np_single_yoy_median": med([m["formal"].get("np_single_yoy_pct") for m in deep]),
            "deep_rev_single_yoy_median": med([m["formal"].get("rev_single_yoy_pct") for m in deep]),
            "deep_ocf_to_np_median": med([m["formal"].get("ocf_to_np_pct") for m in deep]),
            "forecast_n": sum(1 for m in members if m["has_forecast"]),
            "forecast_negative_n": sum(1 for m in members
                                       if m["has_forecast"] and m["forecast"].get("type") in neg_types),
            "forecast_cum_yoy_median": med([m["forecast"].get("cum_yoy_pct")
                                            for m in members if m["has_forecast"]]),
        })
    rows.sort(key=lambda r: -r["n"])
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="合并同一报告期的业绩预告与正式报告")
    ap.add_argument("--period", required=True, help="报告期 YYYYMMDD（季度末）")
    ap.add_argument("--no-snapshot", action="store_true",
                    help="不拉东财全市场快照（行业/市值/PE 只用扫描结果里的）")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    p = args.period
    forecast = _load(os.path.join("reports", f"forecast_scan_{p}.json"))
    enrich = _load(os.path.join("reports", f"cninfo_enrich_{p}.json"))
    universe = _load(os.path.join("reports", f"qreport_universe_{p}.json"))
    qscan = _load(os.path.join("reports", f"qreport_scan_{p}.json"))
    if forecast is None and universe is None:
        print(json.dumps({"error": "no_inputs", "message":
                          f"reports/ 下既没有 forecast_scan_{p}.json 也没有 qreport_universe_{p}.json；"
                          "先运行两个扫描技能中至少一个。"}, ensure_ascii=False))
        return 2
    snapshot = None if args.no_snapshot else load_snapshot()
    merged = merge(p, forecast, enrich, universe, qscan, load_verdicts(p), snapshot)
    stocks = merged["stocks"]
    fmeta = (forecast or {}).get("meta") or {}
    qmeta = (qscan or {}).get("meta") or (universe or {}).get("meta") or {}
    notes: List[str] = []
    if forecast is None:
        notes.append("未找到业绩预告扫描结果：本期只有正式报告视角，触发式预告的边际信息缺失。")
    if universe is None:
        notes.append("未找到正式报告扫描结果：本期全部为推断模式（季报未出或未跑季报扫描/缺 Tushare 权限）。")
    if snapshot is None and not args.no_snapshot:
        notes.append("东财全市场快照不可得：缺 Tushare 时行业与市值为空。")
    elif snapshot:
        notes.append(f"行业缺失时用东财板块（{snapshot['date']}）补齐，口径比 Tushare 行业更细，聚宏观组时注意合并；"
                     "market 字段为同日总市值与 PE_TTM。")
    if fmeta and not (fmeta.get("tushare_status") or {}).get("enabled", True):
        notes.append("业绩预告为无 Tushare 模式：预告的单季拆解与 trailing 营收为空。")
    meta = {
        "period": p,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "sources": {
            "forecast": {"present": forecast is not None, "ann_cutoff": fmeta.get("ann_cutoff"),
                         "stocks": fmeta.get("unique_stocks"),
                         "tushare_enabled": (fmeta.get("tushare_status") or {}).get("enabled", True)
                         if fmeta else None},
            "formal": {"present": universe is not None, "ann_cutoff": qmeta.get("ann_cutoff"),
                       "released": qmeta.get("released_count"),
                       "disclosure_progress_pct": qmeta.get("disclosure_progress_pct")},
        },
        "counts": {
            "stocks": len(stocks),
            "deep": sum(1 for s in stocks if s["mode"] == "deep"),
            "inference": sum(1 for s in stocks if s["mode"] == "inference"),
            "forecast_and_formal": sum(1 for s in stocks if s["has_forecast"] and s["has_formal"]),
            "forecast_verdicts": sum(1 for s in stocks if s["verdicts"]["forecast"]),
            "formal_verdicts": sum(1 for s in stocks if s["verdicts"]["formal"]),
        },
        "notes": notes,
    }
    out = args.out or os.path.join("reports", f"season_{p}.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, **merged}, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"out": out, **meta["counts"], "sources": meta["sources"], "notes": notes},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
