-- =========================================================================
-- Alpha Data — PostgreSQL Schema
-- Compatible with CH Skills (ch-news-reporter + a-stock-daily-market-sense)
-- Run (from repo root): psql -U alpha_user -d alpha_data -f shared/data/init_alpha_data.sql
-- =========================================================================

-- -------------------------------------------------------------------------
-- 1. News items (ch-news-reporter)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    date_key    DATE NOT NULL,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    published_at TIMESTAMPTZ,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title       TEXT NOT NULL,
    content     TEXT,
    url         TEXT,
    author      TEXT,
    tags_json   TEXT,
    metadata_json TEXT,
    raw_json    TEXT,
    search_vector TSVECTOR
);

CREATE INDEX IF NOT EXISTS idx_items_date
    ON items(date_key);

CREATE INDEX IF NOT EXISTS idx_items_source
    ON items(source_type, source_name);

CREATE INDEX IF NOT EXISTS idx_items_search
    ON items USING GIN(search_vector);

-- search_vector is populated by ch-news-reporter/scripts/db_adapter.py with
-- jieba-tokenized text at insert/update time.  Do not use a database trigger
-- here; PostgreSQL simple tokenization breaks Chinese search quality.
DROP TRIGGER IF EXISTS items_search_trigger ON items;
DROP FUNCTION IF EXISTS items_search_update();


