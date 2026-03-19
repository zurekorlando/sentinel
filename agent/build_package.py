"""
Build the Sentinel Windows Agent installer package (ZIP fallback).

Produces: dist/SentinelAgent-installer.zip

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NOTE: The preferred distribution format is SentinelAgent-Setup.exe, produced
by agent/build.py using PyInstaller (no Python required on target machine).
Use this ZIP package only as a fallback when the target machine already has
Python 3.10+ installed and the .exe is unavailable.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The ZIP contains everything needed to install the agent on a Windows machine:
  - Agent Python code  (agent/)
  - Sentinel SSM Windows module (sentinel/ssm/windows/ + base)
  - PowerShell installer script (Install-SentinelAgent.ps1)
  - Requirements file (requirements-agent.txt)
  - Quick-start README (README.txt)
  - tools/nssm.exe (NSSM 2.24 win64 — bundled, no download required)

On the Windows machine, the user runs:
  1. Unzip to any folder
  2. Right-click Install-SentinelAgent.ps1 → Run as Administrator
     (or: powershell -ExecutionPolicy Bypass -File Install-SentinelAgent.ps1)

The installer:
  - Checks Python 3.10+
  - Creates C:\\SentinelAgent\\
  - Creates a virtual environment
  - Installs FastAPI + Uvicorn + dependencies
  - Uses bundled NSSM to wrap Python/Uvicorn as a Windows Service
  - Registers and starts the SentinelAgent Windows Service on port 8765
  - Opens port 8765 in Windows Firewall

Usage:
    python agent/build_package.py
    # → dist/SentinelAgent-installer.zip
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
DIST = ROOT / "dist"
OUTPUT = DIST / "SentinelAgent-installer.zip"
NSSM_BIN = DIST / "nssm_win64.exe"   # pre-downloaded by build_package.py

_AGENT_VERSION = "1.0.0"
_NSSM_SHA256 = "f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97"


# ── Files to include ──────────────────────────────────────────────────────────

AGENT_FILES = [
    ("agent/__init__.py",                  "agent/__init__.py"),
    ("agent/main.py",                      "agent/main.py"),
    ("sentinel/__init__.py",               "sentinel/__init__.py"),
    ("sentinel/ssm/__init__.py",           "sentinel/ssm/__init__.py"),
    ("sentinel/ssm/base.py",               "sentinel/ssm/base.py"),
    ("sentinel/ssm/windows/__init__.py",   "sentinel/ssm/windows/__init__.py"),
    ("sentinel/ssm/windows/vss.py",        "sentinel/ssm/windows/vss.py"),
]

REQUIREMENTS_AGENT = """\
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
anyio>=4.0.0
"""

README_TXT = f"""\
Sentinel Windows Agent v{_AGENT_VERSION}
========================================

QUICK START
-----------
1. Right-click Install-SentinelAgent.ps1
   -> "Run with PowerShell" (or run as Administrator)

   Alternatively:
   powershell -ExecutionPolicy Bypass -File Install-SentinelAgent.ps1

2. The installer will:
   - Verify Python 3.10+ is installed (download it if needed)
   - Create C:\\SentinelAgent\\ and copy files
   - Create a virtual environment and install dependencies
   - Download NSSM and register the Windows Service
   - Start the SentinelAgent service on port 8765
   - Open port 8765 in Windows Firewall

3. After installation, go to the Sentinel web UI -> Machines
   and click "Scan Network" -- your machine will appear with the
   green "Agent" badge.

MANUAL OPERATION
----------------
  sc start SentinelAgent      # start the service
  sc stop SentinelAgent       # stop the service
  sc delete SentinelAgent     # uninstall the service

  # Test without service:
  C:\\SentinelAgent\\.venv\\Scripts\\python.exe -m uvicorn agent.main:app --host 0.0.0.0 --port 8765

SECURITY
--------
Set an API key to protect the agent (optional but recommended):
  1. Open Windows Environment Variables
  2. Add SENTINEL_AGENT_KEY = <your-secret>
  3. Restart the service: sc stop SentinelAgent && sc start SentinelAgent

The Sentinel server must send X-Sentinel-Key: <your-secret> in requests.

