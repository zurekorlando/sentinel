"""
Linux Btrfs Snapshot Provider.

How Btrfs snapshots work
--------------------------
Btrfs is a Copy-on-Write filesystem.  A read-only subvolume snapshot is
an atomic, instant operation that requires no CoW store pre-allocation —
the snapshot is valid as long as at least one reference to a data extent
exists.  This is fundamentally different from LVM: no size limit, no risk
of the snapshot becoming invalid.

Key properties:
  - Instantaneous creation (O(1) metadata operation).
  - No dedicated storage reserve required.
  - Read-only (``-r`` flag) — blocks accidental writes.
  - Crash-consistent by default; application-consistent with pre-freeze hooks.

Workflow
--------
1.  ``btrfs subvolume snapshot -r <source_subvol> <snap_path>``
2.  Walk files under <snap_path>.
3.  ``btrfs subvolume delete <snap_path>``

The snapshot is created as a sibling of the source subvolume inside the
same Btrfs filesystem, at ``<snap_base>/<source_name>_<uuid8>``.

Auto-detection
--------------
``is_available()`` checks whether the target path lives on a Btrfs
filesystem by inspecting the ``stat -f`` type code.

Privilege requirement
---------------------
Btrfs subvolume snapshot requires root (uid 0) or the process to have the
``CAP_SYS_ADMIN`` capability.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from sentinel.ssm.base import ISourceManager, PrePostHooks, SnapshotMount

log = logging.getLogger(__name__)

_REQUIRED_BINS = ("btrfs", "stat")
_BTRFS_FS_TYPE = "btrfs"


class BtrfsProvider(ISourceManager):
    """
    Creates Btrfs read-only subvolume snapshots.

    Args:
        snap_base: Directory where snapshot subvolumes are created.
                   Must be on the same Btrfs filesystem as the source.
                   Defaults to the parent directory of the source path.
    """

    def __init__(self, snap_base: Optional[str] = None) -> None:
        self._snap_base = Path(snap_base) if snap_base else None

    # ------------------------------------------------------------------ #
    #  ISourceManager                                                      #
    # ------------------------------------------------------------------ #

    @contextmanager
    def create_snapshot(
        self,
        volume: str,
        hooks: Optional[PrePostHooks] = None,
    ) -> Iterator[SnapshotMount]:
        """
        Create a read-only Btrfs snapshot of *volume* and yield a SnapshotMount.

        Args:
            volume: Path to the Btrfs subvolume to snapshot (e.g. ``/mnt/data``
                    or ``/home``).  Must be a Btrfs subvolume root.
        """
        source = Path(volume)
        snap_name = f"{source.name or 'root'}_{uuid.uuid4().hex[:8]}"

        # Determine where to place the snapshot
        if self._snap_base:
            snap_path = self._snap_base / snap_name
        else:
            # Place snapshot as a sibling of the source subvolume
            snap_path = source.parent / snap_name

        snap_created = False

        if hooks and hooks.pre_freeze:
            _run_hook("pre_freeze", hooks.pre_freeze)

        try:
            self._create_snapshot(source, snap_path)
            snap_created = True
            log.info("Btrfs snapshot created: %s → %s", source, snap_path)

            if hooks and hooks.post_thaw:
                _run_hook("post_thaw", hooks.post_thaw)

            yield SnapshotMount(
                snapshot_id=snap_name,
                mount_point=snap_path,
                volume=volume,
                provider="btrfs",
                app_consistent=(hooks is not None and hooks.pre_freeze is not None),
            )

        finally:
            if snap_created and snap_path.exists():
                try:
                    self._delete_snapshot(snap_path)
                    log.info("Btrfs snapshot deleted: %s", snap_path)
                except Exception as exc:
                    log.warning("Failed to delete Btrfs snapshot %s: %s", snap_path, exc)

    def is_available(self) -> bool:
        """
        Return True if:
          - Running on Linux, AND
          - ``btrfs`` binary is on PATH, AND
          - Running as root (uid 0).
        """
        if platform.system() != "Linux":
            return False
        if os.getuid() != 0:
            log.debug("Btrfs: not running as root")
            return False
        missing = [b for b in _REQUIRED_BINS if not shutil.which(b)]
        if missing:
            log.debug("Btrfs: missing binaries: %s", missing)
            return False
        return True

    def list_volumes(self) -> List[str]:
        """
        List Btrfs subvolumes on all mounted Btrfs filesystems.

        Returns paths like ``/``, ``/home``, ``/var``.
        """
        volumes: List[str] = []
        try:
            # Find all Btrfs mount points
            result = subprocess.run(
                ["findmnt", "--type", "btrfs", "--noheadings", "--output", "TARGET"],
                capture_output=True, text=True, timeout=10,
            )
            for mount in result.stdout.splitlines():
                mount = mount.strip()
                if not mount:
                    continue
                # List subvolumes under this mount
                sub_result = subprocess.run(
                    ["btrfs", "subvolume", "list", "-o", mount],
                    capture_output=True, text=True, timeout=10,
                )
                for line in sub_result.stdout.splitlines():
                    # Format: "ID <id> gen <gen> top level <lvl> path <path>"
                    parts = line.strip().split(" path ")
                    if len(parts) == 2:
                        volumes.append(str(Path(mount) / parts[1].strip()))
                if not volumes:
                    volumes.append(mount)  # top-level subvolume
        except Exception as exc:
            log.warning("Btrfs list_volumes failed: %s", exc)
        return volumes

    def is_btrfs_volume(self, path: str) -> bool:
        """
        Return True if *path* resides on a Btrfs filesystem.

        Uses ``stat -f -c %T`` to check the filesystem type.
        """
        try:
            result = subprocess.run(
                ["stat", "-f", "-c", "%T", path],
                capture_output=True, text=True, timeout=5,
            )
            fs_type = result.stdout.strip().lower()
            return fs_type == _BTRFS_FS_TYPE
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _create_snapshot(source: Path, dest: Path) -> None:
        """Create a read-only Btrfs subvolume snapshot."""
        result = subprocess.run(
            ["btrfs", "subvolume", "snapshot", "-r", str(source), str(dest)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"btrfs snapshot failed:\n{result.stderr or result.stdout}"
            )

    @staticmethod
    def _delete_snapshot(snap_path: Path) -> None:
        """Delete a Btrfs subvolume snapshot."""
        result = subprocess.run(
            ["btrfs", "subvolume", "delete", str(snap_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"btrfs subvolume delete failed:\n{result.stderr or result.stdout}"
            )


# ------------------------------------------------------------------ #
#  Utilities                                                           #
# ------------------------------------------------------------------ #

def _run_hook(name: str, command: str) -> None:
    log.info("Btrfs: running %s hook: %s", name, command)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            text=True, timeout=60,
        )
        if result.returncode != 0:
            log.warning(
                "Btrfs %s hook exited %d:\n%s",
                name, result.returncode, result.stderr or result.stdout,
            )
    except Exception as exc:
        log.warning("Btrfs %s hook failed: %s", name, exc)
