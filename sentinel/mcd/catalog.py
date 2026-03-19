"""
Metadata & Catalog Database (MCD) — Module 3.

Uses SQLite in WAL (Write-Ahead Logging) mode so readers are never blocked
by concurrent catalog writes.  A thread-local connection pool is maintained
so each worker thread gets its own connection without contention.

Schema
------
chunks         – unique blocks indexed by SHA-256 hash_id; ref_count tracks
                 how many file_chunks rows reference this chunk.
snapshots      – one row per backup job run.
files          – one row per file captured in a snapshot.
file_chunks    – ordered mapping from file → chunks (for reconstruction).

All foreign keys are enforced.  The catalog is compacted and backed up to the
SPI after every successful backup job (see BackupEngine).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

log = logging.getLogger(__name__)

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;

CREATE TABLE IF NOT EXISTS chunks (
    hash_id         TEXT PRIMARY KEY,
    storage_key     TEXT NOT NULL,
    original_size   INTEGER NOT NULL,
    compressed_size INTEGER NOT NULL,
    iv              BLOB NOT NULL,
    ref_count       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks (hash_id);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'in_progress',
    total_bytes INTEGER,
    chunk_count INTEGER,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS files (
    file_id     TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    mtime       REAL,
    mode        INTEGER,
    UNIQUE (snapshot_id, path)
);
CREATE INDEX IF NOT EXISTS idx_files_path     ON files (path);
CREATE INDEX IF NOT EXISTS idx_files_snapshot ON files (snapshot_id);

CREATE TABLE IF NOT EXISTS file_chunks (
    file_id     TEXT    NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    hash_id     TEXT    NOT NULL REFERENCES chunks(hash_id),
    PRIMARY KEY (file_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_fc_hash ON file_chunks (hash_id);

CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL,
    status            TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    files_processed   INTEGER DEFAULT 0,
    files_skipped_cbt INTEGER DEFAULT 0,
    chunks_uploaded   INTEGER DEFAULT 0,
    chunks_deduped    INTEGER DEFAULT 0,
    bytes_original    INTEGER DEFAULT 0,
    bytes_stored      INTEGER DEFAULT 0,
    provider_used     TEXT DEFAULT 'unknown',
    duration_s        REAL DEFAULT 0.0,
    errors            TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_runs_job ON runs (job_id, started_at DESC);
"""


