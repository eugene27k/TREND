import { useState } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, type Strategy, type TrackingPanel, type TrackingRow } from '../api'
import { AXIS, COLORS, Empty, GRID, Loader, Panel, TIP, Tile, useApi } from '../components/Common'
import { cls, num, pct, signed, ts, usd } from '../format'

export function Backtest({ strategy }: { strategy: Strategy }) {
  const [runId, setRunId] = useState<string | null>(null)
  const { data, error, loading } = useApi(
    (s) => api.backtest(strategy, runId ? { run_id: runId } : undefined, s),
    [strategy, runId],
  )

  return (
    <Loader error={error} loading={loading}>
      {!data ? <Empty what="backtest data" /> : (
        <div className="grid">
          <Panel
            title="Runs"
            right={<span className="muted">showing {data.run ? data.run.run_id : 'no run'}</span>}
          >
            {data.runs.length === 0 ? <Empty what="backtest runs" /> : (
              <div className="scroll" style={{ maxHeight: 240 }}>
                <table>
                  <thead><tr><th>Run</th><th>Created</th><th>Range</th><th>Variant</th><th>Commit</th><th>Duration</th></tr></thead>
                  <tbody>
                    {data.runs.map((r) => (
                      <tr
                        key={r.run_id}
                        onClick={() => setRunId(r.run_id)}
                        style={{ cursor: 'pointer' }}
                        className={data.run && data.run.run_id === r.run_id ? '' : 'muted'}
                      >
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

          {data.run && (
            <Panel title={`Run ${data.run.run_id} — ${data.run.variant}`}>
              <div className="grid cols-2">
                <table>
                  <tbody>
                    <tr><td>Range</td><td>{data.run.start_day} → {data.run.end_day}</td></tr>
                    <tr><td>Created</td><td>{ts(data.run.created_ts)}</td></tr>
                    <tr><td>Commit</td><td className="muted">{data.run.git_commit || 'n/a'}</td></tr>
                    <tr><td>Duration</td><td>{num(data.run.duration_s, 0)} s</td></tr>
                    <tr><td>Equity points</td><td>{data.run.equity.length}</td></tr>
                  </tbody>
                </table>
                <div className="scroll" style={{ maxHeight: 240 }}>
                  <table>
                    <thead><tr><th>Metric</th><th>Value</th></tr></thead>
                    <tbody>
                      {Object.entries(data.run.metrics).length === 0 ? (
                        <tr><td colSpan={2} className="muted">n/a</td></tr>
                      ) : (
                        Object.entries(data.run.metrics).map(([k, v]) => (
                          <tr key={k}>
                            <td>{k.replace(/_/g, ' ')}</td>
                            <td>{typeof v === 'number' ? num(v, 4) : String(v)}</td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </Panel>
          )}

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
                          <td className={cls(r.net_pnl)}>{signed(r.net_pnl)}</td>
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

            <Panel title={`Bootstrap ${data.bootstrap_horizon} P&L distribution`}>
              {Object.keys(data.bootstrap).length === 0 ? <Empty what="bootstrap" /> : (
                <table>
                  <thead><tr><th>Percentile</th><th>{data.bootstrap_horizon} P&L</th></tr></thead>
                  <tbody>
                    {Object.entries(data.bootstrap)
                      .sort((a, b) => Number(a[0]) - Number(b[0]))
                      .map(([p, v]) => (
                        <tr key={p} className={Number(p) === 5 ? 'down' : ''}>
                          {/* The API keys these by percentile as a float ("5.0"); label them p5. */}
                          <td>p{Number.isFinite(Number(p)) ? Number(p) : p}</td>
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
                  <thead>
                    <tr><th>Window</th><th>Parameter set</th><th>Test Sharpe</th><th>Rank</th><th>of</th><th>Default in top half</th></tr>
                  </thead>
                  <tbody>
                    {data.walkforward.map((w, i) => (
                      <tr key={`${w.window}-${w.param_set}-${i}`} className={w.is_default ? '' : 'stale'}>
                        <td>{w.window}</td>
                        <td>{w.param_set}{w.is_default && <span className="pill green" style={{ marginLeft: 6 }}>default</span>}</td>
                        <td>{num(w.test_sharpe, 2)}</td>
                        <td>{w.rank}</td>
                        <td className="muted">{w.n_params}</td>
                        <td>
                          <span className={`pill ${w.default_in_top_half ? 'green' : 'amber'}`}>
                            {w.default_in_top_half ? 'yes' : 'no'}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <Tracking panel={data.tracking} />
        </div>
      )}
    </Loader>
  )
}

function Tracking({ panel }: { panel: TrackingPanel }) {
  const latest: TrackingRow | null = panel.latest
  return (
    <Panel
      title="Live tracking error vs the reference run"
      right={
        <span className="muted">
          bounds: corr ≥ {num(panel.min_corr, 2)} · cum diff ≤ {pct(panel.max_cum_diff)} · cost ≤{' '}
          {num(panel.max_cost_ratio, 2)}× · turnover ≤ {num(panel.max_turnover_ratio, 2)}× · block after{' '}
          {panel.breach_days_block} breach days
        </span>
      }
    >
      {!latest ? <Empty what="tracking data" /> : (
        <>
          <div className="grid tiles">
            <Tile label="Day" value={latest.day} />
            <Tile
              label="P&L corr (30d)"
              value={num(latest.corr_30d, 3)}
              tone={latest.corr_30d !== null && latest.corr_30d < panel.min_corr ? 'down' : ''}
            />
            <Tile
              label="Cumulative diff"
              value={pct(latest.cum_diff_frac)}
              tone={Math.abs(latest.cum_diff_frac) > panel.max_cum_diff ? 'down' : ''}
            />
            <Tile
              label="Cost ratio"
              value={num(latest.cost_ratio, 2)}
              tone={latest.cost_ratio !== null && latest.cost_ratio > panel.max_cost_ratio ? 'down' : ''}
            />
            <Tile
              label="Turnover ratio"
              value={num(latest.turnover_ratio, 2)}
              tone={latest.turnover_ratio !== null && latest.turnover_ratio > panel.max_turnover_ratio ? 'down' : ''}
            />
            <Tile
              label="Breach days"
              value={latest.breach_days}
              tone={latest.breach_days >= panel.breach_days_block ? 'down' : ''}
              sub={<span className={`pill ${latest.in_bounds ? 'green' : 'red'}`}>{latest.in_bounds ? 'in bounds' : 'BREACH'}</span>}
            />
          </div>

          {panel.series.length > 0 && (
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={panel.series} margin={{ top: 12, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid {...GRID} strokeDasharray="3 3" />
                <XAxis dataKey="day" {...AXIS} minTickGap={40} />
                <YAxis {...AXIS} width={70} />
                <Tooltip contentStyle={TIP} />
                <Line type="monotone" dataKey="cum_live" stroke={COLORS.accent} dot={false} strokeWidth={2} name="live" />
                <Line type="monotone" dataKey="cum_ref" stroke={COLORS.warn} dot={false} strokeDasharray="3 3" name="reference" />
              </LineChart>
            </ResponsiveContainer>
          )}

          <div className="scroll" style={{ maxHeight: 280 }}>
            <table>
              <thead>
                <tr>
                  <th>Day</th><th>Live</th><th>Reference</th><th>Cum live</th><th>Cum ref</th>
                  <th>Corr 30d</th><th>Cum diff</th><th>Cost</th><th>Turnover</th><th>Breach days</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {panel.series.slice().reverse().map((r) => (
                  <tr key={r.day}>
                    <td>{r.day}</td>
                    <td className={cls(r.live_pnl)}>{signed(r.live_pnl)}</td>
                    <td className={cls(r.ref_pnl)}>{signed(r.ref_pnl)}</td>
                    <td className={cls(r.cum_live)}>{signed(r.cum_live)}</td>
                    <td className={cls(r.cum_ref)}>{signed(r.cum_ref)}</td>
                    <td className={r.corr_30d !== null && r.corr_30d < panel.min_corr ? 'down' : ''}>{num(r.corr_30d, 3)}</td>
                    <td className={Math.abs(r.cum_diff_frac) > panel.max_cum_diff ? 'down' : ''}>{pct(r.cum_diff_frac)}</td>
                    <td className={r.cost_ratio !== null && r.cost_ratio > panel.max_cost_ratio ? 'down' : ''}>{num(r.cost_ratio, 2)}</td>
                    <td className={r.turnover_ratio !== null && r.turnover_ratio > panel.max_turnover_ratio ? 'down' : ''}>
                      {num(r.turnover_ratio, 2)}
                    </td>
                    <td>{r.breach_days}</td>
                    <td><span className={`pill ${r.in_bounds ? 'green' : 'red'}`}>{r.in_bounds ? 'ok' : 'BREACH'}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Panel>
  )
}
