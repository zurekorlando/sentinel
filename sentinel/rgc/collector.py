"""
Retention & Garbage Collector (RGC) — Module 5.

GFS Retention Policy
--------------------
Grandfather-Father-Son (GFS) keeps:
  - Son:         the last 7 daily snapshots
  - Father:      the last 4 weekly snapshots (one per Sunday)
  - Grandfather: the last 12 monthly snapshots (one per 1st of month)

Any snapshot not selected by the above rules is marked for deletion.

Garbage Collection
------------------
Only physical chunk deletion happens when ref_count reaches 0 in the catalog.
The algorithm is:

  1. For each snapshot to prune: decrement ref_count of all its chunks.
  2. Delete snapshot (cascades to files / file_chunks).
  3. Collect all chunks with ref_count == 0.
  4. Delete them from the SPI backend.
  5. Delete their catalog rows.

This ensures no chunk is deleted while still referenced by another snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from sentinel.mcd.catalog import CatalogManager
from sentinel.spi.base import IStorageProvider

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  GFS Policy                                                          #
# ------------------------------------------------------------------ #

@dataclass
class RetentionPolicy:
    """
    Defines how many snapshots to keep at each GFS tier.

    Attributes:
        daily:   Number of recent daily snapshots to preserve.
        weekly:  Number of recent weekly (Sunday) snapshots to preserve.
        monthly: Number of recent monthly (1st-of-month) snapshots to preserve.
    """
    daily: int = 7
    weekly: int = 4
    monthly: int = 12


def _parse_dt(ts: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp from the catalog."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def select_snapshots_to_prune(
    snapshots: list,          # list of sqlite3.Row with snapshot_id, started_at
    policy: RetentionPolicy,
) -> List[str]:
    """
    Apply GFS retention rules and return the snapshot_ids that should be pruned.

    Only considers snapshots with status == 'completed'.

    Algorithm:
      - Sort snapshots from newest to oldest.
      - Walk through them and tag each one as a "keeper" for:
          daily:   first <policy.daily> unique calendar-days seen
          weekly:  first <policy.weekly> unique ISO-week-years seen (Sun preferred)
          monthly: first <policy.monthly> unique year-months seen
      - Everything not tagged is scheduled for pruning.
    """
    completed = [
        s for s in snapshots if s["status"] == "completed"
    ]
    completed.sort(key=lambda s: s["started_at"], reverse=True)  # newest first

    kept_ids: set[str] = set()
    seen_days: set[str] = set()
    seen_weeks: set[str] = set()
    seen_months: set[str] = set()

    for snap in completed:
        dt = _parse_dt(snap["started_at"])
        sid = snap["snapshot_id"]

        day_key = dt.strftime("%Y-%m-%d")
        week_key = dt.strftime("%G-W%V")         # ISO week year + week number
        month_key = dt.strftime("%Y-%m")

        keep = False

        if len(seen_days) < policy.daily and day_key not in seen_days:
            seen_days.add(day_key)
            keep = True

        if len(seen_weeks) < policy.weekly and week_key not in seen_weeks:
            seen_weeks.add(week_key)
            keep = True

        if len(seen_months) < policy.monthly and month_key not in seen_months:
            seen_months.add(month_key)
            keep = True

        if keep:
            kept_ids.add(sid)

    return [s["snapshot_id"] for s in completed if s["snapshot_id"] not in kept_ids]


# ------------------------------------------------------------------ #
#  Garbage Collector                                                   #
# ------------------------------------------------------------------ #

@dataclass
class GCResult:
    """Summary of a garbage-collection run."""
    snapshots_pruned: int = 0
    chunks_deleted: int = 0
    bytes_freed: int = 0
    errors: List[str] = field(default_factory=list)


class GarbageCollector:
    """
    Orchestrates snapshot pruning and physical chunk deletion.

    Args:
        catalog: The :class:`~sentinel.mcd.catalog.CatalogManager` instance.
        storage: The :class:`~sentinel.spi.base.IStorageProvider` to delete from.
        policy:  GFS retention policy (defaults to 7D/4W/12M).
    """

    def __init__(
        self,
        catalog: CatalogManager,
        storage: IStorageProvider,
        policy: Optional[RetentionPolicy] = None,
    ) -> None:
        self.catalog = catalog
        self.storage = storage
        self.policy = policy or RetentionPolicy()

    def run(self, job_id: Optional[str] = None, dry_run: bool = False) -> GCResult:
        """
        Execute the full GC cycle for *job_id* (or all jobs if None).

        Steps:
          1. Identify snapshots to prune using GFS policy.
          2. Decrement ref_counts of their chunks.
          3. Delete snapshot rows (cascades in catalog).
          4. Collect orphaned chunks (ref_count == 0).
          5. Delete orphaned chunks from SPI then from catalog.

        Args:
            job_id:  Scope GC to a single job.  None → all jobs.
            dry_run: If True, log actions but do not modify anything.

        Returns:
            :class:`GCResult` with counts and any non-fatal errors.
        """
        result = GCResult()

        # Step 1 — Which snapshots to prune?
        all_snapshots = self.catalog.list_snapshots(job_id)
        to_prune = select_snapshots_to_prune(all_snapshots, self.policy)

        if not to_prune:
            log.info("GC: no snapshots to prune.")
            return result

        log.info("GC: %d snapshot(s) scheduled for pruning.", len(to_prune))

        for snap_id in to_prune:
            log.info("GC: pruning snapshot %s", snap_id)

            # Step 2 — Decrement ref_counts
            chunk_hashes = self.catalog.get_snapshot_chunk_hashes(snap_id)
            if not dry_run:
                for hash_id in chunk_hashes:
                    self.catalog.decrement_ref(hash_id)

                # Step 3 — Delete snapshot (cascades files + file_chunks)
                self.catalog.delete_snapshot(snap_id)

            result.snapshots_pruned += 1

        if dry_run:
            log.info("GC dry-run complete; no changes written.")
            return result

        # Step 4 — Collect orphaned chunks
        orphans = self.catalog.get_orphan_chunks()
        log.info("GC: %d orphaned chunk(s) to delete.", len(orphans))

        for hash_id in orphans:
            chunk_row = self.catalog.get_chunk(hash_id)
            stored_bytes = len(hash_id)  # fallback

            # Step 5a — Delete from SPI
            try:
                self.storage.delete_chunk(hash_id)
                if chunk_row:
                    stored_bytes = chunk_row["compressed_size"]
            except Exception as exc:
                err = f"Failed to delete chunk {hash_id[:8]}…: {exc}"
                log.error("GC: %s", err)
                result.errors.append(err)
                continue  # Leave catalog row so next GC can retry.

            # Step 5b — Delete from catalog
            self.catalog.delete_chunk_record(hash_id)
            result.chunks_deleted += 1
            result.bytes_freed += stored_bytes

        log.info(
            "GC complete: %d snapshots pruned, %d chunks deleted, %s freed.",
            result.snapshots_pruned,
            result.chunks_deleted,
            _fmt_bytes(result.bytes_freed),
        )
        return result


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} PB"
