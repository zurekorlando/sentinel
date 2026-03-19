"""
Dependency injection and shared state for the Sentinel API.

State management
----------------
- Job configs are loaded from SENTINEL_JOBS_DIR (default: ./jobs).
- CatalogManager instances are lazily created and cached per job_id so the
  SQLite WAL connection is shared across requests for the same job.
- Storage providers follow the same lazy-singleton pattern.
- Active backup runs are tracked in _active_runs (in-memory, lost on restart).
- Active restores are tracked in _active_restores (in-memory, lost on restart).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from sentinel.config import JobConfig, build_storage_provider, load_job
from sentinel.mcd.catalog import CatalogManager
from sentinel.spi.base import IStorageProvider


# ── Lazy singletons ───────────────────────────────────────────────────────────

_catalogs: Dict[str, CatalogManager] = {}
_storages: Dict[str, IStorageProvider] = {}


# ── Run state registry ────────────────────────────────────────────────────────

@dataclass
class RunState:
    """In-memory state for one backup run (survives until the next run starts)."""
    run_id: str
    job_id: str
    status: str = "queued"          # queued | running | success | failed
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    finished_at: Optional[str] = None
    files_processed: int = 0
    files_skipped_cbt: int = 0
    chunks_uploaded: int = 0
    chunks_deduped: int = 0
    bytes_original: int = 0
    bytes_stored: int = 0
    provider_used: str = "unknown"
    duration_s: float = 0.0
    errors: List[str] = field(default_factory=list)


# job_id → most recent RunState
_active_runs: Dict[str, RunState] = {}

# restore_id → RestoreResponse dict (in-memory, lost on restart)
_active_restores: Dict[str, Dict[str, Any]] = {}


# ── Job config helpers ────────────────────────────────────────────────────────

def jobs_dir() -> Path:
    """Directory that holds *.json job configuration files."""
    return Path(os.getenv("SENTINEL_JOBS_DIR", "jobs"))


def list_job_configs() -> List[JobConfig]:
    """Return all valid job configs found in SENTINEL_JOBS_DIR."""
    configs: List[JobConfig] = []
    for path in sorted(jobs_dir().glob("*.json")):
        try:
            configs.append(load_job(path))
        except Exception:
            pass
    return configs


def get_job_config(job_id: str) -> JobConfig:
    """Load and return the JobConfig for *job_id*.  Raises HTTP 404 if absent."""
    path = jobs_dir() / f"{job_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    try:
        return load_job(path)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to load job config: {exc}"
        ) from exc


# ── Catalog / storage accessors ───────────────────────────────────────────────

def get_catalog(job_id: str) -> CatalogManager:
    """Return (or create) the CatalogManager singleton for *job_id*."""
    if job_id not in _catalogs:
        cfg = get_job_config(job_id)
        _catalogs[job_id] = CatalogManager(cfg.catalog_path)
    return _catalogs[job_id]


def get_storage(job_id: str) -> IStorageProvider:
    """Return (or create) the IStorageProvider singleton for *job_id*."""
    if job_id not in _storages:
        cfg = get_job_config(job_id)
        _storages[job_id] = build_storage_provider(cfg.storage)
    return _storages[job_id]


# ── Run state accessors ───────────────────────────────────────────────────────

def get_run_state(job_id: str) -> Optional[RunState]:
    return _active_runs.get(job_id)


def set_run_state(job_id: str, state: RunState) -> None:
    _active_runs[job_id] = state


def persist_run_state(job_id: str, state: RunState) -> None:
    """
    Write (upsert) a RunState to the ``runs`` table of the job's catalog.

    Called at run start (status='running') and completion (status='success'/'failed').
    Silently swallows errors so a catalog write failure never aborts a backup.
    """
    try:
        catalog = get_catalog(job_id)
        catalog.save_run({
            "run_id":            state.run_id,
            "job_id":            state.job_id,
            "status":            state.status,
            "started_at":        state.started_at,
            "finished_at":       state.finished_at,
            "files_processed":   state.files_processed,
            "files_skipped_cbt": state.files_skipped_cbt,
            "chunks_uploaded":   state.chunks_uploaded,
            "chunks_deduped":    state.chunks_deduped,
            "bytes_original":    state.bytes_original,
            "bytes_stored":      state.bytes_stored,
            "provider_used":     state.provider_used,
            "duration_s":        state.duration_s,
            "errors":            state.errors,
        })
    except Exception:
        pass  # never let a catalog write abort the backup


# ── Restore state accessors ───────────────────────────────────────────────────

def get_restore_state(restore_id: str) -> Optional[Dict[str, Any]]:
    return _active_restores.get(restore_id)


def set_restore_state(restore_id: str, state: Dict[str, Any]) -> None:
    _active_restores[restore_id] = state
