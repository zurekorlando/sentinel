/**
 * Jobs page — list all configured jobs, trigger backup runs,
 * real-time WebSocket progress, create/edit/delete jobs, run history.
 */
import { useState } from 'react'
import { useForm, Controller } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import cronstrue from 'cronstrue'
import {
  Play, CheckCircle2, XCircle, Loader2, FileText,
  ChevronRight, Clock, Plus, Pencil, Trash2, History,
  ChevronDown, ChevronUp, ShieldCheck,
} from 'lucide-react'

import * as api from '../lib/api'
import { useJobWS } from '../lib/ws'
import { useT } from '../lib/i18n'
import { formatBytes, formatDate, formatDuration, statusVariant } from '../lib/utils'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Button } from '../components/ui/button'
import { Badge } from '../components/ui/badge'
import { Progress } from '../components/ui/progress'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import { Textarea } from '../components/ui/textarea'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'
import { Switch } from '../components/ui/switch'
import { Slider } from '../components/ui/slider'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import {
  Dialog, DialogContent, DialogDescription,
  DialogFooter, DialogHeader, DialogTitle,
} from '../components/ui/dialog'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '../components/ui/table'
import type { JobConfigResponse, WsEvent, BackupRunResponse } from '../types'
import type { ScrubStateResponse } from '../lib/api'


// ── Cron helper ────────────────────────────────────────────────────────────────

function describeCron(cron: string): string {
  if (!cron.trim()) return ''
  try {
    return cronstrue.toString(cron, { throwExceptionOnParseError: true })
  } catch {
    return '⚠ Invalid cron expression'
  }
}


// ── Form schema ────────────────────────────────────────────────────────────────

const jobSchema = z.object({
  job_id: z.string().min(1, 'Required').regex(/^[a-zA-Z0-9_-]+$/, 'Letters, numbers, hyphens and underscores only'),
  source_paths: z.string().min(1, 'Enter at least one path'),
  exclusions: z.string(),

  provider: z.enum(['local', 's3', 'smb', 'nfs']),
  base_path: z.string(),
  bucket: z.string(),
  prefix: z.string(),
  endpoint_url: z.string(),
  aws_access_key_id: z.string(),
  aws_secret_access_key: z.string(),
  region: z.string(),
  smb_server: z.string(),
  smb_username: z.string(),
  smb_password: z.string(),
  nfs_mount: z.string(),

  schedule_enabled: z.boolean(),
  cron: z.string(),
  timezone: z.string(),

  compression_level: z.number().int().min(1).max(22),
  max_workers: z.number().int().min(1).max(32),
  webhook_url: z.string(),
  retention_daily: z.number().int().min(0),
  retention_weekly: z.number().int().min(0),
  retention_monthly: z.number().int().min(0),
})

type JobFormValues = z.infer<typeof jobSchema>

function defaultValues(job?: JobConfigResponse): JobFormValues {
  return {
    job_id:             job?.job_id ?? '',
    source_paths:       job?.source_paths.join('\n') ?? '',
    exclusions:         job?.exclusions.join('\n') ?? '',
    provider:           (job?.storage.provider as JobFormValues['provider']) ?? 'local',
    base_path:          job?.storage.base_path ?? '',
    bucket:             job?.storage.bucket ?? '',
    prefix:             'chunks/',
    endpoint_url:       job?.storage.endpoint_url ?? '',
    aws_access_key_id:  '',
    aws_secret_access_key: '',
    region:             'us-east-1',
    smb_server:         job?.storage.smb_server ?? '',
    smb_username:       '',
    smb_password:       '',
    nfs_mount:          job?.storage.nfs_mount ?? '',
    schedule_enabled:   job?.schedule?.enabled ?? false,
    cron:               job?.schedule?.cron ?? '0 2 * * *',
    timezone:           job?.schedule?.timezone ?? 'UTC',
    compression_level:  job?.compression_level ?? 3,
    max_workers:        job?.max_workers ?? 4,
    webhook_url:        job?.webhook_url ?? '',
    retention_daily:    job?.retention.daily ?? 7,
    retention_weekly:   job?.retention.weekly ?? 4,
    retention_monthly:  job?.retention.monthly ?? 12,
  }
}


// ── FormField helper ───────────────────────────────────────────────────────────

function FormField({ label, error, children, hint }: {
  label: string
  error?: string
  children: React.ReactNode
  hint?: string
}) {
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      {children}
      {hint && !error && <p className="text-xs text-muted-foreground">{hint}</p>}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  )
}


// ── Job Form Drawer ────────────────────────────────────────────────────────────

