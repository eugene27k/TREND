-- TREND-specific tables (PRD Section 9). Tables marked (A) in the PRD are
-- additions that only TREND writes; they still carry `strategy` for symmetry
-- with the shared layer and so that a combined dashboard query is uniform.

CREATE TABLE IF NOT EXISTS universe_history (
    strategy                 TEXT    NOT NULL,
    month                    TEXT    NOT NULL,   -- YYYY-MM
    symbol                   TEXT    NOT NULL,
    rank                     INTEGER NOT NULL,
    median_quote_volume_30d  REAL    NOT NULL,
    history_days             INTEGER NOT NULL,
    included                 INTEGER NOT NULL,
    reason                   TEXT    NOT NULL DEFAULT '',
    created_ts               INTEGER NOT NULL,
    PRIMARY KEY (strategy, month, symbol)
);
CREATE INDEX IF NOT EXISTS ix_universe_month ON universe_history (strategy, month, included);

CREATE TABLE IF NOT EXISTS daily_bars (
    strategy      TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    day           TEXT    NOT NULL,   -- YYYY-MM-DD (UTC)
    open          REAL    NOT NULL,
    high          REAL    NOT NULL,
    low           REAL    NOT NULL,
    close         REAL    NOT NULL,
    volume        REAL    NOT NULL,
    quote_volume  REAL    NOT NULL,
    open_time     INTEGER NOT NULL,
    close_time    INTEGER NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'rest',
    filled        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy, symbol, day)
);
CREATE INDEX IF NOT EXISTS ix_bars_day ON daily_bars (strategy, day);

CREATE TABLE IF NOT EXISTS signal_snapshots (
    strategy  TEXT NOT NULL,
    day       TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    x1 REAL, x2 REAL, x3 REAL,
    y1 REAL, y2 REAL, y3 REAL,
    z1 REAL, z2 REAL, z3 REAL,
    u1 REAL, u2 REAL, u3 REAL,
    signal    REAL    NOT NULL,
    warm      INTEGER NOT NULL DEFAULT 1,
    bar_ts    INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    PRIMARY KEY (strategy, day, symbol)
);

CREATE TABLE IF NOT EXISTS risk_model_snapshots (
    strategy   TEXT NOT NULL,
    day        TEXT NOT NULL,
    vols_json  TEXT NOT NULL,
    corr_json  TEXT NOT NULL,
    symbols_json TEXT NOT NULL DEFAULT '[]',
    avg_corr   REAL NOT NULL,
    created_ts INTEGER NOT NULL,
    PRIMARY KEY (strategy, day)
);

CREATE TABLE IF NOT EXISTS rebalances (
    strategy         TEXT    NOT NULL,
    rebalance_id     TEXT    NOT NULL,
    day              TEXT    NOT NULL,
    started_ts       INTEGER NOT NULL,
    ended_ts         INTEGER,
    status           TEXT    NOT NULL,
    kind             TEXT    NOT NULL DEFAULT 'scheduled',  -- scheduled | risk_cut | delisting | flatten
    order_plan_json  TEXT    NOT NULL DEFAULT '[]',
    completion_pct   REAL    NOT NULL DEFAULT 0,
    traded_notional  REAL    NOT NULL DEFAULT 0,
    planned_notional REAL    NOT NULL DEFAULT 0,
    fees             REAL    NOT NULL DEFAULT 0,
    avg_slippage_bps REAL    NOT NULL DEFAULT 0,
    maker_ratio      REAL    NOT NULL DEFAULT 0,
    residuals_json   TEXT    NOT NULL DEFAULT '[]',
    equity           REAL    NOT NULL DEFAULT 0,
    governor_g       REAL    NOT NULL DEFAULT 1,
    decision_mids_json TEXT  NOT NULL DEFAULT '{}',
    cursor           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy, rebalance_id)
);
CREATE INDEX IF NOT EXISTS ix_rebalances_day ON rebalances (strategy, day);

CREATE TABLE IF NOT EXISTS targets (
    strategy        TEXT    NOT NULL,
    rebalance_id    TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    signal          REAL    NOT NULL,
    vol             REAL    NOT NULL,
    raw             REAL    NOT NULL,
    sigma_p         REAL    NOT NULL,
    conv            REAL    NOT NULL,
    sigma_eff       REAL    NOT NULL,
    s               REAL    NOT NULL,
    g               REAL    NOT NULL,
    caps_json       TEXT    NOT NULL DEFAULT '[]',
    funding_ann     REAL    NOT NULL DEFAULT 0,
    funding_haircut REAL    NOT NULL DEFAULT 1,
    target_notional REAL    NOT NULL,
    target_qty      REAL    NOT NULL DEFAULT 0,
    current_qty     REAL    NOT NULL DEFAULT 0,
    delta_notional  REAL    NOT NULL DEFAULT 0,
    traded          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy, rebalance_id, symbol)
);

