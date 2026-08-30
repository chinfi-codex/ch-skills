#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把证据包渲染成单页数据中心信用监控 Dashboard（自包含 HTML，无外部依赖）。

一屏之内回答：**这个体系的融资成本在往哪走、梯级有没有在裂开、抵押品还值不值。**
不设发行人 selector、不设时间范围切换，窗口固定 90 天。

脑 / 手边界在这里体现得最清楚：

  * 判断（整体结论、各档状态、面板异动说明）由模型写在报告 Markdown 的
    frontmatter `verdict:` 块里，脚本原样搬运，一个字不改也不生成。
  * 其余全部由脚本从 evidence 里确定性地摆出来：梯级、曲线、跨档距离、
    归因分解、转债位置、SPV 租户锚、事件、源健康度。

所以这个脚本不认识「分层」是什么意思，它只负责把模型的判断和机器的证据放进
同一屏，让读者能当场对账。

三条渲染纪律：

1. **缺失一律显式。** 缺数写「暂无数据」、样本不足写「样本不足」、
   该体制没有这个指标写「不适用」，绝不用前值冒充最新值，也不用 0 冒充空。
2. **disclosure_once 的记录拒绝画成折线。** Beignet 只披露过一次，
   画成时间序列就是造假——这条有单独的守卫函数，不靠自觉。
3. **利差用对数轴。** 梯级从 40bp 到 780bp 跨了一个数量级，线性轴会把
   前六档压成一条线，读者只看得见 CRWV。

用法：
    python scripts/render_report_html.py --evidence evidence/dc-2026-08-26.json
    python scripts/render_report_html.py --evidence … --input reports/dc-2026-08-26.md
    python scripts/render_report_html.py --evidence … --output docs/index.html
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional

import yaml

SCRIPT_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_ROOT.parent
_BUNDLED_SHARED = SCRIPT_ROOT / "_shared"
_DEV_SHARED = SCRIPT_ROOT.parents[1] / "shared"
SHARED_ROOT = _BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED
THEME_CSS = SHARED_ROOT / "html_report" / "themes" / "claude.css"

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", re.DOTALL)

PANEL_KEYS = ("ladder", "curves", "gaps", "structure")
PANEL_NOTE_MAX = 100

# 状态语义。颜色只是辅助，文字始终在场。
TONES = {
    "widening": ("走宽", "t-wide"),
    "watch": ("观察", "t-watch"),
    "tightening": ("收窄", "t-tight"),
    "unknown": ("数据不足", "t-unknown"),
}

# 各档配色：从冷到暖对应从安全到高风险，与梯级顺序一致。
RUNG_COLORS = {
    1: "#6e86a8", 2: "#5a8e89", 3: "#5c8a4e", 4: "#b9802f",
    5: "#cd8a45", 6: "#c0533b", 7: "#8c2f1f",
}

