-- Human-in-the-loop review statuses (approval gate, iter 1).
-- review_task writes human_approved / human_rejected, which the original
-- tasks_status_check did not allow — the UPDATE would have violated the
-- CHECK on a real database. Idempotent: drop-then-add.
DO $$
BEGIN
    ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check;
    ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (status IN (
        'pending', 'in_progress', 'verification_failed',
        'needs_human_review', 'completed', 'failed', 'halted',
        'human_approved', 'human_rejected'
    ));
END $$;
