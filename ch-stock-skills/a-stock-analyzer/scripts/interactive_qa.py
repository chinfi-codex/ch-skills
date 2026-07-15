#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""互动易(P5W)投资者问答抓取模块。

只做原子抓取:按公司(6 位代码/简称)或关键词检索上市公司互动问答，清洗
HTML、按日期/关键词/公司过滤、默认剔除股东户数类低信息问题，返回结构化
items。回复可信度、事实边界、是否印证看点由模型判断，本模块不下结论。

移植自独立 skill ``chstock-interactive-qa`` 的核心逻辑；差异是去掉 argparse/
main CLI 壳，HTTP 调用复用本 skill 的 ``request_with_retry`` 与外部传入的
``requests.Session``（与 ``cninfo.py`` 同为 fetcher_core 之下的抓取模块）。
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from retry import request_with_retry

P5W_SEARCH_URL = "https://ir.p5w.net/interaction/getNewSearchR.shtml"
P5W_COMPANY_REPLY_URL = "https://ir.p5w.net/interaction/getNewR.shtml"
P5W_COMPANY_SEARCH_URL = "https://ir.p5w.net/company/validCompanyJson.shtml"
P5W_QUESTIONLIST_URL = "https://ir.p5w.net/c/{code}/questionlist.shtml"
P5W_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
DEFAULT_QA_TIMEOUT = 20

TAG_RE = re.compile(r"<[^>]+>")
SHAREHOLDER_COUNT_DIRECT_RE = re.compile(
    r"(股东人数|股东户数|股东总数|股东总户数|股东总人数|最新股东户数|最新股东人数|持股人数|持股户数|股东数量)"
)


def strip_html(text: Optional[str]) -> str:
    if text is None:
        return ""
    return TAG_RE.sub("", html.unescape(str(text))).strip()


def parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def is_stock_code(value: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", value.strip()))


def fetch_search_page(
    session: Any,
    page: int,
    rows: int = 10,
    keyword: str = "",
    timeout: int = DEFAULT_QA_TIMEOUT,
) -> Dict[str, Any]:
    """全站关键词检索一页（无公司锚定时使用）。"""
    payload = {
        "isPagination": "1",
        "keyWords": keyword,
        "companyCode": "",
        "companyBaseinfoId": "",
        "page": str(max(0, int(page))),
        "rows": str(max(1, min(int(rows), 10))),
    }
    resp = request_with_retry(session.post, P5W_SEARCH_URL, data=payload, headers=P5W_HEADERS, timeout=timeout)
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"互动易检索接口返回失败: {data}")
    return data


def fetch_company_reply_page(
    session: Any,
    company_code: str,
    company_baseinfo_id: str,
    page: int,
    rows: int = 10,
    keyword: str = "",
    date_from: str = "",
    date_to: str = "",
    timeout: int = DEFAULT_QA_TIMEOUT,
) -> Dict[str, Any]:
    """锚定某公司拉互动问答一页。"""
    payload = {
        "isPagination": "1",
        "companyCode": company_code,
        "companyBaseinfoId": company_baseinfo_id,
        "keyWords": keyword,
        "questionerTimeBegin": date_from,
        "questionerTimeEnd": date_to,
        "page": str(max(0, int(page))),
        "rows": str(max(1, min(int(rows), 20))),
    }
    resp = request_with_retry(session.post, P5W_COMPANY_REPLY_URL, data=payload, headers=P5W_HEADERS, timeout=timeout)
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"互动易公司问答接口返回失败: {data}")
    return data


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    event_time = (row.get("replyerTimeStr") or row.get("questionerTimeStr") or "").strip()
    event_date = event_time[:10] if len(event_time) >= 10 else ""
    return {
        "pid": str(row.get("pid") or "").strip(),
        "company_code": str(row.get("companyCode") or "").strip(),
        "company_name": str(row.get("companyShortname") or "").strip(),
        "question": strip_html(row.get("content")),
        "reply": strip_html(row.get("replyContent")),
        "event_time": event_time,
        "event_date": event_date,
        "url": "https://ir.p5w.net/interaction/",
        "raw": row,
    }


def resolve_company(session: Any, company: str, timeout: int = DEFAULT_QA_TIMEOUT) -> Dict[str, str]:
    """把 6 位代码或公司简称解析成 {company_code, company_name, company_baseinfo_id}。"""
    needle = company.strip()
    if not needle:
        raise ValueError("公司名不能为空")

    if is_stock_code(needle):
        resp = request_with_retry(
            session.get,
            P5W_QUESTIONLIST_URL.format(code=needle),
            headers={"User-Agent": P5W_HEADERS["User-Agent"]},
            timeout=timeout,
        )
        text = resp.text
        match = re.search(r'id="companyBaseinfoId"\s+value="([^"]+)"', text)
        title_match = re.search(r"<title>\s*([^（<]+)（", text)
        if not match:
            raise RuntimeError(f"未找到股票代码 {needle} 对应的 companyBaseinfoId")
        return {
            "company_code": needle,
            "company_name": title_match.group(1).strip() if title_match else needle,
            "company_baseinfo_id": match.group(1).strip(),
        }

    resp = request_with_retry(
        session.post,
        P5W_COMPANY_SEARCH_URL,
        data={"keyword": needle},
        headers={
            "User-Agent": P5W_HEADERS["User-Agent"],
            "Referer": "https://ir.p5w.net/",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": P5W_HEADERS["Content-Type"],
        },
        timeout=timeout,
    )
    data = resp.json()
    candidates = data.get("obj") or []
    if not candidates:
        raise RuntimeError(f"未找到公司“{needle}”的匹配结果")

    lowered = needle.lower()
    selected = None
    for candidate in candidates:
        shortname = str(candidate.get("companyShortname") or "")
        if shortname.lower() == lowered:
            selected = candidate
            break
    if selected is None:
        for candidate in candidates:
            shortname = str(candidate.get("companyShortname") or "")
            if lowered in shortname.lower():
                selected = candidate
                break
    if selected is None:
        selected = candidates[0]

    return {
        "company_code": str(selected.get("companyCode") or "").strip(),
        "company_name": str(selected.get("companyShortname") or "").strip(),
        "company_baseinfo_id": str(selected.get("pid") or "").strip(),
    }


