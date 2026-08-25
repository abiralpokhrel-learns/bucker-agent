"""Result delivery (gateway, iter 7): where the user is, not just the dashboard.

A finished task should tell you it is done — on a webhook, Telegram,
Slack, or Discord. All channels are OPT-IN: with nothing configured, every
function here is a no-op that costs nothing. Message construction is pure
(testable); the POST is a bounded asyncio call with a hard timeout —
delivery failure is logged, never raised, because a notification must not
fail the task it announces.

Channel precedence when several are configured (deterministic, one
delivery per event — no fan-out spam): Telegram > Slack > Discord >
generic webhook.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import ssl
import time
from typing import Any

#: Discord rejects messages over 2000 characters with a 400; truncate to
#: stay under the cap including our envelope overhead.
_DISCORD_MAX_CHARS = 1900

#: Signature header name for the generic webhook channel. GitHub-style
#: scheme: ``t=<unix-ts>,v1=<hex hmac-sha256(secret, f"{ts}.{body}")>``.
SIGNATURE_HEADER = "X-Bucker-Signature"


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


def build_event_data(kind: str, result: dict[str, Any]) -> dict[str, Any]:
    """Structured fields for receivers that want more than prose. Pure.

    Flattened into the generic webhook body alongside ``text``; chat
    channels (Telegram/Slack/Discord) get only the prose message.
    """
    verdict = result.get("verdict") or {}
    return {
        "event": kind,
        "status": result.get("status"),
        "task_id": result.get("task_id"),
        "attempts": result.get("attempts"),
        "cost_usd": result.get("cost_usd"),
        "verifier_passed": verdict.get("passed"),
        "verifier": verdict.get("verifier"),
    }


# ----------------------------------------------------- webhook signing ----


def sign_payload(secret: str, body: bytes, *,
                 timestamp: int | None = None) -> str:
    """The X-Bucker-Signature header value for one webhook POST. Pure.

    Scheme (GitHub-compatible shape): HMAC-SHA256 over
    ``f"{timestamp}.{body}"`` with the shared secret, emitted as
    ``t=<ts>,v1=<hex>``. The timestamp rides in the header so receivers
    can reject replays; the body is bound into the MAC so it cannot be
    swapped after signing.
    """
    ts = int(time.time()) if timestamp is None else int(timestamp)
    mac = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={mac}"


def verify_webhook_signature(
    secret: str,
    header: str,
    body: bytes,
    *,
    tolerance_s: int = 300,
    now: int | None = None,
) -> bool:
    """Receiver-side check of an X-Bucker-Signature header. Pure.

    Rejects stale timestamps (replay window ``tolerance_s``), malformed
    headers, and any MAC mismatch — in constant time per comparison.
    Multiple ``v1=`` entries are allowed (key rotation); one match wins.
    """
    if not secret or not header:
        return False
    timestamp: int | None = None
    candidates: list[str] = []
    for part in header.split(","):
        part = part.strip()
        if part.startswith("t="):
            try:
                timestamp = int(part[2:])
            except ValueError:
                return False
        elif part.startswith("v1="):
            candidates.append(part[3:])
    if timestamp is None or not candidates:
        return False

    current = int(time.time()) if now is None else int(now)
    if abs(current - timestamp) > tolerance_s:
        return False

    expected_base = f"{timestamp}.".encode() + body
    for candidate in candidates:
        mac = hmac.new(secret.encode(), expected_base, hashlib.sha256)
        if hmac.compare_digest(mac.hexdigest(), candidate):
            return True
    return False


def configured_channels() -> list[str]:
    """Which delivery channels have everything they need, in precedence order."""
    from bucker.config import settings

    channels: list[str] = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        channels.append("telegram")
    if settings.slack_webhook_url:
        channels.append("slack")
    if settings.discord_webhook_url:
        channels.append("discord")
    if settings.notify_webhook_url:
        channels.append("webhook")
    return channels


def is_configured() -> bool:
    return bool(configured_channels())


def _payload_for(message: str, data: dict[str, Any] | None = None) -> dict:
    """Build (kind, url, body) for the highest-precedence configured channel.

    ``data`` (structured event fields) is flattened into the GENERIC
    webhook body only — chat platforms reject or misrender unknown keys,
    and their payloads stay minimal on purpose.
    """
    from bucker.config import settings

    channels = configured_channels()
    # Callers guard on is_configured(); this branch keeps the function total.
    if not channels:
        return {"kind": "none", "url": "", "body": {}}

    kind = channels[0]
    if kind == "telegram":
        return {
            "kind": "telegram",
            "url": (
                f"https://api.telegram.org/bot{settings.telegram_bot_token}"
                f"/sendMessage"
            ),
            "body": {"chat_id": settings.telegram_chat_id, "text": message},
        }
    if kind == "slack":
        # Slack incoming webhooks accept {"text": ...} and format it.
        return {
            "kind": "slack",
            "url": settings.slack_webhook_url,
            "body": {"text": message},
        }
    if kind == "discord":
        return {
            "kind": "discord",
            "url": settings.discord_webhook_url,
            "body": {"content": message[:_DISCORD_MAX_CHARS]},
        }
    body: dict[str, Any] = {"text": message}
    if data:
        body.update(data)
    return {
        "kind": "webhook",
        "url": settings.notify_webhook_url,
        "body": body,
    }


def _validate_target(url: str) -> str:
    """SSRF guard (hardening review): only https/http to a PUBLIC host.

    Raises ValueError for anything else — the notification path must
    never be a vector into internal services.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"unsupported scheme {parsed.scheme!r} (https/http only)")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("missing hostname")

    # Literal private/loopback/link-local addresses are refused outright.
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None:
        if addr.is_private or addr.is_loopback or addr.is_link_local \
                or addr.is_reserved or addr.is_multicast:
            raise ValueError(f"refusing to deliver to private address {host}")
        return url

    # Hostname: resolve and re-check (bounded; refusal beats a hang).
    try:
        resolved = socket.gethostbyname(host)
        addr = ipaddress.ip_address(resolved)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"cannot resolve {host}") from exc
    if addr.is_private or addr.is_loopback or addr.is_link_local \
            or addr.is_reserved or addr.is_multicast:
        raise ValueError(f"refusing to deliver to {host} ({resolved} resolves private)")
    return url


