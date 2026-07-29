"""Event type vocabulary for the bucker-agent event log.

[HAND] — read every line. The rest of the system agrees on reality through
these strings. Adding an event type here without adding a fold handler in
``bucker.core.state`` is a hard error at replay time, by design: an event
nobody knows how to interpret must never be silently ignored.

Rules:
  * Event types are past tense. They record what happened, never what should.
  * Never rename or repurpose a type. Historical streams contain the old name
    forever. Add a new type and bump ``SCHEMA_VERSION`` instead.
"""

from __future__ import annotations

from enum import StrEnum

# Bump when an existing event's payload shape changes incompatibly. Stored on
# every row so replay can branch on it across schema evolution.
SCHEMA_VERSION = 1


class EventType(StrEnum):
    """Every fact the system can record about a task."""

    # --- lifecycle -------------------------------------------------------
    TASK_CREATED = "TaskCreated"
    TASK_STARTED = "TaskStarted"
    TASK_COMPLETED = "TaskCompleted"
    TASK_FAILED = "TaskFailed"

    # --- planning (step 16) ----------------------------------------------
    PLAN_REQUESTED = "PlanRequested"
    PLAN_GENERATED = "PlanGenerated"
    SCHEMA_VALIDATION_FAILED = "SchemaValidationFailed"

    # --- execution (steps 17, 19) ----------------------------------------
    STEP_STARTED = "StepStarted"
    STEP_COMPLETED = "StepCompleted"
    TOOL_CALL_COMPLETED = "ToolCallCompleted"
    MODEL_CALL_COMPLETED = "ModelCallCompleted"
    MODEL_CALL_FAILED = "ModelCallFailed"
    WORKER_COMPLETED = "WorkerCompleted"

    # --- verification (steps 20-22) --------------------------------------
    VERIFICATION_REQUESTED = "VerificationRequested"
    VERIFICATION_PASSED = "VerificationPassed"
    VERIFICATION_FAILED = "VerificationFailed"
    RETRY_SCHEDULED = "RetryScheduled"
    NEEDS_HUMAN_REVIEW = "NeedsHumanReview"

    # --- guardrails (step 32) --------------------------------------------
    BUDGET_EXCEEDED = "BudgetExceeded"
    DEADLINE_EXCEEDED = "DeadlineExceeded"

    # --- corrections ------------------------------------------------------
    # The only way to "change" history: append a compensating event.
    CORRECTION_APPLIED = "CorrectionApplied"
    REDACTION_APPLIED = "RedactionApplied"


#: Terminal states — no further events expected after one of these.
TERMINAL_EVENTS: frozenset[EventType] = frozenset({
    EventType.TASK_COMPLETED,
    EventType.TASK_FAILED,
    EventType.NEEDS_HUMAN_REVIEW,
})
