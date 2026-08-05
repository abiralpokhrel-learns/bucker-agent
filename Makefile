# bucker-agent — convenience wrappers around `bucker` (the CLI is the
# source of truth; this file just shortens the common commands).
#
#     make dev     # THE one command: first run bootstraps, then starts the stack
#     make setup   # explicit bootstrap (checks, .env, Postgres, migrations)
#     make test    # full test suite (CI parity)
#     make lint    # ruff

.PHONY: dev setup test lint

dev: ## clone -> make dev -> dashboard
	uv run python -m bucker.cli dev

setup: ## one-command environment bootstrap
	uv run python -m bucker.cli setup

test: ## full test suite (CI parity; needs Docker + Postgres)
	bash scripts/run_full_tests.sh tests/

lint: ## ruff check
	uv run ruff check bucker/ tests/
