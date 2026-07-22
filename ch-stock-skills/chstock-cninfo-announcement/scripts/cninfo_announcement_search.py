#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""查询巨潮资讯公告并按规则过滤。"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from cninfo_rules import classify_cninfo_fulltext


logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 30
DEFAULT_TIMEOUT = 15
ALLOWED_TAB_TYPES = {"fulltext", "relation"}
DATE_RANGE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}~\d{4}-\d{2}-\d{2}$")
STOCK_CODE_RE = re.compile(r"^\d{6}$")
STOCK_WITH_ORG_RE = re.compile(r"^\d{6},[\w-]+$")
CNINFO_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")


def _default_date_range() -> str:
    today = datetime.now(CNINFO_TIMEZONE).date()
    yesterday = today - timedelta(days=1)
    return f"{yesterday:%Y-%m-%d}~{today:%Y-%m-%d}"


def _extend_archive_end(se_date: str, days: int = 1) -> str:
    """把日期范围的结束日向后顺延。

    巨潮 seDate 按公告**归档日**过滤，晚间披露的公告归档在次日：
    查询截止到 D 会漏掉 D 日晚间发布（归档 D+1）的公告。默认把结束日
    +1 天以覆盖晚间披露；代价是可能带入 D+1 白天归档的公告，结果中的
    announcement_time 会如实显示归档日，由调用方甄别。
    """
    start_str, end_str = se_date.split("~")
    end = datetime.strptime(end_str, "%Y-%m-%d").date() + timedelta(days=days)
    return f"{start_str}~{end:%Y-%m-%d}"


def _announcement_date(timestamp_ms: Any) -> str:
    """把巨潮 epoch 毫秒时间戳固定解释为北京时间日期。"""
    if timestamp_ms in (None, ""):
        return ""
    return datetime.fromtimestamp(float(timestamp_ms) / 1000, tz=CNINFO_TIMEZONE).strftime("%Y-%m-%d")


def _validate_date_range(value: str) -> str:
    if not DATE_RANGE_RE.match(value):
        raise argparse.ArgumentTypeError("日期范围必须是 YYYY-MM-DD~YYYY-MM-DD")
    return value


def _validate_stock(value: str) -> str:
    if not value:
        return value
    if STOCK_CODE_RE.match(value) or STOCK_WITH_ORG_RE.match(value):
        return value
    raise argparse.ArgumentTypeError("stock 必须是 6 位股票代码，或 code,orgId 格式")


def _headers() -> dict[str, str]:
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


def get_cninfo_orgid(stock_code: str, timeout: int = DEFAULT_TIMEOUT) -> str | None:
    """根据股票代码获取巨潮资讯 orgId。"""
    url = "http://www.cninfo.com.cn/new/information/topSearch/query"
    data = {"keyWord": stock_code, "maxNum": 10}

    try:
        response = requests.post(url, data=data, headers=_headers(), timeout=timeout)
        response.raise_for_status()
        results = response.json()
    except requests.RequestException as exc:
        logger.error("获取 orgId 失败: %s", exc)
        return None

    if not isinstance(results, list) or not results:
        return None

    for item in results:
        if item.get("code") == stock_code and item.get("orgId"):
            return str(item["orgId"])

    first_org_id = results[0].get("orgId")
    return str(first_org_id) if first_org_id else None


