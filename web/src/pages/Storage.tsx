/**
 * Storage page — per-job analytics with Recharts bar chart.
 *
 * Rules-of-Hooks compliance:
 *  - useAllStats fetches all jobs' stats in ONE query (Promise.all) — no loops.
 *  - JobStorageCard is a proper component: its own useQuery hooks are valid.
 *  - HealthBadge is a proper component: its own useQuery is valid.
 */
import { useQuery } from '@tanstack/react-query'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend,
} from 'recharts'
import { Loader2, CheckCircle2, XCircle, Database, HardDrive, TrendingUp, Layers } from 'lucide-react'

import * as api from '../lib/api'
import { useT } from '../lib/i18n'
import { formatBytes } from '../lib/utils'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Badge } from '../components/ui/badge'
import type { JobConfigResponse, StorageStatsResponse } from '../types'


// ── Single query for all jobs' stats (used by chart + summary) ─────────────────

function useAllStats(jobs: JobConfigResponse[]) {
  return useQuery<(StorageStatsResponse | null)[]>({
    queryKey: ['storage', 'all-stats', jobs.map(j => j.job_id).join(',')],
    queryFn: () =>
      Promise.all(
        jobs.map(j => api.storage.stats(j.job_id).catch(() => null)),
      ),
    enabled: jobs.length > 0,
  })
}


// ── Global summary stat card ──────────────────────────────────────────────────

function SummaryCard({
  icon: Icon,
  label,
  value,
  sub,
  accent,
}: {
  icon: React.ElementType
  label: string
  value: string
  sub?: string
  accent?: boolean
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardDescription>{label}</CardDescription>
        <Icon className={`h-4 w-4 ${accent ? 'text-primary' : 'text-muted-foreground'}`} />
      </CardHeader>
      <CardContent>
        <div className={`text-2xl font-bold ${accent ? 'text-primary' : ''}`}>{value}</div>
        {sub && <p className="text-xs text-muted-foreground mt-1">{sub}</p>}
      </CardContent>
    </Card>
  )
}


// ── Custom Recharts tooltip ────────────────────────────────────────────────────

function ChartTooltip({ active, payload, label }: {
  active?: boolean
  payload?: Array<{ name: string; value: number; fill: string }>
  label?: string
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-lg border bg-card p-3 shadow-lg text-sm space-y-1">
      <p className="font-semibold">{label}</p>
      {payload.map(p => (
        <div key={p.name} className="flex items-center gap-2">
          <span className="inline-block h-2 w-2 rounded-full" style={{ background: p.fill }} />
          <span className="text-muted-foreground">{p.name}:</span>
          <span className="font-medium">{formatBytes(p.value)}</span>
        </div>
      ))}
    </div>
  )
}


// ── Health badge (component — hooks inside are valid) ─────────────────────────

function HealthBadge({ jobId }: { jobId: string }) {
  const { t } = useT()

  const { data, isLoading } = useQuery({
    queryKey: ['storage', 'health', jobId],
    queryFn: () => api.storage.health(jobId),
    refetchInterval: 60_000,
  })

  if (isLoading) return <span className="text-xs text-muted-foreground">{t.common.loading}</span>
  if (!data) return null

  return data.status === 'ok' ? (
    <Badge variant="default" className="gap-1">
      <CheckCircle2 className="h-3 w-3" /> {t.storage.healthy}
    </Badge>
  ) : (
    <Badge variant="destructive" className="gap-1">
      <XCircle className="h-3 w-3" /> {t.storage.degraded}
    </Badge>
  )
}


// ── Per-job stats card (component — hooks inside are valid) ───────────────────

function JobStorageCard({ job }: { job: JobConfigResponse }) {
  const { t } = useT()

  const { data: stats, isLoading } = useQuery({
    queryKey: ['storage', 'stats', job.job_id],
    queryFn: () => api.storage.stats(job.job_id),
  })

  const savings = stats
    ? stats.total_original_bytes - stats.total_compressed_bytes
    : 0
  const compressionRatio =
    stats && stats.total_original_bytes > 0
      ? stats.total_compressed_bytes / stats.total_original_bytes
      : 1

  function Row({ label, value, highlight }: { label: string; value: string | number; highlight?: boolean }) {
    return (
      <div className="flex items-center justify-between py-2 text-sm border-b last:border-0">
        <span className="text-muted-foreground">{label}</span>
        <span className={`font-semibold ${highlight ? 'text-primary' : ''}`}>{value}</span>
      </div>
    )
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">{job.job_id}</CardTitle>
            <CardDescription className="flex items-center gap-1 mt-0.5">
              <Database className="h-3 w-3" />
              {job.storage.provider}
              {job.storage.base_path && (
                <span className="font-mono text-xs truncate max-w-[120px]">
                  {' '}{job.storage.base_path}
                </span>
              )}
              {job.storage.bucket && (
                <span className="font-mono text-xs"> s3://{job.storage.bucket}</span>
              )}
            </CardDescription>
          </div>
          <HealthBadge jobId={job.job_id} />
        </div>
      </CardHeader>

      <CardContent>
        {isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : stats ? (
          <>
            <Row label={t.storage.snapshots}        value={stats.snapshot_count} />
            <Row label={t.storage.uniqueChunks}     value={stats.total_chunks.toLocaleString()} />
            <Row label={t.storage.originalData}     value={formatBytes(stats.total_original_bytes)} />
            <Row label={t.storage.storedData}       value={formatBytes(stats.total_compressed_bytes)} />
            <Row label={t.storage.savedData}        value={savings > 0 ? formatBytes(savings) : '—'} highlight={savings > 0} />
            <Row label={t.storage.dedup}            value={`${(stats.dedup_ratio * 100).toFixed(1)}%`} />
            <Row label={t.storage.compression}      value={`${(compressionRatio * 100).toFixed(1)}%`} />
          </>
        ) : (
          <p className="text-sm text-muted-foreground py-4 text-center">{t.storage.noData}</p>
        )}
      </CardContent>
    </Card>
  )
}