LAYOUT_CSS = """
body { font-size: 14.5px; line-height: 1.6; }
.wrap { width: min(1500px, calc(100vw - 40px)); margin: 0 auto 56px; padding-top: 26px; }
.hd { display: flex; justify-content: space-between; align-items: flex-end; gap: 18px;
      flex-wrap: wrap; margin-bottom: 16px; padding: 0 2px; }
.hd .eyebrow { font-size: 12px; color: var(--ink-4); letter-spacing: .02em; }
.hd h1 { font-family: var(--font-serif); font-size: 25px; font-weight: 700;
         margin: 6px 0 5px; letter-spacing: -.01em; color: var(--ink-1); }
.hd .sub { font-size: 13.5px; color: var(--ink-3); }
.hd .stamp { font-family: var(--font-mono); font-size: 12px; color: var(--ink-4);
             text-align: right; line-height: 1.7; }
.panel { background: var(--surface); border: 1px solid var(--line-1);
         border-radius: 12px; padding: 16px 18px; box-shadow: 0 1px 2px rgba(43,38,32,.04); }
.stack { margin-bottom: 14px; }
.sec-head { display: flex; justify-content: space-between; align-items: baseline;
            gap: 12px; flex-wrap: wrap; }
.sec-head strong { font-size: 14.5px; color: var(--ink-1); }
.sec-hint { font-size: 11.5px; color: var(--ink-4); font-family: var(--font-mono); }
.lead { font-size: 12.5px; color: var(--ink-3); margin-top: 6px; line-height: 1.6;
        max-width: 1080px; }
/* 判断面板 */
.verdict { display: flex; gap: 24px; align-items: flex-start; flex-wrap: wrap; }
.verdict .copy { flex: 1 1 340px; min-width: 300px; }
.verdict h2 { font-family: var(--font-serif); font-size: 20px; line-height: 1.35;
              margin: 9px 0 7px; color: var(--ink-1); }
.verdict .summary { font-size: 13.5px; color: var(--ink-2); max-width: 820px; }
.badge-row { display: flex; gap: 9px; align-items: center; flex-wrap: wrap; }
.badge-row .lbl { font-size: 12px; color: var(--ink-4); }
.badge { font-size: 12px; border: 1px solid currentColor; border-radius: 6px;
         padding: 2px 8px; font-weight: 500; }
.signals { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
           gap: 9px; flex: 0 1 460px; min-width: 300px; }
.sig { border: 1px solid var(--line-1); border-radius: 9px; padding: 11px 12px;
       background: var(--surface-2); }
.sig .name { font-size: 11.5px; color: var(--ink-4); letter-spacing: .03em; }
.sig .state { font-weight: 700; font-size: 15px; margin-top: 5px; }
.sig .why { font-size: 11.5px; color: var(--ink-3); margin-top: 7px; line-height: 1.5; }
.t-wide { color: var(--neg); } .t-watch { color: var(--warn); }
.t-tight { color: var(--pos); } .t-unknown { color: var(--ink-4); }
/* 表格 */
.scroll { overflow-x: auto; margin-top: 11px; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid var(--line-2);
         white-space: nowrap; }
th { color: var(--ink-4); font-weight: 500; font-size: 11.5px; }
tbody tr:last-child td { border-bottom: none; }
td.num { font-family: var(--font-mono); text-align: right; }
td.na, .na { color: var(--ink-4); }
.dim { color: var(--ink-4); font-size: 11.5px; }
.chip { display: inline-block; font-size: 10.5px; padding: 1px 6px; border-radius: 5px;
        border: 1px solid var(--line-1); color: var(--ink-4); background: var(--surface-2);
        margin-left: 5px; font-family: var(--font-mono); }
.chip.warn { color: var(--warn); border-color: var(--warn); }
.chip.bad { color: var(--neg); border-color: var(--neg); }
.pct-up { color: var(--neg); font-weight: 700; }
.pct-down { color: var(--pos); font-weight: 700; }
.pct-flat { color: var(--ink-4); font-weight: 600; }
/* 图 */
svg.chart { width: 100%; min-width: 560px; height: auto; display: block; }
svg.chart text { fill: var(--ink-4); font-size: 10.5px; font-family: var(--font-sans); }
.gridline { stroke: var(--line-2); stroke-width: 1; }
.empty { padding: 34px 16px; text-align: center; color: var(--ink-4); font-size: 12.5px;
         border: 1px dashed var(--line-1); border-radius: 9px; margin-top: 11px;
         line-height: 1.7; }
.footnote { font-size: 11.5px; color: var(--ink-4); margin-top: 9px; line-height: 1.6; }
.panel-note { margin-top: 11px; padding: 9px 12px; border-left: 3px solid var(--clay);
              background: var(--clay-soft); border-radius: 0 7px 7px 0;
              font-size: 12.5px; line-height: 1.65; color: var(--ink-2); }
.two-col { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.gap-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
            gap: 9px; margin-top: 12px; }
.gap-cell { border: 1px solid var(--line-1); border-radius: 9px; padding: 11px 12px;
            background: var(--surface-2); }
.gap-cell .name { font-size: 11.5px; color: var(--ink-4); }
.gap-cell .val { font-family: var(--font-mono); font-size: 19px; font-weight: 700;
                 margin-top: 5px; color: var(--ink-1); }
.gap-cell .drift { font-size: 11.5px; margin-top: 3px; }
.gap-cell .means { font-size: 11.5px; color: var(--ink-3); margin-top: 7px;
                   line-height: 1.5; }
.srcs { display: grid; grid-template-columns: repeat(auto-fit, minmax(178px, 1fr));
        gap: 9px; margin-top: 12px; }
.src { border: 1px solid var(--line-1); border-radius: 9px; padding: 10px 11px;
       background: var(--surface-2); }
.src b { font-size: 13px; }
.src .st { font-size: 11.5px; margin-top: 4px; }
.src .meta { font-size: 11px; color: var(--ink-4); margin-top: 3px;
             font-family: var(--font-mono); line-height: 1.5; white-space: normal; }
.ok { color: var(--pos); } .bad { color: var(--neg); } .warnc { color: var(--warn); }
.ev { border-left: 3px solid var(--line-1); padding: 7px 0 7px 12px; margin-top: 9px; }
.ev .crit { font-size: 11px; color: var(--ink-4); font-family: var(--font-mono); }
.ev .body { font-size: 12.5px; color: var(--ink-2); margin-top: 3px; line-height: 1.6; }
@media (max-width: 1080px) {
  .signals, .two-col { grid-template-columns: 1fr; }
  .wrap { width: calc(100vw - 24px); }
}
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def bp(value: Optional[float], digits: int = 0) -> str:
    return "—" if value is None else f"{value:,.{digits}f}bp"


def signed(value: Optional[float], digits: int = 1, unit: str = "bp") -> str:
    if value is None:
        return '<span class="na">—</span>'
    cls = "pct-up" if value > 0 else "pct-down" if value < 0 else "pct-flat"
    return f'<span class="{cls}">{value:+,.{digits}f}{unit}</span>'


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------
def read_report_meta(md_path: Optional[Path]) -> Dict[str, Any]:
    """读取报告 Markdown 的 frontmatter；缺失或格式不对时返回空。"""
    if md_path is None or not md_path.exists():
        return {}
    match = FRONTMATTER_RE.match(md_path.read_text(encoding="utf-8"))
    if not match:
        return {}
    return yaml.safe_load(match.group(1)) or {}


def read_verdict(md_path: Optional[Path]) -> Dict[str, Any]:
    """从报告 Markdown 的 frontmatter 里取模型写的判断块。

    取不到就返回空——仪表盘会把判断区显示成「未提供」，而不是自己编一个。
    """
    return read_report_meta(md_path).get("verdict") or {}


def iso_date(value: Any) -> Optional[str]:
    """把 YAML date 或 YYYY-MM-DD 字符串归一化；非法值不冒充日期。"""
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def resolve_report_date(meta: Dict[str, Any], override: Optional[str] = None) -> str:
    """报告日默认取本次执行的本地日历日，不从 evidence.asof 推导。"""
    if override:
        parsed = iso_date(override)
        if not parsed:
            raise SystemExit("error: --report-date 必须是 YYYY-MM-DD")
        return parsed
    return iso_date(meta.get("date")) or date.today().isoformat()


def find_evidence(evidence_arg: Optional[str], md_path: Optional[Path],
                  meta: Optional[Dict[str, Any]] = None) -> Path:
    if evidence_arg:
        return Path(evidence_arg)
    # 报告文件名现在按执行日命名，可能与数据截止日不同；优先用 frontmatter
    # 的 data_asof 找证据，不能再默认拿报告日期冒充观测日期。
    data_asof = iso_date((meta or {}).get("data_asof"))
    if data_asof:
        guess = SKILL_ROOT / "evidence" / f"dc-{data_asof}.json"
        if guess.exists():
            return guess
    if md_path is not None:
        found = DATE_RE.search(md_path.name)
        if found:
            guess = SKILL_ROOT / "evidence" / f"dc-{found.group(1)}.json"
            if guess.exists():
                return guess
    raise SystemExit("error: 需要 --evidence（或一个文件名带日期、且证据包已生成的 --input）")


def panel_note(notes: Dict[str, Any], key: str) -> str:
    text = (notes or {}).get(key)
    if not text:
        return ""
    return f'<div class="panel-note">{esc(text)}</div>'


def over_length_notes(notes: Dict[str, Any]) -> Dict[str, int]:
    return {k: len(str(v)) for k, v in (notes or {}).items()
            if k in PANEL_KEYS and len(str(v)) > PANEL_NOTE_MAX}


def refuses_timeseries(quality: Optional[str]) -> bool:
    """disclosure_once 的记录拒绝画成折线。

    Beignet 只在 Blue Owl 2025Q3 10-Q 的期后事项附注里出现过一次，之后并入
    FVO 合计行。把一个点画成一条线就是造假，所以这条守卫写成函数而不是自觉。
    """
    return str(quality or "") == "disclosure_once"


# ---------------------------------------------------------------------------
# 图
# ---------------------------------------------------------------------------
_LOG_TICKS = [25, 50, 100, 200, 400, 800, 1600]


def _log_scale(lo: float, hi: float, top: float, bottom: float):
    """利差用对数轴。梯级从 40bp 到 780bp 跨了一个数量级，线性轴会把前六档
    压成一条线，读者只看得见 CRWV——那张图就废了。"""
    lo = max(lo, 5.0)
    a, b = math.log10(lo), math.log10(hi)
    span = max(b - a, 0.05)

    def y_of(value: float) -> float:
        v = max(float(value), lo)
        return bottom - (math.log10(v) - a) / span * (bottom - top)
    return y_of


def curve_chart(issuers: Dict[str, Any], universe_names: Dict[str, str]) -> str:
    """信用曲线全景：X = 剩余期限，Y = G-spread（对数轴），一个发行人一条线。

    这是本仪表盘的签名图。它一眼回答两件事：梯级的层次结构，以及每条曲线
    自己的形状（向上倾斜还是掉头）。含权券已在指标层剔除，不在这里。
    """
    live = [(k, v) for k, v in issuers.items() if (v.get("points") or [])]
    if not live:
        return ('<div class="empty">暂无数据：没有任何发行人拿到可用的利差点。'
                '<br>不画线，也不用前值补齐。</div>')

    W, H = 1440, 430
    L, R, T, B = 62, 108, 18, 34
    all_pts = [p for _, v in live for p in v["points"]]
    xs = [p["years"] for p in all_pts]
    ys = [p["gspread_bp"] for p in all_pts]
    x_lo, x_hi = 0.0, max(xs) * 1.02
    y_of = _log_scale(min(ys) * 0.8, max(ys) * 1.25, T, H - B)

    def x_of(years: float) -> float:
        return L + (years - x_lo) / max(x_hi - x_lo, 1e-6) * (W - L - R)

    parts: List[str] = []
    for tick in _LOG_TICKS:
        if tick < min(ys) * 0.8 or tick > max(ys) * 1.25:
            continue
        y = y_of(tick)
        parts.append(f'<line class="gridline" x1="{L}" y1="{y:.1f}" x2="{W-R}" y2="{y:.1f}"/>')
        parts.append(f'<text x="{L-6}" y="{y+3.5:.1f}" text-anchor="end">{tick}bp</text>')
    for years in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50):
        if years > x_hi:
            continue
        x = x_of(years)
        parts.append(f'<text x="{x:.1f}" y="{H-11}" text-anchor="middle">{years}Y</text>')
    parts.append(f'<text x="{W-R}" y="{H-11}" text-anchor="end" '
                 f'style="fill:var(--ink-4);font-size:9.5px">剩余期限</text>')

    # 右侧标签去重叠：中间那一档挤了六七个发行人，不推开就是一团糊。
    # 先按 y 排好再逐个往下推，**推完不要再逐点钳回边界**——那样会把推开的
    # 又压回去，重叠原样复现。
    label_ys: List[tuple] = []
    for key, block in live:
        pts = sorted(block["points"], key=lambda p: p["years"])
        label_ys.append((y_of(pts[-1]["gspread_bp"]), key))
    label_ys.sort()
    placed: Dict[str, float] = {}
    min_gap, cursor = 12.0, -1e9
    for y, key in label_ys:
        y = max(y, cursor + min_gap)
        placed[key] = y
        cursor = y
    overflow = placed[label_ys[-1][1]] - (H - B) if placed else 0
    if overflow > 0:                       # 整体上移，保持相对间距
        for key in placed:
            placed[key] -= overflow

    ordered = sorted(live, key=lambda kv: -(kv[1].get("rung") or 0))
    for key, block in ordered:
        color = RUNG_COLORS.get(block.get("rung"), "var(--clay)")
        pts = sorted(block["points"], key=lambda p: p["years"])
        if len(pts) >= 2:
            path = " ".join(("M" if i == 0 else "L")
                            + f"{x_of(p['years']):.1f} {y_of(p['gspread_bp']):.1f}"
                            for i, p in enumerate(pts))
            parts.append(f'<path d="{path}" fill="none" stroke="{color}" '
                         f'stroke-width="1.6" stroke-linejoin="round" opacity=".85"/>')
        for p in pts:
            parts.append(f'<circle cx="{x_of(p["years"]):.1f}" cy="{y_of(p["gspread_bp"]):.1f}" '
                         f'r="2.4" fill="{color}" opacity=".9"><title>'
                         f'{esc(key)} {p["years"]:.1f}Y {p["gspread_bp"]:.0f}bp'
                         f'{" · " + esc(p.get("name") or "") if p.get("name") else ""}'
                         f'</title></circle>')
        last = pts[-1]
        ly = placed.get(key, y_of(last["gspread_bp"]))
        anchor_y = y_of(last["gspread_bp"])
        if abs(ly - anchor_y) > 2:
            # 标签被推开了就拉一根引线回到它真正的端点，否则读者会对错行。
            parts.append(f'<path d="M{x_of(last["years"]):.1f} {anchor_y:.1f} '
                         f'L{W-R:.1f} {ly:.1f}" fill="none" stroke="{color}" '
                         f'stroke-width=".8" opacity=".35" stroke-dasharray="2 2"/>')
        parts.append(f'<text x="{W-R+6}" y="{ly+3.5:.1f}" '
                     f'fill="{color}" style="font-weight:600">'
                     f'{esc(key)} <tspan style="font-weight:400;font-size:9.5px">'
                     f'档{esc(block.get("rung"))}</tspan></text>')

    return (f'<div class="scroll"><svg class="chart" viewBox="0 0 {W} {H}" role="img" '
            f'aria-label="各发行人信用曲线：剩余期限 vs G-spread，对数纵轴">'
            f'{"".join(parts)}</svg></div>'
            f'<div class="footnote">纵轴为对数刻度——梯级跨了一个数量级，'
            f'线性轴会把前六档压成一条线。含权券（次级、永续、浮息）已剔除，'
            f'它们的 G-spread 有偏，不与普通高级无担保券同图比较。</div>')


def ladder_chart(rungs: List[Dict[str, Any]]) -> str:
    """梯级标尺：一档一条横条，长度按对数刻度，锚点用竖线标出。"""
    live = [r for r in rungs if r.get("median_5_10y") is not None]
    if not live:
        return '<div class="empty">暂无数据：没有任何档位拿到 5–10Y 代表读数。</div>'

    row_h, pad_top = 40, 14
    W = 1440
    H = pad_top + row_h * len(live) + 30
    L, R = 168, 130
    hi = max(max(r["median_5_10y"] for r in live),
             max((r.get("anchor_5_10y") or 0) for r in live)) * 1.2
    lo = 15.0
    a, b = math.log10(lo), math.log10(hi)

    def x_of(value: float) -> float:
        v = max(float(value), lo)
        return L + (math.log10(v) - a) / (b - a) * (W - L - R)

    parts: List[str] = []
    for tick in _LOG_TICKS:
        if tick > hi:
            continue
        x = x_of(tick)
        parts.append(f'<line class="gridline" x1="{x:.1f}" y1="{pad_top-6}" '
                     f'x2="{x:.1f}" y2="{H-26}"/>')
        parts.append(f'<text x="{x:.1f}" y="{H-10}" text-anchor="middle">{tick}bp</text>')

    for i, r in enumerate(live):
        y = pad_top + i * row_h
        color = RUNG_COLORS.get(r["rung"], "var(--clay)")
        value = r["median_5_10y"]
        parts.append(f'<text x="{L-10}" y="{y+18:.1f}" text-anchor="end" '
                     f'style="fill:var(--ink-2);font-size:11.5px">'
                     f'档{r["rung"]} {esc(r["name"])}</text>')
        parts.append(f'<rect x="{L}" y="{y+6:.1f}" width="{x_of(value)-L:.1f}" height="17" '
                     f'rx="3" fill="{color}" opacity=".82"><title>'
                     f'{esc("、".join(r["members"]))}</title></rect>')
        parts.append(f'<text x="{x_of(value)+7:.1f}" y="{y+19:.1f}" '
                     f'style="fill:var(--ink-1);font-weight:700;'
                     f'font-family:var(--font-mono);font-size:11.5px">{value:,.0f}</text>')
        anchor = r.get("anchor_5_10y")
        if anchor:
            ax = x_of(anchor)
            parts.append(f'<line x1="{ax:.1f}" y1="{y+3:.1f}" x2="{ax:.1f}" y2="{y+26:.1f}" '
                         f'stroke="var(--ink-1)" stroke-width="1.4" stroke-dasharray="3 2" '
                         f'opacity=".6"><title>锚点 {anchor}bp</title></line>')
    return (f'<div class="scroll"><svg class="chart" viewBox="0 0 {W} {H}" role="img" '
            f'aria-label="信用梯级：各档 5–10Y 代表读数">{"".join(parts)}</svg></div>'
            f'<div class="footnote">条长为各档成员 5–10Y 桶均值的中位数，对数刻度；'
            f'虚线是 config/universe.yaml 里的实测锚点（2026-08-25），'
            f'用来看标尺自己漂了多少。**档位归属由配置维护，脚本不自动调整**——'
            f'重新分档是判断不是计算。</div>')


# ---------------------------------------------------------------------------
# 各面板
# ---------------------------------------------------------------------------
def render_verdict(verdict: Dict[str, Any], ev: Dict[str, Any]) -> str:
    badge = verdict.get("badge")
    tone = TONES.get(str(verdict.get("badge_tone", "unknown")), TONES["unknown"])
    headline = verdict.get("headline")
    summary = verdict.get("summary")
    if not headline:
        headline = "报告未提供整体判断"
        summary = ("报告 frontmatter 里没有 verdict 块，仪表盘不会替模型编一个结论。"
                   "写法见 references/report_template.md。")

    cards: List[str] = []
    per_rung = verdict.get("rungs") or {}
    focus = [r for r in ev.get("ladder", []) if r["rung"] in (4, 6, 7)] or ev.get("ladder", [])[:3]
    for r in focus:
        entry = per_rung.get(r["rung"]) or per_rung.get(str(r["rung"])) or {}
        label, cls = TONES.get(str(entry.get("tone", "unknown")), TONES["unknown"])
        state = entry.get("status") or label
        why = entry.get("note")
        if not why:
            drift = r.get("drift_vs_anchor_bp")
            why = (f"5–10Y 中位 {r['median_5_10y']}bp"
                   + (f"，较锚点 {drift:+.1f}bp" if drift is not None else "")
                   + f"；成员 {'、'.join(r['members'])}")
        cards.append(
            f'<div class="sig"><div class="name">档{r["rung"]} · {esc(r["name"])}</div>'
            f'<div class="state {cls}">{esc(state)}</div>'
            f'<div class="why">{esc(why)}</div></div>')

    state_line = verdict.get("regime_state")
    badge_html = (f'<div class="badge-row"><span class="lbl">体系状态</span>'
                  f'<span class="badge {tone[1]}">{esc(badge or state_line or "未定档")}</span>'
                  f'</div>') if (badge or state_line) else (
        '<div class="badge-row"><span class="lbl">体系状态</span>'
        '<span class="badge t-unknown">未定档</span></div>')

    return f"""<section class="panel stack">
  <div class="verdict">
    <div class="copy">
      {badge_html}
      <h2>{esc(headline)}</h2>
      <div class="summary">{esc(summary or "")}</div>
    </div>
    <div class="signals">{"".join(cards)}</div>
  </div>
