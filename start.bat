@echo off
rem ============================================================
rem  bucker-agent  -  one-click lite launcher (Windows)
rem
rem  Runs the whole platform with NOTHING but Python installed:
rem  no Docker, no Postgres, no Temporal, no uv.
rem
rem  It will:
rem    1. Check for Python 3.11+ (and try to install it if missing)
rem    2. Create a virtualenv
rem    3. Install bucker-agent + its Python dependencies
rem    4. Start the dashboard at http://localhost:8123
rem ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo  ==========================================
echo    bucker-agent  -  lite mode
echo    nothing but Python required
echo  ==========================================
echo.

rem ---------------- 1. find or install Python ----------------
rem bucker needs Python 3.11 - 3.13 (>=3.11,<3.14; tested on 3.11/3.12).
rem A python.exe on PATH may be the WRONG version (e.g. 3.14) — check the
rem version and, if out of range, install the supported 3.12.

set "PYTHON="
where python >nul 2>nul && set "PYTHON=python"
if not defined PYTHON (
    where py >nul 2>nul && set "PYTHON=py"
)
if defined PYTHON (
    rem version gate: exit 0 when 3.11 <= ver < 3.14
    "%PYTHON%" -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,14) else 1)" >nul 2>&1
    if not errorlevel 1 goto found_python
    echo  [1/4] Python found but version is unsupported: %PYTHON%
    "%PYTHON%" --version
    echo        bucker needs Python 3.11-3.13, so installing 3.12...
) else (
    echo  [1/4] Python not found - attempting to install it...
)
echo        (this downloads the official Python installer)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe' -OutFile '%TEMP%\python-installer.exe'"
if errorlevel 1 goto python_download_failed
echo        installing Python 3.12...
"%TEMP%\python-installer.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
echo        done. Re-checking...
set "PYTHON="
where python >nul 2>nul && set "PYTHON=python"
if not defined PYTHON (
    where py >nul 2>nul && set "PYTHON=py"
)
if defined PYTHON (
    "%PYTHON%" -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,14) else 1)" >nul 2>&1
    if not errorlevel 1 goto found_python
)
goto python_still_missing

:python_download_failed
echo  ERROR: could not download Python.
echo  Install Python 3.12 manually from https://www.python.org/downloads/
echo  (check "Add Python to PATH" during install), then run this file again.
pause
exit /b 1

:python_still_missing
echo  ERROR: no supported Python (3.11-3.13) found after install.
echo  Open a NEW terminal and run this file again.
pause
exit /b 1

:found_python
echo  [1/4] Python found: %PYTHON%

rem ---------------- 2. create the virtualenv ----------------
echo  [2/4] Setting up virtual environment...
if not exist ".venv\Scripts\python.exe" (
    %PYTHON% -m venv .venv
    if errorlevel 1 goto venv_failed
)
rem A venv created by uv has no pip; bootstrap it if missing.
if not exist ".venv\Scripts\pip.exe" (
    echo        bootstrapping pip in the virtualenv...
    ".venv\Scripts\python.exe" -m ensurepip --upgrade >nul 2>&1
    if errorlevel 1 goto venv_failed
)

rem ---------------- 3. install the package ----------------
echo  [3/4] Installing bucker-agent (this may take a minute)...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e .
if errorlevel 1 goto pip_failed

rem ---------------- 4. run it ----------------
echo  [4/4] Starting bucker-agent lite mode...
echo.
echo  dashboard will open at:  http://localhost:8123
echo  press Ctrl+C to stop
echo.
".venv\Scripts\python.exe" -m bucker.cli lite
if errorlevel 1 goto run_failed
goto :eof

:venv_failed
echo  ERROR: could not set up the virtual environment.
pause
exit /b 1

:pip_failed
echo  ERROR: pip install failed. Check your internet connection and try again.
pause
exit /b 1

:run_failed
echo.
echo  bucker-agent stopped with an error.
pause
exit /b 1
