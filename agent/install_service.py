"""
Sentinel Agent — Windows Installer & Service Runner.

This is the single entry point for the PyInstaller-compiled SentinelAgent-Setup.exe.

Execution modes
---------------
  SentinelAgent-Setup.exe                                   # Interactive installer
  SentinelAgent-Setup.exe --key KEY --server URL            # Pre-configured installer
  SentinelAgent-Setup.exe --key KEY --server URL --silent   # Unattended / silent
  SentinelAgent-Setup.exe --uninstall                       # Remove service + files
  SentinelAgent-Setup.exe --status                          # Show service status
  SentinelAgent-Setup.exe --run-service                     # Internal: called by NSSM/SCM

Installation steps
------------------
  1. Verify Administrator privileges (re-launch with UAC if needed)
  2. Collect SENTINEL_AGENT_KEY and SENTINEL_SERVER_URL
  3. Create C:\\SentinelAgent\\
  4. Copy this executable to C:\\SentinelAgent\\SentinelAgent.exe
  5. Write C:\\SentinelAgent\\.env with credentials
  6. Extract bundled nssm_win64.exe to C:\\SentinelAgent\\nssm.exe
  7. Register Windows Service via NSSM (points back to this exe --run-service)
  8. Open TCP port 8765 in Windows Firewall
  9. Start service + verify /health endpoint
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_IS_FROZEN = getattr(sys, "frozen", False)
_IS_WINDOWS = platform.system() == "Windows"

# ── Constants ─────────────────────────────────────────────────────────────────

INSTALL_DIR = Path(r"C:\SentinelAgent")
SERVICE_NAME = "SentinelAgent"
SERVICE_DISPLAY = "Sentinel Backup Agent"
SERVICE_DESC = (
    "Sentinel Backup Agent — provides VSS snapshot creation and "
    "file streaming for remote backup by the Sentinel server."
)
AGENT_PORT = 8765
EXE_NAME = "SentinelAgent.exe"

log = logging.getLogger(__name__)


# ── Admin helpers ──────────────────────────────────────────────────────────────

def _is_admin() -> bool:
    """Return True if the process has Administrator privileges."""
    if not _IS_WINDOWS:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_as_admin(extra_args: list[str]) -> None:
    """Re-launch the current executable elevated via UAC ShellExecuteW."""
    import ctypes
    exe = sys.executable
    params = subprocess.list2cmdline(extra_args)
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    if ret <= 32:
        print(f"  ERROR: UAC elevation failed (code {ret}).")
        print("  Please right-click the installer and choose 'Run as administrator'.")
        sys.exit(1)
    sys.exit(0)


# ── NSSM helper ───────────────────────────────────────────────────────────────

def _get_bundled_nssm() -> Path:
    """
    Locate the bundled nssm_win64.exe.
    When frozen with PyInstaller --onefile, data files land in sys._MEIPASS.
    In dev mode, fall back to dist/nssm_win64.exe relative to the project root.
    """
    if _IS_FROZEN:
        return Path(sys._MEIPASS) / "nssm_win64.exe"
    # Development fallback
    return Path(__file__).parent.parent / "dist" / "nssm_win64.exe"


# ── Pretty output ─────────────────────────────────────────────────────────────

def _banner() -> None:
    print()
    print("  +================================================+")
    print("  |        Sentinel Backup Agent — Installer       |")
    print("  +================================================+")
    print()


def _step(n: int, total: int, msg: str) -> None:
    print(f"  [{n}/{total}] {msg}")


def _ok(msg: str) -> None:
    print(f"         OK  {msg}")


def _err(msg: str) -> None:
    print(f"         ERR {msg}", file=sys.stderr)


def _warn(msg: str) -> None:
    print(f"         WARN {msg}")


# ── Subprocess helper ──────────────────────────────────────────────────────────

def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


# ── Installer ─────────────────────────────────────────────────────────────────

def run_installer(key: str, server: str, silent: bool) -> int:
    """
    Full installation flow.  Returns 0 on success, non-zero on failure.
    """
    _banner()
    total = 9

    # ── 1. Admin ───────────────────────────────────────────────────────────────
    _step(1, total, "Checking Administrator privileges ...")
    if not _is_admin():
        print("         Not running as Administrator.")
        print("         Requesting UAC elevation — approve the Windows prompt.")
        _relaunch_as_admin(sys.argv[1:])   # does not return

    _ok("Running as Administrator.")

    # ── 2. Credentials ────────────────────────────────────────────────────────
    _step(2, total, "Collecting configuration ...")
    if not key:
        if silent:
            _err("--key is required for silent installation.")
            return 1
        key = input("         SENTINEL_AGENT_KEY (from Sentinel UI → Agents): ").strip()
    if not server:
        if silent:
            _err("--server is required for silent installation.")
            return 1
        server = input("         SENTINEL_SERVER_URL (e.g. http://192.168.1.87:8000): ").strip()

    if not key:
        _err("SENTINEL_AGENT_KEY cannot be empty.")
        return 1
    if not server:
        _err("SENTINEL_SERVER_URL cannot be empty.")
        return 1

    server = server.rstrip("/")
    _ok(f"Server: {server}")
    _ok(f"Key:    {'*' * min(len(key), 8)}... ({len(key)} chars)")

    # ── 3. Create install directory ────────────────────────────────────────────
    _step(3, total, f"Creating {INSTALL_DIR} ...")
    try:
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        _ok(str(INSTALL_DIR))
    except OSError as exc:
        _err(f"Cannot create directory: {exc}")
        return 1

    # ── 4. Copy executable ─────────────────────────────────────────────────────
    _step(4, total, f"Copying {EXE_NAME} to install directory ...")
    dest_exe = INSTALL_DIR / EXE_NAME
    if _IS_FROZEN:
        src_exe = Path(sys.executable).resolve()
        if src_exe != dest_exe.resolve():
            try:
                shutil.copy2(src_exe, dest_exe)
                _ok(str(dest_exe))
            except OSError as exc:
                _err(f"Copy failed: {exc}")
                return 1
        else:
            _ok("Already in install directory.")
    else:
        # Dev mode: exe copy not applicable
        _ok("(dev mode — skipping exe copy)")

    # ── 5. Write .env ──────────────────────────────────────────────────────────
    _step(5, total, "Writing .env ...")
    env_path = INSTALL_DIR / ".env"
    try:
        env_path.write_text(
            f"SENTINEL_AGENT_KEY={key}\n"
            f"SENTINEL_SERVER_URL={server}\n"
            f"SENTINEL_AGENT_PORT={AGENT_PORT}\n",
            encoding="utf-8",
        )
        # Restrict read access to Administrators / SYSTEM only
        _run(["icacls", str(env_path), "/inheritance:r",
              "/grant:r", "Administrators:(R)",
              "/grant:r", "SYSTEM:(R)"], check=False)
        _ok(str(env_path))
    except OSError as exc:
        _err(f"Cannot write .env: {exc}")
        return 1

    # ── 6. Extract NSSM ────────────────────────────────────────────────────────
    _step(6, total, "Extracting bundled NSSM ...")
    nssm_src = _get_bundled_nssm()
    nssm_dst = INSTALL_DIR / "nssm.exe"
    if not nssm_src.exists():
        _err(
            f"Bundled NSSM not found at {nssm_src}.\n"
            "         Rebuild the installer with nssm_win64.exe present in dist/."
        )
        return 1
    try:
        if nssm_src.resolve() != nssm_dst.resolve():
            shutil.copy2(nssm_src, nssm_dst)
        _ok(str(nssm_dst))
    except OSError as exc:
        _err(f"Cannot copy NSSM: {exc}")
        return 1

    nssm = str(nssm_dst)

    # ── 7. Register Windows Service ────────────────────────────────────────────
    _step(7, total, f"Registering Windows Service '{SERVICE_NAME}' ...")
    try:
        # Stop + remove any prior installation
        _run([nssm, "stop", SERVICE_NAME], check=False)
        _run([nssm, "remove", SERVICE_NAME, "confirm"], check=False)
        time.sleep(1)

        svc_exe = str(dest_exe) if _IS_FROZEN else str(Path(sys.executable))

        _run([nssm, "install",     SERVICE_NAME, svc_exe])
        _run([nssm, "set",         SERVICE_NAME, "AppParameters",   "--run-service"])
        _run([nssm, "set",         SERVICE_NAME, "AppDirectory",    str(INSTALL_DIR)])
        _run([nssm, "set",         SERVICE_NAME, "DisplayName",     SERVICE_DISPLAY])
        _run([nssm, "set",         SERVICE_NAME, "Description",     SERVICE_DESC])
        _run([nssm, "set",         SERVICE_NAME, "Start",           "SERVICE_AUTO_START"])
        # Inject credentials directly into the service environment
        _run([nssm, "set",         SERVICE_NAME, "AppEnvironmentExtra",
              f"SENTINEL_AGENT_KEY={key}",
              f"SENTINEL_SERVER_URL={server}",
              f"SENTINEL_AGENT_PORT={AGENT_PORT}"])
        _run([nssm, "set",         SERVICE_NAME, "AppStdout",       str(INSTALL_DIR / "agent.log")])
        _run([nssm, "set",         SERVICE_NAME, "AppStderr",       str(INSTALL_DIR / "agent-error.log")])
        _run([nssm, "set",         SERVICE_NAME, "AppRotateFiles",  "1"])
        _run([nssm, "set",         SERVICE_NAME, "AppRotateBytes",  "10485760"])
        _ok(f"Service '{SERVICE_NAME}' registered (auto-start).")
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        _err(f"NSSM command failed: {stderr or exc}")
        return 1

    # ── 8. Firewall ────────────────────────────────────────────────────────────
    _step(8, total, f"Opening firewall port {AGENT_PORT}/TCP ...")
    try:
        # Remove stale rule (ignore if absent)
        _run([
            "netsh", "advfirewall", "firewall", "delete", "rule",
            f"name=Sentinel Agent",
        ], check=False)
        _run([
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name=Sentinel Agent",
            "dir=in", "action=allow", "protocol=TCP",
            f"localport={AGENT_PORT}",
            "profile=any",
            f"description=Sentinel Backup Agent on port {AGENT_PORT}",
        ])
        _ok(f"Port {AGENT_PORT} open.")
    except subprocess.CalledProcessError as exc:
        _warn(
            f"Firewall rule failed ({exc.returncode}). "
            f"Open port {AGENT_PORT} manually if remote connections are needed."
        )

    # ── 9. Start + health check ────────────────────────────────────────────────
    _step(9, total, "Starting service and verifying health endpoint ...")
    try:
        _run([nssm, "start", SERVICE_NAME])
    except subprocess.CalledProcessError as exc:
        _err(f"Service start failed: {(exc.stderr or '').strip() or exc}")
        print("         Check agent-error.log in", INSTALL_DIR)
        return 1

    health_url = f"http://localhost:{AGENT_PORT}/health"
    deadline = time.time() + 20
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            with urllib.request.urlopen(health_url, timeout=3) as resp:
                if resp.status == 200:
                    _ok(f"Agent responded at {health_url}")
                    break
        except Exception:
            print(f"         Waiting for agent ({attempt})...", end="\r", flush=True)
            time.sleep(1)
    else:
        print()
        _err(f"Agent did not respond at {health_url} within 20 seconds.")
        print(f"         Check {INSTALL_DIR / 'agent-error.log'}")
        return 1

    print()
    print("  +================================================+")
    print("  |           Instalación completada              |")
    print("  +================================================+")
    print()
    print(f"  Agent running:   http://localhost:{AGENT_PORT}/health")
    print(f"  Install dir:     {INSTALL_DIR}")
    print(f"  Logs:            {INSTALL_DIR / 'agent.log'}")
    print()
    print("  → Go to Sentinel UI → Agents to verify the connection.")
    print()

    if not silent:
        input("  Press Enter to exit...")

    return 0


# ── Uninstaller ───────────────────────────────────────────────────────────────

def run_uninstaller() -> int:
    """Stop and remove the service, firewall rule, and install directory."""
    print()
    print("  Uninstalling Sentinel Agent ...")
    print()

    if not _is_admin():
        print("  Requesting UAC elevation ...")
        _relaunch_as_admin(["--uninstall"])   # does not return

    nssm = str(INSTALL_DIR / "nssm.exe")

    if (INSTALL_DIR / "nssm.exe").exists():
        _run([nssm, "stop",   SERVICE_NAME], check=False)
        _run([nssm, "remove", SERVICE_NAME, "confirm"], check=False)
    else:
        # Fallback: use sc.exe if NSSM is gone
        _run(["sc", "stop",   SERVICE_NAME], check=False)
        _run(["sc", "delete", SERVICE_NAME], check=False)

    # Remove firewall rule
    _run([
        "netsh", "advfirewall", "firewall", "delete", "rule",
        f"name=Sentinel Agent",
    ], check=False)

    # Wait for service process to fully stop before removing files
    time.sleep(3)
    try:
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)
        print(f"  Removed {INSTALL_DIR}")
    except OSError as exc:
        print(f"  WARNING: Could not fully remove {INSTALL_DIR}: {exc}")
        print("  Delete it manually after rebooting.")

    print("  Uninstall complete.")
    return 0


# ── Status ────────────────────────────────────────────────────────────────────

def run_status() -> int:
    """Print the current Windows Service status."""
    result = _run(["sc", "query", SERVICE_NAME], check=False)
    output = (result.stdout or result.stderr or "").strip()
    print(output if output else f"Service '{SERVICE_NAME}' not found.")

    # Also hit /health if the service is running
    health_url = f"http://localhost:{AGENT_PORT}/health"
    try:
        with urllib.request.urlopen(health_url, timeout=3) as resp:
            import json
            data = json.loads(resp.read().decode())
            print(f"\nHealth: {health_url}")
            for k, v in data.items():
                print(f"  {k}: {v}")
    except Exception:
        print(f"\nHealth endpoint not reachable: {health_url}")
    return 0


# ── Service runner ────────────────────────────────────────────────────────────

def run_service() -> int:
    """
    Start the FastAPI agent in-process.  Called by NSSM with --run-service.

    NSSM injects SENTINEL_AGENT_KEY, SENTINEL_SERVER_URL, SENTINEL_AGENT_PORT
    into the process environment before calling this.

    As a fallback (e.g. manual launch), the .env file is loaded first.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Load .env as fallback so standalone `SentinelAgent.exe --run-service` works
    env_file = INSTALL_DIR / ".env"
    if env_file.exists() and not os.getenv("SENTINEL_AGENT_KEY"):
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    # Import here so env vars (including those from .env) are visible at module level
    import uvicorn
    from agent.main import app  # noqa: PLC0415

    port = int(os.getenv("SENTINEL_AGENT_PORT", str(AGENT_PORT)))
    host = os.getenv("SENTINEL_AGENT_HOST", "0.0.0.0")

    log.info("Starting Sentinel Agent on %s:%s", host, port)

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    server.run()
    return 0


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="SentinelAgent-Setup",
        description="Sentinel Backup Agent — Installer and Service Runner",
    )
    parser.add_argument(
        "--key", default="",
        help="Agent API key (generated by Sentinel UI → Agents → New Agent)",
    )
    parser.add_argument(
        "--server", default="",
        help="Sentinel server URL, e.g. http://192.168.1.87:8000",
    )
    parser.add_argument(
        "--silent", action="store_true",
        help="Unattended installation — no interactive prompts (requires --key and --server)",
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help="Stop the service, remove firewall rule, and delete install directory",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print the current service status and agent health",
    )
    parser.add_argument(
        "--run-service", dest="run_service", action="store_true",
        help="(Internal) Start the agent HTTP server — invoked by NSSM/SCM",
    )

    args, _ = parser.parse_known_args()

    if args.run_service:
        sys.exit(run_service())

    if args.uninstall:
        sys.exit(run_uninstaller())

    if args.status:
        sys.exit(run_status())

    # Default: installer
    if not _IS_WINDOWS:
        print("The Windows installer only runs on Windows.")
        print()
        print("For Linux / macOS, run the agent directly:")
        print("  SENTINEL_AGENT_KEY=<key> SENTINEL_SERVER_URL=<url> \\")
        print("  python -m uvicorn agent.main:app --host 0.0.0.0 --port 8765")
        sys.exit(1)

    sys.exit(run_installer(args.key, args.server, args.silent))


if __name__ == "__main__":
    main()