function JobFormDrawer({
  open,
  onOpenChange,
  editJob,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  editJob?: JobConfigResponse
}) {
  const { t } = useT()
  const qc = useQueryClient()
  const isEdit = !!editJob

  const { register, control, handleSubmit, watch, formState: { errors, isSubmitting } } = useForm<JobFormValues>({
    resolver: zodResolver(jobSchema),
    defaultValues: defaultValues(editJob),
  })

  const provider = watch('provider')
  const cronVal = watch('cron')
  const scheduleEnabled = watch('schedule_enabled')
  const compressionLevel = watch('compression_level')
  const maxWorkers = watch('max_workers')

  const createMut = useMutation({
    mutationFn: (vals: JobFormValues) => api.jobs.create({
      job_id: vals.job_id,
      source_paths: vals.source_paths.split('\n').map(s => s.trim()).filter(Boolean),
      exclusions: vals.exclusions.split('\n').map(s => s.trim()).filter(Boolean),
      storage: {
        provider: vals.provider,
        base_path: vals.base_path || undefined,
        bucket: vals.bucket || undefined,
        prefix: vals.prefix || undefined,
        endpoint_url: vals.endpoint_url || undefined,
        aws_access_key_id: vals.aws_access_key_id || undefined,
        aws_secret_access_key: vals.aws_secret_access_key || undefined,
        region: vals.region || undefined,
        smb_server: vals.smb_server || undefined,
        smb_username: vals.smb_username || undefined,
        smb_password: vals.smb_password || undefined,
        nfs_mount: vals.nfs_mount || undefined,
      },
      retention: { daily: vals.retention_daily, weekly: vals.retention_weekly, monthly: vals.retention_monthly },
      compression_level: vals.compression_level,
      max_workers: vals.max_workers,
      webhook_url: vals.webhook_url || undefined,
      schedule: vals.schedule_enabled ? { enabled: true, cron: vals.cron || undefined, timezone: vals.timezone } : undefined,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
      onOpenChange(false)
    },
  })

  const updateMut = useMutation({
    mutationFn: (vals: JobFormValues) => api.jobs.update(editJob!.job_id, {
      source_paths: vals.source_paths.split('\n').map(s => s.trim()).filter(Boolean),
      exclusions: vals.exclusions.split('\n').map(s => s.trim()).filter(Boolean),
      storage: {
        provider: vals.provider,
        base_path: vals.base_path || undefined,
        bucket: vals.bucket || undefined,
        prefix: vals.prefix || undefined,
        endpoint_url: vals.endpoint_url || undefined,
        aws_access_key_id: vals.aws_access_key_id || undefined,
        aws_secret_access_key: vals.aws_secret_access_key || undefined,
        region: vals.region || undefined,
        smb_server: vals.smb_server || undefined,
        smb_username: vals.smb_username || undefined,
        smb_password: vals.smb_password || undefined,
        nfs_mount: vals.nfs_mount || undefined,
      },
      retention: { daily: vals.retention_daily, weekly: vals.retention_weekly, monthly: vals.retention_monthly },
      compression_level: vals.compression_level,
      max_workers: vals.max_workers,
      webhook_url: vals.webhook_url || undefined,
      schedule: { enabled: vals.schedule_enabled, cron: vals.cron || undefined, timezone: vals.timezone },
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
      onOpenChange(false)
    },
  })

  const mutError = createMut.error || updateMut.error

  function onSubmit(vals: JobFormValues) {
    if (isEdit) updateMut.mutate(vals)
    else createMut.mutate(vals)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? `${t.jobs.editJob} — ${editJob.job_id}` : t.jobs.newJob}</DialogTitle>
          <DialogDescription>
            {isEdit ? 'Update the configuration for this job.' : 'Configure a new backup job.'}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit(onSubmit)} className="space-y-6 py-2">
          <Tabs defaultValue="source">
            <TabsList className="grid w-full grid-cols-4">
              <TabsTrigger value="source">{t.jobs.tabSource}</TabsTrigger>
              <TabsTrigger value="storage">{t.jobs.tabStorage}</TabsTrigger>
              <TabsTrigger value="schedule">{t.jobs.tabSchedule}</TabsTrigger>
              <TabsTrigger value="options">{t.jobs.tabOptions}</TabsTrigger>
            </TabsList>

            {/* ── Tab 1: Source ── */}
            <TabsContent value="source" className="space-y-4 pt-4">
              <FormField label={t.jobs.jobId} error={errors.job_id?.message}>
                <Input
                  {...register('job_id')}
                  disabled={isEdit}
                  placeholder={t.jobs.jobIdPlaceholder}
                  className={isEdit ? 'bg-muted text-muted-foreground' : ''}
                />
              </FormField>
              <FormField
                label={t.jobs.sourcePaths}
                error={errors.source_paths?.message}
                hint={t.jobs.sourcePathsHint}
              >
                <Textarea
                  {...register('source_paths')}
                  placeholder={t.jobs.sourcePathsPlaceholder}
                  rows={4}
                />
              </FormField>
              <FormField
                label={t.jobs.exclusions}
                hint={t.jobs.exclusionsHint}
              >
                <Textarea
                  {...register('exclusions')}
                  placeholder={t.jobs.exclusionsPlaceholder}
                  rows={3}
                />
              </FormField>
            </TabsContent>

            {/* ── Tab 2: Storage ── */}
            <TabsContent value="storage" className="space-y-4 pt-4">
              <FormField label={t.jobs.storageProvider}>
                <Controller
                  control={control}
                  name="provider"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger>
                        <SelectValue placeholder="Select provider" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="local">Local filesystem</SelectItem>
                        <SelectItem value="s3">Amazon S3 / MinIO</SelectItem>
                        <SelectItem value="smb">SMB / CIFS share</SelectItem>
                        <SelectItem value="nfs">NFS mount</SelectItem>
                      </SelectContent>
                    </Select>
                  )}
                />
              </FormField>

              {provider === 'local' && (
                <FormField label={t.jobs.localPath} hint="Local directory to store chunks">
                  <Input {...register('base_path')} placeholder={t.jobs.localPathPlaceholder} />
                </FormField>
              )}

              {provider === 's3' && (
                <>
                  <FormField label={t.jobs.bucket}>
                    <Input {...register('bucket')} placeholder="my-sentinel-bucket" />
                  </FormField>
                  <FormField label={t.jobs.endpoint} hint="Leave blank for AWS S3">
                    <Input {...register('endpoint_url')} placeholder="http://localhost:9000" />
                  </FormField>
                  <div className="grid grid-cols-2 gap-3">
                    <FormField label={t.jobs.accessKey}>
                      <Input {...register('aws_access_key_id')} placeholder="AKIA..." />
                    </FormField>
                    <FormField label={t.jobs.secretKey}>
                      <Input {...register('aws_secret_access_key')} type="password" placeholder="••••••••" />
                    </FormField>
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <FormField label={t.jobs.region}>
                      <Input {...register('region')} placeholder="us-east-1" />
                    </FormField>
                    <FormField label="Prefix">
                      <Input {...register('prefix')} placeholder="chunks/" />
                    </FormField>
                  </div>
                </>
              )}

              {provider === 'smb' && (
                <>
                  <FormField label="SMB server" hint="\\\\server\\share or UNC path">
                    <Input {...register('smb_server')} placeholder="\\server\share" />
                  </FormField>
                  <FormField label={t.jobs.mountPoint} hint="Local path where the share is mounted">
                    <Input {...register('base_path')} placeholder="/mnt/smb/backup" />
                  </FormField>
                  <div className="grid grid-cols-2 gap-3">
                    <FormField label="Username">
                      <Input {...register('smb_username')} placeholder="domain\user" />
                    </FormField>
                    <FormField label="Password">
                      <Input {...register('smb_password')} type="password" placeholder="••••••••" />
                    </FormField>
                  </div>
                </>
              )}

              {provider === 'nfs' && (
                <FormField label={t.jobs.mountPoint} hint="Path where the NFS share is mounted">
                  <Input {...register('nfs_mount')} placeholder="/mnt/nfs/backup" />
                </FormField>
              )}
            </TabsContent>

            {/* ── Tab 3: Schedule ── */}
            <TabsContent value="schedule" className="space-y-4 pt-4">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium">{t.jobs.enableSchedule}</p>
                  <p className="text-xs text-muted-foreground">
                    Run automatically using APScheduler
                  </p>
                </div>
                <Controller
                  control={control}
                  name="schedule_enabled"
                  render={({ field }) => (
                    <Switch checked={field.value} onCheckedChange={field.onChange} />
                  )}
                />
              </div>

              {scheduleEnabled && (
                <>
                  <FormField
                    label={t.jobs.cronExpression}
                    error={errors.cron?.message}
                    hint="Standard 5-field cron (minute hour day month weekday)"
                  >
                    <Input {...register('cron')} placeholder="0 2 * * *" />
                    {cronVal && (
                      <p className="text-xs text-primary mt-1">{describeCron(cronVal)}</p>
                    )}
                  </FormField>
                  <FormField label={t.jobs.timezone} hint="IANA timezone (e.g. America/New_York)">
                    <Input {...register('timezone')} placeholder="UTC" />
                  </FormField>
                  <div className="rounded-md bg-muted/50 p-3 text-xs text-muted-foreground space-y-1">
                    <p className="font-medium text-foreground">{t.jobs.passphrase}</p>
                    <p>
                      Scheduled backups use the <code className="bg-muted px-1 rounded">SENTINEL_PASSPHRASE</code>{' '}
                      environment variable. Set it in your deployment environment.
                    </p>
                  </div>
                </>
              )}
            </TabsContent>

            {/* ── Tab 4: Options ── */}
            <TabsContent value="options" className="space-y-6 pt-4">
              <FormField
                label={`${t.jobs.compressionLevel} — ${compressionLevel}`}
                hint={`Level ${compressionLevel} — Higher = smaller files, slower. Zstandard level 1–22.`}
              >
                <Controller
                  control={control}
                  name="compression_level"
                  render={({ field }) => (
                    <Slider
                      min={1} max={22} step={1}
                      value={[field.value]}
                      onValueChange={([v]) => field.onChange(v)}
                      className="mt-2"
                    />
                  )}
                />
              </FormField>

              <FormField
                label={`${t.jobs.workerThreads} — ${maxWorkers}`}
                hint={t.jobs.workerHint}
              >
                <Controller
                  control={control}
                  name="max_workers"
                  render={({ field }) => (
                    <Slider
                      min={1} max={32} step={1}
                      value={[field.value]}
                      onValueChange={([v]) => field.onChange(v)}
                      className="mt-2"
                    />
                  )}
                />
              </FormField>

              <div className="space-y-3">
                <p className="text-sm font-medium">{t.jobs.retention}</p>
                <div className="grid grid-cols-3 gap-3">
                  <FormField label={t.jobs.retentionDaily}>
                    <Input {...register('retention_daily', { valueAsNumber: true })} type="number" min={0} />
                  </FormField>
                  <FormField label={t.jobs.retentionWeekly}>
                    <Input {...register('retention_weekly', { valueAsNumber: true })} type="number" min={0} />
                  </FormField>
                  <FormField label={t.jobs.retentionMonthly}>
                    <Input {...register('retention_monthly', { valueAsNumber: true })} type="number" min={0} />
                  </FormField>
                </div>
              </div>

              <FormField label={t.jobs.webhookUrl} hint={t.common.optional}>
                <Input {...register('webhook_url')} placeholder={t.jobs.webhookPlaceholder} />
              </FormField>
            </TabsContent>
          </Tabs>

          {mutError && (
            <p className="text-sm text-destructive">{String(mutError)}</p>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              {t.common.cancel}
            </Button>
            <Button type="submit" disabled={isSubmitting || createMut.isPending || updateMut.isPending}>
              {(isSubmitting || createMut.isPending || updateMut.isPending) ? (
                <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> {t.common.saving}</>
              ) : (
                isEdit ? t.common.update : t.common.create
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}


// ── Run History Modal ──────────────────────────────────────────────────────────

function RunHistoryModal({
  jobId,
  open,
  onOpenChange,
}: {
  jobId: string
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const { t, interp } = useT()
  const [expanded, setExpanded] = useState<string | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['runs', jobId],
    queryFn: () => api.jobs.listRuns(jobId),
    enabled: open,
  })

  const runs: BackupRunResponse[] = data?.runs ?? []

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{interp(t.jobs.runHistoryTitle, { id: jobId })}</DialogTitle>
          <DialogDescription>
            {runs.length} recorded run{runs.length !== 1 ? 's' : ''}
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : runs.length === 0 ? (
          <p className="text-center text-muted-foreground py-8">{t.jobs.noRunHistory}</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t.snapshots.status}</TableHead>
                <TableHead>{t.jobs.startedAt}</TableHead>
                <TableHead>{t.jobs.duration}</TableHead>
                <TableHead className="text-right">{t.jobs.filesProcessed}</TableHead>
                <TableHead className="text-right">{t.jobs.chunksUploaded}</TableHead>
                <TableHead className="text-right">{t.jobs.bytesOriginal}</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map(run => (
                <>
                  <TableRow
                    key={run.run_id}
                    className="cursor-pointer hover:bg-muted/50"
                    onClick={() => setExpanded(prev => prev === run.run_id ? null : run.run_id)}
                  >
                    <TableCell>
                      <Badge variant={statusVariant(run.status)}>{run.status}</Badge>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {formatDate(run.started_at)}
                    </TableCell>
                    <TableCell className="text-sm">
                      {run.duration_s > 0 ? formatDuration(run.duration_s) : '—'}
                    </TableCell>
                    <TableCell className="text-right text-sm">{run.files_processed}</TableCell>
                    <TableCell className="text-right text-sm">{run.chunks_uploaded}</TableCell>
                    <TableCell className="text-right text-sm">
                      {formatBytes(run.bytes_original)}
                    </TableCell>
                    <TableCell>
                      {expanded === run.run_id
                        ? <ChevronUp className="h-3 w-3" />
                        : <ChevronDown className="h-3 w-3" />
                      }
                    </TableCell>
                  </TableRow>
                  {expanded === run.run_id && (
                    <TableRow key={`${run.run_id}-detail`}>
                      <TableCell colSpan={7} className="bg-muted/30 px-4 py-3">
                        <div className="text-xs space-y-1">
                          <p><span className="text-muted-foreground">Run ID:</span> <span className="font-mono">{run.run_id}</span></p>
                          <p><span className="text-muted-foreground">{t.jobs.provider}:</span> {run.provider_used}</p>
                          <p><span className="text-muted-foreground">{t.jobs.chunksDeduped}:</span> {run.chunks_deduped}</p>
                          <p><span className="text-muted-foreground">{t.jobs.filesSkipped}:</span> {run.files_skipped_cbt}</p>
                          <p><span className="text-muted-foreground">Stored:</span> {formatBytes(run.bytes_stored)}</p>
                          {run.finished_at && (
                            <p><span className="text-muted-foreground">Finished:</span> {formatDate(run.finished_at)}</p>
                          )}
                          {run.errors.length > 0 && (
                            <div className="mt-2 rounded bg-red-50 dark:bg-red-950 p-2 space-y-0.5">
                              <p className="font-medium text-red-700 dark:text-red-300">{t.jobs.errors}:</p>
                              {run.errors.map((e, i) => (
                                <p key={i} className="font-mono text-red-600 dark:text-red-400">{e}</p>
                              ))}
                            </div>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                </>
              ))}
            </TableBody>
          </Table>
        )}
      </DialogContent>
    </Dialog>
  )
}


// ── Delete confirmation ────────────────────────────────────────────────────────

function DeleteJobDialog({
  jobId,
  open,
  onOpenChange,
}: {
  jobId: string
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const { t, interp } = useT()
  const qc = useQueryClient()
  const deleteMut = useMutation({
    mutationFn: () => api.jobs.delete(jobId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
      onOpenChange(false)
    },
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{interp(t.jobs.confirmDelete, { id: jobId })}</DialogTitle>
          <DialogDescription>
            {t.jobs.confirmDeleteDesc}
          </DialogDescription>
        </DialogHeader>
        {deleteMut.isError && (
          <p className="text-sm text-destructive">{String(deleteMut.error)}</p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t.common.cancel}
          </Button>
          <Button
            variant="destructive"
            onClick={() => deleteMut.mutate()}
            disabled={deleteMut.isPending}
          >
            {deleteMut.isPending
              ? <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> {t.common.deleting}</>
              : <><Trash2 className="h-4 w-4 mr-2" /> {t.jobs.deleteJobBtn}</>
            }
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}


// ── Run dialog ─────────────────────────────────────────────────────────────────

function RunDialog({
  job,
  open,
  onOpenChange,
  onStarted,
}: {
  job: JobConfigResponse
  open: boolean
  onOpenChange: (o: boolean) => void
  onStarted: (runId: string) => void
}) {
  const { t } = useT()
  const [passphrase, setPassphrase] = useState('')
  const [provider, setProvider] = useState('')

  const mutation = useMutation({
    mutationFn: () =>
      api.jobs.run(job.job_id, {
        passphrase,
        snapshot_provider: provider || undefined,
      }),
    onSuccess: (data) => {
      onStarted(data.run_id)
      onOpenChange(false)
      setPassphrase('')
    },
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t.jobs.startBackup} — {job.job_id}</DialogTitle>
          <DialogDescription>
            Enter the encryption passphrase to start the backup job.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <Label htmlFor="passphrase">{t.jobs.passphrase}</Label>
            <Input
              id="passphrase"
              type="password"
              placeholder={t.jobs.passphrasePlaceholder}
              value={passphrase}
              onChange={e => setPassphrase(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && passphrase && mutation.mutate()}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="provider">
              {t.jobs.snapshotProvider}{' '}
              <span className="text-muted-foreground font-normal">({t.common.optional})</span>
            </Label>
            <Input
              id="provider"
              placeholder="auto-detect (vss | lvm | btrfs | direct)"
              value={provider}
              onChange={e => setProvider(e.target.value)}
            />
          </div>
          {mutation.isError && (
            <p className="text-sm text-destructive">{String(mutation.error)}</p>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t.common.cancel}
          </Button>
          <Button
            onClick={() => mutation.mutate()}
            disabled={!passphrase || mutation.isPending}
          >
            {mutation.isPending ? (
              <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> {t.jobs.starting}</>
            ) : (
              <><Play className="h-4 w-4 mr-2" /> {t.jobs.startBackup}</>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}


// ── Progress panel ─────────────────────────────────────────────────────────────

function ProgressPanel({
  jobId,
  runId,
  onClose,
}: {
  jobId: string
  runId: string
  onClose: () => void
}) {
  const { t } = useT()
  const { events, lastEvent, isConnected } = useJobWS(jobId)

  const fileEvents = events.filter((e: WsEvent) => e.event === 'FILE_DONE')
  const terminal = events.find(
    (e: WsEvent) => e.event === 'JOB_SUCCESS' || e.event === 'JOB_FAILED',
  )
  const latest = lastEvent

  const filesProcessed = latest?.files_processed ?? 0
  const chunksUploaded = latest?.chunks_uploaded ?? 0
  const chunksDeduped = latest?.chunks_deduped ?? 0
  const bytesOriginal = latest?.bytes_original ?? 0

  const isDone = !!terminal

  return (
    <Card className="border-primary/20 bg-card">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            {!isDone ? (
              <Loader2 className="h-4 w-4 animate-spin text-blue-500" />
            ) : terminal?.event === 'JOB_SUCCESS' ? (
              <CheckCircle2 className="h-4 w-4 text-green-500" />
            ) : (
              <XCircle className="h-4 w-4 text-red-500" />
            )}
            <CardTitle className="text-sm">
              {!isDone ? t.jobs.progressTitle : terminal?.event === 'JOB_SUCCESS' ? t.status.success : t.status.failed}
            </CardTitle>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground font-mono">{runId.slice(0, 8)}</span>
            {isDone && (
              <Button size="sm" variant="ghost" onClick={onClose}>
                {t.jobs.closePanel}
              </Button>
            )}
          </div>
        </div>
        <CardDescription className="flex gap-1 items-center">
          <span className={isConnected ? 'text-green-500' : 'text-muted-foreground'}>●</span>
          {isConnected ? 'WebSocket connected' : 'Disconnected'}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        {!isDone ? (
          <div className="relative h-2 rounded-full bg-secondary overflow-hidden">
            <div className="absolute h-full w-1/3 bg-primary rounded-full animate-progress-indeterminate" />
          </div>
        ) : (
          <Progress value={100} className="h-2" />
        )}

        <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          {[
            { label: t.jobs.filesProcessed, value: filesProcessed },
            { label: t.jobs.chunksUploaded, value: chunksUploaded },
            { label: t.jobs.chunksDeduped, value: chunksDeduped },
            { label: t.jobs.bytesOriginal, value: formatBytes(bytesOriginal) },
          ].map(({ label, value }) => (
            <div key={label} className="space-y-0.5">
              <p className="text-xs text-muted-foreground">{label}</p>
              <p className="font-semibold">{value}</p>
            </div>
          ))}
        </div>

        {terminal && (
          <div className={`rounded-md px-3 py-2 text-sm ${
            terminal.event === 'JOB_SUCCESS'
              ? 'bg-green-50 text-green-800 dark:bg-green-950 dark:text-green-200'
              : 'bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-200'
          }`}>
            {terminal.event === 'JOB_SUCCESS' ? (
              <span>
                Completed in {formatDuration(terminal.duration_s ?? 0)} ·{' '}
                {formatBytes(terminal.bytes_original ?? 0)} backed up
              </span>
            ) : (
              <span>Error: {terminal.error ?? 'Unknown error'}</span>
            )}
          </div>
        )}

        {fileEvents.length > 0 && (
          <div className="rounded-md bg-muted/50 p-2 max-h-32 overflow-y-auto space-y-0.5">
            {fileEvents.slice(-20).reverse().map((e: WsEvent, i: number) => (
              <div key={i} className="flex items-center gap-1.5 text-xs text-muted-foreground font-mono">
                <FileText className="h-3 w-3 shrink-0" />
                <span className="truncate">{e.file}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}


// ── Scrub modal ────────────────────────────────────────────────────────────────

function ScrubModal({
  jobId,
  open,
  onOpenChange,
}: {
  jobId: string
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const { t, interp } = useT()
  const [full, setFull] = useState(false)
  const [sample, setSample] = useState(10) // percent
  const [scrubJobId, setScrubJobId] = useState<string | null>(null)

  const startMutation = useMutation({
    mutationFn: () =>
      api.scrub.start(jobId, { sample: sample / 100, full }),
    onSuccess: () => setScrubJobId(jobId),
  })

  const { data: status } = useQuery({
    queryKey: ['scrub', 'status', jobId],
    queryFn: () => api.scrub.status(jobId),
    enabled: scrubJobId !== null,
    refetchInterval: (query) => {
      const s = (query.state.data as ScrubStateResponse | undefined)?.status
      return s === 'queued' || s === 'running' ? 1500 : false
    },
  })

  const isRunning = status?.status === 'queued' || status?.status === 'running'
  const isDone = status?.status === 'success' || status?.status === 'failed'

  function handleClose() {
    onOpenChange(false)
    setScrubJobId(null)
    setSample(10)
    setFull(false)
  }

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) handleClose() }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{interp(t.jobs.scrubTitle, { id: jobId })}</DialogTitle>
          <DialogDescription>
            {t.jobs.scrubDesc}
          </DialogDescription>
        </DialogHeader>

        {/* Config (only before starting) */}
        {!scrubJobId && (
          <div className="space-y-4 py-2">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium">{t.jobs.fullScan}</p>
                <p className="text-xs text-muted-foreground">
                  {t.jobs.fullScanDesc}
                </p>
              </div>
              <Switch checked={full} onCheckedChange={setFull} />
            </div>

            {!full && (
              <div className="space-y-2">
                <div className="flex justify-between text-sm">
                  <Label>{t.jobs.sampleSize}</Label>
                  <span className="font-medium">{sample}%</span>
                </div>
                <Slider
                  min={1} max={100} step={1}
                  value={[sample]}
                  onValueChange={([v]) => setSample(v)}
                />
              </div>
            )}
          </div>
        )}

        {/* Progress */}
        {scrubJobId && status && (
          <div className="space-y-3 py-2">
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">{t.jobs.mode}</span>
              <Badge variant="outline">
                {status.mode === 'full'
                  ? t.jobs.fullScanLabel
                  : interp(t.jobs.sampleLabel, { pct: Math.round(status.sample_fraction * 100) })
                }
              </Badge>
            </div>

            {isRunning && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>{t.jobs.checkingChunks}</span>
              </div>
            )}

            <div className="grid grid-cols-3 gap-3 text-center">
              {[
                { label: t.jobs.checked, value: status.chunks_checked, color: '' },
                { label: t.jobs.ok, value: status.chunks_ok, color: 'text-green-600' },
                { label: t.jobs.corrupt, value: status.chunks_corrupt, color: status.chunks_corrupt > 0 ? 'text-destructive' : '' },
              ].map(({ label, value, color }) => (
                <div key={label} className="rounded-md border p-2">
                  <p className={`text-2xl font-bold tabular-nums ${color}`}>{value}</p>
                  <p className="text-xs text-muted-foreground">{label}</p>
                </div>
              ))}
            </div>

            {isDone && status.chunks_corrupt === 0 && (
              <div className="flex items-center gap-2 rounded-md bg-green-500/10 p-3 text-sm text-green-700 dark:text-green-400">
                <CheckCircle2 className="h-4 w-4" />
                {interp(t.jobs.allIntact, { duration: status.duration_s.toFixed(1) })}
              </div>
            )}

            {isDone && status.chunks_corrupt > 0 && (
              <div className="space-y-2">
                <div className="flex items-center gap-2 rounded-md bg-destructive/10 p-3 text-sm text-destructive">
                  <XCircle className="h-4 w-4" />
                  {interp(t.jobs.corruptDetected, { count: status.chunks_corrupt })}
                </div>
                {status.affected_snapshot_ids.length > 0 && (
                  <div className="rounded-md border p-2 text-xs space-y-1">
                    <p className="font-medium text-muted-foreground">{t.jobs.affectedSnapshots}:</p>
                    {status.affected_snapshot_ids.map(id => (
                      <p key={id} className="font-mono text-muted-foreground">{id.substring(0, 8)}…</p>
                    ))}
                  </div>
                )}
              </div>
            )}

            {isDone && status.errors.length > 0 && status.chunks_corrupt === 0 && (
              <div className="rounded-md bg-destructive/10 p-2 text-xs text-destructive space-y-0.5 max-h-24 overflow-y-auto">
                {status.errors.slice(0, 5).map((e, i) => <p key={i}>{e}</p>)}
              </div>
            )}
          </div>
        )}

        {startMutation.isError && (
          <p className="text-sm text-destructive">
            {(startMutation.error as Error).message}
          </p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={handleClose}>
            {isDone ? t.common.close : t.common.cancel}
          </Button>
          {!scrubJobId && (
            <Button
              onClick={() => startMutation.mutate()}
              disabled={startMutation.isPending}
            >
              {startMutation.isPending
                ? <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> {t.jobs.starting2}</>
                : <><ShieldCheck className="h-4 w-4 mr-2" /> {t.jobs.startScrub}</>
              }
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}


// ── Job card ───────────────────────────────────────────────────────────────────

function JobCard({
  job,
  onEdit,
}: {
  job: JobConfigResponse
  onEdit: (job: JobConfigResponse) => void
}) {
  const { t } = useT()
  const [runDialogOpen, setRunDialogOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [scrubOpen, setScrubOpen] = useState(false)
  const [activeRunId, setActiveRunId] = useState<string | null>(null)

  const qc = useQueryClient()
  const { data: latestRun } = useQuery({
    queryKey: ['run', 'latest', job.job_id],
    queryFn: () => api.jobs.latestRun(job.job_id).catch(() => null),
    refetchInterval: activeRunId ? 5_000 : false,
  })

  function handleStarted(runId: string) {
    setActiveRunId(runId)
  }

  function handleClose() {
    setActiveRunId(null)
    qc.invalidateQueries({ queryKey: ['run', 'latest', job.job_id] })
    qc.invalidateQueries({ queryKey: ['snapshots'] })
  }

  const isRunning = latestRun?.status === 'running' || latestRun?.status === 'queued'

  return (
    <>
      <Card>
        <CardHeader>
          <div className="flex items-start justify-between gap-2">
            <div>
              <CardTitle className="text-base">{job.job_id}</CardTitle>
              <CardDescription className="mt-1">
                {job.source_paths.join(', ')}
              </CardDescription>
            </div>
            {latestRun && (
              <Badge variant={statusVariant(latestRun.status)}>
                {latestRun.status}
              </Badge>
            )}
          </div>
        </CardHeader>

        <CardContent className="space-y-4">
          {/* Job metadata */}
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
            {[
              ['Provider', job.storage.provider],
              ['Workers', job.max_workers],
              ['Compression', `Level ${job.compression_level}`],
              ['Retention', `${job.retention.daily}d / ${job.retention.weekly}w / ${job.retention.monthly}m`],
            ].map(([k, v]) => (
              <div key={String(k)} className="flex justify-between">
                <span className="text-muted-foreground">{k}</span>
                <span className="font-medium">{v}</span>
              </div>
            ))}
            {job.schedule?.enabled && job.schedule.cron && (
              <div className="col-span-2 flex justify-between">
                <span className="text-muted-foreground">Schedule</span>
                <span className="font-mono text-xs">{job.schedule.cron}</span>
              </div>
            )}
          </div>

          {/* Last run summary */}
          {latestRun && (
            <div className="rounded-md bg-muted/50 p-3 text-sm space-y-1">
              <p className="font-medium text-xs text-muted-foreground uppercase tracking-wide">
                {t.dashboard.lastRun}
              </p>
              <div className="flex items-center gap-1.5 text-muted-foreground">
                <Clock className="h-3 w-3" />
                <span>{formatDate(latestRun.started_at)}</span>
              </div>
              <div className="flex items-center gap-3">
                <span>{latestRun.files_processed} {t.jobs.filesProcessed}</span>
                <ChevronRight className="h-3 w-3 text-muted-foreground" />
                <span>{latestRun.chunks_uploaded} uploaded</span>
                <ChevronRight className="h-3 w-3 text-muted-foreground" />
                <span>{latestRun.chunks_deduped} deduped</span>
              </div>
            </div>
          )}

          {/* Active run progress */}
          {activeRunId && (
            <ProgressPanel
              jobId={job.job_id}
              runId={activeRunId}
              onClose={handleClose}
            />
          )}

          {/* Action buttons */}
          <div className="flex gap-2">
            <Button
              className="flex-1"
              onClick={() => setRunDialogOpen(true)}
              disabled={isRunning || !!activeRunId}
            >
              {isRunning ? (
                <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> {t.jobs.running}</>
              ) : (
                <><Play className="h-4 w-4 mr-2" /> {t.jobs.runNow}</>
              )}
            </Button>
            <Button variant="outline" size="icon" onClick={() => onEdit(job)} title={t.jobs.editJob}>
              <Pencil className="h-4 w-4" />
            </Button>
            <Button variant="outline" size="icon" onClick={() => setHistoryOpen(true)} title={t.jobs.runHistory}>
              <History className="h-4 w-4" />
            </Button>
            <Button variant="outline" size="icon" onClick={() => setScrubOpen(true)} title={t.jobs.integrityCheck}>
              <ShieldCheck className="h-4 w-4" />
            </Button>
            <Button
              variant="outline"
              size="icon"
              className="text-muted-foreground hover:text-destructive"
              onClick={() => setDeleteOpen(true)}
              title={t.jobs.deleteJob}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        </CardContent>
      </Card>

      <ScrubModal
        jobId={job.job_id}
        open={scrubOpen}
        onOpenChange={setScrubOpen}
      />
      <RunDialog
        job={job}
        open={runDialogOpen}
        onOpenChange={setRunDialogOpen}
        onStarted={handleStarted}
      />
      <RunHistoryModal
        jobId={job.job_id}
        open={historyOpen}
        onOpenChange={setHistoryOpen}
      />
      <DeleteJobDialog
        jobId={job.job_id}
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
      />
    </>
  )
}


// ── Page ──────────────────────────────────────────────────────────────────────

export default function Jobs() {
  const { t } = useT()
  const [formOpen, setFormOpen] = useState(false)
  const [editJob, setEditJob] = useState<JobConfigResponse | undefined>(undefined)

  const { data: jobList = [], isLoading, error } = useQuery({
    queryKey: ['jobs'],
    queryFn: api.jobs.list,
  })

  function handleNewJob() {
    setEditJob(undefined)
    setFormOpen(true)
  }

  function handleEdit(job: JobConfigResponse) {
    setEditJob(job)
    setFormOpen(true)
  }

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-8">
        <p className="text-destructive">Failed to load jobs: {String(error)}</p>
      </div>
    )
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">{t.jobs.title}</h1>
          <p className="text-muted-foreground mt-1">
            {t.jobs.subtitle}
          </p>
        </div>
        <Button onClick={handleNewJob}>
          <Plus className="h-4 w-4 mr-2" />
          {t.jobs.newJob}
        </Button>
      </div>

      {jobList.length === 0 ? (
        <Card>
          <CardContent className="py-16 text-center text-muted-foreground space-y-3">
            <p>{t.jobs.noJobsDesc}</p>
            <Button onClick={handleNewJob}>
              <Plus className="h-4 w-4 mr-2" />
              {t.jobs.createFirstJob}
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-6 lg:grid-cols-2 xl:grid-cols-3">
          {jobList.map(job => (
            <JobCard key={job.job_id} job={job} onEdit={handleEdit} />
          ))}
        </div>
      )}

      <JobFormDrawer
        open={formOpen}
        onOpenChange={setFormOpen}
        editJob={editJob}
      />
    </div>
  )
}
