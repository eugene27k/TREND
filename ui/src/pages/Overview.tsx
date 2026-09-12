import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, type OverviewResponse, type Strategy } from '../api'
import { AXIS, COLORS, Empty, GRID, Loader, Panel, StatusPill, TIP, Tile, useApi } from '../components/Common'
import { cls, mult, num, pct, pctPoints, signed, usd } from '../format'

export function Overview({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.overview(strategy, s), [strategy])

  return (
    <Loader error={error} loading={loading}>
      {!data ? (
        <Empty what="data" />
      ) : (
        <OverviewBody data={data} />
      )}
    </Loader>
  )
}

function OverviewBody({ data }: { data: OverviewResponse }) {
  const k = data.kpis
  const comp = data.cumulative_components
  // The API sends cumulative components as one roll-up, not a day series.
  const componentBars = [
    { name: 'price', value: comp.price_pnl },
    { name: 'funding', value: comp.funding },
    { name: 'fees', value: comp.fees },
    { name: 'slippage', value: comp.slippage },
  ]

  return (
    <div className="grid" style={{ gap: 14 }}>
      <div className="grid tiles">
        <Tile label="Equity" value={usd(k.equity)} sub="USDT" />
        <Tile label="Net since start" value={signed(k.since_inception_net)} tone={cls(k.since_inception_net)} />
        <Tile label="Net 30d" value={signed(k.net_30d)} tone={cls(k.net_30d)} />
        <Tile
          label="Realised vol"
          value={pct(k.realised_vol)}
          sub={`target ${pct(k.vol_target)} · ratio ${num(k.vol_ratio, 2)}`}
        />
        <Tile label="Max drawdown" value={pct(k.max_drawdown)} tone="down" />
        <Tile label="Current drawdown" value={pct(k.current_drawdown)} tone="down" />
        <Tile label="Governor g" value={num(k.governor_g, 2)} />
        {/* PRD 10.5: below min_active_days the Sharpe is greyed with its n, never hidden. */}
        <Tile
          label="Sharpe"
          value={num(k.sharpe, 2)}
          stale={k.below_min_active_days}
          sub={`${k.sharpe_std_error === null ? 'n/a' : `± ${num(k.sharpe_std_error, 2)}`} SE · n=${k.sharpe_n_obs}`}
        />
        <Tile label="Cash alternative" value={signed(k.cash_alternative)} sub="same capital at rf" />
        <Tile label="Gross exposure" value={mult(k.gross_x)} sub={`${usd(k.gross_notional, 0)} USDT`} />
        <Tile label="Net exposure" value={mult(k.net_x)} sub={`${usd(k.net_notional, 0)} USDT`} />
        <Tile
          label="Phase"
          value={k.phase}
          sub={
            <>
              <StatusPill status={k.risk_status} /> <span className="muted">{k.state}</span>
              {k.paused && <span className="pill amber" style={{ marginLeft: 6 }}>paused</span>}
            </>
          }
        />
      </div>

      {k.blocks.length > 0 && (
        <Panel title="Active blocks">
          {k.blocks.map((b) => (
            <span key={b} className="pill red" style={{ marginRight: 6 }}>{b}</span>
          ))}
        </Panel>
      )}

      <Panel
        title="Equity vs cash alternative vs backtest reference"
        right={<span className="muted">reference run {data.reference_run_id ?? 'n/a'}</span>}
      >
        {data.curve.length === 0 ? (
          <Empty what="equity history" />
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={data.curve} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid {...GRID} strokeDasharray="3 3" />
              <XAxis dataKey="day" {...AXIS} minTickGap={40} />
              <YAxis {...AXIS} width={70} domain={['auto', 'auto']} />
              <Tooltip contentStyle={TIP} />
              <Line type="monotone" dataKey="equity" stroke={COLORS.accent} dot={false} strokeWidth={2} name="equity" />
              <Line
                type="monotone"
                dataKey="cash_alternative"
                stroke={COLORS.muted}
                dot={false}
                strokeDasharray="4 4"
                name="cash"
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="backtest_reference"
                stroke={COLORS.warn}
                dot={false}
                strokeDasharray="2 3"
                name="reference"
                connectNulls
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </Panel>

      <div className="grid cols-2">
        <Panel title="Cumulative P&L by component">
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={componentBars} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid {...GRID} strokeDasharray="3 3" />
              <XAxis dataKey="name" {...AXIS} />
              <YAxis {...AXIS} width={70} />
              <Tooltip contentStyle={TIP} />
              <Bar dataKey="value">
                {componentBars.map((d) => (
                  <Cell key={d.name} fill={d.value >= 0 ? COLORS.up : COLORS.down} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
          <table>
            <thead>
              <tr><th>Component</th><th>Cumulative</th></tr>
            </thead>
            <tbody>
              <tr><td>price</td><td className={cls(comp.price_pnl)}>{signed(comp.price_pnl)}</td></tr>
              <tr><td>funding</td><td className={cls(comp.funding)}>{signed(comp.funding)}</td></tr>
              <tr><td>fees</td><td className={cls(comp.fees)}>{signed(comp.fees)}</td></tr>
              <tr><td>slippage</td><td className={cls(comp.slippage)}>{signed(comp.slippage)}</td></tr>
              <tr><td>net</td><td className={cls(comp.net_pnl)}>{signed(comp.net_pnl)}</td></tr>
              <tr><td>traded notional</td><td>{usd(comp.traded_notional, 0)}</td></tr>
            </tbody>
          </table>
        </Panel>

        <Panel title="Cumulative P&L by side">
          {data.by_side.length === 0 ? (
            <Empty what="side attribution" />
          ) : (
            <>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={data.by_side} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid {...GRID} strokeDasharray="3 3" />
                  <XAxis dataKey="side" {...AXIS} />
                  <YAxis {...AXIS} width={70} />
                  <Tooltip contentStyle={TIP} />
                  <Bar dataKey="net_pnl">
                    {data.by_side.map((d) => (
                      <Cell key={d.side} fill={d.net_pnl >= 0 ? COLORS.up : COLORS.down} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
              <table>
                <thead><tr><th>Side</th><th>Net P&L</th></tr></thead>
                <tbody>
                  {data.by_side.map((r) => (
                    <tr key={r.side}>
                      <td>{r.side}</td>
                      <td className={cls(r.net_pnl)}>{signed(r.net_pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </Panel>
      </div>

      <Panel title="Drawdown">
        {data.curve.length === 0 ? (
          <Empty what="equity history" />
        ) : (
          <ResponsiveContainer width="100%" height={140}>
            <AreaChart data={data.curve} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid {...GRID} strokeDasharray="3 3" />
              <XAxis dataKey="day" {...AXIS} minTickGap={40} />
              <YAxis {...AXIS} width={62} tickFormatter={(v: number) => `${(v * 100).toFixed(0)}%`} />
              <Tooltip contentStyle={TIP} formatter={(v: number) => pctPoints(v * 100)} />
              <Area type="monotone" dataKey="drawdown" stroke={COLORS.down} fill="#f8514933" />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </Panel>
    </div>
  )
}
