import { useState } from 'react'
import { api, engineStateView, type Strategy } from '../api'
import { Empty, Loader, Panel, Tile, useApi } from '../components/Common'
import { num, ts } from '../format'

// The action list, and which actions need the typed confirmation, come from
// GET /controls — the engine owns that rule (aegis.ops.controls), not the UI.
const LABELS: Record<string, string> = {
  start: 'Start',
  pause: 'Pause (rebalances only)',
  resume: 'Resume',
  stop: 'Stop',
  flatten_all: 'Flatten all',
  clear_halt: 'Clear HALTED_RISK',
}
const DANGEROUS = new Set(['stop', 'flatten_all', 'clear_halt'])
/** The API rejects these without a written reason (US-T13 AC 3). */
const NEEDS_REASON = new Set(['stop', 'flatten_all', 'clear_halt'])

export function Controls({ strategy }: { strategy: Strategy }) {
  const [operator, setOperator] = useState('')
  const [reason, setReason] = useState('')
  const [confirm, setConfirm] = useState('')
  const [result, setResult] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [nonce, setNonce] = useState(0)

  const { data, error, loading } = useApi((s) => api.controls(strategy, s), [strategy, nonce])
  const state = engineStateView(data?.state)
  const actions = data?.actions ?? []
  const confirmRequired = new Set(data?.confirm_required ?? [])

  async function send(action: string) {
    if (!operator.trim()) return setResult('An operator name is required — every action is audited.')
    if (NEEDS_REASON.has(action) && !reason.trim()) return setResult('A reason is required for this action.')
    setBusy(true)
    setResult(null)
    try {
      const r = await api.control(strategy, { action, operator, reason, confirm })
      setResult(
        r.accepted
          ? `Queued: ${r.action} by ${r.operator} at ${ts(r.ts)} — ${r.detail}`
          : `Refused: ${r.action}`,
      )
      setNonce((n) => n + 1)
    } catch (e) {
      setResult(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid">
      <Loader error={error} loading={loading}>
        <div className="grid tiles">
          <Tile label="State" value={state.state || 'n/a'} sub={state.phase || 'n/a'} />
          <Tile label="Paused" value={state.paused ? 'yes' : 'no'} tone={state.paused ? 'warn' : ''} />
          <Tile label="Stopped" value={state.stopped ? 'yes' : 'no'} tone={state.stopped ? 'down' : ''} />
          <Tile label="Safe mode" value={state.safe_mode ? 'yes' : 'no'} tone={state.safe_mode ? 'warn' : ''} />
          <Tile label="Governor g" value={num(state.governor_g, 2)} />
          <Tile label="Updated" value={state.updated_ts === null ? 'n/a' : ts(state.updated_ts)} />
        </div>

        {state.halt_reason && (
          <Panel title="Halt reason">
            <div className="down" style={{ whiteSpace: 'normal' }}>{state.halt_reason}</div>
          </Panel>
        )}

        {state.blocks.length > 0 && (
          <Panel title="Active blocks">
            {state.blocks.map((b) => <span key={b} className="pill red" style={{ marginRight: 6 }}>{b}</span>)}
          </Panel>
        )}
      </Loader>

      <Panel title="Operator">
        <div className="grid cols-2">
          <label className="grid" style={{ gap: 4 }}>
            <span className="muted">Name (recorded with every action)</span>
            <input value={operator} onChange={(e) => setOperator(e.target.value)} placeholder="e.g. eugene" />
          </label>
          <label className="grid" style={{ gap: 4 }}>
            <span className="muted">
              Confirmation token ({[...confirmRequired].join(', ') || 'destructive actions'})
            </span>
            <input value={confirm} onChange={(e) => setConfirm(e.target.value)} type="password" />
          </label>
          <label className="grid" style={{ gap: 4, gridColumn: '1 / -1' }}>
            <span className="muted">Reason (required to stop, flatten or clear a halt — it is stored)</span>
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
        </div>
      </Panel>

      <Panel title="Actions">
        {actions.length === 0 ? <Empty what="available actions" /> : (
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {actions.map((a) => (
              <button
                key={a}
                disabled={busy}
                onClick={() => send(a)}
                style={DANGEROUS.has(a) ? { borderColor: '#6e2b28', color: '#f85149' } : undefined}
              >
                {LABELS[a] ?? a}
                {confirmRequired.has(a) && <span className="muted"> (token)</span>}
              </button>
            ))}
          </div>
        )}
        <p className="muted" style={{ marginBottom: 0 }}>
          Pause blocks rebalances but never risk actions. Nothing here can increase exposure — the API only
          appends to the control log and the engine applies it on its next tick.
        </p>
        {result && <div style={{ marginTop: 10 }} className={result.startsWith('Queued') ? 'up' : 'error'}>{result}</div>}
      </Panel>

      <Panel title="Control log">
        {!data || data.log.length === 0 ? <Empty what="operator actions" /> : (
          <div className="scroll">
            <table>
              <thead><tr><th>When</th><th>Action</th><th>Operator</th><th>Reason</th></tr></thead>
              <tbody>
                {data.log.map((c) => (
                  <tr key={c.id}>
                    <td>{ts(c.ts)}</td><td>{c.action}</td><td>{c.operator}</td>
                    <td style={{ whiteSpace: 'normal' }}>{c.reason || '—'}</td>
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
