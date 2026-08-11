# ============================================================
#  bucker-agent  -  PowerShell launcher (Windows)
#
#  Usage:  .\start.ps1     (or:  start.ps1)
#
#  Same as start.bat — nothing but Python required:
#  no Docker, no Postgres, no Temporal, no uv.
# ============================================================
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host ""
Write-Host " =========================================="
Write-Host "   bucker-agent  -  lite mode"
Write-Host "   nothing but Python required"
Write-Host " =========================================="
Write-Host ""

# ---------------- 1. find or install Python ----------------
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Host " [1/4] Python not found - attempting to install it..."
    $installer = Join-Path $env:TEMP "python-installer.exe"
    try {
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe" -OutFile $installer
        Write-Host "       installing Python 3.12..."
        Start-Process -FilePath $installer -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_test=0" -Wait
        Write-Host "       done. Re-checking..."
    } catch {
        Write-Host " ERROR: could not download Python."
        Write-Host " Install it manually from https://www.python.org/downloads/"
        Write-Host " (check `"Add Python to PATH`" during install), then run this file again."
        Read-Host " Press Enter to exit..."
        exit 1
    }
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
}
if (-not $py) {
    Write-Host " ERROR: Python still not found after install."
    Write-Host " Open a NEW terminal and run this file again."
    Read-Host " Press Enter to exit..."
    exit 1
}
Write-Host " [1/4] Python found: $($py.Name)"

# ---------------- 2. create the virtualenv ----------------
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$venvPip = Join-Path $PSScriptRoot ".venv\Scripts\pip.exe"
Write-Host " [2/4] Setting up virtual environment..."
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

# ---------------- 3. install the package ----------------
Write-Host " [3/4] Installing bucker-agent (this may take a minute)..."
& $venvPy -m pip install --quiet --disable-pip-version-check -e .
if ($LASTEXITCODE -ne 0) {
    Write-Host " ERROR: pip install failed. Check your internet connection and try again."
    Read-Host " Press Enter to exit..."
    exit 1
}

# ---------------- 4. run it ----------------
Write-Host " [4/4] Starting bucker-agent lite mode..."
Write-Host ""
Write-Host "  dashboard will open at:  http://localhost:8123"
Write-Host "  press Ctrl+C to stop"
Write-Host ""
& $venvPy -m bucker.cli lite
