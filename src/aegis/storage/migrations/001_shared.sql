-- Shared Aegis layer (CARRY US-10/11/13/15/16/17/19). Every row carries
-- `strategy` so that CARRY and TREND can share a schema while remaining
-- queryable in isolation (US-T01 AC 2).

CREATE TABLE IF NOT EXISTS symbol_meta (
    strategy              TEXT    NOT NULL,
    symbol                TEXT    NOT NULL,
    base_asset            TEXT    NOT NULL,
    quote_asset           TEXT    NOT NULL,
    status                TEXT    NOT NULL,
    contract_type         TEXT    NOT NULL,
    tick_size             REAL    NOT NULL,
    step_size             REAL    NOT NULL,
    min_qty               REAL    NOT NULL,
    min_notional          REAL    NOT NULL,
    price_precision       INTEGER NOT NULL,
    quantity_precision    INTEGER NOT NULL,
    onboard_date_ms       INTEGER NOT NULL DEFAULT 0,
    maker_fee             REAL    NOT NULL DEFAULT 0.0002,
    taker_fee             REAL    NOT NULL DEFAULT 0.0005,
    funding_interval_hours REAL   NOT NULL DEFAULT 8.0,
    updated_ts            INTEGER NOT NULL,
    PRIMARY KEY (strategy, symbol)
);

CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT    NOT NULL,
    ts            INTEGER NOT NULL,
    income_type   TEXT    NOT NULL,
    asset         TEXT    NOT NULL,
    amount        REAL    NOT NULL,
    symbol        TEXT,
    tran_id       TEXT    NOT NULL,
    trade_id      TEXT,
    info          TEXT    NOT NULL DEFAULT '',
    UNIQUE (strategy, tran_id, income_type, ts, amount)
);
CREATE INDEX IF NOT EXISTS ix_ledger_strategy_ts ON ledger (strategy, ts);
CREATE INDEX IF NOT EXISTS ix_ledger_type ON ledger (strategy, income_type, ts);

CREATE TABLE IF NOT EXISTS income_sync_state (
    strategy      TEXT PRIMARY KEY,
    last_ts       INTEGER NOT NULL,
    updated_ts    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy          TEXT    NOT NULL,
    ts                INTEGER NOT NULL,
    wallet_balance    REAL    NOT NULL,
    margin_balance    REAL    NOT NULL,
    unrealized_pnl    REAL    NOT NULL,
    available_balance REAL    NOT NULL,
    maint_margin      REAL    NOT NULL,
    initial_margin    REAL    NOT NULL,
    gross_notional    REAL    NOT NULL DEFAULT 0,
    net_notional      REAL    NOT NULL DEFAULT 0,
    margin_ratio      REAL    NOT NULL DEFAULT 0,
    positions_json    TEXT    NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_snapshots_strategy_ts ON snapshots (strategy, ts);

CREATE TABLE IF NOT EXISTS equity_curve (
    strategy      TEXT    NOT NULL,
    day           TEXT    NOT NULL,
    ts            INTEGER NOT NULL,
    equity        REAL    NOT NULL,
    net_transfer  REAL    NOT NULL DEFAULT 0,
    twr_factor    REAL    NOT NULL DEFAULT 1,
    twr_index     REAL    NOT NULL DEFAULT 1,
    peak_index    REAL    NOT NULL DEFAULT 1,
    drawdown      REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy, day)
);

CREATE TABLE IF NOT EXISTS orders (
    strategy        TEXT    NOT NULL,
    order_id        TEXT    NOT NULL,
    client_order_id TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    order_type      TEXT    NOT NULL,
    qty             REAL    NOT NULL,
    price           REAL,
    time_in_force   TEXT    NOT NULL,
    reduce_only     INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL,
    filled_qty      REAL    NOT NULL DEFAULT 0,
    avg_price       REAL    NOT NULL DEFAULT 0,
    created_ts      INTEGER NOT NULL,
    updated_ts      INTEGER NOT NULL,
    rebalance_id    TEXT,
    slice_id        TEXT,
    intent          TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (strategy, order_id)
);
CREATE INDEX IF NOT EXISTS ix_orders_rebalance ON orders (strategy, rebalance_id);
CREATE INDEX IF NOT EXISTS ix_orders_symbol_ts ON orders (strategy, symbol, created_ts);

CREATE TABLE IF NOT EXISTS fills (
    strategy      TEXT    NOT NULL,
    trade_id      TEXT    NOT NULL,
    order_id      TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    qty           REAL    NOT NULL,
    price         REAL    NOT NULL,
    fee           REAL    NOT NULL,
    fee_asset     TEXT    NOT NULL,
    is_maker      INTEGER NOT NULL,
    realized_pnl  REAL    NOT NULL DEFAULT 0,
    ts            INTEGER NOT NULL,
    rebalance_id  TEXT,
    slice_id      TEXT,
    decision_mid  REAL,
    slippage_bps  REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy, trade_id)
);
CREATE INDEX IF NOT EXISTS ix_fills_ts ON fills (strategy, ts);
CREATE INDEX IF NOT EXISTS ix_fills_symbol_ts ON fills (strategy, symbol, ts);
CREATE INDEX IF NOT EXISTS ix_fills_rebalance ON fills (strategy, rebalance_id);

