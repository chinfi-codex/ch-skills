#!/usr/bin/env python3
"""把证据包渲染成单页 GPU 算力监控 Dashboard（自包含 HTML，无外部依赖）。

这是 PRD §5 要的那个东西：一屏之内回答「不同代际的价格在怎么变、供给是否
同步松紧」，不设 GPU selector、不设时间范围切换、窗口固定 90 天。

脑 / 手边界在这里体现得最清楚：
  * 判断（整体结论、每个型号的状态与一句话依据）由模型写在报告 Markdown 的
    frontmatter `verdict:` 块里，脚本原样搬运，一个字不改也不生成。
  * 其余全部由脚本从 evidence 里确定性地摆出来：曲线、分位数、报价矩阵、
    折价、样本量、源健康度。
所以这个脚本不认识"偏紧"是什么意思，它只负责把模型的判断和机器的证据
放进同一屏，让读者能当场对账。

缺失一律显式：缺数写「暂无数据」、样本不足写「样本不足」、不可比写
「不可比」，绝不用前值冒充最新值，也不用 0 冒充空。

用法：
    python scripts/render_report_html.py --evidence evidence/gpu-2026-08-25.json
    python scripts/render_report_html.py --input reports/gpu-2026-08-25.md   # 从报告读 verdict
    python scripts/render_report_html.py --evidence … --output docs/index.html
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

SCRIPT_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_ROOT.parent
_BUNDLED_SHARED = SCRIPT_ROOT / "_shared"
_DEV_SHARED = SCRIPT_ROOT.parents[1] / "shared"
SHARED_ROOT = _BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED
THEME_CSS = SHARED_ROOT / "html_report" / "themes" / "claude.css"

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", re.DOTALL)

# 三个主力 SKU 的显示顺序：由新到旧，跟代际溢价的读法一致。
ORDER = ["B200", "H200 SXM", "H100 SXM"]
SHORT = {"B200": "B200", "H200 SXM": "H200", "H100 SXM": "H100"}

# 状态语义。颜色只是辅助，文字始终在场（PRD §10）。
# 每块面板底部那行「异动说明」：说今天这块图里什么在动、值得看哪里。
# 这是判断不是取数，所以由模型写在 verdict.panels 里，脚本只搬运 + 查字数。
PANEL_KEYS = ("price", "supply", "quotes", "matrix", "tokens")
PANEL_NOTE_MAX = 100

TONES = {
    "tight": ("偏紧", "t-tight"),
    "watch": ("观察", "t-watch"),
    "loose": ("偏松", "t-loose"),
    "unknown": ("数据不足", "t-unknown"),
}

LAYOUT_CSS = """
:root { --gpu-b200: #b4522f; --gpu-h200: #a07d2c; --gpu-h100: #4f6f7a; }
body { font-size: 14.5px; line-height: 1.6; }
.wrap { width: min(1500px, calc(100vw - 40px)); margin: 0 auto 56px; padding-top: 26px; }
.hd { display: flex; justify-content: space-between; align-items: flex-end; gap: 18px;
      flex-wrap: wrap; margin-bottom: 16px; padding: 0 2px; }
.hd .eyebrow { font-size: 12px; color: var(--ink-4); letter-spacing: .02em; }
.hd h1 { font-family: var(--font-serif); font-size: 25px; font-weight: 700;
         margin: 6px 0 5px; letter-spacing: -.01em; color: var(--ink-1); }
.hd .sub { font-size: 13.5px; color: var(--ink-3); }
.hd .stamp { font-family: var(--font-mono); font-size: 12px; color: var(--ink-4); }
.panel { background: var(--surface); border: 1px solid var(--line-1);
         border-radius: 12px; padding: 16px 18px; box-shadow: 0 1px 2px rgba(43,38,32,.04); }
.stack { margin-bottom: 14px; }
.sec-head { display: flex; justify-content: space-between; align-items: baseline;
            gap: 12px; flex-wrap: wrap; }
.sec-head strong { font-size: 14.5px; color: var(--ink-1); }
.sec-hint { font-size: 11.5px; color: var(--ink-4); font-family: var(--font-mono); }
/* 判断面板 */
.verdict { display: flex; gap: 24px; align-items: flex-start; flex-wrap: wrap; }
.verdict .copy { flex: 1 1 320px; min-width: 300px; }
.verdict h2 { font-family: var(--font-serif); font-size: 20px; line-height: 1.35;
              margin: 9px 0 7px; color: var(--ink-1); }
.verdict .summary { font-size: 13.5px; color: var(--ink-2); max-width: 820px; }
.badge-row { display: flex; gap: 9px; align-items: center; flex-wrap: wrap; }
.badge-row .lbl { font-size: 12px; color: var(--ink-4); }
.badge { font-size: 12px; border: 1px solid currentColor; border-radius: 6px;
         padding: 2px 8px; font-weight: 500; }
.signals { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
           gap: 9px; flex: 0 1 470px; min-width: 300px; }
.sig { border: 1px solid var(--line-1); border-radius: 9px; padding: 11px 12px;
       background: var(--surface-2); }
.sig .name { font-size: 11.5px; color: var(--ink-4); letter-spacing: .03em; }
.sig .state { font-weight: 700; font-size: 15px; margin-top: 5px; }
.sig .why { font-size: 11.5px; color: var(--ink-3); margin-top: 7px; line-height: 1.5; }
.t-tight { color: var(--neg); } .t-watch { color: var(--warn); }
.t-loose { color: var(--pos); } .t-unknown { color: var(--ink-4); }
/* 表格 */
.scroll { overflow-x: auto; margin-top: 11px; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid var(--line-2);
         white-space: nowrap; }
th { color: var(--ink-4); font-weight: 500; font-size: 11.5px; }
tbody tr:last-child td { border-bottom: none; }
td.num { font-family: var(--font-mono); }
td.na, .na { color: var(--ink-4); }
.dim { color: var(--ink-4); font-size: 11.5px; }
.pct-up { color: var(--neg); font-weight: 700; }
.pct-down { color: var(--pos); font-weight: 700; }
.pct-flat { color: var(--ink-4); font-weight: 600; }
.stock-tag { display: inline-flex; align-items: center; min-width: 42px;
             justify-content: center; margin-left: 4px; padding: 2px 7px;
             border-radius: 999px; border: 1px solid currentColor;
             font-family: var(--font-mono); font-size: 10.5px; font-weight: 700;
             line-height: 1.25; letter-spacing: .02em; }
.stock-high { color: var(--pos); background: var(--pos-soft); }
.stock-low { color: var(--neg); background: var(--neg-soft); }
.stock-other { color: var(--warn); background: var(--warn-soft); }
/* 图 */
svg.chart { width: 100%; min-width: 520px; height: auto; display: block; }
svg.chart text { fill: var(--ink-4); font-size: 10.5px; font-family: var(--font-sans); }
.gridline { stroke: var(--line-2); stroke-width: 1; }
.empty { padding: 34px 16px; text-align: center; color: var(--ink-4); font-size: 12.5px;
         border: 1px dashed var(--line-1); border-radius: 9px; margin-top: 11px;
         line-height: 1.7; }
.footnote { font-size: 11.5px; color: var(--ink-4); margin-top: 9px; line-height: 1.6; }
.conf-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
             gap: 9px; margin-top: 11px; }
