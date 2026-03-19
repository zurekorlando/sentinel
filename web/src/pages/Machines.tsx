/**
 * Machines page — LAN network discovery, Windows machine management,
 * SMB credential setup, and agent-based VSS backup job creation.
 */
import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Monitor, Search, RefreshCw, Wifi, HardDrive,
  Key, Plus, Trash2, Loader2, Zap, Download,
  Folder, FolderOpen, ChevronRight, X, Check, ShieldCheck,
} from 'lucide-react'

import * as api from '../lib/api'
import type { MachineRecord } from '../lib/api'
import { useT } from '../lib/i18n'
import { formatDate } from '../lib/utils'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Button } from '../components/ui/button'
import { Badge } from '../components/ui/badge'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import {
  Dialog, DialogContent, DialogDescription,
  DialogFooter, DialogHeader, DialogTitle,
} from '../components/ui/dialog'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '../components/ui/table'


// ── Machine status badge ───────────────────────────────────────────────────────

function MachineStatusBadge({ machine }: { machine: MachineRecord }) {
  const { t } = useT()

  if (machine.agent_detected) {
    return (
      <Badge className="bg-green-500 hover:bg-green-600 text-white gap-1">
        <Zap className="h-3 w-3" />
        {t.machines.badgeAgent}
      </Badge>
    )
  }
  if (machine.smb_available) {
    return (
      <Badge variant="secondary" className="gap-1">
        <HardDrive className="h-3 w-3" />
        {t.machines.badgeSMB}
      </Badge>
    )
  }
  if (machine.windows_likely) {
    return (
      <Badge variant="outline" className="gap-1">
        <Monitor className="h-3 w-3" />
        {t.machines.badgeWindows}
      </Badge>
    )
  }
  return <Badge variant="outline">{t.machines.badgeUnknown}</Badge>
}


// ── Credentials dialog ────────────────────────────────────────────────────────

interface CredentialsDialogProps {
  machine: MachineRecord | null
  open: boolean
  onClose: () => void
}

