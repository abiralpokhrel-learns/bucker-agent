"""Delivery activity: announce task/graph completion where the user is."""

from __future__ import annotations

from typing import Any

from bucker.temporal_compat import activity


@activity.defn
async def notify_task_result(kind: str, result: dict[str, Any]) -> dict[str, Any]:
    """Send a completion notification (webhook or Telegram).

    No-op when nothing is configured; delivery failures are swallowed —
    a notification must never fail the task it announces.
    """
    from bucker.core.notify import (
        build_graph_message,
        build_task_message,
        deliver,
        is_configured,
    )

    if not is_configured():
        return {"delivered": False, "reason": "not configured"}
    message = build_graph_message(result) if kind == "graph" \
        else build_task_message(result)
    return await deliver(message)