.conf-cell { border: 1px solid var(--line-1); border-radius: 9px; padding: 10px 12px;
             background: var(--surface-2); }
.conf-cell .name { font-size: 11.5px; color: var(--ink-4); letter-spacing: .03em; }
.conf-cell .state { font-weight: 600; font-size: 13.5px; margin-top: 4px; }
.conf-cell .why { font-size: 11.5px; color: var(--ink-3); margin-top: 6px; line-height: 1.5; }
.panel-note { margin-top: 11px; padding: 9px 12px; border-left: 3px solid var(--clay);
              background: var(--clay-soft); border-radius: 0 7px 7px 0;
              font-size: 12.5px; line-height: 1.65; color: var(--ink-2); }
/* 源状态 */
.srcs { display: grid; grid-template-columns: repeat(auto-fit, minmax(148px, 1fr));
        gap: 9px; margin-top: 12px; }
.src { border: 1px solid var(--line-1); border-radius: 9px; padding: 10px 11px;
       background: var(--surface-2); }
.src b { font-size: 13px; }
.src .st { font-size: 11.5px; margin-top: 4px; }
.src .meta { font-size: 11px; color: var(--ink-4); margin-top: 3px;
             font-family: var(--font-mono); }
.ok { color: var(--pos); } .bad { color: var(--neg); } .warnc { color: var(--warn); }
@media (max-width: 980px) {
  .signals, .conf-grid { grid-template-columns: 1fr; }
  .wrap { width: calc(100vw - 24px); }
}
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def money(value: Optional[float], digits: int = 2) -> str:
    return "—" if value is None else f"${value:,.{digits}f}"


def pct(value: Optional[float], digits: int = 1) -> str:
    return "—" if value is None else f"{value:+.{digits}f}%"


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------
def read_verdict(md_path: Optional[Path]) -> Dict[str, Any]:
    """从报告 Markdown 的 frontmatter 里取模型写的判断块。

    取不到就返回空——仪表盘会把判断区显示成「未提供」，而不是自己编一个。
    """
    if md_path is None or not md_path.exists():
        return {}
    match = FRONTMATTER_RE.match(md_path.read_text(encoding="utf-8"))
    if not match:
        return {}
    meta = yaml.safe_load(match.group(1)) or {}
    return meta.get("verdict") or {}


def find_evidence(evidence_arg: Optional[str], md_path: Optional[Path]) -> Path:
    if evidence_arg:
        return Path(evidence_arg)
    if md_path is not None:
        found = DATE_RE.search(md_path.name)
        if found:
            guess = SKILL_ROOT / "evidence" / f"gpu-{found.group(1)}.json"
            if guess.exists():
                return guess
    raise SystemExit("error: 需要 --evidence（或一个文件名带日期、且证据包已生成的 --input）")


# ---------------------------------------------------------------------------
# 各面板
# ---------------------------------------------------------------------------
def render_verdict(verdict: Dict[str, Any], models: Dict[str, Any]) -> str:
    badge = verdict.get("badge")
    badge_tone = TONES.get(str(verdict.get("badge_tone", "unknown")),
                           TONES["unknown"])[1]
    headline = verdict.get("headline")
    summary = verdict.get("summary")

    if not headline:
        headline = "报告未提供整体判断"
        summary = ("报告 frontmatter 里没有 verdict 块，仪表盘不会替模型编一个结论。"
                   "写法见 references/report_template.md。")

    cards = []
    per_model = verdict.get("models") or {}
    for model in ORDER:
        entry = per_model.get(model) or per_model.get(SHORT.get(model, "")) or {}
        label, cls = TONES.get(str(entry.get("tone", "unknown")), TONES["unknown"])
        state = entry.get("status") or label
        why = entry.get("note")
        if not why:
            block = models.get(model) or {}
            score = (block.get("score") or {})
            why = "评分未出：" + "；".join(score.get("blockers") or ["缺判断依据"])
        cards.append(
            f'<div class="sig"><div class="name">{esc(SHORT.get(model, model))}</div>'
            f'<div class="state {cls}">{esc(state)}</div>'
            f'<div class="why">{esc(why)}</div></div>')

    badge_html = (f'<span class="badge {badge_tone}">{esc(badge)}</span>'
                  if badge else '<span class="badge t-unknown">未定档</span>')
    return f"""<section class="panel stack">
  <div class="verdict">
    <div class="copy">
      <div class="badge-row"><span class="lbl">整体市场判断 / 供需拐点</span>{badge_html}</div>
      <h2>{esc(headline)}</h2>
      <div class="summary">{esc(summary)}</div>
    </div>
    <div class="signals">{''.join(cards)}</div>
  </div>
</section>"""


def panel_note(notes: Dict[str, Any], key: str) -> str:
    """面板底部的异动说明。模型没写就整行不出现，脚本不代笔。"""
    text = (notes or {}).get(key)
    if not text:
        return ""
    return f'<div class="panel-note">{esc(str(text).strip())}</div>'


def over_length_notes(notes: Dict[str, Any]) -> Dict[str, int]:
    """超出 100 字的异动说明。只报不截——截断会把话拦腰砍断。"""
    out = {}
    for key in PANEL_KEYS:
        text = (notes or {}).get(key)
        if text and len(str(text).strip()) > PANEL_NOTE_MAX:
            out[key] = len(str(text).strip())
    return out


def render_confirmation_strip(ev: Dict[str, Any]) -> str:
    """确认型拐点的状态条（PRD §4.3 / §8）。

    这是全项目的落脚点，所以单独占一条：三个型号各自的连续天数、门槛、
    以及差在哪一步。「还没确认」和「确认没有」不是一回事，blockers 要露出来。
    """
    cells = []
    thresholds = None
    for model in ORDER:
        conf = ((ev.get("models") or {}).get(model) or {}).get("confirmation") or {}
        if not conf:
            continue
        thresholds = thresholds or conf.get("thresholds")
        need = (conf.get("thresholds") or {}).get("min_consecutive_collection_days", 10)
        loose = (conf.get("loosening") or {}).get("streak_days", 0)
        tight = (conf.get("tightening") or {}).get("streak_days", 0)
        verdict = conf.get("verdict", "none")
        if verdict == "loosening":
            state, cls = f"确认宽松（连续 {loose} 天）", "t-loose"
        elif verdict == "tightening":
            state, cls = f"确认收紧（连续 {tight} 天）", "t-tight"
        elif conf.get("blockers"):
            state, cls = "信号不足，未判定", "t-unknown"
        else:
            state, cls = f"未确认（最长 {max(loose, tight)}/{need} 天）", "t-watch"
        detail = (conf.get("blockers") or [None])[0] or (
            f"松 {loose} 天 · 紧 {tight} 天 · 门槛 {need} 天")
        cells.append(f'<div class="conf-cell"><div class="name">{esc(SHORT.get(model, model))}</div>'
                     f'<div class="state {cls}">{esc(state)}</div>'
                     f'<div class="why">{esc(detail)}</div></div>')
    if not cells:
        return ""
    t = thresholds or {}
    rule = (f"价格类 ≥{t.get('min_price_signals', 3)} 个信号 + "
            f"供给类 ≥{t.get('min_supply_signals', 2)} 个信号，"
            f"连续 ≥{t.get('min_consecutive_collection_days', 10)} 个采集日，"
            f"中间不许有缺口")
    return f"""<section class="panel stack">
  <div class="sec-head">
    <div><strong>拐点确认</strong></div>
  </div>
  <div class="conf-grid">{''.join(cells)}</div>
</section>"""


