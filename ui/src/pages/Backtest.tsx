import { api, type Strategy } from '../api'
import { Empty, Loader, Panel, useApi } from '../components/Common'
import { cls, num, pct, ts, usd } from '../format'

export function Backtest({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.backtest(strategy, s), [strategy])

  return (
    <Loader error={error} loading={loading}>
      {!data ? <Empty what="backtest data" /> : (
        <div className="grid">
          <Panel title="Runs">
            {data.runs.length === 0 ? <Empty what="backtest runs" /> : (
              <div className="scroll" style={{ maxHeight: 240 }}>
                <table>
                  <thead><tr><th>Run</th><th>Created</th><th>Range</th><th>Variant</th><th>Commit</th><th>Duration</th></tr></thead>
                  <tbody>
                    {data.runs.map((r) => (
                      <tr key={r.run_id}>
                        <td>{r.run_id.slice(0, 12)}</td>
                        <td>{ts(r.created_ts)}</td>
                        <td>{r.start_day} → {r.end_day}</td>
                        <td>{r.variant}</td>
                        <td className="muted">{r.git_commit.slice(0, 8)}</td>
                        <td>{num(r.duration_s, 0)} s</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <div className="grid cols-2">
            <Panel title="Robustness variants (P0: the sign of net P&L must not flip)">
              {data.robustness.length === 0 ? <Empty what="robustness runs" /> : (
                <div className="scroll">
                  <table>
                    <thead><tr><th>Variant</th><th>Net P&L</th><th>Sharpe</th><th>Max DD</th><th>Sign</th></tr></thead>
                    <tbody>
                      {data.robustness.map((r) => (
                        <tr key={r.variant}>
                          <td>{r.variant}</td>
                          <td className={cls(r.net_pnl)}>{usd(r.net_pnl, 0)}</td>
                          <td>{num(r.sharpe, 2)}</td>
                          <td className="down">{pct(r.max_dd)}</td>
                          <td><span className={`pill ${r.sign_ok ? 'green' : 'red'}`}>{r.sign_ok ? 'ok' : 'FLIPPED'}</span></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Panel>

            <Panel title="Bootstrap 3-month P&L distribution">
              {Object.keys(data.bootstrap).length === 0 ? <Empty what="bootstrap" /> : (
                <table>
                  <thead><tr><th>Percentile</th><th>3-month P&L</th></tr></thead>
                  <tbody>
                    {Object.entries(data.bootstrap)
                      .sort((a, b) => Number(a[0]) - Number(b[0]))
                      .map(([p, v]) => (
                        <tr key={p} className={Number(p) === 5 ? 'down' : ''}>
                          <td>p{p}</td>
                          <td className={cls(v)}>{usd(v, 0)}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              )}
            </Panel>
          </div>

          <Panel title="Walk-forward ranking (robustness only — nothing here reaches the live config)">
            {data.walkforward.length === 0 ? <Empty what="walk-forward windows" /> : (
              <div className="scroll">
                <table>
                  <thead><tr><th>Window</th><th>Parameter set</th><th>Test Sharpe</th><th>Rank</th><th>of</th></tr></thead>
                  <tbody>
                    {data.walkforward.map((w, i) => (
                      <tr key={i} className={w.is_default ? '' : 'stale'}>
                        <td>{w.window}</td>
                        <td>{w.param_set}{w.is_default && <span className="pill green" style={{ marginLeft: 6 }}>default</span>}</td>
                        <td>{num(w.test_sharpe, 2)}</td>
                        <td>{w.rank}</td>
                        <td className="muted">{w.n_params}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <Panel title="Live tracking error vs the reference run">
            {!data.tracking ? <Empty what="tracking data" /> : (
              <table>
                <thead><tr><th>Day</th><th>P&L corr (30d)</th><th>Cum diff</th><th>Cost ratio</th><th>Turnover ratio</th><th>Status</th></tr></thead>
                <tbody>
                  <tr>
                    <td>{data.tracking.day}</td>
                    <td className={(data.tracking.corr_30d ?? 1) < 0.7 ? 'down' : ''}>{num(data.tracking.corr_30d, 3)}</td>
                    <td className={Math.abs(data.tracking.cum_diff_frac) > 0.03 ? 'down' : ''}>{pct(data.tracking.cum_diff_frac)}</td>
                    <td className={(data.tracking.cost_ratio ?? 0) > 2 ? 'down' : ''}>{num(data.tracking.cost_ratio, 2)}</td>
                    <td className={(data.tracking.turnover_ratio ?? 0) > 1.5 ? 'down' : ''}>{num(data.tracking.turnover_ratio, 2)}</td>
                    <td><span className={`pill ${data.tracking.in_bounds ? 'green' : 'red'}`}>{data.tracking.in_bounds ? 'in bounds' : 'BREACH'}</span></td>
                  </tr>
                </tbody>
              </table>
            )}
          </Panel>
        </div>
      )}
    </Loader>
  )
}
