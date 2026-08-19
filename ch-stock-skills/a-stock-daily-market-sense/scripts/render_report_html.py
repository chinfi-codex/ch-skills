#!/usr/bin/env python3
"""Render a daily-market-sense Markdown report as a self-contained HTML page.

All the generic machinery — CLI parsing, Markdown→HTML, theming, the chart kit
(``window.CK``), text-preservation validation and the pill/hero decorations —
comes from the shared ``html_report`` package (synced into ``scripts/_shared/``).
This file owns only the market-sense-specific bits: loading market_data.json /
evidence_*.json, extracting the index K-line + stock K-line + market-trend
payloads, the pill vocabulary and the JS that draws those charts.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


SCRIPT_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_ROOT.parent
DEFAULT_MARKET_DATA = SKILL_ROOT / "references" / "market_data.json"
INDEX_KLINE_DISPLAY_DAYS = 120
MONTHLY_KLINE_DISPLAY_MONTHS = 72

_BUNDLED_SHARED = SCRIPT_ROOT / "_shared"
_DEV_SHARED = SCRIPT_ROOT.parents[2] / "shared"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

from html_report import (  # noqa: E402
    ChartHook,
    HeroDecoration,
    HookExpectation,
    HtmlReportBuilder,
    PillDecoration,
    RenderJob,
    SectionContract,
    SectionSpec,
    render_report,
)
from html_report.contract import ContractError  # noqa: E402
from dms_output_contract import validate_dms_content  # noqa: E402


# --------------------------------------------------------------------------- #
# Section contract — the only place where the report's display titles matter.
#
# Section numbers deliberately do not appear in any pattern: they are stripped
# before matching, because they *do* drift — adding 1.1 市场状态定位 once pushed
# 情绪趋势 from 1.1 to 1.2 (silently detaching the trend-chart panel and dumping
# five charts at the end of the document), and removing that same section in
# 2026-08 pushed it straight back. Everything downstream addresses these
# sections by key via ``window.__sec``, so neither move touches the charts.
#
# Bump the version whenever references/template/section*.md changes structure.
# --------------------------------------------------------------------------- #
DMS_CONTRACT = SectionContract(
    version="dms/1.4.0",
    sections=[
        SectionSpec("hero_verdict", [r"^一句话盘面判断$"], level=3,
                    source="references/report_template.md:1"),
        SectionSpec("sentiment_trend", [r"^情绪趋势$"], level=3,
                    source="references/template/section1.md:3"),
        SectionSpec("index_trend", [r"^指数趋势$"], level=3,
                    source="references/template/section1.md:24"),
        SectionSpec("market_style", [r"^市场风格$"], level=3,
                    degraded_patterns=[r"风格证据不足，不强行定性"],
                    source="references/template/section1.md:34"),
        SectionSpec("m3_mainline", [r"^主线判定$"], level=3,
                    degraded_patterns=[r"无二星/三星主线，赚钱效应偏资金轮动"],
                    source="references/template/section3.md:7"),
        # 唯一允许整节缺席的一节：没有 ★★★ 主线时模板要求连标题带兜底句一起不输出
        # （d63d4a2）。所以它不是「降级为一句话」，而是真的不在——没有 degraded_patterns。
        # 在场与否由 _validate_dynamic_catalyst 对着 3.1 的 ★★★ 行数双向判定。
        SectionSpec("m3_catalyst", [r"^催化与细分线路推演$"], level=3, required=False,
                    source="references/template/section3.md:27"),
        SectionSpec("m3_leaders", [r"主线领导股与弹性股"], level=3,
                    degraded_patterns=[r"(?:领导股尚未浮现|暂无可靠的(?:领导股|弹性股)|(?:领导股|弹性股)证据不足)"],
                    source="references/template/section3.md:44"),
        SectionSpec("m4_decline", [r"^亏钱效应（爆量下跌）$"], level=2,
                    source="references/template/section4.md:1"),
        SectionSpec("m4_risk_types", [r"^风险类型归纳$"], level=3,
                    source="references/template/section4.md:5"),
        SectionSpec("m4_decline_details", [r"^高强度爆量下跌(?:个股)?明细$"], level=3,
                    source="references/template/section4.md:13"),
        SectionSpec("m5_capacity_up", [r"^容量上涨明细$"], level=3,
                    degraded_patterns=[r"暂无命中"],
                    source="references/template/section5.md:5"),
        SectionSpec("m5_monthly_base", [r"^(?:全市场)?月线平台突破明细$"], level=3,
                    degraded_patterns=[r"暂无命中", r"(?:月线|Tushare).*(?:不可用|失败).+"],
                    source="references/template/section5.md:13"),
        SectionSpec("m5_early_limit", [r"^10:30 前涨停明细$"], level=3,
                    degraded_patterns=[r"暂无命中", r"JRJ.*(?:不可用|error|错误)"],
                    source="references/template/section5.md:21"),
        SectionSpec("m5_discount_relaunch", [r"^折扣启动明细$"], level=3,
                    degraded_patterns=[r"暂无命中"],
                    source="references/template/section5.md:29"),
        SectionSpec("m5_overlap", [r"^交叉命中上涨归因$"], level=3,
                    degraded_patterns=[r"今日四组之间无交叉命中股票"],
                    source="references/template/section5.md:37"),
    ],
    order="strict",
)


# --------------------------------------------------------------------------- #
# Data loading (market-sense-specific)
# --------------------------------------------------------------------------- #
def load_market_data(path: Path) -> dict:
    if not path.exists():
        return {
            "metadata": {"missing": True, "source": str(path)},
            "columns": [],
            "records": [],
            "series": {},
            "quality": {"records_available": 0, "has_120_records": False},
        }
    return json.loads(path.read_text(encoding="utf-8"))


def report_trade_date(input_path: Path) -> Optional[str]:
    match = re.match(r"^report_(\d{8})$", input_path.stem)
    return match.group(1) if match else None


def market_data_for_report(payload: dict, trade_date: Optional[str], source_path: Path) -> dict:
    """Date-scope chart data and reject stale derivatives.

    A global market_data.json may extend beyond a historical report date, so
    the renderer filters future rows.  For dated daily reports, the requested
    date itself is mandatory; otherwise a stale JSON must fail closed.
    """
    if not trade_date:
        return payload
    records = payload.get("records") or []
    eligible = [
        row for row in records
        if isinstance(row, dict)
        and str(row.get("trade_date") or "")
        and str(row.get("trade_date")) <= trade_date
    ]
    eligible_dates = {str(row.get("trade_date")) for row in eligible}
    source_window_end = str((payload.get("metadata") or {}).get("window_end") or "")
    if trade_date not in eligible_dates:
        raise RuntimeError(
            "market chart data freshness gate failed: "
            f"required trade_date={trade_date}, window_end={source_window_end or 'missing'}, "
            f"path={source_path}"
        )
    scoped = dict(payload)
    scoped_metadata = dict(payload.get("metadata") or {})
    scoped_metadata["report_trade_date"] = trade_date
    scoped_metadata["window_end"] = trade_date
    scoped["metadata"] = scoped_metadata
    scoped["records"] = eligible
    scoped_quality = dict(payload.get("quality") or {})
    scoped_quality["records_available"] = len(eligible)
    scoped_quality["has_120_records"] = len(eligible) >= 120
    scoped["quality"] = scoped_quality
    return scoped


def default_evidence_path(input_path: Path) -> Optional[Path]:
    match = re.match(r"^report_(\d{8})$", input_path.stem)
    if not match:
        return None
    return input_path.with_name(f"evidence_{match.group(1)}_utf8.json")


def default_kline_path(input_path: Path) -> Optional[Path]:
    match = re.match(r"^report_(\d{8})$", input_path.stem)
    if not match:
        return None
    return input_path.with_name(f"kline_{match.group(1)}.json")


def load_evidence(path: Optional[Path]) -> dict:
    if path is None or not path.exists():
        return {
            "metadata": {"missing": True, "source": str(path) if path is not None else ""},
            "market_trend": {"indices": {}},
        }
    return json.loads(path.read_text(encoding="utf-8"))


def load_stock_klines(evidence: dict, kline_path: Optional[Path]) -> dict:
    """Stock klines live in a sibling kline_YYYYMMDD.json; older evidence
    files carried them inline under stock_kline_records."""
    if kline_path is not None and kline_path.exists():
        return json.loads(kline_path.read_text(encoding="utf-8"))
    inline = evidence.get("stock_kline_records")
    return inline if isinstance(inline, dict) else {}


def extract_index_kline_payload(evidence: dict, source_path: Optional[Path]) -> Dict[str, Any]:
    indices = ((evidence.get("market_trend") or {}).get("indices") or {})
    payload: Dict[str, Any] = {
        "metadata": {
            "source": str(source_path) if source_path is not None else "",
            "missing": bool((evidence.get("metadata") or {}).get("missing")),
        },
        "indices": {},
    }
    for key in ("shanghai", "chinext", "star50"):
        item = indices.get(key) or {}
        records = item.get("kline_records") if isinstance(item, dict) else []
        if isinstance(records, list):
            records = records[-INDEX_KLINE_DISPLAY_DAYS:]
        else:
            records = []
        payload["indices"][key] = {
            "available": bool(item.get("available")) and bool(records),
            "name": item.get("name"),
            "ts_code": item.get("ts_code"),
            "trade_date": item.get("trade_date"),
            "kline_days": item.get("kline_days"),
            "kline_days_requested": item.get("kline_days_requested"),
            "records": records,
        }
    return payload


def extract_stock_kline_payload(raw: dict, source_path: Optional[Path], missing: bool = False) -> Dict[str, Any]:
    raw = raw or {}
    by_ts_code = raw.get("by_ts_code") if isinstance(raw, dict) else {}
    name_to_ts_code = raw.get("name_to_ts_code") if isinstance(raw, dict) else {}
    payload: Dict[str, Any] = {
        "metadata": {
            "source": str(source_path) if source_path is not None else "",
            "missing": missing,
            "kline_days_requested": (raw.get("metadata") or {}).get("kline_days_requested") if isinstance(raw, dict) else None,
            "price_adjustment": (raw.get("metadata") or {}).get("price_adjustment") if isinstance(raw, dict) else None,
        },
        "by_ts_code": {},
        "name_to_ts_code": name_to_ts_code if isinstance(name_to_ts_code, dict) else {},
    }
    if not isinstance(by_ts_code, dict):
        return payload
    for ts_code, item in by_ts_code.items():
        if not isinstance(item, dict):
            continue
        records = item.get("records")
        if isinstance(records, list):
            records = records[-INDEX_KLINE_DISPLAY_DAYS:]
        else:
            records = []
        payload["by_ts_code"][str(ts_code)] = {
            "available": bool(item.get("available")) and bool(records),
            "name": item.get("name"),
            "ts_code": item.get("ts_code") or ts_code,
            "trade_date": item.get("trade_date"),
            "price_adjustment": item.get("price_adjustment") or payload["metadata"].get("price_adjustment"),
            "kline_days": item.get("kline_days"),
            "kline_days_requested": item.get("kline_days_requested"),
            "records": records,
        }

    # 月线平台突破组（4.2）的月线序列：多年底部箱体 + pivot + 突破月，供月线 K 线图。
    monthly_raw = raw.get("monthly") if isinstance(raw, dict) else None
    if isinstance(monthly_raw, dict):
        m_by = monthly_raw.get("by_ts_code")
        m_name = monthly_raw.get("name_to_ts_code")
        monthly_payload: Dict[str, Any] = {
            "by_ts_code": {},
            "name_to_ts_code": m_name if isinstance(m_name, dict) else {},
        }
        if isinstance(m_by, dict):
            for ts_code, item in m_by.items():
                if not isinstance(item, dict):
                    continue
                records = item.get("records")
                records = records[-MONTHLY_KLINE_DISPLAY_MONTHS:] if isinstance(records, list) else []
                monthly_payload["by_ts_code"][str(ts_code)] = {
                    "available": bool(records),
                    "name": item.get("name"),
                    "ts_code": item.get("ts_code") or ts_code,
                    "base_top_pivot": item.get("base_top_pivot"),
                    "base_start_month": item.get("base_start_month"),
                    "breakout_month": item.get("breakout_month"),
                    "records": records,
                }
        payload["monthly"] = monthly_payload
    return payload


def extract_state_timeline_payload(evidence: dict) -> Optional[Dict[str, Any]]:
    """状态卡的 20 日档位轨迹：趋势档 + 极值分，按交易日对齐。

    两根轴分别来自 `trend_state_card.history` 与 `extreme_state.recent`，都由脚本
    按日算好；渲染层只负责画，不重算任何读数。任一轴缺失就只画另一轴，两轴都缺
    才返回 None（此时不注册图表，门禁按 no_payload 记）。
    """
    trend = evidence.get("trend_state_card") or {}
    extreme = evidence.get("extreme_state") or {}
    states = [
        {"date": str(row.get("date") or ""), "state": str(row.get("state") or ""),
         "phase": row.get("phase") or "", "groups": row.get("groups") or []}
        for row in (trend.get("history") or [])
        if isinstance(row, dict) and row.get("date") and row.get("state")
    ] if trend.get("available") else []
    scores = [
        {"date": str(row.get("date") or ""), "washout": row.get("washout"), "top": row.get("top")}
        for row in (extreme.get("recent") or [])
        if isinstance(row, dict) and row.get("date")
    ] if extreme.get("available") else []
    if not states and not scores:
        return None
    return {
        "states": states,
        "scores": scores,
        "washout_max": (extreme.get("washout") or {}).get("max_score", 6),
        "top_max": (extreme.get("top") or {}).get("max_score", 5),
        "data_through": trend.get("data_through") or extreme.get("data_through"),
    }


def _clean_style_series_records(records: Any, display_days: int) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    if not isinstance(records, list):
        return cleaned
    for row in records:
        if not isinstance(row, dict):
            continue
        trade_date = str(row.get("trade_date") or "").strip()
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if trade_date and close == close:
            cleaned.append({"trade_date": trade_date, "close": round(close, 2)})
    return sorted(cleaned, key=lambda item: item["trade_date"])[-display_days:]


def extract_style_series_payload(evidence: dict, display_days: int = 60) -> Optional[Dict[str, Any]]:
    style = ((evidence or {}).get("market_trend") or {}).get("market_style") or {}
    if not isinstance(style, dict) or not style.get("available"):
        return None
    indices = style.get("indices") or {}
    items: List[Dict[str, Any]] = []
    for key, item in indices.items():
        if not isinstance(item, dict) or not item.get("available"):
            continue
        series = item.get("series") or {}
        records = _clean_style_series_records(series.get("records"), display_days)
        if not records:
            continue
        items.append({
            "key": key,
            "name": item.get("name"),
            "style_role": item.get("style_role"),
            "proxy_note": item.get("proxy_note"),
            "records": records,
        })
    if not items:
        return None
    return {
        "display_days": int(display_days),
        "trade_date": style.get("trade_date"),
        "indices": items,
    }


# --------------------------------------------------------------------------- #
# Decorations: pill vocabulary + "一句话盘面判断" hero card (mechanism lives in
# the shared package; here we only declare the market-sense-specific data).
# --------------------------------------------------------------------------- #
MARKET_SENSE_PILL_RULES = [
    (r"^高位强势股退潮$", "pill neg"),
    (r"^流动性杀跌$", "pill warn"),
    (r"^主线内部分歧$", "pill warn"),
    (r"^高位趋势$", "pill"),
    (r"^高$", "pill neg"),
    (r"^中$", "pill warn"),
    (r"^低$", "pill pos"),
    (r"^领导股$", "pill"),
    (r"^弹性股$", "pill violet"),
    (r"^启动型$|^持续换手型$|^分歧型$", "pill"),
    (r"^[ABC]$", "pill"),
]

HERO_KEYWORDS = "上证|创业板|科创50|国证2000|中证红利|半导体设备与材料|电力能源"
MARKET_SENSE_EXTRA_CSS = """
.trend-state-card { border: 1px solid var(--line-2); border-radius: 12px; padding: 14px 18px; margin: 14px 0 22px; background: rgba(127,127,127,.05); }
.trend-state-card .tsc-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.trend-state-card .tsc-state { font-weight: 700; font-size: 14px; padding: 2px 12px; border-radius: 999px; }
.trend-state-card .tsc-state.s-pos { background: rgba(40,160,90,.14); color: #1e8449; }
.trend-state-card .tsc-state.s-std { background: rgba(127,127,127,.14); color: var(--ink-2, #555); }
.trend-state-card .tsc-state.s-warn { background: rgba(230,160,30,.16); color: #b9770e; }
.trend-state-card .tsc-state.s-neg { background: rgba(220,60,60,.14); color: #c0392b; }
.trend-state-card .tsc-state.s-ice { background: rgba(70,120,220,.16); color: #2e5fb8; }
.trend-state-card .tsc-extreme { font-weight: 600; font-size: 13px; padding: 2px 10px; border-radius: 999px; background: rgba(127,127,127,.12); color: var(--ink-2, #555); }
.trend-state-card .tsc-extreme.x-wash { background: rgba(70,120,220,.16); color: #2e5fb8; }
.trend-state-card .tsc-extreme.x-top { background: rgba(230,160,30,.16); color: #b9770e; }
.trend-state-card .tsc-through { margin-left: auto; font-size: 12px; color: var(--ink-3, #888); }
.trend-state-card ul { margin: 0; padding: 0; list-style: none; }
.trend-state-card li { padding: 5px 0; border-top: 1px dashed var(--line-2); font-size: 13px; line-height: 1.65; }
.trend-state-card li:first-child { border-top: 0; }
.state-timeline { margin-top: 12px; border-top: 1px dashed var(--line-2); padding-top: 10px; overflow-x: auto; }
.state-timeline svg { display: block; max-width: 100%; height: auto; }
.state-timeline .stl-legend { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 6px; font-size: 11px; color: var(--ink-3, #888); }
.state-timeline .stl-legend span { display: inline-flex; align-items: center; gap: 4px; }
.state-timeline .stl-legend i { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.kline-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.style-compare-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 12px 0 22px; }
.style-compare-card { min-width: 0; margin: 0; }
.style-compare-legend { display: flex; flex-wrap: wrap; gap: 6px 12px; color: var(--ink-3); font-size: 12px; margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--line-2); }
.style-compare-legend span { display: inline-flex; align-items: center; gap: 6px; font-family: var(--font-mono); }
.style-compare-legend svg { width: 24px; height: 8px; overflow: visible; }
.style-compare-note { margin-top: 6px; color: var(--ink-3); font-size: 12px; line-height: 1.55; }
@media (max-width: 900px) { .style-compare-grid { grid-template-columns: 1fr; } }
@media (max-width: 900px) { .kline-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .kline-grid { grid-template-columns: 1fr; } }
"""


# Upgrade the trend-state readings at the top of "1.1 情绪趋势" (the ULs whose
# first item is 趋势状态) into a styled state card with a coloured state badge;
# the sentiment table and the ==趋势判断== highlight stay outside the card.
TREND_STATE_CARD_JS = r"""(function () {
  const root = document.getElementById("report-body");
  if (!root) return;
  const heading = window.__sec ? window.__sec.head("sentiment_trend") : null;
  if (!heading) {
    if (window.__render) window.__render.fail("decoration:trend-state-card", "section [sentiment_trend] not found");
    return;
  }
  const uls = [];
  let cur = heading.nextElementSibling;
  while (cur && !/^H[1-6]$/.test(cur.tagName)) {
    const next = cur.nextElementSibling;
    if (cur.tagName === "UL") { uls.push(cur); cur = next; continue; }
    break;
  }
  if (!uls.length || !/趋势状态[：:]/.test(uls[0].textContent)) return;
  const card = document.createElement("aside");
  card.className = "trend-state-card";
  const head = document.createElement("div");
  head.className = "tsc-head";
  card.appendChild(head);
  heading.after(card);
  uls.forEach(ul => card.appendChild(ul));

  const text = card.textContent;
  const m = /趋势状态[：:]\s*(深度退潮|退潮|冰点|谨慎|标准|进攻)/.exec(text);
  if (m) {
    const cls = { "进攻": "s-pos", "标准": "s-std", "谨慎": "s-warn", "退潮": "s-neg", "深度退潮": "s-neg", "冰点": "s-ice" }[m[1]];
    const badge = document.createElement("span");
    badge.className = "tsc-state " + cls;
    const dm = /（第\s*(\d+)\s*日）/.exec(text);
    badge.textContent = m[1] + (dm ? "（第 " + dm[1] + " 日）" : "");
    head.appendChild(badge);
    const li = card.querySelector("li");
    if (li && /^\s*趋势状态[：:]/.test(li.textContent)) li.remove();
  }
  /* 极值轴单独起一个胶囊：它和趋势档是两根正交的轴，并排放才不会被读成
     "趋势档的补充说明"。行文里那条 li 同样收进卡头，避免重复。 */
  const ex = /极值轴[：:]\s*出清\s*(\d)\s*\/\s*6[^｜|]*[｜|]\s*顶部\s*(\d)\s*\/\s*5/.exec(text);
  if (ex) {
    const wash = Number(ex[1]), top = Number(ex[2]);
    const pill = document.createElement("span");
    pill.className = "tsc-extreme" + (wash >= 2 ? " x-wash" : (top >= 2 ? " x-top" : ""));
    pill.textContent = "出清 " + wash + "/6 ｜ 顶部 " + top + "/5";
    head.appendChild(pill);
    Array.from(card.querySelectorAll("li")).forEach(li => {
      if (/^\s*极值轴[：:]/.test(li.textContent)) li.remove();
    });
  }
  if (!head.childElementCount) head.remove();
})();"""


# --------------------------------------------------------------------------- #
# 20 日档位时间轴：趋势档色带 + 极值分柱，全部按交易日对齐。
# 读数来自脚本，这里只负责画。
# --------------------------------------------------------------------------- #
STATE_TIMELINE_JS = r"""
const states = __payload.states || [];
const scores = __payload.scores || [];
const washMax = __payload.washout_max || 6;
const topMax = __payload.top_max || 5;
const SEC = window.__sec;
const REPORT = window.__render;
const hook = "state-timeline";

const anchor = document.querySelector(".trend-state-card") || SEC.head("sentiment_trend");
if (!anchor) {
  REPORT.fail("hook:" + hook, "no trend-state-card or [sentiment_trend] heading to anchor to");
  return;
}
if (!states.length && !scores.length) {
  REPORT.attest(hook, { rendered: 0, matched: 0, expected: 1, unmatched: [{ name: "state-timeline", reason: "no_payload" }] });
  return;
}

const STATE_COLOR = {
  "进攻": "#1e8449", "标准": "#8a8a8a", "谨慎": "#d9a441",
  "退潮": "#d1604f", "深度退潮": "#a32f22", "冰点": "#2e5fb8"
};
const STATE_ORDER = ["进攻", "标准", "谨慎", "退潮", "深度退潮", "冰点"];

/* 两轴的日期取并集后排序：极值卡与趋势卡的可用窗口不一定一样长 */
const dates = Array.from(new Set([].concat(
  states.map(s => s.date), scores.map(s => s.date)
))).filter(Boolean).sort();
const stateBy = Object.fromEntries(states.map(s => [s.date, s]));
const scoreBy = Object.fromEntries(scores.map(s => [s.date, s]));

const n = dates.length;
const padL = 52, padR = 6, padT = 16, padB = 24;
const cellW = Math.max(18, Math.min(38, Math.floor((700 - padL - padR) / Math.max(n, 1))));
const W = padL + padR + cellW * n;
const ribbonH = 26, gap = 10, barH = 26;
const rows = [{ key: "ribbon", h: ribbonH }];
if (scores.length) rows.push({ key: "washout", h: barH }, { key: "top", h: barH });
const H = padT + padB + rows.reduce((a, r) => a + r.h, 0) + gap * (rows.length - 1);

const NS = "http://www.w3.org/2000/svg";
const svg = document.createElementNS(NS, "svg");
svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
svg.setAttribute("width", String(W));
svg.setAttribute("height", String(H));
svg.setAttribute("role", "img");
svg.setAttribute("aria-label", "近 20 个交易日的趋势档位与极值分轨迹");

function label(x, y, text, opts) {
  const t = document.createElementNS(NS, "text");
  t.setAttribute("x", String(x));
  t.setAttribute("y", String(y));
  t.setAttribute("font-size", (opts && opts.size) || "10");
  t.setAttribute("fill", (opts && opts.fill) || "currentColor");
  t.setAttribute("text-anchor", (opts && opts.anchor) || "start");
  if (opts && opts.weight) t.setAttribute("font-weight", opts.weight);
  t.setAttribute("opacity", (opts && opts.opacity) || "0.75");
  t.textContent = text;
  return t;
}

let y = padT;
// —— 档位色带：同一档位连续的几天并成一段，段上写档位名
svg.appendChild(label(4, y + ribbonH / 2 + 4, "趋势档", { size: "11", opacity: "0.85" }));
let i = 0;
while (i < n) {
  const cur = (stateBy[dates[i]] || {}).state || "";
  let j = i;
  while (j + 1 < n && ((stateBy[dates[j + 1]] || {}).state || "") === cur) j++;
  const x = padL + i * cellW;
  const w = (j - i + 1) * cellW;
  const rect = document.createElementNS(NS, "rect");
  rect.setAttribute("x", String(x + 0.5));
  rect.setAttribute("y", String(y));
  rect.setAttribute("width", String(Math.max(w - 1, 1)));
  rect.setAttribute("height", String(ribbonH));
  rect.setAttribute("rx", "3");
  rect.setAttribute("fill", cur ? (STATE_COLOR[cur] || "#8a8a8a") : "rgba(127,127,127,.18)");
  rect.setAttribute("fill-opacity", cur ? "0.85" : "1");
  const title = document.createElementNS(NS, "title");
  const phase = (stateBy[dates[i]] || {}).phase || "";
  title.textContent = dates[i] + (j > i ? " ~ " + dates[j] : "") + "　" + (cur || "无数据") + (phase ? "·" + phase : "");
  rect.appendChild(title);
  svg.appendChild(rect);
  if (cur) {
    const short = { "深度退潮": "深退", "标准": "标准", "进攻": "进攻" }[cur] || cur;
    const text = w >= cur.length * 12 + 8 ? cur : (w >= short.length * 10 + 6 ? short : "");
    if (text) {
      svg.appendChild(label(x + w / 2, y + ribbonH / 2 + 4, text,
        { anchor: "middle", fill: "#fff", opacity: "0.95", weight: "600", size: text === cur ? "11" : "9" }));
    }
  }
  i = j + 1;
}
y += ribbonH + gap;

// —— 极值分：0~max 的小柱，出清分向上、顶部分向上但换色，阈值线画在 2 分处
function bars(key, max, color, name, threshold) {
  svg.appendChild(label(4, y + barH / 2 + 4, name, { size: "11", opacity: "0.85" }));
  const thrY = y + barH - (threshold / max) * barH;
  const line = document.createElementNS(NS, "line");
  line.setAttribute("x1", String(padL)); line.setAttribute("x2", String(padL + n * cellW));
  line.setAttribute("y1", String(thrY)); line.setAttribute("y2", String(thrY));
  line.setAttribute("stroke", "currentColor");
  line.setAttribute("stroke-opacity", "0.25");
  line.setAttribute("stroke-dasharray", "3 3");
  svg.appendChild(line);
  dates.forEach((d, k) => {
    const v = (scoreBy[d] || {})[key];
    if (v === null || v === undefined) return;
    const h = (v / max) * barH;
    const rect = document.createElementNS(NS, "rect");
    rect.setAttribute("x", String(padL + k * cellW + 2));
    rect.setAttribute("y", String(y + barH - h));
    rect.setAttribute("width", String(Math.max(cellW - 4, 2)));
    rect.setAttribute("height", String(Math.max(h, v > 0 ? 2 : 1)));
    rect.setAttribute("rx", "1.5");
    rect.setAttribute("fill", color);
    rect.setAttribute("fill-opacity", v >= threshold ? "0.9" : "0.35");
    const title = document.createElementNS(NS, "title");
    title.textContent = d + "　" + name + " " + v + "/" + max;
    rect.appendChild(title);
    svg.appendChild(rect);
    if (v >= threshold) {
      svg.appendChild(label(padL + k * cellW + cellW / 2, y + barH - h - 2, String(v),
        { anchor: "middle", size: "9", fill: color, opacity: "0.95", weight: "600" }));
    }
  });
  y += barH + gap;
}
if (scores.length) {
  bars("washout", washMax, "#2a9d8f", "出清分", 2);
  bars("top", topMax, "#b9770e", "顶部分", 2);
}

// —— 日期轴：密的时候隔几个标一个，末日必标
const step = cellW >= 34 ? 2 : (cellW >= 24 ? 3 : 4);
dates.forEach((d, k) => {
  if (k % step !== 0 && k !== n - 1) return;
  const md = d.length >= 10 ? d.slice(5) : d;
  svg.appendChild(label(padL + k * cellW + cellW / 2, H - 6, md, { anchor: "middle", size: "9", opacity: "0.6" }));
});

const wrap = document.createElement("div");
wrap.className = "state-timeline";
const cap = document.createElement("div");
cap.style.cssText = "font-size:12px;opacity:.7;margin-bottom:4px;";
cap.textContent = "近 " + n + " 个交易日档位轨迹" + (__payload.data_through ? "（数据截至 " + __payload.data_through + "）" : "");
wrap.appendChild(cap);
wrap.appendChild(svg);
const legend = document.createElement("div");
legend.className = "stl-legend";
STATE_ORDER.forEach(s => {
  const span = document.createElement("span");
  const dot = document.createElement("i");
  dot.style.background = STATE_COLOR[s];
  span.appendChild(dot);
  span.appendChild(document.createTextNode(s));
  legend.appendChild(span);
});
if (scores.length) {
  const note = document.createElement("span");
  note.textContent = "虚线 = 2 分线（出清区 / 顶部风险的起判点）";
  legend.appendChild(note);
}
wrap.appendChild(legend);
anchor.appendChild(wrap);
REPORT.attest(hook, { rendered: 1, matched: 1, expected: 1, unmatched: [], el: wrap });
"""


# --------------------------------------------------------------------------- #
# Chart drawing: index K-lines + stock-table K-lines.
# Reads its payload slice from __payload; draws with the shared chart kit (CK).
# --------------------------------------------------------------------------- #
KLINE_CHARTS_JS = r"""
const payload = __payload.index || {};
const stockPayload = __payload.stocks || {};
const klineDisplayDays = __payload.display_days || 120;
const indices = payload.indices || {};
const stockByCode = stockPayload.by_ts_code || {};
const stockNameIndex = stockPayload.name_to_ts_code || {};
const monthlyPayload = __payload.stocks_monthly || {};
const monthlyByCode = monthlyPayload.by_ts_code || {};
const monthlyNameIndex = monthlyPayload.name_to_ts_code || {};
const klineDisplayMonths = __payload.display_months || 72;
const reportBody = document.getElementById("report-body");
if (!reportBody) return;

const { svgEl, svgText } = CK;
const formatDate = CK.fmt.date;
const formatPercent = CK.fmt.signedPct;

const SEC = window.__sec;
const REPORT = window.__render;

const indexConfigs = [
  { key: "shanghai", fallbackTitle: "上证指数" },
  { key: "chinext", fallbackTitle: "创业板指数" },
  { key: "star50", fallbackTitle: "科创50" }
];
const stockSectionConfigs = [
  { sec: "m3_leaders", hook: "klines.m3_leaders", gridLabel: "module3-leaders" },
  { sec: "m5_capacity_up", hook: "klines.m5_capacity_up", gridLabel: "module5-capacity-up" },
  { sec: "m5_early_limit", hook: "klines.m5_early_limit", gridLabel: "module5-early-limit" },
  { sec: "m5_discount_relaunch", hook: "klines.m5_discount_relaunch", gridLabel: "module5-discount-relaunch" }
];

insertIndexKlines();
insertStockTableKlines();
insertMonthlyBreakoutKlines();

function insertIndexKlines() {
  const hook = "klines.index";
  const unmatched = [];
  const prepared = [];
  indexConfigs.forEach(config => {
    const indexData = indices[config.key] || {};
    const rows = normalizeRows(indexData.records).slice(-klineDisplayDays);
    if (!rows.length) {
      unmatched.push({
        name: config.fallbackTitle,
        reason: (indexData.records || []).length ? "no_records_in_window" : "no_kline_data"
      });
      return;
    }
    prepared.push({ ...config, indexData, rows });
  });

  if (!prepared.length) {
    REPORT.attest(hook, { rendered: 0, matched: 0, expected: indexConfigs.length, unmatched: unmatched });
    return;
  }
  /* Anchor after the section's own table; no document-tail fallback — a chart
     that cannot find its section must fail the gate, not relocate itself. */
  const anchorEl = SEC.find("index_trend", ".table-wrap") || SEC.tail("index_trend");
  if (!anchorEl) {
    REPORT.fail("hook:" + hook, "no insertion anchor inside section [index_trend]");
    return;
  }

  const grid = document.createElement("div");
  grid.className = "kline-grid";
  prepared.forEach(item => {
    grid.appendChild(buildKlineCard(item.rows, item.indexData, item.fallbackTitle));
  });
  anchorEl.after(grid);
  REPORT.attest(hook, {
    rendered: prepared.length, matched: prepared.length,
    expected: indexConfigs.length, unmatched: unmatched, el: grid
  });
}

function insertStockTableKlines() {
  stockSectionConfigs.forEach(config => {
    const tableWrap = SEC.find(config.sec, ".table-wrap");
    if (tableWrap && tableWrap.dataset.stockKlinesInserted === "1") return;
    const table = readStockTable(tableWrap);
    if (table.present && !table.hasStockColumn && table.rows) {
      REPORT.fail("hook:" + config.hook, "table in section [" + config.sec + "] has " + table.rows + " rows but no 股票 column");
      return;
    }
    if (!table.names.length) {
      /* No candidates today: the report writes 暂无命中 instead of a table. */
      REPORT.attest(config.hook, {
        rendered: 0, matched: 0, expected: 0, unmatched: [],
        note: table.present ? "table present, no rows" : "no table — section reports no candidates"
      });
      return;
    }

    const names = table.names;
    const unmatched = [];
    const prepared = [];
    names.forEach(name => {
      const stockData = findStockData(name);
      if (!stockData) { unmatched.push({ name: name, reason: "no_kline_data" }); return; }
      const rows = normalizeRows(stockData.records).slice(-klineDisplayDays);
      if (!rows.length) { unmatched.push({ name: name, reason: "no_records_in_window" }); return; }
      prepared.push({ rows, stockData: { ...stockData, name }, fallbackTitle: name });
    });

    if (!prepared.length) {
      REPORT.attest(config.hook, { rendered: 0, matched: 0, expected: names.length, unmatched: unmatched });
      return;
    }

    const grid = document.createElement("div");
    grid.className = `stock-kline-grid stock-kline-grid-${config.gridLabel}`;
    prepared.forEach(item => {
      grid.appendChild(buildKlineCard(item.rows, item.stockData, item.fallbackTitle));
    });
    tableWrap.after(grid);
    tableWrap.dataset.stockKlinesInserted = "1";
    REPORT.attest(config.hook, {
      rendered: prepared.length, matched: prepared.length,
      expected: names.length, unmatched: unmatched, el: grid
    });
  });
}

// 4.2 全市场月线平台突破：画月线 K 线 + 多年底部箱体阴影 + 箱体上沿 pivot 线 + 突破月标记。
function insertMonthlyBreakoutKlines() {
  const hook = "klines.m5_monthly_base";
  const tableWrap = SEC.find("m5_monthly_base", ".table-wrap");
  if (tableWrap && tableWrap.dataset.stockKlinesInserted === "1") return;
  const table = readStockTable(tableWrap);
  if (table.present && !table.hasStockColumn && table.rows) {
    REPORT.fail("hook:" + hook, "table in section [m5_monthly_base] has " + table.rows + " rows but no 股票 column");
    return;
  }
  if (!table.names.length) {
    REPORT.attest(hook, {
      rendered: 0, matched: 0, expected: 0, unmatched: [],
      note: table.present ? "table present, no rows" : "no table — section reports no candidates"
    });
    return;
  }

  const names = table.names;
  const unmatched = [];
  const prepared = [];
  names.forEach(name => {
    const data = findMonthlyData(name);
    if (!data) { unmatched.push({ name: name, reason: "no_kline_data" }); return; }
    const rows = normalizeRows(data.records).slice(-klineDisplayMonths);
    if (!rows.length) { unmatched.push({ name: name, reason: "no_records_in_window" }); return; }
    prepared.push({ rows, data: { ...data, name }, fallbackTitle: name });
  });

  if (!prepared.length) {
    REPORT.attest(hook, { rendered: 0, matched: 0, expected: names.length, unmatched: unmatched });
    return;
  }
  const grid = document.createElement("div");
  grid.className = "stock-kline-grid stock-kline-grid-module5-monthly-base";
  prepared.forEach(item => {
    grid.appendChild(buildMonthlyCard(item.rows, item.data, item.fallbackTitle));
  });
  tableWrap.after(grid);
  tableWrap.dataset.stockKlinesInserted = "1";
  REPORT.attest(hook, {
    rendered: prepared.length, matched: prepared.length,
    expected: names.length, unmatched: unmatched, el: grid
  });
}

function findMonthlyData(name) {
  const normalized = normalizeStockName(name);
  const tsCode = monthlyNameIndex[name] || monthlyNameIndex[normalized];
  if (!tsCode) return null;
  return monthlyByCode[tsCode] || null;
}

function buildMonthlyCard(rows, data, fallbackTitle) {
  const card = document.createElement("article");
  card.className = "kline-card";
  card.style.position = "relative";
  const title = document.createElement("div");
  title.className = "chart-title";
  title.textContent = `${data.name || fallbackTitle} 月线 · 多年底部箱体突破`;
  const subtitle = document.createElement("div");
  subtitle.className = "chart-subtitle";
  const first = rows[0];
  const last = rows[rows.length - 1];
  const pivot = Number(data.base_top_pivot);
  subtitle.textContent = `${formatMonth(first.trade_date)} 至 ${formatMonth(last.trade_date)} · ${rows.length} 个月`
    + (Number.isFinite(pivot) ? ` · 箱体上沿 ${pivot.toFixed(2)}` : "");
  card.appendChild(title);
  card.appendChild(subtitle);
  card.appendChild(drawMonthlyKline(rows, card, data));
  card.appendChild(CK.legend([
    ["月K", "var(--neg)"],
    ["箱体上沿", "var(--orange)"],
    ["横盘箱体", "rgba(245,158,11,0.18)"],
    ["成交量", "rgba(100,116,139,0.55)"]
  ]));
  return card;
}

function drawMonthlyKline(rows, card, meta) {
  const width = 560;
  const height = 340;
  const pad = { left: 48, right: 14, top: 12, bottom: 24 };
  const usableW = width - pad.left - pad.right;
  const volPanelH = 64;
  const panelGap = 16;
  const priceH = height - pad.top - pad.bottom - volPanelH - panelGap;
  const volTop = pad.top + priceH + panelGap;
  const volBottom = volTop + volPanelH;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });

  const pivot = Number(meta && meta.base_top_pivot);
  const allPrices = rows.flatMap(r => [r.high, r.low]).filter(Number.isFinite);
  if (Number.isFinite(pivot)) allPrices.push(pivot);
  let min = Math.min(...allPrices);
  let max = Math.max(...allPrices);
  if (!Number.isFinite(min) || !Number.isFinite(max)) { min = 0; max = 1; }
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.05;
  max += span * 0.08;

  const x = idx => pad.left + (rows.length <= 1 ? usableW / 2 : idx / (rows.length - 1) * usableW);
  const y = price => pad.top + (max - price) / (max - min) * priceH;
  const candleWidth = Math.max(2, Math.min(7, usableW / Math.max(rows.length, 1) * 0.62));
  const vols = rows.map(r => r.vol).filter(v => Number.isFinite(v) && v > 0);
  const maxVol = vols.length ? Math.max(...vols) : 1;
  const volY = v => volBottom - (v / maxVol) * volPanelH;

  for (let i = 0; i <= 4; i += 1) {
    const gy = pad.top + priceH * i / 4;
    svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: gy, y2: gy, class: "grid-line" }));
  }
  svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: pad.top + priceH, y2: pad.top + priceH, class: "axis" }));
  svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: volBottom, y2: volBottom, class: "axis" }));

  // 横盘箱体阴影：箱体上沿成形月 → 突破月、上沿以下区域。
  const startIdx = findMonthIndex(rows, meta && meta.base_start_month);
  const breakIdx = findMonthIndex(rows, meta && meta.breakout_month);
  if (Number.isFinite(pivot) && startIdx >= 0) {
    const bx = x(startIdx);
    const bxEnd = breakIdx >= 0 ? x(breakIdx) : x(rows.length - 1);
    const boxTop = y(pivot);
    const boxBottom = pad.top + priceH;
    svg.appendChild(svgEl("rect", {
      x: bx.toFixed(2), y: boxTop.toFixed(2),
      width: Math.max(1, bxEnd - bx).toFixed(2),
      height: Math.max(1, boxBottom - boxTop).toFixed(2),
      fill: "rgba(245,158,11,0.14)", stroke: "none"
    }));
  }

  const tooltip = CK.tooltip(card);
  rows.forEach((row, idx) => {
    const px = x(idx);
    const up = row.close >= row.open;
    const cls = up ? "kline-candle-up" : "kline-candle-down";
    if (Number.isFinite(row.vol) && row.vol > 0) {
      const barTop = volY(row.vol);
      svg.appendChild(svgEl("rect", {
        x: (px - candleWidth / 2).toFixed(2), y: barTop.toFixed(2),
        width: candleWidth.toFixed(2), height: Math.max(1, volBottom - barTop).toFixed(2),
        class: `amount-bar ${up ? "amount-bar-up" : "amount-bar-down"}`, rx: 1
      }));
    }
    const bodyTop = y(Math.max(row.open, row.close));
    const bodyBottom = y(Math.min(row.open, row.close));
    svg.appendChild(svgEl("line", {
      x1: px.toFixed(2), x2: px.toFixed(2),
      y1: y(row.high).toFixed(2), y2: y(row.low).toFixed(2),
      class: `kline-wick ${cls}`
    }));
    svg.appendChild(svgEl("rect", {
      x: (px - candleWidth / 2).toFixed(2), y: bodyTop.toFixed(2),
      width: candleWidth.toFixed(2), height: Math.max(1, bodyBottom - bodyTop).toFixed(2),
      class: cls, rx: 1, opacity: 0.88
    }));
    const hit = svgEl("rect", {
      x: (px - Math.max(candleWidth, 4) / 2).toFixed(2), y: pad.top,
      width: Math.max(candleWidth, 4).toFixed(2), height: volBottom - pad.top,
      fill: "transparent", stroke: "none", style: "cursor:pointer"
    });
    hit.addEventListener("mouseenter", () => {
      tooltip.innerHTML = [
        `<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${formatMonth(row.trade_date)}</div>`,
        `<div>开: ${formatNumber(row.open)} 高: ${formatNumber(row.high)}</div>`,
        `<div>低: ${formatNumber(row.low)} 收: ${formatNumber(row.close)}</div>`
      ].join("");
      tooltip.style.opacity = "1";
    });
    hit.addEventListener("mousemove", event => CK.moveTip(tooltip, card, event));
    hit.addEventListener("mouseleave", () => { tooltip.style.opacity = "0"; });
    svg.appendChild(hit);
  });

  if (Number.isFinite(pivot)) {
    const py = y(pivot);
    svg.appendChild(svgEl("line", {
      x1: pad.left, x2: width - pad.right, y1: py.toFixed(2), y2: py.toFixed(2),
      stroke: "var(--orange)", "stroke-width": 1.3, "stroke-dasharray": "5 3", opacity: 0.95
    }));
    svg.appendChild(svgText(width - pad.right - 2, py - 4, `上沿 ${pivot.toFixed(2)}`, "end", "var(--orange)"));
  }

  if (breakIdx >= 0) {
    const bxp = x(breakIdx);
    svg.appendChild(svgEl("line", {
      x1: bxp.toFixed(2), x2: bxp.toFixed(2), y1: pad.top, y2: pad.top + priceH,
      stroke: "var(--neg)", "stroke-width": 1, "stroke-dasharray": "2 3", opacity: 0.6
    }));
    svg.appendChild(svgText(bxp, pad.top + 10, "突破", "middle", "var(--neg)"));
  }

  svg.appendChild(svgText(4, pad.top + 4, formatNumber(max), "start", "var(--text-tertiary)"));
  svg.appendChild(svgText(4, pad.top + priceH, formatNumber(min), "start", "var(--text-tertiary)"));
  svg.appendChild(svgText(4, volTop + 12, "成交量", "start", "var(--text-tertiary)"));
  return svg;
}

function findMonthIndex(rows, monthLabel) {
  if (!monthLabel) return -1;
  const target = String(monthLabel).replace(/[^0-9]/g, "").slice(0, 6);
  if (target.length < 6) return -1;
  for (let i = 0; i < rows.length; i += 1) {
    const ym = String(rows[i].trade_date || "").replace(/[^0-9]/g, "").slice(0, 6);
    if (ym === target) return i;
  }
  for (let i = 0; i < rows.length; i += 1) {
    const ym = String(rows[i].trade_date || "").replace(/[^0-9]/g, "").slice(0, 6);
    if (ym >= target) return i;
  }
  return -1;
}

function formatMonth(value) {
  const digits = String(value || "").replace(/[^0-9]/g, "");
  if (digits.length >= 6) return `${digits.slice(0, 4)}-${digits.slice(4, 6)}`;
  return formatDate(value);
}

/* Returns the names plus enough context to tell "this group had no candidates"
   (a table full of nothing, or the 暂无命中 paragraph the report writes
   instead of a table) apart from "this table is malformed" — the first is
   normal on a quiet day, the second is a defect the gate must surface. */
function readStockTable(tableWrap) {
  if (!tableWrap) return { names: [], rows: 0, hasStockColumn: false, present: false };
  const headers = Array.from(tableWrap.querySelectorAll("thead th")).map(c => normalizeStockName(c.textContent));
  const stockIndex = headers.indexOf("股票");
  const rows = tableWrap.querySelectorAll("tbody tr").length;
  if (stockIndex < 0) return { names: [], rows: rows, hasStockColumn: false, present: true };
  const names = Array.from(tableWrap.querySelectorAll("tbody tr"))
    .map(row => row.children[stockIndex] ? normalizeStockName(row.children[stockIndex].textContent) : "")
    .filter(Boolean);
  return { names: names, rows: rows, hasStockColumn: true, present: true };
}

function findStockData(name) {
  const normalized = normalizeStockName(name);
  const tsCode = stockNameIndex[name] || stockNameIndex[normalized];
  if (!tsCode) return null;
  return stockByCode[tsCode] || null;
}

function normalizeStockName(value) {
  return String(value || "")
    .replace(/\s+/g, "")
    .replace(/[（(]\d{6}\.(?:SH|SZ|BJ)[)）]/g, "")
    .trim();
}

function normalizeRows(records) {
  return (Array.isArray(records) ? records : [])
    .map(row => ({
      trade_date: String(row.trade_date || ""),
      open: toNumber(row.open),
      high: toNumber(row.high),
      low: toNumber(row.low),
      close: toNumber(row.close),
      pct_chg: toNumber(row.pct_chg),
      amount: toNumber(row.amount),
      vol: toNumber(row.vol)
    }))
    .filter(row => row.trade_date && [row.open, row.high, row.low, row.close].every(Number.isFinite))
    .sort((a, b) => a.trade_date.localeCompare(b.trade_date));
}

function buildKlineCard(rows, indexData, fallbackTitle) {
  const card = document.createElement("article");
  card.className = "kline-card";
  card.style.position = "relative";

  const title = document.createElement("div");
  title.className = "chart-title";
  title.textContent = `${indexData.name || fallbackTitle} ${klineDisplayDays}日K线`;
  const subtitle = document.createElement("div");
  subtitle.className = "chart-subtitle";
  const first = rows[0];
  const last = rows[rows.length - 1];
  const requested = Number(indexData.kline_days_requested) || klineDisplayDays;
  subtitle.textContent = `${formatDate(first.trade_date)} 至 ${formatDate(last.trade_date)} · ${rows.length}/${requested} 个交易日`;
  card.appendChild(title);
  card.appendChild(subtitle);
  card.appendChild(drawKline(rows, card));

  card.appendChild(CK.legend([
    ["K线", "var(--neg)"],
    ["MA20", "var(--blue)"],
    ["MA60", "var(--orange)"],
    ["成交金额", "rgba(100,116,139,0.55)"]
  ]));
  return card;
}

function drawKline(rows, card) {
  const enriched = rows.map((row, idx) => ({
    ...row, idx,
    ma20: rollingAverage(rows, idx, 20),
    ma60: rollingAverage(rows, idx, 60)
  }));
  const width = 560;
  const height = 340;
  const pad = { left: 48, right: 14, top: 12, bottom: 24 };
  const usableW = width - pad.left - pad.right;
  const amountPanelH = 64;
  const panelGap = 16;
  const priceH = height - pad.top - pad.bottom - amountPanelH - panelGap;
  const amountTop = pad.top + priceH + panelGap;
  const amountBottom = amountTop + amountPanelH;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });

  const allPrices = enriched.flatMap(row => [row.high, row.low, row.ma20, row.ma60]).filter(Number.isFinite);
  let min = Math.min(...allPrices);
  let max = Math.max(...allPrices);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.05;
  max += span * 0.05;

  const x = idx => pad.left + (enriched.length <= 1 ? usableW / 2 : idx / (enriched.length - 1) * usableW);
  const y = price => pad.top + (max - price) / (max - min) * priceH;
  const candleWidth = Math.max(2, Math.min(8, usableW / Math.max(enriched.length, 1) * 0.62));
  const amounts = enriched.map(row => row.amount).filter(value => Number.isFinite(value) && value > 0);
  const maxAmount = amounts.length ? Math.max(...amounts) : 1;
  const amountY = value => amountBottom - (value / maxAmount) * amountPanelH;

  for (let i = 0; i <= 4; i += 1) {
    const gy = pad.top + priceH * i / 4;
    svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: gy, y2: gy, class: "grid-line" }));
  }
  svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: pad.top + priceH, y2: pad.top + priceH, class: "axis" }));
  svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: amountBottom, y2: amountBottom, class: "axis" }));
  svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: amountTop, y2: amountTop, class: "grid-line", opacity: 0.55 }));

  const tooltip = CK.tooltip(card);

  enriched.forEach(row => {
    const px = x(row.idx);
    const up = row.close >= row.open;
    const cls = up ? "kline-candle-up" : "kline-candle-down";
    if (Number.isFinite(row.amount) && row.amount > 0) {
      const barTop = amountY(row.amount);
      const amountCls = up ? "amount-bar-up" : "amount-bar-down";
      svg.appendChild(svgEl("rect", {
        x: (px - candleWidth / 2).toFixed(2),
        y: barTop.toFixed(2),
        width: candleWidth.toFixed(2),
        height: Math.max(1, amountBottom - barTop).toFixed(2),
        class: `amount-bar ${amountCls}`,
        rx: 1
      }));
    }
    const bodyTop = y(Math.max(row.open, row.close));
    const bodyBottom = y(Math.min(row.open, row.close));
    const bodyHeight = Math.max(1, bodyBottom - bodyTop);
    svg.appendChild(svgEl("line", {
      x1: px.toFixed(2), x2: px.toFixed(2),
      y1: y(row.high).toFixed(2), y2: y(row.low).toFixed(2),
      class: `kline-wick ${cls}`
    }));
    svg.appendChild(svgEl("rect", {
      x: (px - candleWidth / 2).toFixed(2),
      y: bodyTop.toFixed(2),
      width: candleWidth.toFixed(2),
      height: bodyHeight.toFixed(2),
      class: cls, rx: 1, opacity: 0.88
    }));
    const hit = svgEl("rect", {
      x: (px - Math.max(candleWidth, 4) / 2).toFixed(2),
      y: pad.top,
      width: Math.max(candleWidth, 4).toFixed(2),
      height: amountBottom - pad.top,
      fill: "transparent", stroke: "none",
      style: "cursor:pointer"
    });
    hit.addEventListener("mouseenter", () => {
      tooltip.innerHTML = [
        `<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${formatDate(row.trade_date)}</div>`,
        `<div>开: ${formatNumber(row.open)} 高: ${formatNumber(row.high)}</div>`,
        `<div>低: ${formatNumber(row.low)} 收: ${formatNumber(row.close)}</div>`,
        `<div>涨跌幅: ${formatPercent(row.pct_chg)} · 成交额: ${formatAmount(row.amount)}</div>`
      ].join("");
      tooltip.style.opacity = "1";
    });
    hit.addEventListener("mousemove", event => CK.moveTip(tooltip, card, event));
    hit.addEventListener("mouseleave", () => { tooltip.style.opacity = "0"; });
    svg.appendChild(hit);
  });

  drawMaLine(enriched, "ma20", "var(--blue)");
  drawMaLine(enriched, "ma60", "var(--orange)");

  svg.appendChild(svgText(4, pad.top + 4, formatNumber(max), "start", "var(--text-tertiary)"));
  svg.appendChild(svgText(4, pad.top + priceH, formatNumber(min), "start", "var(--text-tertiary)"));
  svg.appendChild(svgText(4, amountTop + 12, "成交金额", "start", "var(--text-tertiary)"));
  svg.appendChild(svgText(4, amountTop + 28, formatAmount(maxAmount), "start", "var(--text-tertiary)"));
  return svg;

  function drawMaLine(items, key, color) {
    const points = items.filter(row => Number.isFinite(row[key]));
    if (!points.length) return;
    const d = points.map((row, idx) => `${idx === 0 ? "M" : "L"} ${x(row.idx).toFixed(2)} ${y(row[key]).toFixed(2)}`).join(" ");
    svg.appendChild(svgEl("path", { d, class: "ma-line", style: `stroke: ${color}` }));
  }
}

