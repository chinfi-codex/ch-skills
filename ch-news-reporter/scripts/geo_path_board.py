#!/usr/bin/env python3
"""geopolitical_daily dashboard figures — build-time static markup.

Three presentation upgrades over the finished Markdown (no judgement here):

- **路径分支板**: one full-width row per war-trajectory path (W1/W2/W3/W4/De).
  The row itself carries the probability as a full-width fill, plus the
  day-over-day delta in percentage points (vs the previous report_state entry)
  and a trailing-window sparkline. Anchored at the end of 「一句话结论」 so it
  sits right under the hero card.
- **时间节点轴**: the watchboard ``next_nodes`` laid out on a horizontal date
  axis, coloured by the channel each node affects, lane-stacked so dense
  clusters stay readable. Anchored before 「主战线」 (the end of 战况速览).
- **关键事件卡片**: a view-time decoration that upgrades the 战况速览
  「今日边际变化」 ordered list into numbered cards. The build-time markup is
  untouched, so script-blockers still see the plain list.

Every input degrades rather than fails: no watchboard → no figures; no PG
history → no deltas and no sparklines; no next_nodes → no timeline.
"""

from __future__ import annotations

import re
from datetime import date
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Severity gradient W1→W4, green for de-escalation. Theme vars where they
# exist, a hex for the W2 slot the palette has no var for.
PATH_COLORS = {
    "W1": "var(--warn)",
    "W2": "#e8710a",
    "W3": "var(--neg)",
    "W4": "var(--violet)",
    "De": "var(--pos)",
}

_SPARK_DAYS = 16  # trailing points per sparkline (aligned with --history-days)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _probs_of(payload: Any, keys: Sequence[str]) -> Optional[Dict[str, float]]:
    if not isinstance(payload, dict):
        return None
    frame = payload.get("frame")
    probs = frame.get("probabilities") if isinstance(frame, dict) else None
    if not isinstance(probs, dict):
        return None
    row: Dict[str, float] = {}
    for key in keys:
        v = _float(probs.get(key))
        if v is not None:
            row[key] = v
    return row or None


def _prob_series(
    history: Sequence[Tuple[str, dict]],
    keys: Sequence[str],
    report_day: Optional[str],
    today_row: Dict[str, float],
) -> List[Tuple[str, Dict[str, float]]]:
    """Oldest→newest (date, probs) up to report_day, today overlaid from the
    watchboard file (the render runs before today's state is saved)."""
    by_date: Dict[str, Dict[str, float]] = {}
    for date_key, payload in history:
        day = str(date_key or "")[:10]
        if not day or (report_day and day > report_day):
            continue
        row = _probs_of(payload, keys)
        if row:
            by_date[day] = row
    if report_day and today_row:
        by_date[report_day] = today_row
    return sorted(by_date.items())


def _frame_value(payload: Any, key: str) -> Optional[float]:
    if not isinstance(payload, dict):
        return None
    frame = payload.get("frame")
    if not isinstance(frame, dict):
        return None
    return _float(frame.get(key))


def _rung_series(
    history: Sequence[Tuple[str, dict]],
    key: str,
    report_day: Optional[str],
    today_val: float,
) -> List[Tuple[str, float]]:
    """Oldest→newest (date, rung) for one ladder, today overlaid from the file."""
    by_date: Dict[str, float] = {}
    for date_key, payload in history:
        day = str(date_key or "")[:10]
        if not day or (report_day and day > report_day):
            continue
        v = _frame_value(payload, key)
        if v is not None:
            by_date[day] = v
    if report_day:
        by_date[report_day] = today_val
    return sorted(by_date.items())


# --------------------------------------------------------------------------- #
# 路径分支板 (full-width rows)
# --------------------------------------------------------------------------- #
def _spark_svg(points: List[float], color: str) -> str:
    if len(points) < 2:
        return '<span class="pbr-nospark">—</span>'
    w, h, pad = 116, 26, 3
    lo, hi = min(points), max(points)
    rng = (hi - lo) or 1.0
    step = (w - 2 * pad) / (len(points) - 1)
    coords = [
        (pad + i * step, h - pad - (v - lo) / rng * (h - 2 * pad))
        for i, v in enumerate(points)
    ]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    ex, ey = coords[-1]
    tip = f"近 {len(points)} 期：{points[0]:g}% → {points[-1]:g}%"
    return (
        f'<svg viewBox="0 0 {w} {h}" role="img"><title>{escape(tip)}</title>'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>'
        f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="2.6" fill="{color}"/></svg>'
    )


