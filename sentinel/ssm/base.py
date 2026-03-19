"""
Source & Snapshot Manager (SSM) — Module 1.
Abstract base classes and shared data types.

Design contract
---------------
Every platform provider (VSS, LVM, Btrfs) must implement ISourceManager.
The central method is ``create_snapshot()``, a context manager that:

  1. Creates an OS-level consistent snapshot of the requested volume.
  2. Yields a ``SnapshotMount`` describing where the snapshot is accessible.
  3. Cleans up (deletes) the snapshot on context exit, regardless of errors.

The BackupEngine uses the mount_point inside the context to walk files
through CBT and feed them into the DPE pipeline.

Application-aware consistency
------------------------------
On Windows, VSS Writers are invoked automatically by vssadmin before the
snapshot is taken, ensuring applications like SQL Server and Exchange flush
their in-memory state to disk.
On Linux, LVM and Btrfs snapshots are filesystem-level; application-aware
consistency requires the application to support freeze/thaw hooks
(configurable pre/post commands in JobConfig).
"""

from __future__ import annotations

import platform
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional


# ------------------------------------------------------------------ #
#  Data types                                                          #
# ------------------------------------------------------------------ #

@dataclass
class SnapshotMount:
    """
    Describes a successfully created and mounted snapshot.

    Attributes:
        snapshot_id:   Unique identifier for this snapshot instance
                       (UUID or provider-assigned ID).
        mount_point:   Filesystem path where the snapshot is accessible
                       for reading.  Walk this path, not the live volume.
        volume:        The original volume / path that was snapshotted.
        provider:      Provider tag: "vss" | "lvm" | "btrfs" | "direct" | "agent".
        app_consistent: True if VSS writers / freeze-thaw hooks were used.
        metadata:      Extra provider-specific data (e.g. AgentClient reference
                       and remote snapshot ID for the "agent" provider).
    """
    snapshot_id: str
    mount_point: Path
    volume: str
    provider: str
    app_consistent: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class PrePostHooks:
    """
    Optional shell commands executed before and after snapshot creation.

    Used for application-aware consistency on Linux when VSS is not
    available (e.g. ``pre_freeze = "mysql -e 'FLUSH TABLES WITH READ LOCK'"``).
    """
    pre_freeze: Optional[str] = None   # run before snapshot
    post_thaw: Optional[str] = None    # run after snapshot (even on error)


# ------------------------------------------------------------------ #
#  Abstract base                                                       #
# ------------------------------------------------------------------ #

class ISourceManager(ABC):
    """
    Contract for all OS-level snapshot providers.

    Implementors:
      - VSSProvider     (Windows, sentinel/ssm/windows/vss.py)
      - LVMProvider     (Linux LVM,  sentinel/ssm/linux/lvm.py)
      - BtrfsProvider   (Linux Btrfs, sentinel/ssm/linux/btrfs.py)
      - DirectProvider  (no snapshot fallback, sentinel/ssm/factory.py)
    """

    @abstractmethod
    @contextmanager
    def create_snapshot(
        self,
        volume: str,
        hooks: Optional[PrePostHooks] = None,
    ) -> Iterator[SnapshotMount]:
        """
        Context manager that creates a snapshot and yields a SnapshotMount.

        The snapshot is deleted on context exit (normal or exception).

        Args:
            volume: Volume identifier.
                    Windows: drive letter or GUID ("C:\\", "D:\\")
                    Linux LVM: logical volume path ("/dev/vg0/data")
                    Linux Btrfs: subvolume path ("/mnt/data")
            hooks:  Optional pre-freeze / post-thaw shell commands.

        Yields:
            A :class:`SnapshotMount` ready for file enumeration.

        Example::

            with manager.create_snapshot("C:\\") as snap:
                for f in snap.mount_point.rglob("*"):
                    process(f)
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """
        Return True if this provider can operate in the current environment.

        Checks: OS compatibility, required binaries, privilege level.
        """
        ...

    @abstractmethod
    def list_volumes(self) -> List[str]:
        """
        Return discoverable volumes/LVs this provider can snapshot.

        Used by the CLI wizard to help users configure job source paths.
        """
        ...
