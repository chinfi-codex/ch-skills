#!/usr/bin/env python3
"""US-market daily dashboard figures — build-time static markup.

Two presentation upgrades over the finished Markdown (no judgement here), in
the visual language of ch-news-reporter's geopolitical path board:

- **指数行板**: one full-width row per index (QQQ / SOXX). The row fill carries
  the 52-week position (越满越贴顶), the big number is the day's change, the
  chip is the 5-day cumulative move, and the sparkline is the trailing 60
  trading days of closes — the 大盘 section's 四数读 in one line per index.
  Anchored at the end of 「大盘」.
- **主题行板**: one row per ledger theme, sorted by dollar-volume share. Fill
  and big number carry the share, the chip is the day-over-day share move in
  percentage points (vs the previous ledger day), the sparkline is the share's
  trailing 16 recorded days, and the state / 池外 / 新 tags come straight from
  the ledger record. Anchored right after the 「板块赚钱效应」 heading, where it
  replaces the old view-time SVG bar chart when data is available.
- **新方向卡片**: a view-time decoration that upgrades the 池外新方向 ordered
  list into numbered cards. The build-time markup is untouched, so
  script-blockers still see the plain list.

Every input degrades rather than fails: no evidence → no index board; no
ledger rows for the report day → fall back to the ``--themes`` payload; no
history → no delta chips and no sparklines; no ordered list → no cards.
"""

from __future__ import annotations

from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Row accents. Theme rows are coloured by ★ confirmation tier — the same
# semantics the retired client-side bar chart used (3★ accent / 2★ cyan /
# 1★ and unrated grey).
INDEX_COLORS = {"QQQ": "var(--accent)", "SOXX": "var(--orange)"}
INDEX_FALLBACK_COLOR = "var(--blue)"
TIER_COLORS = {3: "var(--accent)", 2: "var(--cyan)", 1: "var(--ink-4)", 0: "var(--ink-4)"}

# Lifecycle state → pill colour (bordered text chip in the row's id block).
STATE_COLORS = {
    "确认主线": "var(--pos)",
    "在场": "var(--accent)",
    "低位启动": "var(--blue)",
    "高位分歧": "var(--warn)",
    "退潮": "var(--neg)",
    "沉寂": "var(--ink-4)",
}

_SPARK_DAYS = 16    # trailing ledger days per theme sparkline
_INDEX_SPARK = 60   # trailing trading days per index sparkline
_MAX_THEME_ROWS = 10


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _fmt_signed(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}f}"


def _spark_svg(points: List[float], color: str, unit: str = "") -> str:
    if len(points) < 2:
        return '<span class="ubr-nospark">—</span>'
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
    tip = f"近 {len(points)} 期：{points[0]:g}{unit} → {points[-1]:g}{unit}"
    return (
        f'<svg viewBox="0 0 {w} {h}" role="img"><title>{escape(tip)}</title>'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>'
        f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="2.6" fill="{color}"/></svg>'
    )


def _delta_chip(
    delta: Optional[float],
    prev_day: Optional[str],
    *,
    suffix: str = "",
    tip: str = "",
) -> str:
    if delta is None:
        reason = tip or ("无上一期可比" if prev_day is None else f"上一期 {prev_day} 缺可比读数")
        return f'<div class="ubr-delta none" title="{escape(reason)}">—</div>'
    title = tip or (f"较 {prev_day}" if prev_day else "较上一期")
    if abs(delta) < 1e-9:
        return f'<div class="ubr-delta flat" title="{escape(title)}">持平</div>'
    cls, arrow = ("up", "▲") if delta > 0 else ("down", "▼")
    return (
        f'<div class="ubr-delta {cls}" title="{escape(title)}">'
        f"{arrow} {delta:+g}{suffix}</div>"
    )


