# Service contract (wave 2)

Everything below is the agreed interface between the runtime modules. A module
may add private helpers freely; it may not change a signature listed here
without updating every caller.

## The context object

Every service takes exactly one collaborator bundle:

```python
from aegis.core.context import Context   # cfg, clock, gateway, repos, alerts

class SomeService:
    def __init__(self, ctx: Context) -> None: ...
```

`ctx.repos` is `aegis.storage.repositories.Repositories` (already strategy-bound).
`ctx.alerts` is `aegis.ops.alerts.AlertBus` — use `ctx.alerts.warn(code, msg, context)`.
Alert `code`s are SCREAMING_SNAKE and stable; they are what the dashboard filters on.

## bars/ — BarService (US-T03)

```python
class BarService:
    def backfill(self, symbols: Sequence[str], min_days: int | None = None) -> dict[str, int]
    def fetch_closed_day(self, symbols, day: date) -> tuple[set[str], set[str]]   # (fetched, missing)
    def ensure_day(self, symbols, day: date, deadline_ms: int) -> tuple[set[str], set[str]]
    def forward_fill(self, symbols, through: date) -> int          # writes filled=1 rows
    def closes(self, symbol: str, end: date | None = None, limit: int | None = None) -> list[float]
    def log_returns(self, symbol: str, end: date | None = None, limit: int | None = None) -> list[float]
    def volume_history(self, symbols, days: int, before: date | None = None) -> dict[str, list[DailyBar]]
    def avg_daily_quote_volume(self, symbol: str, days: int = 30, before: date | None = None) -> float
```

Forward-filled bars carry `filled=True` and are used for **signal continuity only,
never for P&L** (AC 2). `ensure_day` retries until `deadline_ms`; a symbol still
missing makes the caller defer the rebalance and alert `WARN` code `BAR_MISSING`.

## universe/ — UniverseService, StatusWatch (US-T02, US-T11 AC 2-3, 5.10)

```python
class UniverseService:
    def refresh_if_due(self, now_ms: int, *, force: bool = False) -> UniverseResult | None
    def current_symbols(self, now_ms: int) -> list[str]           # excludes illiquid-flagged
    def all_symbols(self, now_ms: int) -> list[str]               # includes illiquid-flagged
    def leavers(self, now_ms: int) -> list[str]                   # in last month, not this month
    def result_for(self, month: str) -> UniverseResult | None

class StatusWatch:
    def check(self, now_ms: int) -> list[str]        # symbols whose status left TRADING
    def check_funding_intervals(self, now_ms: int) -> dict[str, float]
```

`refresh_if_due` fires on the 1st at `universe.refresh_time_utc` and on first
start; it persists via `repos.universe.save` and clears illiquid flags
(`repos.illiquid.clear_all`). Alert codes: `UNIVERSE_REFRESH`, `UNIVERSE_ENTRY`,
`UNIVERSE_EXIT`, `SYMBOL_STATUS`, `SYMBOL_DELISTED`, `FUNDING_INTERVAL_CHANGE`.

## accounting/ (CARRY US-10/11 reused, US-T14)

```python
class LedgerService:
    def sync(self, now_ms: int) -> int                      # new rows booked
    def backfill(self, since_ms: int) -> int

class SnapshotService:
    def take(self, now_ms: int) -> AccountState
    def update_equity_curve(self, day: date, now_ms: int) -> EquityPoint
    def drawdown(self, now_ms: int) -> float                # from the time-weighted curve

@dataclass(frozen=True)
class ReconResult:
    ok: bool
    kind: str
    breaks: tuple[dict, ...]
    detail: str

class Reconciler:
    def positions(self, now_ms: int) -> ReconResult
    def balance(self, now_ms: int) -> ReconResult
    def fills(self, now_ms: int) -> ReconResult
    def run_all(self, now_ms: int) -> ReconResult

class Attribution:
    def compute_day(self, day: date, now_ms: int) -> list[dict]    # -> symbol_pnl_daily
    def identity_check(self, day: date) -> tuple[bool, float]      # (ok, residual_usdt)
    def update_trades(self, day: date, now_ms: int) -> int         # open->flat episodes
```

