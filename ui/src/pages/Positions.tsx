import { api, type Strategy } from '../api'
import { Empty, Loader, Panel, useApi } from '../components/Common'
import { cls, mult, num, pct, ts, usd } from '../format'

function CapBar({ label, used, cap }: { label: string; used: number; cap: number }) {
  const frac = cap > 0 ? Math.min(1, Math.abs(used) / cap) : 0
  const tone = frac >= 1 ? '#f85149' : frac >= 0.8 ? '#d29922' : '#3fb950'
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', fontSize: 12, marginBottom: 4 }}>
        <span className="muted">{label}</span>
        <span style={{ flex: 1 }} />
        <span>{mult(Math.abs(used))} / {mult(cap)}</span>
      </div>
      <div style={{ height: 6, background: '#1c232c', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ width: `${frac * 100}%`, height: '100%', background: tone }} />
      </div>
    </div>
  )
}

export function Positions({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.positions(strategy, s), [strategy])

  return (
    <Loader error={error} loading={loading}>
      {!data ? (
        <Empty what="data" />
      ) : (
        <div className="grid">
          <div className="grid cols-2">
            <Panel title="Caps utilisation">
              <CapBar label="Gross" used={data.caps.gross} cap={data.caps.gross_cap} />
              <CapBar label="Net" used={data.caps.net} cap={data.caps.net_cap} />
              <CapBar label={`Single (${data.caps.largest_symbol || '—'})`} used={data.caps.single} cap={data.caps.single_cap} />
            </Panel>
            <Panel title="Margin & survivability">
              <div className="grid tiles" style={{ gap: 10 }}>
                <div className="tile">
                  <div className="label">Margin ratio</div>
                  <div className="value">{pct(data.margin_ratio)}</div>
                </div>
                <div className="tile">
                  <div className="label">Survivable move</div>
                  <div className="value">{pct(data.survivable_move)}</div>
                  <div className="sub">before liquidation</div>
                </div>
              </div>
              {data.blocks.length > 0 && (
                <div style={{ marginTop: 10 }}>
                  <div className="label muted">Active blocks</div>
                  {data.blocks.map((b) => <span key={b} className="pill red" style={{ marginRight: 6 }}>{b}</span>)}
                </div>
              )}
            </Panel>
          </div>

          <Panel title="Positions">
            {data.positions.length === 0 ? (
              <Empty what="open positions" />
            ) : (
              <div className="scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Symbol</th><th>Side</th><th>Qty</th><th>Notional</th><th>Entry</th>
                      <th>Mark</th><th>Unrealised</th><th>Funding</th><th>ADL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.positions.map((p) => (
                      <tr key={p.symbol}>
                        <td>{p.symbol}</td>
                        <td className={p.side === 'long' ? 'up' : p.side === 'short' ? 'down' : 'muted'}>{p.side}</td>
                        <td>{num(p.qty, 4)}</td>
                        <td className={cls(p.notional)}>{usd(p.notional, 0)}</td>
                        <td>{num(p.entry_price, 4)}</td>
                        <td>{num(p.mark_price, 4)}</td>
                        <td className={cls(p.unrealized_pnl)}>{usd(p.unrealized_pnl)}</td>
                        <td className={cls(p.funding_accrued)}>{usd(p.funding_accrued)}</td>
                        <td className={p.adl_quantile >= 4 ? 'down' : ''}>{p.adl_quantile}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <div className="grid cols-2">
            <Panel title="Kill-rule status board">
              <table>
                <thead><tr><th>Rule</th><th>State</th><th>Detail</th></tr></thead>
                <tbody>
                  {data.kill_rules.map((k) => (
                    <tr key={k.rule}>
                      <td>{k.rule}</td>
                      <td><span className={`pill ${k.active ? 'red' : 'green'}`}>{k.active ? 'FIRED' : 'ok'}</span></td>
                      <td className="muted" style={{ whiteSpace: 'normal' }}>{k.detail || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>

            <Panel title="Governor history">
              {data.governor.length === 0 ? (
                <Empty what="governor transitions" />
              ) : (
                <div className="scroll">
                  <table>
                    <thead><tr><th>When</th><th>DD</th><th>g</th><th>Trigger</th></tr></thead>
                    <tbody>
                      {data.governor.slice().reverse().map((g, i) => (
                        <tr key={`${g.ts}-${i}`}>
                          <td>{ts(g.ts)}</td>
                          <td>{pct(g.dd)}</td>
                          <td>{num(g.g_before, 2)} → {num(g.g_after, 2)}</td>
                          <td className="muted">{g.trigger}</td>
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
