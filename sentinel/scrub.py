"""
ScrubEngine — data integrity verification for stored chunks.

Scrub pipeline per chunk
------------------------
1. Download the raw blob from the storage backend.
2. Attempt AES-256-GCM decryption — the GCM tag acts as an AEAD authenticator.
3. Any failure (bad tag, missing chunk, I/O error) is treated as corruption:
   - The chunk is flagged in the catalog (``corrupted_at`` timestamp).
   - The affected snapshot IDs are collected and reported.

Two scan modes
--------------
- Sample mode  (default): checks a random ``sample`` fraction of all chunks.
  Fast enough to run after every backup as a spot-check.
- Full mode (``full=True``): checks every stored chunk.
  Suitable for periodic deep-inspection (weekly/monthly cron).

Corrupt chunks are *marked* but never automatically deleted; removal is
intentionally left to the operator so that no data is lost without review.
If a corrupt chunk is later re-uploaded by a new backup the ``corrupted_at``
field remains, but the new chunk data takes over (same hash_id means same
plaintext content; if the content changed the hash_id changes too, so dedup
creates a new row).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from sentinel.dpe.crypto import ChunkCipher
from sentinel.mcd.catalog import CatalogManager
from sentinel.spi.base import IStorageProvider

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Result type                                                         #
# ------------------------------------------------------------------ #

@dataclass
class ScrubResult:
    """Returned by ScrubEngine.run() regardless of outcome."""

    scrub_id: str
    job_id: str
    status: str                              # "success" | "failed" | "in_progress"

    # Counters
    chunks_checked: int = 0
    chunks_ok: int = 0
    chunks_corrupt: int = 0

    # Detail
    corrupt_chunk_ids: List[str] = field(default_factory=list)
    affected_snapshot_ids: List[str] = field(default_factory=list)

    duration_s: float = 0.0
    errors: List[str] = field(default_factory=list)


# ------------------------------------------------------------------ #
#  Engine                                                              #
# ------------------------------------------------------------------ #

class ScrubEngine:
    """
    Verifies the integrity of chunks stored in the SPI backend.

    Args:
        job_id:      Identifier of the job whose chunks are being checked.
        catalog:     Open :class:`~sentinel.mcd.catalog.CatalogManager`.
        storage:     Ready :class:`~sentinel.spi.base.IStorageProvider`.
        cipher:      Initialised :class:`~sentinel.dpe.crypto.ChunkCipher`
                     (must use the same key as was used during backup).
        on_progress: Optional callback called after each chunk with a dict:
                     ``{"chunks_checked": N, "chunks_ok": N, "chunks_corrupt": N}``.
    """

    def __init__(
        self,
        job_id: str,
        catalog: CatalogManager,
        storage: IStorageProvider,
        cipher: ChunkCipher,
        on_progress: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.job_id = job_id
        self.catalog = catalog
        self.storage = storage
        self._cipher = cipher
        self.on_progress = on_progress

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def run(self, sample: float = 0.01, full: bool = False) -> ScrubResult:
        """
        Execute the scrub and return a :class:`ScrubResult`.

        Args:
            sample: Fraction of chunks to verify in sample mode (0.0–1.0).
                    Ignored when *full* is True.
            full:   If True, verify every stored chunk regardless of *sample*.
        """
        scrub_id = str(uuid.uuid4())
        result = ScrubResult(
            scrub_id=scrub_id,
            job_id=self.job_id,
            status="in_progress",
        )
        t0 = time.monotonic()

        try:
            if full:
                rows = self.catalog.list_all_chunks()
                log.info("Scrub %s | FULL scan: %d chunks", scrub_id[:8], len(rows))
            else:
                rows = self.catalog.random_chunk_sample(percentage=sample)
                log.info(
                    "Scrub %s | sample=%.1f%% → %d chunks",
                    scrub_id[:8], sample * 100, len(rows),
                )

            for row in rows:
                self._verify_chunk(row["hash_id"], result)

            # Resolve which snapshots are affected by corrupt chunks
            if result.corrupt_chunk_ids:
                result.affected_snapshot_ids = self.catalog.get_snapshots_for_chunks(
                    result.corrupt_chunk_ids
                )
                log.warning(
                    "Scrub %s | %d corrupt chunk(s) affect %d snapshot(s): %s",
                    scrub_id[:8],
                    result.chunks_corrupt,
                    len(result.affected_snapshot_ids),
                    [s[:8] for s in result.affected_snapshot_ids],
                )

            result.status = "failed" if result.chunks_corrupt else "success"

        except Exception as exc:
            log.exception("Scrub %s raised an unexpected error", scrub_id[:8])
            result.errors.append(str(exc))
            result.status = "failed"

        finally:
            result.duration_s = time.monotonic() - t0
            log.info(
                "Scrub %s done | status=%s checked=%d ok=%d corrupt=%d %.1fs",
                scrub_id[:8],
                result.status,
                result.chunks_checked,
                result.chunks_ok,
                result.chunks_corrupt,
                result.duration_s,
            )

        return result

    # ------------------------------------------------------------------ #
    #  Per-chunk verification                                              #
    # ------------------------------------------------------------------ #

    def _verify_chunk(self, hash_id: str, result: ScrubResult) -> None:
        """Download and decrypt one chunk; update *result* in place."""
        result.chunks_checked += 1
        try:
            blob = self.storage.download_chunk(hash_id)
            self._cipher.decrypt(blob)   # raises on bad GCM tag or missing IV
            result.chunks_ok += 1
        except Exception as exc:
            log.error(
                "INTEGRITY ALARM: chunk %s… failed — %s",
                hash_id[:12], exc,
            )
            result.chunks_corrupt += 1
            result.corrupt_chunk_ids.append(hash_id)
            result.errors.append(f"chunk({hash_id[:12]}…): {exc}")
            # Persist the corruption flag in the catalog
            try:
                self.catalog.mark_chunk_corrupt(hash_id)
            except Exception as mark_exc:
                log.warning("Failed to mark chunk %s as corrupt: %s", hash_id[:12], mark_exc)

        if self.on_progress:
            try:
                self.on_progress({
                    "chunks_checked": result.chunks_checked,
                    "chunks_ok": result.chunks_ok,
                    "chunks_corrupt": result.chunks_corrupt,
                })
            except Exception:
                pass
