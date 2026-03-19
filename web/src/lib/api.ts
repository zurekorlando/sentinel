/**
 * Typed API client for the Sentinel REST API.
 * All endpoints are relative; Vite proxies /api → http://localhost:8000.
 */

import type {
  ActivityEvent,
  BackupRunRequest,
  BackupRunResponse,
  ExclusionTemplate,
  HealthResponse,
  JobConfigResponse,
  JobCreateRequest,
  JobUpdateRequest,
  RestoreRequest,
  RestoreResponse,
  RunHistoryResponse,
  ScheduleConfigRequest,
  ScheduleResponse,
  SnapshotDetail,
  SnapshotResponse,
  StorageStatsResponse,
  SystemInfoResponse,
} from '../types'

// ── Generic fetch helper ───────────────────────────────────────────────────────

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  })
  if (res.status === 204) return undefined as T
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json()
}

// ── Jobs ──────────────────────────────────────────────────────────────────────

export const jobs = {
  list: (): Promise<JobConfigResponse[]> =>
    request('/api/jobs'),

  get: (jobId: string): Promise<JobConfigResponse> =>
    request(`/api/jobs/${jobId}`),

  create: (body: JobCreateRequest): Promise<JobConfigResponse> =>
    request('/api/jobs', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  update: (jobId: string, body: JobUpdateRequest): Promise<JobConfigResponse> =>
    request(`/api/jobs/${jobId}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  delete: (jobId: string): Promise<void> =>
    request(`/api/jobs/${jobId}`, { method: 'DELETE' }),

  run: (jobId: string, body: BackupRunRequest): Promise<BackupRunResponse> =>
    request(`/api/jobs/${jobId}/run`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  latestRun: (jobId: string): Promise<BackupRunResponse> =>
    request(`/api/jobs/${jobId}/runs/latest`),

  listRuns: (jobId: string, limit = 100): Promise<RunHistoryResponse> =>
    request(`/api/jobs/${jobId}/runs?limit=${limit}`),
}

// ── Snapshots ─────────────────────────────────────────────────────────────────

export const snapshots = {
  list: (jobId?: string): Promise<SnapshotResponse[]> =>
    request(`/api/snapshots${jobId ? `?job_id=${jobId}` : ''}`),

  get: (snapshotId: string, jobId: string): Promise<SnapshotDetail> =>
    request(`/api/snapshots/${snapshotId}?job_id=${jobId}`),

  delete: (snapshotId: string, jobId: string): Promise<void> =>
    request(`/api/snapshots/${snapshotId}?job_id=${jobId}`, { method: 'DELETE' }),
}

// ── Storage ───────────────────────────────────────────────────────────────────

export const storage = {
  stats: (jobId: string): Promise<StorageStatsResponse> =>
    request(`/api/storage/${jobId}/stats`),

  health: (jobId: string): Promise<HealthResponse> =>
    request(`/api/storage/${jobId}/health`),
}

// ── Scheduler ─────────────────────────────────────────────────────────────────

export const scheduler = {
  list: (): Promise<ScheduleResponse[]> =>
    request('/api/scheduler'),

  update: (jobId: string, body: ScheduleConfigRequest): Promise<ScheduleResponse> =>
    request(`/api/scheduler/${jobId}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  remove: (jobId: string): Promise<void> =>
    request(`/api/scheduler/${jobId}`, { method: 'DELETE' }),
}

// ── Restore ───────────────────────────────────────────────────────────────────

export const restore = {
  start: (snapshotId: string, body: RestoreRequest): Promise<RestoreResponse> =>
    request(`/api/snapshots/${snapshotId}/restore`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  status: (restoreId: string): Promise<RestoreResponse> =>
    request(`/api/restore/${restoreId}/status`),
}

// ── Exclusion templates ───────────────────────────────────────────────────────

export const exclusions = {
  list: (): Promise<ExclusionTemplate[]> =>
    request('/api/exclusion-templates'),

  create: (body: Omit<ExclusionTemplate, 'builtin'>): Promise<ExclusionTemplate> =>
    request('/api/exclusion-templates', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  delete: (templateId: string): Promise<void> =>
    request(`/api/exclusion-templates/${templateId}`, { method: 'DELETE' }),
}

// ── Machines (LAN discovery) ──────────────────────────────────────────────────

export interface MachineRecord {
  ip: string
  hostname: string | null
  open_ports: number[]
  windows_likely: boolean
  smb_available: boolean
  agent_detected: boolean
  agent_version: string | null
  last_seen: number
  response_ms: number | null
}

export interface ScanStatus {
  running: boolean
  network: string | null
  started_at: number | null
  finished_at: number | null
  hosts_found: number
  error: string | null
}

export interface AgentPackageInfo {
  version: string
  filename: string
  size_bytes: number
  built_at: number
  download_url: string
}

export const machines = {
  list: (): Promise<{ machines: MachineRecord[]; total: number }> =>
    request('/api/machines'),

  agentInfo: (): Promise<AgentPackageInfo> =>
    request('/api/machines/agent/info'),

  scan: (network?: string, timeout?: number): Promise<{ status: string; network: string }> =>
    request('/api/machines/scan', {
      method: 'POST',
      body: JSON.stringify({ network: network ?? null, timeout: timeout ?? 0.8 }),
    }),

  scanStatus: (): Promise<ScanStatus> =>
    request('/api/machines/scan/status'),

  get: (ip: string): Promise<MachineRecord> =>
    request(`/api/machines/${ip}`),

  setCredentials: (ip: string, body: { username: string; password: string; domain?: string }): Promise<{ ok: boolean }> =>
    request(`/api/machines/${ip}/credentials`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  ping: (ip: string): Promise<MachineRecord & { reachable: boolean }> =>
    request(`/api/machines/${ip}/ping`, { method: 'POST' }),

  delete: (ip: string): Promise<void> =>
    request(`/api/machines/${ip}`, { method: 'DELETE' }),

  createJob: (ip: string, body: {
    job_id: string
    share?: string
    source_type?: string
    agent_paths?: string[]
    storage_base_path?: string
    storage_provider?: string
  }): Promise<{ job_id: string; ip: string; source_type: string }> =>
    request(`/api/machines/${ip}/create-job`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  browse: (ip: string, path?: string): Promise<{ path: string; entries: BrowseEntry[] }> =>
    request(`/api/machines/${ip}/browse${path ? `?path=${encodeURIComponent(path)}` : ''}`),

  setAgentKey: (ip: string, agentKey: string): Promise<{ ok: boolean }> =>
    request(`/api/machines/${ip}/agent-key`, {
      method: 'PUT',
      body: JSON.stringify({ agent_key: agentKey }),
    }),
}

export interface BrowseEntry {
  name: string
  path: string
  is_dir: boolean
  size: number | null
  mtime: number
}

// ── Agent types ───────────────────────────────────────────────────────────────

export interface AgentRecord {
  agent_id: string
  display_name: string
  hostname: string
  os: string
  os_version: string
  ip_last_seen: string
  port: number
  status: 'online' | 'offline' | 'never_connected'
  last_seen: string | null
  version: string
  description: string
  created_at: string
  updated_at: string
}

export interface CreateAgentResponse {
  agent: AgentRecord
  api_key: string
}

export interface AgentBrowseEntry {
  name: string
  path: string
  is_dir: boolean
  size: number | null
  mtime: number
}

export interface AgentBrowseResponse {
  path: string
  platform: string
  drives: string[] | null
  entries: AgentBrowseEntry[]
}

// ── Agents API ────────────────────────────────────────────────────────────────

export const agents = {
  list: (status?: string): Promise<{ agents: AgentRecord[]; total: number }> => {
    const params = status ? `?status=${status}` : ''
    return request(`/api/agents${params}`)
  },
  get: (id: string): Promise<AgentRecord> =>
    request(`/api/agents/${id}`),
  create: (body: { display_name: string; description?: string }): Promise<CreateAgentResponse> =>
    request('/api/agents', { method: 'POST', body: JSON.stringify(body) }),
  update: (id: string, body: { display_name?: string; description?: string; port?: number }): Promise<AgentRecord> =>
    request(`/api/agents/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  delete: (id: string): Promise<void> =>
    request(`/api/agents/${id}`, { method: 'DELETE' }),
  ping: (id: string): Promise<{ reachable: boolean; latency_ms: number; agent: AgentRecord }> =>
    request(`/api/agents/${id}/ping`, { method: 'POST' }),
  browse: (id: string, path?: string): Promise<AgentBrowseResponse> => {
    const params = path ? `?path=${encodeURIComponent(path)}` : ''
    return request(`/api/agents/${id}/browse${params}`)
  },
}

// ── Scrub ─────────────────────────────────────────────────────────────────────

export interface ScrubStateResponse {
  scrub_id: string
  job_id: string
  status: 'queued' | 'running' | 'success' | 'failed'
  started_at: string
  finished_at: string | null
  chunks_checked: number
  chunks_ok: number
  chunks_corrupt: number
  corrupt_chunk_ids: string[]
  affected_snapshot_ids: string[]
  duration_s: number
  errors: string[]
  mode: 'sample' | 'full'
  sample_fraction: number
}

export interface CorruptChunk {
  hash_id: string
  storage_key: string
  original_size: number
  corrupted_at: string
}

export const scrub = {
  start: (
    jobId: string,
    body: { sample?: number; full?: boolean },
  ): Promise<{ scrub_id: string; job_id: string; status: string; mode: string }> =>
    request(`/api/jobs/${jobId}/scrub`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  status: (jobId: string): Promise<ScrubStateResponse> =>
    request(`/api/jobs/${jobId}/scrub/status`),

  corrupt: (jobId: string): Promise<{
    job_id: string
    corrupt_chunks: CorruptChunk[]
    total: number
    affected_snapshot_ids: string[]
  }> => request(`/api/jobs/${jobId}/scrub/corrupt`),
}

// ── System ────────────────────────────────────────────────────────────────────

export const system = {
  healthz: (): Promise<{ status: string; version: string }> =>
    request('/healthz'),

  info: (): Promise<SystemInfoResponse> =>
    request('/api/system/info'),

  activity: (limit = 50): Promise<ActivityEvent[]> =>
    request(`/api/system/activity?limit=${limit}`),
}