CREATE TABLE IF NOT EXISTS positions (
    strategy         TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    qty              REAL    NOT NULL,
    entry_price      REAL    NOT NULL,
    mark_price       REAL    NOT NULL,
    unrealized_pnl   REAL    NOT NULL DEFAULT 0,
    leverage         REAL    NOT NULL DEFAULT 5,
    liquidation_price REAL   NOT NULL DEFAULT 0,
    adl_quantile     INTEGER NOT NULL DEFAULT 0,
    ts               INTEGER NOT NULL,
    PRIMARY KEY (strategy, symbol)
);

CREATE TABLE IF NOT EXISTS reconciliations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT    NOT NULL,
    ts            INTEGER NOT NULL,
    kind          TEXT    NOT NULL,
    ok            INTEGER NOT NULL,
    detail        TEXT    NOT NULL DEFAULT '',
    breaks_json   TEXT    NOT NULL DEFAULT '[]',
    resolved_ts   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_recon_strategy_ts ON reconciliations (strategy, ts);

CREATE TABLE IF NOT EXISTS metrics (
    strategy      TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    period        TEXT    NOT NULL,
    as_of_ts      INTEGER NOT NULL,
    value         REAL,
    n_obs         INTEGER NOT NULL DEFAULT 0,
    std_error     REAL,
    extra_json    TEXT    NOT NULL DEFAULT '{}',
    PRIMARY KEY (strategy, name, period, as_of_ts)
);
CREATE INDEX IF NOT EXISTS ix_metrics_lookup ON metrics (strategy, name, period, as_of_ts DESC);

CREATE TABLE IF NOT EXISTS approvals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT    NOT NULL,
    phase         TEXT    NOT NULL,
    ts            INTEGER NOT NULL,
    granted       INTEGER NOT NULL,
    operator      TEXT    NOT NULL DEFAULT '',
    reason        TEXT    NOT NULL DEFAULT '',
    evidence_json TEXT    NOT NULL DEFAULT '{}',
    capital_usdt  REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_approvals_strategy ON approvals (strategy, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT    NOT NULL,
    ts            INTEGER NOT NULL,
    severity      TEXT    NOT NULL,
    code          TEXT    NOT NULL,
    message       TEXT    NOT NULL,
    context_json  TEXT    NOT NULL DEFAULT '{}',
    delivered     INTEGER NOT NULL DEFAULT 0,
    acked_ts      INTEGER
);
CREATE INDEX IF NOT EXISTS ix_alerts_strategy_ts ON alerts (strategy, ts);
CREATE INDEX IF NOT EXISTS ix_alerts_code ON alerts (strategy, code, ts);

CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT    NOT NULL,
    ts            INTEGER NOT NULL,
    kind          TEXT    NOT NULL,
    period_key    TEXT    NOT NULL,
    body          TEXT    NOT NULL,
    delivered     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (strategy, kind, period_key)
);

CREATE TABLE IF NOT EXISTS engine_state (
    strategy      TEXT PRIMARY KEY,
    state         TEXT    NOT NULL,
    phase         TEXT    NOT NULL,
    paused        INTEGER NOT NULL DEFAULT 0,
    stopped       INTEGER NOT NULL DEFAULT 0,
    safe_mode     INTEGER NOT NULL DEFAULT 0,
    halt_reason   TEXT    NOT NULL DEFAULT '',
    governor_g    REAL    NOT NULL DEFAULT 1.0,
    blocks_json   TEXT    NOT NULL DEFAULT '[]',
    context_json  TEXT    NOT NULL DEFAULT '{}',
    updated_ts    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS control_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT    NOT NULL,
    ts            INTEGER NOT NULL,
    action        TEXT    NOT NULL,
    operator      TEXT    NOT NULL DEFAULT '',
    reason        TEXT    NOT NULL DEFAULT '',
    payload_json  TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT    NOT NULL,
    ts            INTEGER NOT NULL,
    ok            INTEGER NOT NULL,
    detail        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_heartbeats_ts ON heartbeats (strategy, ts);

CREATE TABLE IF NOT EXISTS funding_rates (
    strategy        TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    funding_time    INTEGER NOT NULL,
    rate            REAL    NOT NULL,
    interval_hours  REAL    NOT NULL DEFAULT 8,
    PRIMARY KEY (strategy, symbol, funding_time)
);
