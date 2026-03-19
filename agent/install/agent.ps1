# Sentinel Agent — Windows Installer
# Usage:
#   $env:SENTINEL_AGENT_KEY="your-key-here"
#   $env:SENTINEL_SERVER_URL="http://192.168.1.87:8000"
#   irm http://your-server:8000/install/agent.ps1 | iex
#
# Or run locally after extracting the ZIP:
#   .\install\agent.ps1 -AgentKey "your-key" -ServerUrl "http://..."

param(
    [Parameter(Mandatory=$false)]
    [string]$AgentKey = $env:SENTINEL_AGENT_KEY,

    [Parameter(Mandatory=$false)]
    [string]$ServerUrl = $env:SENTINEL_SERVER_URL,

    [Parameter(Mandatory=$false)]
    [int]$Port = 8765,

    [Parameter(Mandatory=$false)]
    [string]$InstallDir = "C:\SentinelAgent"
)

# Validate required parameters
if (-not $AgentKey) {
    Write-Error "SENTINEL_AGENT_KEY is required. Set it as an environment variable or pass -AgentKey parameter."
    exit 1
}
if (-not $ServerUrl) {
    Write-Error "SENTINEL_SERVER_URL is required. Set it as an environment variable or pass -ServerUrl parameter."
    exit 1
}

Write-Host "=== Sentinel Agent Installer ===" -ForegroundColor Cyan
Write-Host "Install directory : $InstallDir"
Write-Host "Server URL        : $ServerUrl"
Write-Host "Port              : $Port"
Write-Host ""

# Create install directory
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path "$InstallDir\logs" | Out-Null

# Write environment file (used by the service)
$envContent = @"
SENTINEL_AGENT_KEY=$AgentKey
SENTINEL_SERVER_URL=$ServerUrl
SENTINEL_AGENT_PORT=$Port
SENTINEL_AGENT_HOST=0.0.0.0
"@
$envContent | Set-Content -Path "$InstallDir\.env" -Encoding UTF8

Write-Host "Environment file written to $InstallDir\.env" -ForegroundColor Green

# Check for NSSM
$nssmPath = "$InstallDir\nssm.exe"
if (-not (Test-Path $nssmPath)) {
    Write-Host "NSSM not found. Please copy nssm.exe to $InstallDir" -ForegroundColor Yellow
    Write-Host "Download from: https://nssm.cc/release/nssm-2.24.zip" -ForegroundColor Yellow
}

# Write service startup script
$startScript = @"
@echo off
cd /d "$InstallDir"
for /f "tokens=*" %%i in (.env) do set %%i
python -m uvicorn agent.main:app --host 0.0.0.0 --port %SENTINEL_AGENT_PORT%
"@
$startScript | Set-Content -Path "$InstallDir\start_agent.bat" -Encoding ASCII

Write-Host ""
Write-Host "=== Installation complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "To install as a Windows Service, run as Administrator:" -ForegroundColor Yellow
Write-Host "  $nssmPath install SentinelAgent `"$InstallDir\start_agent.bat`"" -ForegroundColor White
Write-Host "  $nssmPath set SentinelAgent AppDirectory `"$InstallDir`"" -ForegroundColor White
Write-Host "  $nssmPath set SentinelAgent DisplayName `"Sentinel Backup Agent`"" -ForegroundColor White
Write-Host "  $nssmPath set SentinelAgent Start SERVICE_AUTO_START" -ForegroundColor White
Write-Host "  Start-Service SentinelAgent" -ForegroundColor White
Write-Host ""
Write-Host "To verify the agent is running:" -ForegroundColor Yellow
Write-Host "  Invoke-RestMethod http://localhost:$Port/health" -ForegroundColor White