async def deliver(message: str, *, data: dict[str, Any] | None = None) -> dict:
    """Send one message. Never raises; returns what happened.

    ``data`` enriches the generic webhook body with structured event
    fields; when BUCKER_NOTIFY_WEBHOOK_SECRET is set, the generic webhook
    POST is signed (X-Bucker-Signature) so the receiver can authenticate
    it — a public webhook URL that anyone can POST to is a prompt-
    injection doorway into whatever automation listens there.
    """
    if not is_configured():
        return {"delivered": False, "reason": "not configured"}

    payload = _payload_for(message, data)
    headers = {"Content-Type": "application/json"}
    try:
        from urllib.parse import urlparse

        from bucker.config import settings

        if payload["kind"] == "webhook" and settings.notify_webhook_secret:
            body_bytes = json.dumps(payload["body"]).encode()
            headers[SIGNATURE_HEADER] = sign_payload(
                settings.notify_webhook_secret, body_bytes
            )

        _validate_target(payload["url"])  # SSRF guard — raise before connecting
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
            header_lines = "".join(
                f"{name}: {value}\r\n" for name, value in headers.items()
            )
            req = (
                f"POST {path} HTTP/1.1\r\nHost: {parsed.hostname}\r\n"
                f"{header_lines}"
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


async def deliver_event(kind: str, result: dict[str, Any]) -> dict:
    """Deliver a task/graph completion: prose for humans, fields for machines."""
    message = build_graph_message(result) if kind == "graph" \
        else build_task_message(result)
    return await deliver(message, data=build_event_data(kind, result))