def matches_company(item: Dict[str, Any], company: str) -> bool:
    if not company:
        return True
    needle = company.strip().lower()
    if is_stock_code(company):
        return item["company_code"] == company.strip()
    company_name = item["company_name"].lower()
    return needle == company_name or needle in company_name


def keyword_matches(item: Dict[str, Any], keyword: str) -> bool:
    if not keyword:
        return True
    needle = keyword.strip().lower().replace(" ", "")
    haystack = "\n".join([item["question"], item["reply"]]).lower().replace(" ", "")
    return needle in haystack


def is_shareholder_count_question(question: str, reply: str) -> bool:
    """识别股东户数/人数类低信息问题，默认从跟踪证据里剔除。"""
    question_text = (question or "").replace(" ", "")
    reply_text = (reply or "").replace(" ", "")

    if SHAREHOLDER_COUNT_DIRECT_RE.search(question_text) or SHAREHOLDER_COUNT_DIRECT_RE.search(reply_text):
        return True

    has_subject = "股东" in question_text or "持股" in question_text
    has_count_term = any(term in question_text for term in ["人数", "户数", "总数", "总户数", "总人数", "数量"])
    has_query_term = any(term in question_text for term in ["多少", "几", "截至", "截止", "最新", "情况"])
    return has_subject and has_count_term and has_query_term


def within_date_range(item: Dict[str, Any], date_from: str, date_to: str) -> bool:
    if not item["event_date"]:
        return False

    item_date = parse_date(item["event_date"])
    start_date = parse_date(date_from) if date_from else None
    end_date = parse_date(date_to) if date_to else None

    if start_date and item_date and item_date < start_date:
        return False
    if end_date and item_date and item_date > end_date:
        return False
    return True


def fetch_interactive_qa(
    *,
    session: Any,
    company: str = "",
    keyword: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 20,
    max_pages: int = 30,
    include_shareholder_count: bool = False,
    timeout: int = DEFAULT_QA_TIMEOUT,
) -> Dict[str, Any]:
    """检索上市公司互动问答，返回 ``{query, total, items}``。

    传 ``company``（6 位代码或简称）时锚定该公司拉问答；只传 ``keyword`` 时走
    全站关键词检索。``date_from/date_to`` 为 ``YYYY-MM-DD``；``limit`` 命中足量即
    提前停止翻页。HTTP 会话由调用方注入，便于复用连接与统一重试。
    """
    if not company and not keyword:
        raise ValueError("至少提供 company 或 keyword 其中之一")

    rows_per_page = 10
    items: List[Dict[str, Any]] = []
    seen_pid: set = set()

    resolved_company: Optional[Dict[str, str]] = None
    if company:
        resolved_company = resolve_company(session, company, timeout=timeout)

    for page in range(max(1, max_pages)):
        if resolved_company:
            data = fetch_company_reply_page(
                session,
                company_code=resolved_company["company_code"],
                company_baseinfo_id=resolved_company["company_baseinfo_id"],
                page=page,
                rows=rows_per_page,
                keyword=keyword,
                date_from=date_from,
                date_to=date_to,
                timeout=timeout,
            )
        else:
            data = fetch_search_page(session, page, rows=rows_per_page, keyword=keyword, timeout=timeout)
        page_rows = data.get("rows") or []
        if not page_rows:
            break

        for row in page_rows:
            item = normalize_row(row)
            if not item["pid"] or item["pid"] in seen_pid:
                continue
            seen_pid.add(item["pid"])

            if resolved_company:
                if item["company_code"] != resolved_company["company_code"]:
                    continue
            elif not matches_company(item, company):
                continue
            if not keyword_matches(item, keyword):
                continue
            if not within_date_range(item, date_from, date_to):
                continue
            if not include_shareholder_count and is_shareholder_count_question(item["question"], item["reply"]):
                continue

            items.append(item)

        if len(items) >= limit:
            break

    items.sort(key=lambda x: (x["event_time"], x["pid"]), reverse=True)
    result_items = items[: max(1, limit)]

    return {
        "query": {
            "keyword": keyword,
            "company": company,
            "resolved_company_code": resolved_company["company_code"] if resolved_company else "",
            "resolved_company_name": resolved_company["company_name"] if resolved_company else "",
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
            "max_pages": max_pages,
            "include_shareholder_count": include_shareholder_count,
        },
        "total": len(result_items),
        "items": result_items,
    }
