#!/usr/bin/env python3
"""Persistence + incremental-fetch cache for the earnings-forecast skill.

Uses the shared connection layer `shared/data/db_core.py` (dev) / `_shared/`
(bundled) — per the house rule, skills must not roll their own DB connection.
Backend follows ALPHA_DB_BACKEND (postgresql default, sqlite offline). All tables
are namespaced `forecast_*` inside the shared alpha_data database.

Cache tables
------------
- forecast_cache      : latest forecast per (ts_code, end_date)
- forecast_fetch_log  : which (period, ann_date) announcement-days were scanned
- forecast_income_cache: actual quarterly income per (ts_code, period); NULL rows
                         record "fetched, no data" so newly-listed gaps aren't
                         re-queried every run
- forecast_basic_cache: stock name/industry/area/market
- forecast_enrich_cache: cninfo 扣非/营收/原因 per (ts_code, period)
- forecast_verdict     : model verdicts (tier + theme attribution) per (period, ts_code)
- forecast_theme_trend : model-judged report-period industry trend per (period, theme_id)
- forecast_period_overview: model-written 产业结构综述 per period (sample fingerprint for staleness)

Writes go through one batched statement per table (execute_values on PostgreSQL,
executemany on SQLite); reads are batched (one query per call, IN-lists for the
per-stock caches) to keep the hot loops off an N+1 connection pattern.

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
    "forecast_cache": """
        CREATE TABLE IF NOT EXISTS forecast_cache (
            ts_code TEXT NOT NULL,
            end_date TEXT NOT NULL,
            ann_date TEXT,
            type TEXT,
            p_change_min REAL, p_change_max REAL,
            net_profit_min REAL, net_profit_max REAL,
            last_parent_net REAL,
            first_ann_date TEXT,
            summary TEXT,
            change_reason TEXT,
            update_flag TEXT,
            fetched_at TEXT,
            PRIMARY KEY (ts_code, end_date)
        )""",
    "forecast_fetch_log": """
        CREATE TABLE IF NOT EXISTS forecast_fetch_log (
            period TEXT NOT NULL,
            ann_date TEXT NOT NULL,
            row_count INTEGER,
            fetched_at TEXT,
            PRIMARY KEY (period, ann_date)
        )""",
    "forecast_income_cache": """
        CREATE TABLE IF NOT EXISTS forecast_income_cache (
            ts_code TEXT NOT NULL,
            period TEXT NOT NULL,
            n_income_attr_p REAL,
            revenue REAL,
            ann_date TEXT,
            fetched_at TEXT,
            PRIMARY KEY (ts_code, period)
        )""",
    "forecast_basic_cache": """
        CREATE TABLE IF NOT EXISTS forecast_basic_cache (
            ts_code TEXT PRIMARY KEY,
            name TEXT, industry TEXT, area TEXT, market TEXT,
            fetched_at TEXT
        )""",
    "forecast_enrich_cache": """
        CREATE TABLE IF NOT EXISTS forecast_enrich_cache (
            ts_code TEXT NOT NULL,
            period TEXT NOT NULL,
            ann_date TEXT,
            title TEXT,
            url TEXT,
            parsed_json TEXT,
            text TEXT,
            fetched_at TEXT,
            PRIMARY KEY (ts_code, period)
        )""",
    "forecast_daily_cache": """
        CREATE TABLE IF NOT EXISTS forecast_daily_cache (
            ts_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, pre_close REAL,
            pct_chg REAL, vol REAL, amount REAL,
            PRIMARY KEY (ts_code, trade_date)
        )""",
    "forecast_verdict": """
        CREATE TABLE IF NOT EXISTS forecast_verdict (
            period TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            tier TEXT NOT NULL,
            reason TEXT,
            caveat TEXT,
            theme_id TEXT,
            theme_rationale TEXT,
            match_confidence TEXT,
            evidence_ann_date TEXT,
            evidence_cum_yoy REAL,
            judged_at TEXT,
            PRIMARY KEY (period, ts_code)
        )""",
    "forecast_theme_trend": """
        CREATE TABLE IF NOT EXISTS forecast_theme_trend (
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
    "forecast_period_overview": """
        CREATE TABLE IF NOT EXISTS forecast_period_overview (
            period TEXT PRIMARY KEY,
            overview TEXT NOT NULL,
            evidence_total INTEGER,
            evidence_positive INTEGER,
            evidence_negative INTEGER,
            judged_at TEXT
        )""",
}

