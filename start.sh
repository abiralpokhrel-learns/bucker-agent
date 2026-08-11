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

echo
echo " =========================================="
echo "   bucker-agent  -  lite mode"
echo "   nothing but Python required"
echo " =========================================="
echo

# ---------------- 1. find Python ----------------
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo " [1/4] Python not found."
    echo "       Install Python 3.11+ from https://www.python.org/downloads/"
    echo "       (or: brew install python / apt install python3), then re-run ./start.sh"
    exit 1
fi

# verify version
VERSION_OK=$("$PYTHON" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')
if [ "$VERSION_OK" != "1" ]; then
    echo " [1/4] Python too old ($($PYTHON --version 2>&1)); need 3.11+."
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
