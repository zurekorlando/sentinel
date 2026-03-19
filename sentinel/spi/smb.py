"""
SMB/CIFS Storage Provider.

SMB shares must be mounted on the host (or container) at ``base_path``
before Sentinel can use them.  Once mounted, access is identical to local
storage — the OS SMB client handles the network layer transparently.

Mounting examples
-----------------
Linux (cifs-utils):
    sudo mount -t cifs //server/share /mnt/smb \\
        -o username=USER,password=PASS,vers=3.0

Docker volume:
    volumes:
      - type: volume
        driver: local
        driver_opts:
          type: cifs
          o: username=USER,password=PASS,vers=3.0
          device: //server/share
        source: smb_data
        target: /mnt/smb
"""

from __future__ import annotations

from pathlib import Path

from sentinel.spi.local import LocalProvider


class SMBProvider(LocalProvider):
    """
    Storage provider for SMB/CIFS shares accessed as a mounted directory.

    Thin wrapper over :class:`~sentinel.spi.local.LocalProvider`.
    The SMB share must be mounted at ``base_path`` on the host (or container)
    before use.  Raises :exc:`NotImplementedError` with actionable guidance
    if the mount point does not exist.

    Args:
        smb_server:  UNC path of the share (e.g. ``\\\\server\\share``).
                     Used only for logging/error messages.
        base_path:   Local mount point path (e.g. ``/mnt/smb``).
    """

    def __init__(self, smb_server: str, base_path: str, **_: object) -> None:
        mount = Path(base_path)
        if not mount.exists():
            raise NotImplementedError(
                f"SMB share '{smb_server}' must be mounted at '{base_path}' before use.\n"
                "On Linux:  sudo mount -t cifs //server/share /mnt/smb "
                "-o username=USER,password=PASS,vers=3.0\n"
                "In Docker: use a 'local' volume with driver_opts type=cifs."
            )
        self._smb_server = smb_server
        super().__init__(base_path=base_path)

    def __repr__(self) -> str:
        return f"SMBProvider(server={self._smb_server!r}, base_path={self.base_path!r})"
