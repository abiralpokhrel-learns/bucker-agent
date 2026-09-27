#!/usr/bin/env bash
# ============================================================
#  bucker-agent  -  one-click lite launcher (macOS / Linux)
#
#  Runs the whole platform with NOTHING but Python installed:
#  no Docker, no Postgres, no Temporal, no uv.
#
#    ./start.sh [--no-browser] [--port N]
#
#  It will:
#    1. Check for Python 3.11-3.13 (and say how to install it if missing)
#    2. Create a virtualenv (+ bootstrap pip if missing)
#    3. Create .env with a fresh API token (first run only)
#    4. Install bucker-agent + its Python dependencies
#    5. Start the dashboard at http://localhost:8123
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

# Windows guard: if this .sh is being run from PowerShell/cmd (e.g. someone
# typed `./start.sh` or `/start.sh` on Windows), give the right command
# instead of a cryptic failure.
if [[ "${OS:-}" == "Windows_NT" || "$(uname -s 2>/dev/null || echo unknown)" == MINGW* ]]; then
    echo
    echo " =========================================="
    echo "   This is the macOS/Linux launcher."
    echo "   You are on Windows - use start.bat instead:"
    echo
    echo "       start.bat          (or double-click it)"
    echo
    echo "   If PowerShell won't run it: type the filename"
    echo "   with .\\ in front, e.g.:"
    echo
    echo "       .\\start.bat"
    echo " =========================================="
    echo
    read -r -p "Press Enter to exit... " || true
    exit 0
fi

echo
echo " =========================================="
echo "   bucker-agent  -  lite mode"
echo "   nothing but Python required"
echo " =========================================="
echo

# ---------------- args (first, so --help works without Python) ----------------
# 5-minute path: ./start.sh [--no-browser] [--port N]
PORT="${PORT:-8123}"
NO_BROWSER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-browser) NO_BROWSER="--no-browser"; shift ;;
        --port)
            if [ $# -lt 2 ]; then echo "ERROR: --port needs a number"; exit 2; fi
            PORT="$2"; shift 2 ;;
        --port=*) PORT="${1#--port=}"; shift ;;
        -h|--help)
            echo "Usage: ./start.sh [--no-browser] [--port N]"
            echo "  --no-browser   don't auto-open the dashboard"
            echo "  --port N       dashboard port (default 8123, or PORT env)"
            exit 0
            ;;
        *) echo "Unknown argument: $1 (try --help)"; exit 2 ;;
    esac
done
if ! printf '%s' "$PORT" | grep -Eq '^[0-9]+$'; then
    echo "ERROR: --port must be a number (got '$PORT')"
    exit 2
fi

# ---------------- 1. find Python ----------------
# bucker needs Python 3.11 - 3.13 (>=3.11,<3.14; tested on 3.11/3.12).

# Print the exact one-liner for this machine's package manager (Windows
# users should use start.bat, which downloads and installs Python itself).
python_install_hint() {
    if command -v brew >/dev/null 2>&1; then
        echo "       macOS (Homebrew):  brew install python@3.12"
    elif command -v apt-get >/dev/null 2>&1; then
        echo "       Debian/Ubuntu:     sudo apt install python3.12"
    elif command -v dnf >/dev/null 2>&1; then
        echo "       Fedora/RHEL:       sudo dnf install python3.12"
    elif command -v pacman >/dev/null 2>&1; then
        echo "       Arch:              sudo pacman -S python"
    else
        echo "       Download from https://www.python.org/downloads/"
    fi
}

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo " [1/5] Python not found."
    echo "       bucker needs Python 3.11-3.13. Install it with:"
    python_install_hint
    echo "       then re-run ./start.sh"
    exit 1
fi

# verify version: 3.11 <= ver < 3.14
if ! "$PYTHON" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)' 2>/dev/null; then
    echo " [1/5] Unsupported Python version ($($PYTHON --version 2>&1)); need 3.11-3.13."
    echo "       Install Python 3.12 with:"
    python_install_hint
    echo "       then re-run ./start.sh"
    exit 1
fi
echo " [1/5] Python found: $($PYTHON --version 2>&1)"

# ---------------- 2. virtualenv ----------------
echo " [2/5] Setting up virtual environment..."
if [ ! -x ".venv/bin/python" ]; then
    "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# A venv created by uv has no pip; bootstrap it if missing.
if [ ! -x ".venv/bin/pip" ]; then
    echo "       bootstrapping pip in the virtualenv..."
    python -m ensurepip --upgrade >/dev/null 2>&1 || {
        echo " ERROR: could not set up the virtual environment."
        exit 1
    }
fi

# ---------------- 3. config (.env) ----------------
echo " [3/5] Checking configuration..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        # Fresh clones get a unique API token so the dashboard/API is not
        # stuck on the shared dev default. Python stdlib only — no deps yet.
        python -c 'import secrets; from pathlib import Path; p=Path(".env"); t=p.read_text(encoding="utf-8"); t=t.replace("BUCKER_API_TOKEN=dev-token","BUCKER_API_TOKEN="+secrets.token_hex(24)); p.write_text(t,encoding="utf-8")' 2>/dev/null || true
        echo "       created .env from .env.example (fresh API token generated)"
    else
        echo "       no .env or .env.example found — continuing with defaults"
    fi
else
    echo "       .env found — using your existing configuration"
fi

# Fail fast when the dashboard port is already taken (a leftover server
# answers health probes but serves stale state — seen in CI).
if python -c 'import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(("127.0.0.1",int(sys.argv[1])))==0 else 1)' "$PORT" 2>/dev/null; then
    echo " ERROR: port $PORT is already in use."
    echo "        Kill the old server, or run: ./start.sh --port <free-port>"
    exit 1
fi

# ---------------- 4. install ----------------
echo " [4/5] Installing bucker-agent (first run takes a minute)..."
if ! python -m pip install --quiet --disable-pip-version-check -e .; then
    echo " ERROR: pip install failed. Check your internet connection and try again."
    echo "        Full error (retrying verbosely):"
    python -m pip install --disable-pip-version-check -e .
    exit 1
fi
echo " [5/5] Starting bucker-agent lite mode..."
echo
echo "  dashboard:  http://localhost:$PORT"
echo "  first task: click New task -> type: create a file called hello.py that prints \"hello from the robot\""
echo "  (demo tasks need no API key; AI code tasks need a provider key in .env)"
echo "  press Ctrl+C to stop"
echo
python -m bucker.cli lite $NO_BROWSER --port "$PORT"
