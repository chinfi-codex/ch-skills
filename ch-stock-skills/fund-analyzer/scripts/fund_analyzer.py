#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Atomic public-fund data and rule-evidence CLI.

This script does not call an LLM and does not compose Markdown reports. It
returns JSON evidence for the model to interpret according to SKILL.md.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from buy_point_analyzer import BuyPointAnalyzer
from fund_data_fetcher import FundDataFetcher
from industry_compare_analyzer import IndustryCompareAnalyzer, clean_value


def json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(clean_value(payload), ensure_ascii=False, indent=2)


def emit(payload: Dict[str, Any], output: Optional[str] = None) -> None:
    content = json_dumps(payload)
    if output:
        path = Path(output).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(json_dumps({"status": "success", "output": str(path)}))
        return
    print(content)


def search_funds(fetcher: FundDataFetcher, query: str) -> Dict[str, Any]:
    results = fetcher.search_fund(query)
    return {
        "success": True,
        "type": "fund_search_results",
        "query": query,
        "count": len(results),
        "items": results,
    }


def resolve_fund(fetcher: FundDataFetcher, query: str) -> Dict[str, Any]:
    results = fetcher.search_fund(query)
    if not results:
        return {"success": False, "message": "fund_not_found", "query": query, "candidates": []}
    if len(results) == 1:
        return {"success": True, "fund": results[0], "candidates": results}

    normalized = query.strip().upper()
    exact_matches = [
        item for item in results
        if str(item.get("ts_code", "")).upper() == normalized
        or str(item.get("name", "")).strip() == query.strip()
    ]
    if len(exact_matches) == 1:
        return {"success": True, "fund": exact_matches[0], "candidates": results}

    return {
        "success": False,
        "message": "ambiguous_fund",
        "query": query,
        "candidates": results[:10],
    }


def buy_point_evidence(fetcher: FundDataFetcher, query: str) -> Dict[str, Any]:
    resolved = resolve_fund(fetcher, query)
    if not resolved.get("success"):
        return {"type": "fund_buy_point_evidence", **resolved}

    fund = resolved["fund"]
    analyzer = BuyPointAnalyzer(fetcher)
    evidence = analyzer.analyze_buy_point(fund.get("ts_code", ""))
    evidence.update(
        {
            "type": "fund_buy_point_evidence",
            "query": query,
            "resolved_fund": fund,
            "candidate_count": len(resolved.get("candidates", [])),
        }
    )
    return evidence


def screen_funds(fetcher: FundDataFetcher) -> Dict[str, Any]:
    all_funds = fetcher.get_all_funds()
    if all_funds.empty:
        return {"success": False, "message": "empty_fund_universe", "type": "fund_screen_evidence"}

    candidate_df = all_funds.copy()
    candidate_df = candidate_df[
        candidate_df["fund_type"].isin(["股票型", "混合型"])
        | candidate_df["invest_type"].isin(["偏股混合型", "灵活配置型", "混合型"])
    ]
    candidate_df = candidate_df[~candidate_df["invest_type"].astype(str).str.contains("指数", na=False)]
    candidate_df["found_date"] = pd.to_datetime(candidate_df["found_date"], format="%Y%m%d", errors="coerce")
    candidate_df = candidate_df[candidate_df["found_date"].notna()]
    candidate_df = candidate_df[candidate_df["found_date"] <= (pd.Timestamp.now() - pd.Timedelta(days=365))]
    candidate_df = candidate_df[candidate_df["issue_amount"].notna()]
    candidate_df = candidate_df[(candidate_df["issue_amount"] >= 2.0) & (candidate_df["issue_amount"] <= 50.0)]
    candidate_df = candidate_df.sort_values(["issue_amount", "found_date"], ascending=[False, True]).head(120)

    rows = []
    for _, fund in candidate_df.iterrows():
        ts_code = fund.get("ts_code", "")
        if not ts_code:
            continue
        try:
            total_asset_yi = float(fund.get("issue_amount", 0))
            manager_df = fetcher.get_fund_manager(ts_code)
            if manager_df is None or manager_df.empty:
                continue
            manager = manager_df.iloc[0]
            manager_tenure = float(manager.get("work_time", 0) or 0)
            if manager_tenure < 1:
                continue

            nav_df = fetcher.get_fund_nav(ts_code, count=750)
            if nav_df is None or nav_df.empty or len(nav_df) < 250:
                continue
            nav_series = pd.to_numeric(nav_df["nav"], errors="coerce").dropna()
            if nav_series.empty:
                continue

            current_nav = float(nav_series.iloc[-1])
            year_ago_nav = float(nav_series.iloc[-250])
            first_nav = float(nav_series.iloc[0])
            annual_return_1y = (current_nav / year_ago_nav - 1) * 100 if year_ago_nav > 0 else None
            annual_return_3y = (
                (current_nav / first_nav - 1) / 3 * 100
                if len(nav_series) >= 750 and first_nav > 0
                else None
            )
            if annual_return_1y is None or annual_return_1y <= 0:
                continue
            if annual_return_3y is not None and annual_return_3y <= 0:
                continue

            rows.append(
                {
                    "ts_code": ts_code,
                    "name": fund.get("name", ""),
                    "manager_name": manager.get("name", ""),
                    "manager_tenure_years": manager_tenure,
                    "scale_yi": total_asset_yi,
                    "annual_return_1y_pct": annual_return_1y,
                    "annual_return_3y_pct": annual_return_3y,
                    "current_nav": current_nav,
                }
            )
        except Exception:
            continue

    rows.sort(
        key=lambda item: (
            item.get("annual_return_1y_pct") or -999,
            item.get("annual_return_3y_pct") or -999,
        ),
        reverse=True,
    )
    return {
        "success": True,
        "type": "fund_screen_evidence",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "criteria": {
            "fund_types": ["股票型", "混合型", "偏股混合型", "灵活配置型"],
            "exclude_index_funds": True,
            "scale_yi": [2.0, 50.0],
            "min_manager_tenure_years": 1,
            "positive_1y_return": True,
            "positive_3y_return_when_available": True,
        },
        "items": rows[:20],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch public-fund evidence as JSON")
    parser.add_argument("command", choices=["search", "buy-point", "analyze", "compare-industry", "industry-compare", "screen", "recommend"])
    parser.add_argument("query", nargs="*", help="基金名称/代码或行业词")
    parser.add_argument("--output", "-o", help="输出 JSON 文件路径")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    query = " ".join(args.query).strip()

    try:
        fetcher = FundDataFetcher()
        if args.command == "search":
            if not query:
                raise ValueError("search requires a fund name or code")
            payload = search_funds(fetcher, query)
        elif args.command in {"buy-point", "analyze"}:
            if not query:
                raise ValueError("buy-point requires a fund name or code")
            payload = buy_point_evidence(fetcher, query)
        elif args.command in {"compare-industry", "industry-compare"}:
            if not query:
                raise ValueError("compare-industry requires an industry term")
            payload = IndustryCompareAnalyzer(fetcher).compare(query)
        else:
            payload = screen_funds(fetcher)
        emit(payload, args.output)
    except RuntimeError as exc:
        emit({"success": False, "message": "runtime_error", "detail": str(exc)}, args.output)
        sys.exit(1)
    except Exception as exc:
        emit({"success": False, "message": "error", "detail": str(exc)}, args.output)
        sys.exit(1)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    main()
