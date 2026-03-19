/**
 * Activity Log page — filterable event table with auto-refresh and CSV export.
 */
import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Loader2, PlayCircle, CheckCircle2, XCircle, AlertCircle,
  Download, RefreshCw, Filter,
} from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'

import * as api from '../lib/api'
import { useT } from '../lib/i18n'
import { formatDate } from '../lib/utils'
import { Card, CardContent } from '../components/ui/card'
import { Button } from '../components/ui/button'
import { Badge } from '../components/ui/badge'
import { Input } from '../components/ui/input'
import { Switch } from '../components/ui/switch'
import { Label } from '../components/ui/label'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '../components/ui/table'
import type { ActivityEvent } from '../types'


// ── Helpers ───────────────────────────────────────────────────────────────────

function levelBadgeVariant(level: string): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (level === 'error')   return 'destructive'
  if (level === 'warning') return 'outline'
  return 'secondary'
}

function EventIcon({ event, level }: { event: string; level: string }) {
  if (event === 'JOB_SUCCESS')
    return <CheckCircle2 className="h-4 w-4 text-green-500 shrink-0" />
  if (event === 'JOB_FAILED' || level === 'error')
    return <XCircle className="h-4 w-4 text-red-500 shrink-0" />
  if (level === 'warning')
    return <AlertCircle className="h-4 w-4 text-yellow-500 shrink-0" />
  return <PlayCircle className="h-4 w-4 text-blue-500 shrink-0" />
}

