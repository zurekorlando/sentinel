"""
Windows VSS (Volume Shadow Copy Service) Provider.

How VSS works in Sentinel
--------------------------
1.  ``vssadmin create shadow /for=<volume>`` is called.
    This command automatically invokes ALL registered VSS Writers
    (SQL Server, Active Directory, Exchange, Registry, etc.)
    which flush their in-memory state to disk before the snapshot is
    taken — guaranteeing application-consistent data.

2.  vssadmin outputs a Shadow Copy ID and device path of the form:
        \\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy<N>

3.  We create a directory junction at a temporary path so that Python's
    normal file I/O (os.walk, open, stat) can access the frozen snapshot
    through a plain directory path.

4.  The BackupEngine walks the junction path while the snapshot is live.

5.  On context exit the junction is removed and the shadow copy deleted.

Privilege requirement
---------------------
VSS requires the process to run as Administrator (or with
SeBackupPrivilege + SeRestorePrivilege).  ``is_available()`` checks this
before the BackupEngine attempts to use this provider.

Dependencies
------------
No additional Python packages required — only ``subprocess`` and the
built-in ``ctypes``/``winreg`` for the admin check.
``pywin32`` is an optional dependency for the richer ``VSSBackupComponents``
COM interface; the current implementation uses the vssadmin CLI which is
available on all Windows versions from Vista onward.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
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

# Regex patterns to parse vssadmin output
_RE_SHADOW_ID = re.compile(
    r"Shadow Copy ID:\s*\{([0-9a-fA-F\-]+)\}", re.IGNORECASE
)
_RE_DEVICE_PATH = re.compile(
    r"Shadow Copy Volume Name:\s*(\\\\\?\\[^\r\n]+)", re.IGNORECASE
)

_VSSADMIN = "vssadmin"
_TIMEOUT_CREATE = 120   # seconds — VSS writer invocation can be slow
_TIMEOUT_DELETE = 30


# ------------------------------------------------------------------ #
#  VSS Provider                                                        #
# ------------------------------------------------------------------ #

class VSSProvider(ISourceManager):
    """
    Creates application-consistent VSS shadow copies on Windows.

    Args:
        junction_base: Directory under which junctions are created.
                       Defaults to the system temp directory.
    """

    def __init__(self, junction_base: Optional[str] = None) -> None:
        self._junction_base = Path(junction_base or tempfile.gettempdir())

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
        Create a VSS shadow copy of *volume* and yield a SnapshotMount.

        The junction (accessible path) is created at:
            <junction_base>/sentinel_vss_<uuid8>

        Args:
            volume: Drive letter with trailing backslash, e.g. ``"C:\\"``
                    or ``"D:\\"``.  UNC paths are not supported by VSS.
        """
        if not volume.endswith("\\"):
            volume += "\\"

        shadow_id: Optional[str] = None
        junction_path: Optional[Path] = None

        # Pre-freeze hook (app-quiesce before snapshot)
        if hooks and hooks.pre_freeze:
            _run_hook("pre_freeze", hooks.pre_freeze)

        try:
            shadow_id, device_path = self._create_shadow(volume)
            log.info("VSS shadow created: id=%s device=%s", shadow_id, device_path)

            junction_path = self._junction_base / f"sentinel_vss_{uuid.uuid4().hex[:8]}"
            self._create_junction(junction_path, device_path)
            log.info("VSS junction: %s", junction_path)

            yield SnapshotMount(
                snapshot_id=shadow_id,
                mount_point=junction_path,
                volume=volume,
                provider="vss",
                app_consistent=True,
            )

        finally:
            # Post-thaw hook runs regardless of success/failure
            if hooks and hooks.post_thaw:
                _run_hook("post_thaw", hooks.post_thaw)

            # Remove junction first so handles are released
            if junction_path and junction_path.exists():
                try:
                    self._remove_junction(junction_path)
                    log.debug("VSS junction removed: %s", junction_path)
                except Exception as exc:
                    log.warning("Failed to remove VSS junction %s: %s", junction_path, exc)

            # Delete shadow copy
            if shadow_id:
                try:
                    self._delete_shadow(shadow_id)
                    log.info("VSS shadow deleted: %s", shadow_id)
                except Exception as exc:
                    log.warning("Failed to delete VSS shadow %s: %s", shadow_id, exc)

    def is_available(self) -> bool:
        """
        Return True if:
          - Running on Windows, AND
          - vssadmin.exe is on PATH, AND
          - Process has administrator privileges.
        """
        if platform.system() != "Windows":
            return False
        if not shutil.which(_VSSADMIN):
            log.debug("VSS: vssadmin not found on PATH")
            return False
        if not _is_admin():
            log.debug("VSS: process is not running as Administrator")
            return False
        return True

    def list_volumes(self) -> List[str]:
        """
        Return drive letters of all fixed/local volumes visible to VSS.
        """
        volumes: List[str] = []
        try:
            result = subprocess.run(
                [_VSSADMIN, "list", "volumes"],
                capture_output=True, text=True, timeout=30,
            )
            # Output contains lines like: "Volume path: C:\"
            for match in re.finditer(r"Volume path:\s+([A-Za-z]:\\)", result.stdout):
                volumes.append(match.group(1))
        except Exception as exc:
            log.warning("VSS list_volumes failed: %s", exc)
        return volumes

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _create_shadow(volume: str) -> tuple[str, str]:
        """
        Invoke vssadmin and return (shadow_id, device_path).

        vssadmin automatically triggers VSS Writers before taking the
        snapshot, providing application-consistent backups.
        """
        log.debug("Creating VSS shadow copy for %s …", volume)
        result = subprocess.run(
            [_VSSADMIN, "create", "shadow", f"/for={volume}"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_CREATE,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"vssadmin create shadow failed (rc={result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )

        id_match = _RE_SHADOW_ID.search(result.stdout)
        dev_match = _RE_DEVICE_PATH.search(result.stdout)

        if not id_match or not dev_match:
            raise RuntimeError(
                f"Failed to parse vssadmin output:\n{result.stdout}"
            )

        shadow_id = "{" + id_match.group(1) + "}"
        device_path = dev_match.group(1).strip()
        return shadow_id, device_path

    @staticmethod
    def _delete_shadow(shadow_id: str) -> None:
        """Delete a VSS shadow copy by its GUID."""
        subprocess.run(
            [_VSSADMIN, "delete", "shadows",
             f"/shadow={shadow_id}", "/quiet"],
            capture_output=True,
            timeout=_TIMEOUT_DELETE,
            check=False,   # non-fatal if already gone
        )

    @staticmethod
    def _create_junction(link_path: Path, device_path: str) -> None:
        """
        Create a directory junction so Python can walk the shadow copy
        via a normal filesystem path.

        We use ``mklink /j`` (junction) rather than ``mklink /d``
        (symbolic link) because junctions do not require SeCreateSymbolicLinkPrivilege
        and work for directory traversal on all Windows versions.
        """
        # Junction target must end with backslash
        target = device_path.rstrip("\\") + "\\"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/j", str(link_path), target],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"mklink /j failed: {result.stdout} {result.stderr}"
            )

    @staticmethod
    def _remove_junction(link_path: Path) -> None:
        """
        Remove the directory junction without touching the target.

        ``rmdir`` removes the junction entry; ``shutil.rmtree`` would
        follow it and delete the snapshot contents.
        """
        subprocess.run(
            ["cmd", "/c", "rmdir", str(link_path)],
            capture_output=True,
            check=False,
        )


# ------------------------------------------------------------------ #
#  Utilities                                                           #
# ------------------------------------------------------------------ #

def _is_admin() -> bool:
    """Return True if the current process has Administrator privileges."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except AttributeError:
        return False


def _run_hook(name: str, command: str) -> None:
    """Execute a pre/post hook shell command, logging but not raising on failure."""
    log.info("VSS: running %s hook: %s", name, command)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            log.warning(
                "VSS %s hook exited %d:\n%s",
                name, result.returncode, result.stderr or result.stdout,
            )
    except Exception as exc:
        log.warning("VSS %s hook failed: %s", name, exc)
