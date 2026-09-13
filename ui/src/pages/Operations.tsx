import { api, engineStateView, type OperationsResponse, type Strategy } from '../api'
import { Empty, Loader, Panel, Tile, useApi } from '../components/Common'
import { num, pctPoints, ts } from '../format'

export function Operations({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.operations(strategy, s), [strategy])

  return (
    <Loader error={error} loading={loading}>
      {!data ? <Empty what="operations data" /> : (
        <div className="grid">
          <OpsTiles data={data} />

          <div className="grid cols-2">
            <Panel title="Heartbeat">
              <table>
                <tbody>
                  <tr>
                    <td>Enabled</td>
                    <td>
                      <span className={`pill ${data.heartbeat.enabled ? 'green' : ''}`}>
                        {data.heartbeat.enabled ? 'yes' : 'no'}
                      </span>
                    </td>
                  </tr>
                  <tr><td>Interval</td><td>{data.heartbeat.interval_s} s</td></tr>
                  <tr><td>Last beat</td><td>{data.heartbeat.last_ts === null ? 'n/a' : ts(data.heartbeat.last_ts)}</td></tr>
                  <tr>
                    <td>Last beat ok</td>
                    <td>{data.heartbeat.last_ok === null ? 'n/a' : data.heartbeat.last_ok ? 'yes' : 'no'}</td>
                  </tr>
                  <tr><td>Beats in 24 h</td><td>{data.heartbeat.beats_24h}</td></tr>
                  <tr><td>Uptime 24 h</td><td>{pctPoints(data.heartbeat.uptime_24h_pct)}</td></tr>
                  <tr><td>Uptime 7 d</td><td>{pctPoints(data.heartbeat.uptime_7d_pct)}</td></tr>
                  <tr><td>Uptime 30 d</td><td>{pctPoints(data.heartbeat.uptime_30d_pct)}</td></tr>
                </tbody>
              </table>
            </Panel>

            <Panel title="Reconciliation">
              <table>
                <tbody>
                  <tr><td>Last run</td><td>{data.reconciliation.last_ts === null ? 'n/a' : ts(data.reconciliation.last_ts)}</td></tr>
                  <tr><td>Kind</td><td>{data.reconciliation.last_kind ?? 'n/a'}</td></tr>
                  <tr>
                    <td>Result</td>
                    <td>
                      {data.reconciliation.last_ok === null ? 'n/a' : (
                        <span className={`pill ${data.reconciliation.last_ok ? 'green' : 'red'}`}>
                          {data.reconciliation.last_ok ? 'OK' : 'BREAK'}
                        </span>
                      )}
                    </td>
                  </tr>
                  <tr>
                    <td>Open breaks</td>
                    <td className={data.reconciliation.open_breaks > 0 ? 'down' : ''}>{data.reconciliation.open_breaks}</td>
                  </tr>
                  <tr><td>Detail</td><td style={{ whiteSpace: 'normal' }}>{data.reconciliation.last_detail || '—'}</td></tr>
                </tbody>
              </table>
              {data.reconciliation.breaks.length > 0 && (
                <div className="scroll" style={{ maxHeight: 200, marginTop: 10 }}>
                  <table>
                    <thead><tr><th>When</th><th>Kind</th><th>Detail</th></tr></thead>
                    <tbody>
                      {data.reconciliation.breaks.map((b, i) => (
                        <tr key={i}>
                          <td>{typeof b.ts === 'number' ? ts(b.ts) : 'n/a'}</td>
                          <td>{String(b.kind ?? '—')}</td>
                          <td style={{ whiteSpace: 'normal' }}>{String(b.detail ?? '—')}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Panel>
          </div>

          <div className="grid cols-2">
            <Panel title="Backup">
              <table>
                <tbody>
                  <tr>
                    <td>Enabled</td>
                    <td>
                      <span className={`pill ${data.backup.enabled ? 'green' : ''}`}>
                        {data.backup.enabled ? 'yes' : 'no'}
                      </span>
                    </td>
                  </tr>
                  <tr><td>Replica</td><td style={{ whiteSpace: 'normal' }}>{data.backup.replica_path || 'n/a'}</td></tr>
                  <tr><td>Last OK</td><td>{data.backup.last_ok_ts === null ? 'n/a' : ts(data.backup.last_ok_ts)}</td></tr>
                  <tr><td>Lag</td><td>{data.backup.lag_s === null ? 'n/a' : `${num(data.backup.lag_s, 0)} s`}</td></tr>
                  <tr><td>Restore check</td><td>every {data.backup.restore_check_days} d</td></tr>
                </tbody>
              </table>
            </Panel>

            <Panel title="Infrastructure">
              <table>
                <tbody>
                  <tr><td>Host</td><td>{data.infra.host || 'n/a'}</td></tr>
                  <tr><td>Monthly cost</td><td>€{num(data.infra.monthly_cost_eur, 2)}</td></tr>
                  <tr>
                    <td>RSS</td>
                    <td className={(data.infra.rss_mb ?? 0) > data.infra.max_rss_mb ? 'down' : ''}>
                      {data.infra.rss_mb === null ? 'n/a' : `${num(data.infra.rss_mb, 0)} MB`}
                      <span className="muted"> / {data.infra.max_rss_mb} MB</span>
                    </td>
                  </tr>
                  <tr><td>CPU</td><td>{pctPoints(data.infra.cpu_pct)}</td></tr>
                  <tr><td>Net of infra</td><td>{data.infra.net_of_infra === null ? 'n/a' : num(data.infra.net_of_infra, 2)}</td></tr>
                </tbody>
              </table>
            </Panel>
          </div>

          <Panel
            title="Alerts"
            right={
              data.unacked_critical > 0
                ? <span className="pill red">{data.unacked_critical} unacked critical</span>
                : <span className="muted">no unacked critical</span>
            }
          >
            {data.alerts.length === 0 ? <Empty what="alerts" /> : (
              <div className="scroll">
                <table>
                  <thead><tr><th>When</th><th>Severity</th><th>Code</th><th>Message</th><th>Acked</th><th>Delivered</th></tr></thead>
                  <tbody>
                    {data.alerts.map((a) => (
                      <tr key={a.id}>
                        <td>{ts(a.ts)}</td>
                        <td>
                          <span className={`pill ${a.severity === 'CRITICAL' ? 'red' : a.severity === 'WARN' ? 'amber' : ''}`}>
                            {a.severity}
                          </span>
                        </td>
                        <td>{a.code}</td>
                        <td style={{ whiteSpace: 'normal' }}>{a.message}</td>
                        <td className={a.acked ? 'muted' : 'down'}>{a.acked ? 'yes' : 'no'}</td>
                        <td className="muted">{a.delivered ? 'yes' : 'no'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <div className="grid cols-2">
            <Panel title="Control log">
              {data.control_log.length === 0 ? <Empty what="operator actions" /> : (
                <div className="scroll">
                  <table>
                    <thead><tr><th>When</th><th>Action</th><th>Operator</th><th>Reason</th></tr></thead>
                    <tbody>
                      {data.control_log.map((c) => (
                        <tr key={c.id}>
                          <td>{ts(c.ts)}</td><td>{c.action}</td><td>{c.operator}</td>
                          <td style={{ whiteSpace: 'normal' }}>{c.reason || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Panel>

            <Panel title="Reports">
              {data.reports.length === 0 ? <Empty what="reports" /> : (
                <div className="scroll">
                  <table>
                    <thead><tr><th>Kind</th><th>Period</th><th>When</th><th>Delivered</th></tr></thead>
                    <tbody>
                      {data.reports.map((r, i) => (
                        <tr key={`${r.kind}-${r.period_key}-${i}`}>
                          <td>{r.kind}</td>
                          <td>{r.period_key}</td>
                          <td>{ts(r.ts)}</td>
                          <td className={r.delivered ? '' : 'warn'}>{r.delivered ? 'yes' : 'no'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Panel>
          </div>
        </div>
      )}
    </Loader>
  )
}

function OpsTiles({ data }: { data: OperationsResponse }) {
  const state = engineStateView(data.state)
  const hb = data.heartbeat.uptime_7d_pct
  return (
    <div className="grid tiles">
      <Tile label="Mode" value={data.mode} sub={state.state || 'n/a'} />
      <Tile
        label="Heartbeat 7 d"
        value={pctPoints(hb)}
        tone={hb === null ? '' : hb >= 99.5 ? 'up' : 'down'}
        sub={`24 h ${pctPoints(data.heartbeat.uptime_24h_pct)} · 30 d ${pctPoints(data.heartbeat.uptime_30d_pct)}`}
      />
      <Tile
        label="Reconciliation"
        value={data.reconciliation.last_ok === null ? 'n/a' : data.reconciliation.last_ok ? 'OK' : 'BREAK'}
        tone={data.reconciliation.last_ok === null ? '' : data.reconciliation.last_ok ? 'up' : 'down'}
        sub={`${data.reconciliation.open_breaks} open breaks`}
      />
      <Tile
        label="Backup lag"
        value={data.backup.lag_s === null ? 'n/a' : `${num(data.backup.lag_s, 0)} s`}
        tone={data.backup.enabled ? '' : 'muted'}
      />
      <Tile label="Infra cost" value={`€${num(data.infra.monthly_cost_eur, 2)}`} sub="per month" />
      <Tile
        label="RSS"
        value={data.infra.rss_mb === null ? 'n/a' : `${num(data.infra.rss_mb, 0)} MB`}
        sub={`cap ${data.infra.max_rss_mb} MB`}
      />
      <Tile label="CPU" value={pctPoints(data.infra.cpu_pct)} />
      <Tile
        label="Unacked critical"
        value={data.unacked_critical}
        tone={data.unacked_critical > 0 ? 'down' : ''}
      />
    </div>
  )
}
