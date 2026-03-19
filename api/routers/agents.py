"""
Agents router — remote agent registration, heartbeat and management.

Endpoints
---------
GET    /api/agents                       List all registered agents (api_key hidden).
POST   /api/agents                       Create a new agent slot (returns raw api_key once).
GET    /api/agents/{agent_id}            Get one agent (api_key hidden).
PUT    /api/agents/{agent_id}            Update display_name / description / port.
DELETE /api/agents/{agent_id}            Delete agent slot (204).
POST   /api/agents/register              Agent calls this on startup to register.
POST   /api/agents/{agent_id}/heartbeat  Agent calls this every 30 s.
POST   /api/agents/{agent_id}/ping       Server probes agent's /health endpoint.
GET    /api/agents/{agent_id}/browse     Proxy directory listing from agent.
GET    /api/agents/{agent_id}/download   Download pre-configured installer ZIP.
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sentinel.agent_registry import AgentRegistry

log = logging.getLogger(__name__)

# ── Registry singleton ────────────────────────────────────────────────────────

_registry: AgentRegistry | None = None


def _get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        db_path = Path(os.getenv("SENTINEL_AGENTS_DB", "agents.db"))
        _registry = AgentRegistry(db_path)
    return _registry


# ── Background job (called by APScheduler every 2 minutes) ───────────────────

def _mark_stale_agents_job() -> None:
    """Background job — mark agents as offline if no heartbeat in 90s."""
    try:
        registry = _get_registry()
        count = registry.mark_stale(offline_after_s=90)
        if count > 0:
            log.info("Marked %d agent(s) as offline (no heartbeat)", count)
    except Exception as exc:
        log.error("Agent stale check failed: %s", exc)


# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/agents", tags=["agents"])

# ── Pydantic schemas ──────────────────────────────────────────────────────────


class CreateAgentRequest(BaseModel):
    display_name: str
    description: str = ""


class UpdateAgentRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    port: Optional[int] = None


class AgentRegisterRequest(BaseModel):
    agent_id: str
    api_key: str
    hostname: str
    os: str
    os_version: str = ""
    ip_address: str = ""
    port: int = 8765
    version: str = ""


class AgentHeartbeatRequest(BaseModel):
    api_key: str
    ip_address: str = ""
    port: int = 8765
    version: str = ""


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("")
async def list_agents(status: Optional[str] = None) -> dict:
    """
    Return all registered agent slots.

    Optional query param ``?status=online|offline|never_connected`` filters
    results.  The api_key field is always hidden in responses.
    """
    registry = _get_registry()
    agents = registry.list_agents(status_filter=status)
    return {"agents": agents, "total": len(agents)}


@router.post("", status_code=201)
async def create_agent(req: CreateAgentRequest) -> dict:
    """
    Create a new agent slot.

    The server generates the agent_id and api_key.  The raw api_key is
    returned **once** in this response and never again — store it securely.
    """
    registry = _get_registry()
    agent = registry.create_agent(
        display_name=req.display_name,
        description=req.description,
    )
    # Preserve raw api_key before hiding it in the "agent" payload.
    raw_key = agent["api_key"]
    safe_agent = {**agent, "api_key": "[hidden]"}
    return {"agent": safe_agent, "api_key": raw_key}


@router.post("/register")
async def register_agent(req: AgentRegisterRequest) -> dict:
    """
    Called by the Sentinel agent on startup.

    Validates the api_key, updates agent info and sets status='online'.
    Returns ``{"ok": True, "agent_id": "...", "interval_s": 30}`` on success.
    Returns HTTP 401 if the api_key doesn't match any registered agent slot.
    """
    registry = _get_registry()
    agent = registry.register_agent(
        agent_id=req.agent_id,
        api_key=req.api_key,
        hostname=req.hostname,
        os=req.os,
        os_version=req.os_version,
        ip=req.ip_address,
        port=req.port,
        version=req.version,
    )
    if agent is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid api_key for the given agent_id.",
        )
    return {"ok": True, "agent_id": req.agent_id, "interval_s": 30}


@router.get("/{agent_id}")
async def get_agent(agent_id: str) -> dict:
    """Return one agent by agent_id.  Returns 404 if not found.  api_key hidden."""
    registry = _get_registry()
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
    return agent


@router.put("/{agent_id}")
async def update_agent(agent_id: str, req: UpdateAgentRequest) -> dict:
    """Update display_name, description and/or port for an agent slot."""
    registry = _get_registry()
    if registry.get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

    fields: dict = {}
    if req.display_name is not None:
        fields["display_name"] = req.display_name
    if req.description is not None:
        fields["description"] = req.description
    if req.port is not None:
        fields["port"] = req.port

    agent = registry.update_agent(agent_id, **fields)
    return agent  # type: ignore[return-value]


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str) -> None:
    """Delete an agent slot.  Returns 204 No Content."""
    registry = _get_registry()
    deleted = registry.delete_agent(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")


@router.post("/{agent_id}/heartbeat")
async def agent_heartbeat(agent_id: str, req: AgentHeartbeatRequest) -> dict:
    """
    Called by the agent every 30 seconds to signal liveness.

    Returns HTTP 404 if agent_id not found, HTTP 401 if api_key doesn't match.
    """
    registry = _get_registry()
    # Verify agent exists first for a cleaner 404 vs 401 distinction.
    existing = registry.get_agent(agent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

    agent = registry.heartbeat(
        api_key=req.api_key,
        ip=req.ip_address,
        port=req.port,
        version=req.version,
    )
    if agent is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid api_key.",
        )
    return {"ok": True, "interval_s": 30}


@router.post("/{agent_id}/ping")
async def ping_agent(agent_id: str) -> dict:
    """
    Server-initiated reachability probe.

    Sends HTTP GET to ``http://{ip}:{port}/health`` with the outbound_token
    in the ``X-Sentinel-Key`` header (timeout 5 s).
    Returns ``{"reachable": True/False, "latency_ms": N, "agent": {...}}``.
    """
    registry = _get_registry()
    # Fetch full internal row (with outbound_token) for the HTTP call.
    row = registry._conn.execute(
        "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

    agent_dict = registry._safe_row(row)
    ip = row["ip_last_seen"]
    port = row["port"]
    token = row["outbound_token"]

    if not ip:
        return {"reachable": False, "latency_ms": None, "agent": agent_dict}

    url = f"http://{ip}:{port}/health"
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("X-Sentinel-Key", token)

    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {"reachable": True, "latency_ms": latency_ms, "agent": agent_dict}
    except Exception:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {"reachable": False, "latency_ms": latency_ms, "agent": agent_dict}


@router.get("/{agent_id}/download")
async def download_agent_installer(agent_id: str, request: Request) -> StreamingResponse:
    """
    Generate and serve a pre-configured installer ZIP for the given agent.

    The ZIP contains:
      - SentinelAgent-Setup.exe  (the Windows self-contained installer)
      - Install.bat              (launcher that passes --key and --server silently)

    The server URL is taken from the ``SENTINEL_PUBLIC_URL`` environment variable
    if set; otherwise it is derived from ``request.base_url``.

    Returns 404 if the agent is not found.
    Returns 503 if ``dist/SentinelAgent-Setup.exe`` has not been built yet.
    """
    registry = _get_registry()

    # Fetch full row to access api_key (not exposed in safe responses)
    row = registry._conn.execute(
        "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

    # Locate the pre-built installer (api/routers/agents.py → project root is 3 levels up)
    project_root = Path(__file__).parent.parent.parent
    exe_path = project_root / "dist" / "SentinelAgent-Setup.exe"

    if not exe_path.exists():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Installer not built yet",
                "instructions": "Run: python agent/build.py on a Windows machine",
            },
        )

    # Resolve server URL: env var takes priority over request.base_url
    public_url = os.getenv("SENTINEL_PUBLIC_URL", "").rstrip("/")
    if not public_url:
        public_url = str(request.base_url).rstrip("/")

    api_key: str = row["api_key"]
    display_name: str = row["display_name"] or agent_id

    # ── Build Install.bat ──────────────────────────────────────────────────────
    # Double-quotes inside bat: we use a simple variable assignment to avoid
    # issues with special characters in the key or URL.
    bat_lines = [
        "@echo off",
        f'set AGENT_KEY={api_key}',
        f'set SERVER_URL={public_url}',
        "",
        "echo Installing Sentinel Agent...",
        'SentinelAgent-Setup.exe --key "%AGENT_KEY%" --server "%SERVER_URL%" --silent',
        "",
        "if %errorlevel% neq 0 (",
        "    echo.",
        "    echo Installation failed. See output above for details.",
        "    pause",
        "    exit /b %errorlevel%",
        ")",
        "",
        "echo.",
        "echo Installation complete.",
        "pause",
    ]
    bat_content = "\r\n".join(bat_lines) + "\r\n"

    # ── Pack ZIP in memory ─────────────────────────────────────────────────────
    exe_data = exe_path.read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("SentinelAgent-Setup.exe")
        info.compress_type = zipfile.ZIP_STORED   # binary already compressed
        zf.writestr(info, exe_data)
        zf.writestr("Install.bat", bat_content.encode("utf-8"))
    zip_bytes = buf.getvalue()

    # ── Sanitize display name for the filename ─────────────────────────────────
    safe_name = (
        "".join(c if c.isalnum() or c in "-_ " else "-" for c in display_name)
        .strip()
        .replace(" ", "-")
    ) or agent_id[:8]
    filename = f"SentinelAgent-{safe_name}.zip"

    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{agent_id}/browse")
async def browse_agent(agent_id: str, path: str = "") -> dict:
    """
    Proxy a directory-listing request to the remote agent.

    Builds the URL ``http://{ip}:{port}/browse[?path={path}]`` and forwards
    the response JSON directly.  The outbound_token is sent as
    ``X-Sentinel-Key``.

    Returns HTTP 404 if agent not found.
    Returns HTTP 502 if the agent is unreachable or returns an error.
    """
    registry = _get_registry()
    row = registry._conn.execute(
        "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

    ip = row["ip_last_seen"]
    port = row["port"]
    token = row["outbound_token"]

    if not ip:
        raise HTTPException(
            status_code=502,
            detail="Agent has no known IP address yet — it has never connected.",
        )

    if path:
        encoded_path = urllib.parse.quote(path, safe="")
        url = f"http://{ip}:{port}/browse?path={encoded_path}"
    else:
        url = f"http://{ip}:{port}/browse"

    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("X-Sentinel-Key", token)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data
    except urllib.error.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Agent returned HTTP {exc.code}: {exc.reason}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Agent browse failed: {exc}",
        ) from exc