# --------------------------------------------------------------------------- #
# 指数行板 (QQQ / SOXX full-width rows)
# --------------------------------------------------------------------------- #
def _index_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    snapshots = payload.get("index_snapshots") or {}
    rows: List[Dict[str, Any]] = []
    for item in payload.get("index_klines") or []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").upper()
        snap = snapshots.get(ticker) or {}
        records = item.get("records") or []
        closes = [
            _float(r.get("close"))
            for r in records[-_INDEX_SPARK:]
            if isinstance(r, dict)
        ]
        rows.append(
            {
                "ticker": ticker,
                "name": str(item.get("name") or ticker),
                "snap": snap,
                "closes": [c for c in closes if c is not None],
                "change": _float(item.get("change_pct")),
            }
        )
    return rows


def _index_board(payload: Dict[str, Any], report_day: Optional[str]) -> Optional[Any]:
    from html_report import StaticFigure  # local import: shared pkg path set by caller

    rows_html: List[str] = []
    for row in _index_rows(payload):
        snap = row["snap"]
        ticker = row["ticker"] or "IDX"
        color = INDEX_COLORS.get(ticker, INDEX_FALLBACK_COLOR)
        change = row["change"] if row["change"] is not None else _float(snap.get("change_pct"))
        five_day = _float(snap.get("five_day_trend_pct"))
        pos52 = _float(snap.get("position_52w"))
        vol = _float(snap.get("vol_vs_20d"))
        close = _float(snap.get("close"))

        fill_w = max(0.0, min(pos52 or 0.0, 1.0)) * 100
        num_cls = "" if change is None else ("up" if change >= 0 else "down")
        num_html = "—" if change is None else f"{_fmt_signed(change)}<i>%</i>"
        sub_bits = []
        if close is not None:
            sub_bits.append(f"收 ${close:,.2f}")
        if pos52 is not None:
            sub_bits.append(f"52周 {pos52:.2f}")
        if vol is not None:
            sub_bits.append(f"量比 {vol:.2f}")
        # 中列徽章承载 5 日累计本身（不是填充量的隔日差），涨跌向=红绿向
        if five_day is None:
            chip = '<div class="ubr-delta none" title="缺 5 日读数">—</div>'
        elif abs(five_day) < 1e-9:
            chip = '<div class="ubr-delta flat" title="5 日累计涨跌">5日 持平</div>'
        else:
            cls, arrow = ("up", "▲") if five_day > 0 else ("down", "▼")
            chip = (
                f'<div class="ubr-delta {cls}" title="5 日累计涨跌">'
                f"5日 {arrow} {abs(five_day):.2f}</div>"
            )
        rows_html.append(
            f'<div class="ubr" style="--pc:{color}">'
            f'<div class="ubr-fill" style="width:{fill_w:.1f}%"></div>'
            f'<div class="ubr-edge"></div>'
            f'<div class="ubr-id"><span class="ubr-badge">{escape(ticker)}</span>'
            f'<span class="ubr-name">{escape(row["name"])}</span>'
            f'<span class="ubr-sub">{escape(" · ".join(sub_bits))}</span></div>'
            f'<div class="ubr-num {num_cls}">{num_html}</div>'
            f"{chip}"
            f'<div class="ubr-spark">{_spark_svg(row["closes"], color)}</div>'
            "</div>"
        )
    if not rows_html:
        return None

    return StaticFigure(
        html='<div class="ubr-board">' + "".join(rows_html) + "</div>",
        anchor=("大盘",),
        placement="section_end",
        title="指数速览 · 位置 / 趋势 / 量能",
        subtitle=f"数据 {escape(str(report_day))}" if report_day else "",
        caption=(
            "行填充=<b>52 周位置</b>（越满越贴顶）；大数=当日涨跌；中列=5 日累计；"
            f"右端走势=近 {min(_INDEX_SPARK, 120)} 个交易日收盘。全部读数取自 evidence 快照与 K 线，口径与正文一致。"
        ),
        css=_BOARD_CSS,
    )


# --------------------------------------------------------------------------- #
# 主题行板 (ledger themes as full-width rows)
# --------------------------------------------------------------------------- #
def _theme_key(row: Dict[str, Any]) -> str:
    return str(row.get("theme_id") or row.get("name") or "")