def _delta_chip(delta: Optional[float], prev_day: Optional[str]) -> str:
    if delta is None:
        tip = "无上一期可比" if prev_day is None else f"上一期 {prev_day} 缺本路径读数"
        return f'<div class="pbr-delta none" title="{escape(tip)}">—</div>'
    tip = f"较 {prev_day}" if prev_day else "较上一期"
    if abs(delta) < 1e-9:
        return f'<div class="pbr-delta flat" title="{escape(tip)}">持平</div>'
    cls, arrow = ("up", "▲") if delta > 0 else ("down", "▼")
    return (
        f'<div class="pbr-delta {cls}" title="{escape(tip)}">'
        f"{arrow} {delta:+g}</div>"
    )


def _fmt_pct(value: float) -> str:
    return f"{value:g}"


def _path_board(
    watchboard: Dict[str, Any],
    series: List[Tuple[str, Dict[str, float]]],
    labels: Sequence[Tuple[str, str]],
    report_day: Optional[str],
) -> Optional[Any]:
    from html_report import StaticFigure  # local import: shared pkg path set by caller

    frame = watchboard.get("frame") if isinstance(watchboard, dict) else None
    frame = frame if isinstance(frame, dict) else {}
    cur_path = str(frame.get("path") or "").strip()
    today_row = _probs_of(watchboard, [k for k, _ in labels])
    if not today_row:
        return None

    prev_day: Optional[str] = None
    prev_row: Optional[Dict[str, float]] = None
    for day, row in series:
        if report_day and day < report_day:
            prev_day, prev_row = day, row
    if prev_day is None and len(series) >= 2 and series[-1][0] != report_day:
        prev_day, prev_row = series[-1]

    path_name = ""
    for key, label in labels:
        if key == cur_path:
            path_name = label.split(" ", 1)[-1] if " " in label else label

    rows_html: List[str] = []
    for key, label in labels:
        if key not in today_row:
            continue
        value = today_row[key]
        color = PATH_COLORS.get(key, "var(--accent)")
        name = label.split(" ", 1)[-1] if " " in label else label
        active = key == cur_path
        delta: Optional[float] = None
        if prev_row is not None and key in prev_row:
            delta = value - prev_row[key]
        points = [row[key] for _day, row in series if key in row][-_SPARK_DAYS:]
        cur_chip = '<span class="pbr-cur">当前路径</span>' if active else ""
        rows_html.append(
            f'<div class="pbr{" active" if active else ""}" style="--pc:{color}">'
            f'<div class="pbr-fill" style="width:{max(value, 0):.1f}%"></div>'
            f'<div class="pbr-edge"></div>'
            f'<div class="pbr-id"><span class="pbr-badge">{escape(key)}</span>'
            f'<span class="pbr-name">{escape(name)}</span>{cur_chip}</div>'
            f'<div class="pbr-num">{escape(_fmt_pct(value))}<i>%</i></div>'
            f"{_delta_chip(delta, prev_day)}"
            f'<div class="pbr-spark">{_spark_svg(points, color)}</div>'
            "</div>"
        )
    if not rows_html:
        return None

    subtitle_bits = [f"当前路径 {cur_path}" + (f"（{path_name}）" if path_name else "")]
    subtitle_bits.append(f"隔日基准 {prev_day}" if prev_day else "无隔日基准")

    if prev_day:
        caption = (
            f"隔日变动单位为<b>百分点</b>（基准 {escape(prev_day)}）；"
            f"右端走势为近 {min(len(series), _SPARK_DAYS)} 期概率轨迹。"
            "概率来自当日 watchboard 研判口径，非市场定价。"
        )
    else:
        caption = "无上一期可比，本板只画当日快照；概率来自当日 watchboard 研判口径，非市场定价。"

    return StaticFigure(
        html='<div class="pbr-board">' + "".join(rows_html) + "</div>",
        anchor=("一句话结论",),
        placement="section_end",
        title="路径分支 · 战争轨迹概率",
        subtitle=" · ".join(subtitle_bits),
        caption=caption,
        css=_BOARD_CSS,
    )


