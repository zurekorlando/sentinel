"""
Linux LVM (Logical Volume Manager) Snapshot Provider.

How LVM snapshots work
-----------------------
LVM uses a Copy-on-Write (CoW) mechanism at the block device level.
When a snapshot LV is created, the original LV continues to accept writes
normally.  Any block that is about to be overwritten is first copied into
the snapshot LV's CoW store.  This means:

  - The snapshot represents the exact state of the volume at creation time.
  - It is crash-consistent (not application-consistent unless freeze is used).
  - For application consistency, set a pre_freeze hook (e.g. ``sync`` or
    a database FLUSH command) in the job configuration.

Workflow
--------
1.  Detect the LVM logical volume that backs the requested path.
2.  Call ``lvcreate -s -n <snap_name> -L <size> <lv_path>``
    to create a read-only snapshot.
3.  ``mount -o ro,norecovery <snap_lv> <tmpdir>``
4.  Yield the SnapshotMount.
5.  On exit: ``umount``, ``lvremove -f``, ``rmdir``.

Snapshot size
-------------
The CoW store must be large enough to hold all blocks modified during the
backup window.  The default is 10% of the original LV size (configurable).
If the CoW store fills up the snapshot becomes invalid.  Monitor
``lvs -o snap_percent`` to track usage.

Privilege requirement
---------------------
``lvcreate``, ``mount``, and ``lvremove`` require root (uid 0) or
appropriate sudo rules.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from sentinel.ssm.base import ISourceManager, PrePostHooks, SnapshotMount

log = logging.getLogger(__name__)

_REQUIRED_BINS = ("lvcreate", "lvremove", "lvdisplay", "mount", "umount", "df")
_DEFAULT_SNAP_SIZE = "10%ORIGIN"  # relative CoW store size
_MOUNT_TIMEOUT = 30


class LVMProvider(ISourceManager):
    """
    Creates LVM snapshot LVs and mounts them read-only for backup.

    Args:
        snap_size:  CoW store size passed to ``lvcreate -L`` or ``-l``.
                    Examples: ``"1G"``, ``"10%ORIGIN"``, ``"20%VG"``.
        mount_base: Parent directory for temporary snapshot mount points.
                    Defaults to the system temp directory.
    """

    def __init__(
        self,
        snap_size: str = _DEFAULT_SNAP_SIZE,
        mount_base: Optional[str] = None,
    ) -> None:
        self.snap_size = snap_size
        self._mount_base = Path(mount_base or tempfile.gettempdir())

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
        Create an LVM snapshot of *volume* and yield a SnapshotMount.

        Args:
            volume: Either a logical volume device path (``/dev/vg0/data``)
                    or a mounted filesystem path (``/mnt/data``).  When a
                    filesystem path is given the provider auto-resolves it
                    to its backing LV.
        """
        lv_path = self._resolve_lv(volume)
        snap_name = f"sentinel_snap_{uuid.uuid4().hex[:8]}"
        # snap_lv lives in the same VG as the origin
        vg, _ = self._parse_lv_device(lv_path)
        snap_lv = f"/dev/{vg}/{snap_name}"

        mount_dir = self._mount_base / f"sentinel_lvm_{uuid.uuid4().hex[:8]}"
        mount_dir.mkdir(parents=True, exist_ok=True)

        snap_created = False
        mounted = False

        if hooks and hooks.pre_freeze:
            _run_hook("pre_freeze", hooks.pre_freeze)

        try:
            # Step 1 — Create LVM snapshot
            self._lvcreate(lv_path, snap_name, self.snap_size)
            snap_created = True
            log.info("LVM snapshot created: %s (CoW %s)", snap_lv, self.snap_size)

            # Post-thaw runs right after snapshot is taken (volume is unfrozen)
            if hooks and hooks.post_thaw:
                _run_hook("post_thaw", hooks.post_thaw)

            # Step 2 — Mount snapshot read-only
            self._mount_snapshot(snap_lv, mount_dir, lv_path)
            mounted = True
            log.info("LVM snapshot mounted at %s", mount_dir)

            yield SnapshotMount(
                snapshot_id=snap_name,
                mount_point=mount_dir,
                volume=volume,
                provider="lvm",
                app_consistent=(hooks is not None and hooks.pre_freeze is not None),
            )

        finally:
            if mounted:
                try:
                    _run(["umount", str(mount_dir)], timeout=_MOUNT_TIMEOUT)
                    log.debug("LVM snapshot unmounted")
                except Exception as exc:
                    log.warning("Failed to unmount %s: %s", mount_dir, exc)

            if snap_created:
                try:
                    _run(["lvremove", "-f", snap_lv])
                    log.info("LVM snapshot LV removed: %s", snap_lv)
                except Exception as exc:
                    log.warning("Failed to remove LV %s: %s", snap_lv, exc)

            try:
                mount_dir.rmdir()
            except OSError:
                pass

    def is_available(self) -> bool:
        """
        Return True if:
          - Running on Linux, AND
          - All required binaries (lvcreate, mount, etc.) are on PATH, AND
          - Running as root (uid 0).
        """
        import platform
        if platform.system() != "Linux":
            return False
        if os.getuid() != 0:
            log.debug("LVM: not running as root")
            return False
        missing = [b for b in _REQUIRED_BINS if not shutil.which(b)]
        if missing:
            log.debug("LVM: missing binaries: %s", missing)
            return False
        return True

    def list_volumes(self) -> List[str]:
        """Return all logical volume device paths visible to lvdisplay."""
        volumes: List[str] = []
        try:
            result = subprocess.run(
                ["lvdisplay", "--noheadings", "-C", "-o", "lv_path"],
                capture_output=True, text=True, timeout=15,
            )
            for line in result.stdout.splitlines():
                lv = line.strip()
                if lv:
                    volumes.append(lv)
        except Exception as exc:
            log.warning("LVM list_volumes failed: %s", exc)
        return volumes

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_lv(volume: str) -> str:
        """
        If *volume* looks like a mounted path (starts with ``/`` but is not
        a device node), resolve it to its LVM device using ``df``.

        Example: ``/mnt/data`` → ``/dev/mapper/vg0-data``
        """
        if volume.startswith("/dev/"):
            return volume  # Already a device path

        result = subprocess.run(
            ["df", "--output=source", volume],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            raise RuntimeError(f"Cannot resolve volume to device: {volume}")
        device = lines[-1].strip()

        # Verify it's an LVM LV
        check = subprocess.run(
            ["lvdisplay", device],
            capture_output=True, timeout=10,
        )
        if check.returncode != 0:
            raise RuntimeError(
                f"{device} (backing {volume}) does not appear to be an LVM LV. "
                "Use --provider=direct or configure an LVM path explicitly."
            )
        return device

    @staticmethod
    def _parse_lv_device(lv_path: str) -> tuple[str, str]:
        """
        Return (vg_name, lv_name) from an LV device path.

        Handles both ``/dev/VG/LV`` and ``/dev/mapper/VG-LV`` forms.
        The mapper form is checked first because ``/dev/mapper/VG-LV`` also
        satisfies the generic ``/dev/X/Y`` pattern (matching ``mapper`` as VG).
        Non-greedy matching on the mapper regex ensures the shortest VG prefix
        is taken (standard naming: ``/dev/mapper/vg0-lv0`` → vg=``vg0``, lv=``lv0``).
        """
        # Try /dev/mapper/VG-LV form FIRST (more specific than the generic form)
        m = re.match(r"^/dev/mapper/(.+?)-([^-]+)$", lv_path)
        if m:
            return m.group(1), m.group(2)

        # Try /dev/VG/LV form (excludes /dev/mapper/... already handled above)
        m = re.match(r"^/dev/([^/]+)/([^/]+)$", lv_path)
        if m and m.group(1) != "mapper":
            return m.group(1), m.group(2)

        raise ValueError(f"Cannot parse LV device path: {lv_path}")

    @staticmethod
    def _lvcreate(origin_lv: str, snap_name: str, size: str) -> None:
        """Create a snapshot logical volume."""
        # Size may be absolute ("1G") or relative ("10%ORIGIN")
        size_flag = "-l" if "%" in size else "-L"
        _run(["lvcreate", size_flag, size, "-s", "-n", snap_name, origin_lv])

    @staticmethod
    def _mount_snapshot(snap_lv: str, mount_dir: Path, origin_lv: str) -> None:
        """
        Mount the snapshot LV read-only.

        ``norecovery`` prevents the kernel from running journal replay on
        the snapshot (which would modify the CoW store).  Supported by
        ext3/4, XFS, and most modern Linux filesystems.
        """
        # Try with norecovery first; fall back without it for FAT/NTFS
        for opts in ("ro,norecovery", "ro"):
            result = subprocess.run(
                ["mount", "-o", opts, snap_lv, str(mount_dir)],
                capture_output=True, text=True, timeout=_MOUNT_TIMEOUT,
            )
            if result.returncode == 0:
                return
            if "option" not in result.stderr.lower():
                # Genuine mount failure, not an option compatibility issue
                break

        raise RuntimeError(
            f"Failed to mount snapshot {snap_lv} at {mount_dir}:\n"
            f"{result.stderr}"
        )


# ------------------------------------------------------------------ #
#  Utilities                                                           #
# ------------------------------------------------------------------ #

def _run(cmd: list, timeout: int = 30) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _run_hook(name: str, command: str) -> None:
    log.info("LVM: running %s hook: %s", name, command)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            text=True, timeout=60,
        )
        if result.returncode != 0:
            log.warning(
                "LVM %s hook exited %d:\n%s",
                name, result.returncode, result.stderr or result.stdout,
            )
    except Exception as exc:
        log.warning("LVM %s hook failed: %s", name, exc)