def resolve_stock_argument(stock: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """将纯股票代码扩展成 code,orgId。"""
    if not stock or "," in stock:
        return stock

    org_id = get_cninfo_orgid(stock, timeout=timeout)
    if not org_id:
        return stock
    return f"{stock},{org_id}"


def query_cninfo_announcements(
    *,
    page_num: int,
    tab_type: str,
    stock: str = "",
    searchkey: str = "",
    category: str = "",
    trade: str = "",
    se_date: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """查询巨潮资讯公告。"""
    if tab_type not in ALLOWED_TAB_TYPES:
        raise ValueError(f"tab_type 仅支持: {sorted(ALLOWED_TAB_TYPES)}")

    url = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    payload = {
        "pageNum": int(page_num),
        "pageSize": int(page_size),
        "column": "szse",
        "tabName": tab_type,
        "plate": "",
        "stock": stock,
        "searchkey": searchkey,
        "secid": "",
        "category": category,
        "trade": trade,
        "seDate": se_date or _default_date_range(),
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }

    response = requests.post(url, data=payload, headers=_headers(), timeout=timeout)
    response.raise_for_status()
    results = response.json()
    announcements = results.get("announcements") or []

    normalized_items: list[dict[str, Any]] = []
    for item in announcements:
        publish_date = _announcement_date(item.get("announcementTime"))

        adjunct_url = item.get("adjunctUrl") or ""
        if adjunct_url and not adjunct_url.startswith("http"):
            adjunct_url = f"http://static.cninfo.com.cn/{adjunct_url}"

        normalized_items.append(
            {
                "announcement_time": publish_date,
                "sec_name": item.get("secName", ""),
                "sec_code": item.get("secCode", ""),
                "title": item.get("announcementTitle", ""),
                "adjunct_url": adjunct_url,
                "announcement_id": item.get("announcementId", ""),
            }
        )

    return {
        "total_pages": results.get("totalpages"),
        "total_records": results.get("totalRecordNum"),
        "announcements": normalized_items,
    }


def apply_rules(tab_type: str, announcements: list[dict[str, Any]], include_excluded: bool) -> list[dict[str, Any]]:
    """按规则补充分类信息，并可过滤排除项。"""
    filtered_items: list[dict[str, Any]] = []

    for item in announcements:
        if tab_type == "fulltext":
            rule_result = classify_cninfo_fulltext(item.get("title"))
        else:
            rule_result = {
                "category": "互动易/调研",
                "subcategory": "未定义",
                "rule_id": "cninfo.relation.unclassified.v1",
                "excluded": False,
                "exclude_reason": "",
                "tags": ["relation"],
            }

        enriched_item = {**item, **rule_result}
        if enriched_item["excluded"] and not include_excluded:
            continue
        filtered_items.append(enriched_item)

    return filtered_items


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数。"""
    parser = argparse.ArgumentParser(description="查询巨潮资讯公告，并按 rules 过滤结果")
    parser.add_argument("--tabtype", choices=sorted(ALLOWED_TAB_TYPES), default="fulltext", help="公告类型")
    parser.add_argument("--date", dest="se_date", type=_validate_date_range, default=_default_date_range(), help="日期范围，格式 YYYY-MM-DD~YYYY-MM-DD")
    parser.add_argument("--stock", type=_validate_stock, default="", help="6 位股票代码，或 code,orgId")
    parser.add_argument("--searchkey", default="", help="标题关键词")
    parser.add_argument("--category", default="", help="巨潮 category 参数")
    parser.add_argument("--trade", default="", help="行业参数")
    parser.add_argument("--page-num", type=int, default=1, help="页码")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="每页条数")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="请求超时秒数")
    parser.add_argument("--include-excluded", action="store_true", help="保留被 rules 排除的公告")
    parser.add_argument("--no-archive-extend", action="store_true", help="不自动把结束日 +1 天（默认顺延以覆盖晚间披露、次日归档的公告）")
    parser.add_argument("--disable-orgid-resolve", action="store_true", help="stock 为纯代码时不自动补全 orgId")
    parser.add_argument("--output", help="输出 JSON 文件路径")
    return parser


def main() -> None:
    """CLI 入口。"""
    parser = build_parser()
    args = parser.parse_args()

    stock = args.stock
    if stock and not args.disable_orgid_resolve:
        stock = resolve_stock_argument(stock, timeout=args.timeout)

    se_date_effective = args.se_date if args.no_archive_extend else _extend_archive_end(args.se_date)

    result = query_cninfo_announcements(
        page_num=args.page_num,
        tab_type=args.tabtype,
        stock=stock,
        searchkey=args.searchkey.strip(),
        category=args.category.strip(),
        trade=args.trade.strip(),
        se_date=se_date_effective,
        page_size=args.page_size,
        timeout=args.timeout,
    )
    result["query"] = {
        "tabtype": args.tabtype,
        "se_date": args.se_date,
        "se_date_effective": se_date_effective,
        "stock": stock,
        "searchkey": args.searchkey.strip(),
        "category": args.category.strip(),
        "trade": args.trade.strip(),
        "page_num": args.page_num,
        "page_size": args.page_size,
        "include_excluded": args.include_excluded,
    }
    result["announcements"] = apply_rules(args.tabtype, result["announcements"], args.include_excluded)
    result["filtered_count"] = len(result["announcements"])

    output_text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text, encoding="utf-8")
    else:
        print(output_text)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    main()