def _theme_share(row: Dict[str, Any]) -> Optional[float]:
    return _float(row.get("dollar_vol_share"))


def _theme_stars(row: Dict[str, Any]) -> int:
    stars = _float(row.get("stars"))
    if stars is None:
        return 0
    return max(0, min(3, int(stars)))


def _share_history(
    ledger_records: Sequence[Dict[str, Any]],
    key: str,
    report_day: Optional[str],
) -> List[Tuple[str, float]]:
    """Oldest→newest (asof, share%) for one theme, up to and including today."""
    by_day: Dict[str, float] = {}
    for rec in ledger_records:
        if not isinstance(rec, dict) or _theme_key(rec) != key:
            continue
        day = str(rec.get("asof") or "")[:10]
        share = _theme_share(rec)
        if not day or share is None:
            continue
        if report_day and day > report_day:
            continue
        by_day[day] = share
    return sorted(by_day.items())


def _prev_share(
    series: List[Tuple[str, float]], report_day: Optional[str]
) -> Tuple[Optional[str], Optional[float]]:
    for day, value in reversed(series):
        if report_day is None or day < report_day:
            return day, value
    return None, None


def _state_chip(state: str) -> str:
    color = STATE_COLORS.get(state, "var(--ink-4)")
    return (
        f'<span class="ubr-state" style="color:{color};border-color:{color}">'
        f"{escape(state)}</span>"
    )


def _theme_board(
    themes_today: Sequence[Dict[str, Any]],
    ledger_records: Sequence[Dict[str, Any]],
    report_day: Optional[str],
) -> Optional[Any]:
    from html_report import StaticFigure  # local import: shared pkg path set by caller

    rows: List[Dict[str, Any]] = []
    for rec in themes_today:
        if not isinstance(rec, dict):
            continue
        name = str(rec.get("name") or "").strip()
        if not name:
            continue
        rows.append(rec)
    # 容忍直接传入未过滤的台账记录：带 asof 的行只留报告日当天的；
    # 没有 asof（lifecycle JSON 回退路径）或当天一行都没有时按原样使用。
    if report_day:
        dated = [r for r in rows if str(r.get("asof") or "")[:10] == report_day]
        if dated:
            rows = dated
    if not rows:
        return None
    rows.sort(
        key=lambda r: (
            _theme_share(r) is None,
            -(_theme_share(r) or 0.0),
            -_theme_stars(r),
            str(r.get("name") or ""),
        )
    )
    rows = rows[:_MAX_THEME_ROWS]

    rows_html: List[str] = []
    prev_days: List[str] = []
    for rec in rows:
        name = str(rec.get("name") or "")
        key = _theme_key(rec)
        share = _theme_share(rec)
        stars = _theme_stars(rec)
        color = TIER_COLORS[stars]
        series = _share_history(ledger_records, key, report_day)
        prev_day, prev_val = _prev_share(series, report_day)
        if prev_day:
            prev_days.append(prev_day)
        delta = None
        tip = ""
        if share is not None and prev_val is not None:
            delta = (share - prev_val) * 100
            tip = f"较 {prev_day}：{prev_val * 100:.1f}% → {share * 100:.1f}%"
        points = [value * 100 for _day, value in series][-_SPARK_DAYS:]

        badge = "★" * stars if stars else "—"
        tags = [_state_chip(str(rec["state"]))] if rec.get("state") else []
        if not rec.get("in_pool", True):
            tags.append('<span class="ubr-tag">池外</span>')
        if rec.get("is_new_direction"):
            tags.append('<span class="ubr-tag new">新方向</span>')
        num_html = "—" if share is None else f"{share * 100:.1f}<i>%</i>"
        fill_w = max(0.0, min((share or 0.0) * 100, 100.0))
        hot = stars >= 3
        rows_html.append(
            f'<div class="ubr{" hot" if hot else ""}" style="--pc:{color}">'
            f'<div class="ubr-fill" style="width:{fill_w:.1f}%"></div>'
            f'<div class="ubr-edge"></div>'
            f'<div class="ubr-id"><span class="ubr-badge">{escape(badge)}</span>'
            f'<span class="ubr-name">{escape(name)}</span>{"".join(tags)}</div>'
            f'<div class="ubr-num">{num_html}</div>'
            f"{_delta_chip(delta, prev_day, tip=tip)}"
            f'<div class="ubr-spark">{_spark_svg(points, color, unit="%")}</div>'
            "</div>"
        )
    if not rows_html:
        return None

    prev_used = max(set(prev_days), key=prev_days.count) if prev_days else None
    subtitle_bits = [f"截至 {report_day}" if report_day else "当日快照"]
    subtitle_bits.append(f"隔日基准 {prev_used}" if prev_used else "无隔日基准")
    if prev_used:
        caption = (
            "行填充与大数=<b>成交额占比</b>（口径以当日台账登记为准——全市场扫描或观察池内）；"
            f"中列=占比较 {escape(prev_used)} 的百分点变动；右端=近 {min(len(prev_days) + 1, _SPARK_DAYS)} 期占比轨迹；"
            "行色=★ 确认度（蓝=★★★、青=★★、灰=★及以下）。数据来自跨日主题台账。"
        )
    else:
        caption = (
            "行填充与大数=<b>成交额占比</b>（口径以当日台账登记为准）；"
            "无上一期可比，本板只画当日快照。数据来自跨日主题台账。"
        )
    return StaticFigure(
        html='<div class="ubr-board">' + "".join(rows_html) + "</div>",
        anchor=("板块赚钱效应",),
        placement="after",
        title="主题赚钱效应 · 成交额占比",
        subtitle=" · ".join(subtitle_bits),
        caption=caption,
        css=_BOARD_CSS,
    )


