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


-- -------------------------------------------------------------------------
-- 13. Theme lifecycle (a-stock-daily-market-sense)
-- -------------------------------------------------------------------------
-- Cross-day lifecycle tracking for 上涨主线 identified in daily market-sense
-- reports.  theme_registry holds canonical theme entities; daily ad-hoc theme
-- names from each report's 主线判定 table are accumulated into aliases so the
-- same economic theme maps to one theme_id across days.  theme_daily_state is
-- one row per (trade_date, theme_id) with the model-judged lifecycle state;
-- state transitions are validated by theme_lifecycle.py before insert.
-- evidence quotes the report's 催化逻辑 so every cell is traceable back to
-- the source review.  Markdown reports remain the narrative truth source;
-- these tables are runtime series data only.
CREATE TABLE IF NOT EXISTS theme_registry (
    theme_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    aliases     JSONB NOT NULL DEFAULT '[]',
    overlay     TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS theme_daily_state (
    trade_date      DATE NOT NULL,
    theme_id        TEXT NOT NULL REFERENCES theme_registry(theme_id),
    raw_theme_name  TEXT,
    stars           SMALLINT,
    position        TEXT,
    crowding        TEXT,
    state           TEXT NOT NULL,
    state_prev      TEXT,
    evidence        TEXT,
    members_sample  JSONB,
    source_report   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (trade_date, theme_id)
);

CREATE INDEX IF NOT EXISTS idx_theme_daily_state_theme
    ON theme_daily_state(theme_id, trade_date);

CREATE TABLE IF NOT EXISTS theme_market_day (
    trade_date    DATE PRIMARY KEY,
    market_state  TEXT NOT NULL,
    note          TEXT
);


-- -------------------------------------------------------------------------
-- 14. Strategy pick ledger (a-stock-daily-market-sense, 策略选股实盘台账)
-- -------------------------------------------------------------------------
-- One row per (asof signal day, ts_code): a model-confirmed 策略选股 watch
-- candidate for that day, written by strategy_picks.py record after the model
-- finalises section 6.  Carries a full fingerprint/snapshot (which profile
-- version + which matched conditions + the stock's feature values at T) so the
-- out-of-sample track record stays auditable even after profiles are updated.
-- Per-horizon (T+3 / T+5 / T+10) realized forward returns are backfilled by
-- strategy_picks.py score as data matures; each horizon has its own status so
-- partial maturity / suspension / index gaps never collapse into one flag and
-- re-runs stay idempotent.  Strategy profiles themselves live in
-- references/strategy_profiles/*.json (git, canonical) — NOT in PG.
-- conviction_tier is an enum strong/medium/watch; the report renders 强/中/观察.
-- hk_status ∈ pending/scored/missing_price/expired/error.  Markdown reports stay
-- the narrative truth source; this table is runtime track-record data only.
CREATE TABLE IF NOT EXISTS strategy_pick_ledger (
    asof                    DATE NOT NULL,
    ts_code                 TEXT NOT NULL,
    name                    TEXT,
    board                   TEXT,
    benchmark               TEXT,
    benchmark_wide          TEXT,
    groups_hit              JSONB,
    conviction_tier         TEXT NOT NULL,
    in_main_line            TEXT,
    rationale               TEXT,
    matched_conditions      JSONB,
    feature_snapshot        JSONB,
    profile_fingerprints    JSONB,
    backtest_stats_snapshot JSONB,
    source_evidence         TEXT,
    source_report           TEXT,
    t1_date                 DATE,
    t1_open                 DOUBLE PRECISION,
    t1_close                DOUBLE PRECISION,
    -- horizon T+3
    t3_date     DATE, t3_close DOUBLE PRECISION,
    ro_3 DOUBLE PRECISION, rc_3 DOUBLE PRECISION,
    relo_3 DOUBLE PRECISION, relc_3 DOUBLE PRECISION, relc_3_w DOUBLE PRECISION,
    h3_status   TEXT NOT NULL DEFAULT 'pending', scored_at_3 TIMESTAMPTZ,
    -- horizon T+5
    t5_date     DATE, t5_close DOUBLE PRECISION,
    ro_5 DOUBLE PRECISION, rc_5 DOUBLE PRECISION,
    relo_5 DOUBLE PRECISION, relc_5 DOUBLE PRECISION, relc_5_w DOUBLE PRECISION,
    h5_status   TEXT NOT NULL DEFAULT 'pending', scored_at_5 TIMESTAMPTZ,
    -- horizon T+10
    t10_date    DATE, t10_close DOUBLE PRECISION,
    ro_10 DOUBLE PRECISION, rc_10 DOUBLE PRECISION,
    relo_10 DOUBLE PRECISION, relc_10 DOUBLE PRECISION, relc_10_w DOUBLE PRECISION,
    h10_status  TEXT NOT NULL DEFAULT 'pending', scored_at_10 TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asof, ts_code)
);

CREATE INDEX IF NOT EXISTS idx_strategy_pick_tier
    ON strategy_pick_ledger(conviction_tier);

-- score scans for not-yet-scored horizons; index the earliest-maturing one.
CREATE INDEX IF NOT EXISTS idx_strategy_pick_h3_status
    ON strategy_pick_ledger(h3_status);
