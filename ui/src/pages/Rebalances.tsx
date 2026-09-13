import { useState } from 'react'
import { api, type RebalanceDetail, type Strategy } from '../api'
import { Empty, Loader, Panel, useApi } from '../components/Common'
import { cls, num, pct, pctPoints, sideCls, ts, usd } from '../format'

/** RebalanceStatus (aegis.core.types): an aborted run is a failure, not a neutral state. */
const STATUS_TONE: Record<string, string> = {
  complete: 'green',
  window_end: 'amber',
  aborted: 'red',
}

export function Rebalances({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.rebalances(strategy, undefined, s), [strategy])
  const [open, setOpen] = useState<string | null>(null)
  const detail = useApi(
    (s) => (open ? api.rebalance(strategy, open, s) : Promise.resolve(null)),
    [strategy, open],
  )

  return (
    <Loader error={error} loading={loading}>
      {!data || data.rows.length === 0 ? (
        <Empty what="rebalances" />
      ) : (
        <div className="grid">
          <Panel
            title="Rebalance history"
            right={<span className="muted">average completion {pctPoints(data.avg_completion_pct)}</span>}
          >
            <div className="scroll">
              <table>
                <thead>
                  <tr>
                    <th>Day</th><th>Kind</th><th>Status</th><th>Completion</th><th>Traded</th><th>Planned</th>
                    <th>Fees</th><th>Slippage</th><th>Maker</th><th>Duration</th><th>Residuals</th>
                    <th>Equity</th><th>g</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((r) => (
                    <tr
                      key={r.rebalance_id}
                      onClick={() => setOpen(r.rebalance_id)}
                      style={{ cursor: 'pointer' }}
                    >
                      <td>{r.day}</td>
                      <td className="muted">{r.kind}</td>
                      <td>
                        <span className={`pill ${STATUS_TONE[r.status] ?? ''}`}>
                          {r.status}
                        </span>
                      </td>
                      <td className={r.completion_pct < 95 ? 'down' : ''}>{pctPoints(r.completion_pct)}</td>
                      <td>{usd(r.traded_notional, 0)}</td>
                      <td className="muted">{usd(r.planned_notional, 0)}</td>
                      <td className="down">{usd(r.fees)}</td>
                      <td>{num(r.avg_slippage_bps, 1)} bps</td>
                      <td>{pct(r.maker_ratio)}</td>
                      <td>{r.duration_s === null ? 'n/a' : `${num(r.duration_s / 60, 0)} min`}</td>
                      <td className={r.n_residuals > 0 ? 'warn' : ''}>{r.n_residuals}</td>
                      <td>{usd(r.equity, 0)}</td>
                      <td>{num(r.governor_g, 2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>

          {open && (
            <Panel title={`Drill-down — ${open}`} right={<button onClick={() => setOpen(null)}>close</button>}>
              <Loader error={detail.error} loading={detail.loading}>
                {!detail.data ? <Empty what="detail" /> : <Detail d={detail.data} />}
              </Loader>
            </Panel>
          )}
        </div>
      )}
    </Loader>
  )
}

function Detail({ d }: { d: RebalanceDetail }) {
  const sizing = Object.entries(d.sizing)
  const mids = Object.entries(d.decision_mids)
  return (
    <div className="grid">
      <div className="grid cols-2">
        <div>
          <h2>Run</h2>
          <table>
            <tbody>
              <tr><td>Started</td><td>{ts(d.rebalance.started_ts)}</td></tr>
              <tr><td>Ended</td><td>{d.rebalance.ended_ts === null ? 'n/a' : ts(d.rebalance.ended_ts)}</td></tr>
              <tr><td>Status</td><td>{d.rebalance.status}</td></tr>
              <tr><td>Slice cursor</td><td>{d.cursor}</td></tr>
              <tr><td>Order plan</td><td>{d.order_plan.length} legs</td></tr>
              <tr><td>Residuals</td><td className={d.residuals.length > 0 ? 'warn' : ''}>{d.residuals.length}</td></tr>
            </tbody>
          </table>
        </div>

        <div>
          <h2>Sizing</h2>
          {sizing.length === 0 ? <Empty what="sizing inputs" /> : (
            <table>
              <tbody>
                {sizing.map(([k, v]) => (
                  <tr key={k}><td>{k}</td><td>{num(v, 4)}</td></tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div>
        <h2>Targets</h2>
        {d.targets.length === 0 ? <Empty what="targets" /> : (
          <div className="scroll" style={{ maxHeight: 320 }}>
            <table>
              <thead>
                <tr>
                  <th>Symbol</th><th>Signal</th><th>Vol</th><th>Raw</th><th>Target</th><th>Target qty</th>
                  <th>Current qty</th><th>Δ notional</th><th>Funding</th><th>Haircut</th><th>Caps</th><th>Traded</th>
                </tr>
              </thead>
              <tbody>
                {d.targets.map((t) => (
                  <tr key={t.symbol} className={t.traded ? '' : 'stale'}>
                    <td>{t.symbol}</td>
                    <td className={cls(t.signal)}>{num(t.signal, 3)}</td>
                    <td>{pct(t.vol)}</td>
                    <td>{num(t.raw, 3)}</td>
                    <td className={cls(t.target_notional)}>{usd(t.target_notional, 0)}</td>
                    <td>{num(t.target_qty, 4)}</td>
                    <td>{num(t.current_qty, 4)}</td>
                    <td className={cls(t.delta_notional)}>{usd(t.delta_notional, 0)}</td>
                    <td className={cls(-t.funding_ann)}>{pct(t.funding_ann)}</td>
                    <td>{num(t.funding_haircut, 2)}×</td>
                    <td className="muted">{t.caps_applied.length === 0 ? '—' : t.caps_applied.join(', ')}</td>
                    <td>{t.traded ? 'yes' : 'no'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="grid cols-2">
        <div>
          <h2>Slices</h2>
          {d.slices.length === 0 ? <Empty what="slices" /> : (
            <div className="scroll" style={{ maxHeight: 320 }}>
              <table>
                <thead>
                  <tr>
                    <th>Slice</th><th>Symbol</th><th>Seq</th><th>Side</th><th>Qty</th><th>Filled</th>
                    <th>Avg price</th><th>Repegs</th><th>Outcome</th><th>Type</th><th>Placed</th>
                  </tr>
                </thead>
                <tbody>
                  {d.slices.map((s) => (
                    <tr key={s.slice_id}>
                      <td>{s.slice_id.slice(0, 10)}</td>
                      <td>{s.symbol}</td>
                      <td>{s.seq}</td>
                      <td className={sideCls(s.side)}>
                        {s.side}{s.reduce_only && <span className="muted"> (reduce)</span>}
                      </td>
                      <td>{num(s.qty, 4)}</td>
                      <td>{num(s.fill_qty, 4)}</td>
                      <td>{num(s.avg_price, 4)}</td>
                      <td className={s.repegs > 0 ? 'warn' : ''}>{s.repegs}</td>
                      <td>{s.outcome}</td>
                      <td className="muted">{s.taker ? 'taker' : 'maker'}</td>
                      <td className="muted">{ts(s.placed_ts)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div>
          <h2>Fills</h2>
          {d.fills.length === 0 ? <Empty what="fills" /> : (
            <div className="scroll" style={{ maxHeight: 320 }}>
              <table>
                <thead>
                  <tr>
                    <th>Trade</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Fee</th>
                    <th>Realised</th><th>Slippage</th><th>Type</th><th>When</th>
                  </tr>
                </thead>
                <tbody>
                  {d.fills.map((f) => (
                    <tr key={f.trade_id}>
                      <td>{f.trade_id}</td>
                      <td>{f.symbol}</td>
                      <td className={sideCls(f.side)}>{f.side}</td>
                      <td>{num(f.qty, 4)}</td>
                      <td>{num(f.price, 4)}</td>
                      <td className="down">{usd(f.fee, 4)}</td>
                      <td className={cls(f.realized_pnl)}>{usd(f.realized_pnl)}</td>
                      <td>{num(f.slippage_bps, 1)} bps</td>
                      <td className="muted">{f.is_maker ? 'maker' : 'taker'}</td>
                      <td className="muted">{ts(f.ts)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <div className="grid cols-2">
        <div>
          <h2>Decision mids</h2>
          {mids.length === 0 ? <Empty what="decision mids" /> : (
            <div className="scroll" style={{ maxHeight: 260 }}>
              <table>
                <thead><tr><th>Symbol</th><th>Mid</th></tr></thead>
                <tbody>
                  {mids.map(([s, v]) => <tr key={s}><td>{s}</td><td>{num(v, 4)}</td></tr>)}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div>
          <h2>Residuals &amp; order plan</h2>
          <RawTable title="Residuals" rows={d.residuals} />
          <RawTable title="Order plan" rows={d.order_plan} />
        </div>
      </div>
    </div>
  )
}

/** The API models these two as free-form JSON, so they are rendered as sent. */
function RawTable({ title, rows }: { title: string; rows: Record<string, unknown>[] }) {
  if (!rows || rows.length === 0) return <Empty what={title.toLowerCase()} />
  const cols = Object.keys(rows[0]).filter((c) => c !== 'strategy')
  return (
    <div className="scroll" style={{ maxHeight: 240 }}>
      <table>
        <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>{cols.map((c) => <td key={c}>{fmt(r[c])}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function fmt(v: unknown): string {
  if (v === null || v === undefined) return 'n/a'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(6)
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