</section>"""


def render_ladder(ev: Dict[str, Any], notes: Dict[str, Any]) -> str:
    rungs = ev.get("ladder") or []
    disp = ev.get("dispersion") or {}
    rows = []
    for r in rungs:
        readings = "、".join(
            f"{k} {v:,.0f}" if v is not None else f"{k} —"
            for k, v in (r.get("readings_5_10y") or {}).items())
        rows.append(
            f'<tr><td>档{r["rung"]}</td><td>{esc(r["name"])}</td>'
            f'<td class="num">{bp(r.get("median_5_10y"))}</td>'
            f'<td class="num">{bp(r.get("anchor_5_10y"))}</td>'
            f'<td class="num">{signed(r.get("drift_vs_anchor_bp"))}</td>'
            f'<td class="num">{signed(r.get("drift_vs_anchor_excess_bp"))}</td>'
            f'<td class="dim">{esc(r.get("bench") or "")}</td>'
            f'<td class="dim">{esc(readings)}</td>'
            f'<td class="dim">{esc(r.get("role") or "")}</td></tr>')

    disp_line = (f'离散度 {disp["value"]}bp · 跨度 {disp.get("range_bp")}bp · '
                 f'{disp.get("n_rungs")} 档在场'
                 if disp.get("value") is not None else
                 f'离散度暂不出数：{esc(disp.get("quality", "未知"))}')
    return f"""<section class="panel">
  <div class="sec-head"><strong>信用梯级 · 先有标尺才谈变化</strong>
    <span class="sec-hint">{esc(disp_line)}</span></div>
  <div class="lead">单看「ORCL 205bp」读不出任何东西。有意义的是它在梯级里的位置，
  以及这个位置在移动。离散度扩张 + 弱档走宽是质量分层，压缩 + 全档同向走宽才是体系性，
  压缩 + 全档同向收窄是追逐收益——<strong>三种读法完全不同，判别留给模型</strong>。
  两列漂移要一起看：<strong>漂移</strong>是 G-spread 口径，只剥了利率 beta，整个 IG 市场
  走宽会让七档集体同向移动；<strong>剔市场</strong>再减掉该档对应的指数 OAS，剩下的才是
  这一档自己额外走的部分。<strong>只看后一列会漏掉「整体走宽本身就是风险」</strong>。</div>
  {ladder_chart(rungs)}
  <div class="scroll"><table><thead><tr>
    <th>档</th><th>名称</th><th>5–10Y 中位</th><th>锚点</th><th>漂移</th>
    <th>剔市场</th><th>基准</th><th>成员读数</th><th>它在框架里的角色</th>
  </tr></thead><tbody>{"".join(rows)}</tbody></table></div>
  {panel_note(notes, "ladder")}
</section>"""


def render_curves(ev: Dict[str, Any], notes: Dict[str, Any]) -> str:
    issuers = ev.get("issuers") or {}
    rows = []
    for key, v in sorted(issuers.items(), key=lambda kv: (kv[1].get("rung") or 99, kv[0])):
        cm = v.get("constant_maturity_bp") or {}
        flags = []
        if v.get("thin_curve"):
            flags.append('<span class="chip warn">thin_curve</span>')
        if v.get("curve_inverted"):
            flags.append('<span class="chip bad">倒挂</span>')
        elif not v.get("bucket_monotonic"):
            flags.append('<span class="chip warn">分桶不单调</span>')
        if v.get("n_excluded_option"):
            flags.append(f'<span class="chip">剔含权 {v["n_excluded_option"]}</span>')
        buckets = v.get("buckets") or {}
        rows.append(
            f'<tr><td>档{esc(v.get("rung"))}</td><td><b>{esc(key)}</b> '
            f'<span class="dim">{esc(v.get("name") or "")}</span>{"".join(flags)}</td>'
            f'<td class="num">{v.get("n_bonds")}</td>'
            + "".join(f'<td class="num">{bp(buckets.get(b, {}).get("mean_bp"))}'
                      f'<span class="dim"> ({buckets.get(b, {}).get("n", 0)})</span></td>'
                      for b in ("2-5y", "5-10y", "10-20y", "20y+"))
            + f'<td class="num">{bp(cm.get("5y"))}</td>'
              f'<td class="num">{bp(cm.get("10y"))}</td>'
              f'<td class="num">{bp(cm.get("30y"))}</td>'
              f'<td class="num">{signed(v.get("slope_bp"), 0)}</td>'
              f'<td class="dim">{esc(v.get("negative_segment") or "—")}</td></tr>')

    return f"""<section class="panel">
  <div class="sec-head"><strong>发行人曲线 · 形状比水平更早说话</strong>
    <span class="sec-hint">{len(issuers)} 个发行人 · 纵轴对数</span></div>
  <div class="lead">正常的信用曲线向上倾斜。<strong>短端反超长端（倒挂）是高收益债里
  误报率最低的信号</strong>——市场定价的不再是长期信用而是近期的违约或流动性事件。
  固定期限点（5Y/10Y/30Y）是 rolldown 的解药：债券在曲线上往下滚，利差会自然收窄，
  跟踪单只债的历史必然把它混进重定价，所以发行人层的时间序列只能走这几个点。
  超出观测跨度 3 年的期限一律不外推，显示为「—」。</div>
  {curve_chart(issuers, {})}
  <div class="scroll"><table><thead><tr>
    <th>档</th><th>发行人</th><th>只数</th>
    <th>2–5Y</th><th>5–10Y</th><th>10–20Y</th><th>20Y+</th>
    <th>CM 5Y</th><th>CM 10Y</th><th>CM 30Y</th><th>首尾斜率</th><th>负斜率段</th>
  </tr></thead><tbody>{"".join(rows)}</tbody></table></div>
  {panel_note(notes, "curves")}