function rollingAverage(rows, idx, windowSize) {
  if (idx + 1 < windowSize) return null;
  const slice = rows.slice(idx - windowSize + 1, idx + 1).map(row => row.close).filter(Number.isFinite);
  if (slice.length !== windowSize) return null;
  return slice.reduce((sum, value) => sum + value, 0) / windowSize;
}
function toNumber(value) { const n = Number(value); return Number.isFinite(n) ? n : null; }
function formatNumber(value) {
  if (!Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const digits = abs >= 1000 ? 1 : abs >= 100 ? 2 : 3;
  return value.toFixed(digits);
}
function formatAmount(value) {
  if (!Number.isFinite(value)) return "—";
  return `${(value / 100000).toFixed(2)}亿`;
}
"""


# --------------------------------------------------------------------------- #
# Market-style normalized comparison charts driven by evidence market_style.series.
# --------------------------------------------------------------------------- #
STYLE_COMPARE_JS = r"""
const payload = __payload || {};
const reportBody = document.getElementById("report-body");
if (!reportBody) return;

const styleItems = Array.isArray(payload.indices) ? payload.indices : [];
if (!styleItems.length) return;

const { svgEl, svgText } = CK;
const formatDate = CK.fmt.date;
const displayDays = Number(payload.display_days) || 60;
const byKey = {};
styleItems.forEach(item => { byKey[item.key] = item; });

const chartDefs = [
  {
    title: "规模轴 · 归一化走势（60 日，起点=100）",
    keys: ["mega_cap", "csi300", "csi500", "csi1000", "guozheng2000"],
    footnoteKey: "guozheng2000",
    styles: {
      csi300: { color: "var(--blue)", width: 2.8, dash: "" },
      guozheng2000: { color: "var(--neg)", width: 2.1, dash: "" },
      csi500: { color: "var(--pos)", width: 2.0, dash: "9 5" },
      csi1000: { color: "var(--orange)", width: 2.0, dash: "4 4" },
      mega_cap: { color: "var(--ink-3)", width: 2.0, dash: "1 4" }
    }
  },
  {
    title: "成长 / 价值 / 红利 · 归一化走势（60 日，起点=100）",
    keys: ["csi300_growth", "csi300_value", "csi_dividend"],
    styles: {
      csi300_growth: { color: "var(--violet)", width: 2.8, dash: "" },
      csi300_value: { color: "var(--ink-3)", width: 2.0, dash: "9 5" },
      csi_dividend: { color: "var(--orange)", width: 2.0, dash: "1 4" }
    }
  }
];

const STYLE_HOOK = "style-compare";
const styleUnmatched = [];
const charts = [];
chartDefs.forEach(def => {
  const chart = prepareChart(def);
  if (chart) charts.push(chart);
  else styleUnmatched.push({ name: def.title, reason: "no_payload" });
});

if (!charts.length) {
  window.__render.attest(STYLE_HOOK, {
    rendered: 0, matched: 0, expected: chartDefs.length, unmatched: styleUnmatched
  });
  return;
}

/* The style panel sits after the 市场风格 table. The old pseudo-heading
   fallback (<p><strong>1.3 市场风格</strong></p>) is gone: the section contract
   now requires a real heading, and a report that lost one fails at build time
   with a message naming the section instead of quietly relocating a chart. */
const anchorEl = window.__sec.find("market_style", ".table-wrap") || window.__sec.tail("market_style");
if (!anchorEl) {
  window.__render.fail("hook:" + STYLE_HOOK, "no insertion anchor inside section [market_style]");
  return;
}

const grid = document.createElement("div");
grid.className = "style-compare-grid";
charts.forEach(chart => grid.appendChild(buildChartCard(chart)));
anchorEl.after(grid);
window.__render.attest(STYLE_HOOK, {
  rendered: grid.childElementCount,
  matched: charts.length,
  expected: chartDefs.length,
  unmatched: styleUnmatched,
  el: grid
});

function prepareChart(def) {
  const sliced = def.keys.map(key => {
    const item = byKey[key];
    if (!item) return null;
    const rows = normalizeRecords(item.records).slice(-displayDays);
    return rows.length ? { item, key, rows } : null;
  }).filter(Boolean);
  if (sliced.length < 2) return null;
  /* rebase every line of a chart from the latest common start date so the
     lines stay comparable when one series is shorter than the window */
  const commonStart = sliced.reduce(
    (acc, entry) => (entry.rows[0].trade_date > acc ? entry.rows[0].trade_date : acc),
    sliced[0].rows[0].trade_date
  );
  const series = sliced.map(({ item, key, rows }) => {
    const trimmed = rows.filter(row => row.trade_date >= commonStart);
    if (trimmed.length < 2 || !Number.isFinite(trimmed[0].close) || trimmed[0].close === 0) return null;
    const base = trimmed[0].close;
    const points = trimmed.map(row => ({
      date: row.trade_date,
      value: round2(row.close / base * 100)
    })).filter(point => Number.isFinite(point.value));
    if (points.length < 2) return null;
    return {
      key,
      name: item.name || key,
      styleRole: item.style_role || "",
      proxyNote: item.proxy_note || "",
      style: def.styles[key] || { color: "var(--blue)", width: 2, dash: "" },
      points
    };
  }).filter(Boolean);
  if (series.length < 2) return null;
  const dates = Array.from(new Set(series.flatMap(item => item.points.map(point => point.date)))).sort();
  return { def, series, dates };
}

function normalizeRecords(records) {
  return (Array.isArray(records) ? records : [])
    .map(row => ({
      trade_date: String(row.trade_date || ""),
      close: Number(row.close)
    }))
    .filter(row => row.trade_date && Number.isFinite(row.close))
    .sort((a, b) => a.trade_date.localeCompare(b.trade_date));
}

function buildChartCard(chart) {
  const start = chart.dates[0];
  const end = chart.dates[chart.dates.length - 1];
  const card = CK.card(
    "chart-card style-compare-card",
    chart.def.title,
    `${formatDate(start)} 至 ${formatDate(end)} · 数据截至 ${formatDate(payload.trade_date || end)} · Baostock`
  );
  card.appendChild(drawNormalizedLines(chart, card));
  card.appendChild(buildLineLegend(chart.series));
  const footnote = chart.def.footnoteKey ? (byKey[chart.def.footnoteKey] || {}).proxy_note : "";
  if (footnote) {
    const note = document.createElement("div");
    note.className = "style-compare-note";
    note.textContent = footnote;
    card.appendChild(note);
  }
  return card;
}

function drawNormalizedLines(chart, card) {
  const width = 560;
  const height = 280;
  const pad = { left: 44, right: 18, top: 14, bottom: 30 };
  const usableW = width - pad.left - pad.right;
  const usableH = height - pad.top - pad.bottom;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });
  const allValues = chart.series.flatMap(item => item.points.map(point => point.value));
  let min = Math.min(...allValues, 100);
  let max = Math.max(...allValues, 100);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.04;
  max += span * 0.04;

  const dateIndex = new Map(chart.dates.map((date, idx) => [date, idx]));
  const x = date => pad.left + (chart.dates.length <= 1 ? usableW / 2 : dateIndex.get(date) / (chart.dates.length - 1) * usableW);
  const y = value => pad.top + (max - value) / (max - min) * usableH;

  for (let i = 0; i <= 4; i += 1) {
    const value = min + (max - min) * (4 - i) / 4;
    const gy = y(value);
    svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: gy, y2: gy, class: "grid-line" }));
    svg.appendChild(svgText(4, gy + 3, value.toFixed(1), "start", "var(--text-tertiary)"));
  }
  if (min <= 100 && max >= 100) {
    svg.appendChild(svgEl("line", {
      x1: pad.left, x2: width - pad.right, y1: y(100), y2: y(100),
      class: "axis", "stroke-width": 1.8, opacity: 0.85
    }));
  }
  svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: height - pad.bottom, y2: height - pad.bottom, class: "axis" }));

  const tickDates = pickTicks(chart.dates, 6);
  tickDates.forEach(date => {
    const px = x(date);
    svg.appendChild(svgEl("line", { x1: px, x2: px, y1: height - pad.bottom, y2: height - pad.bottom + 4, class: "axis" }));
    svg.appendChild(svgText(px, height - 10, formatDate(date).slice(5), "middle", "var(--text-tertiary)", 10));
  });

  chart.series.forEach(item => {
    const d = item.points.map((point, idx) => `${idx === 0 ? "M" : "L"} ${x(point.date).toFixed(2)} ${y(point.value).toFixed(2)}`).join(" ");
    svg.appendChild(svgEl("path", {
      d,
      fill: "none",
      stroke: item.style.color,
      "stroke-width": item.style.width,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "stroke-dasharray": item.style.dash || ""
    }));
  });

  const tooltip = CK.tooltip(card);
  const hitWidth = Math.max(6, usableW / Math.max(chart.dates.length, 1));
  chart.dates.forEach(date => {
    const px = x(date);
    const hit = svgEl("rect", {
      x: (px - hitWidth / 2).toFixed(2),
      y: pad.top,
      width: hitWidth.toFixed(2),
      height: usableH,
      fill: "transparent",
      stroke: "none",
      style: "cursor:pointer"
    });
    hit.addEventListener("mouseenter", () => {
      const rows = chart.series.map(item => {
        const point = item.points.find(p => p.date === date);
        return point ? `<div>${item.name}: <strong>${point.value.toFixed(1)}</strong></div>` : "";
      }).filter(Boolean);
      tooltip.innerHTML = [`<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${formatDate(date)}</div>`].concat(rows).join("");
      tooltip.style.opacity = rows.length ? "1" : "0";
    });
    hit.addEventListener("mousemove", event => CK.moveTip(tooltip, card, event));
    hit.addEventListener("mouseleave", () => { tooltip.style.opacity = "0"; });
    svg.appendChild(hit);
  });
  return svg;
}

