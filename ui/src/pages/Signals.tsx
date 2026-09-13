import { useState } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, type Strategy } from '../api'
import { AXIS, COLORS, Empty, GRID, Loader, Panel, TIP, useApi } from '../components/Common'
import { cls, num, pct, usd } from '../format'

const HISTORY_DAYS = 90

export function Signals({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.signals(strategy, s), [strategy])
  const [symbol, setSymbol] = useState<string | null>(null)
  const history = useApi(
    (s) => (symbol ? api.signalHistory(strategy, symbol, HISTORY_DAYS, s) : Promise.resolve(null)),
    [strategy, symbol],
  )

  return (
    <Loader error={error} loading={loading}>
      {!data || data.rows.length === 0 ? (
        <Empty what="signals" />
      ) : (
        <div className="grid">
          <Panel
            title={`Signals — ${data.day ?? 'no bar yet'}`}
            right={<span className="muted">rebalance {data.rebalance_id ?? 'n/a'}</span>}
          >
            <div className="scroll">
              <table>
                <thead>
                  <tr>
                    <th>Symbol</th><th>Signal</th><th>u₁ (8/24)</th><th>u₂ (16/48)</th><th>u₃ (32/96)</th>
                    <th>Vol</th><th>Target</th><th>Current</th><th>Δ</th><th>Funding</th><th>Haircut</th><th>Flags</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((r) => (
                    <tr
                      key={r.symbol}
                      onClick={() => setSymbol(r.symbol)}
                      style={{ cursor: 'pointer' }}
                      className={r.warm ? '' : 'stale'}
                    >
                      <td>{r.symbol}{!r.warm && <span className="muted"> (warming)</span>}</td>
                      <td className={cls(r.signal)}>{num(r.signal, 3)}</td>
                      <td>{num(r.u1, 3)}</td>
                      <td>{num(r.u2, 3)}</td>
                      <td>{num(r.u3, 3)}</td>
                      <td>{pct(r.vol)}</td>
                      <td className={cls(r.target_notional)}>{usd(r.target_notional, 0)}</td>
                      <td className={cls(r.current_notional)}>{usd(r.current_notional, 0)}</td>
                      <td className={cls(r.delta_notional)}>{usd(r.delta_notional, 0)}</td>
                      <td className={cls(-r.funding_ann)}>{pct(r.funding_ann)}</td>
                      <td>
                        {r.haircut_applied
                          ? <span className="pill amber">{num(r.funding_haircut, 2)}×</span>
                          : <span className="muted">{num(r.funding_haircut, 2)}×</span>}
                      </td>
                      <td>
                        {!r.in_universe && <span className="pill" style={{ marginRight: 4 }}>out</span>}
                        {r.illiquid && <span className="pill red">illiquid</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>

          {symbol && (
            <Panel
              title={`${symbol} — ${HISTORY_DAYS}-day signal history`}
              right={<button onClick={() => setSymbol(null)}>close</button>}
            >
              <Loader error={history.error} loading={history.loading}>
                {!history.data || history.data.points.length === 0 ? (
                  <Empty what="history" />
                ) : (
                  <ResponsiveContainer width="100%" height={230}>
                    <LineChart data={history.data.points} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                      <CartesianGrid {...GRID} strokeDasharray="3 3" />
                      <XAxis dataKey="day" {...AXIS} minTickGap={40} />
                      <YAxis {...AXIS} width={50} domain={[-1.05, 1.05]} />
                      <Tooltip contentStyle={TIP} />
                      <Line type="monotone" dataKey="signal" stroke={COLORS.accent} dot={false} strokeWidth={2} connectNulls />
                      <Line type="monotone" dataKey="u1" stroke={COLORS.up} dot={false} strokeWidth={1} connectNulls />
                      <Line type="monotone" dataKey="u2" stroke={COLORS.warn} dot={false} strokeWidth={1} connectNulls />
                      <Line type="monotone" dataKey="u3" stroke={COLORS.down} dot={false} strokeWidth={1} connectNulls />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </Loader>
            </Panel>
          )}
        </div>
      )}
    </Loader>
  )
}
