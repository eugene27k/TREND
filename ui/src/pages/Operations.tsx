import { api, type Strategy } from '../api'
import { Empty, Loader, Panel, Tile, useApi } from '../components/Common'
import { num, pctPoints, ts } from '../format'

export function Operations({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.operations(strategy, s), [strategy])

  return (
    <Loader error={error} loading={loading}>
      {!data ? <Empty what="operations data" /> : (
        <div className="grid">
          <div className="grid tiles">
            <Tile label="Heartbeat uptime" value={pctPoints(data.heartbeat_uptime_pct)}
                  tone={(data.heartbeat_uptime_pct ?? 0) >= 99.5 ? 'up' : 'down'} />
            <Tile label="Reconciliation" value={data.reconciliation_ok ? 'OK' : 'BREAK'}
                  tone={data.reconciliation_ok ? 'up' : 'down'} sub={data.reconciliation_detail} />
            <Tile label="Backup lag" value={data.backup_lag_s === null ? 'n/a' : `${num(data.backup_lag_s, 0)} s`} />
            <Tile label="Infra cost" value={`€${num(data.infra_monthly_cost_eur, 2)}`} sub="per month" />
            <Tile label="RSS" value={data.rss_mb === null ? 'n/a' : `${num(data.rss_mb, 0)} MB`} />
            <Tile label="CPU" value={data.cpu_pct === null ? 'n/a' : pctPoints(data.cpu_pct)} />
          </div>

          <Panel title="Alerts">
            {data.alerts.length === 0 ? <Empty what="alerts" /> : (
              <div className="scroll">
                <table>
                  <thead><tr><th>When</th><th>Severity</th><th>Code</th><th>Message</th></tr></thead>
                  <tbody>
                    {data.alerts.map((a, i) => (
                      <tr key={i}>
                        <td>{ts(a.ts)}</td>
                        <td>
                          <span className={`pill ${a.severity === 'CRITICAL' ? 'red' : a.severity === 'WARN' ? 'amber' : ''}`}>
                            {a.severity}
                          </span>
                        </td>
                        <td>{a.code}</td>
                        <td style={{ whiteSpace: 'normal' }}>{a.message}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <Panel title="Control log">
            {data.controls.length === 0 ? <Empty what="operator actions" /> : (
              <div className="scroll">
                <table>
                  <thead><tr><th>When</th><th>Action</th><th>Operator</th><th>Reason</th></tr></thead>
                  <tbody>
                    {data.controls.map((c, i) => (
                      <tr key={i}>
                        <td>{ts(c.ts)}</td><td>{c.action}</td><td>{c.operator}</td>
                        <td style={{ whiteSpace: 'normal' }}>{c.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      )}
    </Loader>
  )
}
