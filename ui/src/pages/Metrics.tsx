import { useState } from 'react'
import { api, type Strategy } from '../api'
import { Empty, Loader, Panel, useApi } from '../components/Common'
import { isStale, num } from '../format'

const PERIODS = ['7d', '30d', '90d', 'mtd', 'ytd', 'since_inception']

export function Metrics({ strategy }: { strategy: Strategy }) {
  const [period, setPeriod] = useState('30d')
  const { data, error, loading } = useApi((s) => api.metrics(strategy, period, s), [strategy, period])

  return (
    <div className="grid">
      <div className="topbar">
        <label className="muted">Period</label>
        <select value={period} onChange={(e) => setPeriod(e.target.value)}>
          {PERIODS.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      </div>
      <Loader error={error} loading={loading}>
        {!data || data.metrics.length === 0 ? <Empty what="metrics" /> : (
          <Panel title="Metrics">
            {/* PRD 10.5: below 20 active days a metric is shown greyed, with n_obs — never hidden. */}
            <div className="scroll" style={{ maxHeight: 640 }}>
              <table>
                <thead><tr><th>Metric</th><th>Value</th><th>± SE</th><th>n</th></tr></thead>
                <tbody>
                  {data.metrics.map((m) => (
                    <tr key={m.name} className={isStale(m.n_obs) ? 'stale' : ''}>
                      <td>{m.name.replace(/_/g, ' ')}</td>
                      <td>{num(m.value, 4)}</td>
                      <td className="muted">{m.std_error === null ? '' : num(m.std_error, 4)}</td>
                      <td className="muted">{m.n_obs}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        )}
      </Loader>
    </div>
  )
}