def line_chart(series: List[Dict[str, Any]], *, unit_prefix: str,
               colors: Dict[str, str], digits: int = 2) -> str:
    """多序列折线。少于 2 个点的序列不画线，也不补值。"""
    live = [s for s in series if len(s.get("points") or []) >= 2]
    missing = [s for s in series if len(s.get("points") or []) < 2]
    if not live:
        names = "、".join(s["label"] for s in missing) or "全部序列"
        return (f'<div class="empty">暂无数据：{esc(names)}在窗口内不足 2 个观测点。'
                f'<br>不画线，也不用前值补齐。</div>')

    # 面板现在独占一整行，viewBox 必须跟着加宽。SVG 是等比缩放的：
    # 拿 760 宽的画布铺到 1400px 上，10.5px 的字会被放成约 19px。
    W, H = 1440, 320
    L, R, T, B = 56, 60, 16, 30
    days = sorted({p["date"] for s in live for p in s["points"]})
    values = [p["value"] for s in live for p in s["points"]]
    lo, hi = min(values), max(values)
    if hi == lo:
        hi, lo = hi + 1, max(0.0, lo - 1)
    span = hi - lo
    lo -= span * 0.10
    hi += span * 0.10
    # 全是非负值时下界不许跌破 0：量指数这类序列跨度大，10% 的留白会把坐标轴
    # 拉出一个 -93 的刻度，读起来像是这个指数可以为负。
    if min(values) >= 0:
        lo = max(lo, 0.0)

    def x_of(day: str) -> float:
        i = days.index(day)
        return L + (0 if len(days) < 2 else i / (len(days) - 1) * (W - L - R))

    def y_of(value: float) -> float:
        return T + (1 - (value - lo) / (hi - lo)) * (H - T - B)

    parts = []
    for i in range(5):
        y = T + i * (H - T - B) / 4
        v = hi - i * (hi - lo) / 4
        parts.append(f'<line class="gridline" x1="{L}" y1="{y:.1f}" '
                     f'x2="{W - R}" y2="{y:.1f}"/>')
        parts.append(f'<text x="{L - 6}" y="{y + 3.5:.1f}" text-anchor="end">'
                     f'{unit_prefix}{v:.{digits}f}</text>')
    ticks = 6 if len(days) >= 12 else 3
    idxs = sorted({round(i * (len(days) - 1) / (ticks - 1)) for i in range(ticks)})
    for idx in idxs:
        day = days[idx]
        anchor = ("start" if idx == 0
                  else "end" if idx == len(days) - 1 else "middle")
        parts.append(f'<text x="{x_of(day):.1f}" y="{H - 9}" '
                     f'text-anchor="{anchor}">{esc(day[5:])}</text>')
    for s in live:
        color = colors.get(s["name"], "var(--clay)")
        path = " ".join(
            ("M" if i == 0 else "L") + f"{x_of(p['date']):.1f} {y_of(p['value']):.1f}"
            for i, p in enumerate(s["points"]))
        last = s["points"][-1]
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" '
                     f'stroke-width="2" stroke-linejoin="round"/>')
        parts.append(f'<circle cx="{x_of(last["date"]):.1f}" '
                     f'cy="{y_of(last["value"]):.1f}" r="3" fill="{color}"/>')
        parts.append(f'<text x="{W - R + 6}" y="{y_of(last["value"]) + 3.5:.1f}" '
                     f'fill="{color}" style="font-weight:600">'
                     f'{esc(SHORT.get(s["name"], s["name"]))}</text>')

    chart = (f'<div class="scroll"><svg class="chart" viewBox="0 0 {W} {H}" '
             f'role="img" aria-label="90 天趋势">{"".join(parts)}</svg></div>')
    if missing:
        names = "、".join(SHORT.get(s["name"], s["name"]) for s in missing)
        chart += (f'<div class="footnote">暂无数据：{esc(names)}观测点不足 2 个，'
                  f'未画线也未补值。</div>')
    return chart


def bar_chart(series: List[Dict[str, Any]], *, unit_prefix: str,
              colors: Dict[str, str], digits: int = 0) -> str:
    """多序列按日期堆叠成单柱。只画真实观测点，缺日不补值。"""
    live = [s for s in series if len(s.get("points") or []) >= 2]
    missing = [s for s in series if len(s.get("points") or []) < 2]
    if not live:
        names = "、".join(s["label"] for s in missing) or "全部序列"
        return (f'<div class="empty">暂无数据：{esc(names)}在窗口内不足 2 个观测点。'
                f'<br>不画柱，也不用前值补齐。</div>')

    W, H = 1440, 320
    L, R, T, B = 56, 28, 34, 30
    days = sorted({p["date"] for s in live for p in s["points"]})
    values = [float(p["value"]) for s in live for p in s["points"]]
    point_maps = {s["name"]: {p["date"]: float(p["value"])
                               for p in s["points"]} for s in live}
    daily_totals = [sum(points.get(day, 0.0) for points in point_maps.values())
                    for day in days]
    hi = max(100.0, max(daily_totals))

    def y_of(value: float) -> float:
        return T + (1 - value / hi) * (H - T - B)

    parts = []
    for i in range(5):
        y = T + i * (H - T - B) / 4
        value = hi - i * hi / 4
        parts.append(f'<line class="gridline" x1="{L}" y1="{y:.1f}" '
                     f'x2="{W - R}" y2="{y:.1f}"/>')
        parts.append(f'<text x="{L - 6}" y="{y + 3.5:.1f}" text-anchor="end">'
                     f'{unit_prefix}{value:.{digits}f}</text>')

    plot_w = W - L - R
    group_w = plot_w / max(1, len(days))
    bar_w = max(3.0, min(group_w * 0.52, 72.0))
    for day_idx, day in enumerate(days):
        center = L + (day_idx + 0.5) * group_w
        stacked = 0.0
        for s in live:
            value = point_maps[s["name"]].get(day)
            if value is None:
                continue
            x = center - bar_w / 2
            y = y_of(stacked + value)
            height = max(1.0, y_of(stacked) - y)
            color = colors.get(s["name"], "var(--clay)")
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                         f'height="{height:.1f}" rx="1.5" fill="{color}" fill-opacity=".82"/>')
            stacked += value

    ticks = 6 if len(days) >= 12 else min(3, len(days))
    idxs = ([0] if ticks == 1 else
            sorted({round(i * (len(days) - 1) / (ticks - 1)) for i in range(ticks)}))
    for idx in idxs:
        day = days[idx]
        x = L + (idx + 0.5) * group_w
        anchor = "start" if idx == 0 else "end" if idx == len(days) - 1 else "middle"
        parts.append(f'<text x="{x:.1f}" y="{H - 9}" text-anchor="{anchor}">'
                     f'{esc(day[5:])}</text>')

    legend_x = L
    for s in live:
        color = colors.get(s["name"], "var(--clay)")
        label = SHORT.get(s["name"], s["name"])
        parts.append(f'<rect x="{legend_x}" y="10" width="10" height="10" rx="2" fill="{color}"/>')
        parts.append(f'<text x="{legend_x + 15}" y="19" style="font-weight:600">{esc(label)}</text>')
        legend_x += 88

    chart = (f'<div class="scroll"><svg class="chart" viewBox="0 0 {W} {H}" '
             f'role="img" aria-label="90 天可用供给堆叠柱状趋势">{"".join(parts)}</svg></div>')
    if missing:
        names = "、".join(SHORT.get(s["name"], s["name"]) for s in missing)
        chart += (f'<div class="footnote">暂无数据：{esc(names)}观测点不足 2 个，'
                  f'未画柱也未补值。</div>')
    return chart


