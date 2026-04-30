#!/usr/bin/env python3
"""
Atomic Tushare data fetcher for the a-stock-analyzer skill.

This module intentionally does not generate analysis, prompts, reports,
recommendations, or combined outputs. It only resolves stock identifiers and
fetches one requested dataset at a time.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import pandas as pd

try:
    import tushare as ts
except ImportError as exc:
    raise RuntimeError("Missing dependency: install tushare before using this fetcher.") from exc

try:
    import requests
except ImportError:
    requests = None

try:
    from PyPDF2 import PdfReader
except ImportError:
    PdfReader = None


DEFAULT_CNINFO_TIMEOUT = 15
CNINFO_ALLOWED_HOSTS = {"static.cninfo.com.cn", "www.cninfo.com.cn"}
CNINFO_REPORT_TYPES = {"annual", "q1", "half", "q3", "all"}
CNINFO_REPORT_TYPE_ORDER = {"annual": 4, "half": 3, "q3": 2, "q1": 1}
CNINFO_REPORT_TYPE_LABELS = {
    "annual": "年度报告",
    "q1": "一季度报告",
    "half": "半年度报告",
    "q3": "三季度报告",
}
CNINFO_REPORT_CATEGORIES = {
    "annual": "category_ndbg_szsh",
    "half": "category_bndbg_szsh",
    "q1": "category_yjdbg_szsh",
    "q3": "category_sjdbg_szsh",
}
REPORT_SUMMARY_PATTERNS = (
    "摘要",
    "英文版",
    "外文版",
    "提示性公告",
    "问询",
    "回复",
)
REPORT_CORRECTION_PATTERNS = ("更正", "修订", "更新", "补充")


def get_tushare_token() -> str:
    """Read TUSHARE_TOKEN from the environment or cwd/.env."""
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token

    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(env_path):
        return ""

    with open(env_path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "TUSHARE_TOKEN":
                return value.strip().strip('"').strip("'")
    return ""


def get_tushare_pro(token: Optional[str] = None):
    """Create a Tushare Pro client."""
    resolved_token = token or get_tushare_token()
    if not resolved_token:
        raise RuntimeError("Missing TUSHARE_TOKEN. Set it in the environment or cwd/.env.")
    return ts.pro_api(resolved_token)


def normalize_ts_code(code: str) -> str:
    """Normalize common A-share code formats to Tushare ts_code."""
    if not code or not str(code).strip():
        raise ValueError("Stock code cannot be empty.")

    raw = str(code).strip()
    upper = raw.upper()

    if "." in upper:
        prefix, suffix = upper.split(".", 1)
        suffix = suffix.replace("SS", "SH")
        if suffix in {"SH", "SZ", "BJ"}:
            return f"{prefix}.{suffix}"

    if upper.startswith(("SH", "SZ", "BJ")) and len(upper) >= 8:
        return f"{upper[2:]}.{upper[:2]}"

    if raw.isdigit() and len(raw) == 6:
        if raw.startswith(("0", "3")):
            return f"{raw}.SZ"
        if raw.startswith(("6", "9")):
            return f"{raw}.SH"
        if raw.startswith(("4", "8")):
            return f"{raw}.BJ"

    return upper


def dataframe_to_records(df: Optional[pd.DataFrame]) -> List[Dict[str, Any]]:
    """Convert a DataFrame to JSON-serializable records."""
    if df is None or df.empty:
        return []

    cleaned = df.copy()
    for column in cleaned.columns:
        if pd.api.types.is_datetime64_any_dtype(cleaned[column]):
            cleaned[column] = cleaned[column].dt.strftime("%Y-%m-%d")
    cleaned = cleaned.where(pd.notnull(cleaned), None)
    return cleaned.to_dict(orient="records")


def require_requests() -> Any:
    """Ensure requests is installed before using CNInfo-based features."""
    if requests is None:
        raise RuntimeError("Missing dependency: install requests before using CNInfo report retrieval.")
    return requests


def cninfo_headers() -> Dict[str, str]:
    """Headers for CNInfo announcement endpoints."""
    return {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Host": "www.cninfo.com.cn",
        "Origin": "http://www.cninfo.com.cn",
        "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }


def build_cninfo_url(path_or_url: str) -> str:
    """Normalize a CNInfo announcement path or URL to a full URL."""
    raw = str(path_or_url or "").strip()
    if not raw:
        raise ValueError("CNInfo report URL cannot be empty.")
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        if parsed.hostname not in CNINFO_ALLOWED_HOSTS:
            raise ValueError(f"Unsupported CNInfo host: {parsed.hostname}")
        return raw

    normalized = raw.lstrip("/")
    return f"http://static.cninfo.com.cn/{normalized}"


def sanitize_filename(value: str) -> str:
    """Sanitize a value for use as a filename on Windows."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", str(value or "").strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "report"


