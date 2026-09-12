import { api, type Strategy } from '../api'
import { Empty, Loader, Panel, useApi } from '../components/Common'
import { num, ts } from '../format'

export function Universe({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.universe(strategy, s), [strategy])

  return (
    <Loader error={error} loading={loading}>
      {!data ? <Empty what="universe" /> : (
        <div className="grid">
          <div className="grid cols-2">
            <Panel title={`Entrants — ${data.month ?? 'no month yet'}`}>
              {data.entrants.length === 0 ? <Empty what="entrants" />
                : data.entrants.map((s) => <span key={s} className="pill green" style={{ marginRight: 6 }}>{s}</span>)}
            </Panel>
            <Panel title="Leavers (flattened at the next rebalance)">
              {data.leavers.length === 0 ? <Empty what="leavers" />
                : data.leavers.map((s) => <span key={s} className="pill red" style={{ marginRight: 6 }}>{s}</span>)}
            </Panel>
          </div>

          <Panel title="Selection — every considered symbol, with its reason">
            {data.entries.length === 0 ? <Empty what="selection history" /> : (
              <div className="scroll" style={{ maxHeight: 560 }}>
                <table>
                  <thead>
                    <tr><th>Symbol</th><th>Rank</th><th>Median 30d quote vol</th><th>History</th><th>In</th><th>Reason</th></tr>
                  </thead>
                  <tbody>
                    {data.entries.map((e) => (
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

          <Panel title="Illiquid exclusions">
            {data.illiquid.length === 0 ? <Empty what="illiquid flags" /> : (
              <table>
                <thead><tr><th>Symbol</th><th>Reason</th><th>Until</th></tr></thead>
                <tbody>
                  {data.illiquid.map((f) => (
                    <tr key={f.symbol}><td>{f.symbol}</td><td className="muted">{f.reason}</td><td>{ts(f.until_ts)}</td></tr>
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
