/**
 * Dashboard — aggregate overview + per-job status cards.
 *
 * Rules-of-Hooks compliance:
 *  - JobDashCard is a proper component: hooks called unconditionally inside it.
 *  - Aggregate storage figures are fetched in a single useQuery (Promise.all),
 *    never in a .map() loop.
 */
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  HardDrive, Camera, Briefcase, TrendingUp,
  AlertCircle, Clock, ChevronRight,
  PlayCircle, CheckCircle2, XCircle, Calendar,
  Activity,
} from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'

import * as api from '../lib/api'
import { useT } from '../lib/i18n'
import { formatBytes, formatDate, statusVariant } from '../lib/utils'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import type { ActivityEvent, JobConfigResponse, ScheduleResponse, StorageStatsResponse } from '../types'


// ── Aggregate stats helper (single query — no hooks in loops) ─────────────────

function useAggregateStats(jobs: JobConfigResponse[]) {
  return useQuery({
    queryKey: ['storage', 'aggregate', jobs.map(j => j.job_id).join(',')],
    queryFn: async () => {
      if (!jobs.length) return { totalOriginal: 0, totalCompressed: 0, avgDedup: 0 }
      const results = await Promise.allSettled(
        jobs.map(j => api.storage.stats(j.job_id)),
      )
      const valid = results
        .filter((r): r is PromiseFulfilledResult<StorageStatsResponse> =>
          r.status === 'fulfilled')
        .map(r => r.value)
      return {
        totalOriginal:   valid.reduce((s, x) => s + x.total_original_bytes, 0),
        totalCompressed: valid.reduce((s, x) => s + x.total_compressed_bytes, 0),
        avgDedup: valid.length
          ? valid.reduce((s, x) => s + x.dedup_ratio, 0) / valid.length
          : 0,
      }
    },
    enabled: jobs.length > 0,
  })
}


// ── Stat card ─────────────────────────────────────────────────────────────────

function StatCard({
  icon: Icon, label, value, sub,
}: {
  icon: React.ElementType
  label: string
  value: string | number
  sub?: string
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardDescription>{label}</CardDescription>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold">{value}</div>
        {sub && <p className="text-xs text-muted-foreground mt-1">{sub}</p>}
      </CardContent>
    </Card>
  )
}


// ── Per-job dashboard card (hooks live inside this component — correct) ────────

function JobDashCard({ job }: { job: JobConfigResponse }) {
  const { t } = useT()

  const { data: run } = useQuery({
    queryKey: ['run', 'latest', job.job_id],
    queryFn: () => api.jobs.latestRun(job.job_id).catch(() => null),
    refetchInterval: 15_000,
  })

  const { data: stats } = useQuery({
    queryKey: ['storage', 'stats', job.job_id],
    queryFn: () => api.storage.stats(job.job_id),
  })

  return (
    <Card className="hover:shadow-md transition-shadow">
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-base truncate">{job.job_id}</CardTitle>
          {run && (
            <Badge variant={statusVariant(run.status)}>{run.status}</Badge>
          )}
        </div>
        <CardDescription className="truncate">
          {job.source_paths[0]}
          {job.source_paths.length > 1 && ` +${job.source_paths.length - 1} more`}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-1 text-sm text-muted-foreground">
        <div className="flex justify-between">
          <span>Provider</span>
          <span className="font-mono text-xs text-foreground">{job.storage.provider}</span>
        </div>
        {stats && (
          <>
            <div className="flex justify-between">
              <span>{t.dashboard.recentSnapshots}</span>
              <span className="text-foreground">{stats.snapshot_count}</span>
            </div>
            <div className="flex justify-between">
              <span>{t.dashboard.uploaded}</span>
              <span className="text-foreground">
                {formatBytes(stats.total_compressed_bytes)}
              </span>
            </div>
            <div className="flex justify-between">
              <span>{t.dashboard.deduped}</span>
              <span className="text-foreground">{(stats.dedup_ratio * 100).toFixed(1)}%</span>
            </div>
          </>
        )}
        {run?.finished_at && (
          <div className="flex items-center gap-1 pt-1 border-t">
            <Clock className="h-3 w-3" />
            <span className="text-xs">{formatDate(run.finished_at)}</span>
          </div>
        )}
        {run && (
          <div className="flex items-center gap-2 text-xs pt-0.5">
            <span>{run.files_processed} {t.dashboard.files}</span>
            <ChevronRight className="h-3 w-3" />
            <span>{run.chunks_uploaded} up</span>
            <ChevronRight className="h-3 w-3" />
            <span>{run.chunks_deduped} ded</span>
          </div>
        )}
      </CardContent>
    </Card>
  )
}