REQUIREMENTS
------------
  - Windows 10 / Windows Server 2016 or later
  - Python 3.10 or later (https://www.python.org/downloads/)
  - Administrator privileges for installation and VSS

UNINSTALL
---------
  Run Uninstall-SentinelAgent.ps1 (created by the installer in C:\\SentinelAgent\\)

SUPPORT
-------
  GitHub: https://github.com/your-org/sentinel
  Docs: See CLAUDE.md in the main project repository
"""


# ---------------------------------------------------------------------------
# PowerShell installer - strict ASCII-only source (no Unicode characters).
# Written with UTF-8 BOM so PowerShell 5.1 on Windows reads it correctly.
# ---------------------------------------------------------------------------

INSTALL_PS1 = r"""#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Installs Sentinel Windows Agent as a Windows Service.
.DESCRIPTION
  - Copies agent files to C:\SentinelAgent\
  - Creates a Python virtual environment and installs dependencies
  - Downloads NSSM to register Python/Uvicorn as a Windows Service
  - Starts the service on port 8765
  - Opens Windows Firewall port 8765
#>

$ErrorActionPreference = "Stop"
$AgentDir    = "C:\SentinelAgent"
$AgentPort   = 8765
$ServiceName = "SentinelAgent"
$NssmExe     = "$AgentDir\tools\nssm.exe"
$PythonMin   = [Version]"3.10"

# --- Banner ------------------------------------------------------------------
Write-Host ""
Write-Host "  +==========================================+" -ForegroundColor Cyan
Write-Host "  |    Sentinel Windows Agent Installer      |" -ForegroundColor Cyan
Write-Host "  +==========================================+" -ForegroundColor Cyan
Write-Host ""

# --- Step 1: Verify Python ---------------------------------------------------
Write-Host "[1/7] Checking Python installation..." -ForegroundColor Yellow

$PythonExe = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python (\d+\.\d+)") {
            $found = [Version]$Matches[1]
            if ($found -ge $PythonMin) {
                $PythonExe = (Get-Command $cmd).Source
                Write-Host "    Found: $ver at $PythonExe" -ForegroundColor Green
                break
            }
        }
    } catch { }
}

if (-not $PythonExe) {
    Write-Host "    Python $PythonMin or later not found." -ForegroundColor Red
    Write-Host "    Please install Python from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "    Make sure to check 'Add Python to PATH' during installation." -ForegroundColor Yellow
    Write-Host ""
    $choice = Read-Host "Open download page? [Y/n]"
    if ($choice -ne "n" -and $choice -ne "N") {
        Start-Process "https://www.python.org/downloads/"
    }
    Read-Host "Press Enter after installing Python, then re-run this script"
    exit 1
}

# --- Step 2: Create install directory ----------------------------------------
Write-Host "[2/7] Creating $AgentDir ..." -ForegroundColor Yellow

if (Test-Path $AgentDir) {
    $existing = Read-Host "    $AgentDir already exists. Overwrite? [y/N]"
    if ($existing -ne "y" -and $existing -ne "Y") {
        Write-Host "    Installation cancelled." -ForegroundColor Red
        exit 1
    }
}

New-Item -ItemType Directory -Force -Path $AgentDir | Out-Null
New-Item -ItemType Directory -Force -Path "$AgentDir\agent" | Out-Null
New-Item -ItemType Directory -Force -Path "$AgentDir\sentinel\ssm\windows" | Out-Null
New-Item -ItemType Directory -Force -Path "$AgentDir\tools" | Out-Null

# --- Step 3: Copy agent files ------------------------------------------------
Write-Host "[3/7] Copying agent files..." -ForegroundColor Yellow

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$FilesToCopy = @(
    "agent\__init__.py",
    "agent\main.py",
    "sentinel\__init__.py",
    "sentinel\ssm\__init__.py",
    "sentinel\ssm\base.py",
    "sentinel\ssm\windows\__init__.py",
    "sentinel\ssm\windows\vss.py",
    "requirements-agent.txt",
    "tools\nssm.exe"
)

foreach ($f in $FilesToCopy) {
    $src = Join-Path $ScriptDir $f
    $dst = Join-Path $AgentDir $f
    $dstDir = Split-Path -Parent $dst
    New-Item -ItemType Directory -Force -Path $dstDir | Out-Null
    Copy-Item -Force $src $dst
    Write-Host "    Copied: $f" -ForegroundColor Gray
}

# --- Step 4: Create virtual environment --------------------------------------
Write-Host "[4/7] Creating virtual environment..." -ForegroundColor Yellow