def build_report_date_range(report_year: Optional[int]) -> str:
    """Build a CNInfo date range that covers report disclosures."""
    today = datetime.now()
    if report_year:
        start = datetime(report_year, 1, 1)
        end = datetime(report_year + 1, 12, 31)
    else:
        start = datetime(today.year - 5, 1, 1)
        end = today
    return f"{start:%Y-%m-%d}~{end:%Y-%m-%d}"


def classify_report_title(title: str) -> Dict[str, Any]:
    """Parse report title metadata from a CNInfo announcement title."""
    normalized = str(title or "").strip()
    report_type = ""
    if "第一季度报告" in normalized or "一季度报告" in normalized:
        report_type = "q1"
    elif "半年度报告" in normalized:
        report_type = "half"
    elif "第三季度报告" in normalized or "三季度报告" in normalized:
        report_type = "q3"
    elif "年度报告" in normalized and "半年度报告" not in normalized:
        report_type = "annual"

    year_match = re.search(r"(20\d{2})\s*年", normalized)
    report_year = int(year_match.group(1)) if year_match else None
    is_summary = any(token in normalized for token in REPORT_SUMMARY_PATTERNS)
    is_correction = any(token in normalized for token in REPORT_CORRECTION_PATTERNS)
    is_full_report = bool(report_type) and not is_summary

    return {
        "report_type": report_type,
        "report_type_label": CNINFO_REPORT_TYPE_LABELS.get(report_type, ""),
        "report_year": report_year,
        "is_summary": is_summary,
        "is_correction": is_correction,
        "is_full_report": is_full_report,
    }


def report_sort_key(item: Dict[str, Any]) -> Any:
    """Sort reports by fiscal period, disclosure date, and preferred variant."""
    report_year = int(item.get("report_year") or 0)
    report_type = str(item.get("report_type") or "")
    report_type_order = CNINFO_REPORT_TYPE_ORDER.get(report_type, 0)
    announcement_time = str(item.get("announcement_time") or "")
    full_report_order = 1 if item.get("is_full_report") else 0
    correction_order = 1 if item.get("is_correction") else 0
    return (
        report_year,
        report_type_order,
        announcement_time,
        full_report_order,
        correction_order,
    )


def classify_board(code6: str) -> str:
    """Classify an A-share board from the six-digit code."""
    if code6.startswith(("688", "689")):
        return "科创板"
    if code6.startswith(("300", "301")):
        return "创业板"
    if code6.startswith(("4", "8")):
        return "北交所"
    if code6.startswith(("0", "3", "6")):
        return "主板"
    return "其他"


