# ============================================================
#  bucker-agent  -  PowerShell launcher (Windows)
#
#  Usage:  .\start.ps1 [--no-browser] [--port N]
#
#  Same as start.bat -- nothing but Python required:
#  no Docker, no Postgres, no Temporal, no uv.
# ============================================================
param(
    [switch]$noBrowser,
    [int]$port = 0,
    [switch]$help
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($help) {
    Write-Host "Usage: .\start.ps1 [--no-browser] [--port N]"
    exit 0
}
if ($port -eq 0) {
    if ($env:PORT) { $port = [int]$env:PORT } else { $port = 8123 }
}

Write-Host ""
Write-Host " =========================================="
Write-Host "   bucker-agent  -  lite mode"
Write-Host "   nothing but Python required"
Write-Host " =========================================="
Write-Host ""

# ---------------- 1. find or install Python ----------------
# bucker needs Python 3.11 - 3.13 (>=3.11,<3.14; tested on 3.11/3.12).
# A python on PATH may be the WRONG version (e.g. 3.14) -- check the
# version and, if out of range, install the supported 3.12.
function Test-BuckerPython {
    param($Exe)
    if (-not $Exe) { return $false }
    try {
        & $Exe -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,14) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not (Test-BuckerPython $py.Source)) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not (Test-BuckerPython $py.Source)) {
    if ($py) {
        Write-Host " [1/5] Python found but version is unsupported:"
        & $py.Source --version
        Write-Host "       bucker needs Python 3.11-3.13, so installing 3.12..."
    } else {
        Write-Host " [1/5] Python not found - attempting to install it..."
    }
    $installer = Join-Path $env:TEMP "python-installer.exe"
    try {
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe" -OutFile $installer
        Write-Host "       installing Python 3.12..."
        Start-Process -FilePath $installer -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_test=0" -Wait
        Write-Host "       done. Re-checking..."
    } catch {
        Write-Host " ERROR: could not download Python."
        Write-Host " Install Python 3.12 manually from https://www.python.org/downloads/"
        Write-Host " (check `"Add Python to PATH`" during install), then run this file again."
        Read-Host " Press Enter to exit..."
        exit 1
    }
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not (Test-BuckerPython $py.Source)) { $py = Get-Command py -ErrorAction SilentlyContinue }
}
if (-not (Test-BuckerPython $py.Source)) {
    Write-Host " ERROR: no supported Python (3.11-3.13) found after install."
    Write-Host " Open a NEW terminal and run this file again."
    Read-Host " Press Enter to exit..."
    exit 1
}
Write-Host " [1/5] Python found: $($py.Name)"

# ---------------- 2. create the virtualenv ----------------
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$venvPip = Join-Path $PSScriptRoot ".venv\Scripts\pip.exe"
Write-Host " [2/5] Setting up virtual environment..."
if (-not (Test-Path $venvPy)) {
    & $py.Source -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host " ERROR: could not set up the virtual environment."
        Read-Host " Press Enter to exit..."
        exit 1
    }
}
# A venv created by uv has no pip; bootstrap it if missing.
if (-not (Test-Path $venvPip)) {
    Write-Host "       (bootstrapping pip in the virtualenv...)"
    & $venvPy -m ensurepip --upgrade | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host " ERROR: could not set up the virtual environment."
        Read-Host " Press Enter to exit..."
        exit 1
    }
}

# ---------------- 3. config (.env) ----------------
Write-Host " [3/5] Checking configuration..."
# NOTE: .env is copied VERBATIM on purpose. It keeps the dev-token
# default, which is what leaves the local dashboard + API open on
# localhost with no login (the host guard still refuses non-localhost
# callers). Generating a random token here would 401 the dashboard and
# every token-less API call -- that broke CI's launcher smoke job.
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "       created .env from .env.example (dev-token localhost mode)"
    } else {
        Write-Host "       no .env or .env.example found -- continuing with defaults"
    }
} else {
    Write-Host "       .env found -- using your existing configuration"
}

# Fail fast when the port is taken (a leftover server serves stale state).
& $venvPy -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',int(sys.argv[1])))==0 else 1)" $port 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host " ERROR: port $port is already in use."
    Write-Host "        Kill the old server, or run: .\start.ps1 --port <free-port>"
    Read-Host " Press Enter to exit..."
    exit 1
}

# ---------------- 4. install the package ----------------
Write-Host " [4/5] Installing bucker-agent (first run takes a minute)..."
& $venvPy -m pip install --quiet --disable-pip-version-check -e .
if ($LASTEXITCODE -ne 0) {
    Write-Host " ERROR: pip install failed. Check your internet connection and try again."
    Write-Host " Retrying verbosely so you can see the real error:"
    & $venvPy -m pip install --disable-pip-version-check -e .
    Read-Host " Press Enter to exit..."
    exit 1
}

# ---------------- 5. run it ----------------
Write-Host " [5/5] Starting bucker-agent lite mode..."
Write-Host ""
Write-Host "  dashboard:  http://localhost:$port"
Write-Host '  first task: click New task -> type: create a file called hello.py that prints "hello from the robot"'
Write-Host "  (demo tasks need no API key; AI code tasks need a provider key in .env)"
Write-Host "  press Ctrl+C to stop"
Write-Host ""
if ($noBrowser) {
    & $venvPy -m bucker.cli lite --no-browser --port $port
} else {
    & $venvPy -m bucker.cli lite --port $port
}
