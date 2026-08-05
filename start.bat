@echo off
REM One-command launcher (Windows):
REM
REM     git clone https://github.com/abiralpokhrel-learns/bucker-agent
REM     cd bucker-agent
REM     start.bat
REM
REM Installs uv if missing, then runs `bucker dev` - which bootstraps on
REM first run (prereqs, .env + token, Postgres, migrations) and starts
REM Temporal + worker + dashboard, opening the browser when ready.
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo uv not found - installing via winget...
  winget install astral-sh.uv
)

uv run python -m bucker.cli dev %*