function buildLineLegend(series) {
  const legend = document.createElement("div");
  legend.className = "style-compare-legend";
  series.forEach(item => {
    const entry = document.createElement("span");
    const icon = svgEl("svg", { viewBox: "0 0 24 8", "aria-hidden": "true" });
    icon.appendChild(svgEl("line", {
      x1: 1, x2: 23, y1: 4, y2: 4,
      stroke: item.style.color,
      "stroke-width": item.style.width,
      "stroke-linecap": "round",
      "stroke-dasharray": item.style.dash || ""
    }));
    entry.appendChild(icon);
    entry.appendChild(document.createTextNode(item.name));
    legend.appendChild(entry);
  });
  return legend;
}

function pickTicks(dates, count) {
  if (dates.length <= count) return dates;
  const out = [];
  const last = dates.length - 1;
  for (let i = 0; i < count; i += 1) {
    out.push(dates[Math.round(i * last / (count - 1))]);
  }
  return Array.from(new Set(out));
}

function round2(value) { return Math.round(value * 100) / 100; }
"""


# --------------------------------------------------------------------------- #
# Market-trend mini-chart panel driven by market_data.json.
# --------------------------------------------------------------------------- #
MARKET_TRENDS_JS = r"""
const HOOK = "market-trends";
const data = __payload || {};
const records = Array.isArray(data.records) ? data.records.filter(r => r && r.trade_date).slice(-90) : [];
if (!records.length) {
  window.__render.fail("hook:" + HOOK, "market_data carries no dated records — the trend panel cannot be drawn");
  return;
}

