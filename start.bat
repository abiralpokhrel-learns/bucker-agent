@echo off
rem ============================================================
rem  bucker-agent  -  one-click lite launcher (Windows)
rem
rem  Runs the whole platform with NOTHING but Python installed:
rem  no Docker, no Postgres, no Temporal, no uv.
rem
rem  Usage:  start.bat [--no-browser] [--port N]
rem
rem  It will:
rem    1. Check for Python 3.11-3.13 (and try to install 3.12 if missing)
rem    2. Create a virtualenv
rem    3. Copy .env.example to .env (first run only, dev-token stays)
rem    4. Install bucker-agent + its Python dependencies
rem    5. Start the dashboard at http://localhost:8123
rem ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ---------------- args ----------------
rem Honour PORT from the environment when set; default 8123.
if not defined PORT set "PORT=8123"
set "NO_BROWSER="
:parse_args
if "%~1"=="" goto args_done
rem NOTE: one command per IF body via parenthesized blocks. Inline
rem `if cond A & B` runs B unconditionally -- that silently swallowed
rem arguments (and once broke CI), so every branch is explicit here.
if "%~1"=="--no-browser" (
    set "NO_BROWSER=--no-browser"
    shift
    goto parse_args
)
if "%~1"=="--port" (
    if "%~2"=="" (
        echo ERROR: --port needs a number
        exit /b 2
    )
    set "PORT=%~2"
    shift
    shift
    goto parse_args
)
rem NOTE: %~1 is echoed UNQUOTED so findstr /b (beginning-of-line)
rem can match the --port= prefix; "%~1" would start the line with a
rem quote and never match.
echo %~1 | findstr /b /c:"--port=" >nul && (
    for /f "tokens=2 delims==" %%p in ("%~1") do set "PORT=%%p"
    shift & goto parse_args
)
if "%~1"=="--help" goto show_help
if "%~1"=="-h" goto show_help
echo Unknown argument: %~1 (try start.bat --help)
exit /b 2
:show_help
echo Usage: start.bat [--no-browser] [--port N]
echo   --no-browser   do not auto-open the dashboard
echo   --port N       dashboard port (default 8123)
exit /b 0
:args_done

echo.
echo  ==========================================
echo    bucker-agent  -  lite mode
echo    nothing but Python required
echo  ==========================================
echo.

rem ---------------- 1. find or install Python ----------------
rem bucker needs Python 3.11 - 3.13 (>=3.11,<3.14; tested on 3.11/3.12).
rem A python.exe on PATH may be the WRONG version (e.g. 3.14) -- check the
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
    echo  [1/5] Python found but version is unsupported: %PYTHON%
    "%PYTHON%" --version
    echo        bucker needs Python 3.11-3.13, so installing 3.12...
) else (
    echo  [1/5] Python not found - attempting to install it...
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
echo  [1/5] Python found: %PYTHON%

rem ---------------- 2. create the virtualenv ----------------
echo  [2/5] Setting up virtual environment...
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

rem ---------------- 3. config (.env) ----------------
echo  [3/5] Checking configuration...
rem NOTE: .env is copied VERBATIM on purpose. It keeps the dev-token
rem default, which is what leaves the local dashboard + API open on
rem localhost with no login (the host guard still refuses non-localhost
rem callers). Generating a random token here would 401 the dashboard and
rem every token-less API call -- that broke CI's launcher smoke job.
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo        created .env from .env.example (dev-token localhost mode)
    ) else (
        echo        no .env or .env.example found - continuing with defaults
    )
) else (
    echo        .env found - using your existing configuration
)

rem Fail fast when the port is taken (a leftover server serves stale state).
".venv\Scripts\python.exe" -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',int(sys.argv[1])))==0 else 1)" %PORT% >nul 2>&1
if not errorlevel 1 (
    echo  ERROR: port %PORT% is already in use.
    echo         Kill the old server, or run: start.bat --port ^<free-port^>
    pause
    exit /b 1
)

rem ---------------- 4. install the package ----------------
echo  [4/5] Installing bucker-agent (first run takes a minute)...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e .
if errorlevel 1 goto pip_failed

rem ---------------- 5. run it ----------------
echo  [5/5] Starting bucker-agent lite mode...
echo.
echo  dashboard:  http://localhost:%PORT%
echo  first task: click New task -^> type: create a file called hello.py that prints "hello from the robot"
echo  (demo tasks need no API key; AI code tasks need a provider key in .env)
echo  press Ctrl+C to stop
echo.
if defined NO_BROWSER (
    ".venv\Scripts\python.exe" -m bucker.cli lite --no-browser --port %PORT%
) else (
    ".venv\Scripts\python.exe" -m bucker.cli lite --port %PORT%
)
if errorlevel 1 goto run_failed
goto :eof

:venv_failed
echo  ERROR: could not set up the virtual environment.
pause
exit /b 1

:pip_failed
echo  ERROR: pip install failed. Check your internet connection and try again.
echo  Retrying verbosely so you can see the real error:
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -e .
pause
exit /b 1

:run_failed
echo.
echo  bucker-agent stopped with an error.
pause
exit /b 1