# --------------------------------------------------------------------------- #
# 升级阶梯板（军事 / 经济战两条 1-10 阶梯，与路径行同款整行可视化）
# --------------------------------------------------------------------------- #
_RUNG_DEFS: Sequence[Tuple[str, str, str, str]] = (
    ("escalation_rung", "升级阶梯", "军事", "var(--neg)"),
    ("econ_war_rung", "经济战阶梯", "经济战", "#e8710a"),
)


def _rung_delta_chip(delta: Optional[float], prev_day: Optional[str]) -> str:
    if delta is None:
        tip = "无上一期可比" if prev_day is None else f"上一期 {prev_day} 缺本档读数"
        return f'<div class="pbr-delta none" title="{escape(tip)}">—</div>'
    tip = f"较 {prev_day}" if prev_day else "较上一期"
    if abs(delta) < 1e-9:
        return f'<div class="pbr-delta flat" title="{escape(tip)}">持平</div>'
    cls, arrow = ("up", "▲") if delta > 0 else ("down", "▼")
    return (
        f'<div class="pbr-delta {cls}" title="{escape(tip)}">'
        f"{arrow} {delta:+g} 档</div>"
    )


def _rung_board(
    watchboard: Dict[str, Any],
    history: Sequence[Tuple[str, dict]],
    report_day: Optional[str],
) -> Optional[Any]:
    from html_report import StaticFigure  # local import: shared pkg path set by caller

    frame = watchboard.get("frame") if isinstance(watchboard, dict) else None
    frame = frame if isinstance(frame, dict) else {}

    rows_html: List[str] = []
    prev_day_used: Optional[str] = None
    for key, name, badge, color in _RUNG_DEFS:
        cur = _frame_value(watchboard, key)
        if cur is None:
            continue
        series = _rung_series(history, key, report_day, cur)
        prev_day: Optional[str] = None
        prev_val: Optional[float] = None
        for day, val in series:
            if report_day and day < report_day:
                prev_day, prev_val = day, val
        if prev_day is None and len(series) >= 2 and series[-1][0] != report_day:
            prev_day, prev_val = series[-1]
        prev_day_used = prev_day_used or prev_day
        delta = (cur - prev_val) if prev_val is not None else None
        points = [val for _day, val in series][-_SPARK_DAYS:]
        rows_html.append(
            f'<div class="pbr pbr-rung" style="--pc:{color}">'
            f'<div class="pbr-fill" style="width:{max(min(cur, 10.0), 0.0) * 10:.1f}%"></div>'
            f'<div class="pbr-edge"></div><div class="pbr-ticks"></div>'
            f'<div class="pbr-id"><span class="pbr-badge">{escape(badge)}</span>'
            f'<span class="pbr-name">{escape(name)}</span></div>'
            f'<div class="pbr-num">{escape(f"{cur:g}")}<i>/10</i></div>'
            f"{_rung_delta_chip(delta, prev_day)}"
            f'<div class="pbr-spark">{_spark_svg(points, color)}</div>'
            "</div>"
        )
    if not rows_html:
        return None

    if prev_day_used:
        caption = (
            f"阶梯读数 1–10 档，行背景填充为档位进度（每 10% 一档）；隔日变动为档位差"
            f"（基准 {escape(prev_day_used)}）；右端走势为近 16 期档位轨迹。"
        )
    else:
        caption = "阶梯读数 1–10 档，行背景填充为档位进度；无上一期可比，本板只画当日快照。"
    return StaticFigure(
        html='<div class="pbr-board">' + "".join(rows_html) + "</div>",
        anchor=("一句话结论",),
        placement="section_end",
        title="升级阶梯 · 烈度读数",
        subtitle="军事与经济战两条阶梯并列 · "
        + (f"隔日基准 {prev_day_used}" if prev_day_used else "无隔日基准"),
        caption=caption,
        css=_BOARD_CSS,
    )