_BOARD_CSS = """
.ubr-board { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
.ubr {
  position: relative; display: grid; align-items: center; gap: 14px;
  grid-template-columns: minmax(0, 1fr) 96px 100px 128px;
  padding: 10px 16px; border: 1px solid var(--line-2); border-radius: var(--r-md);
  background: var(--surface); overflow: hidden;
}
.ubr.hot { border-color: var(--pc); }
.ubr-fill { position: absolute; top: 0; bottom: 0; left: 0; background: var(--pc); opacity: 0.10; pointer-events: none; }
.ubr-edge { position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--pc); opacity: 0.9; pointer-events: none; }
.ubr-id, .ubr-num, .ubr-delta, .ubr-spark { position: relative; z-index: 1; }
.ubr-id { display: flex; align-items: center; min-width: 0; flex-wrap: wrap; row-gap: 3px; }
.ubr-badge {
  flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center;
  min-width: 34px; padding: 2px 8px; border-radius: 6px;
  background: var(--pc); color: #fff;
  font-family: var(--font-mono); font-size: 12.5px; font-weight: 700; letter-spacing: 0.5px;
}
.ubr-name { margin-left: 9px; font-size: 14px; font-weight: 600; color: var(--ink-1); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ubr-sub { margin-left: 9px; font-size: 11px; color: var(--ink-4); font-family: var(--font-mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ubr-state {
  flex: 0 0 auto; margin-left: 8px; padding: 0 8px; border: 1px solid currentColor;
  border-radius: var(--pill); font-size: 10.5px; font-weight: 600; line-height: 18px; white-space: nowrap;
}
.ubr-tag {
  flex: 0 0 auto; margin-left: 8px; padding: 0 8px; border-radius: var(--pill);
  background: var(--surface-3); color: var(--ink-3); font-size: 10.5px; font-weight: 600;
  line-height: 18px; white-space: nowrap;
}
.ubr-tag.new { background: var(--accent-soft); color: var(--accent-ink); }
.ubr-num { font-family: var(--font-mono); font-size: 20px; font-weight: 700; color: var(--ink-1); text-align: right; }
.ubr-num.up { color: var(--pos); }
.ubr-num.down { color: var(--neg); }
.ubr-num i { font-style: normal; font-size: 12px; font-weight: 500; color: var(--ink-4); margin-left: 1px; }
.ubr-delta { font-family: var(--font-mono); font-size: 12.5px; font-weight: 600; border-radius: var(--pill); padding: 3px 0; text-align: center; white-space: nowrap; }
.ubr-delta.up { color: var(--pos); background: var(--pos-soft); }
.ubr-delta.down { color: var(--neg); background: var(--neg-soft); }
.ubr-delta.flat, .ubr-delta.none { color: var(--ink-3); background: var(--surface-3); }
.ubr-spark svg { display: block; width: 100%; height: 26px; }
.ubr-nospark { color: var(--ink-4); font-size: 12px; display: block; text-align: center; }
@media (max-width: 720px) {
  .ubr { grid-template-columns: minmax(0, 1fr) 76px 88px; gap: 9px; padding: 9px 12px; }
  .ubr-spark { display: none; }
}
"""


