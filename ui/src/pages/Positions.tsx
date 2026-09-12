import { api, type Strategy } from '../api'
import { Empty, Loader, Meter, Panel, StatusPill, Tile, useApi } from '../components/Common'
import { cls, mult, num, pct, sideCls, ts, usd } from '../format'

export function Positions({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.positions(strategy, undefined, s), [strategy])

  return (
    <Loader error={error} loading={loading}>
      {!data ? (
        <Empty what="data" />
      ) : (
        <div className="grid">
          <div className="grid tiles">
            <Tile label="Risk status" value={<StatusPill status={data.risk_status} />} sub={data.state} />
            <Tile label="Governor g" value={num(data.governor_g, 2)} />
            <Tile
              label="Margin ratio"
              value={pct(data.margin.margin_ratio)}
              tone={
                data.margin.margin_ratio >= data.margin.margin_red
                  ? 'down'
                  : data.margin.margin_ratio >= data.margin.margin_amber
                    ? 'warn'
                    : ''
              }
              sub={`amber ${pct(data.margin.margin_amber)} · red ${pct(data.margin.margin_red)}`}
            />
            <Tile
              label="Survivable move"
              value={pct(data.margin.survivable_move)}
              tone={data.margin.survives_downtime ? 'up' : 'down'}
              sub={`shock ${pct(data.margin.shock_price)} — ${data.margin.survives_downtime ? 'survives downtime' : 'DOES NOT SURVIVE'}`}
            />
          </div>

          <div className="grid cols-2">
            {/* One bar per cap the API sends — the set is the engine's, not the UI's. */}
            <Panel title="Caps utilisation">
              {data.caps.length === 0 ? (
                <Empty what="caps" />
              ) : (
                data.caps.map((c) => (
                  <Meter
                    key={c.cap}
                    label={
                      <>
                        {c.cap} {c.breached && <span className="pill red" style={{ marginLeft: 4 }}>breached</span>}
                      </>
                    }
                    frac={c.utilisation}
                    breached={c.breached}
                    right={
                      <>
                        {mult(c.used_x)} / {mult(c.limit_x)}{' '}
                        <span className="muted">
                          ({usd(c.used_usdt, 0)} / {usd(c.limit_usdt, 0)} · {pct(c.utilisation)})
                        </span>
                      </>
                    }
                  />
                ))
              )}
            </Panel>

            <Panel title="Margin & survivability">
              <table>
                <tbody>
                  <tr><td>Equity</td><td>{usd(data.margin.equity)}</td></tr>
                  <tr><td>Wallet balance</td><td>{usd(data.margin.wallet_balance)}</td></tr>
                  <tr><td>Available balance</td><td>{usd(data.margin.available_balance)}</td></tr>
                  <tr><td>Maintenance margin</td><td>{usd(data.margin.maint_margin)}</td></tr>
                  <tr><td>Margin ratio</td><td>{pct(data.margin.margin_ratio)}</td></tr>
                  <tr><td>Survivable move</td><td>{pct(data.margin.survivable_move)}</td></tr>
                  <tr><td>Shock price</td><td>{pct(data.margin.shock_price)}</td></tr>
                  <tr>
                    <td>Survives downtime</td>
                    <td>
                      <span className={`pill ${data.margin.survives_downtime ? 'green' : 'red'}`}>
                        {data.margin.survives_downtime ? 'yes' : 'no'}
                      </span>
                    </td>
                  </tr>
                </tbody>
              </table>
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
                      <th>Symbol</th><th>Side</th><th>Qty</th><th>Notional</th><th>Target</th><th>Entry</th>
                      <th>Mark</th><th>Unrealised</th><th>Funding</th><th>Leverage</th><th>Liq. price</th>
                      <th>ADL</th><th>As of</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.positions.map((p) => (
                      <tr key={p.symbol}>
                        <td>{p.symbol}</td>
                        <td className={sideCls(p.side)}>{p.side}</td>
                        <td>{num(p.qty, 4)}</td>
                        <td className={cls(p.notional)}>{usd(p.notional, 0)}</td>
                        <td className={cls(p.target_notional)}>{usd(p.target_notional, 0)}</td>
                        <td>{num(p.entry_price, 4)}</td>
                        <td>{num(p.mark_price, 4)}</td>
                        <td className={cls(p.unrealized_pnl)}>{usd(p.unrealized_pnl)}</td>
                        <td className={cls(p.funding_accrued)}>{usd(p.funding_accrued)}</td>
                        <td>{mult(p.leverage)}</td>
                        <td>{num(p.liquidation_price, 4)}</td>
                        <td className={p.adl_quantile >= 4 ? 'down' : ''}>{p.adl_quantile}</td>
                        <td className="muted">{ts(p.ts)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <div className="grid cols-2">
            <Panel title="Kill-rule status board">
              {data.kill_rules.length === 0 ? (
                <Empty what="kill rules" />
              ) : (
                <table>
                  <thead><tr><th>Rule</th><th>State</th><th>Last fired</th><th>Detail</th></tr></thead>
                  <tbody>
                    {data.kill_rules.map((k) => (
                      <tr key={k.rule}>
                        <td>{k.rule}</td>
                        <td><span className={`pill ${k.active ? 'red' : 'green'}`}>{k.active ? 'FIRED' : 'ok'}</span></td>
                        <td className="muted">{k.last_fired_ts === null ? 'n/a' : ts(k.last_fired_ts)}</td>
                        <td className="muted" style={{ whiteSpace: 'normal' }}>{k.detail || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Panel>

            <Panel title="Governor history">
              {data.governor_history.length === 0 ? (
                <Empty what="governor transitions" />
              ) : (
                <div className="scroll">
                  <table>
                    <thead><tr><th>When</th><th>DD</th><th>g</th><th>Trigger</th><th>Applied</th></tr></thead>
                    <tbody>
                      {data.governor_history.slice().reverse().map((g, i) => (
                        <tr key={`${g.ts}-${i}`} className={g.applied ? '' : 'stale'}>
                          <td>{ts(g.ts)}</td>
                          <td>{pct(g.dd)}</td>
                          <td>{num(g.g_before, 2)} → {num(g.g_after, 2)}</td>
                          <td className="muted">{g.trigger}</td>
                          <td>{g.applied ? 'yes' : 'no'}</td>
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
