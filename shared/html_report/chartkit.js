/* shared/html_report chart kit — one copy of the SVG + DOM primitives that
 * every skill's chart hook used to redefine for itself. Injected once by the
 * builder (right after the base UI script, before any ChartHook IIFE), so
 * hook JS can read everything off `window.CK` instead of carrying private
 * copies of svgEl / svgText / tooltip / card / legend / heading-finders.
 *
 * Skill-specific number formatting (price precision, 亿/万亿 unit divisors)
 * stays in each skill's hook because the units genuinely differ; only the
 * truly shared plumbing lives here. */
window.CK = (function () {
  const NS = "http://www.w3.org/2000/svg";

  function svgEl(name, attrs) {
    const el = document.createElementNS(NS, name);
    Object.entries(attrs || {}).forEach(([k, v]) => el.setAttribute(k, v));
    return el;
  }

  function svgText(x, y, text, anchor, color, size) {
    const el = svgEl("text", {
      x, y,
      "text-anchor": anchor || "start",
      fill: color || "var(--ink-4)",
      "font-size": size || 11,
    });
    el.textContent = text;
    return el;
  }

  const fmt = {
    /* YYYYMMDD -> YYYY-MM-DD (identical across every skill) */
    date: (v) => String(v || "").replace(/^(\d{4})(\d{2})(\d{2})$/, "$1-$2-$3"),
    /* general numeric: >=100 -> 1 decimal, else 2 (override with explicit d) */
    num: (v, d) => (Number.isFinite(v) ? v.toFixed(d == null ? (Math.abs(v) >= 100 ? 1 : 2) : d) : "—"),
    /* signed percentage, e.g. +2.04% / -3.07% */
    signedPct: (v) => (Number.isFinite(v) ? `${v > 0 ? "+" : ""}${v.toFixed(2)}%` : "—"),
  };

  /* dark floating tooltip; appended to `card`, returns the element */
  function tooltip(card) {
    const tip = document.createElement("div");
    tip.style.cssText =
      "position:absolute;background:rgba(15,23,42,0.92);color:#f1f5f9;padding:8px 12px;border-radius:8px;font-size:12px;line-height:1.5;pointer-events:none;opacity:0;transition:opacity .15s ease;z-index:100;white-space:nowrap;box-shadow:0 4px 12px rgba(0,0,0,0.2);";
    card.style.position = "relative";
    card.appendChild(tip);
    return tip;
  }

  function moveTip(tip, card, e) {
    const r = card.getBoundingClientRect();
    tip.style.left = Math.min(e.clientX - r.left + 12, r.width - tip.offsetWidth - 8) + "px";
    tip.style.top = Math.max(8, e.clientY - r.top - tip.offsetHeight - 12) + "px";
  }

  /* <article class=cls><div.chart-title><div.chart-subtitle> */
  function card(cls, title, subtitle) {
    const c = document.createElement("article");
    c.className = cls;
    if (title) {
      const t = document.createElement("div");
      t.className = "chart-title";
      t.textContent = title;
      c.appendChild(t);
    }
    if (subtitle) {
      const s = document.createElement("div");
      s.className = "chart-subtitle";
      s.textContent = subtitle;
      c.appendChild(s);
    }
    return c;
  }

  /* legend from [[label, color], ...] */
  function legend(items) {
    const l = document.createElement("div");
    l.className = "legend";
    (items || []).forEach(([label, color]) => {
      const span = document.createElement("span");
      span.style.setProperty("--legend-color", color);
      span.textContent = label;
      l.appendChild(span);
    });
    return l;
  }

  function grid(cls) {
    const g = document.createElement("div");
    g.className = cls || "chart-grid";
    return g;
  }

  /* first heading (default h2/h3/h4) whose text contains one of `texts` */
  function findHeading(root, texts, sel) {
    const list = Array.from(root.querySelectorAll(sel || "h2, h3, h4"));
    const arr = Array.isArray(texts) ? texts : [texts];
    for (const t of arr) {
      const h = list.find((e) => e.textContent.includes(t));
      if (h) return h;
    }
    return null;
  }

  /* the next .table-wrap after a heading, or null if a heading intervenes */
  function findNextTable(heading) {
    let cur = heading.nextElementSibling;
    while (cur) {
      if (cur.classList && cur.classList.contains("table-wrap")) return cur;
      if (/^H[234]$/.test(cur.tagName || "")) return null;
      cur = cur.nextElementSibling;
    }
    return null;
  }

  return { NS, svgEl, svgText, fmt, tooltip, moveTip, card, legend, grid, findHeading, findNextTable };
})();