-- -------------------------------------------------------------------------
-- 2. Enrichments (ch-news-reporter)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS enrichments (
    id               TEXT PRIMARY KEY,
    item_id          TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    enrichment_type  TEXT NOT NULL,
    source           TEXT,
    model            TEXT,
    prompt_hash      TEXT,
    result_json      TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_enrichments_item
    ON enrichments(item_id);

CREATE INDEX IF NOT EXISTS idx_enrichments_type
    ON enrichments(enrichment_type);


-- -------------------------------------------------------------------------
-- 3. Stock daily (Tushare pro.daily)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_daily (
    ts_code     TEXT NOT NULL,
    trade_date  DATE NOT NULL,
    open        DECIMAL(10,4),
    high        DECIMAL(10,4),
    low         DECIMAL(10,4),
    close       DECIMAL(10,4),
    pre_close   DECIMAL(10,4),
    change      DECIMAL(10,4),
    pct_chg     DECIMAL(10,4),
    vol         BIGINT,
    amount      DECIMAL(18,4),
    PRIMARY KEY (ts_code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_stock_daily_date
    ON stock_daily(trade_date);


-- -------------------------------------------------------------------------
-- 4. Stock daily basic (Tushare pro.daily_basic)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_daily_basic (
    ts_code         TEXT NOT NULL,
    trade_date      DATE NOT NULL,
    turnover_rate   DECIMAL(8,4),
    turnover_rate_f DECIMAL(8,4),
    volume_ratio    DECIMAL(8,4),
    pe              DECIMAL(10,4),
    pb              DECIMAL(10,4),
    total_mv        DECIMAL(18,4),
    circ_mv         DECIMAL(18,4),
    PRIMARY KEY (ts_code, trade_date)
);


-- -------------------------------------------------------------------------
-- 5. Index daily (Tushare pro.index_daily)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_index_daily (
    ts_code     TEXT NOT NULL,
    trade_date  DATE NOT NULL,
    open        DECIMAL(10,4),
    high        DECIMAL(10,4),
    low         DECIMAL(10,4),
    close       DECIMAL(10,4),
    pre_close   DECIMAL(10,4),
    change      DECIMAL(10,4),
    pct_chg     DECIMAL(10,4),
    vol         BIGINT,
    amount      DECIMAL(18,4),
    PRIMARY KEY (ts_code, trade_date)
);


-- -------------------------------------------------------------------------
-- 5b. Adjustment factors (Tushare pro.adj_factor, for qfq price series)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_adj_factor (
    ts_code     TEXT NOT NULL,
    trade_date  DATE NOT NULL,
    adj_factor  DECIMAL(16,6),
    PRIMARY KEY (ts_code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_stock_adj_factor_date
    ON stock_adj_factor(trade_date);


-- -------------------------------------------------------------------------
-- 6. Trade calendar (Tushare pro.trade_cal)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_trade_cal (
    cal_date    DATE PRIMARY KEY,
    is_open     INTEGER NOT NULL
);


-- -------------------------------------------------------------------------
-- 7. Stock basic info (Tushare pro.stock_basic)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_basic (
    ts_code     TEXT PRIMARY KEY,
    name        TEXT,
    market      TEXT,
    list_date   DATE
);


-- -------------------------------------------------------------------------
-- 8. Margin (Tushare pro.margin / margin_detail)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_margin (
    trade_date              DATE NOT NULL,
    exchange_id             TEXT,
    rzye                    DECIMAL(18,4),
    rzmre                   DECIMAL(18,4),
    rzche                   DECIMAL(18,4),
    rqye                    DECIMAL(18,4),
    rqmcl                   DECIMAL(18,4),
    rzrqye                  DECIMAL(18,4),
    PRIMARY KEY (trade_date, exchange_id)
);


-- -------------------------------------------------------------------------
-- 9. Market history (reference/market_data.csv)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_history (
    date            DATE PRIMARY KEY,
    rise            INTEGER,
    limit_up        INTEGER,
    fall            INTEGER,
    limit_down      INTEGER,
    flat            INTEGER,
    activity        DECIMAL(10,4),
    sentiment       DECIMAL(10,4),
    amount          DECIMAL(18,4),
    margin_net_buy  DECIMAL(18,4),
    turnover_rate   DECIMAL(8,4)
);


-- -------------------------------------------------------------------------
-- 10. Reports metadata (optional, for tracking generated reports)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reports (
    id              SERIAL PRIMARY KEY,
    report_type     TEXT NOT NULL,
    date_key        DATE NOT NULL,
    title           TEXT,
    output_path     TEXT,
    tags            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reports_type_date
    ON reports(report_type, date_key);


-- -------------------------------------------------------------------------
-- 11. Report state / watchboard (ch-news-reporter)
-- -------------------------------------------------------------------------
-- One rolling analysis state per (profile, date_key).  Holds the living
-- "watchboard": current regime, tracking-item ledger, actor weights, signal
-- watchlist, probabilities and next nodes.  Written by scripts/save_report_state.py,
-- read back by scripts/prepare_report_data.py to carry state across days.
-- payload is stored as TEXT (JSON string) in both PostgreSQL and SQLite so the
-- read/write path is identical on both backends.  date_key/created_at/updated_at
-- keep native PG types here (DATE / TIMESTAMPTZ) while SQLite stores them as
-- TEXT; db_adapter._normalize_report_state_row coerces them back to str on read
-- so callers see identical Python types regardless of backend.
CREATE TABLE IF NOT EXISTS report_state (
    profile     TEXT NOT NULL,
    date_key    DATE NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (profile, date_key)
);

CREATE INDEX IF NOT EXISTS idx_report_state_profile_date
    ON report_state(profile, date_key);


-- -------------------------------------------------------------------------
-- 12. Framework state / governance (ch-news-reporter, slow-thinking layer)
-- -------------------------------------------------------------------------
-- One framework-governance review per (profile, review_date_key).  Sparse —
-- written only when the slow-thinking layer runs a review, not daily.  Holds
-- the regime verdict, zero-based divergence, framework-change proposal and the
-- framework-challenge ledger.  Written by scripts/save_framework_state.py.  The
-- per-profile framework.md stays the single source of truth for the *current*
-- framework definition; this table is the governance audit log.  payload is
-- TEXT (JSON string) on both backends, mirroring report_state.
CREATE TABLE IF NOT EXISTS framework_state (
    profile         TEXT NOT NULL,
    review_date_key DATE NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (profile, review_date_key)
);

CREATE INDEX IF NOT EXISTS idx_framework_state_profile_date
    ON framework_state(profile, review_date_key);
