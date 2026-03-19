"""
Change Block Tracking (CBT) — SSM sub-module.

Two-tier strategy
-----------------
Tier 1 — File-level CBT (always available)
    Compares (mtime, size, inode) against a persistent JSON state file.
    On first run every file is "dirty".  After each backup, the state is
    updated so only genuinely changed files are processed next time.
    This is the primary approach used by Restic, BorgBackup, and Duplicacy.

Tier 2 — NTFS USN Change Journal (Windows, optional)
    The NTFS Change Journal records every file-system operation since the
    last read.  Querying it is faster than stat()-ing every file on large
    volumes.  Requires admin privileges and the journal to be enabled on
    the volume (enabled by default on all modern Windows installations).
    Used as an *accelerator* on top of Tier 1: if the journal says a file
    did NOT change, we skip the stat() call entirely.

    Implementation uses DeviceIoControl with:
      FSCTL_QUERY_USN_JOURNAL   → get current journal parameters
      FSCTL_READ_USN_JOURNAL    → enumerate records since last_usn

Spec fallback rule
------------------
If CBT state is unavailable or corrupted, a full scan (all files dirty)
is performed automatically.

Thread-safety
-------------
``FileLevelCBT`` is NOT thread-safe for concurrent writes.
Call ``save()`` from a single thread after processing all files.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional, Set

log = logging.getLogger(__name__)

_STATE_VERSION = 2


# ------------------------------------------------------------------ #
#  File record                                                         #
# ------------------------------------------------------------------ #

@dataclass
class FileRecord:
    """Stored metadata for a single file."""
    mtime: float
    size: int
    inode: int
    last_snapshot_id: str


# ------------------------------------------------------------------ #
#  File-level CBT                                                      #
# ------------------------------------------------------------------ #

class FileLevelCBT:
    """
    Persistent file-level Change Block Tracking.

    The state is stored as a compact JSON file alongside the catalog DB.

    Usage::

        cbt = FileLevelCBT("sentinel_cbt.json")

        # Walk files and yield only changed ones
        for path in source_root.rglob("*"):
            if path.is_file() and cbt.is_changed(path):
                process(path)
                cbt.mark_clean(path, snapshot_id)

        cbt.save()          # persist state after the job
        cbt.purge_missing() # remove records for deleted files
    """

    def __init__(self, state_path: str | Path) -> None:
        self.state_path = Path(state_path)
        self._entries: Dict[str, FileRecord] = {}
        self._dirty = False
        self._load()

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if not self.state_path.exists():
            log.debug("CBT: no existing state at %s — full scan on first run", self.state_path)
            return
        try:
            with self.state_path.open() as fh:
                data = json.load(fh)
            if data.get("version") != _STATE_VERSION:
                log.warning("CBT: state version mismatch — discarding old state")
                return
            for path_str, rec in data.get("entries", {}).items():
                self._entries[path_str] = FileRecord(**rec)
            log.debug("CBT: loaded %d file records", len(self._entries))
        except Exception as exc:
            log.warning("CBT: failed to load state (%s) — full scan will run", exc)
            self._entries = {}

    def save(self) -> None:
        """Atomically persist the current state to disk."""
        data = {
            "version": _STATE_VERSION,
            "entries": {
                path: asdict(rec)
                for path, rec in self._entries.items()
            },
        }
        tmp = self.state_path.with_suffix(".tmp")
        try:
            with tmp.open("w") as fh:
                json.dump(data, fh, separators=(",", ":"))
            tmp.replace(self.state_path)
            self._dirty = False
            log.debug("CBT: state saved (%d entries)", len(self._entries))
        except Exception as exc:
            log.error("CBT: failed to save state: %s", exc)
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    #  Change detection                                                    #
    # ------------------------------------------------------------------ #

    def is_changed(self, file_path: Path) -> bool:
        """
        Return True if *file_path* has changed since the last backup.

        A file is considered changed when:
        - It has no record in the CBT state (new file), OR
        - mtime, size, or inode differ from the stored values.

        stat() failures (e.g. permission denied) are treated as changed
        so the file will be attempted (and fail gracefully) in the engine.
        """
        try:
            st = file_path.stat()
        except OSError:
            return True

        rec = self._entries.get(str(file_path))
        if rec is None:
            return True

        return (
            rec.mtime != st.st_mtime
            or rec.size != st.st_size
            or rec.inode != st.st_ino
        )

    def mark_clean(self, file_path: Path, snapshot_id: str) -> None:
        """
        Record the current state of *file_path* as clean (backed up).

        Call this after the file has been successfully processed and all
        its chunks recorded in the catalog.
        """
        try:
            st = file_path.stat()
        except OSError:
            return
        self._entries[str(file_path)] = FileRecord(
            mtime=st.st_mtime,
            size=st.st_size,
            inode=st.st_ino,
            last_snapshot_id=snapshot_id,
        )
        self._dirty = True

    def is_changed_remote(self, path_key: str, size: int, mtime: float) -> bool:
        """
        Return True if a *remote* file has changed since the last backup.

        Uses path string as key (no local stat() call).

        Args:
            path_key: Canonical remote path string (e.g. "C:\\Users\\alice\\doc.txt").
            size:     File size in bytes reported by the remote agent.
            mtime:    File modification timestamp (Unix epoch float) from agent.
        """
        rec = self._entries.get(path_key)
        if rec is None:
            return True
        return rec.mtime != mtime or rec.size != size

    def mark_clean_remote(self, path_key: str, size: int, mtime: float, snapshot_id: str) -> None:
        """
        Record a remote file as clean after a successful backup.

        Args:
            path_key:    Canonical remote path string.
            size:        File size in bytes.
            mtime:       Modification timestamp (Unix epoch float).
            snapshot_id: The snapshot UUID being recorded.
        """
        self._entries[path_key] = FileRecord(
            mtime=mtime,
            size=size,
            inode=0,  # no inode for remote files
            last_snapshot_id=snapshot_id,
        )
        self._dirty = True

    def purge_missing(self, root: Optional[Path] = None) -> int:
        """
        Remove entries for files that no longer exist under *root*.

        Args:
            root: If provided, only remove entries under this root path.
                  If None, check all entries.

        Returns:
            Number of entries removed.
        """
        if root is not None:
            root_str = str(root)
            candidates = [p for p in self._entries if p.startswith(root_str)]
        else:
            candidates = list(self._entries.keys())

        removed = 0
        for path_str in candidates:
            if not Path(path_str).exists():
                del self._entries[path_str]
                removed += 1

        if removed:
            self._dirty = True
            log.debug("CBT: purged %d missing file records", removed)
        return removed

    def changed_files(self, root: Path) -> Iterator[Path]:
        """
        Yield all changed files under *root* using file-level CBT.

        Skips symlinks and zero-byte files that are likely pipes/sockets.
        """
        for dirpath, dirnames, filenames in os.walk(root):
            # Sort for deterministic ordering
            dirnames.sort()
            for fname in sorted(filenames):
                fpath = Path(dirpath) / fname
                if fpath.is_symlink():
                    continue
                if self.is_changed(fpath):
                    yield fpath

    def stats(self) -> dict:
        """Return a summary dict for logging/monitoring."""
        return {
            "total_tracked": len(self._entries),
            "state_path": str(self.state_path),
            "dirty": self._dirty,
        }


# ------------------------------------------------------------------ #
#  NTFS USN Journal (Windows accelerator)                              #
# ------------------------------------------------------------------ #

class NTFSChangeJournal:
    """
    Reads the NTFS USN (Update Sequence Number) Change Journal to get a
    fast list of files modified since the last backup.

    This is an *accelerator* used alongside ``FileLevelCBT``:  it quickly
    rules out unchanged files so we skip the stat() call entirely on large
    volumes with millions of files.

    Requires:
      - Windows only
      - Admin privileges (SeBackupPrivilege or running as Administrator)
      - NTFS volume (FAT32/exFAT not supported)

    Usage::

        journal = NTFSChangeJournal("C:")
        if journal.is_available():
            changed_refs = journal.get_changed_since(last_usn)
            # Filter CBT walk to only these file refs
    """

    # IOCTL codes (from winioctl.h)
    _FSCTL_QUERY_USN_JOURNAL = 0x000900F4
    _FSCTL_READ_USN_JOURNAL  = 0x000900BB

    # USN_RECORD_V2 structure size (fixed header portion)
    _USN_RECORD_HEADER_SIZE = 60

    def __init__(self, volume: str) -> None:
        """
        Args:
            volume: Drive letter without trailing slash (e.g. "C:").
        """
        self.volume = volume.rstrip("\\")
        self._last_usn: int = 0
        self._journal_id: int = 0

    def is_available(self) -> bool:
        """Return True if the journal is readable on this volume."""
        if platform.system() != "Windows":
            return False
        try:
            import ctypes
            # Check admin
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def query_journal(self) -> dict:
        """
        Return current USN journal parameters.

        Returns dict with keys: journal_id, first_usn, next_usn,
        min_major_version, max_major_version.

        Raises:
            RuntimeError: if the IOCTL fails or journal is not enabled.
        """
        import ctypes
        import ctypes.wintypes

        vol_path = f"\\\\.\\{self.volume}"
        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

        kernel32 = ctypes.windll.kernel32
        h_vol = kernel32.CreateFileW(
            vol_path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if h_vol == ctypes.wintypes.HANDLE(-1).value:
            raise RuntimeError(
                f"Cannot open volume {self.volume}: error {kernel32.GetLastError()}"
            )

        try:
            # USN_JOURNAL_DATA_V0 structure (40 bytes)
            buf = ctypes.create_string_buffer(40)
            bytes_returned = ctypes.wintypes.DWORD(0)
            ok = kernel32.DeviceIoControl(
                h_vol,
                self._FSCTL_QUERY_USN_JOURNAL,
                None, 0,
                buf, ctypes.sizeof(buf),
                ctypes.byref(bytes_returned),
                None,
            )
            if not ok:
                raise RuntimeError(
                    f"FSCTL_QUERY_USN_JOURNAL failed: {kernel32.GetLastError()}"
                )

            (
                journal_id,
                first_usn,
                next_usn,
                lowest_valid_usn,
                max_usn,
                maximum_size,
                allocation_delta,
            ) = struct.unpack_from("<QQQQQQQ", buf.raw[:56])

            self._journal_id = journal_id
            return {
                "journal_id": journal_id,
                "first_usn": first_usn,
                "next_usn": next_usn,
            }
        finally:
            kernel32.CloseHandle(h_vol)

    def get_changed_since(self, since_usn: int) -> Set[int]:
        """
        Return the set of file reference numbers for files changed since
        *since_usn*.

        The caller maps reference numbers back to paths using
        ``get_path_from_ref()``.

        Args:
            since_usn: USN value from the last backup (0 = all files).

        Returns:
            Set of NTFS file reference numbers (FRN).
        """
        import ctypes
        import ctypes.wintypes

        if not self.is_available():
            raise RuntimeError("NTFS Change Journal not available")

        # Ensure journal is queried to get journal_id
        info = self.query_journal()
        journal_id = info["journal_id"]

        vol_path = f"\\\\.\\{self.volume}"
        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

        kernel32 = ctypes.windll.kernel32
        h_vol = kernel32.CreateFileW(
            vol_path, GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS, None,
        )

        changed_refs: Set[int] = set()

        try:
            # READ_USN_JOURNAL_DATA_V0 (40 bytes)
            # StartUsn, ReasonMask, ReturnOnlyOnClose, Timeout, BytesToWaitFor, UsnJournalID
            reason_mask = 0xFFFFFFFF  # all change reasons
            read_data = struct.pack(
                "<QQIQQQ",
                since_usn,       # StartUsn
                reason_mask,     # ReasonMask
                0,               # ReturnOnlyOnClose
                0,               # Timeout
                0,               # BytesToWaitFor
                journal_id,      # UsnJournalID
            )

            buf_size = 65536
            out_buf = ctypes.create_string_buffer(buf_size)
            bytes_returned = ctypes.wintypes.DWORD(0)
            in_buf = ctypes.create_string_buffer(read_data)

            while True:
                ok = kernel32.DeviceIoControl(
                    h_vol,
                    self._FSCTL_READ_USN_JOURNAL,
                    in_buf, len(read_data),
                    out_buf, buf_size,
                    ctypes.byref(bytes_returned),
                    None,
                )
                if not ok:
                    break

                total = bytes_returned.value
                if total < 8:
                    break

                # First 8 bytes is the next USN to continue from
                next_usn = struct.unpack_from("<Q", out_buf.raw[:8])[0]
                offset = 8

                while offset < total:
                    if offset + 4 > total:
                        break
                    rec_len = struct.unpack_from("<I", out_buf.raw, offset)[0]
                    if rec_len < self._USN_RECORD_HEADER_SIZE or offset + rec_len > total:
                        break

                    # FileReferenceNumber is at offset+8 (8 bytes)
                    frn = struct.unpack_from("<Q", out_buf.raw, offset + 8)[0]
                    changed_refs.add(frn)
                    offset += rec_len

                if next_usn == 0:
                    break

                # Update StartUsn for next iteration
                read_data = struct.pack(
                    "<QQIQQQ",
                    next_usn, reason_mask, 0, 0, 0, journal_id,
                )
                in_buf = ctypes.create_string_buffer(read_data)

        finally:
            kernel32.CloseHandle(h_vol)

        log.debug(
            "NTFS journal: %d changed file refs since USN %d",
            len(changed_refs), since_usn,
        )
        return changed_refs