def signed_pct(value: float) -> str:
    """报价变化比例：正值红、负值绿，文字正负号始终保留。"""
    cls = "pct-up" if value > 0 else "pct-down" if value < 0 else "pct-flat"
    return f'<span class="{cls}"> {value:+.0f}%</span>'


def stock_badge(value: Any) -> str:
    """库存档位必须同时用文字和高亮标签表达。"""
    label = str(value or "").strip()
    if not label:
        return '<span class="na">暂无数据</span>'
    normalized = label.lower()
    cls = "stock-high" if normalized == "high" else "stock-low" if normalized == "low" else "stock-other"
    return f'<span class="stock-tag {cls}">{esc(label)}</span>'


def render_price_panel(ev: Dict[str, Any], notes: Dict[str, Any]) -> str:
    colors = {"B200": "var(--gpu-b200)", "H200 SXM": "var(--gpu-h200)",
              "H100 SXM": "var(--gpu-h100)"}
    series = []
    for model in ORDER:
        block = (ev.get("models") or {}).get(model) or {}
        node = ((block.get("by_source") or {}).get("ornn") or {}).get(
            "transaction_index") or {}
        series.append({"name": model, "label": block.get("label", model),
                       "points": node.get("series") or []})
    body = line_chart(series, unit_prefix="$", colors=colors)

    refs = ev.get("reference_models") or {}
    if refs:
        bits = "　·　".join(
            f"{esc(m)} {pct(b.get('window_change_pct'))}" for m, b in refs.items())
        body += f'<div class="footnote">参照系 90D　{bits}</div>' 

    anchor = ""
    first = (ev.get("models") or {}).get(ORDER[0]) or {}
    cross = first.get("cross_platform_median") or {}
    if cross.get("anchor_date"):
        lag = cross.get("anchor_lags_raw_latest_days")
        anchor = f"锚定 {cross['anchor_date']}" + (f"（比日历日晚 {lag} 天）" if lag else "")
    return f"""<section class="panel">
  <div class="sec-head">
    <div><strong>市场成交价趋势</strong></div>
    <div class="sec-hint">{esc(anchor)}</div>
  </div>
  {body}
  {panel_note(notes, "price")}
</section>"""


def render_supply_panel(ev: Dict[str, Any], notes: Dict[str, Any]) -> str:
    colors = {"B200": "var(--gpu-b200)", "H200 SXM": "var(--gpu-h200)",
              "H100 SXM": "var(--gpu-h100)"}
    series = []
    for model in ORDER:
        block = (ev.get("models") or {}).get(model) or {}
        supply = block.get("supply") or {}
        parts = []
        for key in ("offer_share", "available_gpu_count"):
            raw = (supply.get(key) or {}).get("series") or []
            if len(raw) >= 2:
                parts.append({p["date"]: float(p["value"]) for p in raw})
        merged: Dict[str, List[float]] = {}
        for part in parts:
            vals = list(part.values())
            lo, hi = min(vals), max(vals)
            for day, value in part.items():
                merged.setdefault(day, []).append(
                    50.0 if hi == lo else (value - lo) / (hi - lo) * 100.0)
        series.append({"name": model, "label": block.get("label", model),
                       "points": [{"date": d, "value": round(sum(v) / len(v), 2)}
                                  for d, v in sorted(merged.items())]})
    bits = []
    for model in ORDER:
        br = ((ev.get("models") or {}).get(model) or {}).get("supply_breadth") or {}
        if br.get("breadth") is None:
            bits.append(f"{SHORT.get(model, model)} 暂无数据")
        else:
            bits.append(f"{SHORT.get(model, model)} {br['with_stock']}/{br['reporting']} 家有货")
    breadth_line = f'<div class="footnote">供给广度　{esc("　·　".join(bits))}</div>' 
    body = bar_chart(series, unit_prefix="", colors=colors, digits=0)
    if all(len(s["points"]) < 2 for s in series):
        body = ('<div class="empty">暂无数据：供给序列还不足 2 个观测点。<br>'
                'Vast 与 Runpod 都没有历史接口，只回当下快照，'
                '所以这条曲线只能从首次采集当天往后长，补不回去。</div>')
    return f"""<section class="panel">
  <div class="sec-head">
    <div><strong>可用供给趋势</strong></div>
  </div>
  {body}
  {breadth_line}
  {panel_note(notes, "supply")}
</section>"""