// ── Activity event icon ───────────────────────────────────────────────────────

function ActivityIcon({ event, level }: { event: string; level: string }) {
  if (event === 'JOB_SUCCESS')
    return <CheckCircle2 className="h-4 w-4 text-green-500 shrink-0 mt-0.5" />
  if (event === 'JOB_FAILED' || level === 'error')
    return <XCircle className="h-4 w-4 text-red-500 shrink-0 mt-0.5" />
  if (level === 'warning')
    return <AlertCircle className="h-4 w-4 text-yellow-500 shrink-0 mt-0.5" />
  return <PlayCircle className="h-4 w-4 text-blue-500 shrink-0 mt-0.5" />
}


// ── Recent Activity Feed ──────────────────────────────────────────────────────

function ActivityFeed() {
  const { t } = useT()

  const { data: activities = [], isLoading } = useQuery({
    queryKey: ['system', 'activity'],
    queryFn: () => api.system.activity(),
    refetchInterval: 30_000,
  })

  const recent = (activities as ActivityEvent[]).slice(0, 10)

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xl font-semibold">{t.dashboard.recentActivity}</h2>
        <Button asChild variant="outline" size="sm">
          <Link to="/activity">{t.common.viewAll} →</Link>
        </Button>
      </div>

      <Card>
        {isLoading ? (
          <CardContent className="py-8 flex items-center justify-center">
            <Activity className="h-5 w-5 animate-pulse text-muted-foreground" />
          </CardContent>
        ) : recent.length === 0 ? (
          <CardContent className="py-8 text-center text-muted-foreground text-sm">
            {t.dashboard.noActivity}
          </CardContent>
        ) : (
          <CardContent className="p-0 divide-y">
            {recent.map((ev, idx) => (
              <div key={idx} className="flex items-start gap-3 px-4 py-3 hover:bg-muted/40 transition-colors">
                <ActivityIcon event={ev.event} level={ev.level} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium truncate">{ev.event.replace(/_/g, ' ')}</span>
                    <span className="font-mono text-xs bg-muted px-1.5 py-0.5 rounded text-muted-foreground">
                      {ev.job_id}
                    </span>
                  </div>
                  {ev.detail && (
                    <p className="text-xs text-muted-foreground mt-0.5 truncate">{ev.detail}</p>
                  )}
                </div>
                <span className="text-xs text-muted-foreground shrink-0 pt-0.5">
                  {formatDistanceToNow(new Date(ev.timestamp), { addSuffix: true })}
                </span>
              </div>
            ))}
          </CardContent>
        )}
      </Card>
    </div>
  )
}


// ── Next Scheduled Backups ────────────────────────────────────────────────────

function countdown(isoString: string): string {
  const ms = new Date(isoString).getTime() - Date.now()
  if (ms <= 0) return 'overdue'
  const totalMinutes = Math.floor(ms / 60_000)
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  if (hours >= 24) {
    const days = Math.floor(hours / 24)
    return `in ${days}d ${hours % 24}h`
  }
  if (hours > 0) return `in ${hours}h ${minutes}m`
  return `in ${minutes}m`
}

