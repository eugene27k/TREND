// One place for every number that reaches the screen. A missing value renders
// as "n/a" — never as 0, which would read as a real measurement.

export const NA = 'n/a'

export function num(v: number | null | undefined, dp = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return NA
  return v.toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp })
}

export function usd(v: number | null | undefined, dp = 2): string {
  return v === null || v === undefined || !Number.isFinite(v) ? NA : `${num(v, dp)}`
}

export function pct(v: number | null | undefined, dp = 1): string {
  return v === null || v === undefined || !Number.isFinite(v) ? NA : `${num(v * 100, dp)} %`
}

export function pctPoints(v: number | null | undefined, dp = 1): string {
  return v === null || v === undefined || !Number.isFinite(v) ? NA : `${num(v, dp)} %`
}

export function mult(v: number | null | undefined, dp = 2): string {
  return v === null || v === undefined || !Number.isFinite(v) ? NA : `${num(v, dp)}×`
}

export function signed(v: number | null | undefined, dp = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return NA
  return (v >= 0 ? '+' : '') + num(v, dp)
}

export function cls(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return 'muted'
  return v > 0 ? 'up' : v < 0 ? 'down' : ''
}

export function ts(ms: number | null | undefined): string {
  if (!ms) return NA
  return new Date(ms).toISOString().replace('T', ' ').slice(0, 19)
}

/** PRD Section 10.5: a metric with too few observations is shown greyed, not hidden. */
export function isStale(nObs: number, min = 20): boolean {
  return nObs < min
}
