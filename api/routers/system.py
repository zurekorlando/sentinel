"""
System router — system information and activity feed.

Endpoints
---------
GET /api/system/info      Host, OS, CPU, disk, uptime, catalog sizes.
GET /api/system/activity  Last N run events across all jobs (activity feed).
"""

from __future__ import annotations

import json
import os
import platform
import socket
import time
from pathlib import Path
from typing import List

import psutil
from fastapi import APIRouter, HTTPException

from api.deps import jobs_dir, list_job_configs, get_catalog
from api.schemas import ActivityEvent, SystemInfoResponse

router = APIRouter(prefix="/api/system", tags=["system"])

# Application start time (monotonic)
_start_time = time.monotonic()

# Sentinel version (from main app version string)
_SENTINEL_VERSION = "0.6.0"


@router.get("/info", response_model=SystemInfoResponse)
async def system_info():
    """Return host system information and aggregate catalog size."""
    try:
        disk = psutil.disk_usage("/")
        total_disk = disk.total
        free_disk = disk.free
    except Exception:
        total_disk = 0
        free_disk = 0

    cpu_count = psutil.cpu_count(logical=True) or 0
    uptime_s = time.monotonic() - _start_time

    # Sum catalog sizes across all jobs
    catalog_size = 0
    try:
        for cfg in list_job_configs():
            cp = Path(cfg.catalog_path)
            if cp.exists():
                catalog_size += cp.stat().st_size
    except Exception:
        pass

    return SystemInfoResponse(
        hostname=socket.gethostname(),
        os=f"{platform.system()} {platform.release()}",
        cpu_count=cpu_count,
        total_disk_bytes=total_disk,
        free_disk_bytes=free_disk,
        sentinel_version=_SENTINEL_VERSION,
        catalog_size_bytes=catalog_size,
        uptime_seconds=uptime_s,
    )


@router.get("/activity", response_model=List[ActivityEvent])
async def activity_feed(limit: int = 50):
    """
    Return the most recent run events across all jobs.

    Each run generates up to two events:
    - ``JOB_START`` when the run is first persisted (status = running).
    - ``JOB_SUCCESS`` or ``JOB_FAILED`` when the run completes.

    Events are sorted by ``started_at`` descending.
    """
    events: List[ActivityEvent] = []

    for cfg in list_job_configs():
        try:
            catalog = get_catalog(cfg.job_id)
            rows = catalog.list_runs(cfg.job_id, limit=limit)
        except Exception:
            continue

        for row in rows:
            errors_raw = row["errors"] or "[]"
            try:
                errors = json.loads(errors_raw)
            except Exception:
                errors = []

            # JOB_START event
            events.append(ActivityEvent(
                timestamp=row["started_at"],
                job_id=row["job_id"],
                event="JOB_START",
                detail=f"Backup started",
                level="info",
            ))

            # JOB_SUCCESS / JOB_FAILED event (if finished)
            if row["finished_at"]:
                if row["status"] == "success":
                    events.append(ActivityEvent(
                        timestamp=row["finished_at"],
                        job_id=row["job_id"],
                        event="JOB_SUCCESS",
                        detail=(
                            f"Completed in {row['duration_s']:.1f}s — "
                            f"{row['files_processed']} files, "
                            f"{row['chunks_uploaded']} chunks uploaded"
                        ),
                        level="info",
                    ))
                else:
                    first_error = errors[0] if errors else "unknown error"
                    events.append(ActivityEvent(
                        timestamp=row["finished_at"],
                        job_id=row["job_id"],
                        event="JOB_FAILED",
                        detail=first_error[:120],
                        level="error",
                    ))

    # Sort newest first, truncate to limit
    events.sort(key=lambda e: e.timestamp, reverse=True)
    return events[:limit]
