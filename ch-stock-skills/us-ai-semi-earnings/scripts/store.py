#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistence and incremental cache for the US AI/semi earnings skill.

Connects through the shared layer `shared/data/db_core.py` (dev) or the synced
`scripts/_shared/db_core.py` (installed) — per the house rule, a skill does not
roll its own connection. Tables are namespaced `usearn_*` inside the shared
`alpha_data` database.

What is cached and why each thing is cached the way it is:

- `usearn_company`      the resolved universe: ticker → CIK, bucket, chain role.
                        Saves re-resolving 130 tickers against SEC's map daily.
- `usearn_filing`       every 8-K/10-Q/10-K seen, with the calendar frame it was
                        assigned. This is the **discovery water-mark**: a scan
                        only has to look at filings newer than the newest row.
- `usearn_xbrl_fact`    one row per (ticker, frame, concept). Deliberately not a
                        JSON blob like the A-share sibling: cross-company work
                        here asks "every semicap name's gross margin this frame",
                        which is one indexed query over rows and a full scan plus
                        client-side parsing over blobs.
- `usearn_press_release` 8-K EX-99.1 text. Immutable once filed, so fetched once.
- `usearn_transcript`    one row per (ticker, frame) call: source, status, stats.
- `usearn_transcript_segment`
                        one row per speaker turn. Segment-level rows are what
                        make "who else mentioned CoWoS this quarter" a SQL query
                        instead of thirty transcripts loaded into context — the
                        cross-company read is the point of this skill.
- `usearn_surprise`      EPS actual vs estimate, whichever vendor answered.
- `usearn_price_cache`   daily bars for the gap/volume reaction engine.
- `usearn_verdict`       the model's per-company call. Never written by a script.
- `usearn_chain_verdict` the model's per-transmission-chain call.

Transcripts are the reason the cache is not merely an optimisation: Alpha
Vantage's free tier allows 25 requests a day, and a published transcript never
changes. Buying each (ticker, frame) exactly once and serving it from here
forever is what makes a 130-name universe fit inside that quota at all.

If the database cannot be opened, `available` goes False and every method
becomes a no-op; callers then run in full-fetch mode. That is a degradation
path, not the normal one — without it the Alpha Vantage budget is re-spent
every run.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUNDLED_SHARED = _SCRIPT_DIR / "_shared"
_DEV_SHARED = _SCRIPT_DIR.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if (_BUNDLED_SHARED / "db_core.py").exists() else _DEV_SHARED))

try:
    from db_core import BACKEND, Backend, adapt_sql, get_connection  # type: ignore
    _DB_IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # noqa: BLE001 - degrade if the shared layer is missing
    BACKEND = Backend = adapt_sql = get_connection = None  # type: ignore
    _DB_IMPORT_ERROR = str(exc)


