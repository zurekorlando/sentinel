"""
Scrub router — data integrity verification via REST API.

Endpoints
---------
POST   /api/jobs/{job_id}/scrub          Trigger a scrub (async, 202).
GET    /api/jobs/{job_id}/scrub/status   Poll status of the last scrub for this job.
GET    /api/jobs/{job_id}/scrub/corrupt  List all chunks flagged as corrupt.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from api.deps import get_catalog, get_job_config, get_storage

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["scrub"])


# ── In-memory scrub state (job_id → last ScrubState) ─────────────────────────

@dataclass
class ScrubState:
    scrub_id: str
    job_id: str
    status: str = "queued"          # queued | running | success | failed
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    finished_at: Optional[str] = None
    chunks_checked: int = 0
    chunks_ok: int = 0
    chunks_corrupt: int = 0
    corrupt_chunk_ids: List[str] = field(default_factory=list)
    affected_snapshot_ids: List[str] = field(default_factory=list)
    duration_s: float = 0.0
    errors: List[str] = field(default_factory=list)
    mode: str = "sample"            # "sample" | "full"
    sample_fraction: float = 0.01


_active_scrubs: Dict[str, ScrubState] = {}


# ── Request schema ────────────────────────────────────────────────────────────

class ScrubRequest(BaseModel):
    sample: float = 0.01    # fraction to check in sample mode (ignored if full=True)
    full: bool = False       # True → check every stored chunk


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/{job_id}/scrub", status_code=202)
async def start_scrub(
    job_id: str,
    req: ScrubRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Start a data-integrity scrub for *job_id* in the background (HTTP 202).

    Poll ``GET /api/jobs/{job_id}/scrub/status`` for progress and results.
    """
    import uuid

    cfg = get_job_config(job_id)
    if not cfg.key_salt_hex:
        raise HTTPException(
            status_code=422,
            detail="No key salt in job config — run a backup first.",
        )

    passphrase = os.getenv("SENTINEL_PASSPHRASE", "")
    if not passphrase:
        raise HTTPException(
            status_code=422,
            detail="SENTINEL_PASSPHRASE env var is not set.",
        )

    # Reject if a scrub is already running for this job
    existing = _active_scrubs.get(job_id)
    if existing and existing.status == "running":
        raise HTTPException(
            status_code=409,
            detail=f"A scrub for job '{job_id}' is already running ({existing.scrub_id}).",
        )

    scrub_id = str(uuid.uuid4())
    state = ScrubState(
        scrub_id=scrub_id,
        job_id=job_id,
        mode="full" if req.full else "sample",
        sample_fraction=req.sample,
    )
    _active_scrubs[job_id] = state

    background_tasks.add_task(
        _run_scrub_background,
        job_id=job_id,
        passphrase=passphrase,
        sample=req.sample,
        full=req.full,
        state=state,
    )

    return {
        "scrub_id": scrub_id,
        "job_id": job_id,
        "status": "queued",
        "mode": state.mode,
    }


@router.get("/{job_id}/scrub/status")
async def scrub_status(job_id: str) -> dict:
    """Return the status (and result) of the most recent scrub for *job_id*."""
    # Validate job exists
    get_job_config(job_id)

    state = _active_scrubs.get(job_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail=f"No scrub has been run for job '{job_id}'.",
        )
    return asdict(state)


@router.get("/{job_id}/scrub/corrupt")
async def list_corrupt_chunks(job_id: str) -> dict:
    """
    Return all chunks for *job_id* that have been flagged as corrupt by a scrub.

    These chunks were verified and failed AES-256-GCM decryption.  Any snapshot
    that references them may not be fully restorable.
    """
    get_job_config(job_id)
    catalog = get_catalog(job_id)
    rows = catalog.get_corrupt_chunks()
    chunks = [
        {
            "hash_id": r["hash_id"],
            "storage_key": r["storage_key"],
            "original_size": r["original_size"],
            "corrupted_at": r["corrupted_at"],
        }
        for r in rows
    ]
    affected = catalog.get_snapshots_for_chunks([r["hash_id"] for r in rows])
    return {
        "job_id": job_id,
        "corrupt_chunks": chunks,
        "total": len(chunks),
        "affected_snapshot_ids": affected,
    }


# ── Background worker ─────────────────────────────────────────────────────────

def _run_scrub_background(
    job_id: str,
    passphrase: str,
    sample: float,
    full: bool,
    state: ScrubState,
) -> None:
    """Blocking scrub worker — runs in a thread via BackgroundTasks."""
    from datetime import datetime, timezone
    from sentinel.dpe.crypto import ChunkCipher, derive_key
    from sentinel.scrub import ScrubEngine

    state.status = "running"
    log.info("Scrub starting for job %s (mode=%s)", job_id, state.mode)

    try:
        cfg = get_job_config(job_id)
        salt = bytes.fromhex(cfg.key_salt_hex)
        derived = derive_key(passphrase, salt)
        cipher = ChunkCipher(derived.key)

        catalog = get_catalog(job_id)
        storage = get_storage(job_id)

        def _on_progress(progress: dict) -> None:
            state.chunks_checked = progress["chunks_checked"]
            state.chunks_ok = progress["chunks_ok"]
            state.chunks_corrupt = progress["chunks_corrupt"]

        engine = ScrubEngine(
            job_id=job_id,
            catalog=catalog,
            storage=storage,
            cipher=cipher,
            on_progress=_on_progress,
        )
        result = engine.run(sample=sample, full=full)

        state.status = result.status
        state.chunks_checked = result.chunks_checked
        state.chunks_ok = result.chunks_ok
        state.chunks_corrupt = result.chunks_corrupt
        state.corrupt_chunk_ids = result.corrupt_chunk_ids
        state.affected_snapshot_ids = result.affected_snapshot_ids
        state.duration_s = result.duration_s
        state.errors = result.errors

    except Exception as exc:
        log.exception("Scrub background worker failed for job %s", job_id)
        state.status = "failed"
        state.errors.append(str(exc))

    finally:
        state.finished_at = datetime.now(timezone.utc).isoformat()
        log.info(
            "Scrub done for job %s | status=%s corrupt=%d/%d",
            job_id, state.status, state.chunks_corrupt, state.chunks_checked,
        )
