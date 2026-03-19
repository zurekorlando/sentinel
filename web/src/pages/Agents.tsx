/**
 * Agents page — Register, monitor and browse Sentinel remote agents.
 */
import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Bot, Search, RefreshCw, Wifi, Folder, File,
  FolderOpen, ChevronRight, Plus, Trash2, Loader2,
  Pencil, Copy, Check, ChevronLeft, Eye, EyeOff, Download,
} from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'

import * as api from '../lib/api'
import type { AgentRecord } from '../lib/api'
import { useT } from '../lib/i18n'
import { Card, CardContent } from '../components/ui/card'
import { Button } from '../components/ui/button'
import { Badge } from '../components/ui/badge'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import {
  Dialog, DialogContent, DialogDescription,
  DialogFooter, DialogHeader, DialogTitle,
} from '../components/ui/dialog'


// ── Helpers ────────────────────────────────────────────────────────────────────

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`
}

function HardDriveIcon() {
  return (
    <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <line x1="22" y1="12" x2="2" y2="12" />
      <path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
      <line x1="6" y1="16" x2="6.01" y2="16" />
      <line x1="10" y1="16" x2="10.01" y2="16" />
    </svg>
  )
}


// ── Status badge ───────────────────────────────────────────────────────────────

function AgentStatusBadge({ status }: { status: AgentRecord['status'] }) {
  const { t } = useT()

  if (status === 'online') {
    return (
      <Badge className="bg-green-500 hover:bg-green-600 text-white gap-1.5">
        <span className="relative flex h-2 w-2">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-white opacity-75" />
          <span className="relative inline-flex rounded-full h-2 w-2 bg-white" />
        </span>
        {t.agents.online}
      </Badge>
    )
  }
  if (status === 'offline') {
    return (
      <Badge variant="secondary" className="gap-1.5">
        <span className="inline-flex rounded-full h-2 w-2 bg-gray-400" />
        {t.agents.offline}
      </Badge>
    )
  }
  return (
    <Badge variant="outline" className="gap-1.5 text-yellow-600 border-yellow-400">
      <span className="inline-flex rounded-full h-2 w-2 bg-yellow-400" />
      {t.agents.neverConnected}
    </Badge>
  )
}


// ── OS label ───────────────────────────────────────────────────────────────────

function OsLabel({ os }: { os: string }) {
  const emoji = os === 'Windows' ? '🪟' : os === 'Darwin' ? '🍎' : '🐧'
  const label = os || '—'
  return <span className="text-sm">{emoji} {label}</span>
}


// ── Copy button with feedback ──────────────────────────────────────────────────

function CopyButton({ text, label, className }: { text: string; label: string; className?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }
  return (
    <Button variant="outline" size="sm" onClick={copy} className={`gap-2 ${className ?? ''}`}>
      {copied ? <Check className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4" />}
      {copied ? '✓' : label}
    </Button>
  )
}


// ── Installer download ─────────────────────────────────────────────────────────

function downloadInstaller(agentId: string) {
  const a = document.createElement('a')
  a.href = `/api/agents/${agentId}/download`
  a.download = ''
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
}


// ── Add agent wizard ───────────────────────────────────────────────────────────

function AddAgentWizard({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t, interp } = useT()
  const qc = useQueryClient()

  const [step, setStep] = useState(1)
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [selectedOs, setSelectedOs] = useState<'windows' | 'linux'>('windows')
  const [created, setCreated] = useState<{ agent: AgentRecord; api_key: string } | null>(null)
  const [keyVisible, setKeyVisible] = useState(false)
  const [connectionStatus, setConnectionStatus] = useState<'waiting' | 'connected' | 'timeout'>('waiting')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const createMutation = useMutation({
    mutationFn: () => api.agents.create({ display_name: displayName, description: description || undefined }),
    onSuccess: (data) => {
      setCreated(data)
      setStep(2)
    },
  })

  // Polling for step 3
  useEffect(() => {
    if (step !== 3 || !created) return
    setConnectionStatus('waiting')

    pollRef.current = setInterval(async () => {
      try {
        const agent = await api.agents.get(created.agent.agent_id)
        if (agent.status === 'online') {
          setCreated((prev) => prev ? { ...prev, agent } : prev)
          setConnectionStatus('connected')
          clearInterval(pollRef.current!); pollRef.current = null
          clearTimeout(timeoutRef.current!); timeoutRef.current = null
        }
      } catch { /* ignore */ }
    }, 3000)

    timeoutRef.current = setTimeout(() => {
      setConnectionStatus((s) => s === 'connected' ? s : 'timeout')
      clearInterval(pollRef.current!); pollRef.current = null
    }, 120_000)

    return () => {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
      if (timeoutRef.current) { clearTimeout(timeoutRef.current); timeoutRef.current = null }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step])

  const reset = () => {
    setStep(1); setDisplayName(''); setDescription('')
    setSelectedOs('windows'); setCreated(null); setKeyVisible(false)
    setConnectionStatus('waiting')
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
    if (timeoutRef.current) { clearTimeout(timeoutRef.current); timeoutRef.current = null }
  }

  const handleClose = () => {
    reset()
    qc.invalidateQueries({ queryKey: ['agents'] })
    onClose()
  }

  // Build install commands — server URL from browser origin (no hardcoding)
  const serverUrl = window.location.origin
  const apiKey = created?.api_key ?? ''

  const windowsCmd =
    `$env:SENTINEL_AGENT_KEY="${apiKey}"\n` +
    `$env:SENTINEL_SERVER_URL="${serverUrl}"\n` +
    `$env:SENTINEL_AGENT_PORT="8765"\n` +
    `cd <${t.agents.agentDirectory}>\n` +
    `python -m uvicorn agent.main:app --host 0.0.0.0 --port 8765`

  const linuxCmd =
    `SENTINEL_AGENT_KEY="${apiKey}" \\\n` +
    `SENTINEL_SERVER_URL="${serverUrl}" \\\n` +
    `SENTINEL_AGENT_PORT="8765" \\\n` +
    `python -m uvicorn agent.main:app --host 0.0.0.0 --port 8765`

  const currentCmd = selectedOs === 'windows' ? windowsCmd : linuxCmd
  const maskedKey = apiKey ? `${'•'.repeat(Math.min(apiKey.length, 32))}` : ''

  const stepLabel = step === 1 ? t.agents.step1Title : step === 2 ? t.agents.step2Title : t.agents.step3Title

  return (
    <Dialog open={open} onOpenChange={(v) => !v && handleClose()}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t.agents.wizardTitle}</DialogTitle>
          <DialogDescription className="text-xs text-muted-foreground">
            {t.common.loading && ''}
            {`${step} / 3 — ${stepLabel}`}
          </DialogDescription>
        </DialogHeader>

        {/* ── Step 1: Name ── */}
        {step === 1 && (
          <div className="space-y-4 py-2">
            <div className="space-y-1.5">
              <Label htmlFor="agent-name">{t.agents.displayName}</Label>
              <Input
                id="agent-name"
                placeholder={t.agents.displayNamePlaceholder}
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && displayName.trim() && createMutation.mutate()}
                autoFocus
              />
              <p className="text-xs text-muted-foreground">{t.agents.displayNameHint}</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="agent-desc">{t.agents.description}</Label>
              <Input
                id="agent-desc"
                placeholder={t.agents.descriptionPlaceholder}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            {createMutation.isError && (
              <p className="text-sm text-destructive">{(createMutation.error as Error).message}</p>
            )}
          </div>
        )}

        {/* ── Step 2: Install ── */}
        {step === 2 && created && (
          <div className="space-y-4 py-2">
            {/* OS selector */}
            <div className="space-y-1.5">
              <Label>{t.agents.selectOs}</Label>
              <div className="flex gap-2">
                {(['windows', 'linux'] as const).map((os) => (
                  <Button
                    key={os}
                    variant={selectedOs === os ? 'default' : 'outline'}
                    size="sm"
                    onClick={() => setSelectedOs(os)}
                  >
                    {os === 'windows' ? '🪟' : '🐧'} {os === 'windows' ? t.agents.windows : t.agents.linux}
                  </Button>
                ))}
              </div>
            </div>

            {/* Install command */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label>{t.agents.installInstructions}</Label>
                <CopyButton text={currentCmd} label={t.agents.copyCommand} />
              </div>
              <pre className="bg-muted rounded-md p-3 text-xs font-mono overflow-x-auto whitespace-pre leading-relaxed">
                {currentCmd}
              </pre>
            </div>

            {/* API key reveal */}
            <div className="space-y-1.5">
              <Label>{t.agents.apiKey}</Label>
              <div className="flex items-center gap-2">
                <code className="flex-1 bg-muted rounded px-3 py-2 text-xs font-mono break-all">
                  {keyVisible ? apiKey : maskedKey}
                </code>
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => setKeyVisible((v) => !v)}
                  title={keyVisible ? t.agents.hideKey : t.agents.showKey}
                >
                  {keyVisible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </Button>
                <CopyButton text={apiKey} label={t.agents.copyKey} />
              </div>
            </div>

            {/* Warning */}
            <div className="rounded-md border border-yellow-400 bg-yellow-50 dark:bg-yellow-950/20 p-3 flex items-start gap-2">
              <span className="text-yellow-600 text-base leading-tight">⚠</span>
              <p className="text-sm text-yellow-700 dark:text-yellow-400 font-medium">
                {t.agents.keyWarning}
              </p>
            </div>

            {/* Download installer */}
            {selectedOs === 'windows' && (
              <Button
                size="lg"
                className="w-full gap-2"
                onClick={() => downloadInstaller(created.agent.agent_id)}
              >
                <Download className="h-5 w-5" />
                {t.agents.downloadInstaller}
              </Button>
            )}
          </div>
        )}

        {/* ── Step 3: Waiting ── */}
        {step === 3 && created && (
          <div className="py-8 flex flex-col items-center gap-5 text-center">
            {connectionStatus === 'waiting' && (
              <>
                <Loader2 className="h-12 w-12 animate-spin text-primary" />
                <p className="text-sm text-muted-foreground">{t.agents.waitingForAgent}</p>
              </>
            )}
            {connectionStatus === 'connected' && (
              <>
                <div className="flex items-center justify-center w-14 h-14 rounded-full bg-green-100 dark:bg-green-900/30">
                  <Check className="h-7 w-7 text-green-600" />
                </div>
                <p className="text-sm font-medium text-green-700 dark:text-green-400">
                  {interp(t.agents.agentConnected, {
                    hostname: created.agent.hostname || '—',
                    ip: created.agent.ip_last_seen || '—',
                  })}
                </p>
              </>
            )}
            {connectionStatus === 'timeout' && (
              <>
                <div className="flex items-center justify-center w-14 h-14 rounded-full bg-yellow-100 dark:bg-yellow-900/30">
                  <span className="text-yellow-600 text-2xl">⚠</span>
                </div>
                <p className="text-sm text-yellow-700 dark:text-yellow-400">
                  {t.agents.agentConnectionTimeout}
                </p>
              </>
            )}
          </div>
        )}

        <DialogFooter className="gap-2">
          {step > 1 && step < 3 && (
            <Button variant="outline" onClick={() => setStep((s) => s - 1)}>
              {t.agents.back}
            </Button>
          )}
          <Button variant="outline" onClick={handleClose}>
            {t.agents.cancel}
          </Button>
          {step === 1 && (
            <Button
              onClick={() => createMutation.mutate()}
              disabled={createMutation.isPending || !displayName.trim()}
            >
              {createMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {t.agents.next}
            </Button>
          )}
          {step === 2 && (
            <Button onClick={() => setStep(3)}>
              {t.agents.continueAfterInstall}
            </Button>
          )}
          {step === 3 && (
            <Button onClick={handleClose}>{t.agents.done}</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}


// ── Edit agent dialog ──────────────────────────────────────────────────────────

function EditAgentDialog({ agent, open, onClose }: { agent: AgentRecord | null; open: boolean; onClose: () => void }) {
  const { t } = useT()
  const qc = useQueryClient()
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')

  useEffect(() => {
    if (open && agent) { setDisplayName(agent.display_name); setDescription(agent.description) }
  }, [open, agent])

  const saveMutation = useMutation({
    mutationFn: () => api.agents.update(agent!.agent_id, { display_name: displayName, description }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['agents'] }); onClose() },
  })

  if (!agent) return null

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t.agents.editTitle}</DialogTitle>
          <DialogDescription>{agent.hostname || agent.agent_id}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="space-y-1.5">
            <Label htmlFor="edit-name">{t.agents.editName}</Label>
            <Input id="edit-name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="edit-desc">{t.agents.editDescription}</Label>
            <Input id="edit-desc" value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
          {saveMutation.isError && (
            <p className="text-sm text-destructive">{(saveMutation.error as Error).message}</p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t.agents.cancel}</Button>
          <Button onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending || !displayName.trim()}>
            {saveMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t.agents.saveChanges}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}


// ── Browse files dialog ────────────────────────────────────────────────────────

function BrowseFilesDialog({ agent, open, onClose }: { agent: AgentRecord | null; open: boolean; onClose: () => void }) {
  const { t, interp } = useT()
  const [history, setHistory] = useState<string[]>([''])
  const currentPath = history[history.length - 1]

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['agent-browse', agent?.agent_id, currentPath],
    queryFn: () => api.agents.browse(agent!.agent_id, currentPath || undefined),
    enabled: open && !!agent,
    retry: 1,
  })

  useEffect(() => { if (open) setHistory(['']) }, [open])

  if (!agent) return null

  const showDrives = !currentPath && !!data?.drives?.length

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-2xl max-h-[80vh] flex flex-col">
        <DialogHeader>
          <DialogTitle>
            {interp(t.agents.browseTitle, { name: agent.display_name || agent.hostname })}
          </DialogTitle>
          <DialogDescription className="font-mono text-xs truncate">
            {currentPath || (data?.platform === 'Windows' ? t.agents.drives : '/')}
          </DialogDescription>
        </DialogHeader>

        <div className="flex-1 overflow-hidden flex flex-col min-h-0">
          <div className="flex items-center gap-2 py-2 border-b">
            <Button variant="ghost" size="sm" disabled={history.length <= 1} onClick={() => setHistory((h) => h.slice(0, -1))} className="gap-1">
              <ChevronLeft className="h-4 w-4" />
              {t.agents.back}
            </Button>
          </div>

          <div className="flex-1 overflow-y-auto">
            {isLoading && (
              <div className="flex items-center justify-center py-12 text-muted-foreground">
                <Loader2 className="mr-2 h-5 w-5 animate-spin" />{t.common.loading}
              </div>
            )}
            {isError && (
              <div className="flex items-center justify-center py-12 text-sm text-destructive">
                {agent.status !== 'online' ? t.agents.agentOfflineError : (error as Error).message}
              </div>
            )}
            {!isLoading && !isError && data && (
              <>
                {showDrives && (
                  <div className="p-4">
                    <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-3">{t.agents.drives}</p>
                    {data.drives!.length === 0
                      ? <p className="text-sm text-muted-foreground">{t.agents.noDrives}</p>
                      : (
                        <div className="flex flex-wrap gap-2">
                          {data.drives!.map((drive) => (
                            <Button key={drive} variant="outline" size="sm" className="gap-2 font-mono" onClick={() => setHistory((h) => [...h, drive])}>
                              <HardDriveIcon />{drive}
                            </Button>
                          ))}
                        </div>
                      )}
                  </div>
                )}
                {(!showDrives || currentPath) && (
                  data.entries.length === 0
                    ? <div className="flex items-center justify-center py-12 text-muted-foreground text-sm">{t.agents.noFiles}</div>
                    : (
                      <div className="divide-y">
                        {data.entries.map((entry) => (
                          <div
                            key={entry.path}
                            className={`flex items-center gap-3 px-4 py-2 hover:bg-accent ${entry.is_dir ? 'cursor-pointer' : ''}`}
                            onClick={() => entry.is_dir && setHistory((h) => [...h, entry.path])}
                          >
                            {entry.is_dir
                              ? <FolderOpen className="h-4 w-4 text-yellow-500 shrink-0" />
                              : <File className="h-4 w-4 text-muted-foreground shrink-0" />}
                            <span className="flex-1 text-sm truncate">{entry.name}</span>
                            {entry.size !== null && !entry.is_dir && (
                              <span className="text-xs text-muted-foreground shrink-0">{formatBytes(entry.size)}</span>
                            )}
                            {entry.is_dir && <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />}
                          </div>
                        ))}
                      </div>
                    )
                )}
              </>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t.common.close}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}


// ── Delete dialog ──────────────────────────────────────────────────────────────

function DeleteAgentDialog({ agent, open, onClose }: { agent: AgentRecord | null; open: boolean; onClose: () => void }) {
  const { t, interp } = useT()
  const qc = useQueryClient()

  const deleteMutation = useMutation({
    mutationFn: () => api.agents.delete(agent!.agent_id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['agents'] }); onClose() },
  })

  if (!agent) return null

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{interp(t.agents.confirmDelete, { name: agent.display_name || agent.hostname })}</DialogTitle>
          <DialogDescription>{t.agents.confirmDeleteDesc}</DialogDescription>
        </DialogHeader>
        {deleteMutation.isError && (
          <p className="text-sm text-destructive">{(deleteMutation.error as Error).message}</p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t.agents.cancel}</Button>
          <Button variant="destructive" onClick={() => deleteMutation.mutate()} disabled={deleteMutation.isPending}>
            {deleteMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t.agents.deleteAgent}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}


// ── Agent card ─────────────────────────────────────────────────────────────────

interface PingState { reachable: boolean; latency_ms: number }

function AgentCard({
  agent,
  pingState,
  isPinging,
  onPing,
  onBrowse,
  onEdit,
  onDelete,
  onDownload,
}: {
  agent: AgentRecord
  pingState: PingState | undefined
  isPinging: boolean
  onPing: () => void
  onBrowse: () => void
  onEdit: () => void
  onDelete: () => void
  onDownload: () => void
}) {
  const { t, interp } = useT()

  const lastSeenText = agent.last_seen
    ? formatDistanceToNow(new Date(agent.last_seen), { addSuffix: true })
    : t.common.notAvailable

  return (
    <Card className="overflow-hidden">
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-4">
          {/* Left: identity */}
          <div className="flex-1 min-w-0 space-y-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-semibold text-base truncate">
                {agent.display_name || agent.hostname}
              </span>
              {agent.hostname && agent.display_name && (
                <span className="text-sm text-muted-foreground">({agent.hostname})</span>
              )}
              <AgentStatusBadge status={agent.status} />
            </div>

            <div className="flex items-center gap-3 flex-wrap text-sm text-muted-foreground">
              <OsLabel os={agent.os} />
              {agent.ip_last_seen && (
                <span className="font-mono text-xs">{agent.ip_last_seen}</span>
              )}
              {agent.version && (
                <span className="text-xs">v{agent.version}</span>
              )}
            </div>

            <p className="text-xs text-muted-foreground">
              {t.agents.lastSeen}: {lastSeenText}
            </p>
          </div>

          {/* Right: actions */}
          <div className="flex items-center gap-1 shrink-0">
            <Button variant="ghost" size="sm" onClick={onBrowse} title={t.agents.browseFiles}>
              <Folder className="h-4 w-4" />
            </Button>
            <Button variant="ghost" size="sm" onClick={onDownload} title={t.agents.downloadInstaller}>
              <Download className="h-4 w-4" />
            </Button>
            <Button variant="ghost" size="sm" onClick={onPing} disabled={isPinging} title={t.agents.ping}>
              {isPinging ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wifi className="h-4 w-4" />}
            </Button>
            <Button variant="ghost" size="sm" onClick={onEdit} title={t.agents.editAgent}>
              <Pencil className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost" size="sm"
              onClick={onDelete}
              title={t.agents.deleteAgent}
              className="text-destructive hover:text-destructive"
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {/* Ping result */}
        {pingState && (
          <p className={`mt-2 text-xs ${pingState.reachable ? 'text-green-600 dark:text-green-400' : 'text-destructive'}`}>
            {pingState.reachable
              ? interp(t.agents.pingSuccess, { ms: String(Math.round(pingState.latency_ms)) })
              : t.agents.pingFailed}
          </p>
        )}
      </CardContent>
    </Card>
  )
}


// ── Main page ──────────────────────────────────────────────────────────────────

export default function Agents() {
  const { t } = useT()
  const qc = useQueryClient()
  const [search, setSearch] = useState('')
  const [wizardOpen, setWizardOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<AgentRecord | null>(null)
  const [browseTarget, setBrowseTarget] = useState<AgentRecord | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<AgentRecord | null>(null)
  const [pingStates, setPingStates] = useState<Record<string, PingState>>({})
  const [pingingIds, setPingingIds] = useState<Set<string>>(new Set())

  const { data, isLoading } = useQuery({
    queryKey: ['agents'],
    queryFn: () => api.agents.list(),
    refetchInterval: 15_000,
  })

  const agentList = data?.agents ?? []
  const filtered = agentList.filter((a) => {
    const q = search.toLowerCase()
    return !q || a.display_name.toLowerCase().includes(q) || a.hostname.toLowerCase().includes(q) || a.ip_last_seen.toLowerCase().includes(q)
  })

  const handlePing = async (agent: AgentRecord) => {
    setPingingIds((s) => new Set(s).add(agent.agent_id))
    try {
      const result = await api.agents.ping(agent.agent_id)
      setPingStates((prev) => ({ ...prev, [agent.agent_id]: { reachable: result.reachable, latency_ms: result.latency_ms } }))
      qc.invalidateQueries({ queryKey: ['agents'] })
    } catch {
      setPingStates((prev) => ({ ...prev, [agent.agent_id]: { reachable: false, latency_ms: 0 } }))
    } finally {
      setPingingIds((s) => { const next = new Set(s); next.delete(agent.agent_id); return next })
    }
  }

  return (
    <div className="p-8 space-y-6 max-w-4xl">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">{t.agents.title}</h1>
          <p className="text-muted-foreground mt-1">{t.agents.subtitle}</p>
        </div>
        <Button onClick={() => setWizardOpen(true)} className="shrink-0 gap-2">
          <Plus className="h-4 w-4" />
          {t.agents.addAgent}
        </Button>
      </div>

      {/* Search + refresh bar */}
      <div className="flex items-center gap-2">
        <div className="relative flex-1 max-w-xs">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            className="pl-8"
            placeholder={t.agents.searchPlaceholder}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <Button variant="ghost" size="sm" onClick={() => qc.invalidateQueries({ queryKey: ['agents'] })}>
          <RefreshCw className="h-4 w-4" />
        </Button>
      </div>

      {/* Agent list */}
      {isLoading ? (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          <Loader2 className="mr-2 h-5 w-5 animate-spin" />
          {t.common.loading}
        </div>
      ) : agentList.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-muted-foreground gap-4">
          <Bot className="h-16 w-16 opacity-20" />
          <div className="text-center space-y-1">
            <p className="font-medium">{t.agents.noAgents}</p>
            <p className="text-sm text-center max-w-xs">{t.agents.noAgentsDesc}</p>
          </div>
          <Button onClick={() => setWizardOpen(true)} size="sm" className="gap-2">
            <Plus className="h-4 w-4" />
            {t.agents.addAgent}
          </Button>
        </div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-12 text-muted-foreground text-sm">{t.common.noData}</div>
      ) : (
        <div className="grid gap-3">
          {filtered.map((agent) => (
            <AgentCard
              key={agent.agent_id}
              agent={agent}
              pingState={pingStates[agent.agent_id]}
              isPinging={pingingIds.has(agent.agent_id)}
              onPing={() => handlePing(agent)}
              onBrowse={() => setBrowseTarget(agent)}
              onEdit={() => setEditTarget(agent)}
              onDelete={() => setDeleteTarget(agent)}
              onDownload={() => downloadInstaller(agent.agent_id)}
            />
          ))}
        </div>
      )}

      {/* Dialogs */}
      <AddAgentWizard open={wizardOpen} onClose={() => setWizardOpen(false)} />
      <EditAgentDialog agent={editTarget} open={editTarget !== null} onClose={() => setEditTarget(null)} />
      <BrowseFilesDialog agent={browseTarget} open={browseTarget !== null} onClose={() => setBrowseTarget(null)} />
      <DeleteAgentDialog agent={deleteTarget} open={deleteTarget !== null} onClose={() => setDeleteTarget(null)} />
    </div>
  )
}