# --------------------------------------------------------------------------- #
# 新方向卡片 (view-time decoration over the 池外新方向 ordered list)
# --------------------------------------------------------------------------- #
NEW_DIRECTION_JS = r"""(function () {
  const root = document.getElementById("report-body");
  if (!root) return;
  const h = Array.from(root.querySelectorAll("h2, h3")).find(
    x => x.textContent.replace(/\s/g, "").indexOf("池外新方向") === 0);
  if (!h) return;
  /* 引擎把带空行的编号列表拆成 <ol> / <ol start="2"> / … 多个元素，
     要按文档序收齐整组，编号取 start 属性。 */
  let cur = h.nextElementSibling;
  const ols = [];
  while (cur && !/^H[1-3]$/.test(cur.tagName)) {
    if (cur.tagName === "OL") ols.push(cur);
    cur = cur.nextElementSibling;
  }
  if (!ols.length) return;
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
      /* 模板句式是「**标题**：正文」——标题提走后正文还留着冒号，顺手剥掉 */
      txt.innerHTML = (first && first.tagName === "STRONG")
        ? li.innerHTML.replace(/^\s*[：:]\s*/, "")
        : li.innerHTML;
      body.appendChild(txt);
      card.appendChild(no);
      card.appendChild(body);
      wrap.appendChild(card);
    });
  });
  ols[0].before(wrap);
  ols.forEach(ol => ol.remove());
})();"""

NEW_DIRECTION_CSS = """
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
.ke-text .num-pos { color: var(--pos); font-weight: 600; }
.ke-text .num-neg { color: var(--neg); font-weight: 600; }
"""


class _NewDirectionDecoration:
    """Typed decoration handle: builder.add_decoration reads .js / .css."""

    js = NEW_DIRECTION_JS
    css = NEW_DIRECTION_CSS


NEW_DIRECTION_DECORATION = _NewDirectionDecoration()


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def build_usmarket_figures(
    payload: Dict[str, Any],
    themes_today: Sequence[Dict[str, Any]],
    ledger_records: Sequence[Dict[str, Any]],
    report_day: Optional[str],
) -> Tuple[List[Any], Dict[str, bool]]:
    """Return ``(figures, meta)`` for one US-market daily report.

    ``payload`` is the renderer's extracted chart payload (index snapshots +
    K-lines); ``themes_today`` are the theme rows to display (ledger records
    for the report day when available, else the ``--themes`` payload);
    ``ledger_records`` is the full cross-day theme ledger for deltas/sparks.
    ``meta`` flags which boards were actually built, so the caller can retire
    the matching view-time fallback charts.
    """
    figures: List[Any] = []
    meta = {"index_board": False, "theme_board": False}
    if isinstance(payload, dict) and not payload.get("missing"):
        index_board = _index_board(payload, report_day)
        if index_board is not None:
            figures.append(index_board)
            meta["index_board"] = True
    theme_board = _theme_board(themes_today or [], ledger_records or [], report_day)
    if theme_board is not None:
        figures.append(theme_board)
        meta["theme_board"] = True
    return figures, meta
