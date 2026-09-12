# Aegis — TREND

Diversified time-series momentum on Binance USDⓈ-M perpetuals, built to the PRD
`PRD-Trend-TSMOM-Bot.md` v1.0. TREND is sleeve 2 of 3 of the Aegis orchestrator
and shares this repository — and its `gateway/`, `accounting/`, `analytics/`,
`ops/` and `api/` layers — with the CARRY sleeve.

> **What it does.** Once a day, after the 00:00 UTC daily close, it computes a
> three-speed EMA-crossover trend score for each of the 16 most liquid USDT
> perpetuals, sizes each position so that *risk* rather than conviction is what
> stays constant, cuts exposure mechanically when it is losing, and executes
> passively so the costs stay below the edge. Between rebalances it only ever
> reduces risk.

> **What to expect.** Median 10–18 %/yr at a 20 % volatility target, 20–30 %
> maximum drawdown, ~30 % chance of a losing 12 months, ~55 % of months positive.
> Three negative months are inside that distribution, not evidence of failure —
> the kill rules in `docs/RISK.md` define what failure actually looks like.

## Quick start

```bash
uv venv --python 3.12 .venv && .venv/bin/pip install -e ".[dev]"

# The whole test suite is offline and deterministic.
.venv/bin/python -m pytest -q

# Backtest first — P0 is a gate, not a formality.
.venv/bin/python -m engine --strategy trend --mode backtest --start 2021-01-01

# Then paper, on live market data with locally simulated fills.
.venv/bin/python -m engine --strategy trend --mode paper
```

Modes are `backtest` → `paper` → `demo` (Binance testnet) → `live`, and the phase
gates in Section 7 of the PRD decide when you are allowed to move between them.
`--mode live` refuses to start without `LIVE_CONFIRM` and an API key that is
futures-only, IP-allowlisted and cannot withdraw.

## How it decides

| Step | Where | Summary |
|---|---|---|
| Universe | `universe/select.py` | Top 16 USDT perps by 30-day median quote volume, ≥ 400 days of history, stable-pegged bases excluded, BTC and ETH forced in. Refreshed monthly, point-in-time. |
| Signal | `signals/engine.py` | Three EMA speed pairs (8/24, 16/48, 32/96); each normalised by a 63-day price σ then a 250-day σ; the response function `z·e^(−z²/4)/0.89` caps conviction and *shrinks* it once a trend is over-extended. |
| Risk model | `riskmodel/estimators.py` | EWMA vol (10-day half-life, floored at 30 %/yr, capped at 300 %) and EWMA covariance (20-day half-life). |
| Sizing | `portfolio/sizing.py` | 25 %/yr per-asset vol target ÷ 16, scaled to a 20 %/yr portfolio target, then three caps applied in order: single 0.25×E, net 1.5×E, gross 2.5×E. |
| Overlay | `portfolio/funding_overlay.py` | Halve a position that would pay more than 30 %/yr funding to hold a crowded side. |
| Governor | `portfolio/governor.py` | ×0.5 at 12 % drawdown, ×0.25 at 20 %, restored at 15 % / 8 % — with hysteresis, on a time-weighted equity path so deposits cannot fake a peak. |
| Execution | `rebalance/` | Hysteresis band, risk-reducing orders first, post-only pegged limits re-pegged every 30 s, IOC after 5 minutes, TWAP-sliced above the liquidity clip, hard stop at 01:00 UTC. |

Every one of those numbers lives in `config/trend.yaml`. **The engine never
writes to it** (Invariant 7): there is no online tuner, and walk-forward analysis
is a robustness check whose output never reaches the live configuration.

## Safety properties

These are enforced in code and proved by tests, not by convention:

- **Exposure is bounded before an order exists.** The three caps are applied
  inside `size_targets`, and a property test asserts all three hold on every
  output over randomised inputs (Invariant 8).
- **The engine cannot autonomously increase risk.** Growth happens only in the
  scheduled daily rebalance, and only when no block is active. Everything the
  risk path can do is a reduction (Invariant 1).
- **A stale target can never flip a position.** Reducing and closing orders carry
  `reduceOnly`; a sign flip is planned as two legs, never one order through zero.
- **A restart mid-rebalance resumes.** The order plan and a cursor are persisted
  before the first order is sent.
- **Every number reconciles.** Positions, balance and fills are checked against
  the exchange, and daily attribution must satisfy
  `Σ symbol P&L + Σ non-position ledger items = equity change − net transfers`
  to 0.01 USDT.
- **The two sleeves cannot see each other.** Every row carries its strategy and
  every query binds it; a reflective test walks every repository read to prove it.

## Layout

```
src/aegis/core/         types, config, clock, errors, precision, context
src/aegis/storage/      SQLite, migrations, repositories
src/aegis/gateway/      ExchangeGateway protocol; Binance, paper and fake venues
src/aegis/universe/     monthly point-in-time selection, status/delisting watch
src/aegis/bars/         daily bar and funding ingestion
src/aegis/signals/      the trend score
src/aegis/riskmodel/    vol and covariance estimators
src/aegis/portfolio/    sizing, caps, funding overlay, governor, hysteresis
src/aegis/rebalance/    planner, passive sliced executor, drift monitor
src/aegis/strategy_trend/ state machine, scheduler, risk supervisor, kill rules
src/aegis/backtest_trend/ point-in-time backtester, robustness, walk-forward
src/aegis/accounting/   ledger, snapshots, reconciliation, attribution
src/aegis/analytics/    metric engine
src/aegis/ops/          alerts, controls, phase gates, heartbeat, Telegram, reports
src/aegis/api/          read-only FastAPI behind the dashboard
ui/                     React dashboard
docs/                   ARCHITECTURE.md, SERVICES.md, RISK.md, RUNBOOK.md
```

## Deployment

Two containers from one image on a single always-free host, each with its own
database, Healthchecks check and Litestream replica path:

```bash
cp .env.example .env    # fill in keys, then
docker compose up -d trend dashboard
```

Total infrastructure cost is €0 (≤ €6/month on the VPS fallback). See
`docs/RUNBOOK.md` for the operational procedures and `docs/RISK.md` for the
consolidated rule table.

## Documentation

- `docs/ARCHITECTURE.md` — the interface contract every module is written against
- `docs/SERVICES.md` — service signatures
- `docs/RISK.md` — the risk supervisor rule table and kill rules
- `docs/RUNBOOK.md` — day-to-day operation, incident response, restore drill
- `docs/BACKTEST.md` — how the point-in-time backtest is built and reproduced
