#!/usr/bin/env bash
# ============================================================
#  bucker-agent  -  one-click lite launcher (macOS / Linux)
#
#  Runs the whole platform with NOTHING but Python installed:
#  no Docker, no Postgres, no Temporal, no uv.
#
#    ./start.sh
#
#  It will:
#    1. Check for Python 3.11+ (and say how to install it if missing)
#    2. Create a virtualenv
#    3. Install bucker-agent + its Python dependencies
#    4. Start the dashboard at http://localhost:8123
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
    echo " [1/4] Python not found."
    echo "       bucker needs Python 3.11-3.13. Install it with:"
    python_install_hint
    echo "       then re-run ./start.sh"
    exit 1
fi

# verify version: 3.11 <= ver < 3.14
if ! "$PYTHON" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)' 2>/dev/null; then
    echo " [1/4] Unsupported Python version ($($PYTHON --version 2>&1)); need 3.11-3.13."
    echo "       Install Python 3.12 with:"
    python_install_hint
    echo "       then re-run ./start.sh"
    exit 1
fi
echo " [1/4] Python found: $($PYTHON --version 2>&1)"

# ---------------- 2. virtualenv ----------------
echo " [2/4] Setting up virtual environment..."
if [ ! -x ".venv/bin/python" ]; then
    "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ---------------- 3. install ----------------
echo " [3/4] Installing bucker-agent (this may take a minute)..."
python -m pip install --quiet --disable-pip-version-check -e .
echo " [4/4] Starting bucker-agent lite mode..."
echo
echo "  dashboard will open at:  http://localhost:8123"
echo "  press Ctrl+C to stop"
echo
python -m bucker.cli lite
