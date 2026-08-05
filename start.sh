#!/usr/bin/env bash
# One-command launcher (macOS / Linux / WSL):
#
#     git clone https://github.com/abiralpokhrel-learns/bucker-agent
#     cd bucker-agent
#     ./start.sh
#
# Installs uv if missing, then runs `bucker dev` — which bootstraps on
# first run (prereqs, .env + token, Postgres, migrations) and starts
# Temporal + worker + dashboard, opening the browser when ready.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found — installing (Astral installer)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

exec uv run python -m bucker.cli dev "$@"