$VenvDir    = "$AgentDir\.venv"
$VenvPython = "$VenvDir\Scripts\python.exe"
$VenvPip    = "$VenvDir\Scripts\pip.exe"
$VenvReused = $false

if (Test-Path $VenvDir) {
    # Stop the service first so Python/pyd files are not held open by Windows
    Write-Host "    Stopping service to release file locks..." -ForegroundColor Gray
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2

    try {
        Remove-Item -Recurse -Force $VenvDir -ErrorAction Stop
        Write-Host "    Existing .venv removed." -ForegroundColor Gray
    } catch {
        Write-Host "    WARNING: Could not remove .venv (files still locked): $_" -ForegroundColor Yellow
        Write-Host "    Reusing existing .venv and upgrading packages in-place." -ForegroundColor Yellow
        $VenvReused = $true
    }
}

if (-not $VenvReused) {
    & $PythonExe -m venv $VenvDir
}

Write-Host "    Installing dependencies (fastapi, uvicorn)..." -ForegroundColor Gray
& $VenvPip install --quiet --upgrade pip
& $VenvPip install --quiet -r "$AgentDir\requirements-agent.txt"
Write-Host "    Dependencies installed." -ForegroundColor Green

# --- Step 5: Verify bundled NSSM ---------------------------------------------
Write-Host "[5/7] Verifying bundled NSSM..." -ForegroundColor Yellow

if (-not (Test-Path $NssmExe)) {
    Write-Host "    ERROR: tools\nssm.exe not found in $AgentDir" -ForegroundColor Red
    Write-Host "    The installer package may be incomplete. Please re-download it." -ForegroundColor Red
    exit 1
}
Write-Host "    NSSM found: $NssmExe" -ForegroundColor Green

# --- Step 6: Register Windows Service ----------------------------------------
Write-Host "[6/7] Registering Windows Service '$ServiceName'..." -ForegroundColor Yellow

# Stop and remove any existing instance
$svcExisting = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svcExisting) {
    Write-Host "    Removing existing service..." -ForegroundColor Gray
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    & $NssmExe remove $ServiceName confirm 2>$null
    Start-Sleep -Seconds 2
}

$UvicornArgs = "-m uvicorn agent.main:app --host 0.0.0.0 --port $AgentPort"

# Register service via NSSM (bundled - no download required)
& $NssmExe install $ServiceName "$VenvPython"
& $NssmExe set $ServiceName AppParameters $UvicornArgs
& $NssmExe set $ServiceName AppDirectory $AgentDir
& $NssmExe set $ServiceName DisplayName "Sentinel Backup Agent"
& $NssmExe set $ServiceName Description "Provides VSS snapshot creation and file streaming for Sentinel remote backup."
& $NssmExe set $ServiceName Start SERVICE_AUTO_START
& $NssmExe set $ServiceName AppStdout "$AgentDir\agent.log"
& $NssmExe set $ServiceName AppStderr "$AgentDir\agent-error.log"
& $NssmExe set $ServiceName AppRotateFiles 1
& $NssmExe set $ServiceName AppRotateBytes 10485760
Write-Host "    Service registered via NSSM." -ForegroundColor Green

# --- Step 7: Firewall + Start ------------------------------------------------
Write-Host "[7/7] Configuring firewall and starting service..." -ForegroundColor Yellow

Remove-NetFirewallRule -DisplayName "Sentinel Agent" -ErrorAction SilentlyContinue

New-NetFirewallRule `
    -DisplayName "Sentinel Agent" `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $AgentPort `
    -Profile Any `
    -Description "Allow Sentinel Backup Agent on port $AgentPort" | Out-Null

Write-Host "    Firewall rule added for port $AgentPort." -ForegroundColor Green

Start-Service -Name $ServiceName
$svc = Get-Service -Name $ServiceName
Write-Host "    Service status: $($svc.Status)" -ForegroundColor Green

# --- Done --------------------------------------------------------------------
Write-Host ""
Write-Host "  +==========================================+" -ForegroundColor Green
Write-Host "  |   Installation complete!                 |" -ForegroundColor Green
Write-Host "  +==========================================+" -ForegroundColor Green
Write-Host ""
Write-Host "  Agent is running on port $AgentPort" -ForegroundColor White
Write-Host "  Health check: http://localhost:$AgentPort/health" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Go to Sentinel UI -> Machines -> Scan Network" -ForegroundColor White
Write-Host "  Your machine will appear with the green [Agent] badge." -ForegroundColor White
Write-Host ""

