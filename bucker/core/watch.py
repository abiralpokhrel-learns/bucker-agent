"""Task watching: follow a task's event stream until it reaches a verdict.

Extracted from the CLI so the logic is importable and testable without an
argparse surface, and usable by any front door (CLI today, MCP or scripts
tomorrow). Works identically on the full stack and in lite mode because it
reads only through EventStore/SnapshotStore — no Temporal client involved.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from uuid import UUID

#: Statuses after which no further events are expected. Mirrors the
#: terminal events the pipeline records (plus human verdicts and cancel).
TERMINAL_STATUSES = frozenset({
    "completed",
    "failed",
    "halted",
    "needs_human_review",
    "human_approved",
    "human_rejected",
    "cancelled",
})

#: Exit-code classes for `bucker wait`: 0 = good outcome, 1 = bad outcome,
#: 2 = inconclusive (escalated to a human — neither passed nor failed).
GOOD_OUTCOMES = {"completed", "human_approved"}
INCONCLUSIVE_OUTCOMES = {"needs_human_review"}


#: Event types that mark the task's terminal verdicts. Status is derived
#: from the events actually CONSUMED during the watch (never re-queried
#: afterwards), so everything reported has been printed — a terminal event
#: landing between a read_stream and a get_state must not vanish silently.
TERMINAL_EVENT_STATUSES = {
    "TaskCompleted": "completed",
    "TaskFailed": "failed",
    "BudgetExceeded": "halted",
    "DeadlineExceeded": "halted",
    "NeedsHumanReview": "needs_human_review",
    "HumanApproved": "human_approved",
    "HumanRejected": "human_rejected",
}


def is_terminal(status: str | None) -> bool:
    return status in TERMINAL_STATUSES


def exit_code_for(status: str | None) -> int:
    """Map a terminal status to a shell exit code (see class docstring)."""
    if status in GOOD_OUTCOMES:
        return 0
    if status in INCONCLUSIVE_OUTCOMES:
        return 2
    return 1


def format_event_line(event: Any) -> str:
    """One compact line per event, matching `bucker events` output style."""
    import json

    created = getattr(event, "created_at", None)
    stamp = created.strftime("%H:%M:%S") if hasattr(created, "strftime") else "?"
    payload = getattr(event, "payload", {}) or {}
    ref = getattr(event, "tool_output_ref", None)
    suffix = f"  ref={ref}" if ref else ""
    return (
        f"{getattr(event, 'id', '?'):>5}  {stamp}  "
        f"{getattr(event, 'event_type', '?'):<24}"
        f"{json.dumps(payload, default=str)}{suffix}"
    )


async def watch_task(
    store: Any,
    snaps: Any,
    task_id: UUID,
    *,
    interval_s: float = 1.0,
    timeout_s: float = 3600,
    sink: Any = print,
) -> str | None:
    """Follow one task's stream, printing new events as they land.

    Returns the terminal status, or None when the timeout elapsed first.
    Terminality is judged from the events this call has CONSUMED (plus one
    initial state read for already-finished tasks), so every verdict
    reported here was also printed — no event disappears between the read
    and the check.

    Caveat (lite mode): a cancelled runner records no terminal event, so a
    cancelled task watches through to the timeout; that matches lite's
    honest "runs to completion in one process or not at all" contract.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s

    # Already-terminal tasks (watching after the fact) are answered from
    # reconstructed state before any tailing starts.
    state = await snaps.get_state(task_id)
    status = state.get("status")
    if is_terminal(status):
        return status

    last_id = 0
    while True:
        events = await store.read_stream(task_id, after_id=last_id)
        for event in events:
            last_id = max(last_id, event.id)
            sink(format_event_line(event))
            derived = TERMINAL_EVENT_STATUSES.get(event.event_type)
            if derived is not None:
                return derived

        if loop.time() >= deadline:
            return None
        await asyncio.sleep(interval_s)


async def wait_for_status(
    snaps: Any,
    task_id: UUID,
    *,
    interval_s: float = 1.0,
    timeout_s: float = 3600,
) -> str | None:
    """Poll reconstructed state until terminal; quiet variant of watch_task."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        with contextlib.suppress(Exception):
            state = await snaps.get_state(task_id)
            status = state.get("status")
            if is_terminal(status):
                return status
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(interval_s)
