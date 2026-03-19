"""
Jobs router — CRUD for job configs, trigger / query backup runs.

Endpoints
---------
GET    /api/jobs                      List all configured jobs.
POST   /api/jobs                      Create a new job config.
GET    /api/jobs/{job_id}             Get one job's configuration.
PUT    /api/jobs/{job_id}             Update a job config.
DELETE /api/jobs/{job_id}             Delete a job config file.
POST   /api/jobs/{job_id}/run         Trigger a backup (async, HTTP 202).
GET    /api/jobs/{job_id}/runs/latest Return the most recent run state.
GET    /api/jobs/{job_id}/runs        Return full run history from catalog.

The backup job is offloaded to a ThreadPoolExecutor via ``run_in_executor``
so the asyncio event loop is never blocked.  Connect to the WebSocket
``WS /ws/jobs/{job_id}`` before calling the run endpoint to receive
real-time FILE_DONE / JOB_SUCCESS / JOB_FAILED events.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from sentinel.config import (
    JobConfig,
    RetentionConfig,
    ScheduleConfig,
    StorageConfig,
    load_job,
    save_job,
)
from sentinel.engine import BackupEngine

from api.deps import (
    RunState,
    get_catalog,
    get_job_config,
    get_run_state,
    get_storage,
    jobs_dir,
    list_job_configs,
    persist_run_state,
    set_run_state,
)
from api.schemas import (
    BackupRunRequest,
    BackupRunResponse,
    JobConfigResponse,
    JobCreateRequest,
    JobUpdateRequest,
    RetentionConfigResponse,
    RunHistoryResponse,
    ScheduleConfigResponse,
    StorageConfigResponse,
)
from api.ws.manager import ws_manager

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cfg_to_response(cfg: JobConfig) -> JobConfigResponse:
    schedule_resp = None
    if cfg.schedule:
        schedule_resp = ScheduleConfigResponse(
            enabled=cfg.schedule.enabled,
            cron=cfg.schedule.cron,
            timezone=cfg.schedule.timezone,
            next_run_at=cfg.schedule.next_run_at,
        )
    return JobConfigResponse(
        job_id=cfg.job_id,
        source_paths=cfg.source_paths,
        catalog_path=cfg.catalog_path,
        exclusions=cfg.exclusions,
        compression_level=cfg.compression_level,
        max_workers=cfg.max_workers,
        webhook_url=cfg.webhook_url,
        storage=StorageConfigResponse(
            provider=cfg.storage.provider,
            base_path=cfg.storage.base_path,
            bucket=cfg.storage.bucket,
            endpoint_url=cfg.storage.endpoint_url,
            smb_server=cfg.storage.smb_server,
            nfs_mount=cfg.storage.nfs_mount,
        ),
        retention=RetentionConfigResponse(
            daily=cfg.retention.daily,
            weekly=cfg.retention.weekly,
            monthly=cfg.retention.monthly,
        ),
        schedule=schedule_resp,
    )


def _state_to_response(state: RunState) -> BackupRunResponse:
    return BackupRunResponse(
        run_id=state.run_id,
        job_id=state.job_id,
        status=state.status,
        started_at=state.started_at,
        finished_at=state.finished_at,
        files_processed=state.files_processed,
        files_skipped_cbt=state.files_skipped_cbt,
        chunks_uploaded=state.chunks_uploaded,
        chunks_deduped=state.chunks_deduped,
        bytes_original=state.bytes_original,
        bytes_stored=state.bytes_stored,
        provider_used=state.provider_used,
        duration_s=state.duration_s,
        errors=state.errors,
    )


def _row_to_run_response(row) -> BackupRunResponse:
    """Convert a catalog ``runs`` sqlite3.Row to BackupRunResponse."""
    errors_raw = row["errors"] or "[]"
    try:
        errors = json.loads(errors_raw)
    except Exception:
        errors = []
    return BackupRunResponse(
        run_id=row["run_id"],
        job_id=row["job_id"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        files_processed=row["files_processed"] or 0,
        files_skipped_cbt=row["files_skipped_cbt"] or 0,
        chunks_uploaded=row["chunks_uploaded"] or 0,
        chunks_deduped=row["chunks_deduped"] or 0,
        bytes_original=row["bytes_original"] or 0,
        bytes_stored=row["bytes_stored"] or 0,
        provider_used=row["provider_used"] or "unknown",
        duration_s=row["duration_s"] or 0.0,
        errors=errors,
    )


# ── List / Get ────────────────────────────────────────────────────────────────

@router.get("", response_model=List[JobConfigResponse])
async def list_jobs():
    """Return all configured backup jobs found in SENTINEL_JOBS_DIR."""
    return [_cfg_to_response(cfg) for cfg in list_job_configs()]


@router.get("/{job_id}", response_model=JobConfigResponse)
async def get_job(job_id: str):
    """Return the configuration for a single job."""
    return _cfg_to_response(get_job_config(job_id))


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", response_model=JobConfigResponse, status_code=201)
async def create_job(body: JobCreateRequest):
    """
    Create a new job config JSON file in SENTINEL_JOBS_DIR.

    Returns HTTP 409 if a job with the same ``job_id`` already exists.
    """
    jdir = jobs_dir()
    jdir.mkdir(parents=True, exist_ok=True)
    path = jdir / f"{body.job_id}.json"

    if path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Job '{body.job_id}' already exists",
        )

    storage = StorageConfig(
        provider=body.storage.provider,
        base_path=body.storage.base_path,
        bucket=body.storage.bucket,
        prefix=body.storage.prefix,
        endpoint_url=body.storage.endpoint_url,
        aws_access_key_id=body.storage.aws_access_key_id,
        aws_secret_access_key=body.storage.aws_secret_access_key,
        region=body.storage.region,
        smb_server=body.storage.smb_server,
        smb_username=body.storage.smb_username,
        smb_password=body.storage.smb_password,
        nfs_mount=body.storage.nfs_mount,
    )

    retention = RetentionConfig(
        daily=body.retention.daily,
        weekly=body.retention.weekly,
        monthly=body.retention.monthly,
    )

    schedule = None
    if body.schedule:
        schedule = ScheduleConfig(
            enabled=body.schedule.enabled,
            cron=body.schedule.cron,
            timezone=body.schedule.timezone,
        )

    cfg = JobConfig(
        job_id=body.job_id,
        source_paths=body.source_paths,
        catalog_path=body.catalog_path,
        exclusions=body.exclusions,
        compression_level=body.compression_level,
        max_workers=body.max_workers,
        webhook_url=body.webhook_url,
        storage=storage,
        retention=retention,
        schedule=schedule,
    )

    save_job(cfg, path)

    if schedule and schedule.enabled:
        try:
            from api.scheduler import register_job
            register_job(cfg)
        except Exception:
            pass

    return _cfg_to_response(cfg)


# ── Update ────────────────────────────────────────────────────────────────────

@router.put("/{job_id}", response_model=JobConfigResponse)
async def update_job(job_id: str, body: JobUpdateRequest):
    """
    Patch an existing job config.  Only provided fields are updated.

    Returns HTTP 404 if the job doesn't exist.
    """
    cfg = get_job_config(job_id)

    if body.source_paths is not None:
        cfg.source_paths = body.source_paths
    if body.exclusions is not None:
        cfg.exclusions = body.exclusions
    if body.compression_level is not None:
        cfg.compression_level = body.compression_level
    if body.max_workers is not None:
        cfg.max_workers = body.max_workers
    if body.webhook_url is not None:
        cfg.webhook_url = body.webhook_url

    if body.storage is not None:
        cfg.storage = StorageConfig(
            provider=body.storage.provider,
            base_path=body.storage.base_path,
            bucket=body.storage.bucket,
            prefix=body.storage.prefix,
            endpoint_url=body.storage.endpoint_url,
            aws_access_key_id=body.storage.aws_access_key_id,
            aws_secret_access_key=body.storage.aws_secret_access_key,
            region=body.storage.region,
            smb_server=body.storage.smb_server,
            smb_username=body.storage.smb_username,
            smb_password=body.storage.smb_password,
            nfs_mount=body.storage.nfs_mount,
        )

    if body.retention is not None:
        cfg.retention = RetentionConfig(
            daily=body.retention.daily,
            weekly=body.retention.weekly,
            monthly=body.retention.monthly,
        )

    if body.schedule is not None:
        cfg.schedule = ScheduleConfig(
            enabled=body.schedule.enabled,
            cron=body.schedule.cron,
            timezone=body.schedule.timezone,
        )

    path = jobs_dir() / f"{job_id}.json"
    save_job(cfg, path)

    try:
        from api.scheduler import register_job, unregister_job
        if cfg.schedule and cfg.schedule.enabled:
            register_job(cfg)
        else:
            unregister_job(job_id)
    except Exception:
        pass

    return _cfg_to_response(cfg)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{job_id}", status_code=204)
async def delete_job(job_id: str):
    """
    Delete the job config JSON file.

    Does NOT delete snapshots, catalog, or stored chunks.
    Returns HTTP 404 if the job doesn't exist.
    """
    path = jobs_dir() / f"{job_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    path.unlink()

    try:
        from api.scheduler import unregister_job
        unregister_job(job_id)
    except Exception:
        pass


# ── Run history ───────────────────────────────────────────────────────────────

@router.get("/{job_id}/runs", response_model=RunHistoryResponse)
async def list_runs(
    job_id: str,
    limit: int = Query(100, ge=1, le=1000),
):
    """Return historical run records for *job_id* from the catalog DB."""
    get_job_config(job_id)  # 404 if job doesn't exist
    try:
        catalog = get_catalog(job_id)
        rows = catalog.list_runs(job_id, limit=limit)
        run_list = [_row_to_run_response(r) for r in rows]
        return RunHistoryResponse(runs=run_list, total=len(run_list))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{job_id}/runs/latest", response_model=BackupRunResponse)
async def get_latest_run(job_id: str):
    """Return the most recent run state for *job_id* (in-memory first, then catalog)."""
    get_job_config(job_id)  # raise 404 if job doesn't exist
    state = get_run_state(job_id)
    if state is not None:
        return _state_to_response(state)

    # Fall back to catalog
    try:
        catalog = get_catalog(job_id)
        rows = catalog.list_runs(job_id, limit=1)
        if rows:
            return _row_to_run_response(rows[0])
    except Exception:
        pass

    raise HTTPException(
        status_code=404, detail=f"No runs recorded for job '{job_id}'"
    )


# ── Trigger backup ────────────────────────────────────────────────────────────

@router.post("/{job_id}/run", response_model=BackupRunResponse, status_code=202)
async def run_job(job_id: str, body: BackupRunRequest, bg: BackgroundTasks):
    """
    Trigger a backup job asynchronously.

    Returns HTTP 202 Accepted immediately with a *run_id*.
    Poll ``GET /api/jobs/{job_id}/runs/latest`` or connect to
    ``WS /ws/jobs/{job_id}`` for real-time progress.
    """
    cfg = get_job_config(job_id)

    existing = get_run_state(job_id)
    if existing and existing.status in ("queued", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' already running (run_id={existing.run_id})",
        )

    run_id = str(uuid.uuid4())
    state = RunState(run_id=run_id, job_id=job_id, status="queued")
    set_run_state(job_id, state)

    bg.add_task(
        _execute_backup,
        job_id,
        run_id,
        cfg,
        body.passphrase,
        body.snapshot_provider,
    )
    return _state_to_response(state)


# ── Background task ───────────────────────────────────────────────────────────

async def _execute_backup(
    job_id: str,
    run_id: str,
    cfg,
    passphrase: str,
    snapshot_provider,
) -> None:
    """
    Run BackupEngine in a ThreadPoolExecutor and stream progress via WebSocket.
    """
    state = get_run_state(job_id)
    state.status = "running"

    # Persist "running" state to catalog immediately
    persist_run_state(job_id, state)

    loop = asyncio.get_running_loop()
    ws_manager.set_loop(loop)

    await ws_manager.broadcast(job_id, {
        "event": "JOB_START",
        "run_id": run_id,
        "job_id": job_id,
        "status": "running",
    })

    def _on_progress(event: dict) -> None:
        state.files_processed = event.get("files_processed", state.files_processed)
        state.files_skipped_cbt = event.get("files_skipped_cbt", state.files_skipped_cbt)
        state.chunks_uploaded = event.get("chunks_uploaded", state.chunks_uploaded)
        state.chunks_deduped = event.get("chunks_deduped", state.chunks_deduped)
        state.bytes_original = event.get("bytes_original", state.bytes_original)
        ws_manager.broadcast_sync(job_id, {**event, "run_id": run_id, "job_id": job_id})

    def _run_sync():
        catalog = get_catalog(job_id)
        storage = get_storage(job_id)
        salt = bytes.fromhex(cfg.key_salt_hex) if cfg.key_salt_hex else None

        engine = BackupEngine(
            job_id=job_id,
            catalog=catalog,
            storage=storage,
            passphrase=passphrase,
            salt=salt,
            source_paths=cfg.source_paths,
            exclusions=cfg.exclusions,
            max_workers=cfg.max_workers,
            webhook_url=cfg.webhook_url,
            snapshot_provider=snapshot_provider,
            on_progress=_on_progress,
        )
        result = engine.run_backup()

        if not cfg.key_salt_hex:
            cfg.key_salt_hex = engine.key_salt.hex()
            save_job(cfg, jobs_dir() / f"{job_id}.json")

        return result

    try:
        result = await loop.run_in_executor(None, _run_sync)

        state.status = result.status
        state.files_processed = result.files_processed
        state.files_skipped_cbt = result.files_skipped_cbt
        state.chunks_uploaded = result.chunks_uploaded
        state.chunks_deduped = result.chunks_deduped
        state.bytes_original = result.bytes_original
        state.bytes_stored = result.bytes_stored
        state.provider_used = result.provider_used
        state.duration_s = result.duration_s
        state.errors = result.errors
        state.finished_at = datetime.now(timezone.utc).isoformat()

        # Persist completed run
        persist_run_state(job_id, state)

        await ws_manager.broadcast(job_id, {
            "event": "JOB_SUCCESS" if result.status == "success" else "JOB_FAILED",
            "run_id": run_id,
            "job_id": job_id,
            "status": state.status,
            "files_processed": state.files_processed,
            "files_skipped_cbt": state.files_skipped_cbt,
            "chunks_uploaded": state.chunks_uploaded,
            "chunks_deduped": state.chunks_deduped,
            "bytes_original": state.bytes_original,
            "bytes_stored": state.bytes_stored,
            "duration_s": state.duration_s,
        })

    except Exception as exc:
        state.status = "failed"
        state.errors.append(str(exc))
        state.finished_at = datetime.now(timezone.utc).isoformat()

        persist_run_state(job_id, state)

        await ws_manager.broadcast(job_id, {
            "event": "JOB_FAILED",
            "run_id": run_id,
            "job_id": job_id,
            "status": "failed",
            "error": str(exc),
        })