_SCHEMA: Dict[str, str] = {
    "usearn_company": """
        CREATE TABLE IF NOT EXISTS usearn_company (
            ticker TEXT PRIMARY KEY,
            cik TEXT,
            name TEXT,
            bucket TEXT,
            chain_role TEXT,
            fiscal_year_end_month INTEGER,
            statement_source TEXT,
            updated_at TEXT
        )""",
    "usearn_filing": """
        CREATE TABLE IF NOT EXISTS usearn_filing (
            ticker TEXT NOT NULL,
            accession TEXT NOT NULL,
            form TEXT,
            items TEXT,
            filing_date TEXT,
            report_date TEXT,
            accepted_at TEXT,
            frame TEXT,
            kind TEXT,
            dir_url TEXT,
            primary_doc TEXT,
            fetched_at TEXT,
            PRIMARY KEY (ticker, accession)
        )""",
    "usearn_xbrl_fact": """
        CREATE TABLE IF NOT EXISTS usearn_xbrl_fact (
            ticker TEXT NOT NULL,
            frame TEXT NOT NULL,
            concept TEXT NOT NULL,
            val DOUBLE PRECISION,
            unit TEXT,
            tag TEXT,
            span_start TEXT,
            span_end TEXT,
            form TEXT,
            filed TEXT,
            derived TEXT,
            fetched_at TEXT,
            PRIMARY KEY (ticker, frame, concept)
        )""",
    "usearn_xbrl_fetch_log": """
        CREATE TABLE IF NOT EXISTS usearn_xbrl_fetch_log (
            ticker TEXT PRIMARY KEY,
            latest_filed TEXT,
            frames_covered INTEGER,
            missing_concepts TEXT,
            note TEXT,
            fetched_at TEXT
        )""",
    "usearn_press_release": """
        CREATE TABLE IF NOT EXISTS usearn_press_release (
            ticker TEXT NOT NULL,
            accession TEXT NOT NULL,
            frame TEXT,
            filing_date TEXT,
            exhibit_type TEXT,
            url TEXT,
            text TEXT,
            char_count INTEGER,
            fetched_at TEXT,
            PRIMARY KEY (ticker, accession)
        )""",
    "usearn_transcript": """
        CREATE TABLE IF NOT EXISTS usearn_transcript (
            ticker TEXT NOT NULL,
            frame TEXT NOT NULL,
            status TEXT,
            source TEXT,
            url TEXT,
            call_date TEXT,
            published_date TEXT,
            matched_by TEXT,
            segment_count INTEGER,
            char_count INTEGER,
            prepared_segments INTEGER,
            qa_segments INTEGER,
            has_sentiment INTEGER,
            participants TEXT,
            publisher_notes TEXT,
            attempts TEXT,
            fetched_at TEXT,
            PRIMARY KEY (ticker, frame)
        )""",
    "usearn_transcript_segment": """
        CREATE TABLE IF NOT EXISTS usearn_transcript_segment (
            ticker TEXT NOT NULL,
            frame TEXT NOT NULL,
            idx INTEGER NOT NULL,
            speaker TEXT,
            title TEXT,
            section TEXT,
            content TEXT,
            sentiment DOUBLE PRECISION,
            PRIMARY KEY (ticker, frame, idx)
        )""",
    "usearn_surprise": """
        CREATE TABLE IF NOT EXISTS usearn_surprise (
            ticker TEXT NOT NULL,
            frame TEXT NOT NULL,
            fiscal_date_ending TEXT,
            reported_date TEXT,
            eps_reported DOUBLE PRECISION,
            eps_estimated DOUBLE PRECISION,
            surprise DOUBLE PRECISION,
            surprise_pct DOUBLE PRECISION,
            source TEXT,
            fetched_at TEXT,
            PRIMARY KEY (ticker, frame)
        )""",
    "usearn_calendar": """
        CREATE TABLE IF NOT EXISTS usearn_calendar (
            ticker TEXT NOT NULL,
            report_date TEXT NOT NULL,
            fiscal_date_ending TEXT,
            estimate DOUBLE PRECISION,
            time_of_day TEXT,
            source TEXT,
            fetched_at TEXT,
            PRIMARY KEY (ticker, report_date)
        )""",
    "usearn_price_cache": """
        CREATE TABLE IF NOT EXISTS usearn_price_cache (
            ticker TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open DOUBLE PRECISION,
            high DOUBLE PRECISION,
            low DOUBLE PRECISION,
            close DOUBLE PRECISION,
            volume DOUBLE PRECISION,
            PRIMARY KEY (ticker, trade_date)
        )""",
    "usearn_verdict": """
        CREATE TABLE IF NOT EXISTS usearn_verdict (
            frame TEXT NOT NULL,
            ticker TEXT NOT NULL,
            tier TEXT,
            quality_call TEXT,
            guidance_call TEXT,
            transcript_read INTEGER,
            theme TEXT,
            headline TEXT,
            reasons TEXT,
            watch_items TEXT,
            evidence_digest TEXT,
            decided_at TEXT,
            PRIMARY KEY (frame, ticker)
        )""",
    "usearn_chain_verdict": """
        CREATE TABLE IF NOT EXISTS usearn_chain_verdict (
            frame TEXT NOT NULL,
            chain TEXT NOT NULL,
            state TEXT,
            confirmed_by TEXT,
            contradicted_by TEXT,
            note TEXT,
            decided_at TEXT,
            PRIMARY KEY (frame, chain)
        )""",
}