_BOARD_CSS = """
.pbr-board { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
.pbr {
  position: relative; display: grid; align-items: center; gap: 14px;
  grid-template-columns: minmax(0, 1fr) 84px 92px 128px;
  padding: 10px 16px; border: 1px solid var(--line-2); border-radius: var(--r-md);
  background: var(--surface); overflow: hidden;
}
.pbr.active { border-color: var(--pc); }
.pbr-fill { position: absolute; top: 0; bottom: 0; left: 0; background: var(--pc); opacity: 0.10; pointer-events: none; }
.pbr-edge { position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--pc); opacity: 0.9; pointer-events: none; }
.pbr-id, .pbr-num, .pbr-delta, .pbr-spark { position: relative; z-index: 1; }
.pbr-id { display: flex; align-items: center; min-width: 0; }
.pbr-badge {
  flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center;
  min-width: 34px; padding: 2px 8px; border-radius: 6px;
  background: var(--pc); color: #fff;
  font-family: var(--font-mono); font-size: 12.5px; font-weight: 700;
}
.pbr-name { margin-left: 9px; font-size: 14px; font-weight: 600; color: var(--ink-1); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pbr-cur {
  flex: 0 0 auto; margin-left: 8px; padding: 0 8px; border: 1px solid var(--pc);
  border-radius: var(--pill); color: var(--pc); font-size: 10.5px; font-weight: 600;
  line-height: 18px; white-space: nowrap;
}
.pbr-num { font-family: var(--font-mono); font-size: 20px; font-weight: 700; color: var(--ink-1); text-align: right; }
.pbr-num i { font-style: normal; font-size: 12px; font-weight: 500; color: var(--ink-4); margin-left: 1px; }
.pbr-delta { font-family: var(--font-mono); font-size: 12.5px; font-weight: 600; border-radius: var(--pill); padding: 3px 0; text-align: center; white-space: nowrap; }
.pbr-delta.up { color: var(--neg); background: var(--neg-soft); }
.pbr-delta.down { color: var(--pos); background: var(--pos-soft); }
.pbr-delta.flat, .pbr-delta.none { color: var(--ink-3); background: var(--surface-3); }
.pbr-spark svg { display: block; width: 100%; height: 26px; }
.pbr-nospark { color: var(--ink-4); font-size: 12px; display: block; text-align: center; }
/* 阶梯行：10% 一档的分隔刻度，让 1-10 档位在行背景上直接可读 */
.pbr-ticks {
  position: absolute; top: 0; bottom: 0; left: 0; right: 0; pointer-events: none;
  background: repeating-linear-gradient(90deg,
    transparent 0, transparent calc(10% - 1px),
    var(--line-2) calc(10% - 1px), var(--line-2) 10%);
  opacity: 0.9;
}
@media (max-width: 720px) {
  .pbr { grid-template-columns: minmax(0, 1fr) 64px 78px; gap: 9px; padding: 9px 12px; }
  .pbr-spark { display: none; }
}
"""


# --------------------------------------------------------------------------- #
# 时间节点轴 (next_nodes timeline)
# --------------------------------------------------------------------------- #
_CAT_RULES: Sequence[Tuple[Sequence[str], str, str]] = (
    (("W1", "W2", "W3", "W4", "主战线"), "var(--neg)", "战争路径"),
    (("De", "停火", "去升级"), "var(--pos)", "去升级"),
    (("俄乌", "印太", "台海", "红海", "并行"), "var(--cyan)", "并行战线"),
    (("energy", "能源", "IEA", "储备", "油"), "var(--warn)", "能源通道"),
    (("market", "美联储", "加息", "议息", "降息"), "var(--accent)", "市场政策"),
    (("nuclear", "核", "great_power", "大选", "选举", "诺贝尔"), "var(--violet)", "核阈值 / 大国政治"),
)
_CAT_DEFAULT = ("var(--ink-4)", "其他")


def _node_category(name: str, affects: str) -> Tuple[str, str]:
    """Colour by the leading affects token first, then by a full-text scan."""
    first = re.split(r"[ /；;，,、]", affects.strip(), maxsplit=1)[0] if affects.strip() else ""
    for haystack in (first, affects + " " + name):
        if not haystack:
            continue
        for keys, color, label in _CAT_RULES:
            if any(k in haystack for k in keys):
                return color, label
    return _CAT_DEFAULT


