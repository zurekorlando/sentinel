"""
APScheduler integration for Sentinel.

Provides cron-based scheduled backup execution.  The scheduler is started
once during the FastAPI lifespan and runs until the process exits.

Usage (from api/main.py lifespan):
    from api.scheduler import init_scheduler, shutdown_scheduler
    init_scheduler()   # starts AsyncIOScheduler, registers all enabled jobs
    yield
    shutdown_scheduler()
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _HAS_APSCHEDULER = True
except ImportError:
    _HAS_APSCHEDULER = False
    log.warning("APScheduler not installed — scheduled backups disabled. pip install apscheduler")

_scheduler: Optional["AsyncIOScheduler"] = None


def init_scheduler() -> None:
    """
    Create and start the AsyncIOScheduler.

    Scans SENTINEL_JOBS_DIR for jobs with ``schedule.enabled = true`` and
    registers them as cron jobs.  Call once from the FastAPI lifespan.
    """
    global _scheduler

    if not _HAS_APSCHEDULER:
        return

    _scheduler = AsyncIOScheduler()
    _scheduler.start()
    log.info("APScheduler started")

    # Register all enabled jobs found at startup
    try:
        from api.deps import list_job_configs
        for cfg in list_job_configs():
            if cfg.schedule and cfg.schedule.enabled:
                register_job(cfg)
    except Exception as exc:
        log.warning("Could not register scheduled jobs at startup: %s", exc)


def shutdown_scheduler() -> None:
    """Gracefully shut down the scheduler on process exit."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("APScheduler shut down")
    _scheduler = None


def register_job(cfg) -> None:
    """
    Add (or replace) a cron job for the given ``JobConfig.schedule``.

    Does nothing if the scheduler is not running or APScheduler is not installed.
    """
    global _scheduler
    if not _HAS_APSCHEDULER or _scheduler is None:
        return
    if not cfg.schedule or not cfg.schedule.enabled or not cfg.schedule.cron:
        return

    job_id = cfg.job_id
    cron_expr = cfg.schedule.cron
    timezone = cfg.schedule.timezone or "UTC"

    # Remove existing job with same id to avoid duplicates
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)

    try:
        trigger = CronTrigger.from_crontab(cron_expr, timezone=timezone)
        _scheduler.add_job(
            _run_scheduled_backup,
            trigger=trigger,
            id=job_id,
            args=[job_id],
            replace_existing=True,
            misfire_grace_time=300,  # 5 minutes tolerance
        )
        log.info(
            "Scheduled job '%s' registered: cron='%s' tz=%s",
            job_id, cron_expr, timezone,
        )

        # Update next_run_at in job config
        _update_next_run(cfg)

    except Exception as exc:
        log.error("Failed to register scheduled job '%s': %s", job_id, exc)


def unregister_job(job_id: str) -> None:
    """Remove a job from the scheduler (if registered)."""
    global _scheduler
    if not _HAS_APSCHEDULER or _scheduler is None:
        return
    try:
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)
            log.info("Scheduled job '%s' unregistered", job_id)
    except Exception as exc:
        log.warning("Could not unregister job '%s': %s", job_id, exc)


def get_next_run_at(job_id: str) -> Optional[str]:
    """Return the ISO8601 next_run_at for *job_id*, or None."""
    global _scheduler
    if not _HAS_APSCHEDULER or _scheduler is None:
        return None
    try:
        job = _scheduler.get_job(job_id)
        if job and job.next_run_time:
            return job.next_run_time.isoformat()
    except Exception:
        pass
    return None


def get_scheduler() -> "Optional[AsyncIOScheduler]":
    """Return the running AsyncIOScheduler instance, or None if not started."""
    return _scheduler


def list_scheduled_jobs() -> list:
    """Return a list of APScheduler job objects."""
    global _scheduler
    if not _HAS_APSCHEDULER or _scheduler is None:
        return []
    try:
        return _scheduler.get_jobs()
    except Exception:
        return []


def _update_next_run(cfg) -> None:
    """Persist ``next_run_at`` back to the job config JSON file."""
    try:
        from api.deps import jobs_dir
        from sentinel.config import save_job

        next_run = get_next_run_at(cfg.job_id)
        if next_run and cfg.schedule:
            cfg.schedule.next_run_at = next_run
            path = jobs_dir() / f"{cfg.job_id}.json"
            save_job(cfg, path)
    except Exception as exc:
        log.debug("Could not persist next_run_at for '%s': %s", cfg.job_id, exc)


async def _run_scheduled_backup(job_id: str) -> None:
    """
    Called by APScheduler on each scheduled trigger.

    Uses ``SENTINEL_PASSPHRASE`` env var.  If not set, the run is skipped
    with a warning (scheduled backups require a passphrase env var).
    """
    passphrase = os.getenv("SENTINEL_PASSPHRASE", "")
    if not passphrase:
        log.warning(
            "SENTINEL_PASSPHRASE not set — scheduled job '%s' skipped",
            job_id,
        )
        return

    log.info("APScheduler triggering backup for job '%s'", job_id)

    try:
        from api.deps import get_job_config, get_run_state, set_run_state, RunState, persist_run_state, get_catalog, get_storage
        from api.ws.manager import ws_manager
        import asyncio
        import uuid
        from datetime import datetime, timezone

        cfg = get_job_config(job_id)

        # Skip if already running
        existing = get_run_state(job_id)
        if existing and existing.status in ("queued", "running"):
            log.warning(
                "Scheduled job '%s' skipped — already running (run_id=%s)",
                job_id, existing.run_id,
            )
            return

        run_id = str(uuid.uuid4())
        state = RunState(run_id=run_id, job_id=job_id, status="queued")
        set_run_state(job_id, state)
        state.status = "running"
        persist_run_state(job_id, state)

        from sentinel.engine import BackupEngine
        from sentinel.config import save_job
        from api.deps import jobs_dir

        loop = asyncio.get_running_loop()
        ws_manager.set_loop(loop)

        def _on_progress(event: dict) -> None:
            state.files_processed = event.get("files_processed", state.files_processed)
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
                on_progress=_on_progress,
            )
            result = engine.run_backup()

            if not cfg.key_salt_hex:
                cfg.key_salt_hex = engine.key_salt.hex()
                save_job(cfg, jobs_dir() / f"{job_id}.json")

            return result

        result = await loop.run_in_executor(None, _run_sync)

        state.status = result.status
        state.files_processed = result.files_processed
        state.chunks_uploaded = result.chunks_uploaded
        state.chunks_deduped = result.chunks_deduped
        state.bytes_original = result.bytes_original
        state.bytes_stored = result.bytes_stored
        state.provider_used = result.provider_used
        state.duration_s = result.duration_s
        state.errors = result.errors
        state.finished_at = datetime.now(timezone.utc).isoformat()
        persist_run_state(job_id, state)

        # Update next_run_at after a successful run
        _update_next_run(cfg)

        log.info(
            "Scheduled backup '%s' completed: status=%s duration=%.1fs",
            job_id, result.status, result.duration_s,
        )

    except Exception as exc:
        log.exception("Scheduled backup '%s' failed with exception: %s", job_id, exc)
