// Thin typed client over the read-only FastAPI service. Every page loads from
// stored data (US-T18 AC 8: < 1 s on the free VM), so there is no client-side
// computation of anything the metric engine already persisted.

export type Strategy = 'TREND' | 'CARRY'

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

export const api = {
  strategies: (sig?: AbortSignal) => get<{ strategies: Strategy[] }>('/strategies', sig),
  allSleeves: (sig?: AbortSignal) => get<AllSleeves>('/overview', sig),
  overview: (s: Strategy, sig?: AbortSignal) => get<Overview>(`/${s}/overview`, sig),
  signals: (s: Strategy, sig?: AbortSignal) => get<SignalsPage>(`/${s}/signals`, sig),
  signalHistory: (s: Strategy, symbol: string, days = 90, sig?: AbortSignal) =>
    get<{ symbol: string; points: SignalPoint[] }>(`/${s}/signals/${symbol}/history?days=${days}`, sig),
  positions: (s: Strategy, sig?: AbortSignal) => get<PositionsPage>(`/${s}/positions`, sig),
  rebalances: (s: Strategy, sig?: AbortSignal) => get<{ rebalances: RebalanceRow[] }>(`/${s}/rebalances`, sig),
  rebalance: (s: Strategy, id: string, sig?: AbortSignal) => get<RebalanceDetail>(`/${s}/rebalances/${id}`, sig),
  attribution: (s: Strategy, period = '30d', sig?: AbortSignal) =>
    get<Attribution>(`/${s}/attribution?period=${period}`, sig),
  metrics: (s: Strategy, period = '30d', sig?: AbortSignal) =>
    get<{ metrics: MetricRow[] }>(`/${s}/metrics?period=${period}`, sig),
  operations: (s: Strategy, sig?: AbortSignal) => get<Operations>(`/${s}/operations`, sig),
  backtest: (s: Strategy, sig?: AbortSignal) => get<BacktestPage>(`/${s}/backtest`, sig),
  universe: (s: Strategy, sig?: AbortSignal) => get<UniversePage>(`/${s}/universe`, sig),
  control: async (s: Strategy, body: ControlRequest) => {
    const res = await fetch(`/api/${s}/controls`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new ApiError(res.status, await res.text().catch(() => res.statusText))
    return (await res.json()) as { accepted: boolean; action: string }
  },
}

// --- response shapes (mirror the pydantic models in aegis/api/routes) -------

export interface Kpi {
  equity: number | null
  net_since_inception: number | null
  net_30d: number | null
  realised_vol: number | null
  vol_target: number | null
  max_drawdown: number | null
  current_drawdown: number | null
  governor_g: number | null
  sharpe: number | null
  sharpe_se: number | null
  gross_exposure: number | null
  net_exposure: number | null
  phase: string
  risk_status: string
  n_long: number
  n_short: number
}

export interface EquityPoint {
  day: string
  equity: number | null
  cash_alternative: number | null
  backtest_reference: number | null
  drawdown: number | null
}

export interface ComponentPoint {
  day: string
  price: number
  funding: number
  fees: number
  slippage: number
  long: number
  short: number
}

export interface Overview {
  kpi: Kpi
  equity: EquityPoint[]
  components: ComponentPoint[]
}

export interface AllSleeves {
  sleeves: { strategy: Strategy; equity: number | null; drawdown: number | null; status: string; phase: string }[]
}

export interface SignalRow {
  symbol: string
  signal: number
  u: (number | null)[]
  vol: number | null
  target_notional: number | null
  current_notional: number | null
  funding_ann: number | null
  funding_haircut: number | null
  warm: boolean
}

export interface SignalsPage {
  day: string | null
  signals: SignalRow[]
}

export interface SignalPoint {
  day: string
  signal: number
  u1: number | null
  u2: number | null
  u3: number | null
}

export interface PositionRow {
  symbol: string
  qty: number
  notional: number
  side: string
  entry_price: number
  mark_price: number
  unrealized_pnl: number
  funding_accrued: number | null
  adl_quantile: number
}

export interface CapUtilisation {
  gross: number
  gross_cap: number
  net: number
  net_cap: number
  single: number
  single_cap: number
  largest_symbol: string
}

export interface KillRuleRow {
  rule: string
  active: boolean
  detail: string
}

export interface PositionsPage {
  positions: PositionRow[]
  caps: CapUtilisation
  margin_ratio: number | null
  survivable_move: number | null
  governor: { ts: number; dd: number; g_before: number; g_after: number; trigger: string }[]
  kill_rules: KillRuleRow[]
  blocks: string[]
}

export interface RebalanceRow {
  rebalance_id: string
  day: string
  status: string
  kind: string
  completion_pct: number
  traded_notional: number
  fees: number
  avg_slippage_bps: number
  maker_ratio: number
  duration_s: number | null
  residuals: number
}

export interface RebalanceDetail {
  rebalance: RebalanceRow
  slices: Record<string, unknown>[]
  fills: Record<string, unknown>[]
  targets: Record<string, unknown>[]
}

export interface Attribution {
  period: string
  by_symbol: { symbol: string; net_pnl: number; share: number }[]
  by_side: { side: string; net_pnl: number; hit_rate: number | null }[]
  regime: { bucket: string; pnl: number; hit_rate: number | null; avg_exposure: number | null }[]
  concentration: number | null
}

export interface MetricRow {
  name: string
  period: string
  value: number | null
  n_obs: number
  std_error: number | null
}

export interface Operations {
  heartbeat_uptime_pct: number | null
  reconciliation_ok: boolean
  reconciliation_detail: string
  backup_lag_s: number | null
  alerts: { ts: number; severity: string; code: string; message: string }[]
  controls: { ts: number; action: string; operator: string; reason: string }[]
  infra_monthly_cost_eur: number
  rss_mb: number | null
  cpu_pct: number | null
}

export interface BacktestPage {
  runs: { run_id: string; created_ts: number; start_day: string; end_day: string; variant: string; git_commit: string; duration_s: number }[]
  metrics: Record<string, number> | null
  robustness: { variant: string; net_pnl: number; sharpe: number; max_dd: number; sign_ok: boolean }[]
  walkforward: { window: string; param_set: string; test_sharpe: number; rank: number; n_params: number; is_default: boolean }[]
  bootstrap: Record<string, number>
  tracking: { day: string; corr_30d: number | null; cum_diff_frac: number; cost_ratio: number | null; turnover_ratio: number | null; in_bounds: boolean } | null
}

export interface UniversePage {
  month: string | null
  entries: { symbol: string; rank: number; median_quote_volume_30d: number; history_days: number; included: boolean; reason: string }[]
  entrants: string[]
  leavers: string[]
  illiquid: { symbol: string; reason: string; until_ts: number }[]
}

export interface ControlRequest {
  action: 'start' | 'pause' | 'resume' | 'stop' | 'flatten_all' | 'clear_halt'
  operator: string
  reason: string
  confirm?: string
}
