#!/usr/bin/env python3
"""cninfo (巨潮资讯网) client for **official periodic reports**.

Self-contained (skills must be independently syncable/publishable). Two jobs:

1. **Provenance metadata, scanned in bulk.** Enumerating a report category for a
   date range is one cheap paginated call per 30 announcements, so every covered
   stock gets a title + original-PDF link without touching the PDFs themselves.
2. **The PDF itself, only when asked.** Structured Tushare data is the number
   authority for this skill; PDFs are for the things no API exposes (segment
   revenue, MD&A, the 非经常性损益 schedule). Annual/interim reports run to
   hundreds of pages, so `report_pdf.py` slices sections out of them rather than
   dumping full text.

Unlike earnings forecasts, official reports come in revisions: a company that
restates files 《…（更正公告）》 (the notice) and 《…（更正后）》 (the corrected
report). Only the latter is a report; the notice is filtered out and the
corrected version outranks the original.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests

# One requests.Session per thread → HTTP keep-alive/connection reuse, safe under
# the fetch thread pool.
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
ALLOWED_CNINFO_HOSTS = {"static.cninfo.com.cn", "www.cninfo.com.cn"}
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# Period end (mmdd) -> official report category on cninfo.
REPORT_CATEGORY = {
    "0331": "category_yjdbg_szsh",  # 一季报
    "0630": "category_bndbg_szsh",  # 半年报
    "0930": "category_sjdbg_szsh",  # 三季报
    "1231": "category_ndbg_szsh",   # 年报
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Origin": "http://www.cninfo.com.cn",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}

# Period end (mmdd) -> (title keywords that must appear, keywords that must not).
_PERIOD_TITLE = {
    "0331": (("第一季度报告", "一季度报告", "第一季报", "一季报"), ("半年", "第三季", "三季", "年度报告")),
    "0630": (("半年度报告", "半年报", "中期报告"), ("第三季", "三季", "第一季", "一季")),
    "0930": (("第三季度报告", "三季度报告", "第三季报", "三季报"), ("半年", "第一季", "一季")),
    "1231": (("年度报告",), ("半年", "季度", "一季", "三季")),
}

_CN_DIGITS = str.maketrans("0123456789", "〇一二三四五六七八九")

# 更正公告 is the notice *about* a restatement, not a report; 摘要/英文版/审计报告
# are companion filings. All are filtered from the report stream, but 摘要 is
# kept addressable because it is a far cheaper read than a 300-page annual.
_NOISE = ("更正公告", "英文", "English", "审计报告", "全文（", "问询", "回复", "取消")
_SUMMARY_TOKENS = ("摘要",)
_CORRECTED_TOKENS = ("更正后", "修订后", "更新后")


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
    rows = j if isinstance(j, list) else ((j or {}).get("keyBoardList") or [])
    for item in rows:
        if str(item.get("code")) == code and item.get("orgId"):
            return str(item["orgId"])
    for item in rows:
        if item.get("orgId"):
            return str(item["orgId"])
    return None


def normalize_title(title: str) -> str:
    """Remove cninfo search highlighting and whitespace."""
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", title or ""))


def is_summary_title(title: str) -> bool:
    return any(token in normalize_title(title) for token in _SUMMARY_TOKENS)


def is_corrected_title(title: str) -> bool:
    return any(token in normalize_title(title) for token in _CORRECTED_TOKENS)


def is_report_title(title: str, period: str, allow_summary: bool = False) -> bool:
    """True when the title is the periodic report itself for `period`.

    Year matching accepts Chinese-numeral years (some Beijing Stock Exchange
    filings use 二〇二五年). The category is already type-authoritative; this
    guard exists because a category page mixes report periods (a 2026-04 scan of
    the annual category returns both 2025 annuals and stray restated 2024 ones)
    and companion filings.
    """
    plain = normalize_title(title)
    if any(token in plain for token in _NOISE):
        return False
    if not allow_summary and is_summary_title(plain):
        return False
    year = period[:4]
    year_tokens = (year, year.translate(_CN_DIGITS), year.translate(_CN_DIGITS).replace("〇", "零"))
    if not any(token in plain for token in year_tokens):
        return False
    must, forbid = _PERIOD_TITLE.get(period[4:], ((), ()))
    if any(f in plain for f in forbid):
        return False
    return any(m in plain for m in must)


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
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return f"{code}.SZ"
    return None


def _payload(page_num: int, se_date: str, stock: str = "", category: str = "",
             searchkey: str = "") -> Dict[str, str]:
    return {
        "pageNum": str(page_num), "pageSize": "30", "column": "szse", "tabName": "fulltext",
        "plate": "", "stock": stock, "searchkey": searchkey, "secid": "",
        "category": category, "trade": "", "seDate": se_date,
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }


def _epoch_to_date(ms: Any) -> str:
    try:
        # CNInfo announcementTime is an epoch value while the report contract
        # uses the exchange disclosure date in Asia/Shanghai. Never let the host
        # timezone (commonly UTC on servers) shift the date backward.
        return datetime.fromtimestamp(int(ms) / 1000, BEIJING_TZ).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return ""


def _row_to_record(a: Dict[str, Any], period: str) -> Optional[Dict[str, Any]]:
    title = normalize_title(str(a.get("announcementTitle", "")))
    ts_code = to_ts_code(str(a.get("secCode", "")))
    if not ts_code:
        return None
    adjunct = str(a.get("adjunctUrl") or "")
    url = urljoin(STATIC_PREFIX, adjunct)
    if not validate_pdf_url(url):
        return None
    return {
        "announcement_id": str(a.get("announcementId") or adjunct or ""),
        "period": period,
        "ts_code": ts_code,
        "code": ts_code[:6],
        "name": str(a.get("secName") or ""),
        "title": title,
        "ann_date": _epoch_to_date(a.get("announcementTime")).replace("-", ""),
        "url": url,
        "adjunct_url": adjunct,
        "is_corrected": is_corrected_title(title),
        "is_summary": is_summary_title(title),
    }


def list_report_announcements(period: str, se_date: str, timeout: int = 20,
                              allow_summary: bool = False) -> List[Dict[str, Any]]:
    """Enumerate official periodic-report filings for a date range.

    Pagination stops on the first short page; cninfo's ``totalpages`` is
    unreliable across responses so page length is the safer terminator.
    """
    category = REPORT_CATEGORY.get(period[4:])
    if not category:
        raise ValueError(f"unsupported period end: {period}")
    out: List[Dict[str, Any]] = []
    seen_ids = set()
    page = 1
    while True:
        j = _post_json(QUERY_URL, _payload(page, se_date, category=category), timeout=timeout) or {}
        rows = j.get("announcements") or []
        if not rows:
            break
        for a in rows:
            if not is_report_title(str(a.get("announcementTitle", "")), period, allow_summary=allow_summary):
                continue
            rec = _row_to_record(a, period)
            if rec is None or rec["announcement_id"] in seen_ids:
                continue
            seen_ids.add(rec["announcement_id"])
            out.append(rec)
        if len(rows) < 30:
            break
        page += 1
        time.sleep(0.15)  # polite pagination; avoids CNInfo burst throttling
        if page > 500:
            raise RuntimeError(f"cninfo pagination runaway: {se_date} {category}")
    return out


def find_report_announcement(code: str, orgid: str, period: str, se_date: str,
                             timeout: int = 20, prefer_summary: bool = False,
                             max_pages: int = 8) -> Optional[Dict[str, Any]]:
    """Best periodic-report filing for one code within `se_date`.

    Paginates: a single company files hundreds of announcements a year, so a
    wide date window pushes the periodic report well past the first page of 30.
    Callers that know the filing date should still pass a tight `se_date` —
    that is one request instead of eight.

    Ranking: corrected版 > 原版, and full report > 摘要 unless the caller asked
    for the summary (a 20-page 摘要 is often enough for segment revenue and much
    cheaper to parse than a 300-page annual).
    """
    matched: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        j = _post_json(QUERY_URL, _payload(page, se_date, stock=f"{code},{orgid}"), timeout=timeout)
        anns = (j or {}).get("announcements") or []
        if not anns:
            break
        for a in anns:
            if not is_report_title(str(a.get("announcementTitle", "")), period, allow_summary=True):
                continue
            rec = _row_to_record(a, period)
            if rec:
                matched.append(rec)
        if matched or len(anns) < 30:
            break
        time.sleep(0.15)
    if not matched:
        return None
    matched.sort(key=lambda r: (
        1 if r["is_corrected"] else 0,
        1 if r["is_summary"] == prefer_summary else 0,
        r["ann_date"],
    ), reverse=True)
    return matched[0]


def validate_pdf_url(url: str) -> bool:
    """Allow only the official CNInfo HTTP(S) hosts."""
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in ALLOWED_CNINFO_HOSTS


def download_pdf(url: str, timeout: int = 90, attempts: int = 3) -> Optional[bytes]:
    """Fetch an announcement PDF. Annual reports reach tens of MB — long timeout."""
    if not validate_pdf_url(url):
        raise ValueError(f"refusing non-CNInfo PDF URL: {url!r}")
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


def pdf_pages(raw: bytes) -> List[str]:
    """Extract text page by page. Page granularity is what makes section slicing
    possible; a single concatenated string would force the whole annual report
    into context."""
    try:
        import fitz  # PyMuPDF
        with fitz.open(stream=raw, filetype="pdf") as doc:
            return [page.get_text() for page in doc]
    except Exception:  # noqa: BLE001
        pass
    try:
        import io
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return [(page.extract_text() or "") for page in reader.pages]
    except Exception:  # noqa: BLE001
        return []


def pdf_to_text(raw: bytes) -> str:
    return "\n".join(pdf_pages(raw))
