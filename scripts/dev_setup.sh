#!/usr/bin/env bash
# One-shot local setup. Idempotent — safe to re-run.
set -euo pipefail

echo "==> Postgres"
docker compose up -d
until docker compose exec -T postgres pg_isready -U postgres -d bucker >/dev/null 2>&1; do
  sleep 1
done

echo "==> Python deps"
uv sync --extra dev

echo "==> Migrations"
uv run python -m bucker.cli migrate

cat <<'MSG'

Setup complete. Two terminals from here:

  1)  temporal server start-dev          # UI: http://localhost:8233
  2)  uv run python -m bucker.worker

Then:
      uv run python -m bucker.cli start --objective "my first task" --wait
      uv run python -m tests.crash_test   # the M1 durability proof
      uv run python -m pytest -q          # test suite (python -m, never pytest.exe)
MSG
