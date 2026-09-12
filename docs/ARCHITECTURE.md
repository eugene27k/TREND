# Aegis — architecture contract

This document is the interface contract. Every module in the repository is
written against it; if you change a signature here, you change it everywhere.

## Repository shape

```
src/aegis/
  core/        types.py  config.py  clock.py  errors.py  precision.py      <- the contract
  storage/     db.py  migrations/*.sql  repositories.py
  gateway/     base.py (Protocol)  binance.py  paper.py  fake.py  factory.py
  accounting/  ledger.py  snapshots.py  reconcile.py  attribution.py
  analytics/   metrics.py  trend_metrics.py  engine.py
  ops/         alerts.py  controls.py  phases.py  heartbeat.py  telegram.py  reports.py  backup.py
  api/         app.py  routes/*.py
  universe/    select.py  service.py  status_watch.py
  bars/        service.py  archive.py
  signals/     engine.py
  riskmodel/   estimators.py
  portfolio/   sizing.py  funding_overlay.py  governor.py  hysteresis.py
  rebalance/   planner.py  executor.py  drift.py
  strategy_trend/  machine.py  scheduler.py  risk_supervisor.py  kill_rules.py  runner.py
  backtest_trend/  universe_builder.py  simulator.py  robustness.py  walkforward.py  bootstrap.py  tracking.py
src/engine/__main__.py      -> `python -m engine --strategy trend --mode paper`
ui/                          React + Vite dashboard
tests/                       mirrors src/aegis
```

## Non-negotiables (PRD Section 2)

1. No autonomous risk escalation — the engine may only *reduce* risk on its own.
2. Every number reconciles — the ledger identity is a test, not a comment.
3. Idle is valid — no signal means no position, not a forced trade.
4. No generative AI at runtime.
5. Free-first — nothing paid in the runtime path.
6. Sub-account isolation — software never transfers capital.
7. **No live parameter re-optimisation.** Config is read-only to the engine.
8. **Exposure is bounded by construction** — caps applied in `size_targets`
   *before* an order object exists, re-checked after fills.
9. **The daily rebalance is the only scheduled trading event.** Everything else
   the engine does between rebalances is risk-reducing.

## Core rules every module follows

- **Time.** Never call `datetime.now()`. Take a `Clock` (`aegis.core.clock`).
  All timestamps are epoch **milliseconds, UTC**, named `*_ts` / `*_ms`.
  Calendar days are `datetime.date`; the DB stores `YYYY-MM-DD` strings.
- **Money.** USDT floats. Quantities/prices become exchange strings only in
  `aegis.core.precision`. Signed notional: `+` long, `-` short.
- **Purity.** `signals/`, `riskmodel/`, `portfolio/`, `universe/select.py`
  contain pure functions only: no I/O, no clock, no DB, no positions.
  This is enforced by signature tests (US-T04 AC 5).
- **Strategy tagging.** Every shared-table write passes `strategy=`. Every
  shared-table read filters `WHERE strategy = ?`. No exceptions (US-T01 AC 2).
- **Exchange access.** Only through `ExchangeGateway` (`aegis.gateway.base`).
  No module imports `httpx` except `gateway/binance.py`.
- **No network in tests.** Tests use `FakeGateway` and fixture data only.
- **Errors.** Raise from `aegis.core.errors`; never bare `Exception`.

## Key signatures (do not change without updating every caller)