def render_market_quotes(ev: Dict[str, Any], notes: Dict[str, Any]) -> str:
    rows = []
    for model in ORDER:
        block = (ev.get("models") or {}).get(model) or {}
        by_source = block.get("by_source") or {}
        supply = block.get("supply") or {}
        vast = by_source.get("vast") or {}

        def val(key: str) -> Optional[float]:
            node = vast.get(f"{key}@on_demand") or {}
            return (node.get("latest") or {}).get("value")

        sample = ((vast.get("offer_median@on_demand") or {}).get("sample_count")
                  or (vast.get("offer_min@on_demand") or {}).get("sample_count"))
        thin = val("offer_median") is None and val("offer_min") is not None
        gpus = (supply.get("available_gpu_count") or {}).get("latest") or {}
        if vast:
            disp = next((d for d in (block.get("quote_dispersion") or [])
                         if d.get("source") == "vast" and d.get("spread") is not None), None)
            disp_cell = (f'<td class="num">{money(disp["spread"])}'
                         f'<span class="dim"> {disp["spread_pct_of_median"]:.0f}%</span></td>'
                         if disp else '<td class="na">—</td>')
            quant = ('<td class="na" colspan="3">样本不足</td>' if thin else
                     f'<td class="num">{money(val("offer_p25"))}</td>'
                     f'<td class="num">{money(val("offer_median"))}</td>'
                     f'<td class="num">{money(val("offer_p75"))}</td>')
            rows.append(
                f'<tr><td>Vast.ai</td><td>{esc(SHORT.get(model, model))}</td>'
                f'<td class="num">{money(val("offer_min"))}</td>{quant}{disp_cell}'
                f'<td class="num">{esc(sample or "—")}</td>'
                f'<td class="dim">{esc(int(gpus["value"]) if gpus.get("value") is not None else "—")} 张</td></tr>')

        runpod = by_source.get("runpod") or {}
        low = (runpod.get("on_demand@lowest") or {}).get("latest") or {}
        stock = (supply.get("stock_status") or {}).get("latest")
        if low:
            rows.append(
                f'<tr><td>Runpod</td><td>{esc(SHORT.get(model, model))}</td>'
                f'<td class="num">{money(low.get("value"))}</td>'
                f'<td class="na" colspan="3">不提供分位数</td>'
                f'<td class="na">—</td><td class="na">—</td>'
                f'<td class="dim">库存 {stock_badge(stock)}</td></tr>')

    table = ("".join(rows) or
             '<tr><td class="na" colspan="9">暂无数据：今日没有采到市场化报价</td></tr>')
    return f"""<section class="panel">
  <div class="sec-head">
    <div><strong>市场报价</strong></div>
  </div>
  <div class="scroll"><table>
    <thead><tr><th>来源</th><th>GPU</th><th>Min</th><th>P25</th><th>中位</th>
      <th>P75</th><th>分散度</th><th>样本</th><th>供给</th></tr></thead>
    <tbody>{table}</tbody>
  </table></div>
  {panel_note(notes, "quotes")}
</section>"""


def render_standard_matrix(ev: Dict[str, Any], notes: Dict[str, Any]) -> str:
    """Provider × (GPU × OD/Spot) 矩阵。缺失明确显示，不留空白。"""
    providers: Dict[str, Dict[str, Dict[str, Any]]] = {}
    labels = {"coreweave": "CoreWeave", "nebius": "Nebius", "runpod": "Runpod"}
    for model in ORDER:
        block = (ev.get("models") or {}).get(model) or {}
        for source, per_type in (block.get("by_source") or {}).items():
            if source not in labels:
                continue
            slot = providers.setdefault(source, {}).setdefault(model, {})
            for key, node in per_type.items():
                base = key.split("@")[0].split("#")[0]
                value = (node.get("latest") or {}).get("value")
                if value is None:
                    continue
                if base == "on_demand":
                    # 同一源多个 segment / region 时取最低的一档作为公开挂牌代表值
                    slot["od"] = min(slot.get("od", value), value)
                elif base in ("spot", "preemptible"):
                    slot["spot"] = min(slot.get("spot", value), value)

    head = "".join(f'<th>{esc(SHORT.get(m, m))} OD</th><th>{esc(SHORT.get(m, m))} Spot</th>'
                   for m in ORDER)
    rows = []
    for source in ("coreweave", "nebius", "runpod"):
        per_model = providers.get(source)
        if not per_model:
            continue
        cells = []
        for model in ORDER:
            slot = per_model.get(model) or {}
            od, spot = slot.get("od"), slot.get("spot")
            cells.append(f'<td class="num">{money(od)}</td>' if od is not None
                         else '<td class="na">暂无</td>')
            if spot is not None and od:
                cells.append(f'<td class="num">{money(spot)}'
                             f'{signed_pct((spot / od - 1) * 100)}</td>')
            else:
                cells.append('<td class="na">暂无</td>')
        rows.append(f'<tr><td>{esc(labels[source])}</td>{"".join(cells)}</tr>')

    body = ("".join(rows) or
            f'<tr><td class="na" colspan="{1 + 2 * len(ORDER)}">'
            f'暂无数据：标准报价层今天没有可用条目</td></tr>')
    stale = [h["source"] for h in (ev.get("source_health") or [])
             if h.get("mode") == "attested" and not h.get("fresh")]
    # 口径说明去掉了，但「人工核对已过期」是状态警告不是说明，必须留在页面上
    stale_hint = (f'<div class="sec-hint bad">{esc("、".join(stale))} 已过期需重新核对</div>'
                  if stale else "")
    return f"""<section class="panel">
  <div class="sec-head">
    <div><strong>标准报价矩阵</strong></div>
    {stale_hint}
  </div>
  <div class="scroll"><table>
    <thead><tr><th>Provider</th>{head}</tr></thead>
    <tbody>{body}</tbody>
  </table></div>
  {panel_note(notes, "matrix")}
</section>"""


def tokens_fmt(value: Optional[float], digits: int = 2) -> str:
    """token 数按 T / B / M 显示。原始数字有 13 位，摆出来没人读得动。"""
    if value is None:
        return '<span class="na">暂无数据</span>'
    value = float(value)
    for scale, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= scale:
            return f"{value / scale:.{digits}f}{unit}"
    return f"{value:.0f}"


def usd_fmt(value: Optional[float]) -> str:
    if value is None:
        return '<span class="na">暂无数据</span>'
    value = float(value)
    if abs(value) >= 1e6:
        return f"${value / 1e6:.2f}M"
    if abs(value) >= 1e3:
        return f"${value / 1e3:.1f}K"
    return f"${value:.2f}"


def _win(block: Dict[str, Any], label: str = "7d") -> str:
    """一个变化率的展示：不可用就说清为什么，不给近似值。"""
    entry = ((block or {}).get("changes") or {}).get(label) or {}
    if entry.get("pct") is None or not entry.get("usable"):
        reason = entry.get("reason") or "窗口起点无观测"
        return f'<span class="na">{esc(label.upper())} 不可用（{esc(reason)}）</span>'
    return f'{esc(label.upper())} {pct(entry.get("pct"))}'


def _rebased(series: List[Dict[str, Any]], base_date: Optional[str]) -> List[Dict[str, Any]]:
    """把一条水平序列换算成 base=100 的指数，好和固定篮子指数放进同一张图。"""
    points = {p["date"]: p["value"] for p in series or []}
    if not base_date or base_date not in points or not points[base_date]:
        return []
    base = points[base_date]
    return [{"date": d, "value": v / base * 100.0} for d, v in sorted(points.items())]


