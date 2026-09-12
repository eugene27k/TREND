import { useState } from 'react'
import { api, type Strategy } from '../api'
import { Empty, Loader, Panel, useApi } from '../components/Common'
import { num, pct, pctPoints, usd } from '../format'

export function Rebalances({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.rebalances(strategy, s), [strategy])
  const [open, setOpen] = useState<string | null>(null)
  const detail = useApi(
    (s) => (open ? api.rebalance(strategy, open, s) : Promise.resolve(null)),
    [strategy, open],
  )

  return (
    <Loader error={error} loading={loading}>
      {!data || data.rebalances.length === 0 ? (
        <Empty what="rebalances" />
      ) : (
        <div className="grid">
          <Panel title="Rebalance history">
            <div className="scroll">
              <table>
                <thead>
                  <tr>
                    <th>Day</th><th>Kind</th><th>Status</th><th>Completion</th><th>Traded</th>
                    <th>Fees</th><th>Slippage</th><th>Maker</th><th>Duration</th><th>Residuals</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rebalances.map((r) => (
                    <tr key={r.rebalance_id} onClick={() => setOpen(r.rebalance_id)} style={{ cursor: 'pointer' }}>
                      <td>{r.day}</td>
                      <td className="muted">{r.kind}</td>
                      <td>
                        <span className={`pill ${r.status === 'complete' ? 'green' : r.status === 'window_end' ? 'amber' : ''}`}>
                          {r.status}
                        </span>
                      </td>
                      <td className={r.completion_pct < 95 ? 'down' : ''}>{pctPoints(r.completion_pct)}</td>
                      <td>{usd(r.traded_notional, 0)}</td>
                      <td className="down">{usd(r.fees)}</td>
                      <td>{num(r.avg_slippage_bps, 1)} bps</td>
                      <td>{pct(r.maker_ratio)}</td>
                      <td>{r.duration_s === null ? 'n/a' : `${num(r.duration_s / 60, 0)} min`}</td>
                      <td>{r.residuals}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>

          {open && (
            <Panel title={`Drill-down — ${open}`} right={<button onClick={() => setOpen(null)}>close</button>}>
              <Loader error={detail.error} loading={detail.loading}>
                {!detail.data ? <Empty what="detail" /> : (
                  <div className="grid cols-2">
                    <RawTable title="Slices" rows={detail.data.slices} />
                    <RawTable title="Fills" rows={detail.data.fills} />
                    <RawTable title="Targets" rows={detail.data.targets} />
                  </div>
                )}
              </Loader>
            </Panel>
          )}
        </div>
      )}
    </Loader>
  )
}

function RawTable({ title, rows }: { title: string; rows: Record<string, unknown>[] }) {
  if (!rows || rows.length === 0) return <div><h2>{title}</h2><Empty what={title.toLowerCase()} /></div>
  const cols = Object.keys(rows[0]).filter((c) => c !== 'strategy')
  return (
    <div>
      <h2>{title}</h2>
      <div className="scroll">
        <table>
          <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>{cols.map((c) => <td key={c}>{fmt(r[c])}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function fmt(v: unknown): string {
  if (v === null || v === undefined) return 'n/a'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(6)
  return String(v)
}
