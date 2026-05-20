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
DEFAULT_CNINFO_PAGE_SIZE = 30
CNINFO_ALLOWED_HOSTS = {"static.cninfo.com.cn", "www.cninfo.com.cn"}
CNINFO_ALLOWED_TAB_TYPES = {"fulltext", "relation"}
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

INQUIRY_REPLY_PATTERNS = [
    r"问询.*回复",
    r"回复.*问询",
    r"问询函回复",
    r"审核问询.*回复",
    r"反馈意见.*回复",
]
ABNORMAL_VOLATILITY_PATTERNS = [r"股价异常波动", r"股票交易异常波动"]
REGULATORY_PATTERNS = [r"监管函", r"关注函", r"警示函", r"监管工作函", r"纪律处分"]
CAPITAL_EMPLOYEE_PATTERNS = [r"员工持股计划"]
CAPITAL_PRIVATE_OFFER_PATTERNS = [r"向特定对象发行", r"特定对象发行", r"定向增发"]
CAPITAL_EQUITY_INCENTIVE_PATTERNS = [r"股权激励", r"限制性股票", r"股票期权", r"激励计划"]
INCREASE_HOLD_PATTERNS = [r"增持", r"拟增持", r"增持计划"]
DECREASE_HOLD_PATTERNS = [r"减持", r"拟减持", r"减持计划", r"减持股份"]
DECREASE_PROGRESS_PATTERNS = [
    r"减持进展",
    r"减持进度",
    r"减持计划完成",
    r"减持时间过半",
    r"减持计划时间过半",
    r"减持数量过半",
    r"减持计划届满",
    r"实施进展",
]
BUYBACK_PATTERNS = [r"回购", r"股份回购"]
MAJOR_COOPERATION_PATTERNS = [r"重大合作", r"战略合作", r"合作框架", r"签署.*协议", r"投资.*项目", r"项目投资", r"中标", r"中选"]
QUICK_REPORT_PATTERNS = [r"业绩快报", r"季度业绩快报", r"半年度业绩快报", r"年度业绩快报"]
ORIGINAL_TEXT_REQUIRED_TAGS = {
    "问询回复",
    "监管函",
    "资本运作",
    "员工持股计划",
    "特定对象发行",
    "股权激励",
    "增持",
    "减持",
    "回购",
    "合作",
    "投资项目",
    "快报",
    "业绩快报",
}


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