const reportBody = document.getElementById("report-body");
const { svgEl, svgText } = CK;
const formatDate = CK.fmt.date;

const chartSection = document.createElement("div");
chartSection.className = "chart-grid";
chartSection.style.marginTop = "18px";

const charts = [
  { title: "成交额趋势", fields: [{ key: "成交额", color: "var(--blue)", scale: 1e9, unit: "万亿" }] },
  { title: "活跃度 / 情绪值", fields: [{ key: "活跃度", color: "var(--orange)" }, { key: "情绪值", color: "var(--purple)" }] },
  { title: "融资净买入", type: "bar", fields: [{ key: "融资净买入", color: "var(--green)", scale: 1e8, unit: "亿" }] },
  { title: "上涨 vs 下跌家数", fields: [{ key: "上涨", color: "var(--red)" }, { key: "下跌", color: "var(--green)" }] },
  { title: "涨停 vs 跌停家数", fields: [{ key: "涨停", color: "var(--red)" }, { key: "跌停", color: "var(--green)" }] },
];

charts.forEach(config => {
  const card = document.createElement("article");
  card.className = "chart-card";
  const title = document.createElement("div");
  title.className = "chart-title";
  title.textContent = config.title;
  const subtitle = document.createElement("div");
  subtitle.className = "chart-subtitle";
  card.appendChild(title);
  const drawable = drawChart(records, config, card, subtitle);
  card.appendChild(drawable);
  card.appendChild(CK.legend(config.fields.map(field => [field.key, field.color])));
  chartSection.appendChild(card);
});

