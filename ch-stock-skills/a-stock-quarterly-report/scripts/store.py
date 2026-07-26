#!/usr/bin/env python3
"""Persistence + incremental-fetch cache for the quarterly-report skill.

Uses the shared connection layer `shared/data/db_core.py` (dev) / `_shared/`
(bundled) — per the house rule, skills must not roll their own DB connection.
Backend follows ALPHA_DB_BACKEND (postgresql default, sqlite offline). All tables
are namespaced `qreport_*` inside the shared alpha_data database.

Cache tables
------------
- qreport_disclosure     : (period, ts_code) 预约/实际披露日 — the discovery layer
- qreport_fin_cache      : (ts_code, period) one cumulative period of statement
                           line items as a JSON blob, plus a `sources` marker for
                           which endpoints have been fetched. A JSON blob rather
                           than 40 columns because the field set is read whole,
                           never filtered on, and grows as the methodology does.
- qreport_basic_cache    : stock name/industry/area/market
- qreport_daily_cache    : qfq daily bars for the price-reaction engine
- qreport_cninfo_fetch_log / qreport_cninfo_announcement : official report
                           provenance (title + original PDF link), scanned in bulk
- qreport_pdf_section    : (ts_code, period, section) on-demand PDF slices
- qreport_forecast_ref   : forecast / express reference values for 兑现度
- qreport_verdict        : model verdicts (tier + theme attribution) per (period, ts_code)
- qreport_theme_trend    : model-judged report-period industry trend per (period, theme_id)
- qreport_period_overview: model-written 产业结构综述 per period (sample fingerprint)

Writes go through one batched statement per table (execute_values on PostgreSQL,
executemany on SQLite); reads are batched with IN-lists.

If the database cannot be opened the Store degrades to `available = False` and
every method is a no-op — callers then run in non-incremental (full-fetch) mode
so the skill still works without a database.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

try:
    from db_core import BACKEND, Backend, adapt_sql, get_connection  # type: ignore
    _DB_IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # noqa: BLE001 - degrade if shared layer missing
    BACKEND = Backend = adapt_sql = get_connection = None  # type: ignore
    _DB_IMPORT_ERROR = str(exc)


_SCHEMA: Dict[str, str] = {
    "qreport_disclosure": """
        CREATE TABLE IF NOT EXISTS qreport_disclosure (
            period TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            pre_date TEXT,
            actual_date TEXT,
            ann_date TEXT,
            fetched_at TEXT,
            PRIMARY KEY (period, ts_code)
        )""",
    "qreport_fin_cache": """
        CREATE TABLE IF NOT EXISTS qreport_fin_cache (
            ts_code TEXT NOT NULL,
            period TEXT NOT NULL,
            ann_date TEXT,
            sources TEXT,
            data_json TEXT,
            fetched_at TEXT,
            PRIMARY KEY (ts_code, period)
        )""",
    "qreport_basic_cache": """
        CREATE TABLE IF NOT EXISTS qreport_basic_cache (
            ts_code TEXT PRIMARY KEY,
            name TEXT, industry TEXT, area TEXT, market TEXT,
            fetched_at TEXT
        )""",
    "qreport_daily_cache": """
        CREATE TABLE IF NOT EXISTS qreport_daily_cache (
            ts_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, pre_close REAL,
            pct_chg REAL, vol REAL, amount REAL, adj_factor REAL,
            PRIMARY KEY (ts_code, trade_date)
        )""",
    "qreport_cninfo_fetch_log": """
        CREATE TABLE IF NOT EXISTS qreport_cninfo_fetch_log (
            period TEXT NOT NULL,
            ann_date TEXT NOT NULL,
            row_count INTEGER,
            fetched_at TEXT,
            PRIMARY KEY (period, ann_date)
        )""",
    "qreport_cninfo_announcement": """
        CREATE TABLE IF NOT EXISTS qreport_cninfo_announcement (
            announcement_id TEXT PRIMARY KEY,
            period TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            ann_date TEXT NOT NULL,
            name TEXT,
            title TEXT,
            url TEXT,
            is_corrected INTEGER,
            is_summary INTEGER,
            fetched_at TEXT
        )""",
    "qreport_pdf_section": """
        CREATE TABLE IF NOT EXISTS qreport_pdf_section (
            ts_code TEXT NOT NULL,
            period TEXT NOT NULL,
            section TEXT NOT NULL,
            title TEXT,
            url TEXT,
            page_span TEXT,
            text TEXT,
            fetched_at TEXT,
            PRIMARY KEY (ts_code, period, section)
        )""",
    "qreport_forecast_ref": """
        CREATE TABLE IF NOT EXISTS qreport_forecast_ref (
            ts_code TEXT NOT NULL,
            period TEXT NOT NULL,
            kind TEXT NOT NULL,
            ann_date TEXT,
            type TEXT,
            np_min REAL, np_max REAL,
            p_change_min REAL, p_change_max REAL,
            revenue REAL,
            summary TEXT,
            change_reason TEXT,
            fetched_at TEXT,
            PRIMARY KEY (ts_code, period, kind)
        )""",
    "qreport_forecast_fetch_log": """
        CREATE TABLE IF NOT EXISTS qreport_forecast_fetch_log (
            period TEXT NOT NULL,
            ann_date TEXT NOT NULL,
            row_count INTEGER,
            fetched_at TEXT,
            PRIMARY KEY (period, ann_date)
        )""",
    "qreport_verdict": """
        CREATE TABLE IF NOT EXISTS qreport_verdict (
            period TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            tier TEXT NOT NULL,
            reason TEXT,
            caveat TEXT,
            quality_call TEXT,
            fulfillment TEXT,
            theme_id TEXT,
            theme_rationale TEXT,
            match_confidence TEXT,
            evidence_ann_date TEXT,
            evidence_np_single_yoy REAL,
            evidence_rev_single_yoy REAL,
            judged_at TEXT,
            PRIMARY KEY (period, ts_code)
        )""",
    "qreport_theme_trend": """
        CREATE TABLE IF NOT EXISTS qreport_theme_trend (
            period TEXT NOT NULL,
            theme_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            strong_common TEXT,
            weak_common TEXT,
            cross_validation TEXT,
            confidence TEXT,
            evidence_strong_n INTEGER,
            evidence_weak_n INTEGER,
            evidence_member_n INTEGER,
            judged_at TEXT,
            PRIMARY KEY (period, theme_id)
        )""",
    "qreport_period_overview": """
        CREATE TABLE IF NOT EXISTS qreport_period_overview (
            period TEXT PRIMARY KEY,
            overview TEXT NOT NULL,
            evidence_total INTEGER,
            evidence_growth INTEGER,
            evidence_decline INTEGER,
            judged_at TEXT
        )""",
}

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_qreport_fin_period ON qreport_fin_cache(period)",
    "CREATE INDEX IF NOT EXISTS idx_qreport_cninfo_period ON qreport_cninfo_announcement(period)",
    "CREATE INDEX IF NOT EXISTS idx_qreport_cninfo_ann_date ON qreport_cninfo_announcement(period, ann_date)",
    "CREATE INDEX IF NOT EXISTS idx_qreport_disclosure_actual ON qreport_disclosure(period, actual_date)",
]

_DISCLOSURE_COLS = ["period", "ts_code", "pre_date", "actual_date", "ann_date", "fetched_at"]
_FIN_COLS = ["ts_code", "period", "ann_date", "sources", "data_json", "fetched_at"]
_BASIC_COLS = ["ts_code", "name", "industry", "area", "market", "fetched_at"]
_BAR_COLS = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
             "pct_chg", "vol", "amount", "adj_factor"]
_ANN_COLS = ["announcement_id", "period", "ts_code", "ann_date", "name", "title", "url",
             "is_corrected", "is_summary", "fetched_at"]
_PDF_COLS = ["ts_code", "period", "section", "title", "url", "page_span", "text", "fetched_at"]
_FCREF_COLS = ["ts_code", "period", "kind", "ann_date", "type", "np_min", "np_max",
               "p_change_min", "p_change_max", "revenue", "summary", "change_reason", "fetched_at"]
_VERDICT_COLS = ["period", "ts_code", "tier", "reason", "caveat", "quality_call", "fulfillment",
                 "theme_id", "theme_rationale", "match_confidence", "evidence_ann_date",
                 "evidence_np_single_yoy", "evidence_rev_single_yoy", "judged_at"]
_TREND_COLS = ["period", "theme_id", "direction", "strong_common", "weak_common", "cross_validation",
               "confidence", "evidence_strong_n", "evidence_weak_n", "evidence_member_n", "judged_at"]
_OVERVIEW_COLS = ["period", "overview", "evidence_total", "evidence_growth",
                  "evidence_decline", "judged_at"]


def _now() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def qfq_adjust_bars(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Derive a consistent qfq series from persisted raw bars."""
    if not rows:
        return []
    factors = [(str(r.get("trade_date") or ""), r.get("adj_factor")) for r in rows
               if r.get("adj_factor")]
    if not factors:
        return [dict(r) for r in rows]
    latest = max(factors, key=lambda item: item[0])[1]
    if not latest:
        return [dict(r) for r in rows]
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        factor = (item.get("adj_factor") or latest) / latest
        for col in ("open", "high", "low", "close", "pre_close"):
            if item.get(col) is not None:
                item[col] = round(float(item[col]) * factor, 2)
        out.append(item)
    return out


