// Thin typed client over the read-only FastAPI service. Every page loads from
// stored data (US-T18 AC 8: < 1 s on the free VM), so there is no client-side
// computation of anything the metric engine already persisted.
//
// Every interface below mirrors `components.schemas` in build/openapi.json
// one-for-one: same field names, same optionality, same nullability. A field
// that is nullable in the spec is `| null` here, because a wrong name compiles
// perfectly and then renders "n/a" forever.

/** The API accepts any registered sleeve name; GET /api/strategies lists them. */
export type Strategy = string

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message)
  }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`/api${path}`, { signal, headers: { Accept: 'application/json' } })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new ApiError(res.status, body || res.statusText)
  }
  return (await res.json()) as T
}

const q = (params: Record<string, string | number | undefined>): string => {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
  return parts.length ? `?${parts.join('&')}` : ''
}

export const api = {
  health: (sig?: AbortSignal) => get<HealthResponse>('/health', sig),
  strategies: (sig?: AbortSignal) => get<StrategiesResponse>('/strategies', sig),
  allSleeves: (sig?: AbortSignal) => get<CombinedOverviewResponse>('/overview', sig),
  overview: (s: Strategy, sig?: AbortSignal) => get<OverviewResponse>(`/${s}/overview`, sig),
  signals: (s: Strategy, sig?: AbortSignal) => get<SignalsResponse>(`/${s}/signals`, sig),
  signalHistory: (s: Strategy, symbol: string, days = 90, sig?: AbortSignal) =>
    get<SignalHistoryResponse>(`/${s}/signals/${encodeURIComponent(symbol)}/history${q({ days })}`, sig),
  positions: (s: Strategy, governorLimit?: number, sig?: AbortSignal) =>
    get<PositionsResponse>(`/${s}/positions${q({ governor_limit: governorLimit })}`, sig),
  rebalances: (s: Strategy, limit?: number, sig?: AbortSignal) =>
    get<RebalancesResponse>(`/${s}/rebalances${q({ limit })}`, sig),
  rebalance: (s: Strategy, id: string, sig?: AbortSignal) =>
    get<RebalanceDetail>(`/${s}/rebalances/${encodeURIComponent(id)}`, sig),
  attribution: (s: Strategy, sig?: AbortSignal) => get<AttributionResponse>(`/${s}/attribution`, sig),
  metrics: (s: Strategy, period?: string, sig?: AbortSignal) =>
    get<MetricsResponse>(`/${s}/metrics${q({ period })}`, sig),
  operations: (s: Strategy, sig?: AbortSignal) => get<OperationsResponse>(`/${s}/operations`, sig),
  backtest: (s: Strategy, opts?: { run_id?: string; horizon?: string; limit?: number }, sig?: AbortSignal) =>
    get<BacktestResponse>(`/${s}/backtest${q({ ...opts })}`, sig),
  universe: (s: Strategy, months?: number, sig?: AbortSignal) =>
    get<UniverseResponse>(`/${s}/universe${q({ months })}`, sig),
  controls: (s: Strategy, sig?: AbortSignal) => get<ControlsResponse>(`/${s}/controls`, sig),
  control: async (s: Strategy, body: ControlRequest, signal?: AbortSignal): Promise<ControlAccepted> => {
    const res = await fetch(`/api/${s}/controls`, {
      method: 'POST',
      signal,
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new ApiError(res.status, (await res.text().catch(() => '')) || res.statusText)
    return (await res.json()) as ControlAccepted
  },
}

// --- health / registry -----------------------------------------------------

export interface HealthResponse {
  ok: boolean
  now_ms: number
  started_ms: number
  uptime_s: number
  strategies: string[]
}

export interface StrategyInfo {
  strategy: string
  mode: string
  phase: string
  db_path: string
}

export interface StrategiesResponse {
  strategies: StrategyInfo[]
}

// --- overview (GET /api/{strategy}/overview, GET /api/overview) -------------

export interface Kpis {
  equity: number
  since_inception_net: number
  net_30d: number
  realised_vol: number | null
  vol_target: number
  vol_ratio: number | null
  max_drawdown: number | null
  current_drawdown: number
  governor_g: number
  sharpe: number | null
  sharpe_std_error: number | null
  sharpe_n_obs: number
  cash_alternative: number | null
  gross_notional: number
  net_notional: number
  gross_x: number
  net_x: number
  phase: string
  state: string
  risk_status: string
  paused: boolean
  blocks: string[]
  below_min_active_days: boolean
}

export interface CurvePoint {
  day: string
  equity: number
  twr_index: number
  drawdown: number
  cash_alternative?: number | null
  backtest_reference?: number | null
}

export interface PnlComponents {
  price_pnl: number
  funding: number
  fees: number
  slippage: number
  net_pnl: number
  traded_notional: number
}

export interface SidePnl {
  side: string
  net_pnl: number
}

export interface OverviewResponse {
  strategy: string
  as_of_ts: number
  kpis: Kpis
  curve: CurvePoint[]
  cumulative_components: PnlComponents
  by_side: SidePnl[]
  reference_run_id: string | null
}

export interface SleeveSummary {
  strategy: string
  equity: number
  current_drawdown: number
  max_drawdown: number | null
  governor_g: number
  state: string
  phase: string
  risk_status: string
  paused: boolean
  blocks: string[]
}

export interface CombinedOverviewResponse {
  as_of_ts: number
  sleeves: SleeveSummary[]
  total_equity: number
}

// --- signals (GET /api/{strategy}/signals[/{symbol}/history]) ---------------

export interface SignalRow {
  symbol: string
  day: string | null
  signal: number
  u1: number | null
  u2: number | null
  u3: number | null
  warm: boolean
  vol: number | null
  target_notional: number
  current_notional: number
  delta_notional: number
  funding_ann: number
  funding_haircut: number
  haircut_applied: boolean
  in_universe: boolean
  illiquid: boolean
}

export interface SignalsResponse {
  strategy: string
  as_of_ts: number
  day: string | null
  rebalance_id: string | null
  rows: SignalRow[]
}

export interface SignalHistoryPoint {
  day: string
  signal: number
  u1: number | null
  u2: number | null
  u3: number | null
  warm: boolean
}

export interface SignalHistoryResponse {
  strategy: string
  symbol: string
  days: number
  points: SignalHistoryPoint[]
}

// --- positions & risk (GET /api/{strategy}/positions) ----------------------

export interface PositionRow {
  symbol: string
  qty: number
  side: string
  notional: number
  entry_price: number
  mark_price: number
  unrealized_pnl: number
  funding_accrued: number
  adl_quantile: number
  leverage: number
  liquidation_price: number
  target_notional: number
  ts: number
}

export interface CapRow {
  cap: string
  limit_x: number
  limit_usdt: number
  used_usdt: number
  used_x: number
  utilisation: number
  breached: boolean
}

export interface MarginPanel {
  equity: number
  wallet_balance: number
  available_balance: number
  maint_margin: number
  margin_ratio: number
  margin_amber: number
  margin_red: number
  survivable_move: number
  shock_price: number
  survives_downtime: boolean
}

export interface GovernorRow {
  ts: number
  dd: number
  g_before: number
  g_after: number
  trigger: string
  applied: boolean
}

export interface KillRuleRow {
  rule: string
  active: boolean
  detail: string
  last_fired_ts: number | null
}

export interface PositionsResponse {
  strategy: string
  as_of_ts: number
  risk_status: string
  state: string
  blocks: string[]
  governor_g: number
  positions: PositionRow[]
  caps: CapRow[]
  margin: MarginPanel
  governor_history: GovernorRow[]
  kill_rules: KillRuleRow[]
}

// --- rebalances (GET /api/{strategy}/rebalances[/{rebalance_id}]) ----------

export interface RebalanceRow {
  rebalance_id: string
  day: string
  kind: string
  status: string
  started_ts: number
  ended_ts: number | null
  duration_s: number | null
  completion_pct: number
  traded_notional: number
  planned_notional: number
  fees: number
  avg_slippage_bps: number
  maker_ratio: number
  equity: number
  governor_g: number
  n_residuals: number
}

export interface RebalancesResponse {
  strategy: string
  as_of_ts: number
  rows: RebalanceRow[]
  avg_completion_pct: number | null
}

export interface TargetRow {
  symbol: string
  signal: number
  vol: number
  raw: number
  target_notional: number
  target_qty: number
  current_qty: number
  delta_notional: number
  funding_ann: number
  funding_haircut: number
  caps_applied: string[]
  traded: boolean
}

export interface SliceRow {
  slice_id: string
  symbol: string
  seq: number
  side: string
  qty: number
  reduce_only: boolean
  placed_ts: number
  ended_ts: number | null
  repegs: number
  outcome: string
  fill_qty: number
  avg_price: number
  taker: boolean
}

export interface FillRow {
  trade_id: string
  order_id: string
  symbol: string
  side: string
  qty: number
  price: number
  fee: number
  is_maker: boolean
  realized_pnl: number
  slippage_bps: number
  ts: number
}

export interface RebalanceDetail {
  strategy: string
  as_of_ts: number
  rebalance: RebalanceRow
  sizing: Record<string, number>
  order_plan: Record<string, unknown>[]
  residuals: Record<string, unknown>[]
  decision_mids: Record<string, number>
  cursor: number
  targets: TargetRow[]
  slices: SliceRow[]
  fills: FillRow[]
}

// --- attribution (GET /api/{strategy}/attribution) -------------------------

export interface Components {
  price_pnl: number
  funding: number
  fees: number
  slippage: number
  net_pnl: number
  traded_notional: number
}

export interface SymbolRow {
  symbol: string
  net_pnl: number
  contribution_share: number
}

export interface SideRow {
  side: string
  net_pnl: number
  contribution_share: number
}

export interface PeriodAttribution {
  period: string
  start_day: string
  end_day: string
  days: number
  total_net_pnl: number
  components: Components
  by_symbol: SymbolRow[]
  by_side: SideRow[]
  concentration: number | null
  n_obs: number
  below_min_active_days: boolean
}

export interface RegimeRow {
  bucket: string
  label: string
  months: number
  pnl: number
  hit_rate: number | null
  avg_exposure: number | null
}

export interface AttributionResponse {
  strategy: string
  as_of_ts: number
  periods: PeriodAttribution[]
  regime: RegimeRow[]
  monthly_net_pnl: Record<string, number>
}

// --- metrics (GET /api/{strategy}/metrics) ---------------------------------

export interface MetricRow {
  name: string
  period: string
  value: number | null
  n_obs: number
  std_error: number | null
  as_of_ts: number
  extra: Record<string, unknown>
}

export interface PeriodBlock {
  period: string
  as_of_ts: number | null
  active_days: number | null
  below_min_active_days: boolean
  metrics: MetricRow[]
}

export interface MetricsResponse {
  strategy: string
  as_of_ts: number
  min_active_days: number
  periods: PeriodBlock[]
}

// --- operations (GET /api/{strategy}/operations) ---------------------------

export interface HeartbeatPanel {
  enabled: boolean
  interval_s: number
  last_ts: number | null
  last_ok: boolean | null
  beats_24h: number
  uptime_24h_pct: number | null
  uptime_7d_pct: number | null
  uptime_30d_pct: number | null
}

export interface ReconciliationPanel {
  last_ts: number | null
  last_kind: string | null
  last_ok: boolean | null
  last_detail: string
  open_breaks: number
  breaks: Record<string, unknown>[]
}

export interface BackupPanel {
  enabled: boolean
  replica_path: string
  last_ok_ts: number | null
  lag_s: number | null
  restore_check_days: number
}

export interface InfraPanel {
  host: string
  monthly_cost_eur: number
  max_rss_mb: number
  rss_mb: number | null
  cpu_pct: number | null
  net_of_infra: number | null
}

export interface AlertRow {
  id: number
  ts: number
  severity: string
  code: string
  message: string
  acked: boolean
  delivered: boolean
  context: Record<string, unknown>
}

export interface ControlRow {
  id: number
  ts: number
  action: string
  operator: string
  reason: string
  payload: Record<string, unknown>
}

export interface ReportRow {
  kind: string
  period_key: string
  ts: number
  delivered: boolean
}

export interface OperationsResponse {
  strategy: string
  as_of_ts: number
  mode: string
  state: Record<string, unknown>
  heartbeat: HeartbeatPanel
  reconciliation: ReconciliationPanel
  backup: BackupPanel
  infra: InfraPanel
  alerts: AlertRow[]
  unacked_critical: number
  control_log: ControlRow[]
  reports: ReportRow[]
}

// --- backtest (GET /api/{strategy}/backtest) -------------------------------

export interface RunRow {
  run_id: string
  created_ts: number
  start_day: string
  end_day: string
  variant: string
  git_commit: string
  duration_s: number
}

export interface RunDetail {
  run_id: string
  created_ts: number
  start_day: string
  end_day: string
  variant: string
  git_commit: string
  duration_s: number
  metrics: Record<string, unknown>
  manifest: Record<string, unknown>
  equity: Record<string, unknown>[]
}

export interface RobustnessRow {
  variant: string
  net_pnl: number
  sharpe: number
  max_dd: number
  sign_ok: boolean
  detail: Record<string, unknown>
}

export interface WalkforwardRow {
  window: string
  param_set: string
  test_sharpe: number
  rank: number
  n_params: number
  default_in_top_half: boolean
  is_default: boolean
}

export interface TrackingRow {
  day: string
  live_pnl: number
  ref_pnl: number
  cum_live: number
  cum_ref: number
  corr_30d: number | null
  cum_diff_frac: number
  cost_ratio: number | null
  turnover_ratio: number | null
  in_bounds: boolean
  breach_days: number
}

export interface TrackingPanel {
  latest: TrackingRow | null
  series: TrackingRow[]
  min_corr: number
  max_cum_diff: number
  max_cost_ratio: number
  max_turnover_ratio: number
  breach_days_block: number
}

export interface BacktestResponse {
  strategy: string
  as_of_ts: number
  runs: RunRow[]
  run: RunDetail | null
  robustness: RobustnessRow[]
  walkforward: WalkforwardRow[]
  bootstrap: Record<string, number>
  bootstrap_horizon: string
  tracking: TrackingPanel
}

// --- universe (GET /api/{strategy}/universe) -------------------------------

export interface UniverseEntryRow {
  symbol: string
  rank: number
  median_quote_volume_30d: number
  history_days: number
  included: boolean
  reason: string
}

export interface MonthRow {
  month: string
  symbols: string[]
  entrants: string[]
  leavers: string[]
  excluded: UniverseEntryRow[]
  entries: UniverseEntryRow[]
}

export interface IlliquidRow {
  symbol: string
  flagged_ts: number
  until_ts: number
  reason: string
}

export interface UniverseResponse {
  strategy: string
  as_of_ts: number
  size: number
  current_month: string | null
  current_symbols: string[]
  months: MonthRow[]
  illiquid: IlliquidRow[]
}

// --- controls (GET/POST /api/{strategy}/controls) --------------------------

export interface ControlLogRow {
  id: number
  ts: number
  action: string
  operator: string
  reason: string
}

export interface ControlsResponse {
  strategy: string
  as_of_ts: number
  state: Record<string, unknown>
  actions: string[]
  confirm_required: string[]
  log: ControlLogRow[]
}

export interface ControlRequest {
  action: string
  operator: string
  reason?: string
  confirm?: string
}

export interface ControlAccepted {
  accepted: boolean
  strategy: string
  action: string
  operator: string
  reason: string
  ts: number
  detail: string
}

// --- shared error envelope -------------------------------------------------

export interface ValidationError {
  loc: (string | number)[]
  msg: string
  type: string
  input?: unknown
  ctx?: Record<string, unknown>
}

export interface HTTPValidationError {
  detail?: ValidationError[]
}

// --- helpers over the loosely typed `state` dicts ---------------------------
// `engine_state` is declared as a bare object in the spec (additionalProperties),
// so the shape is read defensively here rather than asserted.

export interface EngineStateView {
  state: string
  phase: string
  paused: boolean
  stopped: boolean
  safe_mode: boolean
  halt_reason: string
  governor_g: number | null
  blocks: string[]
  updated_ts: number | null
}

export function engineStateView(raw: Record<string, unknown> | null | undefined): EngineStateView {
  const r = raw ?? {}
  const str = (k: string): string => (typeof r[k] === 'string' ? (r[k] as string) : '')
  const bool = (k: string): boolean => r[k] === true
  const numOrNull = (k: string): number | null => (typeof r[k] === 'number' ? (r[k] as number) : null)
  return {
    state: str('state'),
    phase: str('phase'),
    paused: bool('paused'),
    stopped: bool('stopped'),
    safe_mode: bool('safe_mode'),
    halt_reason: str('halt_reason'),
    governor_g: numOrNull('governor_g'),
    blocks: Array.isArray(r.blocks) ? (r.blocks as unknown[]).map(String) : [],
    updated_ts: numOrNull('updated_ts'),
  }
}
