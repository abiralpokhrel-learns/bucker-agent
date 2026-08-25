"""Delivery activity: announce task/graph completion where the user is."""

from __future__ import annotations

from typing import Any

from bucker.temporal_compat import activity


@activity.defn
async def notify_task_result(kind: str, result: dict[str, Any]) -> dict[str, Any]:
    """Send a completion notification (webhook/Telegram/Slack/Discord).

    No-op when nothing is configured; delivery failures are swallowed —
    a notification must never fail the task it announces.
    """
    from bucker.core.notify import deliver_event, is_configured

    if not is_configured():
        return {"delivered": False, "reason": "not configured"}
    return await deliver_event(kind, result)