function ScheduledSection() {
  const { t } = useT()

  const { data: schedules = [] } = useQuery({
    queryKey: ['scheduler'],
    queryFn: () => api.scheduler.list(),
    refetchInterval: 60_000,
  })

  const upcoming = (schedules as ScheduleResponse[])
    .filter(s => s.enabled && s.next_run_at)
    .sort((a, b) => a.next_run_at!.localeCompare(b.next_run_at!))
    .slice(0, 5)

  if (upcoming.length === 0) return null

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xl font-semibold">{t.dashboard.upcomingBackups}</h2>
        <Button asChild variant="outline" size="sm">
          <Link to="/scheduler">{t.common.viewAll} →</Link>
        </Button>
      </div>

      <Card>
        <CardContent className="p-0 divide-y">
          {upcoming.map(s => (
            <div key={s.job_id} className="flex items-center gap-3 px-4 py-3 hover:bg-muted/40 transition-colors">
              <Calendar className="h-4 w-4 text-primary shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium font-mono">{s.job_id}</p>
                {s.cron && (
                  <p className="text-xs text-muted-foreground font-mono">{s.cron}</p>
                )}
              </div>
              <div className="text-right shrink-0">
                <p className="text-sm font-semibold text-primary">
                  {countdown(s.next_run_at!)}
                </p>
                <p className="text-xs text-muted-foreground">
                  {formatDate(s.next_run_at!)}
                </p>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}


// ── Page ──────────────────────────────────────────────────────────────────────

export default function Dashboard() {
  const { t } = useT()

  const { data: jobList = [], isLoading } = useQuery({
    queryKey: ['jobs'],
    queryFn: api.jobs.list,
  })

  const { data: allSnapshots = [] } = useQuery({
    queryKey: ['snapshots'],
    queryFn: () => api.snapshots.list(),
    refetchInterval: 30_000,
  })

  const { data: agg } = useAggregateStats(jobList)

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-muted-foreground animate-pulse">{t.common.loading}</p>
      </div>
    )
  }

  return (
    <div className="p-8 space-y-8">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">{t.dashboard.title}</h1>
        <p className="text-muted-foreground mt-1">{t.dashboard.subtitle}</p>
      </div>

      {/* ── Aggregate stats ── */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard icon={Briefcase} label={t.dashboard.configuredJobs} value={jobList.length} />
        <StatCard
          icon={Camera}
          label={t.dashboard.totalSnapshots}
          value={allSnapshots.length}
          sub={`${allSnapshots.filter(s => s.status === 'completed').length} ${t.status.completed.toLowerCase()}`}
        />
        <StatCard
          icon={HardDrive}
          label={t.dashboard.dataProtected}
          value={agg ? formatBytes(agg.totalOriginal) : '…'}
          sub={agg ? `${formatBytes(agg.totalCompressed)} stored` : undefined}
        />
        <StatCard
          icon={TrendingUp}
          label={t.dashboard.avgDedup}
          value={agg ? `${(agg.avgDedup * 100).toFixed(1)}%` : '…'}
          sub="chunks saved by deduplication"
        />
      </div>

      {/* ── Upcoming scheduled backups ── */}
      <ScheduledSection />

      {/* ── Job status grid ── */}
      <div>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold">{t.nav.jobs}</h2>
          <Button asChild variant="outline" size="sm">
            <Link to="/jobs">{t.common.viewAll} →</Link>
          </Button>
        </div>

        {jobList.length === 0 ? (
          <Card>
            <CardContent className="flex flex-col items-center justify-center py-12 gap-3">
              <AlertCircle className="h-8 w-8 text-muted-foreground" />
              <p className="text-muted-foreground text-sm">
                {t.dashboard.noJobsDesc}
              </p>
            </CardContent>
          </Card>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {/* Each child is a component — hooks inside are valid */}
            {jobList.map(job => <JobDashCard key={job.job_id} job={job} />)}
          </div>
        )}
      </div>

      {/* ── Recent Activity Feed ── */}
      <ActivityFeed />

      {/* ── Recent snapshots table ── */}
      {allSnapshots.length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-semibold">{t.dashboard.recentSnapshots}</h2>
            <Button asChild variant="outline" size="sm">
              <Link to="/snapshots">{t.common.viewAll} →</Link>
            </Button>
          </div>
          <Card>
            <CardContent className="p-0">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b text-left text-muted-foreground">
                    <th className="px-4 py-3 font-medium">{t.dashboard.job}</th>
                    <th className="px-4 py-3 font-medium">{t.dashboard.date}</th>
                    <th className="px-4 py-3 font-medium">{t.dashboard.status}</th>
                    <th className="px-4 py-3 font-medium text-right">{t.dashboard.size}</th>
                  </tr>
                </thead>
                <tbody>
                  {allSnapshots.slice(0, 8).map(s => (
                    <tr key={s.snapshot_id} className="border-b last:border-0 hover:bg-muted/50">
                      <td className="px-4 py-3 font-mono text-xs">{s.job_id}</td>
                      <td className="px-4 py-3 text-muted-foreground">{formatDate(s.started_at)}</td>
                      <td className="px-4 py-3">
                        <Badge variant={statusVariant(s.status)}>{s.status}</Badge>
                      </td>
                      <td className="px-4 py-3 text-right">
                        {s.total_bytes ? formatBytes(s.total_bytes) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  )
}