# forecast_cache is read by end_date (period); the PK is (ts_code, end_date) so a
# period scan can't use it. Index end_date for multi-period accumulation.
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_forecast_cache_end_date ON forecast_cache(end_date)",
]

FORECAST_COLS = [
    "ts_code", "end_date", "ann_date", "type", "p_change_min", "p_change_max",
    "net_profit_min", "net_profit_max", "last_parent_net", "first_ann_date",
    "summary", "change_reason", "update_flag",
]

_INCOME_COLS = ["ts_code", "period", "n_income_attr_p", "revenue", "ann_date", "fetched_at"]
_BASIC_COLS = ["ts_code", "name", "industry", "area", "market", "fetched_at"]
_ENRICH_COLS = ["ts_code", "period", "ann_date", "title", "url", "parsed_json", "text", "fetched_at"]
_VERDICT_COLS = ["period", "ts_code", "tier", "reason", "caveat", "theme_id", "theme_rationale",
                 "match_confidence", "evidence_ann_date", "evidence_cum_yoy", "judged_at"]
_TREND_COLS = ["period", "theme_id", "direction", "strong_common", "weak_common", "cross_validation",
               "confidence", "evidence_strong_n", "evidence_weak_n", "evidence_member_n", "judged_at"]
_OVERVIEW_COLS = ["period", "overview", "evidence_total", "evidence_positive",
                  "evidence_negative", "judged_at"]
