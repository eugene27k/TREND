import { Area, AreaChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, type Strategy } from '../api'
import { Empty, Loader, Panel, StatusPill, Tile, useApi } from '../components/Common'
import { cls, mult, num, pct, pctPoints, signed, usd } from '../format'

const AXIS = { stroke: '#8b97a6', fontSize: 11 }
const GRID = { stroke: '#2a323d' }
const TIP = { background: '#161b22', border: '1px solid #2a323d', borderRadius: 8, fontSize: 12 }

export function Overview({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.overview(strategy, s), [strategy])

  return (
    <Loader error={error} loading={loading}>
      {!data ? (
        <Empty what="data" />
      ) : (
        <div className="grid" style={{ gap: 14 }}>
          <div className="grid tiles">
            <Tile label="Equity" value={usd(data.kpi.equity)} sub="USDT" />
            <Tile
              label="Net since start"
              value={signed(data.kpi.net_since_inception)}
              tone={cls(data.kpi.net_since_inception)}
            />
            <Tile label="Net 30d" value={signed(data.kpi.net_30d)} tone={cls(data.kpi.net_30d)} />
            <Tile
              label="Realised vol"
              value={pct(data.kpi.realised_vol)}
              sub={`target ${pct(data.kpi.vol_target)}`}
            />
            <Tile label="Max drawdown" value={pct(data.kpi.max_drawdown)} tone="down" />
            <Tile label="Current drawdown" value={pct(data.kpi.current_drawdown)} tone="down" />
            <Tile label="Governor g" value={num(data.kpi.governor_g, 2)} />
            <Tile
              label="Sharpe"
              value={num(data.kpi.sharpe, 2)}
              sub={data.kpi.sharpe_se !== null ? `± ${num(data.kpi.sharpe_se, 2)} SE` : 'n/a'}
            />
            <Tile label="Gross exposure" value={mult(data.kpi.gross_exposure)} />
            <Tile label="Net exposure" value={mult(data.kpi.net_exposure)} />
            <Tile label="Book" value={`${data.kpi.n_long}L / ${data.kpi.n_short}S`} />
            <Tile label="Phase" value={data.kpi.phase} sub={<StatusPill status={data.kpi.risk_status} />} />
          </div>

          <Panel title="Equity vs cash alternative vs backtest reference">
            {data.equity.length === 0 ? (
              <Empty what="equity history" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <LineChart data={data.equity} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid {...GRID} strokeDasharray="3 3" />
                  <XAxis dataKey="day" {...AXIS} minTickGap={40} />
                  <YAxis {...AXIS} width={70} domain={['auto', 'auto']} />
                  <Tooltip contentStyle={TIP} />
                  <Line type="monotone" dataKey="equity" stroke="#58a6ff" dot={false} strokeWidth={2} name="equity" />
                  <Line type="monotone" dataKey="cash_alternative" stroke="#8b97a6" dot={false} strokeDasharray="4 4" name="cash" />
                  <Line type="monotone" dataKey="backtest_reference" stroke="#d29922" dot={false} strokeDasharray="2 3" name="reference" />
                </LineChart>
              </ResponsiveContainer>
            )}
          </Panel>

          <div className="grid cols-2">
            <Panel title="Cumulative P&L by component">
              {data.components.length === 0 ? (
                <Empty what="attribution" />
              ) : (
                <ResponsiveContainer width="100%" height={230}>
                  <AreaChart data={data.components} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                    <CartesianGrid {...GRID} strokeDasharray="3 3" />
                    <XAxis dataKey="day" {...AXIS} minTickGap={40} />
                    <YAxis {...AXIS} width={62} />
                    <Tooltip contentStyle={TIP} />
                    <Area type="monotone" dataKey="price" stackId="1" stroke="#58a6ff" fill="#58a6ff33" />
                    <Area type="monotone" dataKey="funding" stackId="1" stroke="#3fb950" fill="#3fb95033" />
                    <Area type="monotone" dataKey="fees" stackId="1" stroke="#f85149" fill="#f8514933" />
                    <Area type="monotone" dataKey="slippage" stackId="1" stroke="#d29922" fill="#d2992233" />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </Panel>

            <Panel title="Cumulative P&L by side">
              {data.components.length === 0 ? (
                <Empty what="attribution" />
              ) : (
                <ResponsiveContainer width="100%" height={230}>
                  <LineChart data={data.components} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                    <CartesianGrid {...GRID} strokeDasharray="3 3" />
                    <XAxis dataKey="day" {...AXIS} minTickGap={40} />
                    <YAxis {...AXIS} width={62} />
                    <Tooltip contentStyle={TIP} />
                    <Line type="monotone" dataKey="long" stroke="#3fb950" dot={false} />
                    <Line type="monotone" dataKey="short" stroke="#f85149" dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </Panel>
          </div>

          <Panel title="Drawdown">
            <ResponsiveContainer width="100%" height={140}>
              <AreaChart data={data.equity} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid {...GRID} strokeDasharray="3 3" />
                <XAxis dataKey="day" {...AXIS} minTickGap={40} />
                <YAxis {...AXIS} width={62} tickFormatter={(v: number) => `${(v * 100).toFixed(0)}%`} />
                <Tooltip contentStyle={TIP} formatter={(v: number) => pctPoints(v * 100)} />
                <Area type="monotone" dataKey="drawdown" stroke="#f85149" fill="#f8514933" />
              </AreaChart>
            </ResponsiveContainer>
          </Panel>
        </div>
      )}
    </Loader>
  )
}
