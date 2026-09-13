import { api } from '../api'
import { Empty, Loader, Panel, StatusPill, Tile, useApi } from '../components/Common'
import { num, pct, ts, usd } from '../format'

/** US-T18 AC 7 — every sleeve's equity, drawdown and status side by side. */
export function AllSleeves() {
  const { data, error, loading } = useApi((s) => api.allSleeves(s), [])
  return (
    <Loader error={error} loading={loading}>
      {!data || data.sleeves.length === 0 ? <Empty what="sleeves" /> : (
        <div className="grid">
          <div className="grid tiles">
            <Tile label="Total equity" value={usd(data.total_equity)} sub="USDT across sleeves" />
            <Tile label="Sleeves" value={data.sleeves.length} />
            <Tile label="As of" value={ts(data.as_of_ts)} />
          </div>

          <Panel title="All sleeves">
            <div className="scroll">
              <table>
                <thead>
                  <tr>
                    <th>Sleeve</th><th>Equity</th><th>Current DD</th><th>Max DD</th><th>g</th>
                    <th>State</th><th>Phase</th><th>Status</th><th>Blocks</th>
                  </tr>
                </thead>
                <tbody>
                  {data.sleeves.map((s) => (
                    <tr key={s.strategy}>
                      <td>{s.strategy}</td>
                      <td>{usd(s.equity)}</td>
                      <td className="down">{pct(s.current_drawdown)}</td>
                      <td className="down">{pct(s.max_drawdown)}</td>
                      <td>{num(s.governor_g, 2)}</td>
                      <td>
                        {s.state}
                        {s.paused && <span className="pill amber" style={{ marginLeft: 6 }}>paused</span>}
                      </td>
                      <td>{s.phase}</td>
                      <td><StatusPill status={s.risk_status} /></td>
                      <td style={{ whiteSpace: 'normal' }}>
                        {s.blocks.length === 0
                          ? <span className="muted">—</span>
                          : s.blocks.map((b) => <span key={b} className="pill red" style={{ marginRight: 4 }}>{b}</span>)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        </div>
      )}
    </Loader>
  )
}
