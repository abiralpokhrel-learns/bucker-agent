# Operations

Production-readiness procedures for bucker-agent. Everything here is a
*drill you can run today* — this file exists to be executed, not admired.

## Backup / restore

Backups must cover **Postgres + blobstore together**: the event store and
telemetry live in Postgres, verifier diagnostics/diffs live in
`./blobstore`. A dump without its blobs is a broken audit trail.

### Take a backup

```bash
uv run python -m scripts.backup --dest backups --keep 7
```

Creates `backups/<UTC-timestamp>/` containing `db.dump` (custom-format
pg_dump via `docker exec bucker-pg`) and `blobstore.tar.gz`. Older
backups beyond `--keep` are pruned. Schedule it (cron / Temporal
schedule) as often as your RPO demands — hourly for anything you care
about.

### Restore drill (do this quarterly, on a scratch database)

```bash
# 1. stop the API/worker so nothing writes
# 2. recreate the database from the dump
docker exec -i bucker-pg pg_restore -U postgres -d bucker --clean \
  < backups/<stamp>/db.dump
# 3. restore the blobstore
rm -rf blobstore && tar -xzf backups/<stamp>/blobstore.tar.gz
# 4. verify: task count + event count match the pre-backup values
uv run python -m bucker.cli tasks --limit 5
```

A restore that has not been rehearsed is a wish, not a procedure. If the
scratch DB is too much, at minimum verify the dump restores and the
`events` count matches `SELECT COUNT(*) FROM events`.

## Monitoring & alerts

- **Doctor** (`uv run python -m scripts.doctor`) is the health probe:
  config, providers, Postgres, Docker, Temporal, sandbox image. Run it on
  a schedule and alert on any `[FAIL]`.
- **`/api/system`** is the JSON form (provider reachability, key shapes,
  infra status). Poll it from your monitoring; alert on `"ok": false`.
- **Free-tier panel** (`/usage`): the documented-caps panel shows when a
  free model is near its daily limit — the failure mode is silent 429s
  mid-task, so check it before batch runs.
- **Event store growth**: `SELECT count(*), max(created_at) FROM events`
  and `pg_size_pretty(pg_total_relation_size('events'))`. Events are
  append-only by design — plan retention (below).

## Log retention

- Postgres: retain as long as your audit requirements demand (append-only
  is a feature; the DB is the record). Monitor disk with the size query
  above.
- Blobstore: diagnostics accumulate per verification. Same retention as
  the DB — they are referenced by `tool_output_ref` from events.
- Worker/API stdout: keep 30 days on disk, ship to a log collector in
  production. No PII is logged by design (secrets are never printed),
  so retention is a cost decision, not a compliance one.

## Migrations

- Migrations are idempotent `.sql` files under `migrations/`, applied in
  filename order by `bucker migrate` (re-runs everything — every file
  must be safe to re-run).
- **Rollback policy**: because files are append-only and idempotent,
  rollback is *forward*, not reverse: a new migration undoes the previous
  one's effect. Never edit a shipped migration — the audit trail depends
  on it.
- **Practice**: apply migrations during a maintenance window; run
  `scripts/backup.py` first (below).

## Incidents

1. **Task stuck in `pending`/`in_progress` forever.** The workflow is
   the source of truth. `temporal workflow list | grep <task-id>` — if
   the workflow is gone but the row is stuck, the folded-status cache
   drifted: run `uv run python -m scripts.backfill_status` (replays
   events, idempotent). If the workflow is Running but idle, check the
   worker is alive and the task queue has a poller
   (`temporal worker list`).
2. **Everything 503s (degraded mode).** The API cannot reach Postgres —
   it answers 503 + a banner instead of crashing. Check `docker ps`,
   `docker logs bucker-pg`, then `scripts.doctor`. The API recovers
   automatically when the pool returns.
3. **Model calls failing / free-tier exhausted.** `/system` shows
   provider reachability; the fallback chain (local Ollama → free
   OpenRouter) engages automatically. The free-tier panel on `/usage`
   shows the daily cap remaining. Options: wait for the UTC reset, add
   credits, or switch `BUCKER_MODEL`.
4. **Worker crashes mid-task.** Nothing to do — that is the M1 property.
   `tests/crash_test.py` demonstrates it: kill the worker, Temporal
   reschedules after the activity timeout, the task completes exactly
   once. Restart the worker.
5. **Suspect data loss.** Events are append-only at the DB permission
   level (bucker_app has INSERT+SELECT only). Verify a stream with
   replay: `uv run python -m bucker.cli replay <task_id>` and compare
   the reconstructed state against the snapshot store. If a stream is
   truncated, restore from backup (above) — and check who had DDL rights.

## Upgrades

1. `git pull` / deploy new code.
2. `uv sync --extra dev --extra llm --extra mcp`
3. Backup: `uv run python -m scripts.backup`
4. Migrations: `uv run python -m bucker.cli migrate`
5. Restart worker + API.
6. Smoke: `scripts.doctor`, one live task, `tasks/crash_test.py` if the
   workflow layer changed.

## The M2 gate (benchmark evidence)

Production claims (promotion, regression gates) hang on published
benchmark numbers. The gate is a **paired run**: bucker vs a baseline
model on the same SWE-bench Lite instances:

```bash
uv run python -m scripts.m2_gate --instances 25 --model <model>
uv run python -m scripts.m2_gate --instances 5   # smoke: not publishable
```

The gate requires the SWE-bench Lite dataset to be prepared (instance
checkouts, test specs). Do NOT publish numbers from `< 5` instances — the
script itself refuses to draw conclusions. A passing run writes its
statistics to `EXPERIMENT_LOG`; publish that + the run id in the README
when the project flips from Alpha.

## Deployment hardening (pre-publish checklist)

- [ ] `BUCKER_API_TOKEN` set to a real secret (doctor warns on the
      dev-token default); HTTPS-only exposure; never `dev-token` in prod
- [ ] Strong Postgres password; non-dev role for the app (bucker_app is
      already least-privilege: INSERT+SELECT only, no DDL)
- [ ] Secrets via your secret manager, never committed (`.env` is
      git-ignored — keep it that way)
- [ ] Docker images pinned by digest (compose Postgres + sandbox base
      are already pinned; refresh per the comments in those files)
- [ ] Migrations applied by an owner role from a controlled pipeline,
      not ad hoc shells
- [ ] Backup schedule + a rehearsed restore (above)
- [ ] Run the M2 gate and publish real numbers (above)