def normalize_yyyymmdd(value: Optional[str]) -> Optional[str]:
    """Normalize YYYY-MM-DD or YYYYMMDD to YYYYMMDD."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{8}", raw):
        return raw
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw.replace("-", "")
    raise ValueError(f"Date must be YYYYMMDD or YYYY-MM-DD: {value}")


def default_cninfo_date_range(days: int = 365) -> str:
    """Build a CNInfo date range ending today."""
    today = datetime.now()
    start = today - timedelta(days=days)
    return f"{start:%Y-%m-%d}~{today:%Y-%m-%d}"


def validate_cninfo_date_range(value: Optional[str]) -> str:
    """Validate or default CNInfo date range."""
    if not value:
        return default_cninfo_date_range()
    raw = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}~\d{4}-\d{2}-\d{2}", raw):
        raise ValueError("CNInfo date range must be YYYY-MM-DD~YYYY-MM-DD.")
    return raw


def title_hits(title: str, patterns: List[str]) -> bool:
    """Return whether a title matches any regex pattern."""
    return any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in patterns)


def classify_cninfo_fulltext(title: Optional[str]) -> Dict[str, Any]:
    """Classify CNInfo announcement titles with local copied rules."""
    normalized = str(title or "").strip()

    if title_hits(normalized, INQUIRY_REPLY_PATTERNS):
        if title_hits(normalized, ABNORMAL_VOLATILITY_PATTERNS):
            return {
                "category": "上市公司公开信息",
                "subcategory": "其他",
                "rule_id": "cninfo.fulltext.excluded.abnormal_volatility_reply.v1",
                "excluded": True,
                "exclude_reason": "问询回复中的股价或股票交易异常波动公告",
                "tags": ["问询回复", "排除"],
            }
        return {
            "category": "上市公司公开信息",
            "subcategory": "问询回复",
            "rule_id": "cninfo.fulltext.inquiry_reply.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["问询回复"],
        }

    if title_hits(normalized, REGULATORY_PATTERNS):
        return {
            "category": "上市公司公开信息",
            "subcategory": "监管函",
            "rule_id": "cninfo.fulltext.regulatory_letter.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["监管函"],
        }

    if title_hits(normalized, CAPITAL_EMPLOYEE_PATTERNS):
        return {
            "category": "上市公司公开信息",
            "subcategory": "资本运作-员工持股计划",
            "rule_id": "cninfo.fulltext.capital.employee_stock_plan.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["资本运作", "员工持股计划"],
        }

    if title_hits(normalized, CAPITAL_PRIVATE_OFFER_PATTERNS):
        return {
            "category": "上市公司公开信息",
            "subcategory": "资本运作-特定对象发行",
            "rule_id": "cninfo.fulltext.capital.private_offering.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["资本运作", "特定对象发行"],
        }

    if title_hits(normalized, CAPITAL_EQUITY_INCENTIVE_PATTERNS):
        return {
            "category": "上市公司公开信息",
            "subcategory": "资本运作-股权激励",
            "rule_id": "cninfo.fulltext.capital.equity_incentive.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["资本运作", "股权激励"],
        }

    if title_hits(normalized, INCREASE_HOLD_PATTERNS):
        return {
            "category": "上市公司公开信息",
            "subcategory": "增持",
            "rule_id": "cninfo.fulltext.increase_hold.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["增持"],
        }

    if title_hits(normalized, DECREASE_HOLD_PATTERNS):
        if title_hits(normalized, DECREASE_PROGRESS_PATTERNS):
            return {
                "category": "上市公司公开信息",
                "subcategory": "其他",
                "rule_id": "cninfo.fulltext.excluded.decrease_progress.v1",
                "excluded": True,
                "exclude_reason": "减持进度或实施进展类公告",
                "tags": ["减持", "排除"],
            }
        return {
            "category": "上市公司公开信息",
            "subcategory": "减持",
            "rule_id": "cninfo.fulltext.decrease_hold.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["减持"],
        }

    if title_hits(normalized, BUYBACK_PATTERNS):
        return {
            "category": "上市公司公开信息",
            "subcategory": "回购",
            "rule_id": "cninfo.fulltext.buyback.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["回购"],
        }

    if title_hits(normalized, MAJOR_COOPERATION_PATTERNS):
        return {
            "category": "上市公司公开信息",
            "subcategory": "重大合作/投资项目",
            "rule_id": "cninfo.fulltext.major_cooperation_project.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["合作", "投资项目"],
        }

    if title_hits(normalized, QUICK_REPORT_PATTERNS):
        return {
            "category": "上市公司公开信息",
            "subcategory": "快报",
            "rule_id": "cninfo.fulltext.quick_report.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["快报", "业绩快报"],
        }

    return {
        "category": "上市公司公开信息",
        "subcategory": "其他",
        "rule_id": "cninfo.fulltext.other.v1",
        "excluded": False,
        "exclude_reason": "",
        "tags": ["其他"],
    }


def classify_cninfo_relation() -> Dict[str, Any]:
    """Classify CNInfo relation-tab items."""
    return {
        "category": "互动易/调研",
        "subcategory": "未定义",
        "rule_id": "cninfo.relation.unclassified.v1",
        "excluded": False,
        "exclude_reason": "",
        "tags": ["relation"],
    }


def announcement_needs_original_text(item: Dict[str, Any]) -> bool:
    """Flag events where title metadata should usually be upgraded to PDF text."""
    tags = set(item.get("tags") or [])
    return bool(tags & ORIGINAL_TEXT_REQUIRED_TAGS)


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


def compact_records(records: List[Dict[str, Any]], fields: List[str], limit: int) -> List[Dict[str, Any]]:
    """Keep a bounded list of records and selected fields for model context."""
    compacted: List[Dict[str, Any]] = []
    for record in records[: max(0, limit)]:
        compacted.append({field: record.get(field) for field in fields if field in record})
    return compacted


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


def extract_pdf_text(report_bytes: bytes, *, max_pages: int = 120, max_chars: int = 60000) -> Dict[str, Any]:
    """Extract bounded text from PDF bytes with PyPDF2."""
    if PdfReader is None:
        raise RuntimeError("Missing dependency: install PyPDF2 before using PDF text extraction.")

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

    return {
        "page_count": total_pages,
        "extracted_pages": pages_to_read,
        "text_length": sum(len(chunk) for chunk in extracted_chunks),
        "text": "\n\n".join(extracted_chunks),
    }


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

    def get_institutional_research(
        self,
        ts_code: str,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        trade_date: Optional[str] = None,
        limit: int = 50,
    ) -> pd.DataFrame:
        """Fetch institutional research records from Tushare stk_surv."""
        normalized = normalize_ts_code(ts_code)
        request_args: Dict[str, Any] = {"ts_code": normalized}
        normalized_trade_date = normalize_yyyymmdd(trade_date)
        if normalized_trade_date:
            request_args["trade_date"] = normalized_trade_date
        else:
            request_args["start_date"] = normalize_yyyymmdd(start_date) or (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
            request_args["end_date"] = normalize_yyyymmdd(end_date) or datetime.now().strftime("%Y%m%d")

        df = self.pro.stk_surv(**request_args)
        df = self._sort_by_date(df, "trade_date", ascending=False)
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

    def get_valuation_band(self, ts_code: str, years: int = 5) -> Dict[str, Any]:
        """Compute historical valuation percentile bands from daily_basic history.

        Tushare exposes no dedicated valuation-percentile / PE-band endpoint for
        individual stocks (only index_dailybasic covers indices). This derives the
        bands deterministically from daily_basic history. It only computes ranges
        and percentiles; interpreting them (cheap vs. expensive, which metric to
        trust per company type) is the model's job.
        """
        code = normalize_ts_code(ts_code)
        end = datetime.now()
        start = end - timedelta(days=int(years * 365.25) + 5)
        df = self.pro.daily_basic(
            ts_code=code,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            fields="ts_code,trade_date,close,pe_ttm,pb,ps_ttm,dv_ttm",
        )
        if df is None or df.empty:
            return {"ts_code": code, "error": "no daily_basic history returned"}

        df = df.sort_values("trade_date")
        latest_date = str(df["trade_date"].iloc[-1])
        # Metrics whose meaningful samples must be positive (a negative PE is a
        # loss, not a "cheap" reading); dividend yield keeps zeros.
        metrics = {"pe_ttm": True, "pb": True, "ps_ttm": True, "dv_ttm": False}
        windows = [w for w in (1, 3, 5) if w <= years] or [years]

        def _window_stats(series: "pd.Series", current: Optional[float]) -> Dict[str, Any]:
            s = pd.to_numeric(series, errors="coerce").dropna()
            if s.empty:
                return {"sample_size": 0}
            stats = {
                "sample_size": int(s.size),
                "min": round(float(s.min()), 4),
                "p25": round(float(s.quantile(0.25)), 4),
                "median": round(float(s.median()), 4),
                "mean": round(float(s.mean()), 4),
                "p75": round(float(s.quantile(0.75)), 4),
                "max": round(float(s.max()), 4),
            }
            if current is not None:
                stats["current_percentile"] = round(float((s <= current).mean()) * 100, 1)
            return stats

        bands: Dict[str, Any] = {}
        for metric, positive_only in metrics.items():
            col = pd.to_numeric(df[metric], errors="coerce")
            valid = col[col > 0] if positive_only else col.dropna()
            current = round(float(valid.iloc[-1]), 4) if not valid.empty else None
            per_window: Dict[str, Any] = {}
            for w in windows:
                cutoff = (end - timedelta(days=int(w * 365.25))).strftime("%Y%m%d")
                window_df = df[df["trade_date"] >= cutoff]
                window_col = pd.to_numeric(window_df[metric], errors="coerce")
                window_series = window_col[window_col > 0] if positive_only else window_col
                per_window[f"{w}y"] = _window_stats(window_series, current)
            bands[metric] = {
                "current": current,
                "positive_only": positive_only,
                "windows": per_window,
            }

        return {
            "ts_code": code,
            "latest_trade_date": latest_date,
            "requested_years": years,
            "history_start": str(df["trade_date"].iloc[0]),
            "total_trade_days": int(df.shape[0]),
            "bands": bands,
            "model_responsibility": (
                "脚本只给区间和分位。贵贱判断、用哪个指标（成熟龙头看 PE/PB 分位、红利股看 dv_ttm 分位、"
                "成长股 PE 分位仅作下行参考）、是否因增速降档而历史中枢失效，全部由模型按公司类型判断。"
            ),
        }

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

    def query_cninfo_announcement_page(
        self,
        *,
        stock: str,
        tab_type: str = "fulltext",
        searchkey: str = "",
        category: str = "",
        trade: str = "",
        date_range: Optional[str] = None,
        page_num: int = 1,
        page_size: int = DEFAULT_CNINFO_PAGE_SIZE,
        timeout: int = DEFAULT_CNINFO_TIMEOUT,
    ) -> Dict[str, Any]:
        """Query one CNInfo announcement page for one stock only."""
        if not stock:
            raise ValueError("CNInfo announcement queries require one stock.")
        if tab_type not in CNINFO_ALLOWED_TAB_TYPES:
            raise ValueError(f"Unsupported CNInfo tabtype: {tab_type}")

        http = require_requests()
        response = http.post(
            "http://www.cninfo.com.cn/new/hisAnnouncement/query",
            data={
                "pageNum": int(page_num),
                "pageSize": int(page_size),
                "column": "szse",
                "tabName": tab_type,
                "plate": "",
                "stock": stock,
                "searchkey": str(searchkey or "").strip(),
                "secid": "",
                "category": str(category or "").strip(),
                "trade": str(trade or "").strip(),
                "seDate": validate_cninfo_date_range(date_range),
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
            normalized_item = {
                "announcement_time": publish_date,
                "sec_name": item.get("secName", ""),
                "sec_code": item.get("secCode", ""),
                "title": item.get("announcementTitle", ""),
                "adjunct_url": build_cninfo_url(adjunct_url) if adjunct_url else "",
                "announcement_id": item.get("announcementId", ""),
                "cninfo_tabtype": tab_type,
                "cninfo_category": category,
            }
            rule_result = classify_cninfo_fulltext(normalized_item["title"]) if tab_type == "fulltext" else classify_cninfo_relation()
            normalized_item.update(rule_result)
            normalized_item["needs_original_text"] = announcement_needs_original_text(normalized_item)
            normalized_items.append(normalized_item)

        return {
            "total_pages": results.get("totalpages"),
            "total_records": results.get("totalRecordNum"),
            "announcements": normalized_items,
        }

    def get_announcements(
        self,
        ts_code: str,
        *,
        tab_type: str = "fulltext",
        date_range: Optional[str] = None,
        searchkey: str = "",
        category: str = "",
        trade: str = "",
        page_num: int = 1,
        page_size: int = DEFAULT_CNINFO_PAGE_SIZE,
        limit: int = 30,
        include_excluded: bool = False,
        timeout: int = DEFAULT_CNINFO_TIMEOUT,
    ) -> Dict[str, Any]:
        """Fetch classified CNInfo announcements for one stock."""
        normalized = normalize_ts_code(ts_code)
        cninfo_stock = self.resolve_cninfo_stock(normalized, timeout=timeout)
        result = self.query_cninfo_announcement_page(
            stock=cninfo_stock,
            tab_type=tab_type,
            searchkey=searchkey,
            category=category,
            trade=trade,
            date_range=date_range,
            page_num=page_num,
            page_size=page_size,
            timeout=timeout,
        )
        announcements = result.get("announcements", [])
        if not include_excluded:
            announcements = [item for item in announcements if not item.get("excluded")]
        if limit > 0:
            announcements = announcements[:limit]
        return {
            "query": {
                "ts_code": normalized,
                "cninfo_stock": cninfo_stock,
                "tabtype": tab_type,
                "date": validate_cninfo_date_range(date_range),
                "searchkey": str(searchkey or "").strip(),
                "category": str(category or "").strip(),
                "trade": str(trade or "").strip(),
                "page_num": int(page_num),
                "page_size": int(page_size),
                "include_excluded": include_excluded,
                "limit": int(limit),
            },
            "total_pages": result.get("total_pages"),
            "total_records": result.get("total_records"),
            "filtered_count": len(announcements),
            "announcements": announcements,
        }

    def get_raw_announcement(
        self,
        ts_code: str,
        *,
        tab_type: str = "fulltext",
        date_range: Optional[str] = None,
        searchkey: str = "",
        category: str = "",
        trade: str = "",
        announcement_index: int = 1,
        include_excluded: bool = False,
        download_dir: Optional[str] = None,
        timeout: int = DEFAULT_CNINFO_TIMEOUT,
    ) -> Dict[str, Any]:
        """Return one announcement metadata record and optionally download its PDF."""
        if announcement_index < 1:
            raise ValueError("announcement-index must be >= 1.")
        result = self.get_announcements(
            ts_code,
            tab_type=tab_type,
            date_range=date_range,
            searchkey=searchkey,
            category=category,
            trade=trade,
            page_num=1,
            page_size=max(DEFAULT_CNINFO_PAGE_SIZE, announcement_index * 2),
            limit=max(DEFAULT_CNINFO_PAGE_SIZE, announcement_index),
            include_excluded=include_excluded,
            timeout=timeout,
        )
        announcements = result.get("announcements") or []
        if not announcements:
            raise ValueError("No matching CNInfo announcements were found.")
        if announcement_index > len(announcements):
            raise ValueError(f"announcement_index out of range: {announcement_index} (available: {len(announcements)})")

        selected = dict(announcements[announcement_index - 1])
        selected["selected_index"] = announcement_index
        selected["candidate_count"] = len(announcements)
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

    def get_announcement_text(
        self,
        ts_code: str,
        *,
        tab_type: str = "fulltext",
        date_range: Optional[str] = None,
        searchkey: str = "",
        category: str = "",
        trade: str = "",
        announcement_index: int = 1,
        include_excluded: bool = False,
        max_pages: int = 120,
        max_chars: int = 60000,
        timeout: int = DEFAULT_CNINFO_TIMEOUT,
    ) -> Dict[str, Any]:
        """Download one announcement PDF and extract plain text."""
        selected = self.get_raw_announcement(
            ts_code,
            tab_type=tab_type,
            date_range=date_range,
            searchkey=searchkey,
            category=category,
            trade=trade,
            announcement_index=announcement_index,
            include_excluded=include_excluded,
            download_dir=None,
            timeout=timeout,
        )
        report_bytes = self.download_report_bytes(selected["download_url"], timeout=timeout)
        selected.update(extract_pdf_text(report_bytes, max_pages=max_pages, max_chars=max_chars))
        return selected

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
        selected.update(extract_pdf_text(report_bytes, max_pages=max_pages, max_chars=max_chars))
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


def fetch_or_error(name: str, fetch_fn: Callable[[], Any]) -> Dict[str, Any]:
    """Fetch a dataset for evidence-pack and preserve per-dataset errors."""
    try:
        payload = serialize_payload(fetch_fn())
        return {"ok": True, "data": payload, "error": None}
    except Exception as exc:  # noqa: BLE001 - evidence packs should degrade per dataset.
        return {"ok": False, "data": [] if name != "company" else None, "error": str(exc)}


def latest_records(dataset: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """Return bounded records from a packed dataset."""
    data = dataset.get("data") if isinstance(dataset, dict) else []
    if isinstance(data, list):
        return data[:limit]
    if isinstance(data, dict):
        return [data]
    return []


def build_analysis_context(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Build compact model context from the full evidence pack."""
    datasets = evidence.get("datasets", {})
    company_data = (datasets.get("company") or {}).get("data") or {}
    announcements = ((datasets.get("announcements") or {}).get("data") or {}).get("announcements", [])
    original_text_candidates = [
        {
            "announcement_time": item.get("announcement_time"),
            "title": item.get("title"),
            "subcategory": item.get("subcategory"),
            "tags": item.get("tags"),
            "adjunct_url": item.get("adjunct_url"),
            "reason": "标题命中高信息密度事件，默认应进一步读取 PDF 原文。",
        }
        for item in announcements
        if item.get("needs_original_text")
    ][:5]

    research_fields = [
        "ts_code",
        "name",
        "trade_date",
        "surv_date",
        "fund_visitors",
        "rece_place",
        "rece_mode",
        "invest_org",
        "org_type",
        "comp_rece",
        "content",
    ]

    return {
        "metadata": {
            **(evidence.get("metadata") or {}),
            "context_generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "data_fetcher.build_analysis_context",
        },
        "company": company_data,
        "financial_snapshot": {
            "financial": latest_records(datasets.get("financial", {}), 8),
            "income": latest_records(datasets.get("income", {}), 8),
            "balance": latest_records(datasets.get("balance", {}), 8),
            "cashflow": latest_records(datasets.get("cashflow", {}), 8),
        },
        "valuation_market": {
            "daily_basic_latest": latest_records(datasets.get("daily-basic", {}), 5),
            "daily_latest": latest_records(datasets.get("daily", {}), 5),
            "valuation_band": (datasets.get("valuation-band") or {}).get("data"),
        },
        "business_structure": {
            "product": latest_records(datasets.get("main-business-product", {}), 20),
            "region": latest_records(datasets.get("main-business-region", {}), 20),
        },
        "governance": {
            "top10_holders": latest_records(datasets.get("top10-holders", {}), 40),
            "managers": latest_records(datasets.get("managers", {}), 20),
            "rewards": latest_records(datasets.get("rewards", {}), 20),
            "holder_number": latest_records(datasets.get("holder-number", {}), 20),
            "holder_trade": latest_records(datasets.get("holder-trade", {}), 30),
            "share_float": latest_records(datasets.get("share-float", {}), 20),
            "block_trade": latest_records(datasets.get("block-trade", {}), 30),
        },
        "announcements": {
            "summary": {
                "count": len(announcements),
                "original_text_candidate_count": len(original_text_candidates),
            },
            "recent": compact_records(
                announcements,
                [
                    "announcement_time",
                    "sec_name",
                    "sec_code",
                    "title",
                    "subcategory",
                    "tags",
                    "needs_original_text",
                    "adjunct_url",
                ],
                20,
            ),
            "original_text_candidates": original_text_candidates,
        },
        "reports": {
            "recent": compact_records(
                latest_records(datasets.get("report-list", {}), 8),
                [
                    "announcement_time",
                    "sec_name",
                    "sec_code",
                    "title",
                    "report_type",
                    "report_type_label",
                    "report_year",
                    "is_full_report",
                    "adjunct_url",
                ],
                8,
            )
        },
        "institutional_research": {
            "recent": compact_records(
                latest_records(datasets.get("institutional-research", {}), 30),
                research_fields,
                30,
            ),
            "model_responsibility": "机构调研频率、机构类型和问题关键词只验证市场关注度与主线连接，不等同于业绩改善。",
        },
        "dataset_errors": {
            name: payload.get("error")
            for name, payload in datasets.items()
            if isinstance(payload, dict) and not payload.get("ok")
        },
    }


