-- Schedule-failed status (scheduler + reconciliation, iter 2).
--
-- api/app.py and core/tasks.py write 'schedule_failed' when a schedule
-- workflow cannot be started (e.g. the sandbox image is missing in CI):
--    bucker/api/app.py:325  "pending" if workflow_id else "schedule_failed"
--    bucker/core/tasks.py:191  UPDATE tasks SET status = 'schedule_failed'
-- The original tasks_status_check (001) and the human-review relaxation
-- (003) did not allow it, so those writes violated the CHECK on a real
-- database. Idempotent: drop-then-add, same pattern as 003.
DO $$
BEGIN
    ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check;
    ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (status IN (
        'pending', 'in_progress', 'verification_failed',
        'needs_human_review', 'completed', 'failed', 'halted',
        'human_approved', 'human_rejected', 'schedule_failed'
    ));
END $$;
