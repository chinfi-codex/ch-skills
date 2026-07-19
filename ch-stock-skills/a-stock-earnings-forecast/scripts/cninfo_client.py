#!/usr/bin/env python3
"""cninfo (巨潮资讯网) client for earnings-forecast discovery and parsing.

Self-contained (skills must be independently syncable/publishable). Resolves a
stock code to its cninfo orgId, enumerates the official 业绩预告 category with
pagination, and extracts announcement text. Uses `requests` directly — cninfo's public
disclosure endpoints are reachable without the claude.ai proxy that blocks
WebFetch/WebSearch here.
"""

from __future__ import annotations

import threading
import time
import re
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
    "0630": (("半年", "半度", "中期"), ("三季", "第三季")),
    "0930": (("三季", "第三季"), ("半年",)),
    "1231": (("年度", "年报"), ("半年", "季度", "一季", "三季")),
}

_CN_DIGITS = str.maketrans("0123456789", "〇一二三四五六七八九")
_QUICK_OR_NOISE = ("业绩快报", "监管工作函", "问询函", "回复")
_SUPPLEMENTAL_SEMANTICS = (
    "业绩预告", "业绩预增", "业绩预减", "业绩预盈", "业绩预亏",
    "业绩公告", "经营业绩情况", "经营情况公告",
)


def _post_json(url: str, data: Dict[str, Any], timeout: int = 20, attempts: int = 5) -> Optional[Dict[str, Any]]:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            resp = _session().post(url, headers=_HEADERS, data=data, timeout=timeout)
            if resp.status_code == 403:
                # CNInfo temporarily throttles bursty list traffic. Back off
                # materially instead of immediately amplifying the block.
                time.sleep(5.0 * (i + 1))
                resp.raise_for_status()
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


def normalize_title(title: str) -> str:
    """Remove cninfo search highlighting and whitespace."""
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", title or ""))


def _title_matches_period(title: str, period: str) -> bool:
    title = normalize_title(title)
    year = period[:4]
    year_tokens = (year, year.translate(_CN_DIGITS), year.translate(_CN_DIGITS).replace("〇", "零"))
    if not any(token in title for token in year_tokens):
        return False
    must, forbid = _PERIOD_TITLE.get(period[4:], ((), ()))
    if any(f in title for f in forbid):
        return False
    return any(m in title for m in must)


def is_forecast_announcement_title(title: str, period: str) -> bool:
    """Category-level guard: keep the report period, reject quick reports/noise.

    CNInfo's category is authoritative for announcement type but occasionally
    includes 业绩快报 and regulatory replies.  Period matching deliberately
    accepts title variants such as 上半年/半年报/半度 and Chinese-numeral years.
    """
    plain = normalize_title(title)
    if any(token in plain for token in _QUICK_OR_NOISE):
        return False
    return _title_matches_period(plain, period)


def to_ts_code(code: str) -> Optional[str]:
    """CNInfo six-digit security code -> Tushare-style A-share code.

    B shares (200/900) are outside this report's A-share scope.
    """
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code) or code.startswith(("200", "900")):
        return None
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return f"{code}.SH"
    if code.startswith(("4", "8", "920", "430")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _announcement_payload(page_num: int, se_date: str, stock: str = "",
                          category: str = FORECAST_CATEGORY, searchkey: str = "") -> Dict[str, str]:
    return {
        "pageNum": str(page_num), "pageSize": "30", "column": "szse", "tabName": "fulltext",
        "plate": "", "stock": stock, "searchkey": searchkey, "secid": "",
        "category": category, "trade": "", "seDate": se_date,
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }


def list_forecast_announcements(period: str, se_date: str, timeout: int = 20) -> List[Dict[str, Any]]:
    """Enumerate all official forecast-category announcements for a date range.

    Pagination stops only after the short final page (CNInfo's ``totalpages``
    behaves like a zero-based last page in some responses).  The returned rows
    are period-filtered, A-share-only, and retain stable announcement IDs/URLs.
    """
    out: List[Dict[str, Any]] = []
    seen_ids = set()
    # Category is the primary route. Supplemental title queries cover official
    # filings whose CNInfo metadata was not tagged into that category (observed
    # on a handful of otherwise valid 业绩预告/经营业绩公告 records).
    queries = [
        (FORECAST_CATEGORY, "", False),
        ("", "业绩预告", True),
        ("", "半年度经营", True),
        ("", "半年度业绩公告", True),
        ("", "上半年业绩", True),
        ("", "半年报业绩", True),
        ("", "半度业绩", True),
    ]
    for category, searchkey, strict_semantics in queries:
        page = 1
        while True:
            payload = _announcement_payload(page, se_date, category=category, searchkey=searchkey)
            j = _post_json(QUERY_URL, payload, timeout=timeout) or {}
            rows = j.get("announcements") or []
            if not rows:
                break
            for a in rows:
                title = normalize_title(str(a.get("announcementTitle", "")))
                ts_code = to_ts_code(str(a.get("secCode", "")))
                if not ts_code or not is_forecast_announcement_title(title, period):
                    continue
                if strict_semantics and not any(token in title for token in _SUPPLEMENTAL_SEMANTICS):
                    continue
                announcement_id = str(a.get("announcementId") or a.get("adjunctUrl") or "")
                if announcement_id in seen_ids:
                    continue
                seen_ids.add(announcement_id)
                adjunct = str(a.get("adjunctUrl") or "")
                out.append({
                    "announcement_id": announcement_id,
                    "ts_code": ts_code,
                    "code": ts_code[:6],
                    "name": str(a.get("secName") or ""),
                    "title": title,
                    "ann_date": _epoch_to_date(a.get("announcementTime")).replace("-", ""),
                    "url": adjunct if adjunct.startswith("http") else STATIC_PREFIX + adjunct,
                    "adjunct_url": adjunct,
                })
            if len(rows) < 30:
                break
            page += 1
            time.sleep(0.15)  # polite pagination; avoids CNInfo burst throttling
            if page > 500:
                raise RuntimeError(f"cninfo pagination runaway: {se_date} {searchkey}")
    return out


def find_forecast_announcement(code: str, orgid: str, period: str,
                               se_date: str, timeout: int = 20) -> Optional[Dict[str, Any]]:
    """Latest 业绩预告 announcement for `code` matching `period` within se_date."""
    data = _announcement_payload(1, se_date, f"{code},{orgid}")
    j = _post_json(QUERY_URL, data, timeout=timeout)
    anns = (j or {}).get("announcements") or []
    matched = [a for a in anns if is_forecast_announcement_title(str(a.get("announcementTitle", "")), period)]
    if not matched:
        return None
    matched.sort(key=lambda a: a.get("announcementTime", 0), reverse=True)
    a = matched[0]
    return {
        "announcement_id": str(a.get("announcementId") or a.get("adjunctUrl") or ""),
        "ts_code": to_ts_code(code),
        "code": code,
        "title": normalize_title(str(a.get("announcementTitle", ""))),
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