</section>"""


def render_gaps(ev: Dict[str, Any], notes: Dict[str, Any]) -> str:
    cells = []
    for g in ev.get("gaps") or []:
        drift = g.get("drift_bp")
        observed = g.get("observed_bp")
        cells.append(
            f'<div class="gap-cell"><div class="name">{esc(g["a"])} − {esc(g["b"])}</div>'
            f'<div class="val">{bp(observed)}</div>'
            f'<div class="drift">锚点 {bp(g.get("anchor_bp"))} · 漂移 {signed(drift)}</div>'
            f'<div class="means">{esc(g.get("means") or "")}</div></div>')

    ts_rows = []
    for t in ev.get("term_split") or []:
        ts_rows.append(
            f'<tr><td>{esc(t["pair"])}</td>'
            f'<td class="num">{bp(t.get("short_gap_bp"))}</td>'
            f'<td class="num">{bp(t.get("long_gap_bp"))}</td>'
            f'<td class="num">{signed(t.get("split_bp"), 0)}</td>'
            f'<td class="dim">{esc(t.get("quality"))}</td></tr>')
    ts_block = (f'<div class="scroll"><table><thead><tr><th>跨档对</th>'
                f'<th>{ev.get("term_split", [{}])[0].get("short_tenor", 5)}Y 差</th>'
                f'<th>{ev.get("term_split", [{}])[0].get("long_tenor", 30)}Y 差</th>'
                f'<th>长−短</th><th>质量</th></tr></thead>'
                f'<tbody>{"".join(ts_rows)}</tbody></table></div>'
                if ts_rows else
                '<div class="empty">暂无数据：没有一对跨档主体同时拿到长短两端的固定期限点。</div>')

    attrs = ev.get("attribution") or []
    insufficient = [a for a in attrs if a.get("quality") == "insufficient_history"]
    if len(insufficient) == len(attrs) and attrs:
        attr_block = (f'<div class="empty"><b>alpha 分解暂不出数</b><br>'
                      f'{len(insufficient)} 个发行人全部因历史不足被拒。'
                      f'固定期限序列要积累到至少 2 个采集日才能做首次分解，'
                      f'20 日累积 alpha 要 20 个交易日。<br>'
                      f'这是正确状态，不要自己心算一个近似值填进去。</div>')
    else:
        rows = []
        for a in attrs:
            for window, blk in (a.get("windows") or {}).items():
                if blk.get("value_bp") is None:
                    continue
                rows.append(
                    f'<tr><td><b>{esc(a["issuer"])}</b></td><td>{esc(window)}</td>'
                    f'<td class="num">{signed(blk.get("total_bp"), 1)}</td>'
                    f'<td class="num">{signed(blk.get("beta_market_bp"), 1)}</td>'
                    f'<td class="num">{signed(blk.get("beta_tier_bp"), 1)}</td>'
                    f'<td class="num">{signed(blk.get("value_bp"), 1)}</td>'
                    f'<td class="dim">{esc(blk.get("quality"))}</td></tr>')
        attr_block = (f'<div class="scroll"><table><thead><tr><th>发行人</th><th>窗口</th>'
                      f'<th>Δ总</th><th>市场 beta</th><th>档位 beta</th><th>alpha</th>'
                      f'<th>质量</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
                      if rows else
                      '<div class="empty">暂无数据：窗口内没有可用的分解结果。</div>')

    return f"""<section class="panel">
  <div class="sec-head"><strong>跨档距离与归因 · 走宽是谁的事</strong>
    <span class="sec-hint">判据 2 / 判据 4 / 判据 1</span></div>
  <div class="lead">利差走宽 30bp，可能三件事都不是这家公司的：市场整体在走宽、
  这一档整体在走宽、或者才是它自己。所以每个发行人的变动强制拆成
  <span class="chip">Δ总 = 市场 beta + 档位 beta + alpha</span>，
  <strong>只有 alpha 连续同向累积才构成个体信用事件</strong>——单日 alpha 一律不定性，
  ETF 估值有粘滞，单日残差里混着估值噪音。</div>
  <div class="gap-grid">{"".join(cells)}</div>
  <div class="two-col" style="margin-top:14px">
    <div><div class="sec-head"><strong>长短端分化</strong>
      <span class="sec-hint">判据 4 · 长端先动</span></div>
      <div class="lead">同一个故事在长端往往先定价。这个差值收窄（短端追上来）
      = 担忧前移，是升级信号。</div>{ts_block}</div>
    <div><div class="sec-head"><strong>alpha 分解</strong>
      <span class="sec-hint">判据 1</span></div>
      <div class="lead">带 stale 的点不进分解，否则残差会系统性偏小。</div>{attr_block}</div>
  </div>
  {panel_note(notes, "gaps")}
</section>"""


def render_structure(ev: Dict[str, Any], notes: Dict[str, Any]) -> str:
    """转债层 + GPU 抵押载体 + SPV 层。三块都是「不能按公开债那样读」的东西。"""
    cb_rows = []
    for c in ev.get("convertibles") or []:
        state = ('<span class="chip bad">深度价内</span>' if c.get("deep_itm")
                 else '<span class="chip warn">贴债底</span>' if c.get("near_floor")
                 else '<span class="chip">中间态</span>')
        ov = c.get("option_value_bp")
        ov_cell = (f'<span class="na">无直债曲线可参照</span>'
                   if ov is None else f'{ov:,.0f}bp')
        credit_cell = (bp(c.get("gspread_bp")) if c.get("credit_extractable")
                       else '<span class="na">不可提取</span>')
        cb_rows.append(
            f'<tr><td><b>{esc(c["issuer"])}</b></td>'
            f'<td class="dim">{esc(str(c.get("maturity"))[:7])}</td>'
            f'<td class="num">{c.get("coupon")}</td>'
            f'<td class="num">{c.get("price"):,.2f}</td>'
            f'<td>{state}</td>'
            f'<td class="num">{ov_cell}</td>'
            f'<td class="num">{credit_cell}</td>'
            f'<td class="dim">{esc(c.get("reading"))}</td></tr>')
    cb_block = (f'<div class="scroll"><table><thead><tr><th>发行人</th><th>到期</th>'
                f'<th>票息</th><th>价格</th><th>位置</th><th>期权价值下界</th>'
                f'<th>信用利差</th><th>读法</th>'
                f'</tr></thead><tbody>{"".join(cb_rows)}</tbody></table></div>'
                if cb_rows else
                '<div class="empty">暂无数据：库里没有可转债观测。</div>')

    gpu = ev.get("gpu_secured") or {}
    gpu_rows = []
    for issuer, facilities in gpu.items():
        for f in facilities:
            gpu_rows.append(
                f'<tr><td><b>{esc(issuer)}</b></td><td>{esc(f.get("facility"))}</td>'
                f'<td class="dim">{esc(f.get("metric"))}</td>'
                f'<td class="num">{f.get("value")}</td>'
                f'<td class="dim">{esc(f.get("unit"))}</td>'
                f'<td class="dim">{esc(f.get("asof"))}</td></tr>')
    if gpu_rows:
        gpu_block = (f'<div class="scroll"><table><thead><tr><th>主体</th><th>工具</th>'
                     f'<th>指标</th><th>值</th><th>单位</th><th>报告期</th></tr></thead>'
                     f'<tbody>{"".join(gpu_rows)}</tbody></table></div>')
    else:
        gpu_block = ('<div class="empty"><b>暂无数据：GPU 抵押载体</b><br>'
                     '工具级债务事实不在 companyfacts 里（那里只有合计 LongTermDebt），'
                     '必须走 FilingSummary.xml → R{n}.htm。该路径要求 UA 里带联系邮箱，'
                     '未设置 <code>SEC_CONTACT_EMAIL</code> 时 www.sec.gov/Archives 一律 403。<br>'
                     '设置后重跑 <code>collect.py --with-sec</code> 即可，'
                     'data.sec.gov 的基本面那一路不受影响。</div>')

    spv_cards = []
    for s in ev.get("spv") or []:
        gap = s.get("coupon_vs_tenant_bp")
        once = refuses_timeseries(s.get("quality"))
        gap_line = ('<span class="na">租户同期限锚点暂无数据</span>' if gap is None else
                    f'票息 {s["coupon_pct"]}% vs 租户 {esc(s["tenant"])} 同期限 '
                    f'{s["tenant_matched_yield_pct"]}%，'
                    f'<b>{"低" if gap > 0 else "高"} {abs(gap):.0f}bp</b>')
        proxy = s.get("portfolio_proxy") or {}
        spv_cards.append(f"""<div class="gap-cell">
  <div class="name">{esc(s.get("legal_name"))} · 租户 {esc(s.get("tenant"))}</div>
  <div class="val">{s.get("coupon_pct")}%</div>
  <div class="drift">${s.get("notes_outstanding_usd_mn", 0):,.0f}mn · 到期 {esc(s.get("maturity"))}
    · 剩余 {esc(s.get("tenor_years"))}Y</div>
  <div class="means">{gap_line}<br>
    <span class="dim">租户样本 n={s.get("tenant_sample_n")}，取 ±5Y 窗口内的无含权券</span></div>
  <div class="means" style="margin-top:9px">
    {'<span class="chip bad">disclosure_once</span> 只披露过一次，之后并入合计行 —— <b>拒绝画成折线</b>' if once else ''}
    <br><span class="dim">持续可得的只有组合级代理：{esc(proxy.get("line") or "—")}，
    {proxy.get("investments")} 项投资 / {proxy.get("properties")} 处物业，
    公允价值 ${proxy.get("fv_usd_mn", 0):,.0f}mn（{esc(proxy.get("asof"))}）。
    <b>它含 {proxy.get("investments")} 项投资，不是本 SPV 单体。</b></span></div>
