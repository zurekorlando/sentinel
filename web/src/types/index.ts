// ─── API types (mirror api/schemas.py) ────────────────────────────────────────

export interface StorageConfigResponse {
  provider: 'local' | 's3' | 'smb' | 'nfs'
  base_path?: string
  bucket?: string
  endpoint_url?: string
  smb_server?: string
  nfs_mount?: string
}

export interface RetentionConfigResponse {
  daily: number
  weekly: number
  monthly: number
}

export interface ScheduleConfig {
  enabled: boolean
  cron?: string
  timezone: string
  next_run_at?: string
}

export interface JobConfigResponse {
  job_id: string
  source_paths: string[]
  catalog_path: string
  exclusions: string[]
  compression_level: number
  max_workers: number
  webhook_url?: string
  storage: StorageConfigResponse
  retention: RetentionConfigResponse
  schedule?: ScheduleConfig
}

export interface BackupRunRequest {
  passphrase: string
  snapshot_provider?: string
}

export interface BackupRunResponse {
  run_id: string
  job_id: string
  status: 'queued' | 'running' | 'success' | 'failed'
  started_at: string
  finished_at?: string
  files_processed: number
  files_skipped_cbt: number
  chunks_uploaded: number
  chunks_deduped: number
  bytes_original: number
  bytes_stored: number
  provider_used: string
  duration_s: number
  errors: string[]
}

export interface RunHistoryResponse {
  runs: BackupRunResponse[]
  total: number
}

export interface SnapshotResponse {
  snapshot_id: string
  job_id: string
  status: string
  total_bytes?: number
  chunk_count?: number
  started_at: string
  finished_at?: string
}

export interface FileResponse {
  file_id: string
  path: string
  size: number
  mtime?: number
}

export interface SnapshotDetail extends SnapshotResponse {
  files: FileResponse[]
}

export interface StorageStatsResponse {
  job_id: string
  total_chunks: number
  total_original_bytes: number
  total_compressed_bytes: number
  dedup_ratio: number
  snapshot_count: number
  storage_provider: string
}

export interface HealthResponse {
  status: 'ok' | 'degraded'
  storage_healthy: boolean
  catalog_accessible: boolean
}

// ─── Scheduler types ──────────────────────────────────────────────────────────

export interface ScheduleResponse {
  job_id: string
  enabled: boolean
  cron?: string
  timezone: string
  next_run_at?: string
  last_run_at?: string
  last_run_status?: string
}

export interface ScheduleConfigRequest {
  enabled?: boolean
  cron?: string
  timezone?: string
}

// ─── Restore types ────────────────────────────────────────────────────────────

export interface RestoreRequest {
  job_id: string
  passphrase: string
  destination_path: string
  file_paths?: string[]
  overwrite: boolean
}

export interface RestoreResponse {
  restore_id: string
  snapshot_id: string
  status: string
  files_total: number
  files_restored: number
  bytes_restored: number
  started_at: string
  finished_at?: string
  errors: string[]
}

// ─── Exclusion template types ─────────────────────────────────────────────────

export interface ExclusionTemplate {
  id: string
  name: string
  description: string
  patterns: string[]
  builtin: boolean
}

// ─── System info types ────────────────────────────────────────────────────────

export interface SystemInfoResponse {
  hostname: string
  os: string
  cpu_count: number
  total_disk_bytes: number
  free_disk_bytes: number
  sentinel_version: string
  catalog_size_bytes: number
  uptime_seconds: number
}

export interface ActivityEvent {
  timestamp: string
  job_id: string
  event: string
  detail: string
  level: 'info' | 'warning' | 'error'
}

// ─── Job CRUD request types ───────────────────────────────────────────────────

export interface StorageConfigRequest {
  provider: 'local' | 's3' | 'smb' | 'nfs'
  base_path?: string
  bucket?: string
  prefix?: string
  endpoint_url?: string
  aws_access_key_id?: string
  aws_secret_access_key?: string
  region?: string
  smb_server?: string
  smb_username?: string
  smb_password?: string
  nfs_mount?: string
}

export interface JobCreateRequest {
  job_id: string
  source_paths: string[]
  catalog_path?: string
  exclusions?: string[]
  compression_level?: number
  max_workers?: number
  webhook_url?: string
  storage: StorageConfigRequest
  retention?: RetentionConfigResponse
  schedule?: ScheduleConfigRequest
}

export interface JobUpdateRequest {
  source_paths?: string[]
  exclusions?: string[]
  compression_level?: number
  max_workers?: number
  webhook_url?: string
  storage?: StorageConfigRequest
  retention?: RetentionConfigResponse
  schedule?: ScheduleConfigRequest
}

// ─── WebSocket event types ─────────────────────────────────────────────────────

export type WsEventName =
  | 'JOB_START'
  | 'FILE_DONE'
  | 'JOB_SUCCESS'
  | 'JOB_FAILED'

export interface WsEvent {
  event: WsEventName
  run_id: string
  job_id: string
  status?: string
  files_processed?: number
  files_skipped_cbt?: number
  chunks_uploaded?: number
  chunks_deduped?: number
  bytes_original?: number
  bytes_stored?: number
  duration_s?: number
  file?: string
  error?: string
}
