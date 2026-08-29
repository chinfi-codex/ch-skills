#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SEC EDGAR —— 基本面、GPU 抵押载体、SPV 台账。

三扇门，全免费无密钥：

1. `submissions` —— 申报流，用来定位最新 10-Q/10-K 的 accession。
2. `companyfacts` —— 公司层 XBRL 事实。**工具级债务不在这里**，那里只有合计
   `LongTermDebt`。带维度的事实（DDTL 4.0、VIE 资产）拿不到。
3. `FilingSummary.xml` → `R{n}.htm` —— 财报渲染表。**这才是工具级债务的正确路径。**
   按 ShortName 匹配 "Schedule of Total Debt Obligations" 与 "Delayed Draw Term Loans"。

SEC 不在乎你声称是什么浏览器，它在乎你留联系方式。通用浏览器 UA 会 403——
这跟 Yahoo 的边缘分类器规则正好相反。

SPV 那一层是**一次性事件记录**，不是时间序列。Beignet 只在 Blue Owl 2025Q3 10-Q
的期后事项附注里出现过一次，之后并入 FVO 合计行；后续 10-K 与两期 10-Q 里
"Beignet" 出现 0 次。所以它入库时 quality=disclosure_once，
**渲染层必须拒绝把它画成折线**。
"""

from __future__ import annotations

import datetime as dt
import html as html_mod
import os
import re
from typing import Any, Dict, List, Optional

from .base import CollectResult, RateLimiter, http_get, load_config

_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_REPORT_RE = re.compile(r"<Report[^>]*>(.*?)</Report>", re.S)
_FILE_RE = re.compile(r"<HtmlFileName>(.*?)</HtmlFileName>")
_NAME_RE = re.compile(r"<ShortName>(.*?)</ShortName>", re.S)
_NUM_RE = re.compile(r"-?[\d,]+\.?\d*")

# 基本面标签。取不到就标 regime_na，不填空值——NBIS 是外国私人发行人，
# 它缺标签是正常状态不是采集失败。
_FACT_TAGS = {
    "fin.long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
    "fin.ebitda_proxy_op_income": ["OperatingIncomeLoss"],
    "fin.interest_expense": ["InterestExpense", "InterestExpenseNonoperating",
                             "InterestIncomeExpenseNet"],
    "fin.capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "fin.cash": ["CashAndCashEquivalentsAtCarryingValue"],
}


_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def contact_email(cfg: Dict[str, Any]) -> Optional[str]:
    src = cfg["sources"]["sec"]
    value = os.environ.get(src.get("contact_env", "SEC_CONTACT_EMAIL"), "").strip()
    return value if value and _EMAIL_RE.fullmatch(value) else None


def _headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    """SEC 的 UA 契约：它不在乎你声称是什么浏览器，在乎你留了联系邮箱。

    实测两条路的严格程度不同：`data.sec.gov`（submissions / companyfacts）
    没有邮箱也放行；`www.sec.gov/Archives`（FilingSummary、R-file）没有邮箱
    一律 403。所以缺 SEC_CONTACT_EMAIL 时不去硬撞 Archives，
    直接标 missing_contact 交代清楚，而不是重试三次全是 403。
    """
    src = cfg["sources"]["sec"]
    contact = contact_email(cfg) or "no-contact-configured"
    return {"User-Agent": src["user_agent_template"].format(contact=contact)}


def _text_of(fragment: str) -> str:
    return html_mod.unescape(_TAG_RE.sub("", fragment)).strip()


def _to_number(cell: str) -> Optional[float]:
    match = _NUM_RE.search(cell.replace("$", "").strip())
    if not match:
        return None
    try:
        value = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return -value if cell.strip().startswith("(") else value


def latest_filing(cik: str, cfg: Dict[str, Any], limiter: RateLimiter,
                  forms=("10-Q", "10-K")) -> Optional[Dict[str, str]]:
    import json
    src = cfg["sources"]["sec"]
    limiter.wait()
    raw = http_get(src["endpoints"]["submissions"].format(cik=cik.zfill(10)),
                   headers=_headers(cfg), timeout=30)
    recent = (json.loads(raw).get("filings") or {}).get("recent") or {}
    for form, filed, accession, doc in zip(recent.get("form", []),
                                           recent.get("filingDate", []),
                                           recent.get("accessionNumber", []),
                                           recent.get("primaryDocument", [])):
        if form in forms:
            return {"form": form, "filed": filed,
                    "accession": accession.replace("-", ""),
                    "accession_dashed": accession, "doc": doc}
    return None


def company_facts(cik: str, cfg: Dict[str, Any], limiter: RateLimiter) -> Dict[str, Any]:
    import json
    src = cfg["sources"]["sec"]
    limiter.wait()
    raw = http_get(src["endpoints"]["companyfacts"].format(cik=cik.zfill(10)),
                   headers=_headers(cfg), timeout=60)
    return json.loads(raw)


def _latest_fact(facts: Dict[str, Any], tags: List[str]) -> Optional[Dict[str, Any]]:
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    best: Optional[Dict[str, Any]] = None
    for tag in tags:
        block = gaap.get(tag)
        if not block:
            continue
        for unit_rows in (block.get("units") or {}).values():
            for row in unit_rows:
                end = row.get("end")
                if not end or row.get("val") is None:
                    continue
                if best is None or end > best["end"]:
                    best = {"end": end, "val": float(row["val"]), "tag": tag,
                            "accn": row.get("accn"), "form": row.get("form")}
    return best


def collect_fundamentals(cfg: Optional[Dict[str, Any]] = None,
                         universe: Optional[Dict[str, Any]] = None) -> CollectResult:
    cfg = cfg or load_config("sources.yaml")
    universe = universe or load_config("universe.yaml")
    limiter = RateLimiter(cfg["sources"]["sec"].get("rate_limit_per_second", 8))
    today = dt.date.today().isoformat()
    result = CollectResult(source_id="sec_facts")
    missing: List[str] = []

    for issuer, meta in universe["issuers"].items():
        cik = meta.get("cik")
        if not cik:
            continue
        try:
            facts = company_facts(cik, cfg, limiter)
        except Exception as exc:                        # noqa: BLE001
            missing.append(f"{issuer}: {exc}")
            continue
        for metric, tags in _FACT_TAGS.items():
            fact = _latest_fact(facts, tags)
            if fact is None:
                # 外国私人发行人缺标签是正常状态，不是采集失败。
                result.observations.append({
                    "asof_date": today, "instrument_key": f"ISSUER:{issuer}",
                    "metric": metric, "value": None, "value_text": None,
                    "unit": "usd", "method": "disclosed", "source_id": "sec_facts",
                    "obs_date": today, "staleness_days": 0,
                    "quality": "regime_na", "raw_ref": f"CIK{cik}",
                })
                continue
            result.observations.append({
                "asof_date": fact["end"], "instrument_key": f"ISSUER:{issuer}",
                "metric": metric, "value": fact["val"], "value_text": None,
                "unit": "usd", "method": "disclosed", "source_id": "sec_facts",
                "obs_date": today,
                "staleness_days": (dt.date.fromisoformat(today)
                                   - dt.date.fromisoformat(fact["end"])).days,
                "quality": "ok",
                "raw_ref": f"{fact.get('form')} {fact.get('accn')} [{fact['tag']}]",
            })

    if missing:
        result.status = "partial"
    result.note = (f"{len(result.observations)} 条基本面事实"
                   + ("｜失败：" + "；".join(missing) if missing else ""))
    return result


def collect_gpu_secured(cfg: Optional[Dict[str, Any]] = None,
                        universe: Optional[Dict[str, Any]] = None) -> CollectResult:
    """GPU 抵押载体 —— 走 R-file 抽工具级债务与 VIE 抵押品规模。

    VIE 资产余额就是 GPU 抵押品的账面规模，是残值校验（判据 5）的分母。
    """
    cfg = cfg or load_config("sources.yaml")
    universe = universe or load_config("universe.yaml")
    src = cfg["sources"]["sec"]
    limiter = RateLimiter(src.get("rate_limit_per_second", 8))
    today = dt.date.today().isoformat()
    result = CollectResult(source_id="sec_rfile")
    notes: List[str] = []

    if contact_email(cfg) is None:
        # Archives 路没有邮箱一律 403。不硬撞，交代清楚怎么修。
        result.status = "partial"
        result.note = ("missing_contact：R-file 路径（www.sec.gov/Archives）要求 UA 里带联系邮箱，"
                       "未设置 SEC_CONTACT_EMAIL 时一律 403。"
                       "设置后重跑即可，data.sec.gov 的基本面那一路不受影响。")
        return result

    for issuer, meta in universe["issuers"].items():
        if not meta.get("gpu_secured"):
            continue
        cik = meta["cik"]
        cik_int = str(int(cik))
        filing = latest_filing(cik, cfg, limiter)
        if not filing:
            notes.append(f"{issuer}: 找不到 10-Q/10-K")
            continue
        try:
            limiter.wait()
            summary = http_get(src["endpoints"]["filing_summary"].format(
                cik_int=cik_int, accession=filing["accession"]),
                headers=_headers(cfg), timeout=40)
        except Exception as exc:                        # noqa: BLE001
            notes.append(f"{issuer}: FilingSummary 取不到 {exc}")
            continue

        wanted: List[Dict[str, str]] = []
        for block in _REPORT_RE.findall(summary):
            fname = _FILE_RE.search(block)
            sname = _NAME_RE.search(block)
            if not fname or not sname:
                continue
            short = _text_of(sname.group(1))
            if any(pat.lower() in short.lower() for pat in src["debt_report_patterns"]):
                wanted.append({"file": fname.group(1), "short": short})

        if not wanted:
            notes.append(f"{issuer}: R-file 里没有匹配的债务表")
            continue

        rows_written = 0
        for report in wanted:
            try:
                limiter.wait()
                page = http_get(src["endpoints"]["r_file"].format(
                    cik_int=cik_int, accession=filing["accession"],
                    rfile=report["file"]), headers=_headers(cfg), timeout=40)
            except Exception as exc:                    # noqa: BLE001
                notes.append(f"{issuer}/{report['file']}: {exc}")
                continue
            label: Optional[str] = None
            for tr in _ROW_RE.findall(page):
                cells = [_text_of(c) for c in _CELL_RE.findall(tr)]
                cells = [c for c in cells if c and c not in ("$", "%", ")", "(")]
                if not cells:
                    continue
                head = cells[0]
                if len(cells) == 1:
                    if "Line Items" not in head:
                        label = head
                    continue
                value = _to_number(cells[1])
                if value is None or label is None:
                    continue
                metric_key = head.lower()
                if "interest rate" in metric_key or "stated interest" in metric_key:
                    metric = "col.facility_spread_pct"
                elif "amount" in metric_key or "outstanding" in metric_key:
                    metric = "col.facility_size_usd_mn"
                elif "assets, non-current" in metric_key:
                    metric = "col.vie_assets_noncurrent_usd_mn"
                elif "assets, current" in metric_key:
                    metric = "col.vie_assets_current_usd_mn"
                else:
                    continue
                key = f"{issuer}:{label}"[:180]
                result.observations.append({
                    "asof_date": filing["filed"], "instrument_key": key,
                    "metric": metric, "value": value, "value_text": None,
                    "unit": "pct" if metric.endswith("_pct") else "usd_mn",
                    "method": "disclosed", "source_id": "sec_rfile",
                    "obs_date": today, "staleness_days": 0, "quality": "ok",
                    "raw_ref": f"{filing['accession_dashed']} {report['file']} · {report['short']}",
                })
                rows_written += 1
        notes.append(f"{issuer}: {filing['form']} {filing['filed']} 抽出 {rows_written} 行")

    result.note = "｜".join(notes) if notes else "无 gpu_secured 主体"
    return result


def collect_spv(universe: Optional[Dict[str, Any]] = None) -> CollectResult:
    """SPV 台账 —— 一次性条款记录，不是时间序列。

    条款来自配置（人工从 Blue Owl 10-Q 期后事项附注核录），因为发行人自己不注册、
    Meta 的申报里也没有，唯一的门是**投资方的 SEC 申报**。
    """
    universe = universe or load_config("universe.yaml")
    today = dt.date.today().isoformat()
    result = CollectResult(source_id="spv_ledger")
    fields = [
        ("spv.notes_outstanding", "notes_outstanding_usd_mn", "usd_mn"),
        ("spv.coupon", "coupon_pct", "pct"),
        ("spv.sponsor_equity", "sponsor_equity_commitment_usd_mn", "usd_mn"),
        ("spv.sponsor_stake", "sponsor_stake_pct", "pct"),
        ("spv.portfolio_fv", "portfolio_fv_usd_mn", "usd_mn"),
    ]
    for spv_id, meta in (universe.get("spv") or {}).items():
        key = f"SPV:{spv_id}"
        asof = meta.get("disclosure_date", today)
        for metric, field, unit in fields:
            value = meta.get(field)
            if value is None:
                continue
            when = meta.get("portfolio_fv_asof", asof) if field.startswith("portfolio") else asof
            result.observations.append({
                "asof_date": when, "instrument_key": key, "metric": metric,
                "value": float(value), "value_text": None, "unit": unit,
                "method": "disclosed", "source_id": "spv_ledger",
                "obs_date": today,
                "staleness_days": (dt.date.fromisoformat(today)
                                   - dt.date.fromisoformat(when)).days,
                # 只披露过一次，渲染层必须拒绝把它画成折线。
                "quality": meta.get("quality", "disclosure_once"),
                "raw_ref": meta.get("disclosed_in"),
            })
        for metric, field in (("spv.tenant", "tenant"), ("spv.maturity", "maturity")):
            if meta.get(field):
                result.observations.append({
                    "asof_date": asof, "instrument_key": key, "metric": metric,
                    "value": None, "value_text": str(meta[field]), "unit": "text",
                    "method": "disclosed", "source_id": "spv_ledger",
                    "obs_date": today, "staleness_days": 0,
                    "quality": meta.get("quality", "disclosure_once"),
                    "raw_ref": meta.get("disclosed_in"),
                })
    result.note = f"{len(result.observations)} 条 SPV 台账（一次性条款，非序列）"
    return result