class CatalogManager:
    """
    Thread-safe SQLite catalog manager.

    A thread-local connection is opened on first use and reused for the
    lifetime of the thread, avoiding both the overhead of constant opens
    and the ``ProgrammingError: SQLite objects created in a thread`` issue.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._local = threading.local()
        # Initialise schema on a dedicated connection.
        conn = self._open_connection()
        conn.executescript(_DDL)
        conn.commit()
        # Migration: add corrupted_at column if it doesn't exist yet.
        try:
            conn.execute("ALTER TABLE chunks ADD COLUMN corrupted_at TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists — safe to ignore
        conn.close()

    # ------------------------------------------------------------------ #
    #  Connection management                                               #
    # ------------------------------------------------------------------ #

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "PRAGMA foreign_keys=ON;"
            "PRAGMA synchronous=NORMAL;"
        )
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._open_connection()
        return self._local.conn

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager for a single database transaction."""
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        """Close the current thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------ #
    #  Chunk operations                                                    #
    # ------------------------------------------------------------------ #

    def chunk_exists(self, hash_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM chunks WHERE hash_id = ?", (hash_id,)
        ).fetchone()
        return row is not None

    def add_chunk(
        self,
        hash_id: str,
        storage_key: str,
        original_size: int,
        compressed_size: int,
        iv: bytes,
    ) -> None:
        """
        Insert a new chunk or increment ref_count if it already exists.

        The UPSERT pattern ensures atomicity without a prior SELECT.
        """
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO chunks
                    (hash_id, storage_key, original_size, compressed_size, iv)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(hash_id) DO UPDATE
                    SET ref_count = ref_count + 1
                """,
                (hash_id, storage_key, original_size, compressed_size, iv),
            )

    def increment_ref(self, hash_id: str) -> None:
        """Increment ref_count for an existing chunk (dedup hit)."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE chunks SET ref_count = ref_count + 1 WHERE hash_id = ?",
                (hash_id,),
            )

    def decrement_ref(self, hash_id: str) -> int:
        """
        Decrement ref_count and return the new value.

        Returns -1 if the chunk does not exist (should not happen in normal
        operation, but handled gracefully).
        """
        with self._tx() as cur:
            cur.execute(
                "UPDATE chunks SET ref_count = ref_count - 1 WHERE hash_id = ?",
                (hash_id,),
            )
            row = cur.execute(
                "SELECT ref_count FROM chunks WHERE hash_id = ?", (hash_id,)
            ).fetchone()
            return row["ref_count"] if row else -1

    def get_orphan_chunks(self) -> List[str]:
        """Return hash_ids of all chunks whose ref_count has reached zero."""
        rows = self._conn.execute(
            "SELECT hash_id FROM chunks WHERE ref_count <= 0"
        ).fetchall()
        return [r["hash_id"] for r in rows]

    def delete_chunk_record(self, hash_id: str) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM chunks WHERE hash_id = ?", (hash_id,))

    def get_chunk(self, hash_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM chunks WHERE hash_id = ?", (hash_id,)
        ).fetchone()

    def list_all_chunks(self) -> List[sqlite3.Row]:
        """Return every chunk row (for full-scan scrub mode)."""
        return self._conn.execute("SELECT * FROM chunks").fetchall()

    def mark_chunk_corrupt(self, hash_id: str) -> None:
        """Record that a chunk failed integrity verification."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE chunks SET corrupted_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                " WHERE hash_id = ?",
                (hash_id,),
            )

    def get_corrupt_chunks(self) -> List[sqlite3.Row]:
        """Return all chunks that have been flagged as corrupt."""
        return self._conn.execute(
            "SELECT * FROM chunks WHERE corrupted_at IS NOT NULL ORDER BY corrupted_at"
        ).fetchall()

    def get_snapshots_for_chunks(self, hash_ids: List[str]) -> List[str]:
        """Return distinct snapshot_ids that reference any of the given chunk hashes."""
        if not hash_ids:
            return []
        placeholders = ",".join("?" * len(hash_ids))
        rows = self._conn.execute(
            f"""
            SELECT DISTINCT f.snapshot_id
              FROM file_chunks fc
              JOIN files f ON f.file_id = fc.file_id
             WHERE fc.hash_id IN ({placeholders})
            """,
            hash_ids,
        ).fetchall()
        return [r["snapshot_id"] for r in rows]

    def random_chunk_sample(self, percentage: float = 0.01) -> List[sqlite3.Row]:
        """
        Return a random sample of ~*percentage* of all stored chunks.

        Used by the Data Scrubbing routine (M&M module).
        ``RANDOM()`` in SQLite is O(n) — acceptable for scrubbing workloads.
        """
        total = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        limit = max(1, int(total * percentage))
        rows = self._conn.execute(
            "SELECT * FROM chunks ORDER BY RANDOM() LIMIT ?", (limit,)
        ).fetchall()
        return rows

    # ------------------------------------------------------------------ #
    #  Snapshot operations                                                 #
    # ------------------------------------------------------------------ #

    def create_snapshot(self, snapshot_id: str, job_id: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO snapshots (snapshot_id, job_id) VALUES (?, ?)",
                (snapshot_id, job_id),
            )
        log.info("Snapshot %s created for job %s", snapshot_id, job_id)

    def finalize_snapshot(
        self,
        snapshot_id: str,
        total_bytes: int,
        chunk_count: int,
        status: str = "completed",
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE snapshots
                   SET status = ?,
                       total_bytes = ?,
                       chunk_count = ?,
                       finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                 WHERE snapshot_id = ?
                """,
                (status, total_bytes, chunk_count, snapshot_id),
            )

    def fail_snapshot(self, snapshot_id: str) -> None:
        self.finalize_snapshot(snapshot_id, 0, 0, status="failed")

    def list_snapshots(self, job_id: Optional[str] = None) -> List[sqlite3.Row]:
        if job_id:
            return self._conn.execute(
                "SELECT * FROM snapshots WHERE job_id = ? ORDER BY started_at",
                (job_id,),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM snapshots ORDER BY started_at"
        ).fetchall()

    def delete_snapshot(self, snapshot_id: str) -> None:
        """
        Remove a snapshot row.  Cascades to files and file_chunks rows.
        ref_count of affected chunks must be decremented separately by
        the Garbage Collector before calling this.
        """
        with self._tx() as cur:
            cur.execute(
                "DELETE FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            )

    # ------------------------------------------------------------------ #
    #  File operations                                                     #
    # ------------------------------------------------------------------ #

    def add_file(
        self,
        file_id: str,
        snapshot_id: str,
        path: str,
        size: int,
        mtime: float,
        mode: int,
        chunk_hashes: List[str],
    ) -> None:
        """
        Record a file and its ordered list of chunk hashes.

        Inserts into ``files`` and ``file_chunks`` within one transaction.
        """
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO files (file_id, snapshot_id, path, size, mtime, mode)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (file_id, snapshot_id, path, size, mtime, mode),
            )
            cur.executemany(
                "INSERT INTO file_chunks (file_id, chunk_index, hash_id) VALUES (?, ?, ?)",
                [(file_id, idx, h) for idx, h in enumerate(chunk_hashes)],
            )

    def get_file_chunks(self, file_id: str) -> List[sqlite3.Row]:
        """Return chunk rows for *file_id* in index order (for restore)."""
        return self._conn.execute(
            """
            SELECT fc.chunk_index, c.*
              FROM file_chunks fc
              JOIN chunks c ON c.hash_id = fc.hash_id
             WHERE fc.file_id = ?
             ORDER BY fc.chunk_index
            """,
            (file_id,),
        ).fetchall()

    def get_snapshot_files(self, snapshot_id: str) -> List[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM files WHERE snapshot_id = ? ORDER BY path",
            (snapshot_id,),
        ).fetchall()

    def get_snapshot_chunk_hashes(self, snapshot_id: str) -> List[str]:
        """All distinct chunk hashes referenced by *snapshot_id*."""
        rows = self._conn.execute(
            """
            SELECT DISTINCT fc.hash_id
              FROM file_chunks fc
              JOIN files f ON f.file_id = fc.file_id
             WHERE f.snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchall()
        return [r["hash_id"] for r in rows]

    # ------------------------------------------------------------------ #
    #  Run history operations                                              #
    # ------------------------------------------------------------------ #

    def save_run(self, state: Dict[str, Any]) -> None:
        """
        Upsert a run record by ``run_id``.

        *state* must contain at minimum: ``run_id``, ``job_id``, ``status``,
        ``started_at``.  All other fields default to zero/empty if absent.
        """
        errors = state.get("errors", [])
        errors_json = json.dumps(errors) if isinstance(errors, list) else errors

        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO runs
                    (run_id, job_id, status, started_at, finished_at,
                     files_processed, files_skipped_cbt, chunks_uploaded,
                     chunks_deduped, bytes_original, bytes_stored,
                     provider_used, duration_s, errors)
                VALUES
                    (:run_id, :job_id, :status, :started_at, :finished_at,
                     :files_processed, :files_skipped_cbt, :chunks_uploaded,
                     :chunks_deduped, :bytes_original, :bytes_stored,
                     :provider_used, :duration_s, :errors)
                ON CONFLICT(run_id) DO UPDATE SET
                    status            = excluded.status,
                    finished_at       = excluded.finished_at,
                    files_processed   = excluded.files_processed,
                    files_skipped_cbt = excluded.files_skipped_cbt,
                    chunks_uploaded   = excluded.chunks_uploaded,
                    chunks_deduped    = excluded.chunks_deduped,
                    bytes_original    = excluded.bytes_original,
                    bytes_stored      = excluded.bytes_stored,
                    provider_used     = excluded.provider_used,
                    duration_s        = excluded.duration_s,
                    errors            = excluded.errors
                """,
                {
                    "run_id":            state.get("run_id"),
                    "job_id":            state.get("job_id"),
                    "status":            state.get("status", "unknown"),
                    "started_at":        state.get("started_at"),
                    "finished_at":       state.get("finished_at"),
                    "files_processed":   state.get("files_processed", 0),
                    "files_skipped_cbt": state.get("files_skipped_cbt", 0),
                    "chunks_uploaded":   state.get("chunks_uploaded", 0),
                    "chunks_deduped":    state.get("chunks_deduped", 0),
                    "bytes_original":    state.get("bytes_original", 0),
                    "bytes_stored":      state.get("bytes_stored", 0),
                    "provider_used":     state.get("provider_used", "unknown"),
                    "duration_s":        state.get("duration_s", 0.0),
                    "errors":            errors_json,
                },
            )

    def list_runs(self, job_id: str, limit: int = 100) -> List[sqlite3.Row]:
        """Return up to *limit* runs for *job_id*, newest first."""
        return self._conn.execute(
            """
            SELECT * FROM runs
             WHERE job_id = ?
             ORDER BY started_at DESC
             LIMIT ?
            """,
            (job_id, limit),
        ).fetchall()

    def get_run(self, run_id: str) -> Optional[sqlite3.Row]:
        """Return a single run row by *run_id*, or ``None`` if not found."""
        return self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()

    def list_recent_runs(self, limit: int = 50) -> List[sqlite3.Row]:
        """Return the *limit* most recent runs across all jobs (for activity feed)."""
        return self._conn.execute(
            """
            SELECT * FROM runs
             ORDER BY started_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