</div>""")
    spv_block = ("".join(spv_cards) if spv_cards else
                 '<div class="empty">暂无数据：SPV 台账为空。</div>')

    return f"""<section class="panel">
  <div class="sec-head"><strong>结构层 · 不能按公开债那样读的三块</strong>
    <span class="sec-hint">转债 / GPU 抵押 / SPV</span></div>
  <div class="two-col">
    <div><div class="sec-head"><strong>可转债</strong>
      <span class="sec-hint">深度价内 = 信用信息量趋近于零</span></div>
      <div class="lead"><strong>转债的 G-spread 不是信用利差。</strong>1.75% 票息的债
      贴着面值，按直债折现算出来的收益率远低于国债，裸 G-spread 会是负几百 bp——
      那不是「信用极好」，那是期权价值被算进了折价里。转股比例不在持仓文件里
      （parity 标 <span class="chip">paywalled</span>），所以这里用发行人自己的
      直债曲线做参照：<span class="chip">同期限直债利差 − 转债 G-spread</span>
      就是期权价值的下界，这个数越大说明它越是靠股票定价。
      只有贴债底<strong>且</strong>期权价值确实小时才打开
      <span class="chip">credit_extractable</span>——光看价格会被骗。</div>
      {cb_block}</div>
    <div><div class="sec-head"><strong>SPV · 风险在「利差不会动」里</strong>
      <span class="sec-hint">锚是租户，不是它自己</span></div>
      <div class="lead">SPV 的信用不是独立变量，是「租户信用 × 结构增信 × 抵押品残值」
      的函数。它不交易、不再单独披露，所以敞口是真的、价格是冻的——
      <strong>恶化的第一信号是租户长端走宽，不是 SPV 自己</strong>。</div>
      <div class="gap-grid">{spv_block}</div></div>
  </div>
  <div style="margin-top:14px"><div class="sec-head"><strong>GPU 抵押融资载体</strong>
    <span class="sec-hint">VIE 资产 = 残值校验的分母</span></div>
    <div class="lead">账面按直线折旧走，租金按市场走，两条线的裂口就是残值高估的累积。
    剪刀差（判据 5）本轮留插槽未实现——实现时必须先用 capex 剥掉新采购部分，
    否则账面增长会稀释出假的剪刀差。</div>
    {gpu_block}</div>
  {panel_note(notes, "structure")}
</section>"""


def render_events(ev: Dict[str, Any]) -> str:
    events = ev.get("events") or []
    mode = ev.get("events_mode", "record_only")
    if not events:
        body = ('<div class="empty">今天没有阈值穿越事件。'
                '<br>这不等于「没事」——序列还短时大部分判据根本没到能出数的长度。</div>')
    else:
        body = "".join(
            f'<div class="ev"><div class="crit">{esc(e.get("criterion"))} · '
            f'{esc(e.get("rule_id"))}</div>'
            f'<div class="body"><b>{esc(e.get("subject"))}</b> — {esc(e.get("detail"))}</div>'
            f'</div>' for e in events)
    return f"""<section class="panel stack">
  <div class="sec-head"><strong>阈值穿越事件</strong>
    <span class="sec-hint">{len(events)} 条 · mode={esc(mode)}</span></div>
  <div class="lead">这些是<strong>事件不是结论</strong>。脚本报「CRWV 曲线在 5.10Y 与
  5.89Y 之间出现负斜率」，不报「CRWV 出现违约预警」。阈值是起始配置，
  序列长到 3 个月之前它们只负责把值得看一眼的东西挑出来交给模型。</div>
  {body}
</section>"""


def render_sources(ev: Dict[str, Any]) -> str:
    health = ev.get("source_health") or []
    q = ev.get("quality_summary") or {}
    anchors = ev.get("anchors") or {}
    cards = []
    for h in health:
        status = h.get("status")
        cls = "ok" if status == "ok" else "warnc" if status == "empty" else "bad"
        cards.append(f'<div class="src"><b>{esc(h["source"])}</b>'
                     f'<div class="st {cls}">{esc(status)}</div>'
                     f'<div class="meta">{esc(h.get("detail"))}</div></div>')
    lag = anchors.get("curve_lag_days")
    lag_line = (f'持仓 As of {esc(anchors.get("holdings_asof"))}，'
                f'国债曲线锚 {esc(anchors.get("treasury_curve"))}'
                + (f"（比持仓日早 {lag} 天）" if lag else "（同日）"))
    return f"""<section class="panel">
  <div class="sec-head"><strong>数据源与口径</strong>
    <span class="sec-hint">{esc(lag_line)}</span></div>
  <div class="lead">口径限制三条，写报告时必须出现：
  ①ETF 持仓价是<strong>基金管理人的估值不是成交价</strong>，流动性差的券会粘滞；
  ②这是该发行人<strong>在指数样本中的子集</strong>，不是全部存量债，发行人层聚合
  只能说「样本内加权」；③<strong>G-spread 不是 OAS</strong>，含权券已剔除但仍要标注。
  CDS 与逐笔 TRACE 已实测确认无免费源，本监控<strong>不建这两列</strong>。</div>
  <div class="srcs">{"".join(cards)}</div>
  <div class="footnote">在库 {q.get("instruments_total")} 只，有利差 {q.get("instruments_priced")} 只；
  stale {q.get("stale_prices")} 条、含权剔除 {q.get("option_biased")} 条；
  thin_curve 发行人：{esc("、".join(q.get("thin_curve_issuers") or []) or "无")}。
  指数 OAS 锚 {esc(anchors.get("index_oas"))}：IG {bp((anchors.get("index_oas_bp") or {}).get("ig"))}、
  HY {bp((anchors.get("index_oas_bp") or {}).get("hy"))}。</div>
</section>"""


# ---------------------------------------------------------------------------
# 精简页（默认）—— 日频追踪只该有这些
# ---------------------------------------------------------------------------
_BAR_FLOOR_BP = 20.0

COMPACT_CSS = """
/* 表比视口窄不了：名称列 + 当前 + 五档变化，最小内容宽度就有 500px 出头。
   让它在自己的容器里横向滚动，而不是把整个页面推宽——页面横向滚动之后
   顶部判断区和异动列表也会跟着跑偏。 */
.dial-scroll { overflow-x: auto; }
.dial-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 4px; }
.dial-table th { font-size: 11px; color: var(--ink-4); font-weight: 500;
                 text-align: right; padding: 6px 10px; white-space: nowrap; }
.dial-table th:first-child, .dial-table td:first-child { text-align: left; }
.dial-table td { padding: 8px 10px; border-top: 1px solid var(--line-2);
                 white-space: nowrap; text-align: right; }
.dial-table tr.grp td { border-top: none; padding: 14px 10px 2px;
                        font-size: 11px; color: var(--ink-4); letter-spacing: .04em; }
.dial-table tr.grp:first-child td { padding-top: 2px; }
.dial-table tr.category-index td { background: var(--surface-2); }
.dial-table tr.category-index td:first-child { border-left: 3px solid var(--category-color); }
.dial-table tr.category-member td { padding-top: 5px; padding-bottom: 5px;
                                    color: var(--ink-3); }
.dial-table tr.category-member td:first-child { padding-left: 25px; }
.dial-name { display: flex; align-items: baseline; gap: 8px; }
.dial-name b { font-weight: 600; font-size: 13px; color: var(--ink-1); }
.dial-name .sub { font-size: 11px; color: var(--ink-4); font-weight: 400;
                  white-space: normal; }
.category-member .dial-name b { font-family: var(--font-mono); font-size: 12px;
                                font-weight: 600; color: var(--ink-2); }
.category-member .dial-name:before { content: "\u2514"; color: var(--ink-4); margin-right: 1px; }
.category-member .dial-val { font-size: 12.5px; font-weight: 600; color: var(--ink-2); }
.dial-val { font-family: var(--font-mono); font-size: 15px; font-weight: 700;
            color: var(--ink-1); }
.dial-d { font-family: var(--font-mono); font-size: 12.5px; }
.bar { height: 5px; background: var(--line-2); border-radius: 3px; overflow: hidden;
       max-width: 300px; margin-top: 8px; }
.bar > i { display: block; height: 100%; border-radius: 3px; }
/* 类别指数与它的成分装在同一个盒子里：指数动了，往下一眼就能看到是谁在动。 */
.cat { border: 1px solid var(--line-2); border-left: 3px solid var(--category-color);
       border-radius: 10px; padding: 11px 13px 13px; margin-top: 10px; }
.cat > .head { display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap; }
.cat > .head b { font-size: 14px; font-weight: 600; color: var(--ink-1); }
.cat > .head .sub { font-size: 11px; color: var(--ink-4); }
/* 统计块给固定最小宽度，四个盒子的数字才会上下对齐——合并显示不该以
   丢掉跨类别横向比较为代价。 */
.catstats { display: flex; flex-wrap: wrap; gap: 8px 0; margin-top: 9px; }
.catstats .st { min-width: 92px; }
.catstats .st .k { display: block; font-size: 10.5px; color: var(--ink-4);
                   letter-spacing: .02em; }
.catstats .st .v { font-family: var(--font-mono); font-size: 17px; font-weight: 700;
                   color: var(--ink-1); }
.catstats .st.d .v { font-size: 13.5px; }
.catstats .st .v.na { color: var(--ink-4); }
.headline-row { display: flex; gap: 20px; align-items: flex-start; flex-wrap: wrap; }
.headline-row .copy { flex: 1 1 420px; min-width: 320px; }
.watch { margin-top: 4px; }
.watch li { font-size: 12.5px; color: var(--ink-2); line-height: 1.65;
            margin: 7px 0; list-style: none; padding-left: 14px; position: relative; }