The identity (US-T14 AC 3) is
`sum(symbol P&L) + sum(non-position ledger items) == equity change - net transfers`
to **0.01 USDT per day**.

## analytics/

`metrics.py` and `trend_metrics.py` are **pure functions** over arrays; `engine.py`
loads from repos, calls them, and writes `MetricValue` rows.

```python
class MetricsEngine:
    def compute_all(self, now_ms: int) -> list[MetricValue]
    def compute_period(self, period: str, now_ms: int) -> list[MetricValue]
```

Metric `name`s are lowercase snake and stable (`sharpe`, `sortino`, `max_drawdown`,
`calmar`, `var_95`, `skew`, `kurtosis`, `realised_vol`, `vol_ratio`, `gross_exposure`,
`net_exposure`, `turnover`, `cost_per_unit_bps`, `execution_alpha`, `maker_ratio`,
`rebalance_completion`, `beta_btc`, `corr_btc`, `corr_carry`, `long_pnl`, `short_pnl`,
`hit_rate_long`, `hit_rate_short`, `governor_time_g1`, `governor_time_g05`,
`governor_time_g025`, `funding_share`, `vol_target_adherence`, `concentration`,
`trade_count`, `avg_holding_days`, `win_rate`, `information_ratio`, `net_of_infra`,
`cash_alternative`). Periods: `7d 30d 90d mtd ytd since_inception`.

## rebalance/ (US-T09, US-T10, US-T11)

```python
# planner.py — PURE
def build_plan(targets: Targets, positions: Mapping[str, Position],
               symbol_info: Mapping[str, SymbolInfo], marks: Mapping[str, float],
               equity: float, cfg: AppConfig, avg_minute_volume: Mapping[str, float],
               universe: Collection[str]) -> list[PlannedOrder]

@dataclass(frozen=True)
class RebalanceOutcome:
    rebalance_id: str
    status: RebalanceStatus
    completion_pct: float
    traded_notional: float
    planned_notional: float
    fees: float
    avg_slippage_bps: float
    maker_ratio: float
    residuals: tuple[dict, ...]
    duration_s: float

class RebalanceExecutor:
    def execute(self, rebalance_id: str, plan: Sequence[PlannedOrder], *,
                decision_mids: Mapping[str, float], end_ts_ms: int,
                escalate_s: int | None = None) -> RebalanceOutcome
    def resume(self, rebalance_id: str, end_ts_ms: int) -> RebalanceOutcome
    def flatten_all(self, reason: str, now_ms: int) -> RebalanceOutcome
    def reduce_by(self, fractions: Mapping[str, float], reason: str, now_ms: int) -> RebalanceOutcome

class DriftMonitor:
    def check(self, now_ms: int, *, in_rebalance_window: bool) -> list[dict]
```

`execute` persists the plan (with a `cursor`) **before the first order**; `resume`
picks up from the stored cursor. Risk cuts use `escalate_s = cfg.exec.risk_escalate_s`.

## strategy_trend/ (US-T12, US-T13, 5.11)

```python
class RiskSupervisor:
    def check(self, now_ms: int) -> ExposureSnapshot
    def survivable_move(self, positions, equity, maint_margin_rate: float = 0.005) -> float
    def would_breach_downtime_rule(self, targets: Targets, equity: float) -> bool

@dataclass(frozen=True)
class KillAction:
    rule: str
    severity: Severity
    block_risk_increasing: bool
    flatten: bool
    halt: bool
    message: str
    context: dict

class KillRules:
    def evaluate(self, now_ms: int) -> list[KillAction]

class TrendRunner:            # the process main loop
    def __init__(self, ctx: Context) -> None
    def tick(self, now_ms: int) -> None      # one pass; called in a loop and by tests
    def run(self) -> None                    # loop until stopped
```

