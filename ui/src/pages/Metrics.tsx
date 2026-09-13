import { useState } from 'react'
import { api, type Strategy } from '../api'
import { Empty, Loader, Panel, useApi } from '../components/Common'
import { isStale, num, ts } from '../format'

export function Metrics({ strategy }: { strategy: Strategy }) {
  // The whole set is fetched once; the selector filters what the payload holds,
  // so the period list is the engine's and never a hard-coded guess.
  const { data, error, loading } = useApi((s) => api.metrics(strategy, undefined, s), [strategy])
  const [period, setPeriod] = useState<string | null>(null)

  const periods = data?.periods ?? []
  const block = periods.find((p) => p.period === period) ?? periods[0] ?? null

  return (
    <div className="grid">
      <div className="topbar">
        <label className="muted">Period</label>
        <select value={block?.period ?? ''} onChange={(e) => setPeriod(e.target.value)} disabled={periods.length === 0}>
          {periods.length === 0 && <option value="">n/a</option>}
          {periods.map((p) => <option key={p.period} value={p.period}>{p.period}</option>)}
        </select>
        {data && <span className="muted">minimum {data.min_active_days} active days</span>}
      </div>
      <Loader error={error} loading={loading}>
        {!data || !block ? <Empty what="metrics" /> : (
          <Panel
            title={`Metrics — ${block.period}`}
            right={
              <span className={block.below_min_active_days ? 'stale' : ''}>
                {block.active_days === null ? 'n/a' : `${block.active_days} active days`} · as of{' '}
                {block.as_of_ts === null ? 'n/a' : ts(block.as_of_ts)}
                {block.below_min_active_days && (
                  <span className="pill amber" style={{ marginLeft: 6 }}>below {data.min_active_days} active days</span>
                )}
              </span>
            }
          >
            {/* PRD 10.5: a thin metric is greyed, with n_obs shown — never hidden. */}
            {block.metrics.length === 0 ? <Empty what="metrics for this period" /> : (
              <div className="scroll" style={{ maxHeight: 640 }}>
                <table>
                  <thead><tr><th>Metric</th><th>Value</th><th>± SE</th><th>n</th><th>As of</th></tr></thead>
                  <tbody>
                    {block.metrics.map((m) => (
                      <tr
                        key={`${m.period}:${m.name}`}
                        className={block.below_min_active_days || isStale(m.n_obs, data.min_active_days) ? 'stale' : ''}
                      >
                        <td>{m.name.replace(/_/g, ' ')}</td>
                        <td>{num(m.value, 4)}</td>
                        <td className="muted">{m.std_error === null ? 'n/a' : num(m.std_error, 4)}</td>
                        <td className="muted">{m.n_obs}</td>
                        <td className="muted">{ts(m.as_of_ts)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        )}
      </Loader>
    </div>
  )
}