_BAR_COLS = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
             "pct_chg", "vol", "amount"]


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


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
                for idx in _INDEXES:
                    cur.execute(adapt_sql(idx))
            self.available = True
        except Exception as exc:  # noqa: BLE001
            self.reason = f"db connect/init failed: {str(exc)[:120]}"

    # -- generic helpers ----------------------------------------------------
    def _rows(self, sql: str, params: Sequence[Any], cols: Sequence[str]) -> List[Dict[str, Any]]:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(adapt_sql(sql), tuple(params))
            return [{c: r[i] for i, c in enumerate(cols)} for r in cur.fetchall()]

    def _batch_upsert(self, table: str, cols: Sequence[str], conflict_cols: Sequence[str],
                      rows: Sequence[Sequence[Any]], update_cols: Optional[Sequence[str]] = None,
                      where: Optional[str] = None) -> None:
        """One batched INSERT ... ON CONFLICT for the whole row set.

        PostgreSQL uses psycopg2 execute_values (single round-trip per chunk);
        SQLite uses executemany. Both share the same ON CONFLICT upsert body.
        """
        if not rows:
            return
        # De-dupe by conflict key (keep last). PostgreSQL's execute_values runs a
        # single INSERT ... ON CONFLICT statement, which errors if the same
        # conflict target appears twice ("cannot affect row a second time");
        # callers should pre-dedupe with domain order, this is the safety net.
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
    def _in_clause(ts_codes: Sequence[str]) -> str:
        return ", ".join(["?"] * len(ts_codes))

    # -- fetch log (incremental watermark) ----------------------------------
    def logged_ann_dates(self, period: str) -> Set[str]:
        if not self.available:
            return set()
        try:
            rows = self._rows(
                "SELECT ann_date FROM forecast_fetch_log WHERE period = ?",
                [period], ["ann_date"],
            )
            return {str(r["ann_date"]) for r in rows}
        except Exception:  # noqa: BLE001
            return set()

    def record_fetch_days(self, period: str, day_counts: Dict[str, int]) -> None:
        if not self.available or not day_counts:
            return
        try:
            self._batch_upsert(
                "forecast_fetch_log", ["period", "ann_date", "row_count", "fetched_at"],
                ["period", "ann_date"],
                [(period, d, c, _now()) for d, c in day_counts.items()],
            )
        except Exception:  # noqa: BLE001
            pass

    # -- forecast rows ------------------------------------------------------
    def upsert_forecasts(self, rows: List[Dict[str, Any]]) -> bool:
        """Returns True if persisted (or nothing to persist), False on failure —
        so the caller only records the fetch-log watermark on a real write."""
        if not self.available:
            return False
        if not rows:
            return True
        try:
            self._batch_upsert(
                "forecast_cache", FORECAST_COLS + ["fetched_at"], ["ts_code", "end_date"],
                [[r.get(c) for c in FORECAST_COLS] + [_now()] for r in rows],
                where="excluded.ann_date >= forecast_cache.ann_date",
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    def load_forecasts(self, period: str) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        try:
            return self._rows(
                f"SELECT {', '.join(FORECAST_COLS)} FROM forecast_cache WHERE end_date = ? "
                f"ORDER BY ts_code",  # stable base order; the model-facing sort by cum_yoy is stable on top
                [period], FORECAST_COLS,
            )
        except Exception:  # noqa: BLE001
            return []

    # -- income cache (batched read/write across all candidate stocks) ------
    def load_income_many(self, ts_codes: Sequence[str]) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """{ts_code: {period: {n_income_attr_p, revenue}}}. NULL values = 'known missing'."""
        if not self.available or not ts_codes:
            return {}
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        try:
            for chunk in _chunks(list(ts_codes), 900):  # stay under SQLite variable cap
                rows = self._rows(
                    f"SELECT ts_code, period, n_income_attr_p, revenue FROM forecast_income_cache "
                    f"WHERE ts_code IN ({self._in_clause(chunk)})",
                    list(chunk), ["ts_code", "period", "n_income_attr_p", "revenue"],
                )
                for r in rows:
                    out.setdefault(str(r["ts_code"]), {})[str(r["period"])] = {
                        "n_income_attr_p": r["n_income_attr_p"], "revenue": r["revenue"],
                    }
        except Exception:  # noqa: BLE001
            return out
        return out

    def upsert_income_many(self, records: Sequence[Dict[str, Any]]) -> None:
        """records: [{ts_code, period, n_income_attr_p, revenue, ann_date}]."""
        if not self.available or not records:
            return
        try:
            now = _now()
            self._batch_upsert(
                "forecast_income_cache", _INCOME_COLS, ["ts_code", "period"],
                [(r["ts_code"], r["period"], r.get("n_income_attr_p"), r.get("revenue"),
                  r.get("ann_date"), now) for r in records],
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
                    f"SELECT ts_code, name, industry, area, market FROM forecast_basic_cache "
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
                "forecast_basic_cache", _BASIC_COLS, ["ts_code"],
                [(code, b.get("name", ""), b.get("industry", ""), b.get("area", ""),
                  b.get("market", ""), now) for code, b in basic.items()],
            )
        except Exception:  # noqa: BLE001
            pass

    # -- cninfo enrichment --------------------------------------------------
    def get_enrich_many(self, ts_codes: Sequence[str], period: str) -> Dict[str, Dict[str, Any]]:
        if not self.available or not ts_codes:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        try:
            for chunk in _chunks(list(ts_codes), 900):
                rows = self._rows(
                    "SELECT ts_code, ann_date, title, url, parsed_json, text FROM forecast_enrich_cache "
                    f"WHERE period = ? AND ts_code IN ({self._in_clause(chunk)})",
                    [period, *chunk],
                    ["ts_code", "ann_date", "title", "url", "parsed_json", "text"],
                )
                for r in rows:
                    out[str(r["ts_code"])] = {
                        "announcement": {"title": r["title"], "ann_date": r["ann_date"], "url": r["url"]},
                        "parsed": json.loads(r["parsed_json"]) if r["parsed_json"] else None,
                        "text": r["text"],
                    }
        except Exception:  # noqa: BLE001
            return out
        return out

    def get_enrich(self, ts_code: str, period: str) -> Optional[Dict[str, Any]]:
        return self.get_enrich_many([ts_code], period).get(ts_code)

    def upsert_enrich_many(self, records: Sequence[Dict[str, Any]]) -> None:
        """records: [{ts_code, period, ann_date, title, url, parsed, text}]."""
        if not self.available or not records:
            return
        try:
            now = _now()
            self._batch_upsert(
                "forecast_enrich_cache", _ENRICH_COLS, ["ts_code", "period"],
                [(r["ts_code"], r["period"], r.get("ann_date"), r.get("title"), r.get("url"),
                  json.dumps(r["parsed"], ensure_ascii=False) if r.get("parsed") is not None else None,
                  r.get("text"), now) for r in records],
            )
        except Exception:  # noqa: BLE001
            pass

    # -- model verdicts (tier + theme attribution) --------------------------
    def load_verdicts(self, period: str) -> Dict[str, Dict[str, Any]]:
        if not self.available:
            return {}
        try:
            rows = self._rows(
                f"SELECT {', '.join(_VERDICT_COLS)} FROM forecast_verdict WHERE period = ?",
                [period], _VERDICT_COLS,
            )
            return {str(r["ts_code"]): r for r in rows}
        except Exception:  # noqa: BLE001
            return {}

    def upsert_verdicts(self, records: Sequence[Dict[str, Any]]) -> None:
        if not self.available or not records:
            return
        try:
            now = _now()
            self._batch_upsert(
                "forecast_verdict", _VERDICT_COLS, ["period", "ts_code"],
                [(r["period"], r["ts_code"], r["tier"], r.get("reason"), r.get("caveat"),
                  r.get("theme_id"), r.get("theme_rationale"), r.get("match_confidence"),
                  r.get("evidence_ann_date"), r.get("evidence_cum_yoy"), now) for r in records],
            )
        except Exception:  # noqa: BLE001
            pass

    # -- model theme trends (report-period industry trend per 主线) ----------
    def load_theme_trends(self, period: str) -> Dict[str, Dict[str, Any]]:
        if not self.available:
            return {}
        try:
            rows = self._rows(
                f"SELECT {', '.join(_TREND_COLS)} FROM forecast_theme_trend WHERE period = ?",
                [period], _TREND_COLS,
            )
            return {str(r["theme_id"]): r for r in rows}
        except Exception:  # noqa: BLE001
            return {}

    def upsert_theme_trends(self, records: Sequence[Dict[str, Any]]) -> None:
        if not self.available or not records:
            return
        try:
            now = _now()
            self._batch_upsert(
                "forecast_theme_trend", _TREND_COLS, ["period", "theme_id"],
                [(r["period"], r["theme_id"], r["direction"], r.get("strong_common"),
                  r.get("weak_common"), r.get("cross_validation"), r.get("confidence"),
                  r.get("evidence_strong_n"), r.get("evidence_weak_n"),
                  r.get("evidence_member_n"), now) for r in records],
            )
        except Exception:  # noqa: BLE001
            pass

    # -- model period overview (产业结构综述, one row per period) -------------
    def load_period_overview(self, period: str) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        try:
            rows = self._rows(
                f"SELECT {', '.join(_OVERVIEW_COLS)} FROM forecast_period_overview WHERE period = ?",
                [period], _OVERVIEW_COLS,
            )
            return rows[0] if rows else None
        except Exception:  # noqa: BLE001
            return None

    def upsert_period_overview(self, record: Dict[str, Any]) -> None:
        if not self.available or not record:
            return
        try:
            self._batch_upsert(
                "forecast_period_overview", _OVERVIEW_COLS, ["period"],
                [(record["period"], record["overview"], record.get("evidence_total"),
                  record.get("evidence_positive"), record.get("evidence_negative"), _now())],
            )
        except Exception:  # noqa: BLE001
            pass

    # -- daily bars (price-reaction engine; per-stock incremental) ----------
    def load_bars_many(self, ts_codes: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
        """{ts_code: [bar dict asc by trade_date]} for the forecast stocks."""
        if not self.available or not ts_codes:
            return {}
        out: Dict[str, List[Dict[str, Any]]] = {}
        try:
            for chunk in _chunks(list(ts_codes), 900):
                rows = self._rows(
                    f"SELECT {', '.join(_BAR_COLS)} FROM forecast_daily_cache "
                    f"WHERE ts_code IN ({self._in_clause(chunk)}) ORDER BY trade_date",
                    list(chunk), _BAR_COLS,
                )
                for r in rows:
                    out.setdefault(str(r["ts_code"]), []).append(r)
        except Exception:  # noqa: BLE001
            return out
        return out

    def upsert_bars_many(self, records: Sequence[Dict[str, Any]]) -> None:
        if not self.available or not records:
            return
        try:
            self._batch_upsert(
                "forecast_daily_cache", _BAR_COLS, ["ts_code", "trade_date"],
                [tuple(r.get(c) for c in _BAR_COLS) for r in records],
            )
        except Exception:  # noqa: BLE001
            pass

    def delete_bars_many(self, ts_codes: Sequence[str]) -> None:
        """Drop cached price bars before a full price-basis refresh."""
        if not self.available or not ts_codes:
            return
        try:
            with get_connection() as conn:
                cur = conn.cursor()
                for chunk in _chunks(list(ts_codes), 900):
                    cur.execute(
                        adapt_sql(f"DELETE FROM forecast_daily_cache WHERE ts_code IN ({self._in_clause(chunk)})"),
                        tuple(chunk),
                    )
        except Exception:  # noqa: BLE001
            pass

    # -- theme registry / lifecycle (READ-ONLY; owned by daily-market-sense) -
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
        """{theme_id: {trade_date, stars, position, crowding, state}} — latest row per theme."""
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
