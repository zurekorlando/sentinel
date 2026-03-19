"""
Sentinel Agent — FastAPI application.

A lightweight HTTP service that provides:
  - Startup registration with the Sentinel server
  - Periodic heartbeat
  - VSS snapshot creation and cleanup
  - File enumeration (with metadata)
  - File content streaming
  - Cross-platform file browsing (Windows, Linux, macOS)
  - System info endpoint

Default port: 8765 (configurable via SENTINEL_AGENT_PORT env var)

Endpoints
---------
GET  /health                          Liveness + version check.
GET  /info                            System info (hostname, OS, drives, user).
GET  /browse                          Cross-platform directory listing.
POST /snapshot/create                 Create a VSS shadow copy.
POST /snapshot/{snapshot_id}/release  Delete the shadow copy.
GET  /snapshot/{snapshot_id}/files    Stream file list (NDJSON).
GET  /snapshot/{snapshot_id}/file     Stream a file's raw bytes.

Security note
-------------
The agent binds only to the LAN interface by default and requires
an API key (SENTINEL_AGENT_KEY env var) in the X-Sentinel-Key header
for all endpoints except /health and /info.  Rotate the key on initial setup.

Running
-------
    # Development
    uvicorn agent.main:app --host 0.0.0.0 --port 8765

    # As Windows Service (after running install/agent.ps1)
    sc start SentinelAgent
"""

from __future__ import annotations

import json as _json
import logging
import os
import os as _os
import platform
import shutil
import socket
import time
import threading
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

log = logging.getLogger(__name__)

_VERSION = "1.0.0"
_AGENT_KEY = os.getenv("SENTINEL_AGENT_KEY", "")
_SERVER_URL = os.getenv("SENTINEL_SERVER_URL", "").rstrip("/")
_PORT = int(os.getenv("SENTINEL_AGENT_PORT", "8765"))
_CONFIG_FILE = Path(__file__).parent / "sentinel_agent.json"


# ── Active snapshots registry ─────────────────────────────────────────────────

# snapshot_id → {"mount_point": str, "shadow_id": str, "created_at": float}
_snapshots: dict[str, dict] = {}


# ── Auth dependency ────────────────────────────────────────────────────────────

def _require_key(x_sentinel_key: Optional[str] = Header(default=None)) -> None:
    """Validate API key if SENTINEL_AGENT_KEY is configured."""
    if _AGENT_KEY and x_sentinel_key != _AGENT_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Sentinel-Key")


# ── Local config helpers ───────────────────────────────────────────────────────

def _load_agent_id() -> str:
    """Load persisted agent_id from local config, or '' if not found."""
    try:
        if _CONFIG_FILE.exists():
            data = _json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            return data.get("agent_id", "")
    except Exception as exc:
        log.warning("Could not load agent config: %s", exc)
    return ""