# Write uninstall script
$UninstallLines = @(
    "#Requires -RunAsAdministrator",
    "Write-Host 'Uninstalling Sentinel Agent...' -ForegroundColor Yellow",
    "Stop-Service -Name '$ServiceName' -Force -ErrorAction SilentlyContinue",
    "& '$AgentDir\tools\nssm.exe' remove '$ServiceName' confirm",
    "Remove-NetFirewallRule -DisplayName 'Sentinel Agent' -ErrorAction SilentlyContinue",
    "Remove-Item -Recurse -Force '$AgentDir' -ErrorAction SilentlyContinue",
    "Write-Host 'Uninstall complete.' -ForegroundColor Green"
)
$UninstallContent = $UninstallLines -join "`r`n"
Set-Content -Path "$AgentDir\Uninstall-SentinelAgent.ps1" -Value $UninstallContent -Encoding UTF8

Read-Host "Press Enter to exit"
"""


# ── Build function ────────────────────────────────────────────────────────────

def build():
    import hashlib

    DIST.mkdir(parents=True, exist_ok=True)

    # Verify NSSM binary is present and matches expected SHA256
    if not NSSM_BIN.exists():
        raise FileNotFoundError(
            f"NSSM binary not found at {NSSM_BIN}\n"
            "Run once to download it:\n"
            "  curl -L -o dist/nssm_tmp.zip https://nssm.cc/release/nssm-2.24.zip\n"
            "  unzip dist/nssm_tmp.zip -d dist/nssm_tmp\n"
            "  cp dist/nssm_tmp/nssm-2.24/win64/nssm.exe dist/nssm_win64.exe"
        )

    nssm_data = NSSM_BIN.read_bytes()
    actual_sha = hashlib.sha256(nssm_data).hexdigest()
    if actual_sha != _NSSM_SHA256:
        raise ValueError(
            f"NSSM binary SHA256 mismatch!\n"
            f"  expected: {_NSSM_SHA256}\n"
            f"  got:      {actual_sha}"
        )

    print(f"Building Sentinel Windows Agent installer package...")
    print(f"Version: {_AGENT_VERSION}")
    print(f"Output:  {OUTPUT}")
    print(f"NSSM:    {NSSM_BIN.name} ({len(nssm_data)//1024} KB, SHA256 verified)")
    print()

    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as zf:

        # Agent Python files
        for src_rel, arc_path in AGENT_FILES:
            src = ROOT / src_rel
            if not src.exists():
                print(f"  WARNING: {src_rel} not found, skipping")
                continue
            zf.write(src, arc_path)
            print(f"  + {arc_path}")

        # requirements-agent.txt
        zf.writestr("requirements-agent.txt", REQUIREMENTS_AGENT)
        print(f"  + requirements-agent.txt")

        # PowerShell installer — UTF-8 BOM + pure ASCII content
        ps1_bytes = b'\xef\xbb\xbf' + INSTALL_PS1.encode('ascii')
        zf.writestr("Install-SentinelAgent.ps1", ps1_bytes)
        print(f"  + Install-SentinelAgent.ps1 (UTF-8 BOM, pure ASCII, {len(INSTALL_PS1)} chars)")

        # README
        zf.writestr("README.txt", README_TXT.encode('ascii'))
        print(f"  + README.txt")

        # NSSM binary — stored without compression (already a binary)
        zf.writestr(
            zipfile.ZipInfo("tools/nssm.exe"),
            nssm_data,
            compress_type=zipfile.ZIP_STORED,
        )
        print(f"  + tools/nssm.exe ({len(nssm_data)//1024} KB, NSSM 2.24 win64, bundled)")

    size_kb = OUTPUT.stat().st_size // 1024
    print()
    print(f"Package built successfully ({size_kb} KB)")
    print(f"  -> {OUTPUT}")
    print()
    print("Contents:")
    with zipfile.ZipFile(OUTPUT) as zf:
        for info in zf.infolist():
            print(f"  {info.filename:<50} {info.file_size//1024:>4} KB")
    print()
    print("Deploy:")
    print("  1. API endpoint:    GET /api/machines/agent/download")
    print("  2. On Windows:      unzip -> Run Install-SentinelAgent.ps1 as Administrator")

    return OUTPUT


if __name__ == "__main__":
    build()