// ── Page ──────────────────────────────────────────────────────────────────────

export default function Storage() {
  const { t } = useT()

  const { data: jobs = [], isLoading } = useQuery({
    queryKey: ['jobs'],
    queryFn: api.jobs.list,
  })

  // Single query — all stats fetched via Promise.all, no hooks in loops.
  const { data: allStats = [] } = useAllStats(jobs)

  // ── Global summary computation ──────────────────────────────────────────
  const validStats = allStats.filter((s): s is StorageStatsResponse => s !== null)
  const totalOriginal   = validStats.reduce((sum, s) => sum + s.total_original_bytes, 0)
  const totalCompressed = validStats.reduce((sum, s) => sum + s.total_compressed_bytes, 0)
  const totalSaved      = totalOriginal - totalCompressed
  const avgDedup        = validStats.length
    ? validStats.reduce((sum, s) => sum + s.dedup_ratio, 0) / validStats.length
    : 0
  const avgCompression  = totalOriginal > 0 ? totalCompressed / totalOriginal : 1
  const hasStats        = totalOriginal > 0

  const chartData = jobs.map((j, i) => ({
    name: j.job_id,
    [t.storage.original]: allStats[i]?.total_original_bytes ?? 0,
    [t.storage.stored]:   allStats[i]?.total_compressed_bytes ?? 0,
    [t.storage.saved]:    Math.max(0, (allStats[i]?.total_original_bytes ?? 0) - (allStats[i]?.total_compressed_bytes ?? 0)),
  }))

  const hasChartData = allStats.some(s => s && s.total_original_bytes > 0)

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    )
  }

  return (
    <div className="p-8 space-y-8">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">{t.storage.title}</h1>
        <p className="text-muted-foreground mt-1">
          {t.storage.subtitle}
        </p>
      </div>

      {/* ── Global summary ── */}
      {hasStats && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <SummaryCard
            icon={HardDrive}
            label={t.storage.totalProtected}
            value={formatBytes(totalOriginal)}
            sub={`${validStats.length} job${validStats.length !== 1 ? 's' : ''}`}
          />
          <SummaryCard
            icon={Database}
            label={t.storage.physicalStored}
            value={formatBytes(totalCompressed)}
            sub="after compression + dedup"
          />
          <SummaryCard
            icon={Layers}
            label={t.storage.spaceSaved}
            value={formatBytes(totalSaved)}
            sub={`${totalOriginal > 0 ? ((totalSaved / totalOriginal) * 100).toFixed(1) : '0'}% reduction`}
            accent={totalSaved > 0}
          />
          <SummaryCard
            icon={TrendingUp}
            label={t.storage.avgDedup}
            value={`${(avgDedup * 100).toFixed(1)}%`}
            sub={`${(avgCompression * 100).toFixed(1)}% storage ratio`}
          />
        </div>
      )}

      {/* ── Chart ── */}
      {jobs.length > 0 && hasChartData && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">{t.storage.chartTitle}</CardTitle>
            <CardDescription>
              Saved = space recovered by Zstd compression + Rabin-CDC deduplication
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={280}>
              <BarChart data={chartData} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
                <XAxis dataKey="name" tick={{ fontSize: 12 }} />
                <YAxis
                  tickFormatter={v => formatBytes(v as number, 0)}
                  tick={{ fontSize: 11 }}
                  width={80}
                />
                <Tooltip content={<ChartTooltip />} />
                <Legend />
                <Bar dataKey={t.storage.original} fill="hsl(240 3.8% 60%)"  radius={[3, 3, 0, 0]} />
                <Bar dataKey={t.storage.stored}   fill="hsl(240 5.9% 30%)"  radius={[3, 3, 0, 0]} />
                <Bar dataKey={t.storage.saved}    fill="hsl(142 71% 45%)"   radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      )}

      {/* ── Per-job cards — each is a component, no hooks in loops ── */}
      {jobs.length === 0 ? (
        <Card>
          <CardContent className="py-16 text-center text-muted-foreground">
            {t.storage.noJobs}
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {jobs.map(j => <JobStorageCard key={j.job_id} job={j} />)}
        </div>
      )}
    </div>
  )
}