`blocks` (a list of rule names) live in `engine_state.blocks_json`. While any block
is present the engine may only send **risk-reducing** orders (Invariant 1).

## ops/

```python
class Controls:
    def start(self, operator: str, reason: str = "") -> None
    def pause(self, operator: str, reason: str = "") -> None      # blocks rebalances, not risk actions
    def resume(self, operator: str, reason: str = "") -> None
    def stop(self, operator: str, reason: str, confirm: str) -> None   # Aegis US-18 confirmation
    def flatten_all(self, operator: str, reason: str, confirm: str) -> None
    def clear_halt(self, operator: str, reason: str) -> None      # requires a reason (US-T13 AC 3)

@dataclass(frozen=True)
class GateResult:
    phase: Phase
    passed: bool
    criteria: tuple[dict, ...]    # {"name","required","actual","passed"}

class PhaseGates:
    def evaluate(self, phase: Phase, now_ms: int) -> GateResult
    def record(self, result: GateResult, operator: str, reason: str, capital: float, now_ms: int) -> None

class Heartbeat:
    def beat(self, now_ms: int, ok: bool, detail: str = "") -> None
    def uptime_pct(self, start_ms: int, end_ms: int) -> float

class Reporter:
    def daily(self, day: date, now_ms: int) -> str
    def rebalance_summary(self, rebalance_id: str, now_ms: int) -> str
    def weekly(self, week_key: str, now_ms: int) -> str
    def monthly(self, month_key: str, now_ms: int) -> str
```

Report bodies are stored in `reports` and must match Appendix D formats.

## backtest_trend/ (US-T16, Section 11)

```python
class ArchiveLoader:          # data.binance.vision, cached on disk
    def inventory(self, start_month: str, end_month: str) -> dict[str, list[str]]  # symbol -> months
    def daily_bars(self, symbol: str, month: str) -> list[DailyBar]
    def funding(self, symbol: str, month: str) -> list[FundingRate]

class PointInTimeUniverse:
    def build(self, months: Sequence[str]) -> dict[str, UniverseResult]

@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    equity: tuple[dict, ...]           # {"day","equity","twr_index","drawdown"}
    daily_returns: tuple[float, ...]
    metrics: dict[str, float]
    symbol_pnl: tuple[dict, ...]
    manifest: dict
    duration_s: float

class Simulator:
    def run(self, start: date, end: date, *, variant: str = "default",
            overrides: dict | None = None) -> BacktestResult
```

The simulator **reuses `signals/`, `riskmodel/`, `portfolio/` unchanged** (AC 2).
Fills at the next daily open with the conservative cost model (taker fee +
`exec.slippage_bps`). Deterministic: identical manifests produce identical outputs.

## api/

FastAPI, **read-only** over the SQLite files (the engine writes; the API never does),
except the `/controls` endpoints which append to `control_log` for the engine to pick up.

**The controls contract.** `POST /api/{strategy}/controls` must do exactly one thing:

```python
repos.state.log_control(action, operator, reason, {"source": "api", "confirm": confirm}, now_ms)
```

It must NOT touch `engine_state`, and it must never *consume* the confirmation
token: whatever the operator typed is forwarded in the payload and the engine
re-checks it, so the rule lives in one place. The endpoint may pre-flight the
destructive actions (400 rather than a 202 for a command that cannot work), but
only by asking the engine's own question — `cfg.phase.live_confirm or
CONFIRM_TOKEN`, exactly as `Controls._check_confirm` does. `"source": "api"` is what
marks the row as a *command*: the engine skips rows without it, because `Controls`
writes its own audit row for every action it applies and replaying those would
loop forever. The engine drains rows with `repos.state.controls_after(last_id)` at
the top of each tick, applies each exactly once, and records the last applied id in
`engine_state.context["last_control_id"]`. A refused action (bad token, missing
reason, unknown verb) raises an `AegisError`, is alerted as `CONTROL_REFUSED`, and
changes nothing.
Routes are under `/api/{strategy}/...` and every page of US-T18 must be servable from
stored data alone, in < 1 s.
