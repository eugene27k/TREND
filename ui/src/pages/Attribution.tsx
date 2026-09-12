import { useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, type PeriodAttribution, type Strategy } from '../api'
import { AXIS, COLORS, Empty, GRID, Loader, Panel, TIP, useApi } from '../components/Common'
import { cls, num, pct, signed, usd } from '../format'

export function Attribution({ strategy }: { strategy: Strategy }) {
  const { data, error, loading } = useApi((s) => api.attribution(strategy, s), [strategy])
  const [period, setPeriod] = useState<string | null>(null)

  // The period list comes from the payload — the engine decides which windows exist.
  const periods = data?.periods ?? []
  const block = periods.find((p) => p.period === period) ?? periods[0] ?? null
  const monthly = Object.entries(data?.monthly_net_pnl ?? {}).sort((a, b) => a[0].localeCompare(b[0]))

  return (
    <div className="grid">
      <div className="topbar">
        <label className="muted">Period</label>
        <select value={block?.period ?? ''} onChange={(e) => setPeriod(e.target.value)} disabled={periods.length === 0}>
          {periods.length === 0 && <option value="">n/a</option>}
          {periods.map((p) => <option key={p.period} value={p.period}>{p.period}</option>)}
        </select>
      </div>
      <Loader error={error} loading={loading}>
        {!data || !block ? <Empty what="attribution" /> : (
          <div className="grid">
            <PeriodPanels block={block} />

            <Panel title="Regime table (by BTC monthly return)">
              {data.regime.length === 0 ? <Empty what="regime buckets" /> : (
                <table>
                  <thead><tr><th>Bucket</th><th>Label</th><th>Months</th><th>P&L</th><th>Hit rate</th><th>Avg exposure</th></tr></thead>
                  <tbody>
                    {data.regime.map((r) => (
                      <tr key={r.bucket}>
                        <td>{r.bucket}</td>
                        <td className="muted">{r.label}</td>
                        <td>{r.months}</td>
                        <td className={cls(r.pnl)}>{signed(r.pnl)}</td>
                        <td>{pct(r.hit_rate)}</td>
                        <td>{r.avg_exposure === null ? 'n/a' : `${num(r.avg_exposure, 2)}×`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Panel>

            <Panel title="Monthly net P&L">
              {monthly.length === 0 ? <Empty what="monthly P&L" /> : (
                <>
                  <ResponsiveContainer width="100%" height={200}>
                    <BarChart
                      data={monthly.map(([month, value]) => ({ month, value }))}
                      margin={{ top: 4, right: 8, left: 0, bottom: 0 }}
                    >
                      <CartesianGrid {...GRID} strokeDasharray="3 3" />
                      <XAxis dataKey="month" {...AXIS} minTickGap={20} />
                      <YAxis {...AXIS} width={70} />
                      <Tooltip contentStyle={TIP} />
                      <Bar dataKey="value">
                        {monthly.map(([month, value]) => (
                          <Cell key={month} fill={value >= 0 ? COLORS.up : COLORS.down} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                  <div className="scroll" style={{ maxHeight: 200 }}>
                    <table>
                      <thead><tr><th>Month</th><th>Net P&L</th></tr></thead>
                      <tbody>
                        {monthly.map(([month, value]) => (
                          <tr key={month}><td>{month}</td><td className={cls(value)}>{signed(value)}</td></tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </Panel>
          </div>
        )}
      </Loader>
    </div>
  )
}

function PeriodPanels({ block }: { block: PeriodAttribution }) {
  const c = block.components
  return (
    <div className="grid">
      {/* PRD 10.5: a thin period is greyed with its n, never dropped. */}
      <Panel
        title={`${block.period} — ${block.start_day} → ${block.end_day} (${block.days} d)`}
        right={
          <span className={block.below_min_active_days ? 'stale' : ''}>
            net {signed(block.total_net_pnl)} · concentration {pct(block.concentration)} · n={block.n_obs}
            {block.below_min_active_days && <span className="pill amber" style={{ marginLeft: 6 }}>below min active days</span>}
          </span>
        }
      >
        <table>
          <thead><tr><th>Component</th><th>Value</th></tr></thead>
          <tbody>
            <tr><td>price</td><td className={cls(c.price_pnl)}>{signed(c.price_pnl)}</td></tr>
            <tr><td>funding</td><td className={cls(c.funding)}>{signed(c.funding)}</td></tr>
            <tr><td>fees</td><td className={cls(c.fees)}>{signed(c.fees)}</td></tr>
            <tr><td>slippage</td><td className={cls(c.slippage)}>{signed(c.slippage)}</td></tr>
            <tr><td>net</td><td className={cls(c.net_pnl)}>{signed(c.net_pnl)}</td></tr>
            <tr><td>traded notional</td><td>{usd(c.traded_notional, 0)}</td></tr>
          </tbody>
        </table>
      </Panel>

      <Panel title={`Per-symbol contribution — concentration ${pct(block.concentration)}`}>
        {block.by_symbol.length === 0 ? <Empty what="symbol P&L" /> : (
          <>
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={block.by_symbol} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid {...GRID} strokeDasharray="3 3" />
                <XAxis dataKey="symbol" stroke={COLORS.muted} fontSize={10} interval={0} angle={-35} textAnchor="end" height={60} />
                <YAxis {...AXIS} width={70} />
                <Tooltip contentStyle={TIP} />
                <Bar dataKey="net_pnl">
                  {block.by_symbol.map((d) => (
                    <Cell key={d.symbol} fill={d.net_pnl >= 0 ? COLORS.up : COLORS.down} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
            <div className="scroll" style={{ maxHeight: 240 }}>
              <table>
                <thead><tr><th>Symbol</th><th>Net P&L</th><th>Contribution share</th></tr></thead>
                <tbody>
                  {block.by_symbol.map((r) => (
                    <tr key={r.symbol}>
                      <td>{r.symbol}</td>
                      <td className={cls(r.net_pnl)}>{signed(r.net_pnl)}</td>
                      <td className={r.contribution_share > 0.5 ? 'down' : ''}>{pct(r.contribution_share)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Panel>

      <Panel title="By side">
        {block.by_side.length === 0 ? <Empty what="side P&L" /> : (
          <table>
            <thead><tr><th>Side</th><th>Net P&L</th><th>Contribution share</th></tr></thead>
            <tbody>
              {block.by_side.map((r) => (
                <tr key={r.side}>
                  <td>{r.side}</td>
                  <td className={cls(r.net_pnl)}>{signed(r.net_pnl)}</td>
                  <td>{pct(r.contribution_share)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  )
}