```python
# universe/select.py                                    (US-T02 AC 1)
def select_universe(
    exchange_info: dict[str, SymbolInfo],
    volume_history: dict[str, list[DailyBar]],
    params: UniverseConfig,
    month: str,
) -> UniverseResult: ...

# signals/engine.py                                     (US-T04 AC 1)
def compute_signal(prices: Sequence[float] | pd.Series, params: SignalConfig,
                   symbol: str = "", bar_day: date | None = None) -> SignalResult: ...

# riskmodel/estimators.py                               (US-T05)
def ewma_vol(returns: Sequence[float], half_life: float = 10.0, *,
             floor: float = 0.30, cap: float = 3.00, annualisation_days: int = 365) -> float: ...
def ewma_cov(returns: Mapping[str, Sequence[float]], half_life: float = 20.0,
             *, min_obs: int = 60, annualisation_days: int = 365) -> RiskModel: ...

# portfolio/sizing.py                                   (US-T06)
def size_targets(signals: Mapping[str, float], vols: Mapping[str, float],
                 risk_model: RiskModel, equity: float, governor_g: float,
                 cfg: AppConfig, funding_ann: Mapping[str, float] | None = None) -> Targets: ...

# portfolio/funding_overlay.py                          (US-T07)
def apply_funding_overlay(target_notional: float, funding_ann: float,
                          cfg: FundingConfig) -> tuple[float, float]: ...   # (notional, haircut)

# portfolio/governor.py                                 (US-T08)
def governor(dd: float, current_g: float, cfg: GovernorConfig) -> float: ...

# portfolio/hysteresis.py                               (US-T09 AC 2)
def should_trade(target: float, current: float, equity: float,
                 cfg: RebalanceConfig, in_universe: bool = True) -> bool: ...
```

## Sizing — the exact order of operations (Section 5.5 -> 5.6 -> 5.8)

```
raw_i    = signal_i * (sigma_tgt / sigma_i) / N * E          # N = universe.size, fixed 16
w        = raw / E
sigma_p  = sqrt(w^T Sigma w)          Sigma = diag(vol) Corr diag(vol), annualised
conv     = mean_i |signal_i|          over the *universe* (missing signal = 0)
sigma_eff= sigma_p,tgt * min(1, conv / conviction_full)
s        = clip(sigma_eff / sigma_p, 0, s_max)
target_i = raw_i * s * g
-- caps, in this order, each re-checked after the previous one:
   1. single:  |target_i| <= caps.single * E                  (clip each, independently)
   2. net:     sum(target) in [-caps.net * E, +caps.net * E]  (scale the dominant side)
   3. gross:   sum|target| <= caps.gross * E                  (scale everything)
-- funding overlay (5.6), then rounding:
   long  with funding_ann > +threshold -> x haircut
   short with funding_ann < -threshold -> x haircut
-- zeroing: |target| < max(min_notional, sizing.min_target_frac * E) -> 0
```

`sigma_p` is computed over the symbols with a signal; a zero-variance book
(`sigma_p == 0`) yields `s = 0` and therefore flat targets — idle is valid.

## Rebalance lifecycle

`IDLE -00:05-> COMPUTING -targets-> REBALANCING -done|01:00-> IDLE`, and from
any state `RISK_ACTION` (governor cut / kill rule / delisting) or `HALTED_RISK`.
The plan is persisted in `rebalances.order_plan_json` **before the first order**,
with a `cursor` column, so a restart at 00:30 resumes rather than recomputing
(US-T09 AC 4).

Deltas are computed against **reconciled exchange positions**, never local state
(US-T09 AC 1), and ordered risk-reducing first (`|current|` decreasing), then
risk-increasing.

## Execution (Section 5.9)

Decision mid recorded per symbol at rebalance start. Clip =
`0.005 * avg_daily_quote_volume_30d / 1440`. Orders above the clip are TWAP-sliced
into <= 30 slices over <= 30 minutes. Each slice: post-only (`GTX`) at the passive
best, re-peg every 30 s, escalate to IOC taker after 300 s (60 s for risk cuts).
`reduce_only=True` on any order that reduces or closes. Window ends 01:00 UTC.

## Testing rules (PRD Section 14)

- Coverage floor **85 %** on `signals/`, `riskmodel/`, `portfolio/`,
  `rebalance/`, `universe/`, `backtest_trend/`.
- Appendix C vectors are reproduced to **1e-6** and live in
  `tests/fixtures/appendix_c.py` — they are verified correct against the PRD.
- Every test is deterministic and offline. Seeded RNG only.
- Test names carry their AC: `test_us_t06_ac2_net_cap_scales_dominant_side`.
