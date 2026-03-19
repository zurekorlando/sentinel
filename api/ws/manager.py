"""
WebSocket connection manager for real-time backup progress events.

Thread-safety
-------------
``broadcast_sync()`` may be called from BackupEngine worker threads that run
inside a ``ThreadPoolExecutor``.  It uses ``asyncio.run_coroutine_threadsafe``
to schedule the actual broadcast coroutine on the asyncio event loop that was
captured at API startup via ``set_loop()``.

Usage::

    # 1. In FastAPI lifespan (startup):
    ws_manager.set_loop(asyncio.get_running_loop())

    # 2. In the WebSocket endpoint:
    await ws_manager.connect(job_id, websocket)

    # 3. From BackupEngine worker thread (on_progress callback):
    ws_manager.broadcast_sync(job_id, {"event": "FILE_DONE", ...})
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional, Set

from fastapi import WebSocket

log = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages WebSocket connections grouped by job_id.

    Multiple dashboard tabs can subscribe to the same job simultaneously;
    each receives a copy of every broadcast event.
    """

    def __init__(self) -> None:
        self._sockets: Dict[str, Set[WebSocket]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── Loop registration ─────────────────────────────────────────────────────

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the running event loop so worker threads can schedule sends."""
        self._loop = loop

    # ── Connection lifecycle ──────────────────────────────────────────────────

    async def connect(self, job_id: str, ws: WebSocket) -> None:
        """Accept and register a WebSocket for *job_id*."""
        await ws.accept()
        self._sockets.setdefault(job_id, set()).add(ws)
        log.info(
            "WS connect: job=%s subscribers=%d",
            job_id,
            len(self._sockets[job_id]),
        )

    def disconnect(self, job_id: str, ws: WebSocket) -> None:
        """Remove a WebSocket from the *job_id* group."""
        if job_id in self._sockets:
            self._sockets[job_id].discard(ws)
        log.info(
            "WS disconnect: job=%s remaining=%d",
            job_id,
            len(self._sockets.get(job_id, set())),
        )

    # ── Broadcasting ──────────────────────────────────────────────────────────

    async def broadcast(self, job_id: str, data: dict) -> None:
        """Send *data* as JSON to every subscriber of *job_id* (async)."""
        dead: Set[WebSocket] = set()
        for ws in list(self._sockets.get(job_id, set())):
            try:
                await ws.send_json(data)
            except Exception as exc:
                log.debug("WS dead socket for job=%s: %s", job_id, exc)
                dead.add(ws)
        for ws in dead:
            self._sockets[job_id].discard(ws)

    def broadcast_sync(self, job_id: str, data: dict) -> None:
        """
        Thread-safe bridge — schedule a broadcast from a worker thread.

        If the event loop has not been registered, or no clients are
        subscribed to *job_id*, this is a silent no-op.
        """
        if not self._loop:
            return
        if not self._sockets.get(job_id):
            return
        asyncio.run_coroutine_threadsafe(
            self.broadcast(job_id, data), self._loop
        )

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def subscriber_count(self, job_id: str) -> int:
        """Return the number of active WebSocket connections for *job_id*."""
        return len(self._sockets.get(job_id, set()))


# Module-level singleton used by routers and the /ws endpoint in main.py.
ws_manager = ConnectionManager()
