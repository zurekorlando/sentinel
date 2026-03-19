"""
NFS Storage Provider.

NFS shares are accessed as mounted directories — functionally identical to
local storage.  The OS NFS client handles all network transport; from
Sentinel's perspective it is just a path on the filesystem.

Usage (Docker): mount the NFS export as a volume:
    volumes:
      - type: volume
        driver: local
        driver_opts:
          type: nfs
          o: addr=server,nolock,soft,rw
          device: ":/export/path"
        source: nfs_data
        target: /mnt/nfs
Then set nfs_mount to "/mnt/nfs" in the job config.
"""

from __future__ import annotations

from pathlib import Path

from sentinel.spi.local import LocalProvider


class NFSProvider(LocalProvider):
    """
    Storage provider for NFS shares accessed as a mounted directory.

    Thin wrapper over :class:`~sentinel.spi.local.LocalProvider`.
    The NFS share must be mounted on the host (or container) before use.

    Args:
        nfs_mount: Path to the NFS mount point (e.g. ``/mnt/nfs/backups``).
    """

    def __init__(self, nfs_mount: str) -> None:
        mount = Path(nfs_mount)
        if not mount.exists():
            raise RuntimeError(
                f"NFS mount point '{nfs_mount}' does not exist. "
                "Ensure the NFS share is mounted before starting Sentinel."
            )
        super().__init__(base_path=nfs_mount)

    def __repr__(self) -> str:
        return f"NFSProvider(nfs_mount={self.base_path!r})"