def _text_width(s: str) -> float:
    return sum(11.5 if ord(c) > 0x2E7F else 6.4 for c in s)


def _clip_name(name: str, max_px: float = 172.0) -> str:
    if _text_width(name) <= max_px:
        return name
    out = ""
    for ch in name:
        if _text_width(out + ch) > max_px - 12:
            return out + "…"
        out += ch
    return out


def _nodes_timeline(
    watchboard: Dict[str, Any],
    report_day: Optional[str],
) -> Optional[Any]:
    from html_report import StaticFigure  # local import: shared pkg path set by caller

    raw = watchboard.get("next_nodes")
    if not isinstance(raw, list):
        return None
    try:
        day0 = date.fromisoformat(str(report_day)) if report_day else None
    except ValueError:
        day0 = None

    nodes: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        date_s = str(item.get("date") or "").strip()[:10]
        affects = str(item.get("affects") or "").strip()
        if not name or not date_s:
            continue
        try:
            d = date.fromisoformat(date_s)
        except ValueError:
            continue
        nodes.append({
            "name": name,
            "date": d,
            "date_s": date_s,
            "affects": affects,
            "days": (d - day0).days if day0 else None,
        })
    if not nodes:
        return None
    nodes.sort(key=lambda n: (n["date"], n["name"]))

    start = min([n["date"] for n in nodes] + ([day0] if day0 else []))
    end = max(n["date"] for n in nodes)
    span = max((end - start).days, 1)

    W, pad_l, pad_r = 960.0, 46.0, 46.0
    track = W - pad_l - pad_r

    def px(d: date) -> float:
        return pad_l + track * ((d - start).days / span)

    for n in nodes:
        color, cat = _node_category(n["name"], n["affects"])
        n["color"], n["cat"] = color, cat
        if n["days"] is None:
            when = ""
        elif n["days"] == 0:
            when = "今日"
        elif n["days"] > 0:
            when = f"+{n['days']}d"
        else:
            when = f"{n['days']}d"
        n["date_line"] = n["date_s"][5:] + (f" · {when}" if when else "")
        n["disp"] = _clip_name(n["name"])
        n["label_w"] = max(_text_width(n["disp"]), _text_width(n["date_line"])) + 10

    lanes: Dict[int, float] = {}
    lane_h = 42.0
    max_above = max_below = 0
    for n in nodes:
        nx, w = px(n["date"]), n["label_w"]
        #  clamp first: edge nodes slide inward, and the lane packing must
        #  account for the label's *actual* footprint, not its ideal centre
        cx = min(max(nx, pad_l - 8 + w / 2), W - pad_r + 8 - w / 2)
        n["cx"] = cx
        n["lane"] = 7  # overflow lane: stacked deepest, still drawn
        for lvl in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6):
            edge = lanes.get(lvl)
            if edge is None or cx - w / 2 > edge + 8:
                n["lane"] = lvl
                lanes[lvl] = cx + w / 2
                break
        if n["lane"] > 0:
            max_above = max(max_above, n["lane"])
        else:
            max_below = max(max_below, -n["lane"])

    axis_y = 26.0 + max_above * lane_h
    H = axis_y + max_below * lane_h + 42.0

    parts: List[str] = []
    parts.append(
        f'<line x1="{pad_l - 8:.1f}" y1="{axis_y:.1f}" x2="{W - pad_r + 8:.1f}" y2="{axis_y:.1f}" '
        f'stroke="var(--line-1)" stroke-width="1.2"/>'
    )
    if day0 and start <= day0 <= end:
        tx = px(day0)
        parts.append(
            f'<line x1="{tx:.1f}" y1="{axis_y - max_above * lane_h - 16:.1f}" x2="{tx:.1f}" '
            f'y2="{axis_y + max_below * lane_h + 26:.1f}" stroke="var(--accent)" '
            f'stroke-width="1" stroke-dasharray="3 4" opacity="0.55"/>'
            f'<text x="{tx:.1f}" y="{axis_y - max_above * lane_h - 19:.1f}" text-anchor="middle" '
            f'fill="var(--accent-ink)" font-size="10" font-weight="600" '
            f'font-family="var(--font-mono)">今日 {day0.isoformat()[5:]}</text>'
        )

    for n in nodes:
        nx = px(n["date"])
        lane = n["lane"]
        cx = n["cx"]
        is_today = n["days"] == 0
        r = 6 if is_today else 4.5
        ring = (
            f'<circle cx="{nx:.1f}" cy="{axis_y:.1f}" r="{r + 3:.1f}" fill="none" '
            f'stroke="{n["color"]}" stroke-width="1" opacity="0.45"/>'
        ) if is_today else ""
        if lane > 0:
            name_y = axis_y - 14 - (lane - 1) * lane_h
            date_y = name_y - 13
            lead_y = name_y + 4
            lead = (
                f'<line x1="{nx:.1f}" y1="{axis_y - r - 2:.1f}" x2="{nx:.1f}" y2="{lead_y:.1f}" '
                f'stroke="var(--line-2)" stroke-width="1"/>'
            )
        else:
            name_y = axis_y + 22 + (-lane - 1) * lane_h
            date_y = name_y + 13.5
            lead_y = name_y - 9
            lead = (
                f'<line x1="{nx:.1f}" y1="{axis_y + r + 2:.1f}" x2="{nx:.1f}" y2="{lead_y:.1f}" '
                f'stroke="var(--line-2)" stroke-width="1"/>'
            )
        tip = f'{n["date_s"]}｜{n["name"]}' + (f'｜影响：{n["affects"]}' if n["affects"] else "")
        parts.append(
            f"<g><title>{escape(tip)}</title>{ring}"
            f'<circle cx="{nx:.1f}" cy="{axis_y:.1f}" r="{r}" fill="{n["color"]}" '
            f'stroke="var(--surface)" stroke-width="1.5"/>'
            f"{lead}"
            f'<text x="{cx:.1f}" y="{name_y:.1f}" text-anchor="middle" fill="var(--ink-1)" '
            f'font-size="11.5" font-weight="600">{escape(n["disp"])}</text>'
            f'<text x="{cx:.1f}" y="{date_y:.1f}" text-anchor="middle" fill="{n["color"]}" '
            f'font-size="9.5" font-weight="600" font-family="var(--font-mono)">'
            f'{escape(n["date_line"])}</text>'
            "</g>"
        )

    svg = (
        f'<svg viewBox="0 0 {W:.0f} {H:.0f}" role="img" aria-label="关键时间节点时间轴">'
        + "".join(parts)
        + "</svg>"
    )

    used: List[Tuple[str, str]] = []
    for n in nodes:
        pair = (n["color"], n["cat"])
        if pair not in used:
            used.append(pair)
    legend = '<div class="rf-legend">' + "".join(
        f'<span><i class="rf-dot" style="background:{color}"></i>{escape(label)}</span>'
        for color, label in used
    ) + "</div>"

    caption = (
        f"节点来自当日 watchboard next_nodes（{len(nodes)} 个），距今按报告日 "
        f"{escape(str(report_day)) if report_day else '当日'} 计；悬停节点查看影响路径说明。"
    )
    return StaticFigure(
        html=svg + legend,
        anchor=("主战线",),
        placement="before",
        title="关键时间节点",
        subtitle=f"窗口 {start.isoformat()} → {end.isoformat()} · {len(nodes)} 个节点",
        caption=caption,
    )


