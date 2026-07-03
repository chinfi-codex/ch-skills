#!/usr/bin/env python3
"""Minimal cninfo (巨潮资讯网) client for earnings-forecast enrichment.

Self-contained (skills must be independently syncable/publishable). Resolves a
stock code to its cninfo orgId, lists 业绩预告 announcements for a period, and
extracts the announcement's text. Uses `requests` directly — cninfo's public
disclosure endpoints are reachable without the claude.ai proxy that blocks
WebFetch/WebSearch here.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import requests

# One requests.Session per thread → HTTP keep-alive/connection reuse across the
# ~3 calls each code makes, safe under the enrichment thread pool.
_thread_local = threading.local()


def _session() -> requests.Session:
    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        _thread_local.session = sess
    return sess

TOPSEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_PREFIX = "http://static.cninfo.com.cn/"
FORECAST_CATEGORY = "category_yjygjxz_szsh"  # 业绩预告

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Origin": "http://www.cninfo.com.cn",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}

# Period end (mmdd) -> title keywords that must / must-not appear.
_PERIOD_TITLE = {
    "0331": (("一季", "第一季"), ("半年", "三季", "年度业绩", "第三季")),
    "0630": (("半年", "中期"), ("三季", "第三季")),
    "0930": (("三季", "第三季"), ("半年",)),
    "1231": (("年度", "年报"), ("半年", "季度", "一季", "三季")),
}


def _post_json(url: str, data: Dict[str, Any], timeout: int = 20, attempts: int = 3) -> Optional[Dict[str, Any]]:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            resp = _session().post(url, headers=_HEADERS, data=data, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.8 * (2 ** i))
    if last:
        raise last
    return None


def resolve_orgid(code: str, timeout: int = 20) -> Optional[str]:
    """Six-digit code -> cninfo orgId."""
    j = _post_json(TOPSEARCH_URL, {"keyWord": code, "maxNum": "10"}, timeout=timeout)
    for item in (j or []) if isinstance(j, list) else (j.get("keyBoardList") or []):
        if str(item.get("code")) == code and item.get("orgId"):
            return str(item["orgId"])
    # Some responses wrap results differently; fall back to first match.
    rows = j if isinstance(j, list) else (j.get("keyBoardList") or [])
    for item in rows:
        if item.get("orgId"):
            return str(item["orgId"])
    return None


def _title_matches_period(title: str, period: str) -> bool:
    year = period[:4]
    if year not in title:
        return False
    must, forbid = _PERIOD_TITLE.get(period[4:], ((), ()))
    if any(f in title for f in forbid):
        return False
    return any(m in title for m in must)


def find_forecast_announcement(code: str, orgid: str, period: str,
                               se_date: str, timeout: int = 20) -> Optional[Dict[str, Any]]:
    """Latest 业绩预告 announcement for `code` matching `period` within se_date."""
    data = {
        "pageNum": "1", "pageSize": "30", "column": "szse", "tabName": "fulltext",
        "plate": "", "stock": f"{code},{orgid}", "searchkey": "", "secid": "",
        "category": FORECAST_CATEGORY, "trade": "", "seDate": se_date,
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    j = _post_json(QUERY_URL, data, timeout=timeout)
    anns = (j or {}).get("announcements") or []
    matched = [a for a in anns if _title_matches_period(str(a.get("announcementTitle", "")), period)]
    if not matched:
        # Fall back to any forecast in range if title parsing is too strict.
        matched = anns
    if not matched:
        return None
    matched.sort(key=lambda a: a.get("announcementTime", 0), reverse=True)
    a = matched[0]
    return {
        "title": str(a.get("announcementTitle", "")),
        "ann_date": _epoch_to_date(a.get("announcementTime")),
        "url": STATIC_PREFIX + str(a.get("adjunctUrl", "")),
        "adjunct_url": str(a.get("adjunctUrl", "")),
    }


def _epoch_to_date(ms: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ms) / 1000))
    except Exception:  # noqa: BLE001
        return ""


def download_pdf(url: str, timeout: int = 40, attempts: int = 3) -> Optional[bytes]:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            resp = _session().get(url, headers={"User-Agent": _HEADERS["User-Agent"]}, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.8 * (2 ** i))
    if last:
        raise last
    return None


def pdf_to_text(raw: bytes) -> str:
    """Extract text; PyMuPDF preferred, PyPDF2 fallback. Forecasts are text PDFs."""
    try:
        import fitz  # PyMuPDF
        with fitz.open(stream=raw, filetype="pdf") as doc:
            return "\n".join(page.get_text() for page in doc)
    except Exception:  # noqa: BLE001
        pass
    try:
        import io
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:  # noqa: BLE001
        return ""
