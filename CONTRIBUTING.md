# Contributing to bucker-agent

Thanks for wanting to help. This file tells you where to start.

**Repo:** <https://github.com/abiralpokhrel-learns/bucker-agent> — issues,
PRs, and discussions live there.

## The one rule

**Files marked `[HAND]` in their module docstring must not be regenerated casually.** These are the parts where a subtle bug silently poisons everything downstream: the event store, the state fold, the replay engine, the sandbox config, and the stats module. Read every line. If you can't explain a line in those files, rewrite it until you can.

Everything else — configs, dashboards, plumbing — is fine to generate, but the test suite is the gate. Nothing merges without passing.

## Getting started

```bash
git clone https://github.com/abiralpokhrel-learns/bucker-agent && cd bucker-agent
cp .env.example .env
docker compose up -d                   # Postgres
temporal server start-dev              # UI at http://localhost:8233
uv sync --extra dev
uv run python -m bucker.cli migrate
uv run python -m pytest                # all pure tests, no infra needed
```

## Running the full test suite

```bash
# Pure tests (always run, no infra needed):
uv run python -m pytest

# With Postgres (Docker must be running):
BUCKER_TEST_DATABASE_URL=postgresql://postgres:dev@localhost:5432/bucker uv run python -m pytest

# With Docker sandbox:
uv run python -m pytest  # sandbox integration tests auto-detect Docker
```

## Project conventions

- **Python 3.12+**, managed with `uv`.
- **Line length**: 96 chars (configured in `pyproject.toml`).
- **Lint**: `uv run python -m ruff check .`
- **Format**: let ruff handle it; no separate formatter.
- **Tests**: pytest with `asyncio_mode = "auto"`. No test framework besides pytest.
- **Never invoke tools via `.exe` shims** on Windows — use `python -m <tool>` instead. The `.exe` launchers break when the project folder moves and may be blocked by Smart App Control.

## Where to help

1. **Run the M2 gate** — the paired benchmark needs live SWE-bench runs. `uv run python -m scripts.m2_gate --instances 25`.
2. **Second verifier** — the citation checker (`bucker/verifiers/citation_checker.py`) is a stub. A fuzzy-matching version with entity extraction would make it real.
3. **Adaptive planning** — `bucker/adaptive.py` has the strategy selector. Wire it into the workflow and measure whether it reduces repeat-failure rate.
4. **Dashboard** — the `/tasks/{id}/dashboard` endpoint renders simple HTML. Add charts, cost-over-time graphs, and a run history view.
5. **More verifiers** — add a `docs_consistency` verifier, a `security_scan` verifier, a `performance_regression` verifier. The plugin interface is in `bucker/verifiers/base.py`.

## Architecture decisions

All significant design choices are recorded in `docs/decisions.md`. Before proposing a change, check whether the decision has already been debated there. If you're overturning one, write a new ADR.

## The vibe-code rule (applied to contributors, not just code)

Generated code is untrusted worker output. Your PR's test suite is its verifier. Nothing merges without passing, no matter who wrote it. The same principle the platform applies to its own agents, applied to its own construction.
