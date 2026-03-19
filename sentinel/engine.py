"""
BackupEngine — central orchestrator.

Full pipeline (Sprint 3 — with SSM + CBT):
  SSM creates snapshot → CBT filters changed files → DPE chunks/encrypts
  → SPI uploads → MCD records → M&M notifies via webhook

Flow per source path
---------------------
1.  ``SnapshotFactory`` selects the best available provider (VSS/LVM/Btrfs/Direct).
2.  A snapshot is created; the BackupEngine walks files through its mount_point.
3.  ``FileLevelCBT`` filters the walk: only files changed since the last backup
    are passed to the DPE pipeline.
4.  Each changed file is chunked (Rabin), deduped (SHA-256 catalog lookup),
    compressed (Zstd), encrypted (AES-256-GCM), and uploaded (SPI).
5.  After a successful upload the file is marked clean in CBT.
6.  On completion: CBT state is saved, snapshot is released, catalog is
    encrypted and pushed to the storage backend.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, List, Optional

import requests

from sentinel.dpe.chunker import RabinChunker
from sentinel.dpe.crypto import ChunkCipher, derive_key
from sentinel.dpe.pipeline import ProcessingPipeline
from sentinel.mcd.catalog import CatalogManager
from sentinel.spi.base import IStorageProvider
from sentinel.ssm.base import ISourceManager, PrePostHooks
from sentinel.ssm.cbt import FileLevelCBT
from sentinel.ssm.factory import SnapshotFactory

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Result types                                                         #
# ------------------------------------------------------------------ #

@dataclass
class BackupResult:
    """Summary returned after every backup run, success or failure."""
    snapshot_id: str
    job_id: str
    status: str                       # "success" | "failed"
    files_processed: int = 0
    files_skipped_cbt: int = 0        # unchanged files skipped by CBT
    chunks_uploaded: int = 0
    chunks_deduped: int = 0
    bytes_original: int = 0
    bytes_stored: int = 0
    duration_s: float = 0.0
    provider_used: str = "unknown"    # vss | lvm | btrfs | direct
    errors: List[str] = field(default_factory=list)

    @property
    def dedup_ratio(self) -> float:
        total = self.chunks_uploaded + self.chunks_deduped
        return self.chunks_deduped / total if total else 0.0

    @property
    def compression_ratio(self) -> float:
        return self.bytes_original / self.bytes_stored if self.bytes_stored else 1.0


# ------------------------------------------------------------------ #
#  Engine                                                              #
# ------------------------------------------------------------------ #

class BackupEngine:
    """
    Orchestrates a single backup job.

    Args:
        job_id:          Unique identifier for this backup job configuration.
        catalog:         A ready :class:`~sentinel.mcd.catalog.CatalogManager`.
        storage:         A ready :class:`~sentinel.spi.base.IStorageProvider`.
        passphrase:      User passphrase for AES-256-GCM key derivation.
        salt:            Existing Argon2id salt bytes, or None on first run.
        source_paths:    List of root paths (directories or volumes) to back up.
        exclusions:      Glob patterns to exclude (e.g. ``["**/.git", "*.tmp"]``).
        max_workers:     DPE parallel processing threads (default 4).
        webhook_url:     Optional URL to POST JSON status events to.
        source_manager:  Override the auto-detected SSM provider.
        cbt_state_path:  Path to the CBT state JSON file.  Defaults to a file
                         adjacent to the catalog DB.
        hooks:           Optional pre-freeze / post-thaw shell commands for
                         application-consistent snapshots.
        snapshot_provider: Force a specific provider name: ``"vss"``,
                           ``"lvm"``, ``"btrfs"``, or ``"direct"``.
    """

    def __init__(
        self,
        job_id: str,
        catalog: CatalogManager,
        storage: IStorageProvider,
        passphrase: str,
        salt: Optional[bytes] = None,
        source_paths: Optional[List[str]] = None,
        exclusions: Optional[List[str]] = None,
        max_workers: int = 4,
        webhook_url: Optional[str] = None,
        source_manager: Optional[ISourceManager] = None,
        cbt_state_path: Optional[str] = None,
        hooks: Optional[PrePostHooks] = None,
        snapshot_provider: Optional[str] = None,
        on_progress: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.job_id = job_id
        self.catalog = catalog
        self.storage = storage
        # agent:// URLs must stay as raw strings — Path() collapses // to /
        self.source_paths = [
            p if p.startswith("agent://") else Path(p)
            for p in (source_paths or [])
        ]
        self.exclusions = exclusions or []
        self.max_workers = max_workers
        self.webhook_url = webhook_url
        self.hooks = hooks
        self.on_progress = on_progress

        # SSM — snapshot provider
        self._source_manager: ISourceManager = source_manager or SnapshotFactory.create(
            preferred=snapshot_provider
        )

        # CBT — change tracking state file
        if cbt_state_path:
            cbt_path = Path(cbt_state_path)
        else:
            cbt_path = Path(catalog.db_path).with_suffix(".cbt.json")
        self._cbt = FileLevelCBT(str(cbt_path))

        # DPE — encryption pipeline
        derived = derive_key(passphrase, salt)
        self._key_salt = derived.salt
        cipher = ChunkCipher(derived.key)
        self._chunker = RabinChunker()
        self._pipeline = ProcessingPipeline(cipher=cipher, compression_level=3)

    @property
    def key_salt(self) -> bytes:
        """Argon2id salt — persist in job config for future restores."""
        return self._key_salt

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def run_backup(self) -> BackupResult:
        """
        Execute a full backup job with SSM snapshot + CBT filtering.

        Returns :class:`BackupResult` regardless of success or failure.
        """
        snapshot_id = str(uuid.uuid4())
        result = BackupResult(
            snapshot_id=snapshot_id,
            job_id=self.job_id,
            status="in_progress",
        )

        self._notify("JOB_START", result)
        self.catalog.create_snapshot(snapshot_id, self.job_id)
        t0 = time.monotonic()

        try:
            for source_root in self.source_paths:
                if isinstance(source_root, str) and source_root.startswith("agent://"):
                    self._backup_agent_volume(source_root, snapshot_id, result)
                else:
                    self._backup_volume(source_root, snapshot_id, result)

            self.catalog.finalize_snapshot(
                snapshot_id,
                total_bytes=result.bytes_original,
                chunk_count=result.chunks_uploaded + result.chunks_deduped,
                status="completed",
            )
            self._cbt.save()
            self._upload_catalog_backup(snapshot_id)
            result.status = "success"
            self._notify("JOB_SUCCESS", result)

        except Exception as exc:
            log.exception("Backup job %s failed", self.job_id)
            result.errors.append(str(exc))
            result.status = "failed"
            self.catalog.fail_snapshot(snapshot_id)
            self._notify("JOB_FAILED", result)

        finally:
            result.duration_s = time.monotonic() - t0
            log.info(
                "Backup %s | provider=%s status=%s files=%d skipped=%d "
                "uploaded=%d deduped=%d dedup=%.0f%% %.1fs",
                snapshot_id[:8],
                result.provider_used,
                result.status,
                result.files_processed,
                result.files_skipped_cbt,
                result.chunks_uploaded,
                result.chunks_deduped,
                result.dedup_ratio * 100,
                result.duration_s,
            )

        return result

    # ------------------------------------------------------------------ #
    #  Per-volume flow                                                     #
    # ------------------------------------------------------------------ #

    def _backup_volume(
        self,
        source_root: Path,
        snapshot_id: str,
        result: BackupResult,
    ) -> None:
        """
        Create a snapshot of *source_root* and process all changed files.

        The SSM context manager guarantees the snapshot is deleted on exit
        even if an exception propagates.
        """
        volume = str(source_root)
        log.info(
            "Starting snapshot for %s using %s",
            volume,
            self._source_manager.__class__.__name__,
        )

        with self._source_manager.create_snapshot(volume, self.hooks) as snap:
            result.provider_used = snap.provider
            log.info(
                "Snapshot ready | id=%s provider=%s mount=%s app_consistent=%s",
                snap.snapshot_id,
                snap.provider,
                snap.mount_point,
                snap.app_consistent,
            )

            for file_path in self._walk_changed(snap.mount_point):
                self._backup_file(
                    file_path=file_path,
                    # Translate snapshot mount path back to original source path
                    # so the catalog stores the real path, not the shadow copy path.
                    original_path=self._translate_path(
                        file_path, snap.mount_point, source_root
                    ),
                    snapshot_id=snapshot_id,
                    result=result,
                )

            # Remove CBT entries for files that were deleted since last backup
            deleted = self._cbt.purge_missing(snap.mount_point)
            if deleted:
                log.info("CBT: purged %d deleted file records", deleted)

    # ------------------------------------------------------------------ #
    #  Agent (remote Windows) volume backup                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_agent_url(url: str) -> tuple[str, int, str]:
        """
        Parse an agent:// URL into (host, port, volume).

        Examples:
            "agent://192.168.1.43:7700/C:" → ("192.168.1.43", 7700, "C:")
            "agent://192.168.1.43/C:"      → ("192.168.1.43", 7700, "C:")
        """
        # Strip scheme
        rest = url[len("agent://"):]
        # Split host[:port] from /volume
        slash = rest.find("/")
        if slash == -1:
            raise ValueError(f"Invalid agent URL (missing volume): {url!r}")
        host_part = rest[:slash]
        volume = rest[slash + 1:]  # e.g. "C:"
        if ":" in host_part:
            host, port_str = host_part.rsplit(":", 1)
            port = int(port_str)
        else:
            host = host_part
            port = 7700
        return host, port, volume

    def _backup_agent_volume(
        self,
        agent_url: str,
        snapshot_id: str,
        result: BackupResult,
    ) -> None:
        """
        Backup a remote Windows volume via the Sentinel agent.

        Creates a remote VSS snapshot, iterates files via the agent's
        streaming API, and processes each changed file.
        """
        from sentinel.ssm.windows.remote_provider import RemoteWindowsProvider

        host, port, volume = self._parse_agent_url(agent_url)
        provider = RemoteWindowsProvider(host=host, port=port)

        log.info("Starting agent backup: %s (host=%s port=%d volume=%s)",
                 agent_url, host, port, volume)

        with provider.create_snapshot(volume, self.hooks) as snap:
            result.provider_used = snap.provider
            client = snap.metadata["client"]
            remote_snapshot_id = snap.metadata["remote_snapshot_id"]

            log.info(
                "Agent snapshot ready | id=%s host=%s volume=%s",
                remote_snapshot_id, host, volume,
            )

            for file_info in client.list_files(remote_snapshot_id):
                path_key = file_info.path
                # Apply exclusion patterns
                if self._is_excluded_str(path_key):
                    continue
                # CBT check using remote metadata
                if not self._cbt.is_changed_remote(path_key, file_info.size, file_info.mtime):
                    result.files_skipped_cbt += 1
                    continue
                self._backup_remote_file(
                    client=client,
                    remote_snapshot_id=remote_snapshot_id,
                    file_info=file_info,
                    snapshot_id=snapshot_id,
                    result=result,
                )

    def _backup_remote_file(
        self,
        client,
        remote_snapshot_id: str,
        file_info,
        snapshot_id: str,
        result: BackupResult,
    ) -> None:
        """Process a single remote file from the agent's streaming API."""
        path_key = file_info.path
        chunk_hashes: List[str] = []

        try:
            raw_chunks = list(
                self._chunker.chunkify_stream(
                    _RemoteFileStream(client.read_file(remote_snapshot_id, path_key))
                )
            )
        except Exception as exc:
            log.warning("Cannot read remote file %s: %s", path_key, exc)
            result.errors.append(f"read({path_key}): {exc}")
            return

        processed = list(
            self._pipeline.process_stream(iter(raw_chunks), self.max_workers)
        )

        for proc in processed:
            result.bytes_original += proc.original_size
            if self.catalog.chunk_exists(proc.hash_id):
                self.catalog.increment_ref(proc.hash_id)
                result.chunks_deduped += 1
            else:
                self.storage.upload_chunk(proc.blob, proc.hash_id)
                self.catalog.add_chunk(
                    hash_id=proc.hash_id,
                    storage_key=proc.hash_id,
                    original_size=proc.original_size,
                    compressed_size=proc.compressed_size,
                    iv=proc.iv,
                )
                result.chunks_uploaded += 1
                result.bytes_stored += proc.stored_size
            chunk_hashes.append(proc.hash_id)

        file_id = str(uuid.uuid4())
        self.catalog.add_file(
            file_id=file_id,
            snapshot_id=snapshot_id,
            path=path_key,
            size=file_info.size,
            mtime=file_info.mtime,
            mode=0,  # no mode for remote Windows files
            chunk_hashes=chunk_hashes,
        )

        self._cbt.mark_clean_remote(path_key, file_info.size, file_info.mtime, snapshot_id)
        result.files_processed += 1

        if self.on_progress:
            try:
                self.on_progress({
                    "event": "FILE_DONE",
                    "file": path_key.split("\\")[-1],
                    "files_processed": result.files_processed,
                    "files_skipped_cbt": result.files_skipped_cbt,
                    "chunks_uploaded": result.chunks_uploaded,
                    "chunks_deduped": result.chunks_deduped,
                    "bytes_original": result.bytes_original,
                })
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Walk + CBT                                                          #
    # ------------------------------------------------------------------ #

    def _walk_changed(self, root: Path) -> Iterator[Path]:
        """
        Walk *root* and yield only files that CBT identifies as changed.

        Excludes symlinks and any path matching the job's exclusion patterns.
        Directories are pruned in-place to avoid descending into .git, etc.
        """
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            dirnames[:] = [
                d for d in dirnames
                if not self._is_excluded(Path(dirpath) / d)
            ]
            for fname in sorted(filenames):
                fpath = Path(dirpath) / fname
                if self._is_excluded(fpath) or fpath.is_symlink():
                    continue
                if self._cbt.is_changed(fpath):
                    yield fpath

    def _is_excluded(self, path: Path) -> bool:
        return self._is_excluded_str(str(path), path.name)

    def _is_excluded_str(self, path_str: str, name: Optional[str] = None) -> bool:
        if name is None:
            name = path_str.replace("\\", "/").rstrip("/").split("/")[-1]
        for pat in self.exclusions:
            if fnmatch.fnmatch(name, pat.lstrip("**/")) or fnmatch.fnmatch(path_str, pat):
                return True
        return False

    # ------------------------------------------------------------------ #
    #  Per-file processing                                                 #
    # ------------------------------------------------------------------ #

    def _backup_file(
        self,
        file_path: Path,       # path inside the snapshot mount
        original_path: Path,   # path to record in the catalog
        snapshot_id: str,
        result: BackupResult,
    ) -> None:
        try:
            stat = file_path.stat()
        except OSError as exc:
            log.warning("Skipping %s: %s", file_path, exc)
            result.errors.append(f"stat({file_path}): {exc}")
            return

        chunk_hashes: List[str] = []

        try:
            with open(file_path, "rb") as fh:
                raw_chunks = list(self._chunker.chunkify_stream(fh))
        except OSError as exc:
            log.warning("Cannot read %s: %s", file_path, exc)
            result.errors.append(f"read({file_path}): {exc}")
            return

        processed = list(
            self._pipeline.process_stream(iter(raw_chunks), self.max_workers)
        )

        for proc in processed:
            result.bytes_original += proc.original_size

            if self.catalog.chunk_exists(proc.hash_id):
                self.catalog.increment_ref(proc.hash_id)
                result.chunks_deduped += 1
            else:
                self.storage.upload_chunk(proc.blob, proc.hash_id)
                self.catalog.add_chunk(
                    hash_id=proc.hash_id,
                    storage_key=proc.hash_id,
                    original_size=proc.original_size,
                    compressed_size=proc.compressed_size,
                    iv=proc.iv,
                )
                result.chunks_uploaded += 1
                result.bytes_stored += proc.stored_size

            chunk_hashes.append(proc.hash_id)

        file_id = str(uuid.uuid4())
        self.catalog.add_file(
            file_id=file_id,
            snapshot_id=snapshot_id,
            path=str(original_path),
            size=stat.st_size,
            mtime=stat.st_mtime,
            mode=stat.st_mode,
            chunk_hashes=chunk_hashes,
        )

        # Mark file clean in CBT (use the snapshot path for stat consistency)
        self._cbt.mark_clean(file_path, snapshot_id)
        result.files_processed += 1

        if self.on_progress:
            try:
                self.on_progress({
                    "event": "FILE_DONE",
                    "file": original_path.name,
                    "files_processed": result.files_processed,
                    "files_skipped_cbt": result.files_skipped_cbt,
                    "chunks_uploaded": result.chunks_uploaded,
                    "chunks_deduped": result.chunks_deduped,
                    "bytes_original": result.bytes_original,
                })
            except Exception:
                pass  # never let a callback error abort the backup

        log.debug(
            "✓ %s  chunks=%d deduped=%d",
            original_path.name,
            len(chunk_hashes),
            sum(1 for h in chunk_hashes if self.catalog.chunk_exists(h)),
        )

    # ------------------------------------------------------------------ #
    #  Utilities                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _translate_path(
        snapshot_file: Path,
        mount_point: Path,
        source_root: Path,
    ) -> Path:
        """
        Convert a path inside the snapshot mount back to the original source path.

        Example:
            snapshot_file = /tmp/sentinel_vss_abc123/Users/alice/doc.txt
            mount_point   = /tmp/sentinel_vss_abc123
            source_root   = C:\\
            → C:\\Users\\alice\\doc.txt

        For the DirectProvider (mount_point == source_root), this is a no-op.
        """
        try:
            relative = snapshot_file.relative_to(mount_point)
            return source_root / relative
        except ValueError:
            return snapshot_file

    def _upload_catalog_backup(self, snapshot_id: str) -> None:
        """Encrypt and push the SQLite catalog to the storage backend."""
        try:
            with open(self.catalog.db_path, "rb") as fh:
                db_bytes = fh.read()
            blob = self._pipeline._cipher.encrypt(db_bytes)
            name = f"catalog-{snapshot_id}.db.enc"
            self.storage.upload_catalog(blob.data, name)
            log.info("Catalog backup uploaded: %s", name)
        except Exception as exc:
            log.warning("Catalog backup upload failed: %s", exc)

    def _notify(self, event: str, result: BackupResult) -> None:
        if not self.webhook_url:
            return
        payload = {
            "event": event,
            "snapshot_id": result.snapshot_id,
            "job_id": result.job_id,
            "status": result.status,
            "provider": result.provider_used,
            "files_processed": result.files_processed,
            "files_skipped_cbt": result.files_skipped_cbt,
            "chunks_uploaded": result.chunks_uploaded,
            "chunks_deduped": result.chunks_deduped,
            "bytes_original": result.bytes_original,
            "bytes_stored": result.bytes_stored,
        }
        try:
            requests.post(self.webhook_url, json=payload, timeout=10)
        except Exception as exc:
            log.warning("Webhook delivery failed for %s: %s", event, exc)


