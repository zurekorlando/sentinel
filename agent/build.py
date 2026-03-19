"""
Build script for SentinelAgent-Setup.exe — single-file Windows installer.

Uses PyInstaller (--onefile) to produce a self-contained executable that:
  - Installs the Sentinel agent as a Windows Service (NSSM)
  - Embeds the FastAPI + Uvicorn runtime (no Python required on target)
  - Bundles nssm_win64.exe as a data resource

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT: This script MUST run on Windows (PyInstaller compiles Windows .exe
files only on Windows — cross-compilation is not supported).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Prerequisites
─────────────
1.  Python 3.10+ on Windows
2.  pip install pyinstaller fastapi "uvicorn[standard]" anyio
3.  NSSM 2.24 win64 binary at dist/nssm_win64.exe
    Download once (PowerShell):

        Invoke-WebRequest -Uri https://nssm.cc/release/nssm-2.24.zip `
                          -OutFile dist\\nssm-2.24.zip
        Expand-Archive   dist\\nssm-2.24.zip -DestinationPath dist\\nssm_tmp
        Copy-Item        dist\\nssm_tmp\\nssm-2.24\\win64\\nssm.exe `
                          dist\\nssm_win64.exe
        Remove-Item      dist\\nssm_tmp, dist\\nssm-2.24.zip -Recurse -Force

    Expected SHA-256: f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97

Usage
─────
    python agent/build.py

Output
──────
    dist/SentinelAgent-Setup.exe   (~15–25 MB, fully self-contained)

Deployment (on the target Windows machine)
──────────────────────────────────────────
    # Interactive install (prompts for key + server URL):
    SentinelAgent-Setup.exe

    # Pre-configured (e.g. downloaded from Sentinel UI with key embedded):
    SentinelAgent-Setup.exe --key "abc123" --server "http://192.168.1.87:8000"

    # Silent / unattended:
    SentinelAgent-Setup.exe --key "abc123" --server "http://192.168.1.87:8000" --silent

    # Management:
    SentinelAgent-Setup.exe --status
    SentinelAgent-Setup.exe --uninstall
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
ENTRY = ROOT / "agent" / "install_service.py"
DIST = ROOT / "dist"
NSSM_BIN = DIST / "nssm_win64.exe"

_NSSM_SHA256 = "f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97"

# --add-data uses ";" on Windows (os.pathsep), ":" on Unix
import os as _os
_SEP = _os.pathsep  # ";" on Windows


def _check_nssm() -> None:
    """Verify the NSSM binary is present and matches the expected SHA-256."""
    if not NSSM_BIN.exists():
        print(
            f"ERROR: NSSM binary not found at {NSSM_BIN}\n"
            "\n"
            "Run this once in PowerShell (from the project root) to download it:\n"
            "\n"
            "    Invoke-WebRequest -Uri https://nssm.cc/release/nssm-2.24.zip `\n"
            "                      -OutFile dist\\nssm-2.24.zip\n"
            "    Expand-Archive   dist\\nssm-2.24.zip -DestinationPath dist\\nssm_tmp\n"
            "    Copy-Item        dist\\nssm_tmp\\nssm-2.24\\win64\\nssm.exe `\n"
            "                      dist\\nssm_win64.exe\n"
            "    Remove-Item      dist\\nssm_tmp, dist\\nssm-2.24.zip -Recurse -Force\n"
        )
        sys.exit(1)

    data = NSSM_BIN.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != _NSSM_SHA256:
        print(
            f"ERROR: NSSM binary SHA-256 mismatch!\n"
            f"  expected: {_NSSM_SHA256}\n"
            f"  got:      {actual}\n"
            "\n"
            "Re-download nssm_win64.exe (see instructions above)."
        )
        sys.exit(1)

    print(f"NSSM OK  {NSSM_BIN.name}  ({len(data) // 1024} KB, SHA-256 verified)")


def _build() -> None:
    _check_nssm()

    # --add-data bundles nssm_win64.exe into the root of _MEIPASS at runtime
    add_data = f"{NSSM_BIN}{_SEP}."

    args = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",                           # keep console window for installer output
        "--name", "SentinelAgent-Setup",
        "--distpath", str(DIST),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        # Project root on sys.path so "agent" package resolves correctly
        "--paths", str(ROOT),
        # Bundle NSSM binary as data resource (accessible via sys._MEIPASS)
        "--add-data", add_data,
        # ── agent package (lazy-imported in --run-service mode) ──────────────
        "--hidden-import", "agent",
        "--hidden-import", "agent.main",
        # ── uvicorn internals ─────────────────────────────────────────────────
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.loops.asyncio",
        "--hidden-import", "uvicorn.protocols",
        "--hidden-import", "uvicorn.protocols.http",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.protocols.websockets",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan",
        "--hidden-import", "uvicorn.lifespan.on",
        # ── FastAPI / Starlette ───────────────────────────────────────────────
        "--hidden-import", "fastapi",
        "--hidden-import", "starlette",
        "--hidden-import", "starlette.routing",
        "--hidden-import", "starlette.middleware",
        "--hidden-import", "starlette.middleware.cors",
        "--hidden-import", "starlette.responses",
        "--hidden-import", "starlette.staticfiles",
        # ── Pydantic v2 ───────────────────────────────────────────────────────
        "--hidden-import", "pydantic",
        "--hidden-import", "pydantic.deprecated.class_validators",
        # ── Async runtime ─────────────────────────────────────────────────────
        "--hidden-import", "anyio",
        "--hidden-import", "anyio._backends._asyncio",
        # ── Sentinel SSM (VSS on Windows) ─────────────────────────────────────
        "--hidden-import", "sentinel",
        "--hidden-import", "sentinel.ssm",
        "--hidden-import", "sentinel.ssm.windows",
        "--hidden-import", "sentinel.ssm.windows.vss",
        # ── Exclude large unused packages ─────────────────────────────────────
        "--exclude-module", "boto3",
        "--exclude-module", "botocore",
        "--exclude-module", "pytest",
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "PIL",
        str(ENTRY),
    ]

    print("Building SentinelAgent-Setup.exe ...")
    print(f"  Entry:  {ENTRY}")
    print(f"  Output: {DIST / 'SentinelAgent-Setup.exe'}")
    print(f"  NSSM:   bundled ({NSSM_BIN.name})")
    print()

    result = subprocess.run(args, cwd=str(ROOT))
    if result.returncode != 0:
        print("PyInstaller build FAILED.")
        sys.exit(result.returncode)

    output = DIST / "SentinelAgent-Setup.exe"
    size_mb = output.stat().st_size / 1_048_576
    print()
    print(f"Build successful:  {output}  ({size_mb:.1f} MB)")
    print()
    print("Distribute SentinelAgent-Setup.exe to target Windows machines.")
    print()
    print("Usage:")
    print("  # Interactive:")
    print("  SentinelAgent-Setup.exe")
    print()
    print("  # Pre-configured (Sentinel UI generates this URL):")
    print('  SentinelAgent-Setup.exe --key "abc123" --server "http://192.168.1.87:8000"')
    print()
    print("  # Silent / unattended:")
    print('  SentinelAgent-Setup.exe --key "abc123" --server "http://..." --silent')


if __name__ == "__main__":
    _build()
