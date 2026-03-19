/**
 * Scheduler page — manage cron schedules for all backup jobs.
 *
 * Features:
 *  - Schedule table with enable/disable toggle, inline cron editor
 *  - cronstrue live human-readable preview
 *  - Next run countdown
 *  - Simple month calendar showing scheduled days
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import cronstrue from 'cronstrue'
import {
  Loader2, Calendar, Clock, CheckCircle2, XCircle,
  AlertCircle, Pencil, Check, X,
} from 'lucide-react'

import * as api from '../lib/api'
import { useT } from '../lib/i18n'
import { formatDate, statusVariant } from '../lib/utils'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Button } from '../components/ui/button'
import { Badge } from '../components/ui/badge'
import { Switch } from '../components/ui/switch'
import { Input } from '../components/ui/input'
import type { ScheduleResponse } from '../types'


// ── Helpers ───────────────────────────────────────────────────────────────────

function describeCron(cron: string): string {
  if (!cron?.trim()) return '—'
  try {
    return cronstrue.toString(cron, { throwExceptionOnParseError: true })
  } catch {
    return '⚠ Invalid cron'
  }
}

function countdown(isoString: string | undefined): string {
  if (!isoString) return '—'
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

/** Return which day-of-month values fire for a cron expr in the given month/year. */
function cronFiringDays(cron: string, year: number, month: number): Set<number> {
  const days = new Set<number>()
  if (!cron?.trim()) return days
  try {
    const fields = cron.trim().split(/\s+/)
    if (fields.length < 5) return days
    const [, , domField, , dowField] = fields
    const daysInMonth = new Date(year, month + 1, 0).getDate()

    // day-of-week: 0=Sun…6=Sat
    const validDow = new Set<number>()
    if (dowField !== '*' && dowField !== '?') {
      for (const part of dowField.split(',')) {
        if (part.includes('-')) {
          const [s, e] = part.split('-').map(Number)
          for (let d = s; d <= e; d++) validDow.add(d)
        } else if (part.includes('/')) {
          const [start, step] = part.split('/').map(Number)
          for (let d = (start || 0); d <= 6; d += step) validDow.add(d)
        } else {
          validDow.add(Number(part))
        }
      }
    }

    // day-of-month
    const validDom = new Set<number>()
    if (domField !== '*' && domField !== '?') {
      for (const part of domField.split(',')) {
        if (part.includes('-')) {
          const [s, e] = part.split('-').map(Number)
          for (let d = s; d <= e; d++) validDom.add(d)
        } else {
          validDom.add(Number(part))
        }
      }
    }

    for (let day = 1; day <= daysInMonth; day++) {
      const dow = new Date(year, month, day).getDay()
      const matchesDow = dowField === '*' || dowField === '?' || validDow.has(dow)
      const matchesDom = domField === '*' || domField === '?' || validDom.has(day)
      if (matchesDow && matchesDom) days.add(day)
    }
  } catch {
    // ignore parse errors
  }
  return days
}


// ── Inline cron editor ────────────────────────────────────────────────────────