function CredentialsDialog({ machine, open, onClose }: CredentialsDialogProps) {
  const { t, interp } = useT()
  const qc = useQueryClient()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [domain, setDomain] = useState('')

  const saveMutation = useMutation({
    mutationFn: () =>
      api.machines.setCredentials(machine!.ip, {
        username,
        password,
        domain: domain || undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['machines'] })
      onClose()
    },
  })

  useEffect(() => {
    if (open) {
      setUsername('')
      setPassword('')
      setDomain('')
    }
  }, [open])

  if (!machine) return null

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t.machines.smbCredsTitle}</DialogTitle>
          <DialogDescription>
            {interp(t.machines.smbCredsDesc, { name: machine.hostname || machine.ip })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="space-y-1">
            <Label htmlFor="domain">{t.machines.domain}</Label>
            <Input
              id="domain"
              placeholder={t.machines.domainPlaceholder}
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="username">{t.machines.username}</Label>
            <Input
              id="username"
              placeholder={t.machines.usernamePlaceholder}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="password">{t.machines.password}</Label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
        </div>

        {saveMutation.isError && (
          <p className="text-sm text-destructive">
            {(saveMutation.error as Error).message}
          </p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t.common.cancel}</Button>
          <Button
            onClick={() => saveMutation.mutate()}
            disabled={saveMutation.isPending || !username}
          >
            {saveMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t.common.save}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}


// ── Agent key dialog ──────────────────────────────────────────────────────────

interface AgentKeyDialogProps {
  machine: MachineRecord | null
  open: boolean
  onClose: () => void
}

function AgentKeyDialog({ machine, open, onClose }: AgentKeyDialogProps) {
  const { t, interp } = useT()
  const qc = useQueryClient()
  const [agentKey, setAgentKey] = useState('')

  const saveMutation = useMutation({
    mutationFn: () => api.machines.setAgentKey(machine!.ip, agentKey),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['machines'] })
      onClose()
    },
  })

  useEffect(() => { if (open) setAgentKey('') }, [open])

  if (!machine) return null

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t.machines.agentKeyTitle}</DialogTitle>
          <DialogDescription>
            {interp(t.machines.agentKeyDesc, { name: machine.hostname || machine.ip })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-1 py-2">
          <Label htmlFor="agent-key">{t.machines.agentKeyLabel}</Label>
          <Input
            id="agent-key"
            type="password"
            placeholder={t.machines.agentKeyPlaceholder}
            value={agentKey}
            onChange={(e) => setAgentKey(e.target.value)}
          />
        </div>

        {saveMutation.isError && (
          <p className="text-sm text-destructive">
            {(saveMutation.error as Error).message}
          </p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t.common.cancel}</Button>
          <Button
            onClick={() => saveMutation.mutate()}
            disabled={saveMutation.isPending}
          >
            {saveMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t.common.save}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}


// ── Folder picker (agent browse) ──────────────────────────────────────────────

interface FolderPickerProps {
  ip: string
  selectedPaths: string[]
  onToggle: (path: string) => void
}

function FolderPicker({ ip, selectedPaths, onToggle }: FolderPickerProps) {
  const { t } = useT()
  const [currentPath, setCurrentPath] = useState('C:\\')
  const [history, setHistory] = useState<string[]>([])

  const { data, isLoading, isError } = useQuery({
    queryKey: ['browse', ip, currentPath],
    queryFn: () => api.machines.browse(ip, currentPath),
    retry: 1,
  })

  const navigate = (path: string) => {
    setHistory((h) => [...h, currentPath])
    setCurrentPath(path)
  }

  const goBack = () => {
    const prev = history[history.length - 1]
    if (prev !== undefined) {
      setHistory((h) => h.slice(0, -1))
      setCurrentPath(prev)
    }
  }

  const dirs = data?.entries.filter((e) => e.is_dir) ?? []

  return (
    <div className="border rounded-md overflow-hidden">
      {/* Path bar */}
      <div className="flex items-center gap-1 px-2 py-1.5 bg-muted/40 border-b text-xs font-mono">
        {history.length > 0 && (
          <button
            className="text-muted-foreground hover:text-foreground p-0.5"
            onClick={goBack}
          >
            ←
          </button>
        )}
        <span className="truncate flex-1">{currentPath}</span>
        {isLoading && <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />}
      </div>

      {/* "Select this folder" row */}
      <button
        className={`w-full flex items-center gap-2 px-2 py-1.5 text-sm hover:bg-accent text-left border-b ${
          selectedPaths.includes(currentPath) ? 'bg-primary/10 text-primary' : ''
        }`}
        onClick={() => onToggle(currentPath)}
      >
        {selectedPaths.includes(currentPath)
          ? <Check className="h-3.5 w-3.5 text-primary shrink-0" />
          : <FolderOpen className="h-3.5 w-3.5 text-yellow-500 shrink-0" />}
        <span className="font-medium">{t.machines.selectFolders}</span>
      </button>

      {/* Directory listing */}
      <div className="max-h-40 overflow-y-auto">
        {isError ? (
          <p className="px-3 py-2 text-xs text-destructive">
            {t.machines.folderPickerError}
          </p>
        ) : dirs.length === 0 && !isLoading ? (
          <p className="px-3 py-2 text-xs text-muted-foreground">{t.machines.noSubfolders}</p>
        ) : (
          dirs.map((entry) => {
            const isSelected = selectedPaths.includes(entry.path)
            return (
              <div
                key={entry.path}
                className={`flex items-center group hover:bg-accent ${isSelected ? 'bg-primary/10' : ''}`}
              >
                <button
                  className="flex items-center gap-2 px-2 py-1 text-sm flex-1 text-left"
                  onClick={() => onToggle(entry.path)}
                >
                  {isSelected
                    ? <Check className="h-3.5 w-3.5 text-primary shrink-0" />
                    : <Folder className="h-3.5 w-3.5 text-yellow-500/80 shrink-0" />}
                  <span className="truncate">{entry.name}</span>
                </button>
                <button
                  className="px-2 py-1 opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-foreground"
                  title="Open folder"
                  onClick={() => navigate(entry.path)}
                >
                  <ChevronRight className="h-3.5 w-3.5" />
                </button>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}


// ── Create job dialog ─────────────────────────────────────────────────────────

interface CreateJobDialogProps {
  machine: MachineRecord | null
  open: boolean
  onClose: () => void
}

function CreateJobDialog({ machine, open, onClose }: CreateJobDialogProps) {
  const { t, interp } = useT()
  const qc = useQueryClient()
  const [jobId, setJobId] = useState('')
  const [sourceType, setSourceType] = useState<'smb' | 'agent'>('smb')
  const [share, setShare] = useState('C$')

  // Agent folder selection
  const [agentPaths, setAgentPaths] = useState<string[]>([])

  // Destination
  const [destPath, setDestPath] = useState('')

  const togglePath = (path: string) => {
    setAgentPaths((prev) =>
      prev.includes(path) ? prev.filter((p) => p !== path) : [...prev, path]
    )
  }

  const createMutation = useMutation({
    mutationFn: () =>
      api.machines.createJob(machine!.ip, {
        job_id: jobId,
        share,
        source_type: sourceType,
        agent_paths: sourceType === 'agent' ? agentPaths : undefined,
        storage_base_path: destPath.trim() || undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
    },
  })

  useEffect(() => {
    if (open && machine) {
      const safe = (machine.hostname || machine.ip).replace(/[^a-zA-Z0-9]/g, '-')
      setJobId(`win-${safe}`.toLowerCase().slice(0, 40))
      setSourceType(machine.agent_detected ? 'agent' : 'smb')
      setAgentPaths([])
      setDestPath('')
    }
  }, [open, machine])

  if (!machine) return null

  const jobIdValid = /^[a-zA-Z0-9_-]+$/.test(jobId)

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{interp(t.machines.createJobTitle, { name: machine.hostname || machine.ip })}</DialogTitle>
          <DialogDescription>
            Configure a Sentinel backup job for {machine.hostname || machine.ip}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2 max-h-[70vh] overflow-y-auto pr-1">

          {/* Method */}
          <div className="space-y-1">
            <Label>{t.machines.backupMethod}</Label>
            <div className="flex gap-2">
              <Button
                variant={sourceType === 'smb' ? 'default' : 'outline'}
                size="sm"
                onClick={() => setSourceType('smb')}
                disabled={!machine.smb_available}
              >
                <HardDrive className="mr-1 h-3 w-3" />
                {t.machines.methodSmb}
              </Button>
              <Button
                variant={sourceType === 'agent' ? 'default' : 'outline'}
                size="sm"
                onClick={() => setSourceType('agent')}
                disabled={!machine.agent_detected}
              >
                <Zap className="mr-1 h-3 w-3" />
                {t.machines.methodAgent}
                {!machine.agent_detected && (
                  <span className="ml-1 text-xs opacity-60">(not installed)</span>
                )}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {sourceType === 'agent'
                ? t.machines.methodAgentDesc
                : t.machines.methodSmbDesc}
            </p>
          </div>

          {/* SMB share */}
          {sourceType === 'smb' && (
            <div className="space-y-1">
              <Label htmlFor="share">{t.machines.smbShare}</Label>
              <Input
                id="share"
                placeholder="C$"
                value={share}
                onChange={(e) => setShare(e.target.value)}
              />
            </div>
          )}

          {/* Agent folder picker */}
          {sourceType === 'agent' && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label>{t.machines.browseFolders}</Label>
                {agentPaths.length === 0 && (
                  <span className="text-xs text-muted-foreground">default: entire C:</span>
                )}
              </div>

              <FolderPicker
                ip={machine.ip}
                selectedPaths={agentPaths}
                onToggle={togglePath}
              />

              {/* Selected chips */}
              {agentPaths.length > 0 && (
                <div className="flex flex-wrap gap-1 mt-1">
                  {agentPaths.map((p) => (
                    <span
                      key={p}
                      className="flex items-center gap-1 bg-primary/10 text-primary text-xs rounded px-2 py-0.5"
                    >
                      <Folder className="h-3 w-3" />
                      {p}
                      <button
                        onClick={() => togglePath(p)}
                        className="hover:text-destructive"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Destination */}
          <div className="space-y-1">
            <Label htmlFor="dest-path">{t.machines.destination}</Label>
            <Input
              id="dest-path"
              placeholder={interp(t.machines.destinationPlaceholder, { id: jobId || 'job-id' })}
              value={destPath}
              onChange={(e) => setDestPath(e.target.value)}
            />
          </div>

          {/* Job ID */}
          <div className="space-y-1">
            <Label htmlFor="job-id">{t.machines.jobId}</Label>
            <Input
              id="job-id"
              placeholder={t.machines.jobIdPlaceholder}
              value={jobId}
              onChange={(e) => setJobId(e.target.value)}
            />
            {jobId && !jobIdValid && (
              <p className="text-xs text-destructive">
                Only letters, numbers, hyphens and underscores allowed.
              </p>
            )}
          </div>
        </div>

        {createMutation.isError && (
          <p className="text-sm text-destructive">
            {(createMutation.error as Error).message}
          </p>
        )}
        {createMutation.isSuccess && (
          <p className="text-sm text-green-600">
            {interp(t.machines.jobCreated, { id: jobId })}
          </p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {createMutation.isSuccess ? t.common.close : t.common.cancel}
          </Button>
          {!createMutation.isSuccess && (
            <Button
              onClick={() => createMutation.mutate()}
              disabled={createMutation.isPending || !jobIdValid || !jobId}
            >
              {createMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {t.machines.createJobBtn}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}


// ── Agent download card ───────────────────────────────────────────────────────

function AgentDownloadCard() {
  const { t, interp } = useT()

  const { data: info, isLoading, isError } = useQuery({
    queryKey: ['agent-info'],
    queryFn: () => api.machines.agentInfo(),
    retry: 1,
  })

  const sizeKB = info ? Math.round(info.size_bytes / 1024) : null
  const builtDate = info ? new Date(info.built_at * 1000).toLocaleDateString() : null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Zap className="h-4 w-4 text-green-500" />
          {t.machines.agentTitle}
        </CardTitle>
        <CardDescription>
          {t.machines.agentDesc}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">

        {/* Download button */}
        <div className="flex items-center gap-4 p-4 rounded-lg border bg-muted/30">
          <div className="flex-1">
            {isLoading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t.common.loading}
              </div>
            ) : isError ? (
              <div className="text-sm text-muted-foreground">
                {t.machines.agentNotBuilt}
                <code className="ml-1 bg-muted px-1 rounded text-xs">
                  python agent/build_package.py
                </code>
              </div>
            ) : (
              <div>
                <p className="font-medium text-sm">{info!.filename}</p>
                <p className="text-xs text-muted-foreground mt-0.5">
                  {interp(t.machines.agentVersion, { version: info!.version })} · {interp(t.machines.agentSize, { size: String(sizeKB), date: builtDate ?? '' })}
                </p>
              </div>
            )}
          </div>
          <a href="/api/machines/agent/download" download>
            <Button disabled={isLoading || isError} className="gap-2">
              <Download className="h-4 w-4" />
              {t.machines.downloadAgent}
            </Button>
          </a>
        </div>

        {/* Installation steps */}
        <div className="space-y-2 text-sm">
          <p className="font-medium text-muted-foreground uppercase text-xs tracking-wide">
            {t.machines.installGuide}
          </p>
          <ol className="space-y-2 text-muted-foreground list-none">
            <Step n={1}>{t.machines.installStep1}</Step>
            <Step n={2}>{t.machines.installStep2}</Step>
            <Step n={3}>
              {t.machines.installStep3}
              <pre className="bg-muted rounded p-2 mt-1 text-xs overflow-x-auto">
{`# Or from an Admin PowerShell:
powershell -ExecutionPolicy Bypass -File Install-SentinelAgent.ps1`}
              </pre>
              The installer will:
              <ul className="list-disc list-inside mt-1 ml-3 space-y-0.5 text-xs">
                <li>Verify Python 3.10+ (prompts to download if missing)</li>
                <li>Create <code className="bg-muted px-1 rounded">C:\SentinelAgent\</code> and copy files</li>
                <li>Create a virtual environment and install FastAPI + Uvicorn</li>
                <li>Download NSSM and register <strong>SentinelAgent</strong> as a Windows Service</li>
                <li>Open port <strong>7700</strong> in Windows Firewall automatically</li>
              </ul>
            </Step>
            <Step n={4}>{t.machines.installStep4}</Step>
            <Step n={5}>
              {t.machines.installStep5}{' '}
              <Badge className="bg-green-500 text-white text-xs inline-flex align-middle">
                <Zap className="h-2.5 w-2.5 mr-0.5" />{t.machines.badgeAgent}
              </Badge>{' '}
              badge.
            </Step>
          </ol>
        </div>

        {/* Uninstall note */}
        <p className="text-xs text-muted-foreground border-t pt-3">
          {t.machines.uninstallTitle}: <code className="bg-muted px-1 rounded">{t.machines.uninstallCmd}</code>
        </p>
      </CardContent>
    </Card>
  )
}

function Step({ n, children }: { n: number; children: React.ReactNode }) {
  return (
    <li className="flex gap-3">
      <span className="flex-shrink-0 flex items-center justify-center w-5 h-5 rounded-full bg-primary/10 text-primary text-xs font-bold mt-0.5">
        {n}
      </span>
      <span>{children}</span>
    </li>
  )
}


// ── Main page ─────────────────────────────────────────────────────────────────

export default function Machines() {
  const { t, interp } = useT()
  const qc = useQueryClient()
  const [network, setNetwork] = useState('')
  const [credsMachine, setCredsMachine] = useState<MachineRecord | null>(null)
  const [agentKeyMachine, setAgentKeyMachine] = useState<MachineRecord | null>(null)
  const [jobMachine, setJobMachine] = useState<MachineRecord | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const { data: listData, isLoading: listLoading } = useQuery({
    queryKey: ['machines'],
    queryFn: () => api.machines.list(),
    refetchInterval: 10_000,
  })

  const { data: scanStatus } = useQuery({
    queryKey: ['machines-scan-status'],
    queryFn: () => api.machines.scanStatus(),
    refetchInterval: (query) =>
      (query.state.data as api.ScanStatus | undefined)?.running ? 1500 : false,
  })

  const scanMutation = useMutation({
    mutationFn: () => api.machines.scan(network || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['machines-scan-status'] })
      // Poll until done
      pollRef.current = setInterval(() => {
        qc.invalidateQueries({ queryKey: ['machines-scan-status'] })
        qc.invalidateQueries({ queryKey: ['machines'] })
      }, 2000)
    },
  })

  const pingMutation = useMutation({
    mutationFn: (ip: string) => api.machines.ping(ip),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['machines'] }),
  })

  const deleteMutation = useMutation({
    mutationFn: (ip: string) => api.machines.delete(ip),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['machines'] }),
  })

  // Stop polling when scan finishes
  useEffect(() => {
    if (!scanStatus?.running && pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
      qc.invalidateQueries({ queryKey: ['machines'] })
    }
  }, [scanStatus?.running, qc])

  const machines = listData?.machines ?? []
  const isScanning = scanStatus?.running ?? false

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{t.machines.title}</h1>
        <p className="text-muted-foreground text-sm mt-1">
          {t.machines.subtitle}
        </p>
      </div>

      {/* Scan Controls */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t.machines.discoveryTitle}</CardTitle>
          <CardDescription>
            {t.machines.discoveryDesc}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex gap-3 items-end">
            <div className="flex-1 space-y-1">
              <Label htmlFor="network">{t.machines.networkRange}</Label>
              <Input
                id="network"
                placeholder={t.machines.networkPlaceholder}
                value={network}
                onChange={(e) => setNetwork(e.target.value)}
                disabled={isScanning}
              />
            </div>
            <Button
              onClick={() => scanMutation.mutate()}
              disabled={isScanning || scanMutation.isPending}
              className="shrink-0"
            >
              {isScanning ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  {t.machines.scanning}
                </>
              ) : (
                <>
                  <Search className="mr-2 h-4 w-4" />
                  {t.machines.scanNetwork}
                </>
              )}
            </Button>
          </div>

          {isScanning && (
            <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              {interp(t.machines.scanRunning, { network: scanStatus?.network ?? '' })} — {scanStatus?.hosts_found} host(s) found so far…
            </div>
          )}

          {!isScanning && scanStatus?.finished_at && (
            <p className="mt-2 text-sm text-muted-foreground">
              {interp(t.machines.scanComplete, { count: scanStatus.hosts_found })}
              {scanStatus.error && (
                <span className="text-destructive ml-2">Error: {scanStatus.error}</span>
              )}
            </p>
          )}
        </CardContent>
      </Card>

      {/* Machine List */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-base">
                {t.machines.discoveredMachines}
                {machines.length > 0 && (
                  <span className="ml-2 text-sm font-normal text-muted-foreground">
                    ({machines.length})
                  </span>
                )}
              </CardTitle>
              <CardDescription>
                Click a machine to configure credentials or create a backup job.
              </CardDescription>
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => qc.invalidateQueries({ queryKey: ['machines'] })}
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {listLoading ? (
            <div className="flex items-center justify-center py-8 text-muted-foreground">
              <Loader2 className="mr-2 h-5 w-5 animate-spin" />
              {t.common.loading}
            </div>
          ) : machines.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-muted-foreground gap-2">
              <Monitor className="h-10 w-10 opacity-30" />
              <p className="text-sm">{t.machines.noMachines}</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t.machines.hostname}</TableHead>
                  <TableHead>{t.machines.ipAddress}</TableHead>
                  <TableHead>{t.machines.type}</TableHead>
                  <TableHead>{t.machines.ports}</TableHead>
                  <TableHead>{t.machines.lastSeen}</TableHead>
                  <TableHead className="text-right">{t.machines.actions}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {machines.map((m) => (
                  <TableRow key={m.ip}>
                    <TableCell className="font-medium">
                      <div className="flex items-center gap-2">
                        <Monitor className="h-4 w-4 text-muted-foreground shrink-0" />
                        <span>{m.hostname || '—'}</span>
                      </div>
                    </TableCell>
                    <TableCell className="font-mono text-sm">{m.ip}</TableCell>
                    <TableCell>
                      <MachineStatusBadge machine={m} />
                    </TableCell>
                    <TableCell>
                      <div className="flex gap-1 flex-wrap">
                        {m.open_ports.map((p) => (
                          <span
                            key={p}
                            className="text-xs bg-muted rounded px-1 py-0.5 font-mono"
                          >
                            {p}
                          </span>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {m.last_seen ? formatDate(new Date(m.last_seen * 1000).toISOString()) : '—'}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center justify-end gap-1">
                        {/* Ping */}
                        <Button
                          variant="ghost"
                          size="sm"
                          title={t.machines.reprobe}
                          onClick={() => pingMutation.mutate(m.ip)}
                          disabled={pingMutation.isPending}
                        >
                          <Wifi className="h-4 w-4" />
                        </Button>

                        {/* SMB Credentials */}
                        {m.smb_available && (
                          <Button
                            variant="ghost"
                            size="sm"
                            title={t.machines.setSmbCredentials}
                            onClick={() => setCredsMachine(m)}
                          >
                            <Key className="h-4 w-4" />
                          </Button>
                        )}

                        {/* Agent API key */}
                        {m.agent_detected && (
                          <Button
                            variant="ghost"
                            size="sm"
                            title={t.machines.setAgentKey}
                            onClick={() => setAgentKeyMachine(m)}
                          >
                            <ShieldCheck className="h-4 w-4" />
                          </Button>
                        )}

                        {/* Create job */}
                        {(m.smb_available || m.agent_detected) && (
                          <Button
                            variant="ghost"
                            size="sm"
                            title={t.machines.createBackupJob}
                            onClick={() => setJobMachine(m)}
                          >
                            <Plus className="h-4 w-4" />
                          </Button>
                        )}

                        {/* Delete */}
                        <Button
                          variant="ghost"
                          size="sm"
                          title={t.machines.forgetMachine}
                          onClick={() => deleteMutation.mutate(m.ip)}
                          className="text-destructive hover:text-destructive"
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
        </CardContent>
      </Card>

      {/* Agent Download + Installation Guide */}
      <AgentDownloadCard />

      {/* Dialogs */}
      <CredentialsDialog
        machine={credsMachine}
        open={credsMachine !== null}
        onClose={() => setCredsMachine(null)}
      />
      <AgentKeyDialog
        machine={agentKeyMachine}
        open={agentKeyMachine !== null}
        onClose={() => setAgentKeyMachine(null)}
      />
      <CreateJobDialog
        machine={jobMachine}
        open={jobMachine !== null}
        onClose={() => setJobMachine(null)}
      />
    </div>
  )
}
