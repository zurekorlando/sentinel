"""
Snapshots router — list and manage backup snapshots.

Endpoints
---------
GET    /api/snapshots                List all snapshots (optionally by job_id).
GET    /api/snapshots/{snapshot_id}  Full detail including file list.
DELETE /api/snapshots/{snapshot_id}  Delete snapshot + run chunk GC.

Note: snapshot_id is only unique within a catalog, so all mutating endpoints
require the ``job_id`` query parameter to locate the correct catalog.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from api.deps import get_catalog, get_job_config, get_storage, list_job_configs
from api.schemas import FileResponse, SnapshotDetail, SnapshotResponse

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_snapshot(row, job_id: str) -> SnapshotResponse:
    return SnapshotResponse(
        snapshot_id=row["snapshot_id"],
        job_id=job_id,
        status=row["status"],
        total_bytes=row["total_bytes"],
        chunk_count=row["chunk_count"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=List[SnapshotResponse])
async def list_snapshots(
    job_id: Optional[str] = Query(None, description="Filter by job ID"),
):
    """
    Return a list of snapshots, sorted newest-first.

    If *job_id* is provided only that job's catalog is queried; otherwise
    snapshots from all configured jobs are aggregated.
    """
    if job_id:
        get_job_config(job_id)  # raises 404 if not found
        catalog = get_catalog(job_id)
        rows = catalog.list_snapshots(job_id)
        return [_row_to_snapshot(r, job_id) for r in rows]

    # Aggregate across all jobs
    all_snaps: List[SnapshotResponse] = []
    for cfg in list_job_configs():
        try:
            catalog = get_catalog(cfg.job_id)
            rows = catalog.list_snapshots(cfg.job_id)
            all_snaps.extend(_row_to_snapshot(r, cfg.job_id) for r in rows)
        except Exception:
            pass

    all_snaps.sort(key=lambda s: s.started_at, reverse=True)
    return all_snaps


@router.get("/{snapshot_id}", response_model=SnapshotDetail)
async def get_snapshot(
    snapshot_id: str,
    job_id: str = Query(..., description="Job ID that owns this snapshot"),
):
    """Return full snapshot details, including the list of backed-up files."""
    get_job_config(job_id)
    catalog = get_catalog(job_id)

    snap_row = next(
        (r for r in catalog.list_snapshots(job_id) if r["snapshot_id"] == snapshot_id),
        None,
    )
    if snap_row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Snapshot '{snapshot_id}' not found in job '{job_id}'",
        )

    file_rows = catalog.get_snapshot_files(snapshot_id)
    files = [
        FileResponse(
            file_id=f["file_id"],
            path=f["path"],
            size=f["size"],
            mtime=f["mtime"],
        )
        for f in file_rows
    ]

    return SnapshotDetail(**_row_to_snapshot(snap_row, job_id).model_dump(), files=files)


@router.delete("/{snapshot_id}", status_code=204)
async def delete_snapshot(
    snapshot_id: str,
    job_id: str = Query(..., description="Job ID that owns this snapshot"),
):
    """
    Delete a snapshot and garbage-collect orphaned chunks.

    This endpoint:
    1. Decrements the ref_count of every chunk referenced by the snapshot.
    2. Deletes the snapshot record (cascades to files / file_chunks).
    3. Collects chunks whose ref_count reached 0.
    4. Deletes those chunks from the storage backend.
    5. Removes their catalog records.

    This is a synchronous operation that may take several seconds for
    large snapshots.
    """
    get_job_config(job_id)
    catalog = get_catalog(job_id)
    storage = get_storage(job_id)

    # Confirm the snapshot exists
    exists = any(
        r["snapshot_id"] == snapshot_id for r in catalog.list_snapshots(job_id)
    )
    if not exists:
        raise HTTPException(
            status_code=404,
            detail=f"Snapshot '{snapshot_id}' not found in job '{job_id}'",
        )

    # Decrement ref_counts for all chunks in this snapshot
    chunk_hashes = catalog.get_snapshot_chunk_hashes(snapshot_id)
    for h in chunk_hashes:
        catalog.decrement_ref(h)

    # Delete the snapshot (cascades to files / file_chunks)
    catalog.delete_snapshot(snapshot_id)

    # Collect and physically delete orphaned chunks (ref_count <= 0)
    orphans = catalog.get_orphan_chunks()
    for h in orphans:
        try:
            storage.delete_chunk(h)
        except Exception:
            pass
        catalog.delete_chunk_record(h)
