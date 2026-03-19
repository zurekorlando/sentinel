"""
Restore router — trigger and monitor snapshot restores.

Endpoints
---------
POST /api/snapshots/{snapshot_id}/restore   Start a restore (async, 202).
GET  /api/restore/{restore_id}/status       Poll restore progress.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException

from sentinel.config import build_storage_provider
from sentinel.engine import RestoreEngine
from sentinel.mcd.catalog import CatalogManager

from api.deps import (
    get_catalog,
    get_job_config,
    get_restore_state,
    get_storage,
    set_restore_state,
)
from api.schemas import RestoreRequest, RestoreResponse

router = APIRouter(prefix="/api", tags=["restore"])


@router.post(
    "/snapshots/{snapshot_id}/restore",
    response_model=RestoreResponse,
    status_code=202,
)
async def start_restore(
    snapshot_id: str,
    body: RestoreRequest,
    bg: BackgroundTasks,
):
    """
    Start a restore of *snapshot_id* asynchronously.

    Returns HTTP 202 Accepted with a ``restore_id``.
    Poll ``GET /api/restore/{restore_id}/status`` for progress.

    The ``passphrase`` and ``key_salt_hex`` (from the job config) are used
    together to re-derive the AES-256-GCM key.  They must match the values
    used at backup time.
    """
    # Validate job exists and has a salt
    cfg = get_job_config(body.job_id)
    if not cfg.key_salt_hex:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Job '{body.job_id}' has no key_salt_hex — "
                "run a backup first to derive and store the encryption salt."
            ),
        )

    restore_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    initial_state = {
        "restore_id": restore_id,
        "snapshot_id": snapshot_id,
        "status": "running",
        "files_total": 0,
        "files_restored": 0,
        "bytes_restored": 0,
        "started_at": started_at,
        "finished_at": None,
        "errors": [],
    }
    set_restore_state(restore_id, initial_state)

    bg.add_task(
        _execute_restore,
        restore_id=restore_id,
        snapshot_id=snapshot_id,
        job_id=body.job_id,
        passphrase=body.passphrase,
        destination_path=body.destination_path,
        file_paths=body.file_paths,
        overwrite=body.overwrite,
        started_at=started_at,
    )

    return RestoreResponse(**initial_state)


@router.get("/restore/{restore_id}/status", response_model=RestoreResponse)
async def restore_status(restore_id: str):
    """Poll the status of a running or completed restore."""
    state = get_restore_state(restore_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail=f"Restore '{restore_id}' not found",
        )
    return RestoreResponse(**state)


# ── Background task ───────────────────────────────────────────────────────────

async def _execute_restore(
    restore_id: str,
    snapshot_id: str,
    job_id: str,
    passphrase: str,
    destination_path: str,
    file_paths,
    overwrite: bool,
    started_at: str,
) -> None:
    """Run RestoreEngine in a thread pool and update restore state."""

    def _on_progress(event: dict) -> None:
        state = get_restore_state(restore_id)
        if state:
            state["files_restored"] = event.get("files_restored", state["files_restored"])
            state["files_total"] = event.get("files_total", state["files_total"])
            state["bytes_restored"] = event.get("bytes_restored", state["bytes_restored"])

    def _run_sync():
        cfg = get_job_config(job_id)
        catalog = get_catalog(job_id)
        storage = get_storage(job_id)
        salt = bytes.fromhex(cfg.key_salt_hex)

        engine = RestoreEngine(
            catalog=catalog,
            storage=storage,
            passphrase=passphrase,
            salt=salt,
            destination_path=destination_path,
            overwrite=overwrite,
            on_progress=_on_progress,
        )
        return engine.restore_snapshot(snapshot_id, file_paths)

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _run_sync)

        finished_at = datetime.now(timezone.utc).isoformat()
        set_restore_state(restore_id, {
            "restore_id": restore_id,
            "snapshot_id": snapshot_id,
            "status": result.status,
            "files_total": result.files_total,
            "files_restored": result.files_restored,
            "bytes_restored": result.bytes_restored,
            "started_at": started_at,
            "finished_at": finished_at,
            "errors": result.errors,
        })

    except Exception as exc:
        finished_at = datetime.now(timezone.utc).isoformat()
        state = get_restore_state(restore_id) or {}
        state.update({
            "status": "failed",
            "finished_at": finished_at,
            "errors": state.get("errors", []) + [str(exc)],
        })
        set_restore_state(restore_id, state)
