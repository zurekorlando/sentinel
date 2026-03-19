"""
Scheduler router — manage cron schedules for backup jobs.

Endpoints
---------
GET    /api/scheduler              List all jobs with schedule info.
PUT    /api/scheduler/{job_id}     Enable/disable or update the cron expression.
DELETE /api/scheduler/{job_id}     Remove the schedule from a job config.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException

from sentinel.config import ScheduleConfig, save_job

from api.deps import get_job_config, jobs_dir, list_job_configs, get_catalog
from api.schemas import ScheduleConfigRequest, ScheduleResponse

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


def _get_last_run(job_id: str):
    """Return (last_run_at, last_run_status) from catalog, or (None, None)."""
    try:
        catalog = get_catalog(job_id)
        rows = catalog.list_runs(job_id, limit=1)
        if rows:
            row = rows[0]
            return row["finished_at"] or row["started_at"], row["status"]
    except Exception:
        pass
    return None, None


def _job_to_schedule_response(cfg) -> ScheduleResponse:
    last_at, last_status = _get_last_run(cfg.job_id)

    # Get next_run_at from APScheduler live if possible
    next_run_at = None
    if cfg.schedule and cfg.schedule.enabled:
        try:
            from api.scheduler import get_next_run_at
            next_run_at = get_next_run_at(cfg.job_id) or (
                cfg.schedule.next_run_at if cfg.schedule else None
            )
        except Exception:
            next_run_at = cfg.schedule.next_run_at if cfg.schedule else None

    return ScheduleResponse(
        job_id=cfg.job_id,
        enabled=cfg.schedule.enabled if cfg.schedule else False,
        cron=cfg.schedule.cron if cfg.schedule else None,
        timezone=cfg.schedule.timezone if cfg.schedule else "UTC",
        next_run_at=next_run_at,
        last_run_at=last_at,
        last_run_status=last_status,
    )


@router.get("", response_model=List[ScheduleResponse])
async def list_schedules():
    """Return all jobs with their schedule configuration."""
    return [_job_to_schedule_response(cfg) for cfg in list_job_configs()]


@router.put("/{job_id}", response_model=ScheduleResponse)
async def update_schedule(job_id: str, body: ScheduleConfigRequest):
    """
    Enable/disable or update the cron expression for a job's schedule.

    Creates the schedule section if it doesn't exist yet.
    """
    cfg = get_job_config(job_id)

    cfg.schedule = ScheduleConfig(
        enabled=body.enabled,
        cron=body.cron,
        timezone=body.timezone,
    )

    path = jobs_dir() / f"{job_id}.json"
    save_job(cfg, path)

    # Re-register with APScheduler
    try:
        from api import scheduler as sched_module
        if body.enabled and body.cron:
            sched_module.register_job(cfg)
        else:
            sched_module.unregister_job(job_id)
    except Exception:
        pass

    return _job_to_schedule_response(cfg)


@router.delete("/{job_id}", status_code=204)
async def remove_schedule(job_id: str):
    """
    Remove the schedule from a job config entirely.

    Returns HTTP 404 if the job doesn't exist.
    """
    cfg = get_job_config(job_id)
    cfg.schedule = None

    path = jobs_dir() / f"{job_id}.json"
    save_job(cfg, path)

    try:
        from api import scheduler as sched_module
        sched_module.unregister_job(job_id)
    except Exception:
        pass
