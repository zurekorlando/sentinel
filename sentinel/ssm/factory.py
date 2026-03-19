"""
SSM Factory — auto-detects the best available snapshot provider.

Provider selection logic (in priority order)
--------------------------------------------
1.  VSSProvider    — Windows only, requires Administrator
2.  BtrfsProvider  — Linux, source path is on a Btrfs filesystem
3.  LVMProvider    — Linux, source path is backed by an LVM logical volume
4.  DirectProvider — Universal fallback: reads the live filesystem directly.
                     WARNING: files may change during backup.  Suitable for
                     dev/test environments or where snapshot privileges are
                     not available.

The factory can also be given an explicit provider name (from JobConfig) to
skip auto-detection, e.g. ``provider="lvm"`` or ``provider="direct"``.

DirectProvider
--------------
Yields a SnapshotMount whose mount_point IS the source volume itself (no
copy is made).  This is safe for:
  - Directories where application consistency is not required.
  - Read-only or rarely-written data.
  - Development and CI environments.
"""

from __future__ import annotations

import logging
import platform
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from sentinel.ssm.base import ISourceManager, PrePostHooks, SnapshotMount

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Direct (fallback) provider                                          #
# ------------------------------------------------------------------ #

class DirectProvider(ISourceManager):
    """
    No-op provider: reads the live filesystem without creating a snapshot.

    Always available.  Used when VSS/LVM/Btrfs are unavailable or the user
    explicitly opts out of snapshot-based backup.
    """

    @contextmanager
    def create_snapshot(
        self,
        volume: str,
        hooks: Optional[PrePostHooks] = None,
    ) -> Iterator[SnapshotMount]:
        """Yield the original path as-is — no snapshot is taken."""
        log.warning(
            "DirectProvider: backing up live filesystem at %s "
            "(no snapshot — files may change during backup)",
            volume,
        )
        if hooks and hooks.pre_freeze:
            _run_hook_direct("pre_freeze", hooks.pre_freeze)
        try:
            yield SnapshotMount(
                snapshot_id=f"direct_{Path(volume).name}",
                mount_point=Path(volume),
                volume=volume,
                provider="direct",
                app_consistent=False,
            )
        finally:
            if hooks and hooks.post_thaw:
                _run_hook_direct("post_thaw", hooks.post_thaw)

    def is_available(self) -> bool:
        return True

    def list_volumes(self) -> List[str]:
        """Return all mounted filesystem paths on the current OS."""
        volumes: List[str] = []
        import subprocess
        try:
            if platform.system() == "Windows":
                import string
                import ctypes
                bitmask = ctypes.windll.kernel32.GetLogicalDrives()
                for letter in string.ascii_uppercase:
                    if bitmask & 1:
                        volumes.append(f"{letter}:\\")
                    bitmask >>= 1
            else:
                result = subprocess.run(
                    ["findmnt", "--noheadings", "--output", "TARGET"],
                    capture_output=True, text=True, timeout=5,
                )
                volumes = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        except Exception:
            pass
        return volumes or ["/"]


# ------------------------------------------------------------------ #
#  Factory                                                             #
# ------------------------------------------------------------------ #

