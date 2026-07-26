#!/usr/bin/env python3
"""On-demand section extraction from an official report PDF.

Why sections and not full text: Tushare already answers every *number* question,
so a PDF is only worth opening for what the API has no field for — segment
revenue, the management discussion, the 非经常性损益 schedule, customer
concentration. Those live in a handful of pages of a filing that can run past
300, and dumping the whole thing into context would cost more than the answer is
worth. This script locates the pages a section spans and returns only those.

Usage
-----
    # preset sections
    python3 scripts/report_pdf.py --period 20260331 --code 300750.SZ \
        --sections segment,nonrecurring

    # free-text lookup when the preset list does not cover the question
    python3 scripts/report_pdf.py --period 20251231 --code 002594.SZ \
        --find 海外收入,单车盈利 --context-chars 1500

    python3 scripts/report_pdf.py --list-sections

Quarterly reports (Q1/Q3) are short and carry no MD&A or segment table by
regulation — expect `segment`/`mdna` to come back empty for them and say so
rather than inventing a breakdown. Interim and annual reports have both.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import cninfo_client as cn  # noqa: E402
from store import Store  # noqa: E402

# Each section: start anchors (any match opens it), stop anchors (any match on a
# *later* page closes it), and a page budget so a bad anchor cannot run away.
SECTIONS: Dict[str, Dict[str, Any]] = {
    "segment": {
        "label": "主营业务构成（分行业/分产品/分地区）",
        "start": [r"主营业务分行业", r"营业收入构成", r"分行业、分产品、分地区",
                  r"分产品情况", r"主营业务分产品"],
        "stop": [r"主要销售客户", r"成本分析情况", r"研发投入", r"现金流量表相关"],
        "max_pages": 6,
        "why": "API 没有分产品/分地区收入，判断增长来自哪块业务只能读这里",
    },
    "mdna": {
        "label": "管理层讨论与分析 / 经营情况讨论",
        "start": [r"管理层讨论与分析", r"经营情况讨论与分析", r"董事会报告"],
        "stop": [r"公司治理", r"重要事项", r"环境和社会责任"],
        "max_pages": 14,
        "why": "公司自己解释这一期变化的唯一一手文本，判断可持续性用",
    },
    "nonrecurring": {
        "label": "非经常性损益项目及金额",
        # Issuers split between 「项目和金额」 and 「项目及金额」; both are the same
        # statutory heading, so anchor on the shared stem.
        "start": [r"非经常性损益项目[和及]金额", r"非经常性损益的项目", r"非经常性损益项目\s*\n"],
        "stop": [r"净资产收益率", r"采用公允价值计量", r"主要会计数据"],
        "max_pages": 3,
        "why": "扣非与归母的差额由哪些科目构成——处置？补助？公允价值？",
    },
    "customers": {
        "label": "前五大客户与供应商集中度",
        "start": [r"前五名客户", r"主要销售客户", r"前五大客户", r"主要供应商"],
        "stop": [r"费用", r"研发投入", r"现金流"],
        "max_pages": 3,
        "why": "收入集中度与大客户依赖，决定增长的脆弱性",
    },
    "rd": {
        "label": "研发投入与在研项目",
        "start": [r"研发投入情况", r"研发投入表", r"主要研发项目"],
        "stop": [r"现金流", r"非主营业务", r"资产及负债状况"],
        "max_pages": 5,
        "why": "研发费用化/资本化拆分与在研管线，成长股的投入前置证据",
    },
    "balance_change": {
        "label": "资产负债项目重大变动说明",
        "start": [r"资产及负债状况", r"财务报表项目.{0,6}变动", r"资产负债表项目.{0,6}变动",
                  r"报表项目大幅变动"],
        "stop": [r"投资状况分析", r"重大资产和股权出售", r"主要控股参股公司"],
        "max_pages": 6,
        "why": "合同负债/存货/应收异动的公司自述原因，机械 gap 之外的解释",
    },
    "outlook": {
        "label": "未来展望与风险",
        "start": [r"公司未来发展的展望", r"公司面临的风险", r"可能面对的风险", r"未来发展战略"],
        "stop": [r"公司治理", r"报告期内接待调研", r"重要事项"],
        "max_pages": 6,
        "why": "在手订单/产能/指引的定性表述，前瞻判断的锚",
    },
    "shareholders": {
        "label": "股东与股本变动",
        "start": [r"前十名股东持股情况", r"股东总数", r"股份变动情况表"],
        "stop": [r"优先股", r"债券相关", r"董事、监事"],
        "max_pages": 4,
        "why": "股东户数与前十大变化，机构进出的公开痕迹",
    },
}


def list_sections() -> str:
    lines = ["可用 section（--sections 逗号分隔）：", ""]
    for key, spec in SECTIONS.items():
        lines.append(f"  {key:<15} {spec['label']}")
        lines.append(f"  {'':<15} 用途：{spec['why']}")
    lines.append("")
    lines.append("预设覆盖不到时用 --find 关键词1,关键词2 做全文定位（返回命中处上下文）。")
    return "\n".join(lines)


def _slice_section(pages: Sequence[str], spec: Dict[str, Any]) -> Optional[Tuple[int, int, str]]:
    """(first_page, last_page, text) for a section, or None when absent.

    The last start-anchor hit wins, not the first: a periodic report's table of
    contents mentions every section heading, and anchoring on page 2 would return
    the contents page instead of the section.
    """
    starts = [re.compile(p) for p in spec["start"]]
    stops = [re.compile(p) for p in spec["stop"]]
    hits = [i for i, text in enumerate(pages) if any(rx.search(text) for rx in starts)]
    if not hits:
        return None
    # A report's table of contents names every section heading, so the first
    # anchor hit is usually the contents page. Dot-leader density identifies it;
    # a near-empty page is a divider.
    def is_index(text: str) -> bool:
        return text.count("...") > 8 or text.count("…") > 8 or len(text) < 400

    body = [i for i in hits if not is_index(pages[i])]
    start = (body or hits)[0]
    end = min(len(pages) - 1, start + int(spec["max_pages"]) - 1)
    for i in range(start + 1, end + 1):
        if any(rx.search(pages[i]) for rx in stops):
            end = i
            break
    return start, end, "\n".join(pages[start:end + 1])


def _find_keyword(pages: Sequence[str], keyword: str, context_chars: int,
                  max_hits: int = 6) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, text in enumerate(pages):
        for m in re.finditer(re.escape(keyword), text):
            lo = max(0, m.start() - context_chars // 2)
            hi = min(len(text), m.end() + context_chars // 2)
            out.append({"page": i + 1, "excerpt": text[lo:hi].strip()})
            if len(out) >= max_hits:
                return out
            break  # one excerpt per page keeps repeated headers from flooding
    return out


def disclosure_window(store: Store, code: str, period: str, pad_days: int = 4) -> str:
    """A tight cninfo `seDate` around the known filing date.

    Without it the fallback lookup has to page through a year of a company's
    announcements — a large listed company files hundreds, and the periodic
    report is nowhere near page one. `qreport_disclosure` already knows the
    actual filing date from the scan, so use it.
    """
    actual = (store.load_disclosure(period).get(code) or {}).get("actual_date")
    if actual and len(str(actual)) == 8:
        d = dt.datetime.strptime(str(actual), "%Y%m%d").date()
        lo = (d - dt.timedelta(days=pad_days)).strftime("%Y-%m-%d")
        hi = (d + dt.timedelta(days=pad_days)).strftime("%Y-%m-%d")
        return f"{lo}~{hi}"
    # No calendar row (e.g. an older period never scanned): reports are filed
    # after the period ends, so start there rather than at the year start.
    start = dt.datetime.strptime(period, "%Y%m%d").date()
    today = dt.datetime.now(cn.BEIJING_TZ).date()
    return f"{start.strftime('%Y-%m-%d')}~{today.strftime('%Y-%m-%d')}"


def resolve_announcement(store: Store, code: str, period: str, se_date: str,
                         prefer_summary: bool) -> Optional[Dict[str, Any]]:
    """Prefer the bulk-scanned provenance row; fall back to a targeted lookup."""
    rows = [dict(r) for r in store.load_cninfo_announcements(period) if str(r["ts_code"]) == code]
    if rows:
        rows.sort(key=lambda r: (bool(r.get("is_summary")) == prefer_summary,
                                 bool(r.get("is_corrected")),
                                 str(r.get("ann_date") or "")), reverse=True)
        return rows[0]
    orgid = cn.resolve_orgid(code[:6])
    if not orgid:
        return None
    return cn.find_report_announcement(code[:6], orgid, period, se_date, prefer_summary=prefer_summary)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="按需抽取正式报告 PDF 的指定章节")
    ap.add_argument("--list-sections", action="store_true", help="打印可用 section 后退出")
    ap.add_argument("--period", default=None, help="报告期末 YYYYMMDD")
    ap.add_argument("--code", default=None, help="股票代码，如 300750.SZ")
    ap.add_argument("--sections", default="segment,nonrecurring",
                    help="要抽的章节，逗号分隔；all 表示全部预设章节")
    ap.add_argument("--find", default=None, help="自由关键词定位，逗号分隔")
    ap.add_argument("--context-chars", type=int, default=1200, help="--find 命中处返回的上下文字符数")
    ap.add_argument("--max-chars", type=int, default=12000, help="单个 section 返回的最大字符数")
    ap.add_argument("--summary", action="store_true", help="优先取『报告摘要』（更短，够看分产品收入）")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新下载解析")
    ap.add_argument("--no-cache", action="store_true", help="不落库")
    ap.add_argument("--out", default=None, help="输出 JSON 路径")
    ap.add_argument("--stdout", action="store_true", help="打印 JSON 到标准输出")
    args = ap.parse_args(argv)

    if args.list_sections:
        print(list_sections())
        return 0
    if not args.period or not args.code:
        ap.error("--period 与 --code 必填（或用 --list-sections）")

    period, code = args.period, args.code.strip().upper()
    if not re.fullmatch(r"\d{8}", period) or period[4:] not in cn.REPORT_CATEGORY:
        ap.error("--period 必须是季度末 YYYYMMDD（0331/0630/0930/1231）")
    if not re.fullmatch(r"\d{6}\.(SZ|SH|BJ)", code):
        ap.error("--code 必须是 Tushare A 股代码，如 300750.SZ")
    wanted = list(SECTIONS) if args.sections.strip() == "all" else \
        [s.strip() for s in args.sections.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in SECTIONS]
    if unknown:
        ap.error(f"未知 section: {', '.join(unknown)}\n\n{list_sections()}")

    store = Store(enabled=not args.no_cache)
    notes: List[str] = []
    if not store.available:
        notes.append(f"缓存不可用，本次不复用也不落库：{store.reason}")

    cached = {} if args.refresh else store.load_pdf_sections(code, period)
    keywords = [k.strip() for k in (args.find or "").split(",") if k.strip()]
    missing = [s for s in wanted if s not in cached]

    result: Dict[str, Any] = {
        "meta": {"ts_code": code, "period": period, "requested": wanted,
                 "find": keywords, "notes": notes,
                 "role": "PDF 为按需补充，数值权威仍是 Tushare 结构化报表"},
        "announcement": None,
        "sections": {},
        "find_hits": {},
    }
    for s in wanted:
        if s in cached:
            row = cached[s]
            result["sections"][s] = {
                "label": SECTIONS[s]["label"], "page_span": row.get("page_span"),
                "text": (row.get("text") or "")[:args.max_chars], "from_cache": True,
            }
            result["announcement"] = result["announcement"] or {
                "title": row.get("title"), "url": row.get("url")}

    if not missing and not keywords:
        payload = result
    else:
        se_date = disclosure_window(store, code, period)
        ann = resolve_announcement(store, code, period, se_date, args.summary)
        if not ann:
            notes.append("CNInfo 未找到该期正式报告公告，无法取 PDF")
            payload = result
        else:
            result["announcement"] = {"title": ann.get("title"), "url": ann.get("url"),
                                      "ann_date": ann.get("ann_date"),
                                      "is_corrected": bool(ann.get("is_corrected")),
                                      "is_summary": bool(ann.get("is_summary"))}
            try:
                raw = cn.download_pdf(str(ann.get("url")))
            except Exception as exc:  # noqa: BLE001
                notes.append(f"PDF 下载失败：{str(exc)[:80]}")
                raw = None
            pages = cn.pdf_pages(raw) if raw else []
            if not pages:
                notes.append("PDF 无法解析为文本（可能是扫描件），只能回到结构化数据")
            else:
                result["meta"]["pdf_pages"] = len(pages)
                writes: List[Dict[str, Any]] = []
                for s in missing:
                    sliced = _slice_section(pages, SECTIONS[s])
                    if sliced is None:
                        result["sections"][s] = {
                            "label": SECTIONS[s]["label"], "page_span": None, "text": None,
                            "note": "本报告未检出该章节（季报按规定不含管理层讨论/分部数据时属正常）",
                        }
                        continue
                    lo, hi, text = sliced
                    span = f"{lo + 1}-{hi + 1}"
                    result["sections"][s] = {"label": SECTIONS[s]["label"], "page_span": span,
                                             "text": text[:args.max_chars], "from_cache": False}
                    writes.append({"ts_code": code, "period": period, "section": s,
                                   "title": ann.get("title"), "url": ann.get("url"),
                                   "page_span": span, "text": text})
                store.upsert_pdf_sections(writes)
                for kw in keywords:
                    result["find_hits"][kw] = _find_keyword(pages, kw, args.context_chars)
            payload = result

    out_path = args.out or os.path.join("reports", f"qreport_pdf_{period}_{code.replace('.', '_')}.json")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    if args.stdout:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    found = [s for s, v in payload["sections"].items() if v.get("text")]
    print(f"[ok] {code} {period}：命中 {len(found)}/{len(wanted)} 章节 → {out_path}", file=sys.stderr)
    for n in notes:
        print(f"  · {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
