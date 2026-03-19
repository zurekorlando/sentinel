"""
Sentinel Windows Agent HTTP Client.

Communicates with the Sentinel Windows Agent (running on port 7700 by default)
to trigger VSS snapshots and stream file lists for remote backup.

Agent API contract
------------------
GET  /health                  → {"status": "ok", "version": "...", "hostname": "..."}
POST /snapshot/create         → {"volume": "C:\\"} → {"snapshot_id": "...", "mount_point": "..."}
POST /snapshot/{id}/release   → {} → {"ok": true}
GET  /snapshot/{id}/files     → JSON lines stream of file metadata
GET  /snapshot/{id}/file?path=<path> → raw file bytes (chunked)
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterator, List, Optional

log = logging.getLogger(__name__)

_DEFAULT_PORT = 7700
_DEFAULT_TIMEOUT = 30


@dataclass
class RemoteFileInfo:
    path: str
    size: int
    mtime: float
    is_dir: bool = False


class AgentClient:
    """
    HTTP client for the Sentinel Windows Agent.

    Args:
        host:    IP or hostname of the Windows machine.
        port:    TCP port the agent listens on (default: 7700).
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        host: str,
        port: int = _DEFAULT_PORT,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base = f"http://{host}:{port}"
        self._timeout = timeout

    # ------------------------------------------------------------------ #
    #  Health                                                              #
    # ------------------------------------------------------------------ #

    def health(self) -> dict:
        """Return the agent health dict. Raises on connection failure."""
        return self._get("/health")

    def is_reachable(self) -> bool:
        """Return True if the agent responds to /health."""
        try:
            self.health()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    #  Snapshot lifecycle                                                  #
    # ------------------------------------------------------------------ #

    def create_snapshot(self, volume: str) -> dict:
        """
        Request a VSS snapshot of *volume* (e.g. ``"C:\\"``).

        Returns a dict with ``snapshot_id`` and ``mount_point``.
        """
        return self._post("/snapshot/create", {"volume": volume})

    def release_snapshot(self, snapshot_id: str) -> None:
        """Delete the VSS shadow copy with the given ID."""
        try:
            self._post(f"/snapshot/{snapshot_id}/release", {})
        except Exception as exc:
            log.warning("Failed to release remote snapshot %s: %s", snapshot_id, exc)

    # ------------------------------------------------------------------ #
    #  File enumeration                                                    #
    # ------------------------------------------------------------------ #

    def list_files(self, snapshot_id: str) -> Iterator[RemoteFileInfo]:
        """
        Yield RemoteFileInfo objects for every file in the snapshot.

        The agent streams newline-delimited JSON, one object per line.
        """
        url = f"{self._base}/snapshot/{snapshot_id}/files"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                for raw_line in resp:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        yield RemoteFileInfo(
                            path=obj["path"],
                            size=obj.get("size", 0),
                            mtime=obj.get("mtime", 0.0),
                            is_dir=obj.get("is_dir", False),
                        )
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception as exc:
            raise RuntimeError(f"Failed to list files from agent: {exc}") from exc

    def read_file(self, snapshot_id: str, path: str) -> Iterator[bytes]:
        """
        Stream the raw bytes of a file from the agent.

        Yields chunks as received. Use in a context manager pattern::

            with open(dest, 'wb') as f:
                for chunk in client.read_file(snap_id, path):
                    f.write(chunk)
        """
        params = urllib.parse.urlencode({"path": path})
        url = f"{self._base}/snapshot/{snapshot_id}/file?{params}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    yield chunk
        except Exception as exc:
            raise RuntimeError(
                f"Failed to read file {path!r} from agent: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    def _get(self, path: str) -> dict:
        url = f"{self._base}{path}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self._base}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
