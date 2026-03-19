"""
Pydantic schemas for the Sentinel REST API.

Request / response models are kept separate from internal dataclasses so the
API surface can evolve independently of the engine internals.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# ── Storage / Retention (nested inside JobConfigResponse) ─────────────────────

class StorageConfigResponse(BaseModel):
    provider: str
    base_path: Optional[str] = None
    bucket: Optional[str] = None
    endpoint_url: Optional[str] = None
    # SMB/NFS fields (optional — older jobs won't have them)
    smb_server: Optional[str] = None
    nfs_mount: Optional[str] = None


class RetentionConfigResponse(BaseModel):
    daily: int
    weekly: int
    monthly: int


class ScheduleConfigResponse(BaseModel):
    enabled: bool = False
    cron: Optional[str] = None
    timezone: str = "UTC"
    next_run_at: Optional[str] = None


# ── Job schemas ────────────────────────────────────────────────────────────────

class JobConfigResponse(BaseModel):
    job_id: str
    source_paths: List[str]
    catalog_path: str
    exclusions: List[str]
    compression_level: int
    max_workers: int
    webhook_url: Optional[str] = None
    storage: StorageConfigResponse
    retention: RetentionConfigResponse
    schedule: Optional[ScheduleConfigResponse] = None


class StorageConfigRequest(BaseModel):
    provider: Literal["local", "s3", "smb", "nfs"]
    base_path: Optional[str] = None
    bucket: Optional[str] = None
    prefix: str = "chunks/"
    endpoint_url: Optional[str] = None
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    region: str = "us-east-1"
    smb_server: Optional[str] = None
    smb_username: Optional[str] = None
    smb_password: Optional[str] = None
    nfs_mount: Optional[str] = None


class ScheduleConfigRequest(BaseModel):
    enabled: bool = False
    cron: Optional[str] = None
    timezone: str = "UTC"


class JobCreateRequest(BaseModel):
    job_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$", description="Alphanumeric + hyphens/underscores")
    source_paths: List[str] = Field(..., min_length=1)
    catalog_path: str = "sentinel_catalog.db"
    exclusions: List[str] = Field(default_factory=list)
    compression_level: int = Field(3, ge=1, le=22)
    max_workers: int = Field(4, ge=1, le=32)
    webhook_url: Optional[str] = None
    storage: StorageConfigRequest
    retention: RetentionConfigResponse = Field(
        default_factory=lambda: RetentionConfigResponse(daily=7, weekly=4, monthly=12)
    )
    schedule: Optional[ScheduleConfigRequest] = None


class JobUpdateRequest(BaseModel):
    source_paths: Optional[List[str]] = None
    exclusions: Optional[List[str]] = None
    compression_level: Optional[int] = Field(None, ge=1, le=22)
    max_workers: Optional[int] = Field(None, ge=1, le=32)
    webhook_url: Optional[str] = None
    storage: Optional[StorageConfigRequest] = None
    retention: Optional[RetentionConfigResponse] = None
    schedule: Optional[ScheduleConfigRequest] = None


class BackupRunRequest(BaseModel):
    passphrase: str = Field(
        ...,
        description="Encryption passphrase — sent over HTTPS, never stored",
    )
    snapshot_provider: Optional[str] = Field(
        None,
        description="Force a specific SSM provider: vss | lvm | btrfs | direct",
    )


class BackupRunResponse(BaseModel):
    run_id: str
    job_id: str
    status: str                       # queued | running | success | failed
    started_at: str
    finished_at: Optional[str] = None
    files_processed: int = 0
    files_skipped_cbt: int = 0
    chunks_uploaded: int = 0
    chunks_deduped: int = 0
    bytes_original: int = 0
    bytes_stored: int = 0
    provider_used: str = "unknown"
    duration_s: float = 0.0
    errors: List[str] = Field(default_factory=list)


class RunHistoryResponse(BaseModel):
    runs: List[BackupRunResponse]
    total: int


# ── Snapshot schemas ──────────────────────────────────────────────────────────

class SnapshotResponse(BaseModel):
    snapshot_id: str
    job_id: str
    status: str
    total_bytes: Optional[int] = None
    chunk_count: Optional[int] = None
    started_at: str
    finished_at: Optional[str] = None


class FileResponse(BaseModel):
    file_id: str
    path: str
    size: int
    mtime: Optional[float] = None


class SnapshotDetail(SnapshotResponse):
    files: List[FileResponse]


# ── Storage schemas ───────────────────────────────────────────────────────────

class StorageStatsResponse(BaseModel):
    job_id: str
    total_chunks: int
    total_original_bytes: int
    total_compressed_bytes: int
    dedup_ratio: float          # chunks_deduped / total (savings ratio)
    snapshot_count: int
    storage_provider: str


class HealthResponse(BaseModel):
    status: str                 # ok | degraded
    storage_healthy: bool
    catalog_accessible: bool


# ── Scheduler schemas ─────────────────────────────────────────────────────────

class ScheduleResponse(BaseModel):
    job_id: str
    enabled: bool
    cron: Optional[str] = None
    timezone: str = "UTC"
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_run_status: Optional[str] = None


# ── Restore schemas ───────────────────────────────────────────────────────────

class RestoreRequest(BaseModel):
    job_id: str
    passphrase: str = Field(..., description="Encryption passphrase for decryption")
    destination_path: str = Field(..., description="Directory where files will be restored")
    file_paths: Optional[List[str]] = Field(None, description="Specific paths to restore; None restores all")
    overwrite: bool = False


class RestoreResponse(BaseModel):
    restore_id: str
    snapshot_id: str
    status: str                 # running | success | failed
    files_total: int = 0
    files_restored: int = 0
    bytes_restored: int = 0
    started_at: str
    finished_at: Optional[str] = None
    errors: List[str] = Field(default_factory=list)


# ── Exclusion template schemas ────────────────────────────────────────────────

class ExclusionTemplate(BaseModel):
    id: str
    name: str
    description: str
    patterns: List[str]
    builtin: bool


# ── System info schemas ───────────────────────────────────────────────────────

class SystemInfoResponse(BaseModel):
    hostname: str
    os: str
    cpu_count: int
    total_disk_bytes: int
    free_disk_bytes: int
    sentinel_version: str
    catalog_size_bytes: int
    uptime_seconds: float


class ActivityEvent(BaseModel):
    timestamp: str
    job_id: str
    event: str
    detail: str
    level: str                  # info | warning | error