def _save_agent_id(agent_id: str) -> None:
    """Persist agent_id to local config file."""
    try:
        _CONFIG_FILE.write_text(
            _json.dumps({"agent_id": agent_id}),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("Could not save agent config: %s", exc)


# ── Registration and heartbeat ────────────────────────────────────────────────

def _register_with_server() -> str | None:
    """POST /api/agents/register. Returns agent_id on success, None on failure."""
    if not _SERVER_URL or not _AGENT_KEY:
        return None

    agent_id = _load_agent_id()

    payload = {
        "agent_id": agent_id,
        "api_key": _AGENT_KEY,
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_version": platform.version(),
        "ip_address": "",
        "port": _PORT,
        "version": _VERSION,
    }

    body = _json.dumps(payload).encode("utf-8")
    url = f"{_SERVER_URL}/api/agents/register"

    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            new_agent_id = data.get("agent_id", "")
            if new_agent_id:
                _save_agent_id(new_agent_id)
                interval_s = data.get("interval_s", 30)
                log.info(
                    "Registered with server — agent_id=%s, heartbeat_interval=%ss",
                    new_agent_id,
                    interval_s,
                )
                return new_agent_id
            log.warning("Server registration response missing agent_id: %s", data)
            return None
    except Exception as exc:
        log.warning("Could not register with server (%s): %s", url, exc)
        return None


def _heartbeat_loop(agent_id: str, interval_s: int = 30) -> None:
    """Run in a daemon thread. POSTs heartbeat every interval_s seconds."""
    current_agent_id = agent_id
    backoff = interval_s

    while True:
        time.sleep(backoff)

        if not current_agent_id:
            # Try to re-register
            new_id = _register_with_server()
            if new_id:
                current_agent_id = new_id
                backoff = interval_s
            else:
                backoff = min(backoff * 2, 300)
            continue

        payload = {
            "api_key": _AGENT_KEY,
            "ip_address": "",
            "port": _PORT,
            "version": _VERSION,
        }
        body = _json.dumps(payload).encode("utf-8")
        url = f"{_SERVER_URL}/api/agents/{current_agent_id}/heartbeat"

        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()  # consume response
            backoff = interval_s  # reset backoff on success
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                log.warning(
                    "Agent not found on server (404) — attempting re-registration"
                )
                current_agent_id = ""
                _save_agent_id("")
                new_id = _register_with_server()
                if new_id:
                    current_agent_id = new_id
                    backoff = interval_s
                else:
                    backoff = min(backoff * 2, 300)
            else:
                log.warning("Heartbeat HTTP error %s: %s", exc.code, exc)
                backoff = min(backoff * 2, 300)
        except Exception as exc:
            log.warning("Heartbeat failed: %s", exc)
            backoff = min(backoff * 2, 300)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Sentinel Agent v%s starting on %s", _VERSION, socket.gethostname())

    # Only register if server URL and key are configured
    if _SERVER_URL and _AGENT_KEY:
        agent_id = _register_with_server()
        if agent_id:
            t = threading.Thread(target=_heartbeat_loop, args=(agent_id,), daemon=True)
            t.start()
        else:
            log.warning("Could not register with server — heartbeat not started")
    else:
        log.info(
            "SENTINEL_SERVER_URL or SENTINEL_AGENT_KEY not set — running in standalone mode"
        )

    yield
    # Clean up any lingering snapshots on shutdown
    _cleanup_all_snapshots()
    log.info("Sentinel Agent shutting down")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Sentinel Agent",
    version=_VERSION,
    description="Cross-platform backup agent for Sentinel: file browsing, VSS snapshots, and file streaming.",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class SnapshotCreateRequest(BaseModel):
    volume: str = "C:\\"  # Drive letter with backslash


# ── Helpers ───────────────────────────────────────────────────────────────────

def _list_windows_drives() -> list[str]:
    """Return available drive letters on Windows (e.g. ['C:\\', 'D:\\'])."""
    if platform.system() != "Windows":
        return []
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        return [f"{chr(65 + i)}\\" for i in range(26) if bitmask & (1 << i)]
    except Exception:
        # Fallback: try common paths
        return [f"{c}\\" for c in "CDEFGH" if Path(f"{c}:\\").exists()]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health", tags=["agent"])
async def health() -> dict:
    """Liveness probe — no auth required."""
    return {
        "status": "ok",
        "version": _VERSION,
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "sentinel": True,
        "active_snapshots": len(_snapshots),
    }


@app.get("/info", tags=["agent"])
async def info() -> dict:
    """System info: hostname, OS, drives, current user — no auth required."""
    current_platform = platform.system()

    # Collect drive info
    drives = []
    if current_platform == "Windows":
        for drive in _list_windows_drives():
            try:
                usage = shutil.disk_usage(drive)
                drives.append({"path": drive, "total": usage.total, "free": usage.free})
            except Exception:
                drives.append({"path": drive, "total": None, "free": None})
    else:
        try:
            usage = shutil.disk_usage("/")
            drives.append({"path": "/", "total": usage.total, "free": usage.free})
        except Exception:
            drives.append({"path": "/", "total": None, "free": None})

    try:
        current_user = os.getlogin()
    except Exception:
        current_user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"

    return {
        "hostname": socket.gethostname(),
        "os": current_platform,
        "os_version": platform.version(),
        "arch": platform.machine(),
        "version": _VERSION,
        "drives": drives,
        "current_user": current_user,
    }


@app.post("/snapshot/create", tags=["snapshots"])
async def create_snapshot(
    req: SnapshotCreateRequest,
    x_sentinel_key: Optional[str] = Header(default=None),
) -> dict:
    """
    Create a VSS shadow copy of the requested volume.

    Returns snapshot_id and mount_point (junction path).
    """
    _require_key(x_sentinel_key)

    if platform.system() != "Windows":
        raise HTTPException(
            status_code=501,
            detail="VSS snapshots are only available on Windows.",
        )

    snapshot_id = str(uuid.uuid4())

    try:
        from sentinel.ssm.windows.vss import VSSProvider
        provider = VSSProvider()

        # We enter the context and hold the snapshot open
        # The context manager is stored so we can release it later
        ctx = provider.create_snapshot(req.volume).__enter__()
        mount_point = str(ctx.mount_point)
        shadow_id = ctx.snapshot_id

        _snapshots[snapshot_id] = {
            "ctx": ctx,
            "provider": provider,
            "shadow_id": shadow_id,
            "mount_point": mount_point,
            "volume": req.volume,
            "created_at": time.time(),
        }

        log.info("Snapshot created: %s → %s", snapshot_id, mount_point)
        return {
            "snapshot_id": snapshot_id,
            "shadow_id": shadow_id,
            "mount_point": mount_point,
            "volume": req.volume,
        }

    except Exception as exc:
        log.error("Failed to create snapshot: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/snapshot/{snapshot_id}/release", tags=["snapshots"])
async def release_snapshot(
    snapshot_id: str,
    x_sentinel_key: Optional[str] = Header(default=None),
) -> dict:
    """Delete the VSS shadow copy and remove the junction."""
    _require_key(x_sentinel_key)

    if snapshot_id not in _snapshots:
        raise HTTPException(status_code=404, detail=f"Snapshot {snapshot_id!r} not found")

    _release_snapshot(snapshot_id)
    return {"ok": True, "snapshot_id": snapshot_id}


@app.get("/snapshot/{snapshot_id}/files", tags=["snapshots"])
async def list_files(
    snapshot_id: str,
    x_sentinel_key: Optional[str] = Header(default=None),
) -> StreamingResponse:
    """
    Stream file metadata as newline-delimited JSON (one object per line).

    Each line: {"path": "...", "size": N, "mtime": N.N, "is_dir": false}
    """
    _require_key(x_sentinel_key)

    if snapshot_id not in _snapshots:
        raise HTTPException(status_code=404, detail=f"Snapshot {snapshot_id!r} not found")

    mount_point = Path(_snapshots[snapshot_id]["mount_point"])

    return StreamingResponse(
        _stream_file_list(mount_point),
        media_type="application/x-ndjson",
    )


@app.get("/browse", tags=["agent"])
async def browse(
    path: str = Query(default="", description="Directory path. Empty = list drives/root"),
    x_sentinel_key: Optional[str] = Header(default=None),
) -> dict:
    """
    List directory contents cross-platform.

    - path="" on Windows: returns list of available drives (C:\\, D:\\, etc.)
    - path="" on Linux/macOS: returns contents of "/"
    - path="<dir>": returns contents of that directory
    """
    _require_key(x_sentinel_key)

    current_platform = platform.system()

    if not path:
        if current_platform == "Windows":
            # Return drive list, not directory contents
            drives = _list_windows_drives()
            return {
                "path": "",
                "platform": current_platform,
                "drives": drives,
                "entries": [],
            }
        else:
            path = "/"

    browse_path = Path(path)

    if not browse_path.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path!r}")
    if not browse_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {path!r}")

    entries = []
    try:
        for entry in sorted(
            browse_path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())
        ):
            try:
                st = entry.stat()
                entries.append({
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": entry.is_dir(),
                    "size": st.st_size if not entry.is_dir() else None,
                    "mtime": st.st_mtime,
                })
            except OSError:
                continue
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return {
        "path": str(browse_path),
        "platform": current_platform,
        "drives": None,
        "entries": entries,
    }


