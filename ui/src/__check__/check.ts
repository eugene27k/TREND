import type {
  AttributionResponse, BacktestResponse, CombinedOverviewResponse, ControlsResponse,
  HealthResponse, MetricsResponse, OperationsResponse, OverviewResponse, PositionsResponse,
  RebalanceDetail, RebalancesResponse, SignalHistoryResponse, SignalsResponse,
  StrategiesResponse, UniverseResponse,
} from '../api'

import eAttribution from './empty_attribution.json'
import eBacktest from './empty_backtest.json'
import eCombined from './empty_combined_overview.json'
import eControls from './empty_controls.json'
import eHealth from './empty_health.json'
import eMetrics from './empty_metrics.json'
import eOperations from './empty_operations.json'
import eOverview from './empty_overview.json'
import ePositions from './empty_positions.json'
import eRebalances from './empty_rebalances.json'
import eSignalHistory from './empty_signal_history.json'
import eSignals from './empty_signals.json'
import eStrategies from './empty_strategies.json'
import eUniverse from './empty_universe.json'
import sAttribution from './seeded_attribution.json'
import sBacktest from './seeded_backtest.json'
import sCombined from './seeded_combined_overview.json'
import sControls from './seeded_controls.json'
import sMetrics from './seeded_metrics.json'
import sOperations from './seeded_operations.json'
import sOverview from './seeded_overview.json'
import sPositions from './seeded_positions.json'
import sRebalanceDetail from './seeded_rebalance_detail.json'
import sRebalances from './seeded_rebalances.json'
import sSignalHistory from './seeded_signal_history.json'
import sSignals from './seeded_signals.json'
import sUniverse from './seeded_universe.json'

export const checks: unknown[] = [
  eHealth as HealthResponse,
  eStrategies as StrategiesResponse,
  eCombined as CombinedOverviewResponse,
  eOverview as OverviewResponse,
  eSignals as SignalsResponse,
  eSignalHistory as SignalHistoryResponse,
  ePositions as PositionsResponse,
  eRebalances as RebalancesResponse,
  eAttribution as AttributionResponse,
  eMetrics as MetricsResponse,
  eOperations as OperationsResponse,
  eBacktest as BacktestResponse,
  eUniverse as UniverseResponse,
  eControls as ControlsResponse,
  sCombined as CombinedOverviewResponse,
  sOverview as OverviewResponse,
  sSignals as SignalsResponse,
  sSignalHistory as SignalHistoryResponse,
  sPositions as PositionsResponse,
  sRebalances as RebalancesResponse,
  sRebalanceDetail as RebalanceDetail,
  sAttribution as AttributionResponse,
  sMetrics as MetricsResponse,
  sOperations as OperationsResponse,
  sBacktest as BacktestResponse,
  sUniverse as UniverseResponse,
  sControls as ControlsResponse,
]
