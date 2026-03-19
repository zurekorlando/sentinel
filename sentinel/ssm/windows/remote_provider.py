"""
RemoteWindowsProvider — SSM provider for agent-based Windows backup.

Communicates with the Sentinel Windows Agent (running on port 7700) to:
  1. Trigger a remote VSS snapshot.
  2. Yield a SnapshotMount that signals the engine to use the agent's
     file listing and streaming APIs instead of local os.walk + open().
  3. Release the remote snapshot when the context exits.

The SnapshotMount.metadata dict carries:
  - "client"             : AgentClient instance
  - "remote_snapshot_id" : The snapshot ID assigned by the remote agent
  - "volume"             : The Windows volume that was snapshotted (e.g. "C:")
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from sentinel.discovery.agent_client import AgentClient
from sentinel.ssm.base import ISourceManager, PrePostHooks, SnapshotMount

log = logging.getLogger(__name__)


class RemoteWindowsProvider(ISourceManager):
    """
    ISourceManager implementation that delegates snapshot creation to a
    remote Sentinel Windows Agent via HTTP.

    Args:
        host:    IP address or hostname of the Windows machine running the agent.
        port:    TCP port where the agent is listening (default: 7700).
        volume:  Windows volume to snapshot (default: "C:").
    """

    def __init__(self, host: str, port: int = 7700) -> None:
        self.host = host
        self.port = port
        self._client = AgentClient(host, port)

    @contextmanager
    def create_snapshot(
        self,
        volume: str,
        hooks: Optional[PrePostHooks] = None,
    ) -> Iterator[SnapshotMount]:
        """
        Ask the remote agent to create a VSS snapshot of *volume*, yield a
        SnapshotMount with provider="agent", then release the remote snapshot.

        The mount_point is set to a synthetic Path so the engine can detect
        provider=="agent" and switch to the remote walk/read path.
        """
        log.info(
            "RemoteWindowsProvider: creating VSS snapshot on %s:%d volume=%s",
            self.host, self.port, volume,
        )

        # Health check before starting
        try:
            self._client.health()
        except Exception as exc:
            raise RuntimeError(
                f"Sentinel agent at {self.host}:{self.port} is not reachable: {exc}"
            ) from exc

        snap_info = self._client.create_snapshot(volume)
        remote_snapshot_id = snap_info["snapshot_id"]

        log.info(
            "RemoteWindowsProvider: remote VSS snapshot created | id=%s volume=%s",
            remote_snapshot_id, volume,
        )

        # Synthetic mount_point — the engine detects provider=="agent" and
        # never actually calls os.walk on this path.
        synthetic_mount = Path(f"/agent/{self.host}/{volume.rstrip(':')}")

        try:
            yield SnapshotMount(
                snapshot_id=remote_snapshot_id,
                mount_point=synthetic_mount,
                volume=volume,
                provider="agent",
                app_consistent=True,  # VSS is always app-consistent
                metadata={
                    "client": self._client,
                    "remote_snapshot_id": remote_snapshot_id,
                    "host": self.host,
                    "port": self.port,
                    "volume": volume,
                },
            )
        finally:
            try:
                self._client.release_snapshot(remote_snapshot_id)
                log.info(
                    "RemoteWindowsProvider: released remote snapshot %s",
                    remote_snapshot_id,
                )
            except Exception as exc:
                log.warning(
                    "RemoteWindowsProvider: failed to release snapshot %s: %s",
                    remote_snapshot_id, exc,
                )

    def is_available(self) -> bool:
        """Return True if the remote agent is reachable and healthy."""
        try:
            self._client.health()
            return True
        except Exception:
            return False

    def list_volumes(self) -> List[str]:
        """Return volumes available on the remote Windows machine."""
        try:
            info = self._client.health()
            return info.get("volumes", ["C:"])
        except Exception:
            return ["C:"]