# --------------------------------------------------------------------------- #
# 关键事件卡片 (view-time decoration over the 今日边际变化 list)
# --------------------------------------------------------------------------- #
KEY_EVENTS_JS = r"""(function () {
  const root = document.getElementById("report-body");
  if (!root) return;
  /* 契约层 +1：markdown 的 ## 战况速览 渲染为 h3，故按 data-sec 任意标题级匹配 */
  const h = root.querySelector('[data-sec="overview"]') ||
    Array.from(root.querySelectorAll("h2, h3")).find(
      x => x.textContent.replace(/\s/g, "").indexOf("战况速览") === 0);
  if (!h) return;
  /* 引擎把带空行的编号列表拆成 <ol> / <ol start="2"> / … 多个元素，
     要按文档序收齐整组，编号取 start 属性。 */
  let cur = h.nextElementSibling, label = null;
  const ols = [];
  while (cur && !/^H[1-3]$/.test(cur.tagName)) {
    if (cur.tagName === "P" && /今日边际变化/.test(cur.textContent)) label = cur;
    if (cur.tagName === "OL") ols.push(cur);
    cur = cur.nextElementSibling;
  }
  if (!ols.length) return;
  if (label) label.classList.add("ke-label");
  const wrap = document.createElement("div");
  wrap.className = "ke-list";
  let seq = 0;
  ols.forEach(ol => {
    const start = parseInt(ol.getAttribute("start") || "", 10);
    Array.from(ol.children).filter(li => li.tagName === "LI").forEach((li, idx) => {
      seq = Number.isFinite(start) ? start + idx : seq + 1;
      const card = document.createElement("div");
      card.className = "ke-card";
      const no = document.createElement("div");
      no.className = "ke-no";
      no.textContent = String(seq);
      const body = document.createElement("div");
      body.className = "ke-body";
      const first = li.firstElementChild;
      if (first && first.tagName === "STRONG") {
        const t = document.createElement("div");
        t.className = "ke-title";
        t.innerHTML = first.innerHTML;
        body.appendChild(t);
        first.remove();
      }
      const txt = document.createElement("div");
      txt.className = "ke-text";
      txt.innerHTML = li.innerHTML;
      body.appendChild(txt);
      card.appendChild(no);
      card.appendChild(body);
      wrap.appendChild(card);
    });
  });
  ols[0].before(wrap);
  ols.forEach(ol => ol.remove());
})();"""