# ------------------------------------------------------------------ #
#  Restore types + engine                                              #
# ------------------------------------------------------------------ #

@dataclass
class RestoreResult:
    """Summary returned after a restore operation, success or failure."""
    restore_id: str
    snapshot_id: str
    status: str                       # "success" | "failed"
    files_total: int = 0
    files_restored: int = 0
    bytes_restored: int = 0
    errors: List[str] = field(default_factory=list)


class RestoreEngine:
    """
    Reconstructs files from a snapshot by reversing the DPE pipeline.

    Restore pipeline per chunk:
        SPI download → AES-256-GCM decrypt → Zstd decompress → write

    Args:
        catalog:          A :class:`~sentinel.mcd.catalog.CatalogManager`
                          pointing to the job's SQLite catalog.
        storage:          A ready :class:`~sentinel.spi.base.IStorageProvider`.
        passphrase:       User passphrase for AES-256-GCM key derivation.
        salt:             Argon2id salt (from ``key_salt_hex`` in job config).
                          Must match the salt used during backup.
        destination_path: Directory where restored files will be written.
        overwrite:        If ``False`` (default) existing files are skipped.
        on_progress:      Optional callback called after each restored file
                          with ``{"files_restored": N, "bytes_restored": N}``.
    """

    def __init__(
        self,
        catalog: CatalogManager,
        storage: IStorageProvider,
        passphrase: str,
        salt: bytes,
        destination_path: str,
        overwrite: bool = False,
        on_progress: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.catalog = catalog
        self.storage = storage
        self.destination = Path(destination_path)
        self.overwrite = overwrite
        self.on_progress = on_progress

        from sentinel.dpe.crypto import ChunkCipher, derive_key
        from sentinel.dpe.pipeline import ProcessingPipeline

        derived = derive_key(passphrase, salt)
        cipher = ChunkCipher(derived.key)
        self._pipeline = ProcessingPipeline(cipher=cipher)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def restore_snapshot(
        self,
        snapshot_id: str,
        file_paths: Optional[List[str]] = None,
    ) -> RestoreResult:
        """
        Restore all (or a subset of) files from *snapshot_id*.

        Args:
            snapshot_id: The snapshot to restore from.
            file_paths:  Specific file paths to restore.  ``None`` restores
                         all files in the snapshot.

        Returns:
            :class:`RestoreResult` regardless of success or failure.
        """
        restore_id = str(uuid.uuid4())
        result = RestoreResult(
            restore_id=restore_id,
            snapshot_id=snapshot_id,
            status="in_progress",
        )

        all_files = self.catalog.get_snapshot_files(snapshot_id)
        if file_paths is not None:
            path_set = set(file_paths)
            all_files = [f for f in all_files if f["path"] in path_set]

        result.files_total = len(all_files)
        self.destination.mkdir(parents=True, exist_ok=True)

        try:
            for file_row in all_files:
                self._restore_file(file_row, result)
            result.status = "success"
        except Exception as exc:
            log.exception("Restore %s failed", restore_id[:8])
            result.errors.append(str(exc))
            result.status = "failed"

        log.info(
            "Restore %s | snapshot=%s status=%s files=%d/%d bytes=%d",
            restore_id[:8],
            snapshot_id[:8],
            result.status,
            result.files_restored,
            result.files_total,
            result.bytes_restored,
        )
        return result

    # ------------------------------------------------------------------ #
    #  Per-file restore                                                    #
    # ------------------------------------------------------------------ #

    def _restore_file(self, file_row, result: RestoreResult) -> None:
        src_path = file_row["path"]
        # Compute destination path: strip leading slash then place under dest dir
        relative = src_path.lstrip("/").lstrip("\\")
        dest = self.destination / relative

        if dest.exists() and not self.overwrite:
            log.debug("Skipping existing file: %s", dest)
            return

        try:
            chunks = self.catalog.get_file_chunks(file_row["file_id"])
        except Exception as exc:
            result.errors.append(f"catalog({src_path}): {exc}")
            return

        raw_parts: List[bytes] = []
        for chunk_row in chunks:
            try:
                blob = self.storage.download_chunk(chunk_row["hash_id"])
                raw_data = self._pipeline.decrypt_chunk(blob)
                raw_parts.append(raw_data)
            except Exception as exc:
                result.errors.append(f"chunk({chunk_row['hash_id'][:8]}…): {exc}")
                return

        dest.parent.mkdir(parents=True, exist_ok=True)
        assembled = b"".join(raw_parts)
        dest.write_bytes(assembled)

        # Restore mtime if available
        if file_row["mtime"]:
            try:
                os.utime(dest, (file_row["mtime"], file_row["mtime"]))
            except OSError:
                pass

        result.files_restored += 1
        result.bytes_restored += len(assembled)

        if self.on_progress:
            try:
                self.on_progress({
                    "files_restored": result.files_restored,
                    "files_total": result.files_total,
                    "bytes_restored": result.bytes_restored,
                })
            except Exception:
                pass


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

class _RemoteFileStream:
    """
    Wraps an Iterator[bytes] from AgentClient.read_file() as a file-like
    object with a read() method so RabinChunker.chunkify_stream() can
    consume it the same way it would a real file handle.
    """

    def __init__(self, chunks_iter) -> None:
        self._iter = chunks_iter
        self._buf = b""

    def read(self, size: int = -1) -> bytes:
        if size == -1:
            return self._buf + b"".join(self._iter)
        while len(self._buf) < size:
            try:
                self._buf += next(self._iter)
            except StopIteration:
                break
        out, self._buf = self._buf[:size], self._buf[size:]
        return out
