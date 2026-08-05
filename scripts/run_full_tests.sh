#!/usr/bin/env bash
# Run the bucker test suite with a superuser DB URL (the conftest fixture
# executes migrations as the test connection, so it needs CREATE rights).
# Password is never printed. Local dev compose password: postgres/dev.
set -a
source .env
set +a
export BUCKER_TEST_DATABASE_URL="postgresql://postgres:dev@localhost:5432/bucker"
cd "$(dirname "$0")/.."
# NOTE: pyproject addopts already carries -q; adding another would make
# -qq and suppress the final summary line.
exec .venv/Scripts/python.exe -m pytest "$@" --ignore=tests/crash_test.py