/* The panel belongs at the end of the sentiment-trend section. There is
   deliberately no "append to the document instead" fallback: that fallback is
   what silently moved five charts to the bottom of the page the day 情绪趋势
   was renumbered from 1.1 to 1.2. */
const insertAfter = window.__sec.tail("sentiment_trend");
if (!insertAfter) {
  window.__render.fail("hook:" + HOOK, "section [sentiment_trend] not found — refusing to relocate the trend panel");
  return;
}
insertAfter.after(chartSection);
window.__render.attest(HOOK, {
  rendered: chartSection.childElementCount,
  matched: charts.length,
  expected: charts.length,
  unmatched: [],
  el: chartSection
});

function drawChart(rows, config, card, subtitle) {
  const width = 480;
  const height = 200;
  const pad = { left: 42, right: 16, top: 14, bottom: 28 };
  const usableW = width - pad.left - pad.right;
  const usableH = height - pad.top - pad.bottom;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });
  const series = config.fields.map(field => {
    const points = rows.map((row, idx) => {
      const raw = row[field.key];
      const value = typeof raw === "number" ? raw / (field.scale || 1) : null;
      return { idx, date: row.trade_date, value };
    }).filter(p => Number.isFinite(p.value));
    return { field, points };
  }).filter(item => item.points.length);
  if (!series.length) {
    svg.appendChild(svgText(width / 2, height / 2, "暂无数据", "middle", "var(--text-tertiary)"));
    subtitle.textContent = "该列暂无可绘制数值";
    return svg;
  }
  const allValues = series.flatMap(item => item.points.map(p => p.value));
  let min = Math.min(...allValues);
  let max = Math.max(...allValues);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.08;
  max += span * 0.08;
  const x = idx => pad.left + (rows.length <= 1 ? usableW / 2 : idx / (rows.length - 1) * usableW);
  const y = value => pad.top + (max - value) / (max - min) * usableH;
  for (let i = 0; i <= 4; i += 1) {
    const gy = pad.top + usableH * i / 4;
    svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: gy, y2: gy, class: "grid-line" }));
  }
  svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: height - pad.bottom, y2: height - pad.bottom, class: "axis" }));

  const tooltip = CK.tooltip(card);

  const isBar = config.type === "bar";
  const barWidth = isBar ? Math.max(2, usableW / rows.length * 0.55) : 0;
  const y0 = y(0);

  series.forEach(item => {
    if (isBar) {
      item.points.forEach(p => {
        const px = x(p.idx);
        const py = y(p.value);
        const barH = Math.abs(py - y0);
        const barY = p.value >= 0 ? py : y0;
        const barColor = p.value >= 0 ? "var(--red)" : "var(--green)";
        const rect = svgEl("rect", {
          x: (px - barWidth / 2).toFixed(2),
          y: barY.toFixed(2),
          width: barWidth.toFixed(2),
          height: Math.max(1, barH).toFixed(2),
          fill: barColor, rx: 2, opacity: 0.85
        });
        svg.appendChild(rect);
        const hit = svgEl("rect", {
          x: (px - barWidth / 2).toFixed(2),
          y: Math.min(py, y0).toFixed(2),
          width: barWidth.toFixed(2),
          height: Math.max(1, barH).toFixed(2),
          fill: "transparent", stroke: "none",
          style: "cursor:pointer"
        });
        hit.addEventListener("mouseenter", () => {
          rect.setAttribute("opacity", "1");
          tooltip.innerHTML = `<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${formatDate(p.date)}</div><div style="font-weight:600;">${item.field.key}: ${formatValue(p.value, item.field.unit)}</div>`;
          tooltip.style.opacity = "1";
        });
        hit.addEventListener("mousemove", e => CK.moveTip(tooltip, card, e));
        hit.addEventListener("mouseleave", () => {
          rect.setAttribute("opacity", "0.85");
          tooltip.style.opacity = "0";
        });
        svg.appendChild(hit);
      });
    } else {
      const d = item.points.map((p, i) => `${i === 0 ? "M" : "L"} ${x(p.idx).toFixed(2)} ${y(p.value).toFixed(2)}`).join(" ");
      svg.appendChild(svgEl("path", { d, class: "series-line", style: `stroke: ${item.field.color}` }));
      item.points.forEach(p => {
        svg.appendChild(svgEl("circle", {
          cx: x(p.idx).toFixed(2), cy: y(p.value).toFixed(2),
          r: 3.5, fill: item.field.color, stroke: "#ffffff", "stroke-width": 1.5
        }));
        const hit = svgEl("circle", {
          cx: x(p.idx).toFixed(2), cy: y(p.value).toFixed(2),
          r: 10, fill: "transparent", stroke: "none",
          style: "cursor:pointer"
        });
        hit.addEventListener("mouseenter", () => {
          tooltip.innerHTML = `<div style="color:#94a3b8;font-size:11px;margin-bottom:2px;">${formatDate(p.date)}</div><div style="font-weight:600;">${item.field.key}: ${formatValue(p.value, item.field.unit)}</div>`;
          tooltip.style.opacity = "1";
        });
        hit.addEventListener("mousemove", e => CK.moveTip(tooltip, card, e));
        hit.addEventListener("mouseleave", () => { tooltip.style.opacity = "0"; });
        svg.appendChild(hit);
      });
    }
  });
  svg.appendChild(svgText(4, pad.top + 4, formatValue(max, config.fields[0].unit), "start", "var(--text-tertiary)"));
  svg.appendChild(svgText(4, height - pad.bottom, formatValue(min, config.fields[0].unit), "start", "var(--text-tertiary)"));
  return svg;
}