function exportCsv(events: ActivityEvent[]) {
  const header = 'Timestamp,Job ID,Event,Detail,Level\n'
  const rows = events.map(e =>
    [
      e.timestamp,
      e.job_id,
      e.event,
      `"${(e.detail ?? '').replace(/"/g, '""')}"`,
      e.level,
    ].join(','),
  )
  const csv = header + rows.join('\n')
  const blob = new Blob([csv], { type: 'text/csv' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `sentinel-activity-${new Date().toISOString().slice(0, 10)}.csv`
  a.click()
  URL.revokeObjectURL(url)
}


// ── Page ──────────────────────────────────────────────────────────────────────

export default function ActivityLog() {
  const { t } = useT()

  const [autoRefresh, setAutoRefresh] = useState(true)
  const [jobFilter, setJobFilter] = useState('')
  const [levelFilter, setLevelFilter] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [search, setSearch] = useState('')

  const { data: events = [], isLoading, error, dataUpdatedAt, refetch, isFetching } = useQuery({
    queryKey: ['system', 'activity'],
    queryFn: () => api.system.activity(),
    refetchInterval: autoRefresh ? 30_000 : false,
  })

  const { data: jobs = [] } = useQuery({
    queryKey: ['jobs'],
    queryFn: api.jobs.list,
  })

  const activityEvents = events as ActivityEvent[]

  // Derive unique job IDs from events for filter dropdown
  const jobIds = useMemo(() => {
    const ids = new Set(activityEvents.map(e => e.job_id))
    jobs.forEach(j => ids.add(j.job_id))
    return Array.from(ids).sort()
  }, [activityEvents, jobs])

  // Filter
  const filtered = useMemo(() => {
    return activityEvents.filter(e => {
      if (jobFilter && e.job_id !== jobFilter) return false
      if (levelFilter && e.level !== levelFilter) return false
      if (dateFrom && e.timestamp < dateFrom) return false
      if (dateTo && e.timestamp > dateTo + 'T23:59:59') return false
      if (search && !e.event.toLowerCase().includes(search.toLowerCase()) &&
          !e.detail?.toLowerCase().includes(search.toLowerCase())) return false
      return true
    })
  }, [activityEvents, jobFilter, levelFilter, dateFrom, dateTo, search])

  const lastUpdated = dataUpdatedAt
    ? formatDistanceToNow(new Date(dataUpdatedAt), { addSuffix: true })
    : '—'

  const hasFilters = !!(jobFilter || levelFilter || dateFrom || dateTo || search)

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">{t.activity.title}</h1>
          <p className="text-muted-foreground mt-1">
            {filtered.length} event{filtered.length !== 1 ? 's' : ''}
            {hasFilters && ' (filtered)'}
            {' · '}
            <span className="text-xs">updated {lastUpdated}</span>
          </p>
        </div>

        <div className="flex items-center gap-3">
          {/* Auto-refresh toggle */}
          <div className="flex items-center gap-2">
            <Switch
              id="auto-refresh"
              checked={autoRefresh}
              onCheckedChange={setAutoRefresh}
            />
            <Label htmlFor="auto-refresh" className="text-sm cursor-pointer">
              {t.activity.autoRefresh}
            </Label>
          </div>

          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            disabled={isFetching}
          >
            {isFetching
              ? <Loader2 className="h-4 w-4 animate-spin" />
              : <RefreshCw className="h-4 w-4" />
            }
          </Button>

          <Button
            variant="outline"
            size="sm"
            onClick={() => exportCsv(filtered)}
            disabled={filtered.length === 0}
          >
            <Download className="h-4 w-4 mr-2" />
            {t.activity.exportCsv}
          </Button>
        </div>
      </div>

      {/* ── Filter bar ── */}
      <Card>
        <CardContent className="p-4">
          <div className="flex items-center gap-2 flex-wrap">
            <Filter className="h-4 w-4 text-muted-foreground shrink-0" />

            {/* Search */}
            <Input
              placeholder={t.activity.searchPlaceholder}
              className="h-8 w-48"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />

            {/* Job filter */}
            <select
              className="h-8 rounded-md border border-input bg-background px-2 text-sm"
              value={jobFilter}
              onChange={e => setJobFilter(e.target.value)}
            >
              <option value="">{t.activity.allJobs}</option>
              {jobIds.map(id => (
                <option key={id} value={id}>{id}</option>
              ))}
            </select>

            {/* Level filter */}
            <select
              className="h-8 rounded-md border border-input bg-background px-2 text-sm"
              value={levelFilter}
              onChange={e => setLevelFilter(e.target.value)}
            >
              <option value="">{t.activity.allLevels}</option>
              <option value="info">{t.activity.info}</option>
              <option value="warning">{t.activity.warning}</option>
              <option value="error">{t.activity.error}</option>
            </select>

            {/* Date from */}
            <div className="flex items-center gap-1 text-sm text-muted-foreground">
              <span>{t.activity.from}</span>
              <Input
                type="date"
                className="h-8 w-36"
                value={dateFrom}
                onChange={e => setDateFrom(e.target.value)}
              />
            </div>

            {/* Date to */}
            <div className="flex items-center gap-1 text-sm text-muted-foreground">
              <span>{t.activity.to}</span>
              <Input
                type="date"
                className="h-8 w-36"
                value={dateTo}
                onChange={e => setDateTo(e.target.value)}
              />
            </div>

            {/* Clear filters */}
            {hasFilters && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setJobFilter('')
                  setLevelFilter('')
                  setDateFrom('')
                  setDateTo('')
                  setSearch('')
                }}
              >
                Clear
              </Button>
            )}
          </div>
        </CardContent>
      </Card>

      {/* ── Events table ── */}
      <Card>
        {isLoading ? (
          <CardContent className="flex items-center justify-center py-16">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </CardContent>
        ) : error ? (
          <CardContent className="py-12 text-center">
            <AlertCircle className="h-6 w-6 text-destructive mx-auto mb-2" />
            <p className="text-sm text-destructive">{String(error)}</p>
          </CardContent>
        ) : filtered.length === 0 ? (
          <CardContent className="py-16 text-center text-muted-foreground">
            {hasFilters ? t.activity.noEvents : t.activity.noEvents}
          </CardContent>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead>{t.activity.timestamp}</TableHead>
                <TableHead>{t.activity.job}</TableHead>
                <TableHead>{t.activity.event}</TableHead>
                <TableHead>{t.activity.detail}</TableHead>
                <TableHead>{t.activity.level}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((ev, idx) => (
                <TableRow key={idx} className="hover:bg-muted/30">
                  <TableCell className="py-2">
                    <EventIcon event={ev.event} level={ev.level} />
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground py-2">
                    <div>{formatDate(ev.timestamp)}</div>
                    <div className="text-xs">
                      {formatDistanceToNow(new Date(ev.timestamp), { addSuffix: true })}
                    </div>
                  </TableCell>
                  <TableCell className="py-2">
                    <span className="font-mono text-xs bg-muted px-1.5 py-0.5 rounded">
                      {ev.job_id}
                    </span>
                  </TableCell>
                  <TableCell className="py-2">
                    <span className="text-sm font-medium">
                      {ev.event.replace(/_/g, ' ')}
                    </span>
                  </TableCell>
                  <TableCell className="py-2 max-w-xs">
                    <p className="text-sm text-muted-foreground truncate" title={ev.detail}>
                      {ev.detail || '—'}
                    </p>
                  </TableCell>
                  <TableCell className="py-2">
                    <Badge variant={levelBadgeVariant(ev.level)} className="capitalize text-xs">
                      {ev.level}
                    </Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>
    </div>
  )
}