function CronEditor({
  schedule,
  onSave,
}: {
  schedule: ScheduleResponse
  onSave: (cron: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(schedule.cron ?? '')

  function handleSave() {
    onSave(value)
    setEditing(false)
  }

  if (!editing) {
    return (
      <div className="flex items-center gap-2 group">
        <div>
          <p className="font-mono text-sm">{schedule.cron ?? '—'}</p>
          <p className="text-xs text-muted-foreground">{describeCron(schedule.cron ?? '')}</p>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 opacity-0 group-hover:opacity-100 transition-opacity"
          onClick={() => { setValue(schedule.cron ?? ''); setEditing(true) }}
        >
          <Pencil className="h-3 w-3" />
        </Button>
      </div>
    )
  }

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1">
        <Input
          className="h-7 font-mono text-sm w-36"
          value={value}
          onChange={e => setValue(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') handleSave(); if (e.key === 'Escape') setEditing(false) }}
          autoFocus
        />
        <Button size="icon" className="h-7 w-7" onClick={handleSave}>
          <Check className="h-3 w-3" />
        </Button>
        <Button size="icon" variant="ghost" className="h-7 w-7" onClick={() => setEditing(false)}>
          <X className="h-3 w-3" />
        </Button>
      </div>
      {value && (
        <p className="text-xs text-primary">{describeCron(value)}</p>
      )}
    </div>
  )
}


// ── Calendar view ─────────────────────────────────────────────────────────────

function MonthCalendar({ schedules }: { schedules: ScheduleResponse[] }) {
  const { t } = useT()

  const now = new Date()
  const year = now.getFullYear()
  const month = now.getMonth()
  const daysInMonth = new Date(year, month + 1, 0).getDate()
  const firstDow = new Date(year, month, 1).getDay() // 0=Sun

  const MONTH_NAMES = [
    'January','February','March','April','May','June',
    'July','August','September','October','November','December',
  ]
  const DAY_NAMES = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat']

  // Aggregate firing days across all enabled schedules
  const firingDays = new Map<number, string[]>() // day → [job_ids]
  for (const s of schedules) {
    if (!s.enabled || !s.cron) continue
    const days = cronFiringDays(s.cron, year, month)
    for (const d of days) {
      if (!firingDays.has(d)) firingDays.set(d, [])
      firingDays.get(d)!.push(s.job_id)
    }
  }

  const cells: (number | null)[] = []
  for (let i = 0; i < firstDow; i++) cells.push(null)
  for (let d = 1; d <= daysInMonth; d++) cells.push(d)

  const today = now.getDate()

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Calendar className="h-4 w-4" />
          {MONTH_NAMES[month]} {year}
        </CardTitle>
        <CardDescription>{t.scheduler.calendarDesc}</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-7 gap-1 text-center">
          {DAY_NAMES.map(d => (
            <div key={d} className="text-xs font-medium text-muted-foreground py-1">{d}</div>
          ))}
          {cells.map((day, idx) => {
            if (day === null) return <div key={`empty-${idx}`} />
            const jobs = firingDays.get(day) ?? []
            const isToday = day === today
            return (
              <div
                key={day}
                className={`relative rounded-md p-1.5 text-sm transition-colors ${
                  isToday ? 'bg-primary text-primary-foreground font-bold' : 'hover:bg-muted/50'
                } ${jobs.length > 0 && !isToday ? 'bg-primary/10' : ''}`}
                title={jobs.length > 0 ? `Jobs: ${jobs.join(', ')}` : undefined}
              >
                {day}
                {jobs.length > 0 && (
                  <span
                    className={`absolute bottom-0.5 left-1/2 -translate-x-1/2 h-1.5 w-1.5 rounded-full ${
                      isToday ? 'bg-primary-foreground' : 'bg-primary'
                    }`}
                  />
                )}
              </div>
            )
          })}
        </div>
        {firingDays.size > 0 && (
          <p className="text-xs text-muted-foreground mt-3 text-center">
            {firingDays.size} day{firingDays.size !== 1 ? 's' : ''} with scheduled backups
          </p>
        )}
      </CardContent>
    </Card>
  )
}


// ── Schedule row ──────────────────────────────────────────────────────────────

function ScheduleRow({ schedule }: { schedule: ScheduleResponse }) {
  const { t } = useT()
  const qc = useQueryClient()

  const updateMut = useMutation({
    mutationFn: (patch: { enabled?: boolean; cron?: string; timezone?: string }) =>
      api.scheduler.update(schedule.job_id, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['scheduler'] }),
  })

  return (
    <tr className="border-b last:border-0 hover:bg-muted/30 transition-colors">
      <td className="px-4 py-3">
        <p className="font-mono text-sm font-medium">{schedule.job_id}</p>
        <p className="text-xs text-muted-foreground">{schedule.timezone}</p>
      </td>
      <td className="px-4 py-3">
        <Switch
          checked={schedule.enabled}
          onCheckedChange={enabled => updateMut.mutate({ enabled })}
          disabled={updateMut.isPending}
        />
      </td>
      <td className="px-4 py-3">
        <CronEditor
          schedule={schedule}
          onSave={cron => updateMut.mutate({ cron, enabled: schedule.enabled, timezone: schedule.timezone })}
        />
      </td>
      <td className="px-4 py-3">
        {schedule.next_run_at ? (
          <div>
            <p className="text-sm font-semibold text-primary">{countdown(schedule.next_run_at)}</p>
            <p className="text-xs text-muted-foreground">{formatDate(schedule.next_run_at)}</p>
          </div>
        ) : (
          <span className="text-sm text-muted-foreground">—</span>
        )}
      </td>
      <td className="px-4 py-3">
        {schedule.last_run_status ? (
          <div className="flex items-center gap-1.5">
            {schedule.last_run_status === 'success'
              ? <CheckCircle2 className="h-3.5 w-3.5 text-green-500" />
              : <XCircle className="h-3.5 w-3.5 text-red-500" />
            }
            <Badge variant={statusVariant(schedule.last_run_status)} className="text-xs">
              {schedule.last_run_status}
            </Badge>
          </div>
        ) : (
          <span className="text-xs text-muted-foreground">{t.scheduler.noRunYet}</span>
        )}
        {schedule.last_run_at && (
          <p className="text-xs text-muted-foreground mt-0.5">{formatDate(schedule.last_run_at)}</p>
        )}
      </td>
    </tr>
  )
}


// ── Page ──────────────────────────────────────────────────────────────────────

export default function Scheduler() {
  const { t } = useT()
  const qc = useQueryClient()

  const { data: schedules = [], isLoading, error } = useQuery({
    queryKey: ['scheduler'],
    queryFn: api.scheduler.list,
    refetchInterval: 60_000,
  })

  const allEnabled = (schedules as ScheduleResponse[]).filter(s => s.enabled)
  const anyEnabled = allEnabled.length > 0

  const pauseAllMut = useMutation({
    mutationFn: async () => {
      await Promise.all(
        allEnabled.map(s => api.scheduler.update(s.job_id, { enabled: false })),
      )
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['scheduler'] }),
  })

  return (
    <div className="p-8 space-y-8">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">{t.scheduler.title}</h1>
          <p className="text-muted-foreground mt-1">
            {t.scheduler.subtitle}
          </p>
        </div>
        {anyEnabled && (
          <Button
            variant="outline"
            onClick={() => pauseAllMut.mutate()}
            disabled={pauseAllMut.isPending}
          >
            {pauseAllMut.isPending
              ? <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> {t.scheduler.pauseAll}…</>
              : t.scheduler.pauseAll
            }
          </Button>
        )}
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : error ? (
        <Card>
          <CardContent className="py-12 text-center">
            <AlertCircle className="h-6 w-6 text-destructive mx-auto mb-2" />
            <p className="text-sm text-destructive">{String(error)}</p>
          </CardContent>
        </Card>
      ) : (schedules as ScheduleResponse[]).length === 0 ? (
        <Card>
          <CardContent className="py-16 text-center text-muted-foreground space-y-2">
            <Clock className="h-8 w-8 mx-auto opacity-40" />
            <p>{t.scheduler.noSchedules}</p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-8 xl:grid-cols-3">
          {/* Schedule table */}
          <div className="xl:col-span-2">
            <Card>
              <CardContent className="p-0">
                <table className="w-full">
                  <thead>
                    <tr className="border-b text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                      <th className="px-4 py-3">{t.scheduler.job}</th>
                      <th className="px-4 py-3">{t.scheduler.enabled}</th>
                      <th className="px-4 py-3">{t.scheduler.cron}</th>
                      <th className="px-4 py-3">{t.scheduler.nextRun}</th>
                      <th className="px-4 py-3">{t.scheduler.lastRun}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(schedules as ScheduleResponse[]).map(s => (
                      <ScheduleRow key={s.job_id} schedule={s} />
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          </div>

          {/* Calendar */}
          <div>
            <MonthCalendar schedules={schedules as ScheduleResponse[]} />
          </div>
        </div>
      )}
    </div>
  )
}