function formatValue(value, unit) {
  if (!Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const digits = abs >= 100 ? 0 : abs >= 10 ? 1 : 2;
  return `${value.toFixed(digits)}${unit || ""}`;
}
"""


# --------------------------------------------------------------------------- #
# Thin manifest: declare inputs, decorations and charts; shared runner does
# the rest (parse args, render, validate, write, print summary).
# --------------------------------------------------------------------------- #
def add_arguments(parser) -> None:
    parser.add_argument("--market-data", default=str(DEFAULT_MARKET_DATA), help="Derived market_data.json path.")
    parser.add_argument(
        "--evidence",
        default=None,
        help="Evidence JSON path. Defaults to sibling evidence_YYYYMMDD_utf8.json when input is report_YYYYMMDD.md.",
    )
    parser.add_argument(
        "--stock-klines",
        default=None,
        help="Stock kline JSON path. Defaults to sibling kline_YYYYMMDD.json; falls back to evidence stock_kline_records.",
    )
    parser.add_argument(
        "--lifecycle-days",
        type=int,
        default=22,
        help="主线生命周期区块的交易日窗口宽度。",
    )
    parser.add_argument(
        "--no-lifecycle",
        action="store_true",
        help="不注入主线生命周期区块（默认在 theme_daily_state 有数据时自动注入）。",
    )


def load_lifecycle_payload(input_path: Path, days: int) -> Optional[Dict[str, Any]]:
    """Trailing lifecycle window for the report date, or None.

    Availability is optional display-layer data: no date in the filename, DB
    unreachable or an empty ledger all skip the block without failing the
    render.  *Currency* is not optional — a ledger that has the window but
    nothing for the report date would draw a band with a blank last column,
    so that case is a hard failure pointing at the skipped record step.
    """
    m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", input_path.name)
    if not m:
        return None
    asof = "-".join(m.groups())
    try:
        from theme_lifecycle import build_window_payload, get_connection

        with get_connection() as conn:
            payload = build_window_payload(conn, asof, days)
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        print(f"[theme-lifecycle] 跳过生命周期区块：{exc}", file=sys.stderr)
        return None
    if not payload.get("themes"):
        return None
    from theme_lifecycle import lifecycle_currency_gap

    gap = lifecycle_currency_gap(payload, asof)
    if gap:
        raise ContractError(
            f"主线生命周期台账没有当期数据：{gap}。\n"
            "  泳道图会照画，但最后一列是空的——先补执行流第 6 步（写 "
            f"reports/lifecycle_{asof.replace('-', '')}.json 后 "
            "`python3 scripts/theme_lifecycle.py record --input <该文件>`），再重跑渲染。\n"
            "  历史回补等确实不需要台账的场景，用 --no-lifecycle 明确跳过。"
        )
    return payload


def build_job(args) -> RenderJob:
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_suffix(".html")
    market_data_path = Path(args.market_data)
    evidence_path = Path(args.evidence) if args.evidence else default_evidence_path(input_path)
    kline_path = Path(args.stock_klines) if args.stock_klines else default_kline_path(input_path)
    markdown_text = input_path.read_text(encoding="utf-8")
    title = args.title or input_path.stem
    market_data = market_data_for_report(
        load_market_data(market_data_path),
        report_trade_date(input_path),
        market_data_path,
    )
    evidence = load_evidence(evidence_path)
    content_contract_audit = validate_dms_content(markdown_text, evidence, DMS_CONTRACT)
    index_kline_data = extract_index_kline_payload(evidence, evidence_path)
    stock_klines_raw = load_stock_klines(evidence, kline_path)
    stock_kline_source = kline_path if kline_path is not None and kline_path.exists() else evidence_path
    stock_kline_data = extract_stock_kline_payload(
        stock_klines_raw,
        stock_kline_source,
        missing=bool((evidence.get("metadata") or {}).get("missing")) and not stock_klines_raw,
    )
    style_series_payload = extract_style_series_payload(evidence)
    state_timeline_payload = extract_state_timeline_payload(evidence)

    builder = HtmlReportBuilder(
        title=title,
        theme=args.theme,
        extra_css=MARKET_SENSE_EXTRA_CSS,
        contract=DMS_CONTRACT,
        contract_audit=content_contract_audit,
    )
    builder.add_decoration(PillDecoration(MARKET_SENSE_PILL_RULES))
    builder.add_ui_decoration(TREND_STATE_CARD_JS)
    builder.add_decoration(HeroDecoration(
        heading_prefix="一句话盘面判断",
        collect_tags=("P",),
        max_blocks=3,
        stop_at_numbered=True,
        number_units="%|pct|倍",
        keyword_pattern=HERO_KEYWORDS,
        stop_mode="any_heading",
    ))
    builder.add_chart_hook(
        ChartHook(
            name="klines",
            payload={
                "index": index_kline_data,
                "stocks": stock_kline_data,
                "stocks_monthly": stock_kline_data.get("monthly") or {},
                "display_days": INDEX_KLINE_DISPLAY_DAYS,
                "display_months": MONTHLY_KLINE_DISPLAY_MONTHS,
            },
            js=KLINE_CHARTS_JS,
        ),
        # One K-line bundle draws into six sections; each insertion attests
        # separately, so a single broken section is named rather than hidden
        # behind a bundle-level "it ran".
        expects=[
            HookExpectation(name="klines.index", target_sec="index_trend", expect_count=3,
                            note="上证 / 创业板 / 科创50"),
            HookExpectation(name="klines.m3_leaders", target_sec="m3_leaders", expect_from="table_rows"),
            HookExpectation(name="klines.m5_capacity_up", target_sec="m5_capacity_up", expect_from="table_rows"),
            HookExpectation(name="klines.m5_monthly_base", target_sec="m5_monthly_base", expect_from="table_rows"),
            HookExpectation(name="klines.m5_early_limit", target_sec="m5_early_limit", expect_from="table_rows"),
            HookExpectation(name="klines.m5_discount_relaunch", target_sec="m5_discount_relaunch",
                            expect_from="table_rows"),
        ],
    )
    builder.add_chart_hook(
        ChartHook(name="market-trends", payload=market_data, js=MARKET_TRENDS_JS),
        # The count is declared here and produced there: the JS builds its own
        # chart list, so a drift between the two is a real signal, not noise.
        expects=[HookExpectation(name="market-trends", target_sec="sentiment_trend", expect_count=5,
                                 note="成交额 / 活跃度 / 融资净买入 / 涨跌家数 / 涨跌停家数")],
    )
    if style_series_payload:
        builder.add_chart_hook(
            ChartHook(name="style-compare", payload=style_series_payload, js=STYLE_COMPARE_JS),
            expects=[HookExpectation(name="style-compare", target_sec="market_style", expect_min=1,
                                     note="规模轴 + 成长/价值/红利，任一轴缺数据记 no_payload")],
        )
    if state_timeline_payload:
        builder.add_chart_hook(
            # 时间轴挂在状态卡里面，所以要排在 TREND_STATE_CARD_JS 之后执行；
            # chart hook 本来就在 ui decoration 之后跑，顺序天然成立。
            ChartHook(name="state-timeline", payload=state_timeline_payload, js=STATE_TIMELINE_JS),
            expects=[HookExpectation(name="state-timeline", target_sec="sentiment_trend", expect_count=1,
                                     note="20 日趋势档色带 + 出清分 / 顶部分")],
        )

    lifecycle_payload = None if args.no_lifecycle else load_lifecycle_payload(input_path, args.lifecycle_days)
    if lifecycle_payload:
        from theme_lifecycle import HOOK_NAME, LIFECYCLE_JS_BODY

        builder.add_chart_hook(
            ChartHook(name=HOOK_NAME, payload=lifecycle_payload, js=LIFECYCLE_JS_BODY),
            expects=[HookExpectation(name="theme-lifecycle", target_sec="m3_mainline", expect_count=1)],
        )

    return RenderJob(
        markdown_text=markdown_text,
        builder=builder,
        output_path=output_path,
        summary={
            "market_data": str(market_data_path),
            "evidence": str(evidence_path) if evidence_path is not None else None,
            "index_kline_records": {
                key: len((value or {}).get("records") or [])
                for key, value in (index_kline_data.get("indices") or {}).items()
            },
            "stock_kline_records": len(stock_kline_data.get("by_ts_code") or {}),
            "state_timeline_days": len((state_timeline_payload or {}).get("states") or []),
            "style_series_records": {
                item["key"]: len(item.get("records") or [])
                for item in (style_series_payload or {}).get("indices", [])
            },
            "records_available": (market_data.get("quality") or {}).get("records_available", 0),
            "market_data_window_end": (market_data.get("metadata") or {}).get("window_end"),
            "theme_lifecycle_themes": len(lifecycle_payload["themes"]) if lifecycle_payload else 0,
            "content_contract": content_contract_audit,
        },
    )


if __name__ == "__main__":
    raise SystemExit(
        render_report(
            description="Render a Markdown daily market report to static HTML.",
            build_job=build_job,
            add_arguments=add_arguments,
        )
    )
