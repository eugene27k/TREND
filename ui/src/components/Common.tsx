import type { ReactNode } from 'react'
import { useEffect, useState } from 'react'
import { ApiError } from '../api'

export function Panel({ title, children, right }: { title?: string; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="panel">
      {title && (
        <h2 style={{ display: 'flex', alignItems: 'center' }}>
          <span>{title}</span>
          <span style={{ flex: 1 }} />
          {right}
        </h2>
      )}
      {children}
    </section>
  )
}

export function Tile({
  label,
  value,
  sub,
  tone,
  stale,
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  tone?: string
  /** PRD Section 10.5: too few observations greys the tile, it never hides it. */
  stale?: boolean
}) {
  return (
    <div className={`panel tile${stale ? ' stale' : ''}`}>
      <div className="label">{label}</div>
      <div className={`value ${tone ?? ''}`}>{value}</div>
      {sub !== undefined && <div className="sub">{sub}</div>}
    </div>
  )
}

export function StatusPill({ status }: { status: string }) {
  const s = (status || '').toLowerCase()
  const tone = s === 'green' ? 'green' : s === 'amber' ? 'amber' : s === 'red' ? 'red' : ''
  return <span className={`pill ${tone}`}>{status || 'unknown'}</span>
}

export function Empty({ what }: { what: string }) {
  return <div className="empty">No {what} yet.</div>
}

/** A labelled bar, used for cap utilisation and any other 0-1 fraction. */
export function Meter({ label, right, frac, breached }: { label: ReactNode; right?: ReactNode; frac: number; breached?: boolean }) {
  const f = Number.isFinite(frac) ? Math.max(0, Math.min(1, frac)) : 0
  const tone = breached || f >= 1 ? 'var(--down)' : f >= 0.8 ? 'var(--warn)' : 'var(--up)'
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', fontSize: 12, marginBottom: 4 }}>
        <span className="muted">{label}</span>
        <span style={{ flex: 1 }} />
        <span>{right}</span>
      </div>
      <div style={{ height: 6, background: 'var(--panel-2)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ width: `${f * 100}%`, height: '100%', background: tone }} />
      </div>
    </div>
  )
}

/** Chart chrome shared by every recharts panel, so the dark theme stays in one place. */
export const AXIS = { stroke: '#8b97a6', fontSize: 11 } as const
export const GRID = { stroke: '#2a323d' } as const
export const TIP = { background: '#161b22', border: '1px solid #2a323d', borderRadius: 8, fontSize: 12 } as const
export const COLORS = {
  accent: '#58a6ff',
  up: '#3fb950',
  down: '#f85149',
  warn: '#d29922',
  muted: '#8b97a6',
} as const

/** Load once per dependency change, with the request cancelled on unmount. */
export function useApi<T>(fn: (signal: AbortSignal) => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const ctrl = new AbortController()
    setLoading(true)
    setError(null)
    fn(ctrl.signal)
      .then((d) => setData(d))
      .catch((e: unknown) => {
        if (ctrl.signal.aborted) return
        setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e))
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false)
      })
    return () => ctrl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, error, loading }
}

export function Loader({ error, loading, children }: { error: string | null; loading: boolean; children: ReactNode }) {
  if (error) return <div className="error">{error}</div>
  if (loading) return <div className="empty">Loading…</div>
  return <>{children}</>
}