_INDEXES: Sequence[str] = (
    "CREATE INDEX IF NOT EXISTS idx_usearn_fact_frame ON usearn_xbrl_fact (frame, concept)",
    "CREATE INDEX IF NOT EXISTS idx_usearn_filing_date ON usearn_filing (filing_date)",
    "CREATE INDEX IF NOT EXISTS idx_usearn_seg_frame ON usearn_transcript_segment (frame)",
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _chunks(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _jd(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _jl(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


class Store:
    """Batched upserts and IN-list reads over the `usearn_*` tables."""

    def __init__(self, enabled: bool = True):
        self.available = False
        self.error: Optional[str] = _DB_IMPORT_ERROR
        if not enabled or get_connection is None:
            if not enabled:
                self.error = "cache disabled by caller"
            return
        try:
            # db_core hands out pooled connections through a context manager and
            # reclaims them on exit, so a connection is borrowed per operation
            # rather than held for the life of the Store.
            with get_connection() as conn:
                cur = conn.cursor()
                for ddl in _SCHEMA.values():
                    cur.execute(self._sql(ddl))
                for idx in _INDEXES:
                    cur.execute(self._sql(idx))
            self.available = True
            self.error = None
        except Exception as exc:  # noqa: BLE001 - degrade to no-cache
            self.error = f"db connect/init failed: {str(exc)[:160]}"

    # -- plumbing ---------------------------------------------------------

    @staticmethod
    def _sql(sql: str) -> str:
        return adapt_sql(sql) if adapt_sql else sql

    @staticmethod
    def _dedupe(keys: Sequence[str], columns: Sequence[str],
                rows: Sequence[Sequence[Any]]) -> List[Sequence[Any]]:
        """Keep the last row per conflict key.

        PostgreSQL refuses an `INSERT ... ON CONFLICT` that touches the same key
        twice inside one statement, and a batch assembled from several fetches
        routinely does.
        """
        idx = [columns.index(k) for k in keys]
        seen: Dict[Tuple[Any, ...], int] = {}
        for i, row in enumerate(rows):
            seen[tuple(row[j] for j in idx)] = i
        return [rows[i] for i in sorted(seen.values())]

    def _upsert(self, table: str, columns: Sequence[str], keys: Sequence[str],
                rows: Sequence[Sequence[Any]]) -> int:
        """Batched INSERT ... ON CONFLICT DO UPDATE, portable across backends."""
        if not self.available or not rows:
            return 0
        rows = self._dedupe(keys, columns, rows)
        cols = ", ".join(columns)
        placeholders = "(" + ", ".join(["%s"] * len(columns)) + ")"
        updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in columns if c not in keys)
        conflict = ", ".join(keys)
        tail = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
        written = 0
        try:
            with get_connection() as conn:
                cur = conn.cursor()
                for chunk in _chunks(rows, 400):
                    values = ", ".join([placeholders] * len(chunk))
                    sql = (f"INSERT INTO {table} ({cols}) VALUES {values} "
                           f"ON CONFLICT ({conflict}) {tail}")
                    cur.execute(self._sql(sql), [v for row in chunk for v in row])
                    written += len(chunk)
        except Exception as exc:  # noqa: BLE001
            self.error = f"{table} upsert failed: {str(exc)[:160]}"
            return 0
        return written

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        if not self.available:
            return []
        try:
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(self._sql(sql), list(params))
                return [tuple(r) for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            self.error = f"query failed: {str(exc)[:160]}"
            return []

    # -- universe ---------------------------------------------------------

    def save_companies(self, rows: Sequence[Dict[str, Any]]) -> int:
        cols = ("ticker", "cik", "name", "bucket", "chain_role",
                "fiscal_year_end_month", "statement_source", "updated_at")
        payload = [(r["ticker"], r.get("cik"), r.get("name"), r.get("bucket"),
                    r.get("chain_role"), r.get("fiscal_year_end_month"),
                    r.get("statement_source"), _now()) for r in rows]
        return self._upsert("usearn_company", cols, ("ticker",), payload)

    def load_companies(self) -> Dict[str, Dict[str, Any]]:
        rows = self._query(
            "SELECT ticker, cik, name, bucket, chain_role, fiscal_year_end_month, "
            "statement_source FROM usearn_company")
        return {r[0]: {"ticker": r[0], "cik": r[1], "name": r[2], "bucket": r[3],
                       "chain_role": r[4], "fiscal_year_end_month": r[5],
                       "statement_source": r[6]} for r in rows}

    # -- filings ----------------------------------------------------------

    def save_filings(self, ticker: str, filings: Sequence[Dict[str, Any]]) -> int:
        cols = ("ticker", "accession", "form", "items", "filing_date", "report_date",
                "accepted_at", "frame", "kind", "dir_url", "primary_doc", "fetched_at")
        payload = [(ticker, f["accession"], f.get("form"), f.get("items"),
                    f.get("filing_date"), f.get("report_date"), f.get("accepted_at"),
                    f.get("frame"), f.get("kind"), f.get("dir_url"),
                    f.get("primary_doc"), _now()) for f in filings]
        return self._upsert("usearn_filing", cols, ("ticker", "accession"), payload)

    def latest_filing_date(self, ticker: str) -> Optional[str]:
        rows = self._query(
            "SELECT MAX(filing_date) FROM usearn_filing WHERE ticker = %s", (ticker,))
        return rows[0][0] if rows and rows[0][0] else None

    def load_filings(self, ticker: str, *, kind: Optional[str] = None,
                     frame: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT accession, form, items, filing_date, report_date, accepted_at, "
               "frame, kind, dir_url, primary_doc FROM usearn_filing WHERE ticker = %s")
        params: List[Any] = [ticker]
        if kind:
            sql += " AND kind = %s"
            params.append(kind)
        if frame:
            sql += " AND frame = %s"
            params.append(frame)
        sql += " ORDER BY filing_date DESC"
        keys = ("accession", "form", "items", "filing_date", "report_date",
                "accepted_at", "frame", "kind", "dir_url", "primary_doc")
        return [dict(zip(keys, r)) for r in self._query(sql, params)]

    # -- XBRL facts -------------------------------------------------------

    def save_facts(self, ticker: str, fact_table: Dict[str, Any],
                   *, keep_frames: Optional[Sequence[str]] = None) -> int:
        """Flatten a `build_fact_table` result into rows.

        `keep_frames` bounds the write to the frames a scan actually needs
        (current, prior quarter, prior year); a full history rewrite of 130
        companies every run is pure churn.
        """
        rows: List[Sequence[Any]] = []
        wanted = set(keep_frames) if keep_frames else None
        now = _now()
        for concept, series in (fact_table.get("series") or {}).items():
            for frame, row in (series.get("quarters") or {}).items():
                if wanted and frame not in wanted:
                    continue
                val = row.get("val")
                try:
                    val = float(val) if val is not None else None
                except (TypeError, ValueError):
                    val = None
                rows.append((ticker, frame, concept, val, row.get("unit"),
                             row.get("tag"), row.get("start"), row.get("end"),
                             row.get("form"), row.get("filed"), row.get("derived"), now))
        # A requested frame may legitimately contain no configured fact (new IPO,
        # sparse filer, or foreign issuer). Persist a sentinel so a later scan can
        # distinguish "fetched and empty" from "never fetched"; otherwise an empty
        # historical frame causes companyfacts to be downloaded on every run.
        for frame in wanted or ():
            rows.append((ticker, frame, "__frame_fetched__", None, None, None,
                         None, None, None, None, "cache_marker", now))
        cols = ("ticker", "frame", "concept", "val", "unit", "tag", "span_start",
                "span_end", "form", "filed", "derived", "fetched_at")
        written = self._upsert("usearn_xbrl_fact", cols, ("ticker", "frame", "concept"), rows)

        log_cols = ("ticker", "latest_filed", "frames_covered", "missing_concepts",
                    "note", "fetched_at")
        latest = max((r[9] or "" for r in rows), default="") or None
        self._upsert("usearn_xbrl_fetch_log", log_cols, ("ticker",), [(
            ticker, latest, len(fact_table.get("frames_covered") or []),
            _jd(fact_table.get("missing_concepts")), None, now)])
        return written

    def load_facts(self, tickers: Sequence[str], frames: Sequence[str]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """`{(ticker, frame): {concept: {...}}}` for a batch of names."""
        if not self.available or not tickers or not frames:
            return {}
        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for chunk in _chunks(list(tickers), 200):
            ph_t = ", ".join(["%s"] * len(chunk))
            ph_f = ", ".join(["%s"] * len(frames))
            rows = self._query(
                f"SELECT ticker, frame, concept, val, unit, tag, span_start, span_end, "
                f"form, filed, derived FROM usearn_xbrl_fact "
                f"WHERE ticker IN ({ph_t}) AND frame IN ({ph_f})",
                list(chunk) + list(frames))
            for r in rows:
                out.setdefault((r[0], r[1]), {})[r[2]] = {
                    "val": r[3], "unit": r[4], "tag": r[5], "start": r[6],
                    "end": r[7], "form": r[8], "filed": r[9], "derived": r[10]}
        return out

    def xbrl_fetch_log(self, tickers: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        if not self.available or not tickers:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for chunk in _chunks(list(tickers), 300):
            ph = ", ".join(["%s"] * len(chunk))
            for r in self._query(
                f"SELECT ticker, latest_filed, frames_covered, missing_concepts, fetched_at "
                f"FROM usearn_xbrl_fetch_log WHERE ticker IN ({ph})", list(chunk)):
                out[r[0]] = {"latest_filed": r[1], "frames_covered": r[2],
                             "missing_concepts": _jl(r[3]) or [], "fetched_at": r[4]}
        return out

    # -- press releases ---------------------------------------------------

    def save_press_release(self, ticker: str, accession: str, *, frame: Optional[str],
                           filing_date: Optional[str], exhibit_type: Optional[str],
                           url: Optional[str], text: str) -> int:
        cols = ("ticker", "accession", "frame", "filing_date", "exhibit_type",
                "url", "text", "char_count", "fetched_at")
        return self._upsert("usearn_press_release", cols, ("ticker", "accession"), [(
            ticker, accession, frame, filing_date, exhibit_type, url, text,
            len(text or ""), _now())])

    def load_press_release(self, ticker: str, accession: str) -> Optional[Dict[str, Any]]:
        rows = self._query(
            "SELECT frame, filing_date, exhibit_type, url, text, char_count "
            "FROM usearn_press_release WHERE ticker = %s AND accession = %s",
            (ticker, accession))
        if not rows:
            return None
        keys = ("frame", "filing_date", "exhibit_type", "url", "text", "char_count")
        return dict(zip(keys, rows[0]))

    def press_release_frames(self, tickers: Sequence[str]) -> Dict[str, List[str]]:
        if not self.available or not tickers:
            return {}
        out: Dict[str, List[str]] = {}
        for chunk in _chunks(list(tickers), 300):
            ph = ", ".join(["%s"] * len(chunk))
            for r in self._query(
                f"SELECT ticker, accession FROM usearn_press_release WHERE ticker IN ({ph})",
                list(chunk)):
                out.setdefault(r[0], []).append(r[1])
        return out

    # -- transcripts ------------------------------------------------------

    def save_transcript(self, ticker: str, frame: str, payload: Dict[str, Any]) -> int:
        stats = payload.get("stats") or {}
        cols = ("ticker", "frame", "status", "source", "url", "call_date",
                "published_date", "matched_by", "segment_count", "char_count",
                "prepared_segments", "qa_segments", "has_sentiment", "participants",
                "publisher_notes", "attempts", "fetched_at")
        self._upsert("usearn_transcript", cols, ("ticker", "frame"), [(
            ticker, frame, payload.get("status"), payload.get("source"),
            payload.get("url"), payload.get("call_date_hint"),
            payload.get("published_date"), payload.get("matched_by"),
            stats.get("segment_count"), stats.get("char_count"),
            stats.get("prepared_segments"), stats.get("qa_segments"),
            1 if stats.get("has_sentiment") else 0,
            _jd(payload.get("participants")), _jd(payload.get("publisher_notes")),
            _jd(payload.get("attempts")), _now())])

        segments = payload.get("segments") or []
        if not segments:
            return 0
        seg_cols = ("ticker", "frame", "idx", "speaker", "title", "section",
                    "content", "sentiment")
        seg_rows = [(ticker, frame, s.get("idx", i), s.get("speaker"), s.get("title"),
                     s.get("section"), s.get("content"), s.get("sentiment"))
                    for i, s in enumerate(segments)]
        return self._upsert("usearn_transcript_segment", seg_cols,
                            ("ticker", "frame", "idx"), seg_rows)

    def transcript_status(self, frames: Sequence[str],
                          tickers: Optional[Sequence[str]] = None) -> Dict[Tuple[str, str], Dict[str, Any]]:
        if not self.available or not frames:
            return {}
        sql = ("SELECT ticker, frame, status, source, url, published_date, segment_count, "
               "char_count, prepared_segments, qa_segments, has_sentiment, fetched_at "
               "FROM usearn_transcript WHERE frame IN (" + ", ".join(["%s"] * len(frames)) + ")")
        params: List[Any] = list(frames)
        if tickers:
            sql += " AND ticker IN (" + ", ".join(["%s"] * len(tickers)) + ")"
            params += list(tickers)
        keys = ("status", "source", "url", "published_date", "segment_count", "char_count",
                "prepared_segments", "qa_segments", "has_sentiment", "fetched_at")
        return {(r[0], r[1]): dict(zip(keys, r[2:])) for r in self._query(sql, params)}

    def load_transcript(self, ticker: str, frame: str) -> Optional[Dict[str, Any]]:
        head = self._query(
            "SELECT status, source, url, call_date, published_date, matched_by, "
            "participants, publisher_notes, attempts FROM usearn_transcript "
            "WHERE ticker = %s AND frame = %s", (ticker, frame))
        if not head:
            return None
        keys = ("status", "source", "url", "call_date", "published_date", "matched_by")
        payload = dict(zip(keys, head[0][:6]))
        payload["participants"] = _jl(head[0][6]) or []
        payload["publisher_notes"] = _jl(head[0][7]) or {}
        payload["attempts"] = _jl(head[0][8]) or []
        payload["segments"] = [
            {"idx": r[0], "speaker": r[1], "title": r[2], "section": r[3],
             "content": r[4], "sentiment": r[5]}
            for r in self._query(
                "SELECT idx, speaker, title, section, content, sentiment "
                "FROM usearn_transcript_segment WHERE ticker = %s AND frame = %s "
                "ORDER BY idx", (ticker, frame))]
        return payload

    def search_segments(self, term: str, *, frames: Optional[Sequence[str]] = None,
                        tickers: Optional[Sequence[str]] = None,
                        section: Optional[str] = None,
                        limit: int = 60) -> List[Dict[str, Any]]:
        """Case-insensitive substring search across every cached call.

        This is the cross-company primitive: one query answers "which names
        talked about HBM supply this quarter, and in prepared remarks or only
        under analyst pressure" without pulling whole transcripts into context.
        """
        sql = ("SELECT ticker, frame, idx, speaker, title, section, content, sentiment "
               "FROM usearn_transcript_segment WHERE LOWER(content) LIKE %s")
        params: List[Any] = [f"%{term.lower()}%"]
        if frames:
            sql += " AND frame IN (" + ", ".join(["%s"] * len(frames)) + ")"
            params += list(frames)
        if tickers:
            sql += " AND ticker IN (" + ", ".join(["%s"] * len(tickers)) + ")"
            params += list(tickers)
        if section:
            sql += " AND section = %s"
            params.append(section)
        sql += " ORDER BY ticker, idx LIMIT %s"
        params.append(int(limit))
        keys = ("ticker", "frame", "idx", "speaker", "title", "section", "content", "sentiment")
        return [dict(zip(keys, r)) for r in self._query(sql, params)]

    # -- surprises, calendar, prices --------------------------------------

    def save_surprises(self, rows: Sequence[Dict[str, Any]]) -> int:
        cols = ("ticker", "frame", "fiscal_date_ending", "reported_date", "eps_reported",
                "eps_estimated", "surprise", "surprise_pct", "source", "fetched_at")
        payload = [(r["ticker"], r["frame"], r.get("fiscal_date_ending"),
                    r.get("reported_date"), r.get("eps_reported"), r.get("eps_estimated"),
                    r.get("surprise"), r.get("surprise_pct"), r.get("source"), _now())
                   for r in rows]
        return self._upsert("usearn_surprise", cols, ("ticker", "frame"), payload)

    def load_surprises(self, frames: Sequence[str]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """Cached surprises, with the near-zero-consensus flag recomputed.

        The flag is derived rather than stored so the threshold stays in one
        place (`earnings_scan.fetch_surprise`) instead of being frozen into rows
        written by an older run.
        """
        if not self.available or not frames:
            return {}
        ph = ", ".join(["%s"] * len(frames))
        keys = ("fiscal_date_ending", "reported_date", "eps_reported", "eps_estimated",
                "surprise", "surprise_pct", "source")
        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for r in self._query(
            f"SELECT ticker, frame, fiscal_date_ending, reported_date, eps_reported, "
            f"eps_estimated, surprise, surprise_pct, source FROM usearn_surprise "
            f"WHERE frame IN ({ph})", list(frames)):
            rec = dict(zip(keys, r[2:]))
            est = rec.get("eps_estimated")
            unstable = est is not None and abs(est) < 0.25
            rec["surprise_pct_unstable"] = unstable or None
            rec["surprise_note"] = ("consensus EPS is near zero — quote the cents, "
                                    "not the percentage") if unstable else None
            out[(r[0], r[1])] = rec
        return out

    def save_calendar(self, rows: Sequence[Dict[str, Any]]) -> int:
        cols = ("ticker", "report_date", "fiscal_date_ending", "estimate",
                "time_of_day", "source", "fetched_at")
        payload = [(r["ticker"], r["report_date"], r.get("fiscal_date_ending"),
                    r.get("estimate"), r.get("time_of_day"), r.get("source"), _now())
                   for r in rows]
        return self._upsert("usearn_calendar", cols, ("ticker", "report_date"), payload)

    def load_calendar(self, tickers: Sequence[str], *, since: str,
                      until: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.available or not tickers:
            return []
        out: List[Dict[str, Any]] = []
        keys = ("ticker", "report_date", "fiscal_date_ending", "estimate", "time_of_day")
        for chunk in _chunks(list(tickers), 200):
            ph = ", ".join(["%s"] * len(chunk))
            sql = (f"SELECT ticker, report_date, fiscal_date_ending, estimate, time_of_day "
                   f"FROM usearn_calendar WHERE ticker IN ({ph}) AND report_date >= %s")
            params = list(chunk) + [since]
            if until:
                sql += " AND report_date <= %s"
                params.append(until)
            out += [dict(zip(keys, r)) for r in self._query(sql + " ORDER BY report_date", params)]
        return out

    def save_bars(self, ticker: str, bars: Sequence[Dict[str, Any]]) -> int:
        cols = ("ticker", "trade_date", "open", "high", "low", "close", "volume")
        payload = [(ticker, b["date"], b.get("open"), b.get("high"), b.get("low"),
                    b.get("close"), b.get("volume")) for b in bars if b.get("date")]
        return self._upsert("usearn_price_cache", cols, ("ticker", "trade_date"), payload)

    def load_bars(self, ticker: str, *, since: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT trade_date, open, high, low, close, volume FROM usearn_price_cache "
               "WHERE ticker = %s")
        params: List[Any] = [ticker]
        if since:
            sql += " AND trade_date >= %s"
            params.append(since)
        keys = ("date", "open", "high", "low", "close", "volume")
        return [dict(zip(keys, r)) for r in self._query(sql + " ORDER BY trade_date", params)]

    def latest_bar_date(self, ticker: str) -> Optional[str]:
        rows = self._query("SELECT MAX(trade_date) FROM usearn_price_cache WHERE ticker = %s",
                           (ticker,))
        return rows[0][0] if rows and rows[0][0] else None

    # -- model ledger -----------------------------------------------------

    def save_verdicts(self, frame: str, records: Sequence[Dict[str, Any]]) -> int:
        cols = ("frame", "ticker", "tier", "quality_call", "guidance_call",
                "transcript_read", "theme", "headline", "reasons", "watch_items",
                "evidence_digest", "decided_at")
        payload = [(frame, r["ticker"], r.get("tier"), r.get("quality_call"),
                    r.get("guidance_call"), 1 if r.get("transcript_read") else 0,
                    r.get("theme"), r.get("headline"), _jd(r.get("reasons")),
                    _jd(r.get("watch_items")), _jd(r.get("evidence_digest")), _now())
                   for r in records]
        return self._upsert("usearn_verdict", cols, ("frame", "ticker"), payload)

    def load_verdicts(self, frame: str) -> Dict[str, Dict[str, Any]]:
        keys = ("tier", "quality_call", "guidance_call", "transcript_read", "theme",
                "headline", "reasons", "watch_items", "evidence_digest", "decided_at")
        out: Dict[str, Dict[str, Any]] = {}
        for r in self._query(
            "SELECT ticker, tier, quality_call, guidance_call, transcript_read, theme, "
            "headline, reasons, watch_items, evidence_digest, decided_at "
            "FROM usearn_verdict WHERE frame = %s", (frame,)):
            rec = dict(zip(keys, r[1:]))
            rec["transcript_read"] = bool(rec.get("transcript_read"))
            for k in ("reasons", "watch_items", "evidence_digest"):
                rec[k] = _jl(rec.get(k))
            out[r[0]] = rec
        return out

    def save_chain_verdicts(self, frame: str, records: Sequence[Dict[str, Any]]) -> int:
        cols = ("frame", "chain", "state", "confirmed_by", "contradicted_by",
                "note", "decided_at")
        payload = [(frame, r["chain"], r.get("state"), _jd(r.get("confirmed_by")),
                    _jd(r.get("contradicted_by")), r.get("note"), _now())
                   for r in records]
        return self._upsert("usearn_chain_verdict", cols, ("frame", "chain"), payload)

    def load_chain_verdicts(self, frame: str) -> Dict[str, Dict[str, Any]]:
        keys = ("state", "confirmed_by", "contradicted_by", "note", "decided_at")
        out: Dict[str, Dict[str, Any]] = {}
        for r in self._query(
            "SELECT chain, state, confirmed_by, contradicted_by, note, decided_at "
            "FROM usearn_chain_verdict WHERE frame = %s", (frame,)):
            rec = dict(zip(keys, r[1:]))
            rec["confirmed_by"] = _jl(rec.get("confirmed_by")) or []
            rec["contradicted_by"] = _jl(rec.get("contradicted_by")) or []
            out[r[0]] = rec
        return out

    # -- misc -------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        if not self.available:
            return {"cache": "off", "error": self.error}
        out: Dict[str, Any] = {"cache": "on", "backend": str(BACKEND)}
        for table in _SCHEMA:
            rows = self._query(f"SELECT COUNT(*) FROM {table}")
            out[table] = rows[0][0] if rows else None
        return out

    def close(self) -> None:
        """No-op: connections are borrowed per operation and returned by db_core."""
        self.available = False


if __name__ == "__main__":
    store = Store()
    print(json.dumps(store.stats(), indent=2, default=str))
    store.close()
