"""
Sentinel Backup API — FastAPI application entry point (Sprint 6).

Routes
------
GET  /healthz                        System health check.
GET  /api/jobs                       List all configured jobs.
POST /api/jobs                       Create a new job config.
GET  /api/jobs/{job_id}              Get job configuration.
PUT  /api/jobs/{job_id}              Update a job config.
DELETE /api/jobs/{job_id}            Delete a job config.
POST /api/jobs/{job_id}/run          Trigger a backup (async, 202).
GET  /api/jobs/{job_id}/runs/latest  Latest run status.
GET  /api/jobs/{job_id}/runs         Run history from catalog.
GET    /api/snapshots                List all snapshots.
GET    /api/snapshots/{id}           Snapshot detail + files.
DELETE /api/snapshots/{id}           Delete snapshot + GC.
POST   /api/snapshots/{id}/restore   Start a restore (async, 202).
GET    /api/restore/{id}/status      Poll restore progress.
GET  /api/storage/{job_id}/stats     Storage usage stats.
GET  /api/storage/{job_id}/health    Storage + catalog health.
GET  /api/scheduler                  List scheduled jobs.
PUT  /api/scheduler/{job_id}         Update job schedule.
DELETE /api/scheduler/{job_id}       Remove job schedule.
GET  /api/exclusion-templates        List exclusion templates.
POST /api/exclusion-templates        Create custom template.
DELETE /api/exclusion-templates/{id} Delete custom template.
GET  /api/system/info                System information.
GET  /api/system/activity            Recent activity feed.
WS   /ws/jobs/{job_id}              Real-time backup progress events.
GET  /api/machines                   List discovered machines.
POST /api/machines/scan              Trigger LAN discovery scan.
GET  /api/machines/scan/status       Scan progress / status.
GET  /api/machines/{ip}              Machine detail.
PUT  /api/machines/{ip}/credentials  Save SMB credentials.
POST /api/machines/{ip}/ping         Re-probe machine.
DELETE /api/machines/{ip}            Forget machine.
POST /api/machines/{ip}/create-job   Auto-create backup job.
POST /api/jobs/{job_id}/scrub        Trigger integrity scrub (async, 202).
GET  /api/jobs/{job_id}/scrub/status Latest scrub status / result.
GET  /api/jobs/{job_id}/scrub/corrupt List chunks flagged as corrupt.

Run locally
-----------
    uvicorn api.main:app --reload --port 8000

Or via Docker Compose (see docker-compose.yml sentinel-api service).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from api.routers import jobs, snapshots, storage
from api.routers import scheduler as scheduler_router
from api.routers import restore, exclusions, system, machines, scrub as scrub_router
from api.routers import agents as agents_router
from api.ws.manager import ws_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Sentinel API starting up (version %s)", app.version)
    ws_manager.set_loop(asyncio.get_running_loop())

    # Start APScheduler (gracefully degraded if not installed)
    try:
        from api.scheduler import init_scheduler
        init_scheduler()
    except Exception as exc:
        log.warning("Scheduler init failed (non-fatal): %s", exc)

    # Stale agent check every 2 minutes
    try:
        from api.scheduler import get_scheduler
        from api.routers.agents import _mark_stale_agents_job
        scheduler = get_scheduler()
        if scheduler:
            scheduler.add_job(
                _mark_stale_agents_job,
                trigger='interval',
                minutes=2,
                id='agent_stale_check',
                replace_existing=True,
            )
    except Exception as exc:
        log.warning("Agent stale check job not registered: %s", exc)

    yield

    try:
        from api.scheduler import shutdown_scheduler
        shutdown_scheduler()
    except Exception:
        pass

    log.info("Sentinel API shutting down")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Sentinel Backup API",
    version="0.6.0",
    description=(
        "REST + WebSocket API for the Sentinel modular backup system. "
        "Trigger backup jobs, query snapshots, monitor storage usage, "
        "manage schedules, restore files, and stream real-time progress."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ──────────────────────────────────────────────────────────────────────

_cors_origins = os.getenv(
    "SENTINEL_CORS_ORIGINS",
    "http://localhost:5173,http://localhost:3000",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(jobs.router)
app.include_router(snapshots.router)
app.include_router(storage.router)
app.include_router(scheduler_router.router)
app.include_router(restore.router)
app.include_router(exclusions.router)
app.include_router(system.router)
app.include_router(machines.router)
app.include_router(scrub_router.router)
app.include_router(agents_router.router)


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/jobs/{job_id}")
async def ws_job_progress(job_id: str, ws: WebSocket):
    """
    Real-time backup progress stream for *job_id*.

    Connect **before** calling ``POST /api/jobs/{job_id}/run`` to receive
    all events from the start of the job.

    Events
    ------
    ``JOB_START``   — job has been picked up by the worker
    ``FILE_DONE``   — a file was processed (incremental, one per file)
    ``JOB_SUCCESS`` — backup completed successfully
    ``JOB_FAILED``  — backup failed; ``error`` field contains the message
    """
    await ws_manager.connect(job_id, ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(job_id, ws)


# ── Health endpoint ───────────────────────────────────────────────────────────

@app.get("/healthz", tags=["system"])
async def healthz():
    """Liveness probe — returns 200 when the API process is running."""
    return {"status": "ok", "version": app.version}
