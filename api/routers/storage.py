"""
Storage router — usage statistics and health checks.

Endpoints
---------
GET /api/storage/{job_id}/stats   Chunk counts, byte savings, dedup ratio.
GET /api/storage/{job_id}/health  Storage backend + catalog health check.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.deps import get_catalog, get_job_config, get_storage
from api.schemas import HealthResponse, StorageStatsResponse

router = APIRouter(prefix="/api/storage", tags=["storage"])


@router.get("/{job_id}/stats", response_model=StorageStatsResponse)
async def get_storage_stats(job_id: str):
    """
    Return storage usage statistics for *job_id*.

    Metrics
    -------
    total_chunks           Total unique chunks stored (after dedup).
    total_original_bytes   Sum of original (pre-compression) sizes.
    total_compressed_bytes Sum of compressed (post-encryption) blob sizes.
    dedup_ratio            Fraction of shared chunks (chunks_deduped / total).
    snapshot_count         Number of completed snapshots for this job.
    storage_provider       "local" or "s3".
    """
    cfg = get_job_config(job_id)
    catalog = get_catalog(job_id)
    conn = catalog._conn

    total_chunks: int = conn.execute(
        "SELECT COUNT(*) FROM chunks"
    ).fetchone()[0]

    total_original: int = conn.execute(
        "SELECT COALESCE(SUM(original_size), 0) FROM chunks"
    ).fetchone()[0]

    total_compressed: int = conn.execute(
        "SELECT COALESCE(SUM(compressed_size), 0) FROM chunks"
    ).fetchone()[0]

    # Total ref_count across all chunks equals the sum of references.
    # (total_refs - total_chunks) / total_refs gives the fraction saved by dedup.
    total_refs: int = conn.execute(
        "SELECT COALESCE(SUM(ref_count), 0) FROM chunks"
    ).fetchone()[0]

    dedup_ratio = (
        (total_refs - total_chunks) / total_refs if total_refs > 0 else 0.0
    )

    snapshot_count = len(catalog.list_snapshots(job_id))

    return StorageStatsResponse(
        job_id=job_id,
        total_chunks=total_chunks,
        total_original_bytes=total_original,
        total_compressed_bytes=total_compressed,
        dedup_ratio=round(dedup_ratio, 4),
        snapshot_count=snapshot_count,
        storage_provider=cfg.storage.provider,
    )


@router.get("/{job_id}/health", response_model=HealthResponse)
async def get_storage_health(job_id: str):
    """
    Check storage backend and catalog accessibility.

    Returns ``status: "ok"`` only when both are healthy.
    """
    get_job_config(job_id)  # raises 404 if job doesn't exist

    storage_ok = False
    catalog_ok = False

    try:
        storage = get_storage(job_id)
        storage_ok = storage.health_check()
    except Exception:
        pass

    try:
        catalog = get_catalog(job_id)
        catalog._conn.execute("SELECT 1")
        catalog_ok = True
    except Exception:
        pass

    return HealthResponse(
        status="ok" if (storage_ok and catalog_ok) else "degraded",
        storage_healthy=storage_ok,
        catalog_accessible=catalog_ok,
    )
