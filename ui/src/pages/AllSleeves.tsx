import { api } from '../api'
import { Empty, Loader, Panel, StatusPill, useApi } from '../components/Common'
import { pct, usd } from '../format'

/** US-T18 AC 7 — every sleeve's equity, drawdown and status side by side. */
export function AllSleeves() {
  const { data, error, loading } = useApi((s) => api.allSleeves(s), [])
  return (
    <Loader error={error} loading={loading}>
      {!data || data.sleeves.length === 0 ? <Empty what="sleeves" /> : (
        <Panel title="All sleeves">
          <table>
            <thead><tr><th>Sleeve</th><th>Equity</th><th>Drawdown</th><th>Phase</th><th>Status</th></tr></thead>
            <tbody>
              {data.sleeves.map((s) => (
                <tr key={s.strategy}>
                  <td>{s.strategy}</td>
                  <td>{usd(s.equity)}</td>
                  <td className="down">{pct(s.drawdown)}</td>
                  <td>{s.phase}</td>
                  <td><StatusPill status={s.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </Loader>
  )
}