def build_module_contexts(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Split compact context into subagent-friendly modules."""
    context = build_analysis_context(evidence)
    metadata = context.get("metadata", {})
    return {
        "meta": {
            "metadata": metadata,
            "subagent_contract": {
                "module1_growth_financial": ["module1_growth_financial.json", "SKILL.md 第四节 + 第五节 5.7", "成长与财务质量"],
                "module2_valuation_market": ["module2_valuation_market.json", "SKILL.md 第五节（先 5.1 分型，再 5.2 估值快照/分位带）", "估值与市场定价"],
                "module3_governance": ["module3_governance.json", "SKILL.md 第六节", "股东与治理"],
                "module4_announcements": ["module4_announcements.json", "SKILL.md 公告与原文升级规则", "公告事件验证"],
                "module5_research_mainline": ["module5_research_mainline.json", "SKILL.md 主线归属 + 机构调研方法", "机构调研与主线验证"],
            },
        },
        "module1_growth_financial": {
            "metadata": metadata,
            "company": context.get("company"),
            "financial_snapshot": context.get("financial_snapshot"),
            "business_structure": context.get("business_structure"),
        },
        "module2_valuation_market": {
            "metadata": metadata,
            "valuation_market": context.get("valuation_market"),
            "financial_snapshot": context.get("financial_snapshot"),
        },
        "module3_governance": {
            "metadata": metadata,
            "governance": context.get("governance"),
        },
        "module4_announcements": {
            "metadata": metadata,
            "announcements": context.get("announcements"),
            "reports": context.get("reports"),
        },
        "module5_research_mainline": {
            "metadata": metadata,
            "institutional_research": context.get("institutional_research"),
            "valuation_market": context.get("valuation_market"),
            "announcements": {
                "original_text_candidates": (context.get("announcements") or {}).get("original_text_candidates"),
            },
        },
    }


def build_evidence_pack(fetcher: StockDataFetcher, args: argparse.Namespace) -> Dict[str, Any]:
    """Build full single-stock evidence plus compact/module contexts."""
    ts_code = fetcher.resolve_ts_code(args.query)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    announcement_date = validate_cninfo_date_range(args.date)

    datasets: Dict[str, Dict[str, Any]] = {
        "company": fetch_or_error("company", lambda: fetcher.get_company(ts_code)),
        "financial": fetch_or_error("financial", lambda: fetcher.get_financial_indicators(ts_code, args.financial_limit)),
        "income": fetch_or_error("income", lambda: fetcher.get_income(ts_code, args.financial_limit)),
        "balance": fetch_or_error("balance", lambda: fetcher.get_balance(ts_code, args.financial_limit)),
        "cashflow": fetch_or_error("cashflow", lambda: fetcher.get_cashflow(ts_code, args.financial_limit)),
        "daily": fetch_or_error("daily", lambda: fetcher.get_daily(ts_code, args.market_limit)),
        "daily-basic": fetch_or_error("daily-basic", lambda: fetcher.get_daily_basic(ts_code, args.market_limit)),
        "valuation-band": fetch_or_error("valuation-band", lambda: fetcher.get_valuation_band(ts_code, years=args.band_years)),
        "main-business-product": fetch_or_error("main-business-product", lambda: fetcher.get_main_business(ts_code, "P", args.business_limit)),
        "main-business-region": fetch_or_error("main-business-region", lambda: fetcher.get_main_business(ts_code, "D", args.business_limit)),
        "top10-holders": fetch_or_error("top10-holders", lambda: fetcher.get_top10_holders(ts_code, args.holder_periods)),
        "managers": fetch_or_error("managers", lambda: fetcher.get_managers(ts_code)),
        "rewards": fetch_or_error("rewards", lambda: fetcher.get_rewards(ts_code)),
        "holder-number": fetch_or_error("holder-number", lambda: fetcher.get_holder_number(ts_code, args.holder_limit)),
        "holder-trade": fetch_or_error("holder-trade", lambda: fetcher.get_holder_trade(ts_code, limit=args.holder_limit)),
        "share-float": fetch_or_error("share-float", lambda: fetcher.get_share_float(ts_code, limit=args.holder_limit)),
        "block-trade": fetch_or_error("block-trade", lambda: fetcher.get_block_trade(ts_code, limit=args.holder_limit)),
        "report-list": fetch_or_error(
            "report-list",
            lambda: fetcher.get_report_announcements(
                ts_code,
                report_type=args.report_type,
                report_year=args.report_year,
                limit=args.report_limit,
                include_variants=args.include_report_variants,
                timeout=args.timeout,
            ),
        ),
        "announcements": fetch_or_error(
            "announcements",
            lambda: fetcher.get_announcements(
                ts_code,
                tab_type=args.tabtype,
                date_range=announcement_date,
                searchkey=args.searchkey,
                category=args.category,
                trade=args.trade,
                page_num=args.page_num,
                page_size=args.page_size,
                limit=args.announcement_limit,
                include_excluded=args.include_excluded,
                timeout=args.timeout,
            ),
        ),
        "institutional-research": fetch_or_error(
            "institutional-research",
            lambda: fetcher.get_institutional_research(
                ts_code,
                start_date=args.start_date,
                end_date=args.end_date,
                trade_date=args.trade_date,
                limit=args.research_limit,
            ),
        ),
    }
    evidence = {
        "metadata": {
            "type": "stock_ai_analyzer_evidence_pack",
            "ts_code": ts_code,
            "query": args.query,
            "generated_at": generated_at,
            "announcement_date": announcement_date,
            "source": "data_fetcher.build_evidence_pack",
        },
        "datasets": datasets,
    }
    return {
        "evidence": evidence,
        "analysis_context": build_analysis_context(evidence),
        "module_contexts": build_module_contexts(evidence),
    }


def write_json_file(path: str, payload: Any) -> str:
    """Write JSON to a path and return the resolved path."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(output_path.resolve())


def write_pack_outputs(package: Dict[str, Any], args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    """Write requested pack artifacts and return a manifest, if any files are written."""
    manifest: Dict[str, Any] = {}
    if args.evidence_out:
        manifest["evidence"] = write_json_file(args.evidence_out, package["evidence"])
    if args.context_out:
        manifest["analysis_context"] = write_json_file(args.context_out, package["analysis_context"])
    if args.module_context_dir:
        module_dir = Path(args.module_context_dir)
        module_dir.mkdir(parents=True, exist_ok=True)
        module_paths: Dict[str, str] = {}
        for name, payload in package["module_contexts"].items():
            path = module_dir / f"{name}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            module_paths[name] = str(path.resolve())
        manifest["module_context_dir"] = str(module_dir.resolve())
        manifest["module_contexts"] = module_paths
    if not manifest:
        return None
    manifest["ts_code"] = package["evidence"].get("metadata", {}).get("ts_code")
    return manifest


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
        "valuation-band": lambda fetcher, args: fetcher.get_valuation_band(args.query, years=args.years),
        "main-business-product": lambda fetcher, args: fetcher.get_main_business(args.query, "P", args.limit),
        "main-business-region": lambda fetcher, args: fetcher.get_main_business(args.query, "D", args.limit),
        "top10-holders": lambda fetcher, args: fetcher.get_top10_holders(args.query, args.limit),
        "managers": lambda fetcher, args: fetcher.get_managers(args.query),
        "rewards": lambda fetcher, args: fetcher.get_rewards(args.query),
        "share-float": lambda fetcher, args: fetcher.get_share_float(args.query, limit=args.limit),
        "block-trade": lambda fetcher, args: fetcher.get_block_trade(args.query, limit=args.limit),
        "holder-trade": lambda fetcher, args: fetcher.get_holder_trade(args.query, limit=args.limit),
        "holder-number": lambda fetcher, args: fetcher.get_holder_number(args.query, args.limit),
        "institutional-research": (
            lambda fetcher, args: fetcher.get_institutional_research(
                args.query,
                start_date=args.start_date,
                end_date=args.end_date,
                trade_date=args.trade_date,
                limit=args.limit,
            )
        ),
        "announcements": (
            lambda fetcher, args: fetcher.get_announcements(
                args.query,
                tab_type=args.tabtype,
                date_range=args.date,
                searchkey=args.searchkey,
                category=args.category,
                trade=args.trade,
                page_num=args.page_num,
                page_size=args.page_size,
                limit=args.limit,
                include_excluded=args.include_excluded,
                timeout=args.timeout,
            )
        ),
        "announcement-raw": (
            lambda fetcher, args: fetcher.get_raw_announcement(
                args.query,
                tab_type=args.tabtype,
                date_range=args.date,
                searchkey=args.searchkey,
                category=args.category,
                trade=args.trade,
                announcement_index=args.announcement_index,
                include_excluded=args.include_excluded,
                download_dir=args.download_dir,
                timeout=args.timeout,
            )
        ),
        "announcement-text": (
            lambda fetcher, args: fetcher.get_announcement_text(
                args.query,
                tab_type=args.tabtype,
                date_range=args.date,
                searchkey=args.searchkey,
                category=args.category,
                trade=args.trade,
                announcement_index=args.announcement_index,
                include_excluded=args.include_excluded,
                max_pages=args.max_pages,
                max_chars=args.max_chars,
                timeout=args.timeout,
            )
        ),
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
    fetch.add_argument("--date", help="CNInfo date range: YYYY-MM-DD~YYYY-MM-DD.")
    fetch.add_argument("--tabtype", choices=sorted(CNINFO_ALLOWED_TAB_TYPES), default="fulltext")
    fetch.add_argument("--searchkey", default="")
    fetch.add_argument("--category", default="")
    fetch.add_argument("--trade", default="")
    fetch.add_argument("--page-num", type=int, default=1)
    fetch.add_argument("--page-size", type=int, default=DEFAULT_CNINFO_PAGE_SIZE)
    fetch.add_argument("--include-excluded", action="store_true")
    fetch.add_argument("--announcement-index", type=int, default=1)
    fetch.add_argument("--start-date", help="Tushare start date: YYYYMMDD or YYYY-MM-DD.")
    fetch.add_argument("--end-date", help="Tushare end date: YYYYMMDD or YYYY-MM-DD.")
    fetch.add_argument("--trade-date", help="Tushare trade/survey date: YYYYMMDD or YYYY-MM-DD.")
    fetch.add_argument("--report-type", choices=sorted(CNINFO_REPORT_TYPES), default="all")
    fetch.add_argument("--report-year", type=int)
    fetch.add_argument("--report-index", type=int, default=1)
    fetch.add_argument("--include-report-variants", action="store_true")
    fetch.add_argument("--download-dir")
    fetch.add_argument("--max-pages", type=int, default=120)
    fetch.add_argument("--max-chars", type=int, default=60000)
    fetch.add_argument("--years", type=int, default=5, help="Years of history for valuation-band.")
    fetch.add_argument("--timeout", type=int, default=DEFAULT_CNINFO_TIMEOUT)

    pack = subparsers.add_parser("pack", help="Build full evidence, compact context, and module contexts for one stock.")
    pack.add_argument("query", help="Stock name, six-digit code, or ts_code.")
    pack.add_argument("--format", choices=["json"], default="json")
    pack.add_argument("--financial-limit", type=int, default=8)
    pack.add_argument("--market-limit", type=int, default=60)
    pack.add_argument("--band-years", type=int, default=5, help="Years of history for valuation-band.")
    pack.add_argument("--business-limit", type=int, default=30)
    pack.add_argument("--holder-periods", type=int, default=4)
    pack.add_argument("--holder-limit", type=int, default=30)
    pack.add_argument("--report-limit", type=int, default=8)
    pack.add_argument("--announcement-limit", type=int, default=30)
    pack.add_argument("--research-limit", type=int, default=30)
    pack.add_argument("--date", help="CNInfo date range: YYYY-MM-DD~YYYY-MM-DD.")
    pack.add_argument("--tabtype", choices=sorted(CNINFO_ALLOWED_TAB_TYPES), default="fulltext")
    pack.add_argument("--searchkey", default="")
    pack.add_argument("--category", default="")
    pack.add_argument("--trade", default="")
    pack.add_argument("--page-num", type=int, default=1)
    pack.add_argument("--page-size", type=int, default=DEFAULT_CNINFO_PAGE_SIZE)
    pack.add_argument("--include-excluded", action="store_true")
    pack.add_argument("--start-date", help="Tushare start date: YYYYMMDD or YYYY-MM-DD.")
    pack.add_argument("--end-date", help="Tushare end date: YYYYMMDD or YYYY-MM-DD.")
    pack.add_argument("--trade-date", help="Tushare trade/survey date: YYYYMMDD or YYYY-MM-DD.")
    pack.add_argument("--report-type", choices=sorted(CNINFO_REPORT_TYPES), default="all")
    pack.add_argument("--report-year", type=int)
    pack.add_argument("--include-report-variants", action="store_true")
    pack.add_argument("--timeout", type=int, default=DEFAULT_CNINFO_TIMEOUT)
    pack.add_argument("--evidence-out")
    pack.add_argument("--context-out")
    pack.add_argument("--module-context-dir")

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
        if args.dataset in {"announcement-raw", "announcement-text"} and args.announcement_index < 1:
            raise ValueError("announcement-index must be >= 1.")
        if args.dataset in {"announcements", "announcement-raw", "announcement-text"} and not str(args.query or "").strip():
            raise ValueError("CNInfo announcement queries require one stock.")
        ts_code = fetcher.resolve_ts_code(args.query)
        args.query = ts_code
        handler = _dataset_handlers()[args.dataset]
        payload = handler(fetcher, args)
        print_payload(payload, args.format)
        return 0

    if args.command == "pack":
        package = build_evidence_pack(fetcher, args)
        manifest = write_pack_outputs(package, args)
        print_payload(manifest or package, args.format)
        return 0

    return 1


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