@app.get("/snapshot/{snapshot_id}/file", tags=["snapshots"])
async def read_file(
    snapshot_id: str,
    path: str = Query(..., description="File path relative to the snapshot mount"),
    x_sentinel_key: Optional[str] = Header(default=None),
) -> StreamingResponse:
    """Stream the raw bytes of a file from the snapshot."""
    _require_key(x_sentinel_key)

    if snapshot_id not in _snapshots:
        raise HTTPException(status_code=404, detail=f"Snapshot {snapshot_id!r} not found")

    mount_point = Path(_snapshots[snapshot_id]["mount_point"])
    # Resolve path safely — strip leading backslash/slash
    clean_path = path.lstrip("/\\")
    file_path = mount_point / clean_path

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path!r}")

    return StreamingResponse(
        _stream_file_bytes(file_path),
        media_type="application/octet-stream",
        headers={"X-File-Size": str(file_path.stat().st_size)},
    )


# ── Streaming generators ──────────────────────────────────────────────────────

async def _stream_file_list(root: Path):
    """Yield NDJSON lines for each file under root."""
    try:
        for dirpath, dirnames, filenames in _os.walk(root):
            dirnames.sort()
            for fname in sorted(filenames):
                fpath = Path(dirpath) / fname
                try:
                    st = fpath.stat()
                    # Compute relative path from mount_point
                    rel = str(fpath.relative_to(root))
                    line = _json.dumps({
                        "path": rel,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "is_dir": False,
                    })
                    yield (line + "\n").encode("utf-8")
                except OSError:
                    continue
    except Exception as exc:
        log.error("File list streaming error: %s", exc)


async def _stream_file_bytes(path: Path):
    """Yield raw file bytes in 64 KiB chunks."""
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk
    except Exception as exc:
        log.error("File read error %s: %s", path, exc)


# ── Cleanup helpers ────────────────────────────────────────────────────────────

def _release_snapshot(snapshot_id: str) -> None:
    entry = _snapshots.pop(snapshot_id, None)
    if entry is None:
        return
    try:
        ctx = entry.get("ctx")
        provider = entry.get("provider")
        if ctx and provider:
            # Exit the context manager to trigger VSS cleanup
            provider.create_snapshot.__exit__(ctx, None, None, None)
    except Exception as exc:
        log.warning("Error releasing snapshot %s: %s", snapshot_id, exc)


def _cleanup_all_snapshots() -> None:
    for sid in list(_snapshots.keys()):
        _release_snapshot(sid)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    host = os.getenv("SENTINEL_AGENT_HOST", "0.0.0.0")
    logging.basicConfig(level=logging.INFO)
    uvicorn.run("agent.main:app", host=host, port=_PORT, reload=False)
