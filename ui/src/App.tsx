import { useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import type { Strategy } from './api'
import { api } from './api'
import { useApi } from './components/Common'
import { AllSleeves } from './pages/AllSleeves'
import { Attribution } from './pages/Attribution'
import { Backtest } from './pages/Backtest'
import { Controls } from './pages/Controls'
import { Metrics } from './pages/Metrics'
import { Operations } from './pages/Operations'
import { Overview } from './pages/Overview'
import { Positions } from './pages/Positions'
import { Rebalances } from './pages/Rebalances'
import { Signals } from './pages/Signals'
import { Universe } from './pages/Universe'

const PAGES = [
  { path: 'overview', label: 'Overview' },
  { path: 'signals', label: 'Signals' },
  { path: 'positions', label: 'Positions & risk' },
  { path: 'rebalances', label: 'Rebalances' },
  { path: 'attribution', label: 'Attribution' },
  { path: 'metrics', label: 'Metrics' },
  { path: 'universe', label: 'Universe' },
  { path: 'backtest', label: 'Backtest' },
  { path: 'operations', label: 'Operations' },
  { path: 'controls', label: 'Controls' },
]

export function App() {
  const { data } = useApi((s) => api.strategies(s), [])
  const available = data?.strategies ?? ['TREND']
  const [strategy, setStrategy] = useState<Strategy>('TREND')
  const active = available.includes(strategy) ? strategy : available[0]

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">AEGIS</div>
        <nav className="nav">
          <NavLink to="/all" className={({ isActive }) => (isActive ? 'active' : '')}>All sleeves</NavLink>
          {PAGES.map((p) => (
            <NavLink key={p.path} to={`/${p.path}`} className={({ isActive }) => (isActive ? 'active' : '')}>
              {p.label}
            </NavLink>
          ))}
        </nav>
      </aside>

      <main className="main">
        <div className="topbar">
          <h1>{active}</h1>
          <select value={active} onChange={(e) => setStrategy(e.target.value as Strategy)}>
            {available.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <div className="spacer" />
        </div>

        <Routes>
          <Route path="/" element={<Navigate to="/overview" replace />} />
          <Route path="/all" element={<AllSleeves />} />
          <Route path="/overview" element={<Overview strategy={active} />} />
          <Route path="/signals" element={<Signals strategy={active} />} />
          <Route path="/positions" element={<Positions strategy={active} />} />
          <Route path="/rebalances" element={<Rebalances strategy={active} />} />
          <Route path="/attribution" element={<Attribution strategy={active} />} />
          <Route path="/metrics" element={<Metrics strategy={active} />} />
          <Route path="/universe" element={<Universe strategy={active} />} />
          <Route path="/backtest" element={<Backtest strategy={active} />} />
          <Route path="/operations" element={<Operations strategy={active} />} />
          <Route path="/controls" element={<Controls strategy={active} />} />
          <Route path="*" element={<Navigate to="/overview" replace />} />
        </Routes>
      </main>
    </div>
  )
}
