import { useState } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, type Strategy } from '../api'
import { Empty, Loader, Panel, useApi } from '../components/Common'
import { cls, num, pct, usd } from '../format'

export function Signals({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.signals(strategy, s), [strategy])
  const [symbol, setSymbol] = useState<string | null>(null)
  const history = useApi(
    (s) => (symbol ? api.signalHistory(strategy, symbol, 90, s) : Promise.resolve(null)),
    [strategy, symbol],
  )

  return (
    <Loader error={error} loading={loading}>
      {!data || data.signals.length === 0 ? (
        <Empty what="signals" />
      ) : (
        <div className="grid">
          <Panel title={`Signals — ${data.day ?? 'no bar yet'}`}>
            <div className="scroll">
              <table>
                <thead>
                  <tr>
                    <th>Symbol</th><th>Signal</th><th>u₁ (8/24)</th><th>u₂ (16/48)</th><th>u₃ (32/96)</th>
                    <th>Vol</th><th>Target</th><th>Current</th><th>Funding</th><th>Haircut</th>
                  </tr>
                </thead>
                <tbody>
                  {data.signals.map((r) => (
                    <tr
                      key={r.symbol}
                      onClick={() => setSymbol(r.symbol)}
                      style={{ cursor: 'pointer' }}
                      className={r.warm ? '' : 'stale'}
                    >
                      <td>{r.symbol}{!r.warm && <span className="muted"> (warming)</span>}</td>
                      <td className={cls(r.signal)}>{num(r.signal, 3)}</td>
                      <td>{num(r.u[0], 3)}</td>
                      <td>{num(r.u[1], 3)}</td>
                      <td>{num(r.u[2], 3)}</td>
                      <td>{pct(r.vol)}</td>
                      <td className={cls(r.target_notional)}>{usd(r.target_notional, 0)}</td>
                      <td className={cls(r.current_notional)}>{usd(r.current_notional, 0)}</td>
                      <td className={cls(r.funding_ann === null ? null : -r.funding_ann)}>{pct(r.funding_ann)}</td>
                      <td>{r.funding_haircut === 0.5 ? <span className="pill amber">0.5×</span> : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>

          {symbol && (
            <Panel title={`${symbol} — 90-day signal history`}>
              <Loader error={history.error} loading={history.loading}>
                {!history.data || history.data.points.length === 0 ? (
                  <Empty what="history" />
                ) : (
                  <ResponsiveContainer width="100%" height={230}>
                    <LineChart data={history.data.points} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke="#2a323d" strokeDasharray="3 3" />
                      <XAxis dataKey="day" stroke="#8b97a6" fontSize={11} minTickGap={40} />
                      <YAxis stroke="#8b97a6" fontSize={11} width={50} domain={[-1.05, 1.05]} />
                      <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #2a323d', borderRadius: 8 }} />
                      <Line type="monotone" dataKey="signal" stroke="#58a6ff" dot={false} strokeWidth={2} />
                      <Line type="monotone" dataKey="u1" stroke="#3fb950" dot={false} strokeWidth={1} />
                      <Line type="monotone" dataKey="u2" stroke="#d29922" dot={false} strokeWidth={1} />
                      <Line type="monotone" dataKey="u3" stroke="#f85149" dot={false} strokeWidth={1} />
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