# 构成图的条带配色。前 7 条给模型，最后一条固定给「其他」——
# 用最灰的那个色，免得长尾在视觉上比主力还抢眼。
BAND_COLORS = ["var(--clay)", "var(--blue)", "var(--cyan)", "var(--violet)",
               "var(--orange)", "var(--pos)", "var(--warn)"]
OTHER_COLOR = "var(--ink-4)"


def band_color(index: int, key: str) -> str:
    if key == "__other__":
        return OTHER_COLOR
    return BAND_COLORS[index % len(BAND_COLORS)]


def stacked_area(comp: Dict[str, Any]) -> str:
    """堆叠面积图：每条带子是一个模型，叠起来的高度就是当日总量。"""
    series = comp.get("series") or []
    bands = comp.get("bands") or []
    if len(series) < 2:
        return ""
    W, H = 1440, 340
    L, R, T, B = 66, 24, 16, 30
    days = [p["date"] for p in series]
    top = max(p["total"] for p in series) or 1
    hi = top * 1.08

    def x_of(i: int) -> float:
        return L + (0 if len(days) < 2 else i / (len(days) - 1) * (W - L - R))

    def y_of(value: float) -> float:
        return T + (1 - value / hi) * (H - T - B)

    parts = []
    for i in range(5):
        y = T + i * (H - T - B) / 4
        v = hi - i * hi / 4
        parts.append(f'<line class="gridline" x1="{L}" y1="{y:.1f}" '
                     f'x2="{W - R}" y2="{y:.1f}"/>')
        parts.append(f'<text x="{L - 6}" y="{y + 3.5:.1f}" text-anchor="end">'
                     f'{v / 1e12:.1f}T</text>')
    ticks = 6 if len(days) >= 12 else min(3, len(days))
    idxs = sorted({round(i * (len(days) - 1) / max(ticks - 1, 1))
                   for i in range(ticks)})
    for idx in idxs:
        anchor = ("start" if idx == 0
                  else "end" if idx == len(days) - 1 else "middle")
        parts.append(f'<text x="{x_of(idx):.1f}" y="{H - 9}" '
                     f'text-anchor="{anchor}">{esc(days[idx][5:])}</text>')

    floor = [0.0] * len(series)
    for bi, band in enumerate(bands):
        key = band["key"]
        tops = [floor[i] + (p["values"].get(key) or 0) for i, p in enumerate(series)]
        upper = " ".join(("M" if i == 0 else "L") + f"{x_of(i):.1f} {y_of(v):.1f}"
                         for i, v in enumerate(tops))
        lower = " ".join(f"L{x_of(i):.1f} {y_of(v):.1f}"
                         for i in range(len(series) - 1, -1, -1)
                         for v in [floor[i]])
        parts.append(f'<path d="{upper} {lower} Z" fill="{band_color(bi, key)}" '
                     f'fill-opacity="0.85" stroke="none"/>')
        floor = tops
    return (f'<div class="scroll"><svg class="chart" viewBox="0 0 {W} {H}" '
            f'role="img" aria-label="日度 token 构成堆叠面积图">{"".join(parts)}</svg></div>')


def stacked_bar(comp: Dict[str, Any]) -> str:
    """只有一天时的退路：横向 100% 堆叠条，照样把构成摆出来。

    面积图需要至少两天才有"面积"。序列只有一天就画一条线是自欺欺人，
    但构成本身当天就成立——所以换成条，而不是显示"暂无数据"。
    """
    series = comp.get("series") or []
    bands = comp.get("bands") or []
    if not series:
        return ""
    latest = series[-1]
    total = latest["total"] or 1
    W, H = 1440, 62
    x = 0.0
    parts = []
    for bi, band in enumerate(bands):
        value = latest["values"].get(band["key"]) or 0
        width = value / total * W
        if width <= 0:
            continue
        parts.append(f'<rect x="{x:.1f}" y="0" width="{width:.1f}" height="34" '
                     f'fill="{band_color(bi, band["key"])}" fill-opacity="0.85"/>')
        if width > 62:
            parts.append(f'<text x="{x + width / 2:.1f}" y="52" text-anchor="middle">'
                         f'{value / total * 100:.1f}%</text>')
        x += width
    return (f'<div class="scroll"><svg class="chart" viewBox="0 0 {W} {H}" '
            f'style="min-width:520px" role="img" '
            f'aria-label="当日 token 构成堆叠条">{"".join(parts)}</svg></div>')


