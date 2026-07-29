-- bucker-agent :: 001_init.sql
-- Phase 0, step 5. Event-sourced core schema (Database Design doc 13).
--
-- Design rules encoded here, do not "simplify" them later:
--   * events is APPEND-ONLY at the DB permission level, not by convention.
--     The application role gets INSERT + SELECT and nothing else. Corrections
--     happen via new compensating events, never UPDATE/DELETE.
--   * events.id is BIGSERIAL so ordering within a task stream is monotonic.
--   * idempotency_key makes activity retries safe: the same logical step
--     appended twice collapses to one row (see uq_events_idempotency).
--
-- Idempotent: safe to run repeatedly.

BEGIN;

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ---------------------------------------------------------------- tasks ----
CREATE TABLE IF NOT EXISTS tasks (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_id        UUID REFERENCES tasks (id),
    task_type        TEXT        NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'pending',
    objective        TEXT,
    budget_usd       NUMERIC(12, 6),
    deadline         TIMESTAMPTZ,
    verifier         TEXT,
    current_snapshot JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tasks_status_check CHECK (status IN (
        'pending', 'in_progress', 'verification_failed',
        'needs_human_review', 'completed', 'failed', 'halted'
    ))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);

-- --------------------------------------------------------------- events ----
CREATE TABLE IF NOT EXISTS events (
    id              BIGSERIAL PRIMARY KEY,
    task_id         UUID        NOT NULL REFERENCES tasks (id),
    event_type      TEXT        NOT NULL,
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    schema_version  INT         NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    tool_output_ref TEXT,
    idempotency_key TEXT
);

-- Ordered replay of one task's stream. The workhorse index.
CREATE INDEX IF NOT EXISTS idx_events_task_id_id ON events (task_id, id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);

-- Exactly-once append per logical step. A retried Temporal activity that
-- re-appends with the same key hits this and is silently deduped.
CREATE UNIQUE INDEX IF NOT EXISTS uq_events_idempotency
    ON events (task_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- ------------------------------------------------------------ snapshots ----
CREATE TABLE IF NOT EXISTS snapshots (
    task_id     UUID        NOT NULL REFERENCES tasks (id),
    version     INT         NOT NULL,          -- = events.id of last folded event
    state       JSONB       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, version)
);

-- ------------------------------------------------------------ telemetry ----
-- Populated from step 31; created now so cost/latency capture has a home
-- from the first model call rather than being retrofitted.
CREATE TABLE IF NOT EXISTS telemetry (
    event_id            BIGINT PRIMARY KEY REFERENCES events (id),
    task_id             UUID   NOT NULL REFERENCES tasks (id),
    model_used          TEXT,
    tool_used           TEXT,
    latency_ms          INT,
    cost_usd            NUMERIC(12, 6),
    verification_result TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_telemetry_model_created
    ON telemetry (model_used, created_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_task ON telemetry (task_id);

-- ----------------------------------------------------------- candidates ----
-- Offline evaluation pipeline (Phase 3, steps 36-38). Empty until then.
CREATE TABLE IF NOT EXISTS candidates (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    description      TEXT NOT NULL,
    config_patch     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    benchmark_result JSONB,
    status           TEXT        NOT NULL DEFAULT 'proposed',
    approved_by      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT candidates_status_check CHECK (status IN (
        'proposed', 'benchmarked', 'approved', 'rejected', 'promoted', 'rolled_back'
    ))
);

-- ------------------------------------------------------ append-only role ----
-- The application connects as bucker_app. It cannot rewrite history.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bucker_app') THEN
        CREATE ROLE bucker_app LOGIN PASSWORD 'dev';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO bucker_app;

GRANT INSERT, SELECT                 ON events     TO bucker_app;
GRANT INSERT, SELECT, UPDATE, DELETE ON tasks      TO bucker_app;
GRANT INSERT, SELECT, UPDATE, DELETE ON snapshots  TO bucker_app;
GRANT INSERT, SELECT                 ON telemetry  TO bucker_app;
GRANT INSERT, SELECT, UPDATE         ON candidates TO bucker_app;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bucker_app;

-- Explicitly revoke, in case a previous grant was wider. This is the line that
-- makes "the event log is the truth" enforceable rather than aspirational.
REVOKE UPDATE, DELETE, TRUNCATE ON events FROM bucker_app;

COMMIT;
