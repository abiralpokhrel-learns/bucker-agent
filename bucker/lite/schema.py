"""Lite-mode schema: mirrors migrations/*.sql in SQLite dialect.

The full stack uses Postgres (BIGSERIAL, JSONB, TIMESTAMPTZ, partial
unique indexes, gen_random_uuid). SQLite has none of those, but the
application layer only ever speaks through EventStore/SnapshotStore/plain
queries, so the lite schema just needs the same *columns* with
compatible types:

* ``id`` / ``task_id`` -> TEXT (stored as uuid strings; LiteRow decodes
  them back to UUID objects on read).
* ``payload`` / ``state`` / ``current_snapshot`` -> TEXT (JSON; LiteRow
  decodes on read).
* timestamps -> TEXT (ISO-8601 UTC; LiteRow decodes to datetime).
* ``events.id`` -> INTEGER PRIMARY KEY AUTOINCREMENT (BIGSERIAL analog);
  monotonic per stream is all the code relies on.
* The idempotency unique index is a *partial* index, exactly like the
  Postgres original (``WHERE idempotency_key IS NOT NULL``) so that
  NULL keys never collide.

The append-only property of ``events`` is NOT enforced by SQLite
permissions (SQLite has no roles); lite mode is for trusted code, and
the full Postgres path keeps the hard guarantee.
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,
    parent_id        TEXT,
    task_type        TEXT        NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'pending',
    objective        TEXT,
    budget_usd       REAL,
    deadline         TEXT,
    verifier         TEXT,
    current_snapshot TEXT,
    created_at       TEXT        NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT        NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT        NOT NULL REFERENCES tasks (id),
    event_type      TEXT        NOT NULL,
    payload         TEXT        NOT NULL DEFAULT '{}',
    schema_version  INTEGER     NOT NULL DEFAULT 1,
    created_at      TEXT        NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    tool_output_ref TEXT,
    idempotency_key TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_task_id_id ON events (task_id, id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);

CREATE UNIQUE INDEX IF NOT EXISTS uq_events_idempotency
    ON events (task_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS snapshots (
    task_id     TEXT        NOT NULL REFERENCES tasks (id),
    version     INTEGER     NOT NULL,
    state       TEXT        NOT NULL,
    created_at  TEXT        NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (task_id, version)
);

CREATE TABLE IF NOT EXISTS telemetry (
    event_id            INTEGER PRIMARY KEY REFERENCES events (id),
    task_id             TEXT   NOT NULL REFERENCES tasks (id),
    model_used          TEXT,
    tool_used           TEXT,
    latency_ms          INTEGER,
    cost_usd            REAL,
    verification_result TEXT,
    purpose             TEXT,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    total_tokens        INTEGER,
    created_at          TEXT   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_telemetry_model_created
    ON telemetry (model_used, created_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_task ON telemetry (task_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_purpose
    ON telemetry (purpose, created_at);

CREATE TABLE IF NOT EXISTS candidates (
    id               TEXT PRIMARY KEY,
    description      TEXT NOT NULL,
    config_patch     TEXT NOT NULL DEFAULT '{}',
    benchmark_result TEXT,
    status           TEXT NOT NULL DEFAULT 'proposed',
    approved_by      TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS gateway_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        TEXT NOT NULL,
    tenant_id         TEXT NOT NULL DEFAULT 'default',
    purpose           TEXT,
    provider          TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    outcome           TEXT NOT NULL,
    error_type        TEXT,
    attempt_count     INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_gateway_usage_day
    ON gateway_usage (provider, model, created_at);
CREATE INDEX IF NOT EXISTS idx_gateway_usage_request
    ON gateway_usage (request_id);
"""
