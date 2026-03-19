import { Outlet } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, Moon, Sun } from 'lucide-react'
import { useState, useEffect } from 'react'
import Sidebar from './Sidebar'
import { system, jobs } from '../../lib/api'
import { useT } from '../../lib/i18n'

// ── Health dot ────────────────────────────────────────────────────────────────

function HealthDot() {
  const { t } = useT()
  const { data, isError } = useQuery({
    queryKey: ['healthz'],
    queryFn: system.healthz,
    refetchInterval: 30_000,
    retry: 1,
  })

  const color = isError
    ? 'bg-red-500'
    : data?.status === 'ok'
      ? 'bg-green-500'
      : 'bg-yellow-400'

  return (
    <div className="flex items-center gap-1.5">
      <span className={`inline-block h-2 w-2 rounded-full ${color}`} />
      <span className="text-xs text-muted-foreground">
        {isError ? t.layout.offline : t.layout.online}
      </span>
    </div>
  )
}

// ── Active job indicator ───────────────────────────────────────────────────────

function ActiveJobIndicator() {
  const { t } = useT()
  const { data: jobList } = useQuery({
    queryKey: ['jobs'],
    queryFn: jobs.list,
    refetchInterval: 10_000,
  })

  // Check if any job has a running latest run (best-effort via parallel queries)
  const [hasActive, setHasActive] = useState(false)

  useEffect(() => {
    if (!jobList?.length) { setHasActive(false); return }
    let active = false
    const checks = jobList.map(async (j) => {
      try {
        const run = await jobs.latestRun(j.job_id)
        if (run.status === 'running' || run.status === 'queued') active = true
      } catch { /* no run yet */ }
    })
    Promise.all(checks).then(() => setHasActive(active))
  }, [jobList])

  if (!hasActive) return null

  return (
    <div className="flex items-center gap-1.5 text-primary">
      <Loader2 className="h-4 w-4 animate-spin" />
      <span className="text-xs font-medium">{t.layout.backupRunning}</span>
    </div>
  )
}

// ── Theme toggle ──────────────────────────────────────────────────────────────

function ThemeToggle() {
  const { t } = useT()
  const [dark, setDark] = useState(() =>
    typeof window !== 'undefined'
      ? document.documentElement.classList.contains('dark')
      : false,
  )

  function toggle() {
    const next = !dark
    setDark(next)
    document.documentElement.classList.toggle('dark', next)
    localStorage.setItem('sentinel-theme', next ? 'dark' : 'light')
  }

  return (
    <button
      onClick={toggle}
      className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors"
      title={t.layout.toggleTheme}
    >
      {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
    </button>
  )
}

// ── AppLayout ─────────────────────────────────────────────────────────────────

export default function AppLayout() {
  // Apply saved theme on mount
  useEffect(() => {
    const saved = localStorage.getItem('sentinel-theme')
    if (saved === 'dark') {
      document.documentElement.classList.add('dark')
    }
  }, [])

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Top header bar */}
        <header className="flex h-12 shrink-0 items-center gap-3 border-b bg-card px-4">
          <HealthDot />
          <ActiveJobIndicator />
          <div className="flex-1" />
          <ThemeToggle />
        </header>
        <main className="flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
