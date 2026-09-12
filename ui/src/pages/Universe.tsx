import { useState } from 'react'
import { api, type MonthRow, type Strategy } from '../api'
import { Empty, Loader, Panel, Tile, useApi } from '../components/Common'
import { num, ts } from '../format'

export function Universe({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.universe(strategy, undefined, s), [strategy])
  const [month, setMonth] = useState<string | null>(null)

  const months = data?.months ?? []
  // Newest month first: the selection history reads backwards from today.
  const ordered = months.slice().reverse()
  const selected = ordered.find((m) => m.month === month) ?? ordered[0] ?? null

  return (
    <Loader error={error} loading={loading}>
      {!data ? <Empty what="universe" /> : (
        <div className="grid">
          <div className="grid tiles">
            <Tile label="Target size" value={data.size} sub="symbols per month" />
            <Tile label="Current month" value={data.current_month ?? 'n/a'} />
            <Tile label="Current symbols" value={data.current_symbols.length} sub={data.current_symbols.join(' ') || 'n/a'} />
            <Tile label="Illiquid flags" value={data.illiquid.length} tone={data.illiquid.length > 0 ? 'warn' : ''} />
          </div>

          <Panel title="Monthly selection history">
            {ordered.length === 0 ? <Empty what="monthly selections" /> : (
              <div className="scroll" style={{ maxHeight: 320 }}>
                <table>
                  <thead>
                    <tr><th>Month</th><th>Size</th><th>Entrants</th><th>Leavers</th><th>Considered</th><th>Excluded</th></tr>
                  </thead>
                  <tbody>
                    {ordered.map((m) => (
                      <tr
                        key={m.month}
                        onClick={() => setMonth(m.month)}
                        style={{ cursor: 'pointer' }}
                        className={selected?.month === m.month ? '' : 'muted'}
                      >
                        <td>{m.month}</td>
                        <td>{m.symbols.length}</td>
                        <td className="up" style={{ whiteSpace: 'normal' }}>{m.entrants.join(' ') || '—'}</td>
                        <td className="down" style={{ whiteSpace: 'normal' }}>{m.leavers.join(' ') || '—'}</td>
                        <td>{m.entries.length}</td>
                        <td>{m.excluded.length}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          {selected && <MonthDetail month={selected} />}

          <Panel title="Illiquid exclusions (in the universe, not traded)">
            {data.illiquid.length === 0 ? <Empty what="illiquid flags" /> : (
              <table>
                <thead><tr><th>Symbol</th><th>Reason</th><th>Flagged</th><th>Until</th></tr></thead>
                <tbody>
                  {data.illiquid.map((f) => (
                    <tr key={f.symbol}>
                      <td>{f.symbol}</td>
                      <td className="muted" style={{ whiteSpace: 'normal' }}>{f.reason}</td>
                      <td>{ts(f.flagged_ts)}</td>
                      <td>{ts(f.until_ts)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
        </div>
      )}
    </Loader>
  )
}

function MonthDetail({ month }: { month: MonthRow }) {
  return (
    <div className="grid">
      <div className="grid cols-2">
        <Panel title={`Entrants — ${month.month}`}>
          {month.entrants.length === 0 ? <Empty what="entrants" />
            : month.entrants.map((s) => <span key={s} className="pill green" style={{ marginRight: 6 }}>{s}</span>)}
        </Panel>
        <Panel title="Leavers (flattened at the next rebalance)">
          {month.leavers.length === 0 ? <Empty what="leavers" />
            : month.leavers.map((s) => <span key={s} className="pill red" style={{ marginRight: 6 }}>{s}</span>)}
        </Panel>
      </div>

      <Panel title={`Selection — every considered symbol for ${month.month}, with its reason`}>
        {month.entries.length === 0 ? <Empty what="selection history" /> : (
          <div className="scroll" style={{ maxHeight: 560 }}>
            <table>
              <thead>
                <tr><th>Symbol</th><th>Rank</th><th>Median 30d quote vol</th><th>History</th><th>In</th><th>Reason</th></tr>
              </thead>
              <tbody>
                {month.entries.map((e) => (
                  <tr key={e.symbol} className={e.included ? '' : 'stale'}>
                    <td>{e.symbol}</td>
                    <td>{e.rank > 0 ? e.rank : '—'}</td>
                    <td>{num(e.median_quote_volume_30d, 0)}</td>
                    <td>{e.history_days} d</td>
                    <td><span className={`pill ${e.included ? 'green' : ''}`}>{e.included ? 'yes' : 'no'}</span></td>
                    <td className="muted" style={{ whiteSpace: 'normal' }}>{e.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  )
}