class StockDataFetcher:
    """Atomic data access wrapper around Tushare Pro."""

    def __init__(self, token: Optional[str] = None):
        self.pro = get_tushare_pro(token)
        self._stock_list_cache: Optional[pd.DataFrame] = None

    def get_all_stocks(self) -> pd.DataFrame:
        """Fetch listed A-share stocks."""
        if self._stock_list_cache is not None:
            return self._stock_list_cache

        df = self.pro.stock_basic(
            list_status="L",
            fields="ts_code,symbol,name,area,industry,market,list_date",
        )
        if df is None or df.empty:
            self._stock_list_cache = pd.DataFrame()
            return self._stock_list_cache

        df = df.copy()
        df["code6"] = df["ts_code"].str.split(".").str[0]
        df["board"] = df["code6"].map(classify_board)
        self._stock_list_cache = df
        return df

    def search_stock(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search by six-digit code, ts_code, or Chinese stock name."""
        query = str(query).strip()
        if not query:
            return []

        stocks = self.get_all_stocks()
        if stocks.empty:
            return []

        upper = query.upper()
        if query.isdigit() and len(query) == 6:
            matched = stocks[stocks["code6"] == query]
        elif "." in upper:
            matched = stocks[stocks["ts_code"] == normalize_ts_code(upper)]
        else:
            by_name = stocks[stocks["name"].str.contains(query, case=False, na=False)]
            by_code = stocks[stocks["code6"].str.contains(query, na=False)]
            matched = pd.concat([by_name, by_code]).drop_duplicates(subset=["ts_code"])

        return dataframe_to_records(matched.head(limit))

    def resolve_ts_code(self, query: str) -> str:
        """Resolve a query to the first matching ts_code."""
        if "." in str(query) or (str(query).isdigit() and len(str(query)) == 6):
            return normalize_ts_code(str(query))

        matches = self.search_stock(query, limit=1)
        if not matches:
            raise ValueError(f"No stock matched query: {query}")
        return str(matches[0]["ts_code"])

    def get_company(self, ts_code: str) -> Optional[Dict[str, Any]]:
        """Fetch listed company profile."""
        df = self.pro.stock_company(ts_code=normalize_ts_code(ts_code))
        records = dataframe_to_records(df)
        return records[0] if records else None

    def get_financial_indicators(self, ts_code: str, limit: int = 12) -> pd.DataFrame:
        """Fetch financial indicator periods."""
        df = self.pro.fina_indicator(ts_code=normalize_ts_code(ts_code), limit=limit)
        return self._sort_by_date(df, "end_date", ascending=False)

    def get_income(self, ts_code: str, limit: int = 12) -> pd.DataFrame:
        """Fetch income statement periods."""
        df = self.pro.income(ts_code=normalize_ts_code(ts_code), limit=limit)
        return self._sort_by_date(df, "end_date", ascending=False)

    def get_balance(self, ts_code: str, limit: int = 12) -> pd.DataFrame:
        """Fetch balance sheet periods."""
        df = self.pro.balancesheet(ts_code=normalize_ts_code(ts_code), limit=limit)
        return self._sort_by_date(df, "end_date", ascending=False)

    def get_cashflow(self, ts_code: str, limit: int = 12) -> pd.DataFrame:
        """Fetch cash flow statement periods."""
        df = self.pro.cashflow(ts_code=normalize_ts_code(ts_code), limit=limit)
        return self._sort_by_date(df, "end_date", ascending=False)

    def get_main_business(self, ts_code: str, bz_type: str = "P", limit: int = 30) -> pd.DataFrame:
        """Fetch main business composition by product (P) or region (D)."""
        df = self.pro.fina_mainbz(ts_code=normalize_ts_code(ts_code), type=bz_type)
        df = self._sort_by_date(df, "end_date", ascending=False)
        return df.head(limit) if not df.empty else df

    def get_top10_holders(self, ts_code: str, periods: int = 4) -> pd.DataFrame:
        """Fetch top 10 holders for recent reporting periods."""
        df = self.pro.top10_holders(ts_code=normalize_ts_code(ts_code))
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.sort_values(["end_date", "hold_ratio"], ascending=[False, False])
        latest_periods = list(df["end_date"].dropna().unique()[:periods])
        return df[df["end_date"].isin(latest_periods)]

    def get_managers(self, ts_code: str) -> pd.DataFrame:
        """Fetch current disclosed managers."""
        df = self.pro.stk_managers(ts_code=normalize_ts_code(ts_code))
        if df is None or df.empty or "ann_date" not in df.columns:
            return pd.DataFrame()
        latest = df["ann_date"].max()
        return df[df["ann_date"] == latest]

    def get_rewards(self, ts_code: str) -> pd.DataFrame:
        """Fetch latest management rewards and holdings."""
        df = self.pro.stk_rewards(ts_code=normalize_ts_code(ts_code))
        if df is None or df.empty or "end_date" not in df.columns:
            return pd.DataFrame()
        latest = df["end_date"].max()
        return df[df["end_date"] == latest]

    def get_share_float(self, ts_code: str, days: int = 1095, limit: int = 50) -> pd.DataFrame:
        """Fetch upcoming restricted-share unlocks."""
        start_date = datetime.now().strftime("%Y%m%d")
        end_date = (datetime.now() + timedelta(days=days)).strftime("%Y%m%d")
        df = self.pro.share_float(
            ts_code=normalize_ts_code(ts_code),
            start_date=start_date,
            end_date=end_date,
        )
        df = self._sort_by_date(df, "float_date", ascending=True)
        return df.head(limit) if not df.empty else df

    def get_block_trade(self, ts_code: str, days: int = 90, limit: int = 100) -> pd.DataFrame:
        """Fetch recent block trades."""
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        df = self.pro.block_trade(
            ts_code=normalize_ts_code(ts_code),
            start_date=start_date,
            end_date=end_date,
        )
        df = self._sort_by_date(df, "trade_date", ascending=False)
        return df.head(limit) if not df.empty else df

    def get_holder_trade(self, ts_code: str, days: int = 365, limit: int = 100) -> pd.DataFrame:
        """Fetch shareholder increase/decrease records."""
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        df = self.pro.stk_holdertrade(
            ts_code=normalize_ts_code(ts_code),
            start_date=start_date,
            end_date=end_date,
        )
        df = self._sort_by_date(df, "ann_date", ascending=False)
        return df.head(limit) if not df.empty else df

    def get_holder_number(self, ts_code: str, limit: int = 50) -> pd.DataFrame:
        """Fetch shareholder count periods."""
        df = self.pro.stk_holdernumber(ts_code=normalize_ts_code(ts_code))
        df = self._sort_by_date(df, "end_date", ascending=False)
        return df.head(limit) if not df.empty else df

    def get_daily(self, ts_code: str, limit: int = 120) -> pd.DataFrame:
        """Fetch daily OHLCV bars."""
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=max(limit * 2, 30))).strftime("%Y%m%d")
        df = self.pro.daily(
            ts_code=normalize_ts_code(ts_code),
            start_date=start_date,
            end_date=end_date,
        )
        df = self._sort_by_date(df, "trade_date", ascending=False)
        return df.head(limit) if not df.empty else df

    def get_daily_basic(self, ts_code: str, limit: int = 60) -> pd.DataFrame:
        """Fetch daily valuation and market snapshot fields."""
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=max(limit * 2, 30))).strftime("%Y%m%d")
        df = self.pro.daily_basic(
            ts_code=normalize_ts_code(ts_code),
            start_date=start_date,
            end_date=end_date,
            fields=(
                "ts_code,trade_date,close,turnover_rate,volume_ratio,pe,pe_ttm,"
                "pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,total_mv,circ_mv"
            ),
        )
        df = self._sort_by_date(df, "trade_date", ascending=False)
        return df.head(limit) if not df.empty else df

    def get_cninfo_orgid(self, stock_code: str, timeout: int = DEFAULT_CNINFO_TIMEOUT) -> Optional[str]:
        """Resolve a stock code to CNInfo orgId."""
        http = require_requests()
        response = http.post(
            "http://www.cninfo.com.cn/new/information/topSearch/query",
            data={"keyWord": stock_code, "maxNum": 10},
            headers=cninfo_headers(),
            timeout=timeout,
        )
        response.raise_for_status()
        results = response.json()
        if not isinstance(results, list) or not results:
            return None

        for item in results:
            if item.get("code") == stock_code and item.get("orgId"):
                return str(item["orgId"])

        first_org_id = results[0].get("orgId")
        return str(first_org_id) if first_org_id else None

    def resolve_cninfo_stock(self, ts_code: str, timeout: int = DEFAULT_CNINFO_TIMEOUT) -> str:
        """Resolve a Tushare ts_code to CNInfo stock argument format."""
        normalized = normalize_ts_code(ts_code)
        stock_code = normalized.split(".", 1)[0]
        org_id = self.get_cninfo_orgid(stock_code, timeout=timeout)
        return f"{stock_code},{org_id}" if org_id else stock_code

    def query_cninfo_announcements(
        self,
        *,
        stock: str,
        category: str,
        date_range: str,
        page_size: int,
        timeout: int,
    ) -> List[Dict[str, Any]]:
        """Query CNInfo announcements for one stock and one category."""
        http = require_requests()
        response = http.post(
            "http://www.cninfo.com.cn/new/hisAnnouncement/query",
            data={
                "pageNum": 1,
                "pageSize": int(page_size),
                "column": "szse",
                "tabName": "fulltext",
                "plate": "",
                "stock": stock,
                "searchkey": "",
                "secid": "",
                "category": category,
                "trade": "",
                "seDate": date_range,
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            },
            headers=cninfo_headers(),
            timeout=timeout,
        )
        response.raise_for_status()
        results = response.json()
        announcements = results.get("announcements") or []

        normalized_items: List[Dict[str, Any]] = []
        for item in announcements:
            announcement_time = item.get("announcementTime")
            publish_date = ""
            if announcement_time:
                publish_date = datetime.fromtimestamp(announcement_time / 1000).strftime("%Y-%m-%d")

            adjunct_url = item.get("adjunctUrl") or ""
            report_meta = classify_report_title(item.get("announcementTitle") or "")

            normalized_items.append(
                {
                    "announcement_time": publish_date,
                    "sec_name": item.get("secName", ""),
                    "sec_code": item.get("secCode", ""),
                    "title": item.get("announcementTitle", ""),
                    "adjunct_url": build_cninfo_url(adjunct_url) if adjunct_url else "",
                    "announcement_id": item.get("announcementId", ""),
                    "cninfo_category": category,
                    **report_meta,
                }
            )

        return normalized_items

    def get_report_announcements(
        self,
        ts_code: str,
        *,
        report_type: str = "all",
        report_year: Optional[int] = None,
        limit: int = 12,
        include_variants: bool = False,
        timeout: int = DEFAULT_CNINFO_TIMEOUT,
    ) -> List[Dict[str, Any]]:
        """Fetch annual/quarter report announcement metadata from CNInfo."""
        normalized_type = str(report_type or "all").strip().lower()
        if normalized_type not in CNINFO_REPORT_TYPES:
            raise ValueError(f"Unsupported report type: {normalized_type}")

        cninfo_stock = self.resolve_cninfo_stock(ts_code, timeout=timeout)
        date_range = build_report_date_range(report_year)
        requested_types = [normalized_type] if normalized_type != "all" else ["annual", "half", "q3", "q1"]
        page_size = max(50, min(max(limit, 1) * 8, 100))
        deduped: Dict[str, Dict[str, Any]] = {}

        for current_type in requested_types:
            items = self.query_cninfo_announcements(
                stock=cninfo_stock,
                category=CNINFO_REPORT_CATEGORIES[current_type],
                date_range=date_range,
                page_size=page_size,
                timeout=timeout,
            )
            for item in items:
                if report_year and item.get("report_year") != report_year:
                    continue
                if not include_variants and not item.get("is_full_report"):
                    continue
                key = str(item.get("announcement_id") or item.get("adjunct_url") or item.get("title"))
                deduped[key] = item

        reports = sorted(deduped.values(), key=report_sort_key, reverse=True)
        return reports[:limit]

    def get_raw_report(
        self,
        ts_code: str,
        *,
        report_type: str = "all",
        report_year: Optional[int] = None,
        report_index: int = 1,
        include_variants: bool = False,
        download_dir: Optional[str] = None,
        timeout: int = DEFAULT_CNINFO_TIMEOUT,
    ) -> Dict[str, Any]:
        """Return one report metadata record and optionally download the PDF."""
        reports = self.get_report_announcements(
            ts_code,
            report_type=report_type,
            report_year=report_year,
            limit=max(report_index, 20),
            include_variants=include_variants,
            timeout=timeout,
        )
        if not reports:
            raise ValueError("No matching annual/quarter reports were found.")
        if report_index < 1 or report_index > len(reports):
            raise ValueError(f"report_index out of range: {report_index} (available: {len(reports)})")

        selected = dict(reports[report_index - 1])
        selected["selected_index"] = report_index
        selected["candidate_count"] = len(reports)
        selected["download_url"] = selected.get("adjunct_url", "")
        selected["saved_path"] = None
        if not download_dir:
            return selected

        report_bytes = self.download_report_bytes(selected["download_url"], timeout=timeout)
        output_dir = Path(download_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = sanitize_filename(f"{selected.get('sec_code', '')}_{selected.get('title', '')}.pdf")
        file_path = output_dir / filename
        file_path.write_bytes(report_bytes)
        selected["saved_path"] = str(file_path.resolve())
        selected["file_size_bytes"] = len(report_bytes)
        return selected

    def get_report_text(
        self,
        ts_code: str,
        *,
        report_type: str = "all",
        report_year: Optional[int] = None,
        report_index: int = 1,
        include_variants: bool = False,
        max_pages: int = 120,
        max_chars: int = 60000,
        timeout: int = DEFAULT_CNINFO_TIMEOUT,
    ) -> Dict[str, Any]:
        """Download one report PDF and extract plain text."""
        if PdfReader is None:
            raise RuntimeError("Missing dependency: install PyPDF2 before using report-text.")

        selected = self.get_raw_report(
            ts_code,
            report_type=report_type,
            report_year=report_year,
            report_index=report_index,
            include_variants=include_variants,
            download_dir=None,
            timeout=timeout,
        )
        report_bytes = self.download_report_bytes(selected["download_url"], timeout=timeout)
        reader = PdfReader(io.BytesIO(report_bytes))

        total_pages = len(reader.pages)
        pages_to_read = total_pages if max_pages <= 0 else min(total_pages, max_pages)
        extracted_chunks: List[str] = []
        current_chars = 0
        for page_index in range(pages_to_read):
            page = reader.pages[page_index]
            page_text = (page.extract_text() or "").strip()
            if not page_text:
                continue
            remaining_chars = max_chars - current_chars
            if remaining_chars <= 0:
                break
            if len(page_text) > remaining_chars:
                extracted_chunks.append(page_text[:remaining_chars])
                current_chars += remaining_chars
                break
            extracted_chunks.append(page_text)
            current_chars += len(page_text)

        selected["page_count"] = total_pages
        selected["extracted_pages"] = pages_to_read
        selected["text_length"] = sum(len(chunk) for chunk in extracted_chunks)
        selected["text"] = "\n\n".join(extracted_chunks)
        return selected

    def download_report_bytes(self, url: str, timeout: int = DEFAULT_CNINFO_TIMEOUT) -> bytes:
        """Download one CNInfo PDF file and return its raw bytes."""
        http = require_requests()
        normalized_url = build_cninfo_url(url)
        response = http.get(normalized_url, timeout=timeout)
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type") or "")
        if "pdf" not in content_type.lower():
            raise RuntimeError(f"Unexpected report content type: {content_type or 'unknown'}")
        return response.content

    @staticmethod
    def _sort_by_date(df: Optional[pd.DataFrame], column: str, ascending: bool) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        if column not in df.columns:
            return df
        return df.sort_values(column, ascending=ascending)


DatasetHandler = Callable[[StockDataFetcher, argparse.Namespace], Any]


def _dataset_handlers() -> Dict[str, DatasetHandler]:
    return {
        "company": lambda fetcher, args: fetcher.get_company(args.query),
        "financial": lambda fetcher, args: fetcher.get_financial_indicators(args.query, args.limit),
        "income": lambda fetcher, args: fetcher.get_income(args.query, args.limit),
        "balance": lambda fetcher, args: fetcher.get_balance(args.query, args.limit),
        "cashflow": lambda fetcher, args: fetcher.get_cashflow(args.query, args.limit),
        "daily": lambda fetcher, args: fetcher.get_daily(args.query, args.limit),
        "daily-basic": lambda fetcher, args: fetcher.get_daily_basic(args.query, args.limit),
        "main-business-product": lambda fetcher, args: fetcher.get_main_business(args.query, "P", args.limit),
        "main-business-region": lambda fetcher, args: fetcher.get_main_business(args.query, "D", args.limit),
        "top10-holders": lambda fetcher, args: fetcher.get_top10_holders(args.query, args.limit),
        "managers": lambda fetcher, args: fetcher.get_managers(args.query),
        "rewards": lambda fetcher, args: fetcher.get_rewards(args.query),
        "share-float": lambda fetcher, args: fetcher.get_share_float(args.query, limit=args.limit),
        "block-trade": lambda fetcher, args: fetcher.get_block_trade(args.query, limit=args.limit),
        "holder-trade": lambda fetcher, args: fetcher.get_holder_trade(args.query, limit=args.limit),
        "holder-number": lambda fetcher, args: fetcher.get_holder_number(args.query, args.limit),
        "report-list": (
            lambda fetcher, args: fetcher.get_report_announcements(
                args.query,
                report_type=args.report_type,
                report_year=args.report_year,
                limit=args.limit,
                include_variants=args.include_report_variants,
                timeout=args.timeout,
            )
        ),
        "report-raw": (
            lambda fetcher, args: fetcher.get_raw_report(
                args.query,
                report_type=args.report_type,
                report_year=args.report_year,
                report_index=args.report_index,
                include_variants=args.include_report_variants,
                download_dir=args.download_dir,
                timeout=args.timeout,
            )
        ),
        "report-text": (
            lambda fetcher, args: fetcher.get_report_text(
                args.query,
                report_type=args.report_type,
                report_year=args.report_year,
                report_index=args.report_index,
                include_variants=args.include_report_variants,
                max_pages=args.max_pages,
                max_chars=args.max_chars,
                timeout=args.timeout,
            )
        ),
    }


def serialize_payload(payload: Any) -> Any:
    """Convert supported fetch results to JSON-serializable values."""
    if isinstance(payload, pd.DataFrame):
        return dataframe_to_records(payload)
    if isinstance(payload, dict) or payload is None:
        return payload
    return payload


def print_payload(payload: Any, output_format: str) -> None:
    """Print raw fetched data in JSON or CSV."""
    if output_format == "csv":
        if isinstance(payload, pd.DataFrame):
            print(payload.to_csv(index=False))
            return
        print(pd.DataFrame([payload] if isinstance(payload, dict) else payload).to_csv(index=False))
        return

    print(json.dumps(serialize_payload(payload), ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch one Tushare dataset without analysis.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="Search listed A-share stocks.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--format", choices=["json", "csv"], default="json")

    fetch = subparsers.add_parser("fetch", help="Fetch one dataset for one stock.")
    fetch.add_argument("dataset", choices=sorted(_dataset_handlers().keys()))
    fetch.add_argument("query", help="Stock name, six-digit code, or ts_code.")
    fetch.add_argument("--limit", type=int, default=12)
    fetch.add_argument("--format", choices=["json", "csv"], default="json")
    fetch.add_argument("--report-type", choices=sorted(CNINFO_REPORT_TYPES), default="all")
    fetch.add_argument("--report-year", type=int)
    fetch.add_argument("--report-index", type=int, default=1)
    fetch.add_argument("--include-report-variants", action="store_true")
    fetch.add_argument("--download-dir")
    fetch.add_argument("--max-pages", type=int, default=120)
    fetch.add_argument("--max-chars", type=int, default=60000)
    fetch.add_argument("--timeout", type=int, default=DEFAULT_CNINFO_TIMEOUT)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    fetcher = StockDataFetcher()

    if args.command == "search":
        payload = fetcher.search_stock(args.query, limit=args.limit)
        print_payload(payload, args.format)
        return 0

    if args.command == "fetch":
        if args.dataset in {"report-raw", "report-text"} and args.report_index < 1:
            raise ValueError("report-index must be >= 1.")
        ts_code = fetcher.resolve_ts_code(args.query)
        args.query = ts_code
        handler = _dataset_handlers()[args.dataset]
        payload = handler(fetcher, args)
        print_payload(payload, args.format)
        return 0

    return 1


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
