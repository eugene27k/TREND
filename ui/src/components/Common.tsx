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

export function Tile({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className="panel tile">
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
