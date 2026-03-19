"""
Machines router — LAN discovery, Windows machine management, SMB/Agent backup.

Endpoints
---------
GET  /api/machines                    List known/discovered machines.
POST /api/machines/scan               Start a background LAN scan.
GET  /api/machines/scan/status        Current scan status.
GET  /api/machines/agent/info         Agent package metadata (version, size, filename).
GET  /api/machines/agent/download     Download the agent installer ZIP.
GET  /api/machines/{ip}               Details for one machine.
PUT  /api/machines/{ip}/credentials   Save SMB credentials for a machine.
PUT  /api/machines/{ip}/agent-key     Save the agent API key for a machine.
POST /api/machines/{ip}/ping          Re-probe a single machine.
DELETE /api/machines/{ip}             Forget a machine.
POST /api/machines/{ip}/create-job    Auto-create a backup job for this machine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from sentinel.discovery.scanner import MachineRecord, NetworkScanner, _detect_local_network

log = logging.getLogger(__name__)

# ── Agent package path ────────────────────────────────────────────────────────

def _agent_package_path() -> Optional[Path]:
    """
    Return the path to the agent installer ZIP, or None if it doesn't exist.

    Looks in:  <project_root>/dist/SentinelAgent-installer.zip
    """
    # Walk up from this file to find the project root
    here = Path(__file__).resolve()
    for parent in [here.parent.parent.parent, here.parent.parent]:
        candidate = parent / "dist" / "SentinelAgent-installer.zip"
        if candidate.exists():
            return candidate
    return None

router = APIRouter(prefix="/api/machines", tags=["machines"])

# ── Persistence ───────────────────────────────────────────────────────────────

_MACHINES_FILE = Path(os.getenv("SENTINEL_MACHINES_FILE", "machines.json"))


def _load_machines() -> Dict[str, MachineRecord]:
    if not _MACHINES_FILE.exists():
        return {}
    try:
        raw = json.loads(_MACHINES_FILE.read_text())
        return {ip: MachineRecord(**data) for ip, data in raw.items()}
    except Exception as exc:
        log.warning("Failed to load machines file: %s", exc)
        return {}


def _save_machines(machines: Dict[str, MachineRecord]) -> None:
    try:
        _MACHINES_FILE.write_text(
            json.dumps({ip: m.as_dict() for ip, m in machines.items()}, indent=2)
        )
    except Exception as exc:
        log.warning("Failed to save machines file: %s", exc)


# In-memory store (loaded at import time)
_machines: Dict[str, MachineRecord] = _load_machines()

# ── Scan state ────────────────────────────────────────────────────────────────

@dataclass
class ScanState:
    running: bool = False
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    network: Optional[str] = None
    hosts_found: int = 0
    error: Optional[str] = None


_scan_state = ScanState()


# ── Schemas ───────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    network: Optional[str] = None  # CIDR, e.g. "192.168.1.0/24". Auto-detected if None.
    timeout: float = 0.8


class CredentialsRequest(BaseModel):
    username: str
    password: str
    domain: Optional[str] = None


class AgentKeyRequest(BaseModel):
    agent_key: str


class CreateJobRequest(BaseModel):
    job_id: str
    share: str = "C$"               # SMB share name (default: admin share)
    source_type: str = "smb"        # "smb" | "agent"
    passphrase_hint: Optional[str] = None
    agent_paths: List[str] = []     # specific paths for agent backup (empty = whole volume)
    storage_base_path: Optional[str] = None   # override default local storage path
    storage_provider: str = "local" # "local" | "s3"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def list_machines() -> dict:
    """Return all known machines (persisted + discovered)."""
    return {
        "machines": [m.as_dict() for m in _machines.values()],
        "total": len(_machines),
    }


@router.post("/scan", status_code=202)
async def start_scan(req: ScanRequest, background_tasks: BackgroundTasks) -> dict:
    """
    Trigger a background LAN scan.

    Resolves Windows hosts by probing ports 135, 139, 445 and the Sentinel
    agent port 7700.  Results are merged into the persistent machines store.
    """
    if _scan_state.running:
        raise HTTPException(status_code=409, detail="Scan already in progress")

    network = req.network or _detect_local_network()
    _scan_state.running = True
    _scan_state.started_at = time.time()
    _scan_state.finished_at = None
    _scan_state.network = network
    _scan_state.hosts_found = 0
    _scan_state.error = None

    background_tasks.add_task(_run_scan, network, req.timeout)
    return {"status": "scanning", "network": network}


@router.get("/scan/status")
async def scan_status() -> dict:
    """Return the status of the most recent scan."""
    return {
        "running": _scan_state.running,
        "network": _scan_state.network,
        "started_at": _scan_state.started_at,
        "finished_at": _scan_state.finished_at,
        "hosts_found": _scan_state.hosts_found,
        "error": _scan_state.error,
    }


@router.get("/agent/info")
async def agent_info() -> dict:
    """Return metadata about the downloadable agent package."""
    pkg = _agent_package_path()
    if pkg is None:
        raise HTTPException(
            status_code=404,
            detail="Agent package not built yet. Run: python agent/build_package.py",
        )
    stat = pkg.stat()
    return {
        "version": "0.1.0",
        "filename": pkg.name,
        "size_bytes": stat.st_size,
        "built_at": stat.st_mtime,
        "download_url": "/api/machines/agent/download",
    }


@router.get("/agent/download")
async def agent_download():
    """Download the Sentinel Windows Agent installer ZIP."""
    pkg = _agent_package_path()
    if pkg is None:
        raise HTTPException(
            status_code=404,
            detail="Agent package not built yet. Run: python agent/build_package.py",
        )
    return FileResponse(
        path=str(pkg),
        filename=pkg.name,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{pkg.name}"'},
    )


@router.get("/{ip}")
async def get_machine(ip: str) -> dict:
    """Return details for a specific machine by IP."""
    if ip not in _machines:
        raise HTTPException(status_code=404, detail=f"Machine {ip!r} not found")
    return _machines[ip].as_dict()


@router.put("/{ip}/credentials")
async def set_credentials(ip: str, req: CredentialsRequest) -> dict:
    """Save SMB credentials for a machine (stored in memory + machines.json)."""
    if ip not in _machines:
        # Allow adding machines manually
        _machines[ip] = MachineRecord(ip=ip)

    _machines[ip].smb_username = req.username
    _machines[ip].smb_password = req.password
    _machines[ip].smb_domain = req.domain
    _save_machines(_machines)
    return {"ok": True, "ip": ip}


@router.put("/{ip}/agent-key")
async def set_agent_key(ip: str, req: AgentKeyRequest) -> dict:
    """Save the agent API key for a machine (must match SENTINEL_AGENT_KEY on the Windows agent)."""
    if ip not in _machines:
        _machines[ip] = MachineRecord(ip=ip)

    _machines[ip].agent_key = req.agent_key
    _save_machines(_machines)
    return {"ok": True, "ip": ip}


@router.post("/{ip}/ping")
async def ping_machine(ip: str) -> dict:
    """Re-probe a single machine to refresh its status."""
    scanner = NetworkScanner(network=f"{ip}/32", timeout=1.5)
    record = await scanner._probe_host(ip)

    if record is None:
        # Host didn't respond — update last_seen if known
        if ip in _machines:
            _machines[ip].open_ports = []
            _machines[ip].windows_likely = False
            _machines[ip].smb_available = False
            _machines[ip].agent_detected = False
            _save_machines(_machines)
        return {"ip": ip, "reachable": False}

    # Merge — preserve stored credentials and agent key
    if ip in _machines:
        record.smb_username = _machines[ip].smb_username
        record.smb_password = _machines[ip].smb_password
        record.smb_domain = _machines[ip].smb_domain
        record.agent_key = _machines[ip].agent_key
    _machines[ip] = record
    _save_machines(_machines)
    return {**record.as_dict(), "reachable": True}


@router.get("/{ip}/browse")
async def browse_machine(ip: str, path: str = "C:\\") -> dict:
    """
    Proxy a directory listing from the Sentinel agent running on the machine.

    Used by the web UI folder picker when configuring a new agent-based job.
    """
    if ip not in _machines:
        raise HTTPException(status_code=404, detail=f"Machine {ip!r} not found")

    machine = _machines[ip]
    if not machine.agent_detected:
        raise HTTPException(
            status_code=422,
            detail=f"Sentinel agent not detected on {ip}. Scan first.",
        )

    import urllib.request
    import urllib.parse

    agent_port = 7700
    encoded_path = urllib.parse.quote(path)
    url = f"http://{ip}:{agent_port}/browse?path={encoded_path}"

    try:
        req = urllib.request.Request(url, method="GET")
        if machine.agent_key:
            req.add_header("X-Sentinel-Key", machine.agent_key)
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json as _json
            data = _json.loads(resp.read())
            return data
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Agent browse failed: {exc}",
        ) from exc


@router.delete("/{ip}", status_code=204)
async def delete_machine(ip: str) -> None:
    """Remove a machine from the known list."""
    _machines.pop(ip, None)
    _save_machines(_machines)


@router.post("/{ip}/create-job", status_code=201)
async def create_machine_job(ip: str, req: CreateJobRequest) -> dict:
    """
    Auto-create a Sentinel backup job for this machine.

    For SMB source_type:  creates a job pointing to \\\\ip\\share
    For agent source_type: creates a job using the RemoteWindowsProvider (VSS)

    The job JSON is written to SENTINEL_JOBS_DIR and immediately usable.
    """
    machine = _machines.get(ip)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Machine {ip!r} not found")

    # Validate job_id
    import re
    if not re.match(r"^[a-zA-Z0-9_-]+$", req.job_id):
        raise HTTPException(status_code=422, detail="job_id must be alphanumeric + hyphens/underscores")

    jobs_dir = Path(os.getenv("SENTINEL_JOBS_DIR", "jobs"))
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_path = jobs_dir / f"{req.job_id}.json"

    if job_path.exists():
        raise HTTPException(status_code=409, detail=f"Job '{req.job_id}' already exists")

    if req.source_type == "smb":
        if not machine.smb_username:
            raise HTTPException(
                status_code=422,
                detail="SMB credentials required. Set them via PUT /api/machines/{ip}/credentials first.",
            )
        unc_path = f"\\\\{ip}\\{req.share}"
        job = _build_smb_job(req.job_id, ip, unc_path, machine, req.storage_base_path)

    elif req.source_type == "agent":
        if not machine.agent_detected:
            raise HTTPException(
                status_code=422,
                detail="Sentinel agent not detected on this machine.",
            )
        job = _build_agent_job(req.job_id, ip, machine, req.agent_paths, req.storage_base_path)

    else:
        raise HTTPException(status_code=422, detail=f"Unknown source_type: {req.source_type!r}")

    job_path.write_text(json.dumps(job, indent=2))
    log.info("Created job %s for machine %s (%s)", req.job_id, ip, req.source_type)

    return {"job_id": req.job_id, "ip": ip, "source_type": req.source_type, "job_file": str(job_path)}


# ── Background scan task ──────────────────────────────────────────────────────

async def _run_scan(network: str, timeout: float) -> None:
    """Background coroutine: scan and merge results into _machines."""
    global _scan_state
    try:
        scanner = NetworkScanner(network=network, timeout=timeout)
        count = 0
        async for record in scanner.scan():
            ip = record.ip
            if ip in _machines:
                # Preserve credentials and agent key, update everything else
                record.smb_username = _machines[ip].smb_username
                record.smb_password = _machines[ip].smb_password
                record.smb_domain = _machines[ip].smb_domain
                record.agent_key = _machines[ip].agent_key
            _machines[ip] = record
            count += 1
            _scan_state.hosts_found = count

        _save_machines(_machines)
        log.info("LAN scan complete: %d Windows host(s) found in %s", count, network)

    except Exception as exc:
        log.error("LAN scan failed: %s", exc)
        _scan_state.error = str(exc)
    finally:
        _scan_state.running = False
        _scan_state.finished_at = time.time()


# ── Job template builders ─────────────────────────────────────────────────────

def _build_smb_job(
    job_id: str,
    ip: str,
    unc_path: str,
    machine: MachineRecord,
    storage_base_path: Optional[str] = None,
) -> dict:
    """Build a job config JSON dict for agentless SMB backup."""
    hostname = machine.hostname or ip
    return {
        "job_id": job_id,
        "source_paths": [unc_path],
        "catalog_path": f"sentinel_catalog_{job_id}.db",
        "exclusions": [
            "*.tmp", "*.temp", "~$*",
            "Thumbs.db", "desktop.ini",
            "pagefile.sys", "hiberfil.sys", "swapfile.sys",
        ],
        "compression_level": 3,
        "max_workers": 4,
        "storage": {
            "provider": "local",
            "base_path": storage_base_path or f"store_{job_id}",
        },
        "retention": {"daily": 7, "weekly": 4, "monthly": 12},
        "metadata": {
            "machine_ip": ip,
            "machine_hostname": hostname,
            "source_type": "smb",
            "smb_share": unc_path,
            "smb_username": machine.smb_username,
            "smb_domain": machine.smb_domain,
        },
    }


def _build_agent_job(
    job_id: str,
    ip: str,
    machine: MachineRecord,
    agent_paths: Optional[List[str]] = None,
    storage_base_path: Optional[str] = None,
) -> dict:
    """Build a job config JSON dict for agent-based VSS backup."""
    hostname = machine.hostname or ip

    # Build source_paths from selected agent paths, or default to full C: drive
    if agent_paths:
        source_paths = [f"agent://{ip}:7700/{p.lstrip('/\\')}" for p in agent_paths]
    else:
        source_paths = [f"agent://{ip}:7700/C:"]

    return {
        "job_id": job_id,
        "source_paths": source_paths,
        "catalog_path": f"sentinel_catalog_{job_id}.db",
        "exclusions": [
            "*.tmp", "*.temp", "~$*",
            "Thumbs.db", "desktop.ini",
            "pagefile.sys", "hiberfil.sys", "swapfile.sys",
            "C:\\Windows\\Temp\\*",
            "C:\\$Recycle.Bin\\*",
        ],
        "compression_level": 3,
        "max_workers": 4,
        "storage": {
            "provider": "local",
            "base_path": storage_base_path or f"store_{job_id}",
        },
        "retention": {"daily": 7, "weekly": 4, "monthly": 12},
        "metadata": {
            "machine_ip": ip,
            "machine_hostname": hostname,
            "source_type": "agent",
            "agent_port": 7700,
            "agent_version": machine.agent_version,
            "agent_paths": agent_paths or [],
        },
    }
