/**
 * Snapshots page — browse, filter, delete snapshots; view files; restore.
 */
import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Loader2, Trash2, ChevronDown, ChevronUp, AlertCircle,
  FolderOpen, RotateCcw, Search, SquareCheck, Square,
  ChevronLeft, ChevronRight,
} from 'lucide-react'

import * as api from '../lib/api'
import { useT } from '../lib/i18n'
import { formatBytes, formatDate, statusVariant } from '../lib/utils'
import { Card, CardContent } from '../components/ui/card'
import { Button } from '../components/ui/button'
import { Badge } from '../components/ui/badge'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import { Progress } from '../components/ui/progress'
import { Switch } from '../components/ui/switch'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '../components/ui/table'
import {
  Dialog, DialogContent, DialogDescription,
  DialogFooter, DialogHeader, DialogTitle,
} from '../components/ui/dialog'
import type { SnapshotResponse, SnapshotDetail, FileResponse, RestoreResponse } from '../types'

const PAGE_SIZE = 50


// ── Restore modal ──────────────────────────────────────────────────────────────

function RestoreModal({
  snapshot,
  selectedFiles,
  open,
  onOpenChange,
}: {
  snapshot: SnapshotResponse
  selectedFiles: FileResponse[] | null  // null = all files
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const { t, interp } = useT()

  const [destination, setDestination] = useState('')
  const [passphrase, setPassphrase] = useState('')
  const [overwrite, setOverwrite] = useState(false)
  const [restoreId, setRestoreId] = useState<string | null>(null)
  const [startError, setStartError] = useState<string | null>(null)
  const [isStarting, setIsStarting] = useState(false)

  // Poll restore status — TanStack Query v5: refetchInterval receives the Query object
  const { data: restoreStatus } = useQuery({
    queryKey: ['restore', restoreId],
    queryFn: () => api.restore.status(restoreId!),
    enabled: !!restoreId,
    refetchInterval: (query) => {
      const d = (query.state.data as RestoreResponse | undefined)
      if (!d) return 2_000
      return d.status === 'running' || d.status === 'queued' ? 2_000 : false
    },
  })

  const isRunning = restoreStatus?.status === 'running' || restoreStatus?.status === 'queued'
  const isDone = restoreStatus?.status === 'success' || restoreStatus?.status === 'failed'

  async function handleStart() {
    if (!destination || !passphrase) return
    setStartError(null)
    setIsStarting(true)
    try {
      const result = await api.restore.start(snapshot.snapshot_id, {
        job_id: snapshot.job_id,
        passphrase,
        destination_path: destination,
        file_paths: selectedFiles ? selectedFiles.map(f => f.path) : undefined,
        overwrite,
      })
      setRestoreId(result.restore_id)
    } catch (e) {
      setStartError(String(e))
    } finally {
      setIsStarting(false)
    }
  }

  function handleClose() {
    setDestination('')
    setPassphrase('')
    setOverwrite(false)
    setRestoreId(null)
    setStartError(null)
    onOpenChange(false)
  }

  const progressPct = restoreStatus && restoreStatus.files_total > 0
    ? Math.round((restoreStatus.files_restored / restoreStatus.files_total) * 100)
    : 0

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t.snapshots.restoreTitle}</DialogTitle>
          <DialogDescription>
            {selectedFiles
              ? `${t.snapshots.restoreSelected} (${selectedFiles.length})`
              : `${t.snapshots.restoreAll} — ${snapshot.snapshot_id.slice(0, 12)}…`
            }
          </DialogDescription>
        </DialogHeader>

        {!restoreId ? (
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label htmlFor="dest">{t.snapshots.destination} <span className="text-destructive">*</span></Label>
              <Input
                id="dest"
                placeholder={t.snapshots.destinationPlaceholder}
                value={destination}
                onChange={e => setDestination(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="pass">Passphrase <span className="text-destructive">*</span></Label>
              <Input
                id="pass"
                type="password"
                placeholder="Encryption passphrase"
                value={passphrase}
                onChange={e => setPassphrase(e.target.value)}
              />
            </div>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium">{t.snapshots.overwrite}</p>
                <p className="text-xs text-muted-foreground">
                  Skip files that already exist at the destination
                </p>
              </div>
              <Switch checked={overwrite} onCheckedChange={setOverwrite} />
            </div>
            {startError && (
              <p className="text-sm text-destructive">{startError}</p>
            )}
            <DialogFooter>
              <Button variant="outline" onClick={handleClose}>{t.common.cancel}</Button>
              <Button
                onClick={handleStart}
                disabled={!destination || !passphrase || isStarting}
              >
                {isStarting
                  ? <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> {t.snapshots.restoring}</>
                  : <><RotateCcw className="h-4 w-4 mr-2" /> {t.snapshots.startRestore}</>
                }
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground">
                  {restoreStatus
                    ? interp(t.snapshots.restoreProgress, { done: restoreStatus.files_restored, total: restoreStatus.files_total })
                    : t.common.loading
                  }
                </span>
                <Badge variant={statusVariant(restoreStatus?.status ?? 'queued')}>
                  {restoreStatus?.status ?? 'queued'}
                </Badge>
              </div>
              <Progress value={progressPct} className="h-2" />
            </div>

            {restoreStatus && (
              <div className="text-sm space-y-1 text-muted-foreground">
                <div className="flex justify-between">
                  <span>Bytes restored</span>
                  <span className="font-medium text-foreground">
                    {formatBytes(restoreStatus.bytes_restored)}
                  </span>
                </div>
                {restoreStatus.finished_at && (
                  <div className="flex justify-between">
                    <span>Finished</span>
                    <span className="font-medium text-foreground">
                      {formatDate(restoreStatus.finished_at)}
                    </span>
                  </div>
                )}
              </div>
            )}

            {isDone && restoreStatus?.status === 'success' && (
              <div className="rounded-md bg-green-50 dark:bg-green-950 p-3 text-sm text-green-800 dark:text-green-200">
                {interp(t.snapshots.restoreComplete, {
                  done: restoreStatus.files_restored,
                  bytes: formatBytes(restoreStatus.bytes_restored),
                })}
              </div>
            )}

            {isDone && restoreStatus?.errors && restoreStatus.errors.length > 0 && (
              <div className="rounded-md bg-red-50 dark:bg-red-950 p-3 text-sm space-y-1">
                <p className="font-medium text-red-700 dark:text-red-300">{t.snapshots.restoreError}:</p>
                {restoreStatus.errors.map((e, i) => (
                  <p key={i} className="font-mono text-red-600 dark:text-red-400 text-xs">{e}</p>
                ))}
              </div>
            )}

            {!isRunning && (
              <DialogFooter>
                <Button onClick={handleClose}>{t.common.close}</Button>
              </DialogFooter>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}


// ── Snapshot detail modal ──────────────────────────────────────────────────────

function SnapshotDetailModal({
  snapshot,
  open,
  onOpenChange,
}: {
  snapshot: SnapshotResponse
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const { t, interp } = useT()

  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [restoreOpen, setRestoreOpen] = useState(false)
  const [restoreFiles, setRestoreFiles] = useState<FileResponse[] | null>(null)

  const { data: detail, isLoading } = useQuery<SnapshotDetail>({
    queryKey: ['snapshot', snapshot.snapshot_id, snapshot.job_id],
    queryFn: () => api.snapshots.get(snapshot.snapshot_id, snapshot.job_id),
    enabled: open,
  })

  const allFiles = detail?.files ?? []
  const filtered = allFiles.filter(f =>
    f.path.toLowerCase().includes(search.toLowerCase()),
  )
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE)
  const paginated = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)

  // Reset page when search changes
  useEffect(() => { setPage(0) }, [search])

  function toggleSelect(fileId: string) {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(fileId)) next.delete(fileId)
      else next.add(fileId)
      return next
    })
  }

  function toggleAll() {
    if (selected.size === paginated.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(paginated.map(f => f.file_id)))
    }
  }

  function openRestoreSelected() {
    const files = allFiles.filter(f => selected.has(f.file_id))
    setRestoreFiles(files)
    setRestoreOpen(true)
  }

  function openRestoreAll() {
    setRestoreFiles(null)
    setRestoreOpen(true)
  }

  const allPageSelected = paginated.length > 0 && paginated.every(f => selected.has(f.file_id))

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-4xl max-h-[90vh] overflow-hidden flex flex-col">
          <DialogHeader>
            <DialogTitle>{interp(t.snapshots.filesIn, { id: snapshot.snapshot_id.slice(0, 12) })}</DialogTitle>
            <DialogDescription>
              <span className="font-mono text-xs">{snapshot.snapshot_id}</span>
              {' · '}
              {allFiles.length} file{allFiles.length !== 1 ? 's' : ''}
              {snapshot.total_bytes ? ` · ${formatBytes(snapshot.total_bytes)}` : ''}
            </DialogDescription>
          </DialogHeader>

          {/* Search + action row */}
          <div className="flex items-center gap-3 flex-shrink-0">
            <div className="relative flex-1">
              <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder={t.snapshots.searchFiles}
                className="pl-8"
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
            </div>
            {selected.size > 0 && (
              <Button size="sm" onClick={openRestoreSelected}>
                <RotateCcw className="h-3 w-3 mr-1.5" />
                {t.snapshots.restoreSelected} ({selected.size})
              </Button>
            )}
            <Button size="sm" variant="outline" onClick={openRestoreAll}>
              <RotateCcw className="h-3 w-3 mr-1.5" />
              {t.snapshots.restoreAll}
            </Button>
          </div>

          {/* File table */}
          <div className="flex-1 overflow-y-auto rounded-md border">
            {isLoading ? (
              <div className="flex items-center justify-center py-16">
                <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
              </div>
            ) : filtered.length === 0 ? (
              <div className="py-16 text-center text-muted-foreground text-sm">
                {t.snapshots.noFiles}
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-10">
                      <button onClick={toggleAll} className="flex items-center">
                        {allPageSelected
                          ? <SquareCheck className="h-4 w-4 text-primary" />
                          : <Square className="h-4 w-4 text-muted-foreground" />
                        }
                      </button>
                    </TableHead>
                    <TableHead>{t.snapshots.path}</TableHead>
                    <TableHead className="text-right">{t.snapshots.fileSize}</TableHead>
                    <TableHead className="text-right">{t.snapshots.modified}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {paginated.map(f => (
                    <TableRow
                      key={f.file_id}
                      className="cursor-pointer hover:bg-muted/50"
                      onClick={() => toggleSelect(f.file_id)}
                    >
                      <TableCell>
                        {selected.has(f.file_id)
                          ? <SquareCheck className="h-4 w-4 text-primary" />
                          : <Square className="h-4 w-4 text-muted-foreground" />
                        }
                      </TableCell>
                      <TableCell className="font-mono text-xs truncate max-w-[400px]">
                        {f.path}
                      </TableCell>
                      <TableCell className="text-right text-sm">
                        {formatBytes(f.size)}
                      </TableCell>
                      <TableCell className="text-right text-sm text-muted-foreground">
                        {f.mtime ? formatDate(new Date(f.mtime * 1000).toISOString()) : '—'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between flex-shrink-0 text-sm text-muted-foreground">
              <span>
                {interp(t.snapshots.page, { current: page + 1, total: totalPages })}
              </span>
              <div className="flex gap-1">
                <Button
                  variant="outline" size="sm"
                  onClick={() => setPage(p => p - 1)}
                  disabled={page === 0}
                >
                  <ChevronLeft className="h-4 w-4" />
                </Button>
                <Button
                  variant="outline" size="sm"
                  onClick={() => setPage(p => p + 1)}
                  disabled={page >= totalPages - 1}
                >
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {restoreOpen && (
        <RestoreModal
          snapshot={snapshot}
          selectedFiles={restoreFiles}
          open={restoreOpen}
          onOpenChange={setRestoreOpen}
        />
      )}
    </>
  )
}


// ── Confirm delete dialog ──────────────────────────────────────────────────────

function ConfirmDeleteDialog({
  snapshot,
  open,
  onOpenChange,
  onConfirm,
  loading,
}: {
  snapshot: SnapshotResponse | null
  open: boolean
  onOpenChange: (o: boolean) => void
  onConfirm: () => void
  loading: boolean
}) {
  const { t, interp } = useT()

  if (!snapshot) return null

  const freeable = snapshot.total_bytes ? `~${formatBytes(snapshot.total_bytes)}` : ''

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{interp(t.snapshots.confirmDelete, { id: snapshot.snapshot_id.slice(0, 12) })}</DialogTitle>
          <DialogDescription>
            {interp(t.snapshots.confirmDeleteDesc, { size: freeable || '—' })}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t.common.cancel}
          </Button>
          <Button variant="destructive" onClick={onConfirm} disabled={loading}>
            {loading ? (
              <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> {t.common.deleting}</>
            ) : (
              <><Trash2 className="h-4 w-4 mr-2" /> {t.snapshots.delete}{freeable ? ` (free ${freeable})` : ''}</>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}


// ── Page ──────────────────────────────────────────────────────────────────────

type SortKey = 'started_at' | 'status' | 'total_bytes' | 'job_id'

export default function Snapshots() {
  const { t } = useT()
  const qc = useQueryClient()
  const [jobFilter, setJobFilter] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('started_at')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [deleteTarget, setDeleteTarget] = useState<SnapshotResponse | null>(null)
  const [detailTarget, setDetailTarget] = useState<SnapshotResponse | null>(null)

  const { data: snapshots = [], isLoading, error } = useQuery({
    queryKey: ['snapshots', jobFilter],
    queryFn: () => api.snapshots.list(jobFilter || undefined),
  })

  const { data: jobs = [] } = useQuery({
    queryKey: ['jobs'],
    queryFn: api.jobs.list,
  })

  const deleteMutation = useMutation({
    mutationFn: (s: SnapshotResponse) => api.snapshots.delete(s.snapshot_id, s.job_id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['snapshots'] })
      qc.invalidateQueries({ queryKey: ['storage'] })
      setDeleteTarget(null)
    },
  })

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir(d => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  const sorted = [...snapshots].sort((a, b) => {
    let av: string | number = a[sortKey] ?? ''
    let bv: string | number = b[sortKey] ?? ''
    if (typeof av === 'string') av = av.toLowerCase()
    if (typeof bv === 'string') bv = bv.toLowerCase()
    return sortDir === 'asc'
      ? av < bv ? -1 : av > bv ? 1 : 0
      : av > bv ? -1 : av < bv ? 1 : 0
  })

  function SortIcon({ col }: { col: SortKey }) {
    if (sortKey !== col) return null
    return sortDir === 'asc'
      ? <ChevronUp className="inline h-3 w-3 ml-1" />
      : <ChevronDown className="inline h-3 w-3 ml-1" />
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">{t.snapshots.title}</h1>
          <p className="text-muted-foreground mt-1">
            {snapshots.length} snapshot{snapshots.length !== 1 ? 's' : ''}
            {jobFilter && ` for job "${jobFilter}"`}
          </p>
        </div>

        {/* Job filter */}
        <div className="flex items-center gap-2">
          <label htmlFor="job-filter" className="text-sm text-muted-foreground">
            {t.snapshots.filterByJob}:
          </label>
          <select
            id="job-filter"
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            value={jobFilter}
            onChange={e => setJobFilter(e.target.value)}
          >
            <option value="">{t.snapshots.allJobs}</option>
            {jobs.map(j => (
              <option key={j.job_id} value={j.job_id}>
                {j.job_id}
              </option>
            ))}
          </select>
        </div>
      </div>

      <Card>
        {isLoading ? (
          <CardContent className="flex items-center justify-center py-16">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </CardContent>
        ) : error ? (
          <CardContent className="py-12 text-center">
            <AlertCircle className="h-6 w-6 text-destructive mx-auto mb-2" />
            <p className="text-destructive text-sm">{String(error)}</p>
          </CardContent>
        ) : sorted.length === 0 ? (
          <CardContent className="py-16 text-center text-muted-foreground">
            {t.snapshots.noSnapshots}
          </CardContent>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead
                  className="cursor-pointer select-none"
                  onClick={() => toggleSort('snapshot_id' as SortKey)}
                >
                  {t.snapshots.snapshotId}
                </TableHead>
                <TableHead
                  className="cursor-pointer select-none"
                  onClick={() => toggleSort('job_id')}
                >
                  {t.snapshots.job} <SortIcon col="job_id" />
                </TableHead>
                <TableHead
                  className="cursor-pointer select-none"
                  onClick={() => toggleSort('status')}
                >
                  {t.snapshots.status} <SortIcon col="status" />
                </TableHead>
                <TableHead
                  className="cursor-pointer select-none"
                  onClick={() => toggleSort('started_at')}
                >
                  {t.snapshots.started} <SortIcon col="started_at" />
                </TableHead>
                <TableHead
                  className="cursor-pointer select-none text-right"
                  onClick={() => toggleSort('total_bytes')}
                >
                  {t.snapshots.size} <SortIcon col="total_bytes" />
                </TableHead>
                <TableHead className="text-right">{t.snapshots.chunks}</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {sorted.map(s => (
                <TableRow key={s.snapshot_id}>
                  <TableCell className="font-mono text-xs">
                    {s.snapshot_id.slice(0, 16)}…
                  </TableCell>
                  <TableCell className="font-mono text-xs">{s.job_id}</TableCell>
                  <TableCell>
                    <Badge variant={statusVariant(s.status)}>{s.status}</Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground text-sm">
                    {formatDate(s.started_at)}
                  </TableCell>
                  <TableCell className="text-right">
                    {s.total_bytes ? formatBytes(s.total_bytes) : '—'}
                  </TableCell>
                  <TableCell className="text-right">
                    {s.chunk_count?.toLocaleString() ?? '—'}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center justify-end gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="text-muted-foreground hover:text-foreground"
                        onClick={() => setDetailTarget(s)}
                        title={t.snapshots.viewFiles}
                      >
                        <FolderOpen className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="text-muted-foreground hover:text-destructive"
                        onClick={() => setDeleteTarget(s)}
                        title={t.snapshots.delete}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>

      <ConfirmDeleteDialog
        snapshot={deleteTarget}
        open={!!deleteTarget}
        onOpenChange={open => !open && setDeleteTarget(null)}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget)}
        loading={deleteMutation.isPending}
      />

      {detailTarget && (
        <SnapshotDetailModal
          snapshot={detailTarget}
          open={!!detailTarget}
          onOpenChange={open => !open && setDetailTarget(null)}
        />
      )}
    </div>
  )
}
