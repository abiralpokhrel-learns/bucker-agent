"""Result delivery (gateway, iter 7): where the user is, not just the dashboard.

A finished task should tell you it is done — on a webhook, or on Telegram.
Both are OPT-IN: with nothing configured, every function here is a no-op
that costs nothing. Message construction is pure (testable); the POST is a
bounded asyncio call with a hard timeout — delivery failure is logged,
never raised, because a notification must not fail the task it announces.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
from typing import Any


def build_task_message(result: dict[str, Any]) -> str:
    """The text delivered on completion. Pure.

    result is the workflow's terminal dict (status, attempts, verdict…).
    """
    status = result.get("status", "?")
    attempts = result.get("attempts", "?")
    verdict = result.get("verdict") or {}
    passed = verdict.get("passed")
    verifier = verdict.get("verifier", "")

    if passed is True:
        line = f"✅ bucker: task {status} — verifier {verifier} passed"
    elif passed is False:
        line = f"❌ bucker: verification failed via {verifier}"
    elif status == "needs_human_review":
        line = "👀 bucker: task needs human review — approve or reject it"
    elif status == "halted":
        line = f"⏹ bucker: task halted — {result.get('reason', '')[:80]}"
    else:
        line = f"bucker: task {status}"
    return f"{line} (attempts: {attempts})"


def build_graph_message(result: dict[str, Any]) -> str:
    """Graph completion message: step summary. Pure."""
    steps = result.get("steps") or {}
    failed = result.get("failed") or []
    total = len(steps)
    if failed:
        line = f"❌ bucker graph done: {total} step(s), {len(failed)} failed"
    else:
        line = f"✅ bucker graph done: {total} step(s) verified"
    return f"{line} — {', '.join(failed) if failed else 'all passed'}"[:400]


def is_configured() -> bool:
    from bucker.config import settings

    return bool(settings.notify_webhook_url or
                (settings.telegram_bot_token and settings.telegram_chat_id))


def _payload_for(message: str) -> dict:
    from bucker.config import settings

    if settings.telegram_bot_token and settings.telegram_chat_id:
        return {
            "kind": "telegram",
            "url": (
                f"https://api.telegram.org/bot{settings.telegram_bot_token}"
                f"/sendMessage"
            ),
            "body": {"chat_id": settings.telegram_chat_id, "text": message},
        }
    return {
        "kind": "webhook",
        "url": settings.notify_webhook_url,
        "body": {"text": message},
    }


async def deliver(message: str) -> dict[str, Any]:
    """Send one message. Never raises; returns what happened."""
    from bucker.config import settings

    if not settings.notify_webhook_url and not (
        settings.telegram_bot_token and settings.telegram_chat_id
    ):
        return {"delivered": False, "reason": "not configured"}

    payload = _payload_for(message)
    try:
        from urllib.parse import urlparse

        parsed = urlparse(payload["url"])
        path = parsed.path or "/"
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                parsed.hostname, parsed.port or 443,
                ssl=ssl.create_default_context(),
            ),
            timeout=10,
        )
        try:
            body = json.dumps(payload["body"]).encode()
            req = (
                f"POST {path} HTTP/1.1\r\nHost: {parsed.hostname}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode() + body
            writer.write(req)
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), timeout=10)
            ok = b" 2" in status[:12]
            return {"delivered": ok, "kind": payload["kind"],
                    "status": status.decode().strip()}
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    except Exception as exc:  # noqa: BLE001 — delivery must not raise
        return {"delivered": False, "kind": payload["kind"],
                "reason": f"{type(exc).__name__}: {str(exc)[:100]}"}