KEY_EVENTS_CSS = """
.ke-label { display: inline-flex; align-items: center; gap: 8px; font-weight: 650; color: var(--ink-1); }
.ke-label::before {
  content: ""; width: 7px; height: 7px; border-radius: 50%;
  background: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft);
}
.ke-list { display: flex; flex-direction: column; gap: 10px; margin: 12px 0 20px; }
.ke-card {
  display: flex; gap: 13px; padding: 13px 16px;
  background: var(--surface); border: 1px solid var(--line-2);
  border-left: 3px solid var(--accent); border-radius: var(--r-md);
  box-shadow: var(--shadow-1);
}
.ke-no {
  flex: 0 0 24px; width: 24px; height: 24px; border-radius: 50%;
  background: var(--accent); color: #fff; font-size: 12px; font-weight: 700;
  display: flex; align-items: center; justify-content: center; margin-top: 2px;
  font-family: var(--font-mono);
}
.ke-title { font-weight: 650; color: var(--ink-1); line-height: 1.6; margin-bottom: 3px; }
.ke-text { color: var(--ink-2); font-size: 14px; line-height: 1.75; }
.ke-text .num-pos { color: var(--neg); font-weight: 600; }
.ke-text .num-neg { color: var(--pos); font-weight: 600; }
"""


class _KeyEventsDecoration:
    """Typed decoration handle: builder.add_decoration reads .js / .css."""

    js = KEY_EVENTS_JS
    css = KEY_EVENTS_CSS


KEY_EVENTS_DECORATION = _KeyEventsDecoration()


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def build_geo_figures(
    watchboard: Dict[str, Any],
    history: Sequence[Tuple[str, dict]],
    labels: Sequence[Tuple[str, str]],
    report_day: Optional[str],
) -> List[Any]:
    """Return the StaticFigures for one geopolitical_daily report (possibly empty)."""
    if not isinstance(watchboard, dict):
        return []
    keys = [key for key, _label in labels]
    today_row = _probs_of(watchboard, keys) or {}
    series = _prob_series(history, keys, report_day, today_row)

    figures: List[Any] = []
    board = _path_board(watchboard, series, labels, report_day)
    if board is not None:
        figures.append(board)
    rung_board = _rung_board(watchboard, history, report_day)
    if rung_board is not None:
        figures.append(rung_board)
    timeline = _nodes_timeline(watchboard, report_day)
    if timeline is not None:
        figures.append(timeline)
    return figures