def composition_table(comp: Dict[str, Any]) -> str:
    rows = []
    for bi, band in enumerate(comp.get("bands") or []):
        swatch = (f'<span style="display:inline-block;width:10px;height:10px;'
                  f'border-radius:2px;background:{band_color(bi, band["key"])};'
                  f'margin-right:7px"></span>')
        variant = band.get("variant")
        tag = (f' <span class="dim">:{esc(variant)}</span>'
               if variant and variant != "standard" else "")
        if band["key"] == "__other__":
            name = f'其他 <span class="dim">（{band.get("model_count", 0)} 个模型）</span>'
            priced = '<span class="na">混合</span>'
        else:
            name = esc(band["label"]) + tag
            priced = ('有价' if band.get("is_priced")
                      else '<span class="warnc">零价</span>')
        rows.append(
            f'<tr><td>{swatch}{name}</td>'
            f'<td class="num">{(band.get("share") or 0) * 100:.1f}%</td>'
            f'<td class="num">{tokens_fmt(band.get("tokens"))}</td>'
            f'<td class="num">{(band.get("requests") or 0):,}</td>'
            f'<td class="num">{tokens_fmt(band.get("tokens_per_request"), 1)}</td>'
            f'<td class="num">{usd_fmt(band.get("spend_usd"))}</td>'
            f'<td>{priced}</td></tr>')
    return (f'<div class="scroll"><table><thead><tr><th>模型</th><th>份额</th>'
            f'<th>token</th><th>调用次数</th><th>tok/次</th><th>名义 spend</th>'
            f'<th>计价</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def render_composition(comp: Dict[str, Any]) -> str:
    if not comp.get("usable"):
        reason = comp.get("reason") or "证据包里没有构成数据"
        return (f'<div class="empty">暂无数据：{esc(reason)}。</div>')
    days = len(comp.get("series") or [])
    chart = stacked_area(comp) if days >= 2 else stacked_bar(comp)
    rank_label = "token 量" if comp.get("ranked_by") == "tokens" else "调用次数"
    total = comp.get("total_tokens_anchor")
    lead = (f'日度总量构成　按{rank_label}取前 {comp.get("top_n")} 名，其余归「其他」'
            f'　·　全站 {tokens_fmt(total)} tokens　·　'
            f'{comp.get("model_count_anchor")} 个模型 × 变体'
            + ('' if days >= 2 else '　·　序列仅 1 天，暂以堆叠条呈现'))
    tail = '<b>「零价」那几条不产生任何收入</b>，别把它们算进需求强度'
    return (f'<div class="footnote">{lead}</div>{chart}'
            f'{composition_table(comp)}<div class="footnote">{tail}</div>')


def render_token_panel(ev: Dict[str, Any], notes: Dict[str, Any],
                       verdict: Dict[str, Any]) -> str:
    tok = ev.get("token_market") or {}
    if not tok.get("usable"):
        reason = tok.get("reason") or "证据包里没有 token_market 段"
        return f"""<section class="panel">
  <div class="sec-head">
    <div><strong>推理需求：token 量与价</strong></div>
  </div>
  <div class="empty">暂无数据：{esc(reason)}。<br>不用前值冒充，也不画空图。</div>
  {panel_note(notes, "tokens")}
</section>"""

    price = tok.get("price") or {}
    volume = tok.get("volume") or {}
    spend = tok.get("spend") or {}
    cov = tok.get("coverage") or {}
    conc = tok.get("concentration") or {}
    basket = price.get("basket") or {}

    mix7 = (price.get("mix_shift") or {}).get("7d") or {}
    mix_value = (f'{mix7["pct_points"]:+.1f}pp' if mix7.get("pct_points") is not None
                 else '<span class="na">不可用</span>')
    mix_why = (mix7.get("meaning") if mix7.get("pct_points") is not None
               else mix7.get("reason") or "序列不足")

    cells = [
        ("付费 token / 日", tokens_fmt((volume.get("paid") or {}).get("latest", {}).get("value")
                                       if (volume.get("paid") or {}).get("latest") else None),
         _win(volume.get("paid"))),
        ("名义 spend / 日", usd_fmt(((spend.get("nominal_usd_per_day") or {}).get("latest") or {})
                                    .get("value")),
         _win(spend.get("nominal_usd_per_day"))),
        ("混合价（当期权重）",
         (f'${(price.get("blended") or {}).get("latest", {}).get("value", 0):.3f}/Mtok'
          if (price.get("blended") or {}).get("latest") else '<span class="na">暂无</span>'),
         _win(price.get("blended"))),
        ("固定篮子真价格",
         (f'{(price.get("laspeyres") or {}).get("latest", {}).get("value", 0):.1f}'
          if (price.get("laspeyres") or {}).get("latest") else '<span class="na">暂无</span>'),
         _win(price.get("laspeyres"))),
        ("结构迁移贡献 7D", mix_value, esc(str(mix_why))),
    ]
    grid = "".join(
        f'<div class="conf-cell"><div class="name">{esc(name)}</div>'
        f'<div class="state">{value}</div><div class="why">{why}</div></div>'
        for name, value, why in cells)

    base_date = basket.get("base_date")
    price_series = [
        {"name": "混合价", "label": "混合价（当期权重）",
         "points": _rebased((price.get("blended") or {}).get("series"), base_date)},
        {"name": "固定篮子", "label": "固定篮子真价格",
         "points": (price.get("laspeyres") or {}).get("series") or []},
    ]
    price_chart = line_chart(price_series, unit_prefix="",
                             colors={"混合价": "var(--gpu-b200)",
                                     "固定篮子": "var(--gpu-h100)"}, digits=1)
    volume_chart = render_composition(tok.get("composition") or {})

    rows = []
    for key, label, note in (
            ("paid", "付费 token", "进 spend 与混合价的那一条，主轴"),
            ("free_variant", "free 变体", "明码零价的免费档"),
            ("zero_priced_standard", "零价 standard", "主体是匿名 stealth 模型免费放量")):
        block = volume.get(key) or {}
        latest = (block.get("latest") or {}).get("value")
        rows.append(
            f'<tr><td>{esc(label)}</td><td class="num">{tokens_fmt(latest)}</td>'
            f'<td class="num">{_win(block, "1d")}</td>'
            f'<td class="num">{_win(block, "7d")}</td>'
            f'<td class="num">{_win(block, "30d")}</td>'
            f'<td class="dim">{esc(note)}</td></tr>')
    table = (f'<div class="scroll"><table><thead><tr><th>量的分层</th><th>最新</th>'
             f'<th>1D</th><th>7D</th><th>30D</th><th>说明</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>')

    hist = tok.get("history") or {}
    history_html = ""
    if hist.get("usable"):
        vi = hist.get("volume_index") or {}
        se = hist.get("structure_effect_index") or {}
        vol_chart = line_chart(
            [{"name": "量指数", "label": "厂商级周度量指数",
              "points": vi.get("series") or []}],
            unit_prefix="", colors={"量指数": "var(--gpu-h200)"}, digits=0)
        se_chart = line_chart(
            [{"name": "结构效应", "label": "结构效应指数",
              "points": se.get("series") or []}],
            unit_prefix="", colors={"结构效应": "var(--gpu-b200)"}, digits=1)
        shares = hist.get("author_shares") or {}
        def _top(block, n=4):
            items = sorted((block or {}).items(), key=lambda kv: -kv[1])[:n]
            return "、".join(f"{k} {v * 100:.1f}%" for k, v in items)
        g4 = (vi.get("growth_4w") or {}).get("pct")
        g13 = (vi.get("growth_13w") or {}).get("pct")
        se_latest = (se.get("series") or [{}])[-1].get("value")
        se_line = (f'结构效应指数 {se_latest:.1f}　·　<b>买家往便宜厂商迁移，'
                   f'把均价拉低了 {100 - se_latest:.0f}%</b>'
                   if se.get("usable") and se_latest else
                   f'结构效应指数不可用：{esc(str(se.get("reason")))}')
        history_html = f"""
  <div class="sec-head" style="margin-top:18px">
    <div><strong>一年结构史</strong></div>
  </div>
  <div class="footnote">量指数　近 4 周 {pct(g4)}　·　近 13 周 {pct(g13)}</div>
  {vol_chart}
  <div class="footnote">{se_line}</div>
  {se_chart}
  <div class="footnote">份额搬家　{esc(str(shares.get("first_week")))}：{esc(_top(shares.get("first")))}
    　→　{esc(str(shares.get("latest_week")))}：{esc(_top(shares.get("latest")))}</div>"""

    cache = spend.get("cache_sensitivity") or []
    cache_bits = "、".join(
        f'命中率 {int(c["assumed_cache_hit_rate"] * 100)}% → 高估 {c["nominal_overstatement_pct"]:.0f}%'
        for c in cache if c.get("nominal_overstatement_pct") is not None)
    band = price.get("provider_band") or {}
    band_bit = (f'默认价 → 各 provider 中位 {band["median_ratio"]:.3f}x'
                f'（最低 {band["min_ratio"]:.3f}x、最高 {band["max_ratio"]:.3f}x，'
                f'覆盖 {band["covered_spend_share"] * 100:.0f}% 的 prompt 支出）'
                if band.get("median_ratio") else "逐 provider 价差带暂无数据")

    # 只留「发现」与「证据溯源」两类。口径说明（名义支出、样本偏斜、
    # reasoning 拆不了、默认价的 provider 价差带）不在仪表盘上讲——
    # 它们的归属是 references/token_taxonomy.md 与报告正文。
    caveats = []
    if conc.get("concentration_warning"):
        caveats.append(
            f'<b>集中度告警</b>　{esc(str(conc.get("top_model")))} 一家占当日 token '
            f'{(conc.get("top_model_share") or 0) * 100:.1f}%，超过 '
            f'{(conc.get("warn_line") or 0) * 100:.0f}% 的守卫线')
    caveats.append(
        f'覆盖率 {(cov.get("matched_token_share") or 0) * 100:.2f}%　·　'
        f'篮子 {basket.get("member_count", "—")} 个家族　·　基期 {esc(str(base_date))}　·　'
        f'指纹 {esc(str(basket.get("fingerprint", "—")))}')
    footnotes = "".join(f'<div class="footnote">{c}</div>' for c in caveats)

    entry = (verdict or {}).get("token") or {}
    chip = ""
    if entry.get("status"):
        label, cls = TONES.get(str(entry.get("tone", "unknown")), TONES["unknown"])
        chip = (f'<div class="conf-cell"><div class="name">模型判断</div>'
                f'<div class="state {cls}">{esc(entry["status"])}</div>'
                f'<div class="why">{esc(entry.get("note") or label)}</div></div>')

    anchor = tok.get("anchor_date")
    lag = tok.get("alignment_lag_days")
    align = ("与 GPU 侧同日" if lag == 0 else
             f"与 GPU 锚定日差 {lag} 天" if lag is not None else "GPU 锚定日缺失")
    days = tok.get("series_days") or 0
    start_hint = (f'　·　序列自 {esc(str(tok.get("series_start")))} 起累积，共 {days} 天'
                  if days < 7 else "")
    return f"""<section class="panel">
  <div class="sec-head">
    <div><strong>推理需求：token 量与价</strong></div>
    <div class="sec-hint">锚定 {esc(str(anchor))}（{esc(align)}）{start_hint}</div>
  </div>
  <div class="conf-grid">{grid}{chip}</div>
  {price_chart}
  {volume_chart}
  {table}
  {history_html}
  {footnotes}
  {panel_note(notes, "tokens")}
</section>"""


def render_sources(ev: Dict[str, Any]) -> str:
    cards = []
    for row in ev.get("source_health") or []:
        status = row.get("status")
        if status == "ok":
            cls, text = "ok", "● 已采集"
        elif status == "empty":
            cls, text = "warnc", "○ 无数据行"
        else:
            cls, text = "bad", "● 采集失败"
        if not row.get("fresh"):
            text += "（不新鲜）"
        meta = f"{row.get('price_rows') or 0}P / {row.get('supply_rows') or 0}S"
        # 需求端的源一行价格都没有，只有 token 行；不显示出来会看着像空跑
        if row.get("token_rows"):
            meta += f" / {row['token_rows']}T"
        if row.get("age_days") is not None:
            meta += "　今日" if row["age_days"] == 0 else f"　{row['age_days']}d 前"
        cards.append(
            f'<div class="src"><b>{esc(row.get("source"))}</b>'
            f'<span class="dim"> {esc(row.get("priority") or "")}</span>'
            f'<div class="st {cls}">{esc(text)}</div>'
            f'<div class="meta">{esc(meta)}</div></div>')
    body = "".join(cards) or '<div class="empty">暂无采集记录</div>'
    return f"""<section class="panel">
  <div class="sec-head">
    <div><strong>数据源与采集状态</strong>
      </div>
    <div class="sec-hint">P = 价格行 / S = 供给行 / T = token 行</div>
  </div>
  <div class="srcs">{body}</div>
</section>"""


# ---------------------------------------------------------------------------
def build_html(ev: Dict[str, Any], verdict: Dict[str, Any]) -> str:
    theme = THEME_CSS.read_text(encoding="utf-8") if THEME_CSS.exists() else ""
    asof = ev.get("asof", "—")
    window = ev.get("window_days", 90)
    health = ev.get("source_health") or []
    ok = sum(1 for h in health if h.get("status") in ("ok", "empty"))
    stamp = (ev.get("generated_at") or
             datetime.now(timezone.utc).isoformat())[:16].replace("T", " ")
    models = ev.get("models") or {}
    notes = verdict.get("panels") or {}
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPU 算力价格与供给监控 · {esc(asof)}</title>
<style>{theme}{LAYOUT_CSS}</style>
</head>
<body>
<main class="wrap">
  <header class="hd">
    <div>
      <div class="eyebrow">全型号总览 · 最近 {esc(window)} 天 · 不设型号与时间切换</div>
      <h1>GPU Compute Price &amp; Supply Monitor</h1>
      <div class="sub">成交价 × 市场报价 × 标准报价 × 可用供给 × 推理 token 量价 → 算力供需与下游需求的边际变化</div>
    </div>
    <div class="stamp">观测日 {esc(asof)} · 数据源 {ok}/{len(health)} · UTC {esc(stamp)}</div>
  </header>
  {render_verdict(verdict, models)}
  {render_confirmation_strip(ev)}
  <div class="stack">{render_price_panel(ev, notes)}</div>
  <div class="stack">{render_supply_panel(ev, notes)}</div>
  <div class="stack">{render_market_quotes(ev, notes)}</div>
  <div class="stack">{render_standard_matrix(ev, notes)}</div>
  <div class="stack">{render_token_panel(ev, notes, verdict)}</div>
  {render_sources(ev)}
</main>
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render the GPU compute price & supply dashboard.")
    parser.add_argument("--evidence", default=None, help="metrics.py 产出的证据包")
    parser.add_argument("--input", default=None,
                        help="报告 Markdown；只用来读 frontmatter 里的 verdict 判断块")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    md_path = Path(args.input) if args.input else None
    evidence_path = find_evidence(args.evidence, md_path)
    ev = json.loads(evidence_path.read_text(encoding="utf-8"))
    verdict = read_verdict(md_path)

    output = Path(args.output) if args.output else (
        SKILL_ROOT / "reports" / f"gpu-{ev.get('asof', 'latest')}.html")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_html(ev, verdict), encoding="utf-8")

    print(json.dumps({
        "output": str(output),
        "evidence": str(evidence_path),
        "asof": ev.get("asof"),
        "verdict_present": bool(verdict.get("headline")),
        "panel_notes": [k for k in PANEL_KEYS if (verdict.get("panels") or {}).get(k)],
        "panel_notes_over_limit": over_length_notes(verdict.get("panels") or {}),
        "models_charted": [m for m in ORDER
                           if ((((ev.get("models") or {}).get(m) or {}).get("by_source") or {})
                               .get("ornn", {}).get("transaction_index", {}).get("series"))],
        "sources": {h["source"]: h["status"] for h in ev.get("source_health") or []},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
