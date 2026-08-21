#!/usr/bin/env python3
"""Build the ai_daily two-axis figures as static inline SVG.

Two figures, both rendered at build time (no runtime JS — see
``html_report.figures`` for why):

- **双轴定位图**: the day's cognitive map. Vectors and product forms form the
  reference frame; the day's graded objects are highlighted on top; a
  same-window stage move is drawn as an arrow. Anchored to 「一句话结论」,
  because the first line of that section is exactly the claim this chart backs
  ("地图今天动没动").
- **档位迁移带**: the stage trajectory of every vector and form across the
  window. Anchored to 「待跟踪」, because that section *is* the projection of
  this issue's watchboard and the previous ones.

Coordinates are ``档位整数 + 档内完成度小数``. Both halves come from the
framework, not from this file: the integer is ``stage`` / ``penetration_stage``,
the fraction is how many of the framework's own graduation criteria (毕业三条)
or penetration questions (五追问) the model marked satisfied. Nothing here
decides how mature a vector is; it only maps recorded judgements onto pixels.

Every optional field degrades rather than fails: a vector with no
``graduation`` sits mid-band, one with no ``links`` sits in the "未挂钩" lane,
and with no objects at all the figure quietly becomes the plain structure map.
"""

from __future__ import annotations

import math
from collections import defaultdict
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple

STAGE_ORDINALS = {"实验": 1, "收敛中": 2, "事实标准": 3}
PEN_ORDINALS = {"萌芽": 1, "早期采用": 2, "主流化": 3}
STAGE_NAMES = ["", "实验", "收敛中", "事实标准"]
PEN_NAMES = ["", "萌芽", "早期采用", "主流化"]

SIDE_COLORS = {"both": "var(--violet)", "lab": "var(--blue)", "oss": "var(--pos)"}
GRADE_COLORS = {"S+": "var(--accent)", "S?": "var(--accent)", "A": "var(--violet)", "B": "var(--ink-4)"}
GRADE_LABELS = {"S+": "S-confirmed", "S?": "S-candidate", "A": "A 级", "B": "B 级"}
GRADE_ORDER = {"S+": 0, "S?": 1, "A": 2, "B": 3}
GRADE_ALIASES = {
    "s_confirmed": "S+", "s-confirmed": "S+", "sconfirmed": "S+", "s+": "S+",
    "s_candidate": "S?", "s-candidate": "S?", "scandidate": "S?", "s?": "S?", "a+": "S?",
    "a": "A", "b": "B",
}
STAGE_COLORS = {1: "var(--warn)", 2: "var(--blue)", 3: "var(--pos)"}

# plot geometry. The "未挂钩" lane on the left is sized per report (see
# _lane_width), so the plot origin px0 is computed inside _map_svg.
W, H = 780, 470
L, T, B, R = 104, 30, 384, 740


# --------------------------------------------------------------------------- #
# svg primitives
# --------------------------------------------------------------------------- #
def _el(tag: str, *, title: Optional[str] = None, **attrs: Any) -> str:
    body = "".join(f' {k.replace("_", "-")}="{v}"' for k, v in attrs.items() if v is not None)
    if title:
        return f"<{tag}{body}><title>{escape(title)}</title></{tag}>"
    return f"<{tag}{body}/>"


def _text(x: float, y: float, s: Any, anchor: str = "start", fill: str = "var(--ink-4)",
          size: float = 11, weight: Optional[int] = None) -> str:
    w = f' font-weight="{weight}"' if weight else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" fill="{fill}" '
            f'font-size="{size}"{w}>{escape(str(s))}</text>')


def _text_tip(x: float, y: float, s: str, tip: str, anchor: str = "start",
              fill: str = "var(--ink-4)", size: float = 11) -> str:
    """A label that keeps its full text reachable on hover after clipping."""
    return f"<g><title>{escape(tip)}</title>{_text(x, y, s, anchor, fill, size)}</g>"


