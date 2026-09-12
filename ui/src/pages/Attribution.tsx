import { useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, type Strategy } from '../api'
import { Empty, Loader, Panel, useApi } from '../components/Common'
import { cls, pct, usd } from '../format'

const PERIODS = ['7d', '30d', '90d', 'mtd', 'ytd', 'since_inception']

export function Attribution({ strategy }: { strategy: Strategy }) {
  const [period, setPeriod] = useState('30d')
  const { data, error, loading } = useApi((s) => api.attribution(strategy, period, s), [strategy, period])

  return (
    <div className="grid">
      <div className="topbar">
        <label className="muted">Period</label>
        <select value={period} onChange={(e) => setPeriod(e.target.value)}>
          {PERIODS.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      </div>
      <Loader error={error} loading={loading}>
        {!data ? <Empty what="attribution" /> : (
          <div className="grid">
            <Panel title={`Per-symbol contribution — concentration ${pct(data.concentration)}`}>
              {data.by_symbol.length === 0 ? <Empty what="symbol P&L" /> : (
                <>
                  <ResponsiveContainer width="100%" height={240}>
                    <BarChart data={data.by_symbol} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke="#2a323d" strokeDasharray="3 3" />
                      <XAxis dataKey="symbol" stroke="#8b97a6" fontSize={10} interval={0} angle={-35} textAnchor="end" height={60} />
                      <YAxis stroke="#8b97a6" fontSize={11} width={62} />
                      <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #2a323d', borderRadius: 8 }} />
                      <Bar dataKey="net_pnl">
                        {data.by_symbol.map((d) => (
                          <Cell key={d.symbol} fill={d.net_pnl >= 0 ? '#3fb950' : '#f85149'} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                  <div className="scroll" style={{ maxHeight: 240 }}>
                    <table>
                      <thead><tr><th>Symbol</th><th>Net P&L</th><th>Share</th></tr></thead>
                      <tbody>
                        {data.by_symbol.map((r) => (
                          <tr key={r.symbol}>
                            <td>{r.symbol}</td>
                            <td className={cls(r.net_pnl)}>{usd(r.net_pnl)}</td>
                            <td className={r.share > 0.5 ? 'down' : ''}>{pct(r.share)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </Panel>

            <div className="grid cols-2">
              <Panel title="By side">
                <table>
                  <thead><tr><th>Side</th><th>Net P&L</th><th>Hit rate</th></tr></thead>
                  <tbody>
                    {data.by_side.map((r) => (
                      <tr key={r.side}>
                        <td>{r.side}</td>
                        <td className={cls(r.net_pnl)}>{usd(r.net_pnl)}</td>
                        <td>{pct(r.hit_rate)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Panel>

              <Panel title="Regime table (by BTC monthly return)">
                <table>
                  <thead><tr><th>Bucket</th><th>P&L</th><th>Hit rate</th><th>Avg exposure</th></tr></thead>
                  <tbody>
                    {data.regime.map((r) => (
                      <tr key={r.bucket}>
                        <td>{r.bucket}</td>
                        <td className={cls(r.pnl)}>{usd(r.pnl)}</td>
                        <td>{pct(r.hit_rate)}</td>
                        <td>{r.avg_exposure === null ? 'n/a' : `${r.avg_exposure.toFixed(2)}×`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Panel>
            </div>
          </div>
        )}
      </Loader>
    </div>
  )
}