CREATE TABLE IF NOT EXISTS slices (
    strategy      TEXT    NOT NULL,
    slice_id      TEXT    NOT NULL,
    rebalance_id  TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    seq           INTEGER NOT NULL DEFAULT 0,
    side          TEXT    NOT NULL,
    qty           REAL    NOT NULL,
    reduce_only   INTEGER NOT NULL DEFAULT 0,
    placed_ts     INTEGER NOT NULL,
    repegs        INTEGER NOT NULL DEFAULT 0,
    outcome       TEXT    NOT NULL DEFAULT 'pending',
    fill_qty      REAL    NOT NULL DEFAULT 0,
    avg_price     REAL    NOT NULL DEFAULT 0,
    taker         INTEGER NOT NULL DEFAULT 0,
    ended_ts      INTEGER,
    PRIMARY KEY (strategy, slice_id)
);
CREATE INDEX IF NOT EXISTS ix_slices_rebalance ON slices (strategy, rebalance_id);

CREATE TABLE IF NOT EXISTS governor_state (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy   TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    dd         REAL    NOT NULL,
    g_before   REAL    NOT NULL,
    g_after    REAL    NOT NULL,
    trigger    TEXT    NOT NULL DEFAULT '',
    applied    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_governor_ts ON governor_state (strategy, ts);

CREATE TABLE IF NOT EXISTS symbol_pnl_daily (
    strategy      TEXT    NOT NULL,
    day           TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    avg_notional  REAL    NOT NULL DEFAULT 0,
    price_pnl     REAL    NOT NULL DEFAULT 0,
    funding       REAL    NOT NULL DEFAULT 0,
    fees          REAL    NOT NULL DEFAULT 0,
    slippage      REAL    NOT NULL DEFAULT 0,
    net_pnl       REAL    NOT NULL DEFAULT 0,
    traded_notional REAL  NOT NULL DEFAULT 0,
    signal        REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy, day, symbol)
);
CREATE INDEX IF NOT EXISTS ix_sympnl_symbol ON symbol_pnl_daily (strategy, symbol, day);

CREATE TABLE IF NOT EXISTS trades (
    strategy     TEXT    NOT NULL,
    trade_key    TEXT    NOT NULL,   -- symbol:open_ts
    symbol       TEXT    NOT NULL,
    side         TEXT    NOT NULL,
    open_ts      INTEGER NOT NULL,
    close_ts     INTEGER,
    days         REAL    NOT NULL DEFAULT 0,
    pnl          REAL    NOT NULL DEFAULT 0,
    mae          REAL    NOT NULL DEFAULT 0,
    max_notional REAL    NOT NULL DEFAULT 0,
    entry_signal REAL    NOT NULL DEFAULT 0,
    exit_signal  REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy, trade_key)
);

CREATE TABLE IF NOT EXISTS illiquid_flags (
    strategy    TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    flagged_ts  INTEGER NOT NULL,
    until_ts    INTEGER NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    cleared_ts  INTEGER,
    PRIMARY KEY (strategy, symbol, flagged_ts)
);

CREATE TABLE IF NOT EXISTS rebalance_failures (
    strategy   TEXT    NOT NULL,
    symbol     TEXT    NOT NULL,
    day        TEXT    NOT NULL,
    reason     TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (strategy, symbol, day)
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id         TEXT PRIMARY KEY,
    strategy       TEXT    NOT NULL,
    created_ts     INTEGER NOT NULL,
    start_day      TEXT    NOT NULL,
    end_day        TEXT    NOT NULL,
    variant        TEXT    NOT NULL DEFAULT 'default',
    params_json    TEXT    NOT NULL DEFAULT '{}',
    manifest_json  TEXT    NOT NULL DEFAULT '{}',
    git_commit     TEXT    NOT NULL DEFAULT '',
    metrics_json   TEXT    NOT NULL DEFAULT '{}',
    equity_json    TEXT    NOT NULL DEFAULT '[]',
    duration_s     REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS robustness_reports (
    run_id    TEXT    NOT NULL,
    variant   TEXT    NOT NULL,
    net_pnl   REAL    NOT NULL,
    sharpe    REAL    NOT NULL,
    max_dd    REAL    NOT NULL,
    sign_ok   INTEGER NOT NULL,
    detail_json TEXT  NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, variant)
);

CREATE TABLE IF NOT EXISTS walkforward (
    run_id               TEXT    NOT NULL,
    window               TEXT    NOT NULL,
    param_set            TEXT    NOT NULL,
    test_sharpe          REAL    NOT NULL,
    rank                 INTEGER NOT NULL,
    n_params             INTEGER NOT NULL DEFAULT 0,
    default_in_top_half  INTEGER NOT NULL DEFAULT 0,
    is_default           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, window, param_set)
);

CREATE TABLE IF NOT EXISTS tracking (
    strategy       TEXT    NOT NULL,
    day            TEXT    NOT NULL,
    live_pnl       REAL    NOT NULL DEFAULT 0,
    ref_pnl        REAL    NOT NULL DEFAULT 0,
    cum_live       REAL    NOT NULL DEFAULT 0,
    cum_ref        REAL    NOT NULL DEFAULT 0,
    corr_30d       REAL,
    cum_diff_frac  REAL    NOT NULL DEFAULT 0,
    cost_ratio     REAL,
    turnover_ratio REAL,
    in_bounds      INTEGER NOT NULL DEFAULT 1,
    breach_days    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy, day)
);

CREATE TABLE IF NOT EXISTS bootstrap_distribution (
    run_id     TEXT    NOT NULL,
    horizon    TEXT    NOT NULL,   -- e.g. 3m
    percentile REAL    NOT NULL,
    value      REAL    NOT NULL,
    PRIMARY KEY (run_id, horizon, percentile)
);