class SnapshotFactory:
    """
    Resolves the best available :class:`~sentinel.ssm.base.ISourceManager`
    for the current environment.

    Usage::

        manager = SnapshotFactory.create()
        with manager.create_snapshot("C:\\") as snap:
            # snap.mount_point is safe to walk
            ...

        # Or force a specific provider:
        manager = SnapshotFactory.create(preferred="vss")
    """

    @staticmethod
    def create(
        preferred: Optional[str] = None,
        **kwargs,
    ) -> ISourceManager:
        """
        Return an :class:`~sentinel.ssm.base.ISourceManager` instance.

        Args:
            preferred: Optional provider name: ``"vss"``, ``"lvm"``,
                       ``"btrfs"``, or ``"direct"``.  When specified,
                       the factory returns that provider without checking
                       availability (the provider itself will raise on
                       ``create_snapshot`` if it cannot operate).
            **kwargs:  Forwarded to the provider constructor (e.g.
                       ``snap_size="2G"`` for LVMProvider).

        Returns:
            An :class:`~sentinel.ssm.base.ISourceManager` instance.
        """
        if preferred:
            return SnapshotFactory._build(preferred, **kwargs)

        return SnapshotFactory._auto_detect(**kwargs)

    @staticmethod
    def _auto_detect(**kwargs) -> ISourceManager:
        """Try providers in priority order; return the first available one."""
        if platform.system() == "Windows":
            try:
                from sentinel.ssm.windows.vss import VSSProvider
                p = VSSProvider(**kwargs)
                if p.is_available():
                    log.info("SSM: using VSSProvider (Windows, admin)")
                    return p
                else:
                    log.info(
                        "SSM: VSSProvider unavailable (not admin or vssadmin missing) "
                        "— falling back to DirectProvider"
                    )
            except ImportError:
                pass
        else:
            # Linux: try Btrfs first (no size limit), then LVM
            try:
                from sentinel.ssm.linux.btrfs import BtrfsProvider
                p = BtrfsProvider(**{k: v for k, v in kwargs.items()
                                     if k in ("snap_base",)})
                if p.is_available():
                    log.info("SSM: using BtrfsProvider (Linux, root)")
                    return p
            except ImportError:
                pass

            try:
                from sentinel.ssm.linux.lvm import LVMProvider
                p = LVMProvider(**{k: v for k, v in kwargs.items()
                                   if k in ("snap_size", "mount_base")})
                if p.is_available():
                    log.info("SSM: using LVMProvider (Linux, root)")
                    return p
            except ImportError:
                pass

            log.info(
                "SSM: neither Btrfs nor LVM available (not root or binaries missing) "
                "— falling back to DirectProvider"
            )

        return DirectProvider()

    @staticmethod
    def _build(name: str, **kwargs) -> ISourceManager:
        """Instantiate a provider by name."""
        name = name.lower()
        if name == "vss":
            from sentinel.ssm.windows.vss import VSSProvider
            return VSSProvider(**{k: v for k, v in kwargs.items()
                                  if k in ("junction_base",)})
        if name == "lvm":
            from sentinel.ssm.linux.lvm import LVMProvider
            return LVMProvider(**{k: v for k, v in kwargs.items()
                                  if k in ("snap_size", "mount_base")})
        if name == "btrfs":
            from sentinel.ssm.linux.btrfs import BtrfsProvider
            return BtrfsProvider(**{k: v for k, v in kwargs.items()
                                    if k in ("snap_base",)})
        if name == "direct":
            return DirectProvider()
        if name == "agent":
            from sentinel.ssm.windows.remote_provider import RemoteWindowsProvider
            host = kwargs.get("host", "")
            port = int(kwargs.get("port", 7700))
            if not host:
                raise ValueError("RemoteWindowsProvider requires 'host' kwarg")
            return RemoteWindowsProvider(host=host, port=port)
        raise ValueError(
            f"Unknown snapshot provider: {name!r}. "
            "Choose from: vss, lvm, btrfs, direct, agent."
        )

    @staticmethod
    def describe() -> str:
        """Return a human-readable string describing the auto-selected provider."""
        manager = SnapshotFactory._auto_detect()
        return f"{manager.__class__.__name__} ({'available' if manager.is_available() else 'fallback'})"


# ------------------------------------------------------------------ #
#  Utility                                                             #
# ------------------------------------------------------------------ #

def _run_hook_direct(name: str, command: str) -> None:
    import subprocess
    log.info("Direct: running %s hook: %s", name, command)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            text=True, timeout=60,
        )
        if result.returncode != 0:
            log.warning(
                "Direct %s hook exited %d:\n%s",
                name, result.returncode, result.stderr or result.stdout,
            )
    except Exception as exc:
        log.warning("Direct %s hook failed: %s", name, exc)
