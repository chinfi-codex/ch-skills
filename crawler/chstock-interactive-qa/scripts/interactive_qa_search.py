#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import html
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

P5W_URL = "https://ir.p5w.net/interaction/getNewSearchR.shtml"
P5W_COMPANY_REPLY_URL = "https://ir.p5w.net/interaction/getNewR.shtml"
P5W_COMPANY_SEARCH_URL = "https://ir.p5w.net/company/validCompanyJson.shtml"
P5W_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
TAG_RE = re.compile(r"<[^>]+>")
SHAREHOLDER_COUNT_DIRECT_RE = re.compile(
    r"(股东人数|股东户数|股东总数|股东总户数|股东总人数|最新股东户数|最新股东人数|持股人数|持股户数|股东数量)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检索上市公司互动问答")
    parser.add_argument("--keyword", default="", help="问答关键词")
    parser.add_argument("--company", default="", help="公司简称或 6 位股票代码")
    parser.add_argument("--date-from", default="", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--date-to", default="", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=20, help="返回条数上限，默认 20")
    parser.add_argument("--max-pages", type=int, default=30, help="最多抓取页数，默认 30")
    parser.add_argument(
        "--include-shareholder-count",
        action="store_true",
        help="保留股东人数/股东户数类问题",
    )
    parser.add_argument("--output", default="", help="输出 JSON 文件路径")
    return parser.parse_args()


def strip_html(text: Optional[str]) -> str:
    if text is None:
        return ""
    return TAG_RE.sub("", html.unescape(str(text))).strip()


def parse_date(value: str) -> Optional[datetime.date]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def fetch_page(
    session: requests.Session,
    page: int,
    rows: int = 10,
    keyword: str = "",
) -> Dict[str, Any]:
    payload = {
        "isPagination": "1",
        "keyWords": keyword,
        "companyCode": "",
        "companyBaseinfoId": "",
        "page": str(max(0, int(page))),
        "rows": str(max(1, min(int(rows), 10))),
    }
    response = session.post(P5W_URL, data=payload, headers=P5W_HEADERS, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"接口返回失败: {data}")
    return data


def fetch_company_reply_page(
    session: requests.Session,
    company_code: str,
    company_baseinfo_id: str,
    page: int,
    rows: int = 10,
    keyword: str = "",
    date_from: str = "",
    date_to: str = "",
) -> Dict[str, Any]:
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
    response = session.post(P5W_COMPANY_REPLY_URL, data=payload, headers=P5W_HEADERS, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"公司问答接口返回失败: {data}")
    return data


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    event_time = (row.get("replyerTimeStr") or row.get("questionerTimeStr") or "").strip()
    event_date = event_time[:10] if len(event_time) >= 10 else ""
    question = strip_html(row.get("content"))
    reply = strip_html(row.get("replyContent"))
    return {
        "pid": str(row.get("pid") or "").strip(),
        "company_code": str(row.get("companyCode") or "").strip(),
        "company_name": str(row.get("companyShortname") or "").strip(),
        "question": question,
        "reply": reply,
        "event_time": event_time,
        "event_date": event_date,
        "url": "https://ir.p5w.net/interaction/",
        "raw": row,
    }


def is_stock_code(value: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", value.strip()))


def resolve_company(session: requests.Session, company: str) -> Dict[str, str]:
    needle = company.strip()
    if not needle:
        raise ValueError("公司名不能为空")

    if is_stock_code(needle):
        html_response = session.get(
            f"https://ir.p5w.net/c/{needle}/questionlist.shtml",
            headers={"User-Agent": P5W_HEADERS["User-Agent"]},
            timeout=20,
        )
        html_response.raise_for_status()
        text = html_response.text
        match = re.search(r'id="companyBaseinfoId"\s+value="([^"]+)"', text)
        title_match = re.search(r"<title>\s*([^（<]+)（", text)
        if not match:
            raise RuntimeError(f"未找到股票代码 {needle} 对应的 companyBaseinfoId")
        return {
            "company_code": needle,
            "company_name": title_match.group(1).strip() if title_match else needle,
            "company_baseinfo_id": match.group(1).strip(),
        }

    response = session.post(
        P5W_COMPANY_SEARCH_URL,
        data={"keyword": needle},
        headers={
            "User-Agent": P5W_HEADERS["User-Agent"],
            "Referer": "https://ir.p5w.net/",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": P5W_HEADERS["Content-Type"],
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
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


def collect_results(
    keyword: str,
    company: str,
    date_from: str,
    date_to: str,
    limit: int,
    max_pages: int,
    include_shareholder_count: bool,
) -> Dict[str, Any]:
    rows_per_page = 10
    items: List[Dict[str, Any]] = []
    seen_pid = set()

    with requests.Session() as session:
        resolved_company: Optional[Dict[str, str]] = None
        if company:
            resolved_company = resolve_company(session, company)

        for page in range(max(1, max_pages)):
            if resolved_company:
                data = fetch_company_reply_page(
                    session=session,
                    company_code=resolved_company["company_code"],
                    company_baseinfo_id=resolved_company["company_baseinfo_id"],
                    page=page,
                    rows=rows_per_page,
                    keyword=keyword,
                    date_from=date_from,
                    date_to=date_to,
                )
            else:
                data = fetch_page(session=session, page=page, rows=rows_per_page, keyword=keyword)
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

                excluded_reason = ""
                if not include_shareholder_count and is_shareholder_count_question(item["question"], item["reply"]):
                    excluded_reason = "股东人数/股东户数类问题默认过滤"

                item["excluded_reason"] = excluded_reason
                if excluded_reason:
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
            "resolved_company_code": resolved_company["company_code"] if company and resolved_company else "",
            "resolved_company_name": resolved_company["company_name"] if company and resolved_company else "",
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
            "max_pages": max_pages,
            "include_shareholder_count": include_shareholder_count,
        },
        "total": len(result_items),
        "items": result_items,
    }


def write_output(result: Dict[str, Any], output_path: str) -> None:
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text)


def main() -> int:
    args = parse_args()

    if not args.keyword and not args.company:
        raise SystemExit("至少提供 --keyword 或 --company 其中之一")

    try:
        result = collect_results(
            keyword=args.keyword.strip(),
            company=args.company.strip(),
            date_from=args.date_from.strip(),
            date_to=args.date_to.strip(),
            limit=max(1, args.limit),
            max_pages=max(1, args.max_pages),
            include_shareholder_count=args.include_shareholder_count,
        )
    except Exception as exc:
        error_result = {
            "query": {
                "keyword": args.keyword,
                "company": args.company,
                "date_from": args.date_from,
                "date_to": args.date_to,
            },
            "total": 0,
            "items": [],
            "error": str(exc),
        }
        write_output(error_result, args.output)
        return 1

    write_output(result, args.output)
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