def _tag(index: int) -> str:
    """Vectors are tagged A, B, C… and objects 1, 2, 3… — two layers share the
    plane in the overlay figure, and one numbering for both makes the key list
    ambiguous (object ③ sitting next to vector ③)."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _clip(text: str, limit: int) -> str:
    """Clip a display name for an axis label. The model writes long names with
    parenthesised inventories; the full string stays in the tooltip."""
    cut = text.split("(")[0].split("（")[0].strip() or text
    return cut if len(cut) <= limit else cut[: limit - 1] + "…"


def _star(cx: float, cy: float, r: float) -> str:
    pts = []
    for i in range(10):
        ang = math.pi / 5 * i - math.pi / 2
        rr = r * 0.45 if i % 2 else r
        pts.append(("L" if i else "M") + f"{cx + rr * math.cos(ang):.1f},{cy + rr * math.sin(ang):.1f}")
    return "".join(pts) + "Z"


# --------------------------------------------------------------------------- #
# frame parsing
# --------------------------------------------------------------------------- #
def _ordinal(value: Any, table: Dict[str, int]) -> Optional[int]:
    """Map a stage label onto its ordinal, tolerating qualifiers.

    The model writes free-ish labels ("主流化前段", "收敛中(事实标准候选)"), so an
    exact lookup is not enough. A **prefix** must beat mere containment: the
    label 「收敛中(事实标准候选)」 names the stage it is *approaching*, and reading
    it as 事实标准 would promote a vector the framework deliberately held back.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text in table:
        return table[text]
    for key in sorted(table, key=len, reverse=True):
        if text.startswith(key):
            return table[key]
    for key in sorted(table, key=len, reverse=True):
        if key in text:
            return table[key]
    return None


def _truthy_count(value: Any, expected: int) -> Optional[int]:
    """Count satisfied criteria in a graduation / five-asks dict (or list)."""
    if isinstance(value, dict):
        items = list(value.values())
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return None
    if not items:
        return None
    return min(sum(1 for v in items if v is True or v == 1), expected)


def _score(ordinal: int, satisfied: Optional[int], total: int) -> float:
    """档位整数 + 档内完成度。完成度缺失时落在格子正中，不假装有精度。"""
    if satisfied is None:
        return ordinal + 0.5
    return ordinal + 0.14 + (satisfied / total) * 0.72


