"""
LAN Network Discovery Scanner — Sentinel Layer 1.

Scans an IP range concurrently using asyncio TCP probes to detect Windows
machines and Sentinel Windows agents.

Probed ports
------------
135  — Microsoft RPC / WMI (Windows-specific)
139  — NetBIOS Session Service (Windows-specific)
445  — SMB (Windows-specific; also Linux Samba)
7700 — Sentinel Windows Agent (our custom port)

A host is classified as "windows_likely" when at least one of ports
135/139/445 responds.  ``agent_detected`` is set when port 7700 also
responds and the /health endpoint returns the expected JSON.

Usage
-----
    from sentinel.discovery.scanner import NetworkScanner

    scanner = NetworkScanner("192.168.1.0/24")
    async for machine in scanner.scan():
        print(machine)

    # Or collect all at once
    machines = await scanner.scan_all()
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, List, Optional

log = logging.getLogger(__name__)

# Port → label mapping
_WINDOWS_PORTS = {135: "rpc", 139: "netbios", 445: "smb"}
_AGENT_PORT = 7700
_PROBE_TIMEOUT = 0.8       # seconds per TCP connect attempt
_AGENT_HTTP_TIMEOUT = 2.0  # seconds for HTTP health check
_MAX_CONCURRENCY = 64      # simultaneous probes


# ------------------------------------------------------------------ #
#  Data model                                                          #
# ------------------------------------------------------------------ #

@dataclass
class MachineRecord:
    """Represents a discovered host on the local network."""

    ip: str
    hostname: Optional[str] = None
    open_ports: List[int] = field(default_factory=list)

    # Classification
    windows_likely: bool = False
    smb_available: bool = False
    agent_detected: bool = False
    agent_version: Optional[str] = None

    # Agent API key (optional; must match SENTINEL_AGENT_KEY on the Windows agent)
    agent_key: Optional[str] = None

    # SMB credentials (set by the user after discovery)
    smb_username: Optional[str] = None
    smb_password: Optional[str] = None
    smb_domain: Optional[str] = None

    # Discovery metadata
    last_seen: float = field(default_factory=time.time)
    response_ms: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "ip": self.ip,
            "hostname": self.hostname,
            "open_ports": self.open_ports,
            "windows_likely": self.windows_likely,
            "smb_available": self.smb_available,
            "agent_detected": self.agent_detected,
            "agent_version": self.agent_version,
            "agent_key": self.agent_key,
            "last_seen": self.last_seen,
            "response_ms": self.response_ms,
        }


# ------------------------------------------------------------------ #
#  Scanner                                                             #
# ------------------------------------------------------------------ #

class NetworkScanner:
    """
    Async LAN scanner for Windows machines and Sentinel agents.

    Args:
        network:     CIDR notation, e.g. ``"192.168.1.0/24"``.
                     Default: auto-detect from the host's primary interface.
        timeout:     TCP connect timeout in seconds.
        concurrency: Maximum simultaneous probes.
    """

    def __init__(
        self,
        network: Optional[str] = None,
        timeout: float = _PROBE_TIMEOUT,
        concurrency: int = _MAX_CONCURRENCY,
    ) -> None:
        self._network = network or _detect_local_network()
        self._timeout = timeout
        self._concurrency = concurrency

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def scan_all(self) -> List[MachineRecord]:
        """Scan the entire range and return all discovered Windows machines."""
        results: List[MachineRecord] = []
        async for machine in self.scan():
            results.append(machine)
        return results

    async def scan(self) -> AsyncIterator[MachineRecord]:
        """
        Async generator — yields MachineRecord as each host is discovered.

        Only hosts with at least one open port are yielded.
        """
        try:
            net = ipaddress.ip_network(self._network, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid network: {self._network!r}: {exc}") from exc

        hosts = list(net.hosts())
        log.info("Scanning %s (%d hosts) …", self._network, len(hosts))

        sem = asyncio.Semaphore(self._concurrency)
        queue: asyncio.Queue[Optional[MachineRecord]] = asyncio.Queue()

        async def probe_and_enqueue(ip: str) -> None:
            async with sem:
                record = await self._probe_host(ip)
                if record is not None:
                    await queue.put(record)

        tasks = [asyncio.create_task(probe_and_enqueue(str(h))) for h in hosts]

        # Gather while yielding results as they arrive
        gather_task = asyncio.create_task(_gather_all(tasks))

        pending = len(tasks)
        while pending > 0 or not queue.empty():
            try:
                record = queue.get_nowait()
                yield record
            except asyncio.QueueEmpty:
                if gather_task.done():
                    # Drain remaining
                    while not queue.empty():
                        yield queue.get_nowait()
                    break
                await asyncio.sleep(0.05)

        await gather_task  # propagate any unexpected exceptions
        log.info("Scan of %s complete — %d Windows host(s) found", self._network, 0)

    # ------------------------------------------------------------------ #
    #  Per-host probe                                                      #
    # ------------------------------------------------------------------ #

    async def _probe_host(self, ip: str) -> Optional[MachineRecord]:
        """Probe a single IP for all relevant ports. Returns None if host is silent."""
        t0 = time.monotonic()

        # Probe all ports concurrently
        ports_to_probe = list(_WINDOWS_PORTS.keys()) + [_AGENT_PORT]
        results = await asyncio.gather(
            *[self._tcp_probe(ip, p) for p in ports_to_probe],
            return_exceptions=True,
        )

        open_ports = [
            port for port, ok in zip(ports_to_probe, results)
            if ok is True
        ]

        if not open_ports:
            return None

        elapsed_ms = (time.monotonic() - t0) * 1000

        # Classify
        windows_ports_open = [p for p in open_ports if p in _WINDOWS_PORTS]
        smb_available = 445 in open_ports
        agent_detected = _AGENT_PORT in open_ports

        record = MachineRecord(
            ip=ip,
            open_ports=open_ports,
            windows_likely=len(windows_ports_open) > 0,
            smb_available=smb_available,
            agent_detected=agent_detected,
            last_seen=time.time(),
            response_ms=round(elapsed_ms, 1),
        )

        # Reverse DNS (best-effort, non-blocking via thread executor)
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, socket.gethostbyaddr, ip)
            record.hostname = info[0]
        except Exception:
            pass

        # Verify agent and get version via HTTP /health
        if agent_detected:
            record.agent_version = await self._probe_agent(ip)
            if record.agent_version is None:
                # Port open but not our agent
                record.agent_detected = False

        log.debug("Found host %s (%s) ports=%s", ip, record.hostname or "?", open_ports)
        return record

    async def _tcp_probe(self, ip: str, port: int) -> bool:
        """Return True if a TCP connection to ip:port succeeds within timeout."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port),
                timeout=self._timeout,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False

    async def _probe_agent(self, ip: str) -> Optional[str]:
        """
        Try to reach the Sentinel Windows Agent health endpoint.
        Returns the agent version string on success, None on failure.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, _AGENT_PORT),
                timeout=_AGENT_HTTP_TIMEOUT,
            )
            # Send minimal HTTP GET
            request = (
                f"GET /health HTTP/1.0\r\n"
                f"Host: {ip}\r\n"
                f"Connection: close\r\n\r\n"
            )
            writer.write(request.encode())
            await writer.drain()

            response = b""
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=2.0)
                if not chunk:
                    break
                response += chunk
                if len(response) > 8192:
                    break

            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

            # Parse HTTP response body (JSON)
            if b"200" in response[:20] and b"sentinel" in response.lower():
                import json as _json
                body = response.split(b"\r\n\r\n", 1)[-1]
                data = _json.loads(body.decode("utf-8", errors="replace"))
                return data.get("version", "unknown")

        except Exception:
            pass
        return None


# ------------------------------------------------------------------ #
#  Utilities                                                           #
# ------------------------------------------------------------------ #

def _detect_local_network() -> str:
    """
    Detect the primary LAN network in CIDR notation.

    Connects a UDP socket to an external address (no packets sent) to
    determine the outbound interface IP, then assumes a /24 mask.
    Falls back to 192.168.1.0/24.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        # Build /24 from the first three octets
        parts = local_ip.split(".")
        return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    except Exception:
        return "192.168.1.0/24"


async def _gather_all(tasks: list) -> None:
    """Wait for all tasks, ignoring individual errors."""
    await asyncio.gather(*tasks, return_exceptions=True)
