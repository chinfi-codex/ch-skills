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
PANEL_KEYS = ("price", "supply", "quotes", "matrix")
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
.sec-note { font-size: 11.5px; color: var(--ink-4); margin-top: 3px; }
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
/* 图 */
svg.chart { width: 100%; min-width: 520px; height: auto; display: block; }
svg.chart text { fill: var(--ink-4); font-size: 10.5px; font-family: var(--font-sans); }
.gridline { stroke: var(--line-2); stroke-width: 1; }
.empty { padding: 34px 16px; text-align: center; color: var(--ink-4); font-size: 12.5px;
         border: 1px dashed var(--line-1); border-radius: 9px; margin-top: 11px;
         line-height: 1.7; }
.footnote { font-size: 11.5px; color: var(--ink-4); margin-top: 9px; line-height: 1.6; }
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
  .signals { grid-template-columns: 1fr; }
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
        body += (f'<div class="footnote">参照系 90D：{bits}'
                 f'　—　用来分辨高端上涨是某一代稀缺，还是整条算力曲线在抬。</div>')

    anchor = ""
    first = (ev.get("models") or {}).get(ORDER[0]) or {}
    cross = first.get("cross_platform_median") or {}
    if cross.get("anchor_date"):
        lag = cross.get("anchor_lags_raw_latest_days")
        anchor = f"锚定 {cross['anchor_date']}" + (f"（比日历日晚 {lag} 天）" if lag else "")
    return f"""<section class="panel">
  <div class="sec-head">
    <div><strong>市场成交价趋势</strong>
      <div class="sec-note">Ornn OCPI 日度结算 · 最近 90 天 · USD / GPU·h</div></div>
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
    body = line_chart(series, unit_prefix="", colors=colors, digits=0)
    if all(len(s["points"]) < 2 for s in series):
        body = ('<div class="empty">暂无数据：供给序列还不足 2 个观测点。<br>'
                'Vast 与 Runpod 都没有历史接口，只回当下快照，'
                '所以这条曲线只能从首次采集当天往后长，补不回去。</div>')
    return f"""<section class="panel">
  <div class="sec-head">
    <div><strong>可用供给趋势</strong>
      <div class="sec-note">Offer 份额 / 可用 GPU 数指数化 · 缺失分量不计入</div></div>
    <div class="sec-hint">越高 = 越宽松</div>
  </div>
  {body}
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
            quant = ('<td class="na" colspan="3">样本不足</td>' if thin else
                     f'<td class="num">{money(val("offer_p25"))}</td>'
                     f'<td class="num">{money(val("offer_median"))}</td>'
                     f'<td class="num">{money(val("offer_p75"))}</td>')
            rows.append(
                f'<tr><td>Vast.ai</td><td>{esc(SHORT.get(model, model))}</td>'
                f'<td class="num">{money(val("offer_min"))}</td>{quant}'
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
                f'<td class="na">—</td>'
                f'<td class="dim">库存 {esc(stock or "暂无数据")}</td></tr>')

    table = ("".join(rows) or
             '<tr><td class="na" colspan="8">暂无数据：今日没有采到市场化报价</td></tr>')
    return f"""<section class="panel">
  <div class="sec-head">
    <div><strong>市场报价</strong>
      <div class="sec-note">Marketplace 当前 Offer 分布 · 纯 GPU 费口径</div></div>
    <div class="sec-hint">样本 &lt; 8 不出分位数</div>
  </div>
  <div class="scroll"><table>
    <thead><tr><th>来源</th><th>GPU</th><th>Min</th><th>P25</th><th>中位</th>
      <th>P75</th><th>样本</th><th>供给</th></tr></thead>
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
                             f'<span class="dim"> {(spot / od - 1) * 100:.0f}%</span></td>')
            else:
                cells.append('<td class="na">暂无</td>')
        rows.append(f'<tr><td>{esc(labels[source])}</td>{"".join(cells)}</tr>')

    body = ("".join(rows) or
            f'<tr><td class="na" colspan="{1 + 2 * len(ORDER)}">'
            f'暂无数据：标准报价层今天没有可用条目</td></tr>')
    stale = [h["source"] for h in (ev.get("source_health") or [])
             if h.get("mode") == "attested" and not h.get("fresh")]
    note = "CoreWeave 报整机价已折算单卡 · 含配套资源，与纯 GPU 费不同口径"
    if stale:
        note += f" · {'、'.join(stale)} 已过期需重新核对"
    return f"""<section class="panel">
  <div class="sec-head">
    <div><strong>标准报价矩阵</strong>
      <div class="sec-note">{esc(note)}</div></div>
    <div class="sec-hint">Spot 列附折价</div>
  </div>
  <div class="scroll"><table>
    <thead><tr><th>Provider</th>{head}</tr></thead>
    <tbody>{body}</tbody>
  </table></div>
  {panel_note(notes, "matrix")}
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
      <div class="sec-note">Cron → Raw → Normalize → Validate → Persist → Metrics</div></div>
    <div class="sec-hint">P = 价格行 / S = 供给行</div>
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
      <div class="sub">成交价 × 市场报价 × 标准报价 × 可用供给 → 判断 GPU 算力供需边际变化</div>
    </div>
    <div class="stamp">观测日 {esc(asof)} · 数据源 {ok}/{len(health)} · UTC {esc(stamp)}</div>
  </header>
  {render_verdict(verdict, models)}
  <div class="stack">{render_price_panel(ev, notes)}</div>
  <div class="stack">{render_supply_panel(ev, notes)}</div>
  <div class="stack">{render_market_quotes(ev, notes)}</div>
  <div class="stack">{render_standard_matrix(ev, notes)}</div>
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