def _as_list(value: Any) -> List[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def parse_vectors(frame: dict) -> List[dict]:
    out = []
    for raw in _as_list(frame.get("agent_eng_vectors")):
        vid = str(raw.get("id") or "").strip()
        ordinal = _ordinal(raw.get("stage"), STAGE_ORDINALS)
        if not vid or ordinal is None:
            continue
        links = raw.get("links") or []
        out.append({
            "id": vid,
            "name": str(raw.get("vector") or vid),
            "stage": ordinal,
            "grad": _truthy_count(raw.get("graduation"), 3),
            "side": str(raw.get("side") or "").lower(),
            "links": [str(x) for x in links if isinstance(x, str)],
            "pain": str(raw.get("open_pain") or ""),
        })
    return out


def parse_forms(frame: dict) -> List[dict]:
    out = []
    for raw in _as_list(frame.get("product_forms")):
        fid = str(raw.get("id") or "").strip()
        ordinal = _ordinal(raw.get("penetration_stage"), PEN_ORDINALS)
        if not fid or ordinal is None:
            continue
        links = raw.get("links") or []
        out.append({
            "id": fid,
            "name": str(raw.get("form") or fid),
            "pen": ordinal,
            "links": [str(x) for x in links if isinstance(x, str)],
            "asks": _truthy_count(raw.get("five_asks"), 5),
            "segment": str(raw.get("segment") or ""),
            "signal": str(raw.get("usage_signal") or ""),
        })
    return out


def parse_objects(watchboard: dict) -> List[dict]:
    """Today's plotted objects: explicit ``axis_objects`` first, then any
    ``grading_audit`` row that carries an ``axis_hit``. Deduplicated by name so
    an S object recorded in both places is drawn once."""
    seen: set[str] = set()
    out: List[dict] = []

    def _add(name: str, grade: Any, vid: Any, fid: Any, note: Any) -> None:
        key = str(name).strip()
        norm = GRADE_ALIASES.get(str(grade).strip().lower(), None)
        if not key or key in seen or norm is None:
            return
        seen.add(key)
        out.append({
            "name": key,
            "grade": norm,
            "vector_id": str(vid) if vid else None,
            "form_id": str(fid) if fid else None,
            "note": str(note or ""),
        })

    for raw in _as_list(watchboard.get("axis_objects")):
        _add(raw.get("name") or raw.get("entity"), raw.get("grade"),
             raw.get("vector_id"), raw.get("form_id"), raw.get("note"))
    for raw in _as_list(watchboard.get("grading_audit")):
        hit = raw.get("axis_hit")
        if not isinstance(hit, dict):
            continue
        _add(raw.get("entity") or raw.get("entity_id"), raw.get("final_grade"),
             hit.get("vector_id"), hit.get("form_id"), raw.get("rationale"))
    out.sort(key=lambda o: GRADE_ORDER[o["grade"]])
    return out


# --------------------------------------------------------------------------- #
# history: stage series per canonical id
# --------------------------------------------------------------------------- #
def stage_series(history: Sequence[Tuple[str, dict]]) -> Dict[str, Any]:
    """Per-id stage trajectory across the window, plus display names.

    ``None`` = not in the frame yet, ``"gone"`` = was there and dropped out
    without settling. The distinction matters: frame has no silent-drop guard,
    so a vanished form is invisible in the text but visible here.
    """
    vec: Dict[str, List[Any]] = {}
    form: Dict[str, List[Any]] = {}
    names: Dict[str, str] = {}
    n = len(history)
    for i, (_date_key, payload) in enumerate(history):
        frame = payload.get("frame") if isinstance(payload, dict) else None
        frame = frame if isinstance(frame, dict) else {}
        today_v = {v["id"]: v for v in parse_vectors(frame)}
        today_f = {f["id"]: f for f in parse_forms(frame)}
        for store, today, key in ((vec, today_v, "stage"), (form, today_f, "pen")):
            for oid, item in today.items():
                store.setdefault(oid, [None] * n)[i] = item[key]
                names[oid] = item["name"]
    for store in (vec, form):
        for series in store.values():
            last = max((i for i, v in enumerate(series) if v is not None), default=-1)
            first = next((i for i, v in enumerate(series) if v is not None), None)
            if first is None:
                continue
            for i in range(first, len(series)):
                if series[i] is None:
                    series[i] = "gone" if i > last else None
    return {"vectors": vec, "forms": form, "names": names}


def days_at_stage(series: Optional[List[Any]]) -> Optional[Tuple[int, bool]]:
    """(days held at the current stage, whether that runs off the window)."""
    if not series:
        return None
    current = series[-1]
    if not isinstance(current, int):
        return None
    count = 0
    for value in reversed(series):
        if value != current:
            break
        count += 1
    return count, count == len(series)


# --------------------------------------------------------------------------- #
# figure 1 — the map
# --------------------------------------------------------------------------- #
def _lane_cols(n: int) -> int:
    return 0 if n <= 0 else 1 if n <= 3 else 2 if n <= 8 else 3


def _lane_width(unlinked_vectors: int, lane_objects: int = 0) -> Tuple[int, int]:
    """Size the 未挂钩 lane, and split it between its two tenants.

    Both layers can end up here — a vector with no ``links`` (no x) and an object
    that pushed a capability without touching any product form. They need
    separate sub-columns or they sit on top of each other. Returns
    ``(lane_width, vector_share)``.
    """
    cols_v, cols_o = _lane_cols(unlinked_vectors), _lane_cols(lane_objects)
    vector_share = 12 + cols_v * 28 if cols_v else 0
    return 24 + max(cols_v + cols_o, 1) * 28, vector_share


def _spread_linked(points: List[dict], px0: float) -> None:
    """Deterministic de-overlap for points that borrow a form's column.

    Vectors hanging off the same form land on identical coordinates, so some
    spreading is unavoidable. Doing it by sorted id (not iteration order) keeps
    the same report rendering the same way every time, and the group is shifted
    as a block when it would run off an edge — clamping members individually
    would collapse the very spacing this just created.
    """
    rows: Dict[int, List[dict]] = defaultdict(list)
    for p in points:
        rows[round(p["y"] / 12)].append(p)
    for group in rows.values():
        if len(group) > 1:
            group.sort(key=lambda p: (p["x"], p["id"]))
            n = len(group)
            for i, p in enumerate(group):
                p["x"] += (i - (n - 1) / 2) * 22
                if n > 2:
                    p["y"] += (0, 13, -13)[i % 3]
        lo, hi = min(p["x"] for p in group), max(p["x"] for p in group)
        shift = max(0.0, px0 + 14 - lo) - max(0.0, hi - (R - 14))
        if shift:
            for p in group:
                p["x"] += shift


def _layout_lane(points: List[dict], lane_x0: float, lane_w: float, sy) -> None:
    """Arrange unlinked vectors inside the 未挂钩 lane, one block per stage band.

    Up to three per band keep their exact height (the 档内完成度 is real
    information); a fuller band switches to a rank-ordered grid, which trades
    exact y for legibility. Either way the tooltip and the key list still carry
    the precise档位 / 毕业度.
    """
    cols = max(1, int((lane_w - 8) // 28))
    xs = [lane_x0 + (lane_w / (cols + 1)) * (i + 1) for i in range(cols)]
    bands: Dict[int, List[dict]] = defaultdict(list)
    for p in points:
        bands[p["v"]["stage"]].append(p)
    for stage, group in bands.items():
        group.sort(key=lambda p: (-p["y"], p["id"]))
        top, bottom = sy(stage + 1) + 14, sy(stage) - 14
        n = len(group)
        if n <= 3:
            for i, p in enumerate(group):
                p["x"] = xs[i % cols] if cols > 1 else xs[0]
                if n > 1 and cols == 1:
                    p["y"] = top + (bottom - top) * (i + 0.5) / n
        else:
            rows_n = math.ceil(n / cols)
            for i, p in enumerate(group):
                p["x"] = xs[i % cols]
                p["y"] = top + (bottom - top) * (i // cols + 0.5) / rows_n


def _link_map(vectors, forms) -> Dict[str, List[str]]:
    """vector id → form ids, reading links from whichever side wrote them.

    The coupling is one relationship, so it should not matter whether the model
    recorded it on the vector ("我在喂这个场景") or on the form ("我靠这条能力
    撑着"). Reading only one direction silently discards half the answers.
    """
    form_ids = {f["id"] for f in forms}
    vector_ids = {v["id"] for v in vectors}
    linked: Dict[str, List[str]] = {v["id"]: [] for v in vectors}
    for v in vectors:
        linked[v["id"]] = [fid for fid in v["links"] if fid in form_ids]
    for f in forms:
        for vid in f["links"]:
            if vid in vector_ids and f["id"] not in linked[vid]:
                linked[vid].append(f["id"])
    return linked


def _map_svg(vectors, forms, objects, moves) -> str:
    links = _link_map(vectors, forms)
    unlinked = sum(1 for v in vectors if not links[v["id"]])
    lane_objects = sum(1 for o in objects if not o["form_id"])
    marg_w, vector_share = _lane_width(unlinked, lane_objects)
    px0 = L + marg_w + 16
    sx = lambda v: px0 + (v - 1) / 3 * (R - px0)
    sy = lambda v: B - (v - 1) / 3 * (B - T)
    o: List[str] = [
        '<defs><marker id="axis-arw" viewBox="0 0 8 8" refX="6.5" refY="4" markerWidth="6" '
        'markerHeight="6" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="var(--ink-2)"/></marker></defs>'
    ]
    # quadrant tints: the supply/demand reading of the plane
    o.append(_el("rect", x=f"{px0:.1f}", y=T, width=f"{sx(3)-px0:.1f}", height=f"{sy(3)-T:.1f}",
                 fill="var(--warn)", opacity=.06))
    o.append(_el("rect", x=f"{sx(3):.1f}", y=T, width=f"{R-sx(3):.1f}", height=f"{sy(3)-T:.1f}",
                 fill="var(--pos)", opacity=.08))
    o.append(_el("rect", x=f"{sx(3):.1f}", y=f"{sy(3):.1f}", width=f"{R-sx(3):.1f}",
                 height=f"{B-sy(3):.1f}", fill="var(--neg)", opacity=.05))
    o.append(_text(px0 + 10, T + 15, "供给超前：能力收敛了，还没人真用", size=10.5))
    o.append(_text(R - 8, T + 15, "兑现区：能力成熟 + 真用起来", "end", size=10.5))
    o.append(_text(R - 8, B - 9, "需求拉动：用得广但技术没定型", "end", size=10.5))

    for v in (1, 2, 3, 4):
        o.append(_el("line", x1=f"{px0:.1f}", x2=R, y1=f"{sy(v):.1f}", y2=f"{sy(v):.1f}", stroke="var(--line-1)"))
        o.append(_el("line", x1=f"{sx(v):.1f}", x2=f"{sx(v):.1f}", y1=T, y2=B, stroke="var(--line-1)"))
    o.append(_el("rect", x=L, y=T, width=marg_w, height=B - T, fill="var(--tint)", opacity=.85))
    o.append(_el("line", x1=L + marg_w + 8, x2=L + marg_w + 8, y1=T, y2=B,
                 stroke="var(--line-1)", stroke_dasharray="2 4"))
    o.append(_text(L + marg_w / 2, T - 12, "未挂钩", "middle", "var(--ink-4)", 10.5))
    for v in (1, 2, 3):
        o.append(_text(L - 12, (sy(v) + sy(v + 1)) / 2 + 4, STAGE_NAMES[v], "end", "var(--ink-3)", 12))
        o.append(_text((sx(v) + sx(v + 1)) / 2, B + 56, PEN_NAMES[v], "middle", "var(--ink-3)", 12))
    o.append(_text(L - 12, T - 12, "能力成熟度 ↑", "end", "var(--ink-2)", 12, 600))
    o.append(_text(R, B + 78, "渗透度 →", "end", "var(--ink-2)", 12, 600))
    o.append(_el("line", x1=f"{px0:.1f}", x2=R, y1=B, y2=B, stroke="var(--ink-4)", stroke_width=1.2))
    o.append(_el("line", x1=f"{px0:.1f}", x2=f"{px0:.1f}", y1=T, y2=B, stroke="var(--ink-4)", stroke_width=1.2))

    colx = {f["id"]: sx(_score(f["pen"], f["asks"], 5)) for f in forms}
    dim = bool(objects)  # with a highlight layer the frame recedes to a reference grid

    # Form names run long ("企业知识工作agent(ChatGPT Work/千问办公/…)"), so the axis
    # label is clipped and staggered over two rows; the full name lives in the
    # tooltip. Row alternates in x order, which also separates two forms that
    # share a column.
    for row, f in enumerate(sorted(forms, key=lambda z: (colx[z["id"]], z["id"]))):
        x = colx[f["id"]]
        asks = "—" if f["asks"] is None else f'{f["asks"]}/5'
        o.append(_el("line", x1=f"{x:.1f}", x2=f"{x:.1f}", y1=T + 6, y2=B, stroke="var(--cyan)",
                     stroke_width=1, stroke_dasharray="2 5", opacity=.20 if dim else .42))
        o.append(_el("path", d=f"M{x:.1f},{B-9} L{x+9:.1f},{B} L{x:.1f},{B+9} L{x-9:.1f},{B} Z",
                     fill="var(--cyan)", opacity=.45 if dim else .9,
                     stroke="var(--card)", stroke_width=1.2,
                     title=f'{f["name"]} — 渗透 {PEN_NAMES[f["pen"]]}｜五追问 {asks}'
                           + (f'｜{f["segment"]}' if f["segment"] else "")
                           + (f'｜使用信号：{f["signal"]}' if f["signal"] else "")))
        o.append(_text(x, B + 20 + (row % 2) * 15, _clip(f["name"], 11),
                       "middle", "var(--cyan)", 10.5, 600))

    points = []
    for i, v in enumerate(sorted(vectors, key=lambda z: (-z["stage"], z["id"]))):
        linked = [colx[fid] for fid in links[v["id"]] if fid in colx]
        points.append({
            "id": v["id"], "no": _tag(i), "v": v,
            "x": sum(linked) / len(linked) if linked else L + marg_w / 2,
            "y": sy(_score(v["stage"], v["grad"], 3)),
            "linked": bool(linked),
        })
    _spread_linked([p for p in points if p["linked"]], px0)
    _layout_lane([p for p in points if not p["linked"]], L, vector_share or marg_w, sy)
    for p in points:
        v = p["v"]
        grad = "—" if v["grad"] is None else f'{v["grad"]}/3'
        tip = f'{p["no"]}. {v["name"]} — 档位 {STAGE_NAMES[v["stage"]]}｜毕业度 {grad}'
        if v.get("days_label"):
            tip += f'｜同档停留 {v["days_label"]}'
        if v["pain"]:
            tip += f'｜卡点：{v["pain"]}'
        o.append(_el("circle", cx=f'{p["x"]:.1f}', cy=f'{p["y"]:.1f}', r=9 if dim else 11,
                     fill=SIDE_COLORS.get(v["side"], "var(--ink-3)"), opacity=.34 if dim else .92,
                     stroke="var(--card)", stroke_width=1.5, title=tip))
        label = _text(p["x"], p["y"] + (3.5 if dim else 4), p["no"], "middle",
                      "var(--card)", 10 if dim else 11, 650)
        o.append(f'<g opacity="0.75">{label}</g>' if dim else label)

    pos = {p["id"]: p for p in points}
    for mv in moves:
        p = pos.get(mv["id"])
        if not p:
            continue
        y0 = sy(mv["from_score"])
        o.append(_el("circle", cx=f'{p["x"]:.1f}', cy=f"{y0:.1f}", r=8, fill="none",
                     stroke="var(--ink-4)", stroke_dasharray="2 3", opacity=.6,
                     title=f'{mv["name"]} — 窗口起点：{STAGE_NAMES[mv["from_stage"]]}'))
        direction = -10 if y0 > p["y"] else 10
        o.append(_el("line", x1=f'{p["x"]:.1f}', y1=f"{y0 + direction:.1f}",
                     x2=f'{p["x"]:.1f}', y2=f'{p["y"] - direction:.1f}',
                     stroke="var(--ink-2)", stroke_width=1.4, opacity=.5, marker_end="url(#axis-arw)"))
        o.append(_text(p["x"] + 11, (y0 + p["y"]) / 2, f'{mv["date"]} {"进档" if y0 > p["y"] else "退档"}',
                       fill="var(--ink-3)", size=10))

    if objects:
        pen_of = {f["id"]: f["pen"] for f in forms}
        stage_of = {v["id"]: v["stage"] for v in vectors}
        cells: Dict[Tuple[int, int], List[dict]] = defaultdict(list)
        for i, ob in enumerate(objects, 1):
            xb = pen_of.get(ob["form_id"] or "", 0)
            yb = stage_of.get(ob["vector_id"] or "", 0)
            cells[(xb, yb)].append({**ob, "no": i})
        for (xb, yb), lst in sorted(cells.items()):
            # in the 未挂钩 lane the objects take the share the vectors left over
            x0 = L + vector_share + 4 if xb == 0 else sx(xb) + 8
            x1 = L + marg_w - 4 if xb == 0 else sx(xb + 1) - 8
            y0 = B - 30 if yb == 0 else sy(yb + 1) + 10
            y1 = B - 4 if yb == 0 else sy(yb) - 10
            if len(lst) > 1:
                o.append(_el("rect", x=f"{x0-6:.1f}", y=f"{y0-6:.1f}", width=f"{x1-x0+12:.1f}",
                             height=f"{y1-y0+12:.1f}", rx=8, fill="var(--accent)",
                             opacity=f"{min(0.05 + 0.012 * len(lst), 0.22):.3f}"))
            cols = min(len(lst), max(1, int((x1 - x0) // 34)))
            rows_n = math.ceil(len(lst) / cols)
            step_x = (x1 - x0) / max(1, cols)
            step_y = (y1 - y0) / rows_n if rows_n > 1 else 0
            for j, ob in enumerate(lst):
                cx = x0 + step_x * (j % cols) + step_x / 2
                cy = y0 + step_y * (j // cols) + step_y / 2 if rows_n > 1 else (y0 + y1) / 2
                color = GRADE_COLORS[ob["grade"]]
                tip = f'{ob["no"]}. {ob["name"]} · {GRADE_LABELS[ob["grade"]]}'
                if ob["note"]:
                    tip += f' — {ob["note"]}'
                if ob["grade"] == "S+":
                    o.append(_el("path", d=_star(cx, cy, 13), fill=color, stroke="var(--card)",
                                 stroke_width=1.2, title=tip))
                elif ob["grade"] == "S?":
                    o.append(_el("path", d=_star(cx, cy, 11.5), fill="none", stroke=color,
                                 stroke_width=2.4, title=tip))
                else:
                    o.append(_el("circle", cx=f"{cx:.1f}", cy=f"{cy:.1f}",
                                 r=10 if ob["grade"] == "A" else 8, fill=color,
                                 opacity=.92 if ob["grade"] == "A" else .5,
                                 stroke="var(--card)", stroke_width=1.2, title=tip))
                if ob["grade"].startswith("S"):
                    o.append(_text(cx + 15, cy + 4, ob["no"], fill="var(--accent)", size=10.5, weight=700))
                else:
                    o.append(_text(cx, cy + 4, ob["no"], "middle", "var(--card)", 10.5, 650))
        if any((o_["vector_id"] or "") not in {v["id"] for v in vectors} for o_ in objects):
            o.append(_text(R - 8, B - 36, "↓ 只推渗透，没动能力", "end", size=9.5))

    return f'<svg viewBox="0 0 {W} {H}" role="img">{"".join(o)}</svg>'


def _keys_html(vectors, objects) -> str:
    rows = []
    for i, v in enumerate(sorted(vectors, key=lambda z: (-z["stage"], z["id"]))):
        grad = "毕业 —" if v["grad"] is None else f'毕业 {v["grad"]}/3'
        meta = f'{STAGE_NAMES[v["stage"]]} · {grad}'
        if v.get("days_label"):
            meta += f' · 停留 {v["days_label"]}'
        rows.append(
            f'<div><span class="rf-no" style="background:{SIDE_COLORS.get(v["side"], "var(--ink-3)")}">'
            f'{_tag(i)}</span>'
            f'<span class="rf-nm">{escape(v["name"])}</span>'
            f'<span class="rf-mt">{escape(meta)}</span></div>')
    for i, ob in enumerate(objects, 1):
        style = f'background:{GRADE_COLORS[ob["grade"]]}'
        if ob["grade"] == "S?":
            style = "background:transparent;color:var(--accent);border:2px solid var(--accent)"
        rows.append(
            f'<div><span class="rf-no" style="{style}">{i}</span>'
            f'<span class="rf-nm">{escape(ob["name"])}</span>'
            f'<span class="rf-mt">{escape(GRADE_LABELS[ob["grade"]])}</span></div>')
    return f'<div class="rf-keys">{"".join(rows)}</div>' if rows else ""


def _map_legend(objects) -> str:
    bits = [
        ('<span><span class="rf-dot" style="background:var(--violet)"></span>矢量·双侧证据</span>'),
        ('<span><span class="rf-dot" style="background:var(--blue)"></span>矢量·仅实验室</span>'),
        ('<span><span class="rf-dot" style="background:var(--pos)"></span>矢量·仅开源</span>'),
        ('<span><span class="rf-dot" style="background:var(--cyan);border-radius:2px"></span>产品形态（在横轴上）</span>'),
    ]
    if objects:
        bits += [
            '<span><span class="rf-dot" style="background:var(--accent)"></span>今日 S 级</span>',
            '<span><span class="rf-dot" style="background:var(--violet)"></span>今日 A 级</span>',
            '<span><span class="rf-dot" style="background:var(--ink-4);opacity:.55"></span>今日 B 级</span>',
            '<span>底图矢量记 A/B/C…，今日对象记 1/2/3…</span>',
        ]
    return f'<div class="rf-legend">{"".join(bits)}</div>'


# --------------------------------------------------------------------------- #
# figure 2 — the stage migration band
# --------------------------------------------------------------------------- #
def _band_svg(history, series) -> Tuple[str, int, int]:
    days = [d[5:] if len(d) >= 10 else d for d, _ in history]
    n = len(days)
    vec_rows = sorted(series["vectors"].items(),
                      key=lambda kv: (-(kv[1][-1] if isinstance(kv[1][-1], int) else 0), kv[0]))
    form_rows = sorted(series["forms"].items(),
                       key=lambda kv: (-(kv[1][-1] if isinstance(kv[1][-1], int) else 0), kv[0]))
    names = series["names"]
    left, top, gap = 168, 46, 30
    cell_w = max(18, min(34, int(560 / max(n, 1))))
    row_h = 21
    width = left + n * cell_w + 34
    height = top + (len(vec_rows) + len(form_rows)) * row_h + gap + 56
    o: List[str] = []
    for i, d in enumerate(days):
        if n <= 10 or i % 2 == 0 or i == n - 1:
            o.append(_text(left + i * cell_w + cell_w / 2, top - 13, d, "middle", size=9))

    promotions = 0
    dropped: List[str] = []

    def band(rows, y0: float, title: str, labels: List[str]) -> None:
        nonlocal promotions
        o.append(_text(8, y0 - 10, title, fill="var(--ink-3)", size=11, weight=650))
        for r, (oid, vals) in enumerate(rows):
            y = y0 + r * row_h
            full = names.get(oid, oid)
            o.append(_text_tip(left - 8, y + 13, _clip(full, 14), f"{full}（{oid}）",
                               "end", "var(--ink-2)", 10.5))
            for i, value in enumerate(vals):
                x = left + i * cell_w
                if value is None:
                    o.append(_el("rect", x=x + 2, y=y + 4, width=cell_w - 4, height=12, rx=2,
                                 fill="var(--tint)", opacity=.55))
                    continue
                if value == "gone":
                    o.append(_el("rect", x=x + 2, y=y + 4, width=cell_w - 4, height=12, rx=2,
                                 fill="var(--neg)", opacity=.14,
                                 title=f"{names.get(oid, oid)} — {days[i]}：已不在 frame 里，无结算记录"))
                    if vals[i - 1] != "gone":
                        dropped.append(names.get(oid, oid))
                        o.append(_text(x + cell_w / 2, y + 14, "×", "middle", "var(--neg)", 11, 700))
                    continue
                o.append(_el("rect", x=x + 2, y=y + 4, width=cell_w - 4, height=12, rx=2,
                             fill=STAGE_COLORS.get(value, "var(--ink-4)"),
                             opacity=f"{.3 + value * 0.2:.2f}",
                             title=f"{names.get(oid, oid)} — {days[i]}｜{labels[value]}"))
                prev = vals[i - 1] if i else None
                if isinstance(prev, int) and prev != value:
                    promotions += 1
                    o.append(_el("line", x1=x + 1, x2=x + 1, y1=y + 2, y2=y + 19,
                                 stroke="var(--accent)", stroke_width=2.4))
                    o.append(_text(x + 5, y - 1, "↑ 进档" if value > prev else "↓ 退档",
                                   fill="var(--accent)", size=9, weight=650))

    band(vec_rows, top, "纵轴 · 能力矢量", STAGE_NAMES)
    y2 = top + len(vec_rows) * row_h + gap
    band(form_rows, y2, "横轴 · 产品形态", PEN_NAMES)
    foot = y2 + len(form_rows) * row_h + 20
    o.append(_text(left, foot, f"{n} 天内档位变化合计：{promotions} 次",
                   fill="var(--ink-3)", size=10.5, weight=650))
    if dropped:
        o.append(_text(left, foot + 18,
                       f"另有 {len(dropped)} 项从 frame 消失且无结算记录（frame 没有 silent-drop guard）",
                       fill="var(--neg)", size=10.5))
    return f'<svg viewBox="0 0 {width} {height}" role="img">{"".join(o)}</svg>', promotions, len(dropped)


BAND_LEGEND = (
    '<div class="rf-legend">'
    '<span><span class="rf-dot" style="background:var(--warn)"></span>实验 / 萌芽</span>'
    '<span><span class="rf-dot" style="background:var(--blue)"></span>收敛中 / 早期采用</span>'
    '<span><span class="rf-dot" style="background:var(--pos)"></span>事实标准 / 主流化</span>'
    '<span><span class="rf-dot" style="background:var(--neg);opacity:.25"></span>无声消失</span>'
    "</div>"
)


# --------------------------------------------------------------------------- #
# public entry
# --------------------------------------------------------------------------- #
def build_figures(watchboard: dict, history: Sequence[Tuple[str, dict]] = ()) -> List[Any]:
    """Return the StaticFigures for one ai_daily report (possibly empty)."""
    from html_report import StaticFigure  # local import: shared pkg path set by caller

    frame = watchboard.get("frame") if isinstance(watchboard, dict) else None
    frame = frame if isinstance(frame, dict) else {}
    vectors, forms = parse_vectors(frame), parse_forms(frame)
    if not vectors and not forms:
        return []
    objects = parse_objects(watchboard)
    history = list(history)
    series = stage_series(history) if len(history) >= 2 else {"vectors": {}, "forms": {}, "names": {}}

    moves: List[dict] = []
    for v in vectors:
        past = series["vectors"].get(v["id"])
        held = days_at_stage(past)
        if held:
            v["days_label"] = f'{"≥" if held[1] else ""}{held[0]} 天'
            if not held[1] and held[0] < len(past):
                prior_stage = past[len(past) - held[0] - 1]
                if isinstance(prior_stage, int) and prior_stage != v["stage"]:
                    moves.append({
                        "id": v["id"], "name": v["name"], "from_stage": prior_stage,
                        "from_score": prior_stage + 0.5,
                        "date": history[len(past) - held[0]][0][5:],
                    })

    window = len(history)
    if moves:
        note = "、".join(f'{m["name"]} {m["date"]}' for m in moves[:3])
        cap = f"<b>{window} 天窗口内档位位移 {len(moves)} 次</b>：{escape(note)}。"
    elif window >= 2:
        cap = f"<b>{window} 天窗口内档位一次都没动</b>——今天是证据积累日，不是地图移动日。"
    else:
        cap = "无历史可比，本图只画当日快照。"
    if objects:
        cap += f" 星与圆点是今天定级的 {len(objects)} 个对象，落在它推动的矢量 × 形态那一格。"

    figures = [StaticFigure(
        html=_map_svg(vectors, forms, objects, moves) + _map_legend(objects) + _keys_html(vectors, objects),
        anchor=("一句话结论",),
        placement="section_end",
        title="双轴定位图 · 能力成熟度 × 渗透度",
        subtitle=f'{watchboard.get("as_of") or ""} · 纵轴档位 + 毕业三条完成度，横轴档位 + 五追问命中数'.strip(" ·"),
        caption=cap,
    )]

    if series["vectors"] or series["forms"]:
        svg, promotions, dropped = _band_svg(history, series)
        band_cap = (f"每格一天，颜色是当天的档位。{window} 天里合计 {promotions} 次档位变化"
                    + ("；" if dropped else "。"))
        if dropped:
            band_cap += f"<b>另有 {dropped} 项没结算就从 frame 里消失了</b>，回去把 watchboard 补干净。"
        figures.append(StaticFigure(
            html=svg + BAND_LEGEND,
            anchor=("待跟踪",),
            placement="before",
            match="last",  # 「待跟踪」小节标题
            title=f"{window} 天档位迁移带",
            subtitle=f"{history[0][0]} ~ {history[-1][0]} · 按 canonical id 对齐，全部取自 report_state",
            caption=band_cap,
        ))
    return figures