def _chunks(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class Store:
    """Thin cache over db_core. Never raises to the caller; degrades to no-op."""

    def __init__(self, enabled: bool = True):
        self.available = False
        self.reason = ""
        if not enabled:
            self.reason = "cache disabled by flag"
            return
        if get_connection is None:
            self.reason = f"db_core unavailable: {_DB_IMPORT_ERROR}"
            return
        try:
            with get_connection() as conn:
                cur = conn.cursor()
                for ddl in _SCHEMA.values():
                    cur.execute(adapt_sql(ddl))
                bar_factor_added = self._ensure_column(
                    cur, "qreport_daily_cache", "adj_factor", "REAL")
                self._ensure_column(
                    cur, "qreport_cninfo_announcement", "name", "TEXT")
                if bar_factor_added:
                    # Old cache rows were already qfq-adjusted and have no
                    # factor, so mixing them with raw rows would create gaps.
                    cur.execute(adapt_sql("DELETE FROM qreport_daily_cache"))
                for idx in _INDEXES:
                    cur.execute(adapt_sql(idx))
            self.available = True
        except Exception as exc:  # noqa: BLE001
            self.reason = f"db connect/init failed: {str(exc)[:120]}"

    @staticmethod
    def _ensure_column(cur: Any, table: str, column: str, sql_type: str) -> bool:
        """Add a cache column in place; return whether a migration ran."""
        if BACKEND == Backend.POSTGRESQL:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = %s",
                (table, column),
            )
            exists = cur.fetchone() is not None
        else:
            cur.execute(f"PRAGMA table_info({table})")
            exists = column in {str(row[1]) for row in cur.fetchall()}
        if not exists:
            cur.execute(adapt_sql(
                f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
            return True
        return False

    # -- generic helpers ----------------------------------------------------
    def _rows(self, sql: str, params: Sequence[Any], cols: Sequence[str]) -> List[Dict[str, Any]]:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(adapt_sql(sql), tuple(params))
            return [{c: r[i] for i, c in enumerate(cols)} for r in cur.fetchall()]

    def _batch_upsert(self, table: str, cols: Sequence[str], conflict_cols: Sequence[str],
                      rows: Sequence[Sequence[Any]], update_cols: Optional[Sequence[str]] = None,
                      where: Optional[str] = None) -> None:
        """One batched INSERT ... ON CONFLICT for the whole row set."""
        if not rows:
            return
        # De-dupe by conflict key (keep last): PostgreSQL's single INSERT ...
        # ON CONFLICT errors if the same conflict target appears twice.
        if conflict_cols and len(rows) > 1:
            key_idx = [cols.index(c) for c in conflict_cols]
            deduped: Dict[Any, Sequence[Any]] = {}
            for r in rows:
                deduped[tuple(r[i] for i in key_idx)] = r
            rows = list(deduped.values())
        update_cols = update_cols or [c for c in cols if c not in conflict_cols]
        set_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
        conflict = ", ".join(conflict_cols)
        tail = f" ON CONFLICT({conflict}) DO UPDATE SET {set_clause}" + (f" WHERE {where}" if where else "")
        with get_connection() as conn:
            cur = conn.cursor()
            if BACKEND == Backend.POSTGRESQL:
                from psycopg2.extras import execute_values
                sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s{tail}"
                for chunk in _chunks(rows, 1000):
                    execute_values(cur, sql, [tuple(r) for r in chunk], page_size=1000)
            else:
                marks = "(" + ", ".join(["?"] * len(cols)) + ")"
                sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES {marks}{tail}"
                cur.executemany(sql, [tuple(r) for r in rows])

    @staticmethod
    def _in_clause(items: Sequence[str]) -> str:
        return ", ".join(["?"] * len(items))

    # -- disclosure calendar (the discovery layer) --------------------------
    def load_disclosure(self, period: str) -> Dict[str, Dict[str, Any]]:
        if not self.available:
            return {}
        try:
            rows = self._rows(
                "SELECT ts_code, pre_date, actual_date, ann_date FROM qreport_disclosure WHERE period = ?",
                [period], ["ts_code", "pre_date", "actual_date", "ann_date"],
            )
            return {str(r["ts_code"]): r for r in rows}
        except Exception:  # noqa: BLE001
            return {}

    def upsert_disclosure(self, period: str, rows: Sequence[Dict[str, Any]]) -> None:
        if not self.available or not rows:
            return
        try:
            now = _now()
            self._batch_upsert(
                "qreport_disclosure", _DISCLOSURE_COLS, ["period", "ts_code"],
                [(period, r["ts_code"], r.get("pre_date"), r.get("actual_date"),
                  r.get("ann_date"), now) for r in rows],
            )
        except Exception:  # noqa: BLE001
            pass

    # -- financial statement cache ------------------------------------------
    def load_fin_many(self, ts_codes: Sequence[str]) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """{ts_code: {period: {"ann_date":…, "sources": set, **line_items}}}."""
        if not self.available or not ts_codes:
            return {}
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        try:
            for chunk in _chunks(list(ts_codes), 900):
                rows = self._rows(
                    "SELECT ts_code, period, ann_date, sources, data_json FROM qreport_fin_cache "
                    f"WHERE ts_code IN ({self._in_clause(chunk)})",
                    list(chunk), ["ts_code", "period", "ann_date", "sources", "data_json"],
                )
                for r in rows:
                    data = {}
                    if r["data_json"]:
                        try:
                            data = json.loads(r["data_json"])
                        except Exception:  # noqa: BLE001
                            data = {}
                    data["ann_date"] = r["ann_date"]
                    data["_sources"] = set((r["sources"] or "").split(",")) - {""}
                    out.setdefault(str(r["ts_code"]), {})[str(r["period"])] = data
        except Exception:  # noqa: BLE001
            return out
        return out

    def upsert_fin_many(self, records: Sequence[Dict[str, Any]]) -> None:
        """records: [{ts_code, period, ann_date, sources:set|list, data:dict}]."""
        if not self.available or not records:
            return
        try:
            now = _now()
            payload = []
            for r in records:
                data = {k: v for k, v in (r.get("data") or {}).items() if not k.startswith("_")}
                payload.append((
                    r["ts_code"], r["period"], r.get("ann_date"),
                    ",".join(sorted(r.get("sources") or [])),
                    json.dumps(data, ensure_ascii=False, allow_nan=False),
                    now,
                ))
            self._batch_upsert("qreport_fin_cache", _FIN_COLS, ["ts_code", "period"], payload)
        except Exception:  # noqa: BLE001
            pass

    def delete_fin_periods(self, ts_codes: Sequence[str], periods: Sequence[str]) -> None:
        """Drop cached statement rows before a forced refresh (restatements)."""
        if not self.available or not ts_codes or not periods:
            return
        try:
            with get_connection() as conn:
                cur = conn.cursor()
                for chunk in _chunks(list(ts_codes), 500):
                    cur.execute(
                        adapt_sql(
                            f"DELETE FROM qreport_fin_cache WHERE ts_code IN ({self._in_clause(chunk)}) "
                            f"AND period IN ({self._in_clause(list(periods))})"
                        ),
                        tuple(list(chunk) + list(periods)),
                    )
        except Exception:  # noqa: BLE001
            pass

    # -- stock basic --------------------------------------------------------
    def load_basic(self, ts_codes: Sequence[str]) -> Dict[str, Dict[str, str]]:
        if not self.available or not ts_codes:
            return {}
        out: Dict[str, Dict[str, str]] = {}
        try:
            for chunk in _chunks(list(ts_codes), 900):
                rows = self._rows(
                    "SELECT ts_code, name, industry, area, market FROM qreport_basic_cache "
                    f"WHERE ts_code IN ({self._in_clause(chunk)})",
                    list(chunk), ["ts_code", "name", "industry", "area", "market"],
                )
                for r in rows:
                    out[str(r["ts_code"])] = {k: (r[k] or "") for k in ("name", "industry", "area", "market")}
        except Exception:  # noqa: BLE001
            return out
        return out

    def upsert_basic(self, basic: Dict[str, Dict[str, str]]) -> None:
        if not self.available or not basic:
            return
        try:
            now = _now()
            self._batch_upsert(
                "qreport_basic_cache", _BASIC_COLS, ["ts_code"],
                [(code, b.get("name", ""), b.get("industry", ""), b.get("area", ""),
                  b.get("market", ""), now) for code, b in basic.items()],
            )
        except Exception:  # noqa: BLE001
            pass

    # -- CNInfo provenance ---------------------------------------------------
    def logged_cninfo_ann_dates(self, period: str) -> Set[str]:
        if not self.available:
            return set()
        try:
            rows = self._rows(
                "SELECT ann_date FROM qreport_cninfo_fetch_log WHERE period = ?",
                [period], ["ann_date"],
            )
            return {str(r["ann_date"]) for r in rows}
        except Exception:  # noqa: BLE001
            return set()

    def record_cninfo_fetch_days(self, period: str, day_counts: Dict[str, int]) -> None:
        if not self.available or not day_counts:
            return
        try:
            self._batch_upsert(
                "qreport_cninfo_fetch_log", ["period", "ann_date", "row_count", "fetched_at"],
                ["period", "ann_date"],
                [(period, day, count, _now()) for day, count in day_counts.items()],
            )
        except Exception:  # noqa: BLE001
            pass

    def upsert_cninfo_announcements(self, period: str, rows: Sequence[Dict[str, Any]]) -> bool:
        if not self.available:
            return False
        if not rows:
            return True
        try:
            now = _now()
            self._batch_upsert(
                "qreport_cninfo_announcement", _ANN_COLS, ["announcement_id"],
                [(r["announcement_id"], period, r["ts_code"], r["ann_date"], r.get("name"),
                  r.get("title"),
                  r.get("url"), 1 if r.get("is_corrected") else 0,
                  1 if r.get("is_summary") else 0, now) for r in rows],
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    def load_cninfo_announcements(self, period: str) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        cols = ["announcement_id", "ts_code", "ann_date", "name", "title", "url",
                "is_corrected", "is_summary"]
        try:
            return self._rows(
                f"SELECT {', '.join(cols)} FROM qreport_cninfo_announcement "
                "WHERE period = ? ORDER BY ann_date, announcement_id",
                [period], cols,
            )
        except Exception:  # noqa: BLE001
            return []

    # -- on-demand PDF sections ---------------------------------------------
    def load_pdf_sections(self, ts_code: str, period: str) -> Dict[str, Dict[str, Any]]:
        if not self.available:
            return {}
        cols = ["section", "title", "url", "page_span", "text"]
        try:
            rows = self._rows(
                f"SELECT {', '.join(cols)} FROM qreport_pdf_section WHERE ts_code = ? AND period = ?",
                [ts_code, period], cols,
            )
            return {str(r["section"]): r for r in rows}
        except Exception:  # noqa: BLE001
            return {}

    def upsert_pdf_sections(self, records: Sequence[Dict[str, Any]]) -> None:
        if not self.available or not records:
            return
        try:
            now = _now()
            self._batch_upsert(
                "qreport_pdf_section", _PDF_COLS, ["ts_code", "period", "section"],
                [(r["ts_code"], r["period"], r["section"], r.get("title"), r.get("url"),
                  r.get("page_span"), r.get("text"), now) for r in records],
            )
        except Exception:  # noqa: BLE001
            pass

    # -- forecast / express reference (兑现度) --------------------------------
    def logged_forecast_ann_dates(self, period: str) -> Set[str]:
        if not self.available:
            return set()
        try:
            rows = self._rows(
                "SELECT ann_date FROM qreport_forecast_fetch_log WHERE period = ?",
                [period], ["ann_date"],
            )
            return {str(r["ann_date"]) for r in rows}
        except Exception:  # noqa: BLE001
            return set()

    def record_forecast_fetch_days(self, period: str, day_counts: Dict[str, int]) -> None:
        if not self.available or not day_counts:
            return
        try:
            self._batch_upsert(
                "qreport_forecast_fetch_log", ["period", "ann_date", "row_count", "fetched_at"],
                ["period", "ann_date"],
                [(period, day, count, _now()) for day, count in day_counts.items()],
            )
        except Exception:  # noqa: BLE001
            pass

    def upsert_forecast_ref(self, records: Sequence[Dict[str, Any]]) -> bool:
        if not self.available:
            return False
        if not records:
            return True
        try:
            now = _now()
            self._batch_upsert(
                "qreport_forecast_ref", _FCREF_COLS, ["ts_code", "period", "kind"],
                [(r["ts_code"], r["period"], r["kind"], r.get("ann_date"), r.get("type"),
                  r.get("np_min"), r.get("np_max"), r.get("p_change_min"), r.get("p_change_max"),
                  r.get("revenue"), r.get("summary"), r.get("change_reason"), now) for r in records],
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    def load_forecast_ref(self, period: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """{ts_code: {kind: row}} where kind is 'forecast' or 'express'."""
        if not self.available:
            return {}
        try:
            rows = self._rows(
                f"SELECT {', '.join(_FCREF_COLS[:-1])} FROM qreport_forecast_ref WHERE period = ?",
                [period], _FCREF_COLS[:-1],
            )
        except Exception:  # noqa: BLE001
            return {}
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(str(r["ts_code"]), {})[str(r["kind"])] = r
        return out

    # -- model verdicts ------------------------------------------------------
    def load_verdicts(self, period: str) -> Dict[str, Dict[str, Any]]:
        if not self.available:
            return {}
        try:
            rows = self._rows(
                f"SELECT {', '.join(_VERDICT_COLS)} FROM qreport_verdict WHERE period = ?",
                [period], _VERDICT_COLS,
            )
            return {str(r["ts_code"]): r for r in rows}
        except Exception:  # noqa: BLE001
            return {}

    def upsert_verdicts(self, records: Sequence[Dict[str, Any]]) -> bool:
        if not self.available or not records:
            return False
        try:
            now = _now()
            self._batch_upsert(
                "qreport_verdict", _VERDICT_COLS, ["period", "ts_code"],
                [(r["period"], r["ts_code"], r["tier"], r.get("reason"), r.get("caveat"),
                  r.get("quality_call"), r.get("fulfillment"), r.get("theme_id"),
                  r.get("theme_rationale"), r.get("match_confidence"), r.get("evidence_ann_date"),
                  r.get("evidence_np_single_yoy"), r.get("evidence_rev_single_yoy"), now)
                 for r in records],
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- model theme trends --------------------------------------------------
    def load_theme_trends(self, period: str) -> Dict[str, Dict[str, Any]]:
        if not self.available:
            return {}
        try:
            rows = self._rows(
                f"SELECT {', '.join(_TREND_COLS)} FROM qreport_theme_trend WHERE period = ?",
                [period], _TREND_COLS,
            )
            return {str(r["theme_id"]): r for r in rows}
        except Exception:  # noqa: BLE001
            return {}

    def upsert_theme_trends(self, records: Sequence[Dict[str, Any]]) -> bool:
        if not self.available or not records:
            return False
        try:
            now = _now()
            self._batch_upsert(
                "qreport_theme_trend", _TREND_COLS, ["period", "theme_id"],
                [(r["period"], r["theme_id"], r["direction"], r.get("strong_common"),
                  r.get("weak_common"), r.get("cross_validation"), r.get("confidence"),
                  r.get("evidence_strong_n"), r.get("evidence_weak_n"),
                  r.get("evidence_member_n"), now) for r in records],
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- model period overview ----------------------------------------------
    def load_period_overview(self, period: str) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        try:
            rows = self._rows(
                f"SELECT {', '.join(_OVERVIEW_COLS)} FROM qreport_period_overview WHERE period = ?",
                [period], _OVERVIEW_COLS,
            )
            return rows[0] if rows else None
        except Exception:  # noqa: BLE001
            return None

    def upsert_period_overview(self, record: Dict[str, Any]) -> bool:
        if not self.available or not record:
            return False
        try:
            self._batch_upsert(
                "qreport_period_overview", _OVERVIEW_COLS, ["period"],
                [(record["period"], record["overview"], record.get("evidence_total"),
                  record.get("evidence_growth"), record.get("evidence_decline"), _now())],
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- daily bars ----------------------------------------------------------
    def cached_bar_dates(self, start: str, end: str) -> Set[str]:
        """Trading days already stored, so a rerun only fetches the new tail."""
        if not self.available:
            return set()
        try:
            rows = self._rows(
                "SELECT DISTINCT trade_date FROM qreport_daily_cache WHERE trade_date BETWEEN ? AND ?",
                [start, end], ["trade_date"],
            )
            return {str(r["trade_date"]) for r in rows}
        except Exception:  # noqa: BLE001
            return set()

    def load_bars_many(self, ts_codes: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
        if not self.available or not ts_codes:
            return {}
        out: Dict[str, List[Dict[str, Any]]] = {}
        try:
            for chunk in _chunks(list(ts_codes), 900):
                rows = self._rows(
                    f"SELECT {', '.join(_BAR_COLS)} FROM qreport_daily_cache "
                    f"WHERE ts_code IN ({self._in_clause(chunk)}) ORDER BY trade_date",
                    list(chunk), _BAR_COLS,
                )
                for r in rows:
                    out.setdefault(str(r["ts_code"]), []).append(r)
        except Exception:  # noqa: BLE001
            return out
        return {code: qfq_adjust_bars(rows) for code, rows in out.items()}

    def upsert_bars_many(self, records: Sequence[Dict[str, Any]]) -> None:
        if not self.available or not records:
            return
        try:
            self._batch_upsert(
                "qreport_daily_cache", _BAR_COLS, ["ts_code", "trade_date"],
                [tuple(r.get(c) for c in _BAR_COLS) for r in records],
            )
        except Exception:  # noqa: BLE001
            pass

    def delete_bars_many(self, ts_codes: Sequence[str]) -> None:
        if not self.available or not ts_codes:
            return
        try:
            with get_connection() as conn:
                cur = conn.cursor()
                for chunk in _chunks(list(ts_codes), 900):
                    cur.execute(
                        adapt_sql(f"DELETE FROM qreport_daily_cache WHERE ts_code IN ({self._in_clause(chunk)})"),
                        tuple(chunk),
                    )
        except Exception:  # noqa: BLE001
            pass

    # -- theme registry / lifecycle (READ-ONLY; owned by daily-market-sense) --
    def load_theme_registry(self) -> Dict[str, Dict[str, Any]]:
        """{theme_id: {name, aliases[], overlay, status}}. Empty if the theme
        tables are absent/empty — this skill never creates or writes them."""
        if not self.available:
            return {}
        try:
            rows = self._rows(
                "SELECT theme_id, name, aliases, overlay, status FROM theme_registry",
                [], ["theme_id", "name", "aliases", "overlay", "status"],
            )
        except Exception:  # noqa: BLE001
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            aliases = r.get("aliases")
            if isinstance(aliases, str):
                try:
                    aliases = json.loads(aliases)
                except Exception:  # noqa: BLE001
                    aliases = []
            out[str(r["theme_id"])] = {
                "name": r.get("name") or "", "aliases": aliases or [],
                "overlay": r.get("overlay"), "status": r.get("status"),
            }
        return out

    def load_theme_latest_state(self) -> Dict[str, Dict[str, Any]]:
        """{theme_id: latest theme_daily_state row}."""
        if not self.available:
            return {}
        try:
            rows = self._rows(
                "SELECT theme_id, trade_date, stars, position, crowding, state, members_sample "
                "FROM theme_daily_state ORDER BY trade_date",
                [], ["theme_id", "trade_date", "stars", "position", "crowding", "state", "members_sample"],
            )
        except Exception:  # noqa: BLE001
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:  # ascending → last write per theme_id wins = latest
            members = r.get("members_sample")
            if isinstance(members, str):
                try:
                    members = json.loads(members)
                except Exception:  # noqa: BLE001
                    members = []
            out[str(r["theme_id"])] = {
                "trade_date": str(r.get("trade_date") or ""),
                "stars": r.get("stars"), "position": r.get("position"),
                "crowding": r.get("crowding"), "state": r.get("state"),
                "members_sample": members or [],
            }
        return out