.watch li:before { content: ""; position: absolute; left: 0; top: .62em;
                   width: 5px; height: 5px; border-radius: 50%; background: var(--clay); }
.watch li .tag { font-family: var(--font-mono); font-size: 10.5px; color: var(--ink-4);
                 margin-right: 6px; }
/* 子项卡：一行两列。窄屏收成一列——两列挤到 160px 以下时曲线就没法读了。 */
.mcards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 10px; margin-top: 8px; }
.mgrp { margin-top: 16px; font-size: 11px; color: var(--ink-4); letter-spacing: .04em; }
.mgrp:first-child { margin-top: 6px; }
.mcard { border: 1px solid var(--line-2); border-left: 3px solid var(--category-color, var(--clay));
         border-radius: 8px; padding: 10px 12px 8px; background: var(--surface-2); }
.mcard .top { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.mcard .who b { font-family: var(--font-mono); font-size: 13px; font-weight: 700;
                color: var(--ink-1); }
.mcard .who span { font-size: 11px; color: var(--ink-4); margin-left: 6px; }
.mcard .now { font-family: var(--font-mono); font-size: 18px; font-weight: 700;
              color: var(--ink-1); white-space: nowrap; }
.mcard .now i { font-style: normal; font-size: 11px; color: var(--ink-4); margin-left: 3px; }
.mcard .meta { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 11px;
               color: var(--ink-4); margin-top: 3px; }
.mcard .meta .d { font-family: var(--font-mono); }
.mcard .spark { display: block; width: 100%; height: auto; margin-top: 7px; }
.mcard .nospark { margin-top: 7px; padding: 13px 10px; border: 1px dashed var(--line-2);
                  border-radius: 6px; font-size: 11px; color: var(--ink-4);
                  line-height: 1.6; text-align: center; }
@media (max-width: 760px) {
  .mcards { grid-template-columns: 1fr; }
  .catstats .st { min-width: 78px; }
}
"""


def _delta_cell(value: Optional[float], css: str = "dial-d") -> str:
    if value is None:
        return f'<td class="{css} na">—</td>'
    cls = "pct-up" if value > 0 else "pct-down" if value < 0 else "pct-flat"
    return f'<td class="{css} {cls}">{value:+,.1f}</td>'


_COMPACT_CATEGORIES = (
    {"name": "超大厂指数", "rungs": (1, 2, 4, 6), "colour": RUNG_COLORS[2]},
    {"name": "受监管公用事业指数", "rungs": (3,), "colour": RUNG_COLORS[3]},
    {"name": "数据中心 REIT 指数", "rungs": (5,), "colour": RUNG_COLORS[5]},
    {"name": "纯算力商指数", "rungs": (7,), "colour": RUNG_COLORS[7]},
)


def _compact_category_dials(ev: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把七档折叠为四个展示类别，但不改变证据包里的底层档位。

    类别指数取成员加权后的档位指数中位数：一个档有两个成员，它的档位指数就在
    类别中占两个位置。历史变化也用同一权重从各档指数反推，确保口径一致。
    """
    rung_dials = {int(d["rung"]): d for d in (ev.get("dials") or [])
                  if d.get("group") == "梯级" and d.get("rung") is not None}
    ladder = {int(r["rung"]): r for r in (ev.get("ladder") or [])
              if r.get("rung") is not None}
    issuers = ev.get("issuers") or {}
    out: List[Dict[str, Any]] = []

    for spec in _COMPACT_CATEGORIES:
        selected = [(rung, rung_dials[rung]) for rung in spec["rungs"]
                    if rung in rung_dials]
        if not selected:
            continue

        weighted_values: List[float] = []
        weighted_anchors: List[float] = []
        weighted_excess: List[float] = []
        weighted_anchor_excess: List[float] = []
        members: List[Dict[str, Any]] = []
        for rung, dial in selected:
            rung_block = ladder.get(rung, {})
            rung_members = rung_block.get("members") or []
            weight = max(len(rung_members), 1)
            if dial.get("value") is not None:
                weighted_values.extend([float(dial["value"])] * weight)
            if dial.get("anchor") is not None:
                weighted_anchors.extend([float(dial["anchor"])] * weight)
            # 超额口径用同一套权重聚合，否则两个数不同口径没法并排读。
            # 一个类别里的档共用同一条指数（超大厂全是 ig、纯算力商是 hy），
            # 所以这里的中位数与「先取中位再减指数」等价。
            if dial.get("excess") is not None:
                weighted_excess.extend([float(dial["excess"])] * weight)
            if dial.get("anchor_excess") is not None:
                weighted_anchor_excess.extend([float(dial["anchor_excess"])] * weight)
            readings = rung_block.get("readings_5_10y") or {}
            for issuer in rung_members or list(readings):
                bucket = ((issuers.get(issuer) or {}).get("buckets") or {}).get("5-10y") or {}
                members.append({
                    "issuer": issuer,
                    "value": readings.get(issuer),
                    "sample_n": bucket.get("n"),
                })

        value = round(float(median(weighted_values)), 1) if weighted_values else None
        anchor = round(float(median(weighted_anchors)), 1) if weighted_anchors else None
        excess = round(float(median(weighted_excess)), 1) if weighted_excess else None
        anchor_excess = (round(float(median(weighted_anchor_excess)), 1)
                         if weighted_anchor_excess else None)

        changes: Dict[str, Optional[float]] = {}
        for field in ("d1", "d7", "d30"):
            prior: List[float] = []
            complete = value is not None
            for rung, dial in selected:
                rung_members = ladder.get(rung, {}).get("members") or []
                weight = max(len(rung_members), 1)
                if dial.get("value") is None or dial.get(field) is None:
                    complete = False
                    break
                prior.extend([float(dial["value"]) - float(dial[field])] * weight)
            changes[field] = (round(value - float(median(prior)), 1)
                              if complete and prior else None)

        out.append({
            "group": "类别指数",
            "name": spec["name"],
            "note": f"{len(members)} 家 · 成员加权档位中位数",
            "value": value,
            "anchor": anchor,
            "vs_anchor": (round(value - anchor, 1)
                          if value is not None and anchor is not None else None),
            "excess": excess,
            "vs_anchor_excess": (round(excess - anchor_excess, 1)
                                 if excess is not None and anchor_excess is not None
                                 else None),
            "bench": next((d.get("bench") for _, d in selected if d.get("bench")), None),
            "d1": changes["d1"], "d7": changes["d7"], "d30": changes["d30"],
            "colour": spec["colour"],
            "members": members,
        })
    return out


def member_spark(series: List[List[Any]], anchor: Optional[float], colour: str) -> str:
    """子项的 5–10Y 曲线。线性刻度——单个发行人的量程窄，对数会把变化压平。

    横轴按观测序号等距，不按日历：持仓文件周末不更新，按日历排会在每个周末留一段
    空白，读起来像「利差不动」，其实是没数据。首尾日期标在轴下。

    viewBox 的宽度按卡片实际宽度（两列布局下约 340px）取，不是随便挑一个大数：
    SVG 里的字号是 viewBox 单位，viewBox 比容器宽多少，字就被缩小多少倍。
    锚点标在左端、当前值标在右端，**两个标签各占一头就不会叠**——它们的纵坐标
    经常只差一两个 bp，挤在同一侧必然重合。
    """
    points = [(str(d), float(v)) for d, v in series if v is not None]
    if len(points) < 2:
        return ""
    W, H = 340.0, 86.0
    L, R, T, B = 4.0, 34.0, 13.0, 13.0
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    if anchor is not None:
        lo, hi = min(lo, float(anchor)), max(hi, float(anchor))
    pad = max((hi - lo) * 0.14, 0.8)
    lo, hi = lo - pad, hi + pad

    def x_of(i: int) -> float:
        return L + (i / (len(points) - 1)) * (W - L - R)

    def y_of(v: float) -> float:
        return T + (hi - v) / (hi - lo) * (H - T - B)

    parts: List[str] = []
    if anchor is not None:
        ay = y_of(float(anchor))
        parts.append(f'<line x1="{L}" y1="{ay:.1f}" x2="{W-R+6:.1f}" y2="{ay:.1f}" '
                     f'stroke="var(--ink-1)" stroke-width="1" stroke-dasharray="3 3" '
                     f'opacity=".4"/>')
        # 锚点线贴顶时把标签压到线下面，否则会被裁掉。
        label_y = ay - 3.5 if ay - T > 11 else ay + 10
        text = f"锚 {float(anchor):,.0f}"
        # 曲线起点常常就在锚点附近，标签会被线穿过去。垫一块卡片底色的方块，
        # 比把标签挪到图外省地方，也比加一条引导线干净。
        w = 11.0 + 6.0 * (len(text) - 1)
        parts.append(f'<rect x="{L:.1f}" y="{label_y-8:.1f}" width="{w:.1f}" height="10.5" '
                     f'fill="var(--surface-2)" opacity=".9"/>')
        parts.append(f'<text x="{L+1:.1f}" y="{label_y:.1f}" '
                     f'style="fill:var(--ink-4);font-size:10px;'
                     f'font-family:var(--font-mono)">{text}</text>')
    path = " ".join(f'{"M" if i == 0 else "L"}{x_of(i):.1f} {y_of(v):.1f}'
                    for i, (_, v) in enumerate(points))
    parts.append(f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="1.6" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')
    lx, ly = x_of(len(points) - 1), y_of(values[-1])
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.4" fill="{colour}"/>')
    parts.append(f'<text x="{lx+5:.1f}" y="{ly+3.6:.1f}" '
                 f'style="fill:var(--ink-1);font-size:11px;font-weight:700;'
                 f'font-family:var(--font-mono)">{values[-1]:,.0f}</text>')
    parts.append(f'<text x="{L}" y="{H-2:.1f}" style="fill:var(--ink-4);font-size:9px">'
                 f'{esc(points[0][0])}</text>')
    parts.append(f'<text x="{W-R:.1f}" y="{H-2:.1f}" text-anchor="end" '
                 f'style="fill:var(--ink-4);font-size:9px">{esc(points[-1][0])}</text>')
    return (f'<svg class="spark" viewBox="0 0 {W:.0f} {H:.0f}" role="img" '
            f'aria-label="5–10Y 利差曲线">{"".join(parts)}</svg>')


def _member_card(chart: Dict[str, Any], colour: str) -> str:
    """一张子项卡：当前值、锚点与相对锚点的漂移，加一条曲线。

    **点数不够就不画线**，写清楚差多少天以及为什么补不回来——价格层只有当日
    快照，历史无法回补（见 sources.yaml 的 not_available）。留一个空图或者用两个点
    连一条直线，都会让人以为看见了趋势。
    """
    value, anchor = chart.get("value"), chart.get("anchor")
    drift = chart.get("drift_vs_anchor_bp")
    n = chart.get("sample_n")
    drift_cls = ("pct-up" if (drift or 0) > 0 else
                 "pct-down" if (drift or 0) < 0 else "pct-flat")
    meta = [f'档{chart.get("rung")}' if chart.get("rung") else "",
            "暂无样本" if n == 0 else (f"{n} 只样本" if n else ""),
            (f'锚点 {float(anchor):,.0f}bp' if anchor is not None else "无实测锚点")]
    drift_html = (f'<span class="d {drift_cls}">{drift:+,.1f}bp</span>'
                  if drift is not None else '<span class="d">漂移 —</span>')

    # **点数够不够由证据包的 quality 说了算，渲染层不自己判断。**
    # 直接把 series 丢给 member_spark 会绕过 min_points_for_line——两个点照样连出
    # 一条线，正是这条守卫要拦的东西。
    body = (member_spark(chart.get("series") or [], anchor, colour)
            if chart.get("quality") == "ok" else "")
    if not body:
        # 缺数的理由每张卡都一样，写在小节抬头一次就够；卡上只留差多少天。
        # 11 张卡各贴一段同样的话，读者会直接把整块跳过去。
        need = chart.get("min_points_for_line")
        days = chart.get("series_days") or 0
        body = ('<div class="nospark">桶里没有样本，不出读数</div>'
                if chart.get("quality") == "no_reading" else
                f'<div class="nospark">序列 {days} 天 · 还差 {max(need - days, 0)} 天'
                f'才够画线</div>')

    return (f'<div class="mcard" style="--category-color:{colour}">'
            f'<div class="top"><div class="who"><b>{esc(chart["issuer"])}</b>'
            f'<span>{esc(chart.get("name") or "")}</span></div>'
            f'<div class="now">{"—" if value is None else f"{value:,.0f}"}<i>bp</i></div></div>'
            f'<div class="meta">{"".join(f"<span>{esc(m)}</span>" for m in meta if m)}'
            f'{drift_html}</div>{body}</div>')


def member_cards_grid(charts: Dict[str, Any], category: Dict[str, Any]) -> str:
    """一个类别下的子项，一行两列。

    子项不再占表里的一行——表那几列（1D/1W/1M/vs 锚点/剔市场）在子项上本来就全是
    空的，那些变化量只在类别指数层算，留在表里纯属占位。卡片能放下当前值、锚点、
    相对锚点的漂移和一条曲线，信息密度反而更高。
    """
    colour = category.get("colour") or "var(--clay)"
    cards = [_member_card(charts[m["issuer"]], colour)
             for m in (category.get("members") or []) if m["issuer"] in charts]
    return f'<div class="mcards">{"".join(cards)}</div>' if cards else ""


def member_chart_note(charts: Dict[str, Any]) -> str:
    """曲线为什么是空的，整页说一次就够——11 张卡各贴一段同样的话没人会看。"""
    if not charts:
        return ""
    sample = next(iter(charts.values()))
    window, need = sample.get("window_days"), sample.get("min_points_for_line")
    drawn = sum(1 for c in charts.values() if c.get("quality") == "ok")
    short = sum(1 for c in charts.values() if c.get("quality") == "insufficient_series")
    line = f'子项曲线取 5–10Y 桶均值，{window} 天窗口。'
    if drawn == 0 and short:
        line += ('现在一条都画不出来：价格层只有当日快照，历史补不回来'
                 '（SPDR 端点没有归档，逐 CUSIP 历史价格也无免费源），'
                 f'序列只能从开始采集那天往后攒，够 {need} 天才画线。'
                 '虚线锚点与当前值不受影响，那两个数今天就成立。')
    elif short:
        line += f'其中 {short} 家序列还不足 {need} 天，不画线。'
    return f'<div class="footnote">{esc(line)}</div>'


def _stat(label: str, value: Optional[float], *, signed_fmt: bool = True,
          big: bool = False) -> str:
    if value is None:
        text, cls = "—", "na"
    elif signed_fmt:
        text = f"{value:+,.1f}"
        cls = "pct-up" if value > 0 else "pct-down" if value < 0 else "pct-flat"
    else:
        text, cls = f"{value:,.0f}", ""
    return (f'<div class="st{"" if big else " d"}"><span class="k">{esc(label)}</span>'
            f'<span class="v {cls}">{text}</span></div>')


def _category_block(d: Dict[str, Any], cards: str, hi: float) -> str:
    """一个类别指数连同它的子项曲线，装在同一个盒子里。

    分开放过一版：指数在上面的表里、子项卡在页尾。读的时候得来回滚，
    「这个指数由谁构成、是谁把它拉动的」这个问题反而变难了。合在一起之后
    指标与成分同屏，指数动了直接往下看是哪家在动。

    各类别的统计块用同样的最小宽度，所以四个盒子的数字仍然上下对齐，
    跨类别横向比较没丢。
    """
    value = d["value"]
    colour = d.get("colour") or "var(--clay)"
    bar = ""
    # 对数基准从 20bp 起算而不是从 1——从 1 起算会把 40bp 画成满格的 55%，
    # 各类别看起来一样长，条形就白画了。
    if value is not None and hi > _BAR_FLOOR_BP:
        span = math.log10(hi) - math.log10(_BAR_FLOOR_BP)
        frac = (math.log10(max(value, _BAR_FLOOR_BP)) - math.log10(_BAR_FLOOR_BP)) / span
        bar = (f'<div class="bar"><i style="width:{max(3, frac*100):.0f}%;'
               f'background:{colour}"></i></div>')
    stats = (_stat("当前 bp", value, signed_fmt=False, big=True)
             + _stat("1D", d.get("d1")) + _stat("1W", d.get("d7"))
             + _stat("1M", d.get("d30")) + _stat("vs 锚点", d.get("vs_anchor"))
             + _stat("剔市场", d.get("vs_anchor_excess")))
    return (f'<div class="cat" style="--category-color:{colour}">'
            f'<div class="head"><b>{esc(d["name"])}</b>'
            f'<span class="sub">{esc(d.get("note") or "")}</span></div>'
            f'{bar}<div class="catstats">{stats}</div>{cards}</div>')


def render_dials(ev: Dict[str, Any], notes: Dict[str, Any]) -> str:
    """核心刻度 —— 精简页的主体。

    选择标准只有一条：这个数每天看一眼值不值得。水平值本身信息量很低，
    驱动判断的是它相对昨天、上周、以及相对锚点的位移。

    两种排版分工：**类别指数和它的成分是一个盒子**，因为读它们的时候是一件事；
    跨档距离与结构量没有成分，留在下面那张表里横向对齐着比。
    """
    raw_dials = ev.get("dials") or []
    if not raw_dials:
        return ('<section class="panel stack"><div class="empty">'
                '暂无数据：证据包里没有 dials 段，先跑一次 metrics.py。</div></section>')

    category_dials = _compact_category_dials(ev)
    charts = {c["issuer"]: c for c in (ev.get("member_charts") or [])}
    category_vals = [d["value"] for d in category_dials if d["value"] is not None]
    hi = max(category_vals) if category_vals else 1.0
    blocks = "".join(_category_block(d, member_cards_grid(charts, d), hi)
                     for d in category_dials)

    # 「剔市场」在这张表里恒为破折号：跨档距离是差的差，同段的指数 OAS 在减法里
    # 自己抵掉了；离散度与 SPV 票息差本来就不是相对指数的量。给一个带解释的
    # 破折号，而不是留白让人以为是缺数。
    _NO_EXCESS_REASON = {
        "跨档距离": "跨档距离是差的差，同段市场 beta 已自然对消，无需再剔",
        "结构": "不是相对指数的水平量，剔市场无定义",
    }
    rows: List[str] = []
    last_group = None
    for d in [x for x in raw_dials if x.get("group") != "梯级"]:
        if d["group"] != last_group:
            rows.append(f'<tr class="grp"><td colspan="7">{esc(d["group"])}</td></tr>')
            last_group = d["group"]
        value = d["value"]
        rows.append(
            f'<tr><td><div class="dial-name"><b>{esc(d["name"])}</b>'
            f'<span class="sub">{esc(d.get("note") or "")}</span></div></td>'
            f'<td class="dial-val">{"—" if value is None else f"{value:,.0f}"}</td>'
            + _delta_cell(d.get("d1")) + _delta_cell(d.get("d7"))
            + _delta_cell(d.get("d30")) + _delta_cell(d.get("vs_anchor"))
            + f'<td class="dial-d na" title='
              f'"{esc(_NO_EXCESS_REASON.get(d["group"], ""))}">—</td></tr>')

    have_deltas = any(d.get("d1") is not None for d in category_dials)
    hint = ("单位 bp · 类别指数与它的成分同框" if have_deltas else
            "单位 bp · 序列只有 1 天，指数变化不出数")

    # 两个锚点口径必须写在旁边。G-spread 只剥了利率 beta，信用市场 beta 还在里面：
    # IG 指数走宽 14bp，「vs 锚点」会集体 +14，看着像 AI 出事，其实是整个投资级
    # 市场在动。「剔市场」把那部分也减掉。
    anchors = ev.get("anchors") or {}
    anchor_day = anchors.get("anchor_asof")
    now_oas = anchors.get("index_oas_bp") or {}
    then_oas = anchors.get("anchor_index_oas_bp") or {}
    if then_oas:
        oas_line = (f'IG 指数 OAS {bp(then_oas.get("ig"))} → {bp(now_oas.get("ig"))}、'
                    f'HY {bp(then_oas.get("hy"))} → {bp(now_oas.get("hy"))}')
    else:
        oas_line = "锚点日的指数 OAS 缺失，剔市场不出数"
    foot = (f'两个锚点口径不同：<b>vs 锚点</b>是 G-spread 相对 {esc(anchor_day or "锚点日")} '
            f'的漂移，只剥了利率 beta；<b>剔市场</b>再减掉对应指数 OAS，'
            f'剩下的才是这一档相对市场额外走的部分。同期 {esc(oas_line)}——'
            f'两者的差就是这段市场 beta。')

    return f"""<section class="panel stack">
  <div class="sec-head"><strong>核心刻度</strong><span class="sec-hint">{esc(hint)}</span></div>
  {blocks}
  {member_chart_note(charts)}
  <div class="dial-scroll"><table class="dial-table">
    <thead><tr><th>跨档距离 / 结构</th><th>当前</th>
      <th>1D</th><th>1W</th><th>1M</th><th>vs 锚点</th><th>剔市场</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table></div>
  <div class="footnote">{foot}</div>
  {panel_note(notes, "ladder")}
</section>"""


def render_watchlist(ev: Dict[str, Any], verdict: Dict[str, Any]) -> str:
    """今天值得看的 —— 事件 + 结构层的例外状态，最多 6 条。

    这些是**事件不是结论**：脚本报「曲线在某两点之间出现负斜率」，
    不报「出现违约预警」。
    """
    items: List[str] = []
    for e in (ev.get("events") or [])[:6]:
        items.append(f'<li><span class="tag">{esc(e.get("criterion"))}</span>'
                     f'<b>{esc(e.get("subject"))}</b> — {esc(e.get("detail"))}</li>')

    cbs = ev.get("convertibles") or []
    if cbs:
        n_ext = sum(1 for c in cbs if c.get("credit_extractable"))
        if n_ext == 0:
            items.append(f'<li><span class="tag">转债</span>'
                         f'{len(cbs)} 只全部<b>信用不可提取</b>——G-spread 里是期权价值不是信用；'
                         f'这几家的信用观察走基本面层。</li>')

    thin = (ev.get("quality_summary") or {}).get("thin_curve_issuers") or []
    if thin:
        items.append(f'<li><span class="tag">样本</span>{esc("、".join(thin))} '
                     f'样本不足，<b>不谈曲线形状</b>。</li>')

    if not (ev.get("gpu_secured") or {}):
        items.append('<li><span class="tag">缺口</span>GPU 抵押载体未取到——'
                     'R-file 路径需要 <code>SEC_CONTACT_EMAIL</code>，否则 '
                     '<code>www.sec.gov/Archives</code> 一律 403。</li>')

    if not items:
        items.append('<li>今天没有触发项。这不等于「没事」——序列还短时'
                     '大部分判据根本没到能出数的长度。</li>')

    return f"""<section class="panel stack">
  <div class="sec-head"><strong>今天值得看的</strong>
    <span class="sec-hint">{len(items)} 条 · 事件不是结论</span></div>
  <ul class="watch">{"".join(items)}</ul>
</section>"""


def render_compact_verdict(verdict: Dict[str, Any]) -> str:
    tone = TONES.get(str(verdict.get("badge_tone", "unknown")), TONES["unknown"])
    headline = verdict.get("headline")
    summary = verdict.get("summary")
    if not headline:
        headline = "报告未提供整体判断"
        summary = ("报告 frontmatter 里没有 verdict 块，仪表盘不会替模型编一个结论。"
                   "写法见 references/report_template.md。")
    chips = []
    for rung, entry in sorted((verdict.get("rungs") or {}).items(), key=lambda kv: str(kv[0])):
        label, cls = TONES.get(str((entry or {}).get("tone", "unknown")), TONES["unknown"])
        chips.append(f'<span class="badge {cls}">档{esc(rung)} '
                     f'{esc((entry or {}).get("status") or label)}</span>')
    return f"""<section class="panel stack">
  <div class="headline-row">
    <div class="copy">
      <div class="badge-row"><span class="lbl">体系状态</span>
        <span class="badge {tone[1]}">{esc(verdict.get("badge") or "未定档")}</span></div>
      <h2>{esc(headline)}</h2>
      <div class="summary">{esc(summary or "")}</div>
    </div>
  </div>
  {'<div class="badge-row" style="margin-top:12px">' + "".join(chips) + '</div>' if chips else ''}
</section>"""


def build_compact_html(ev: Dict[str, Any], verdict: Dict[str, Any],
                       report_date: Optional[str] = None) -> str:
    theme = THEME_CSS.read_text(encoding="utf-8") if THEME_CSS.exists() else ""
    asof = ev.get("asof", "—")
    report_date = report_date or date.today().isoformat()
    stamp = (ev.get("generated_at") or
             datetime.now(timezone.utc).isoformat())[:16].replace("T", " ")
    notes = verdict.get("panels") or {}
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>数据中心信用监控 · {esc(report_date)}</title>
<style>{theme}{LAYOUT_CSS}{COMPACT_CSS}</style>
</head>
<body>
<main class="wrap" style="width:min(1080px, calc(100vw - 40px))">
  <header class="hd">
    <div>
      <div class="eyebrow">AI 基建信用 · 日频追踪</div>
      <h1>Data Center Credit Monitor</h1>
    </div>
    <div class="stamp">报告日 {esc(report_date)} · 数据截止 {esc(asof)}<br>UTC {esc(stamp)}</div>
  </header>
  {render_compact_verdict(verdict)}
  {render_watchlist(ev, verdict)}
  {render_dials(ev, notes)}
</main>
</body>
</html>"""



def build_html(ev: Dict[str, Any], verdict: Dict[str, Any],
               report_date: Optional[str] = None) -> str:
    theme = THEME_CSS.read_text(encoding="utf-8") if THEME_CSS.exists() else ""
    asof = ev.get("asof", "—")
    report_date = report_date or date.today().isoformat()
    window = ev.get("window_days", 90)
    health = ev.get("source_health") or []
    ok = sum(1 for h in health if h.get("status") in ("ok", "empty"))
    stamp = (ev.get("generated_at") or
             datetime.now(timezone.utc).isoformat())[:16].replace("T", " ")
    notes = verdict.get("panels") or {}
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>数据中心信用监控 · {esc(report_date)}</title>
<style>{theme}{LAYOUT_CSS}</style>
</head>
<body>
<main class="wrap">
  <header class="hd">
    <div>
      <div class="eyebrow">全发行人总览 · 最近 {esc(window)} 天 · 不设发行人与时间切换</div>
      <h1>Data Center Credit Monitor</h1>
      <div class="sub">信用梯级 × 曲线形状 × 跨档距离 × 抵押品 → AI 基建融资成本的边际变化</div>
    </div>
    <div class="stamp">报告日 {esc(report_date)} · 数据截止 {esc(asof)} · 数据源 {ok}/{len(health)}<br>UTC {esc(stamp)}</div>
  </header>
  {render_verdict(verdict, ev)}
  <div class="stack">{render_ladder(ev, notes)}</div>
  <div class="stack">{render_curves(ev, notes)}</div>
  <div class="stack">{render_gaps(ev, notes)}</div>
  <div class="stack">{render_structure(ev, notes)}</div>
  {render_events(ev)}
  {render_sources(ev)}
</main>
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染数据中心信用监控 Dashboard。")
    parser.add_argument("--evidence", default=None, help="metrics.py 产出的证据包")
    parser.add_argument("--input", default=None,
                        help="报告 Markdown；读取 frontmatter 的 date/data_asof/verdict")
    parser.add_argument("--report-date", default=None,
                        help="报告执行日 YYYY-MM-DD；默认读 frontmatter.date，再退回本地今天")
    parser.add_argument("--output", default=None)
    parser.add_argument("--full", action="store_true",
                        help="渲染完整明细版（逐只债、发行人曲线表、转债与抵押品）。"
                             "默认输出日频追踪用的精简页。")
    args = parser.parse_args()

    md_path = Path(args.input) if args.input else None
    meta = read_report_meta(md_path)
    report_date = resolve_report_date(meta, args.report_date)
    evidence_path = find_evidence(args.evidence, md_path, meta)
    ev = json.loads(evidence_path.read_text(encoding="utf-8"))
    verdict = meta.get("verdict") or {}

    suffix = "-full" if args.full else ""
    output = Path(args.output) if args.output else (
        SKILL_ROOT / "reports" / f"dc-{report_date}{suffix}.html")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text((build_html if args.full else build_compact_html)(
                      ev, verdict, report_date),
                      encoding="utf-8")

    print(json.dumps({
        "output": str(output),
        "mode": "full" if args.full else "compact",
        "evidence": str(evidence_path),
        "report_date": report_date,
        "asof": ev.get("asof"),
        "verdict_present": bool(verdict.get("headline")),
        "panel_notes": [k for k in PANEL_KEYS if (verdict.get("panels") or {}).get(k)],
        "panel_notes_over_limit": over_length_notes(verdict.get("panels") or {}),
        "issuers_charted": [k for k, v in (ev.get("issuers") or {}).items()
                            if (v.get("points") or [])],
        "disclosure_once_guarded": [s.get("id") for s in (ev.get("spv") or [])
                                    if refuses_timeseries(s.get("quality"))],
        "sources": {h["source"]: h["status"] for h in ev.get("source_health") or []},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
