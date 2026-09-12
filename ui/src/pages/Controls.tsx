import { useState } from 'react'
import { api, type ControlRequest, type Strategy } from '../api'
import { Panel } from '../components/Common'

// Stop and Flatten-all require a typed confirmation (Aegis US-18). The UI does
// not know the token — it forwards what the operator types and lets the engine
// refuse, so the check lives in exactly one place.
const ACTIONS: { action: ControlRequest['action']; label: string; danger?: boolean; confirm?: boolean; reason?: boolean }[] = [
  { action: 'start', label: 'Start' },
  { action: 'pause', label: 'Pause (rebalances only)' },
  { action: 'resume', label: 'Resume' },
  { action: 'stop', label: 'Stop', danger: true, confirm: true, reason: true },
  { action: 'flatten_all', label: 'Flatten all', danger: true, confirm: true, reason: true },
  { action: 'clear_halt', label: 'Clear HALTED_RISK', danger: true, reason: true },
]

export function Controls({ strategy }: { strategy: Strategy }) {
  const [operator, setOperator] = useState('')
  const [reason, setReason] = useState('')
  const [confirm, setConfirm] = useState('')
  const [result, setResult] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function send(action: ControlRequest['action'], needsReason?: boolean) {
    if (!operator.trim()) return setResult('An operator name is required — every action is audited.')
    if (needsReason && !reason.trim()) return setResult('A reason is required for this action.')
    setBusy(true)
    setResult(null)
    try {
      const r = await api.control(strategy, { action, operator, reason, confirm: confirm || undefined })
      setResult(r.accepted ? `Queued: ${r.action}. The engine applies it on its next tick.` : `Refused: ${r.action}`)
    } catch (e) {
      setResult(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid">
      <Panel title="Operator">
        <div className="grid cols-2">
          <label className="grid" style={{ gap: 4 }}>
            <span className="muted">Name (recorded with every action)</span>
            <input value={operator} onChange={(e) => setOperator(e.target.value)} placeholder="e.g. eugene" />
          </label>
          <label className="grid" style={{ gap: 4 }}>
            <span className="muted">Confirmation token (Stop / Flatten)</span>
            <input value={confirm} onChange={(e) => setConfirm(e.target.value)} type="password" />
          </label>
          <label className="grid" style={{ gap: 4, gridColumn: '1 / -1' }}>
            <span className="muted">Reason (required to clear a halt — it is stored)</span>
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
        </div>
      </Panel>

      <Panel title="Actions">
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          {ACTIONS.map((a) => (
            <button
              key={a.action}
              disabled={busy}
              onClick={() => send(a.action, a.reason)}
              style={a.danger ? { borderColor: '#6e2b28', color: '#f85149' } : undefined}
            >
              {a.label}
            </button>
          ))}
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          Pause blocks rebalances but never risk actions. Nothing here can increase exposure.
        </p>
        {result && <div style={{ marginTop: 10 }} className={result.startsWith('Queued') ? 'up' : 'error'}>{result}</div>}
      </Panel>
    </div>
  )
}
