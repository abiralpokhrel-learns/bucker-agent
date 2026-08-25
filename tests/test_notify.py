"""Delivery tests (gateway, iter 7): message building + opt-in behavior.

Pure tests: message construction never depends on config; deliver() is a
no-op when nothing is configured (verified with a monkeypatched config).
"""

from __future__ import annotations

import pytest

from bucker.core.notify import (
    build_graph_message,
    build_task_message,
    is_configured,
)


def test_task_message_passed():
    msg = build_task_message({"status": "completed", "attempts": 2,
                              "verdict": {"passed": True, "verifier": "noop"}})
    assert "✅" in msg and "noop" in msg and "2" in msg


def test_task_message_failed():
    msg = build_task_message({"status": "completed", "attempts": 3,
                              "verdict": {"passed": False,
                                          "verifier": "python_test_runner"}})
    assert "❌" in msg and "python_test_runner" in msg


def test_task_message_human_review():
    msg = build_task_message({"status": "needs_human_review", "attempts": 2})
    assert "approve" in msg


def test_task_message_halted():
    msg = build_task_message({"status": "halted", "attempts": 1,
                              "reason": "budget exceeded"})
    assert "⏹" in msg and "budget" in msg


def test_graph_message_summary():
    ok = build_graph_message({"steps": {"a": {}, "b": {}}, "failed": []})
    assert "✅" in ok and "2 step(s)" in ok
    bad = build_graph_message({"steps": {"a": {}, "b": {}}, "failed": ["a"]})
    assert "❌" in bad and "1 failed" in bad


def test_notify_is_opt_in(monkeypatch):
    from bucker.config import settings

    original = (settings.notify_webhook_url,
                settings.telegram_bot_token, settings.telegram_chat_id)
    try:
        object.__setattr__(settings, "notify_webhook_url", "")
        object.__setattr__(settings, "telegram_bot_token", "")
        object.__setattr__(settings, "telegram_chat_id", "")
        assert is_configured() is False

        object.__setattr__(settings, "notify_webhook_url", "https://example.com/hook")
        assert is_configured() is True

        object.__setattr__(settings, "notify_webhook_url", "")
        object.__setattr__(settings, "telegram_bot_token", "123:ABC")
        assert is_configured() is False  # chat id missing

        object.__setattr__(settings, "telegram_chat_id", "42")
        assert is_configured() is True  # both present
    finally:
        object.__setattr__(settings, "notify_webhook_url", original[0])
        object.__setattr__(settings, "telegram_bot_token", original[1])
        object.__setattr__(settings, "telegram_chat_id", original[2])


async def test_deliver_is_noop_when_unconfigured(monkeypatch):
    from bucker.config import settings
    from bucker.core.notify import deliver

    original = (settings.notify_webhook_url,
                settings.telegram_bot_token, settings.telegram_chat_id)
    try:
        object.__setattr__(settings, "notify_webhook_url", "")
        object.__setattr__(settings, "telegram_bot_token", "")
        object.__setattr__(settings, "telegram_chat_id", "")
        result = await deliver("hello")
        assert result == {"delivered": False, "reason": "not configured"}
    finally:
        object.__setattr__(settings, "notify_webhook_url", original[0])
        object.__setattr__(settings, "telegram_bot_token", original[1])
        object.__setattr__(settings, "telegram_chat_id", original[2])


async def test_notify_activity_noop_when_unconfigured(monkeypatch):
    from bucker.activities.notify import notify_task_result

    result = await notify_task_result("task", {"status": "completed",
                                               "attempts": 1})
    assert result["delivered"] is False
    assert result["reason"] == "not configured"


def test_payload_for_webhook_and_telegram(monkeypatch):
    """The request target must be origin-form for ANY host (regression:
    generic webhooks used to build an absolute-URI request line)."""
    from bucker.config import settings
    from bucker.core.notify import _payload_for

    original = (settings.notify_webhook_url,
                settings.telegram_bot_token, settings.telegram_chat_id)
    try:
        object.__setattr__(settings, "notify_webhook_url", "https://hooks.example.com/x")
        object.__setattr__(settings, "telegram_bot_token", "")
        object.__setattr__(settings, "telegram_chat_id", "")
        p = _payload_for("hi")
        assert p["kind"] == "webhook"
        assert p["url"] == "https://hooks.example.com/x"

        object.__setattr__(settings, "notify_webhook_url", "")
        object.__setattr__(settings, "telegram_bot_token", "123:ABC")
        object.__setattr__(settings, "telegram_chat_id", "42")
        p = _payload_for("hi")
        assert p["kind"] == "telegram"
        assert p["url"].endswith("/sendMessage")
        assert p["body"] == {"chat_id": "42", "text": "hi"}
    finally:
        object.__setattr__(settings, "notify_webhook_url", original[0])
        object.__setattr__(settings, "telegram_bot_token", original[1])
        object.__setattr__(settings, "telegram_chat_id", original[2])


# --------------------------------------------- Slack / Discord channels --


def _set_channels(**values):
    """Patch the five delivery settings on the frozen Settings object.

    Returns a restore callable — call it in a finally block (the pattern
    the rest of this file uses; monkeypatch.setattr can't write to a
    frozen dataclass).
    """
    import bucker.config as cfg

    fields = ("notify_webhook_url", "telegram_bot_token", "telegram_chat_id",
              "slack_webhook_url", "discord_webhook_url")
    originals = {f: getattr(cfg.settings, f) for f in fields}
    for f in fields:
        object.__setattr__(cfg.settings, f, values.get(f, ""))

    def _restore():
        for f, v in originals.items():
            object.__setattr__(cfg.settings, f, v)
    return _restore


def test_channel_precedence_and_bodies():
    """Telegram > Slack > Discord > generic webhook, one channel per event."""
    from bucker.config import settings
    from bucker.core.notify import _payload_for

    restore = _set_channels(
        telegram_bot_token="123:ABC", telegram_chat_id="42",
        slack_webhook_url="https://hooks.slack.com/services/T00/B00/XXX",
        discord_webhook_url="https://discord.com/api/webhooks/1/abc",
        notify_webhook_url="https://example.com/hook",
    )
    try:
        p = _payload_for("hello")
        assert p["kind"] == "telegram"

        object.__setattr__(settings, "telegram_bot_token", "")
        object.__setattr__(settings, "telegram_chat_id", "")
        p = _payload_for("hello")
        assert p["kind"] == "slack"
        assert p["url"].startswith("https://hooks.slack.com/")
        assert p["body"] == {"text": "hello"}
    finally:
        restore()

    restore_discord = _set_channels(
        discord_webhook_url="https://discord.com/api/webhooks/1/abc",
    )
    try:
        p = _payload_for("hello")
        assert p["kind"] == "discord"
        assert p["body"] == {"content": "hello"}
    finally:
        restore_discord()

    restore_hook = _set_channels(notify_webhook_url="https://example.com/hook")
    try:
        p = _payload_for("hello")
        assert p["kind"] == "webhook"
    finally:
        restore_hook()


def test_discord_message_truncated_to_limit():
    """Discord rejects bodies over 2000 chars with a 400; truncate instead."""
    from bucker.core.notify import _DISCORD_MAX_CHARS, _payload_for

    restore = _set_channels(
        discord_webhook_url="https://discord.com/api/webhooks/1/abc",
    )
    try:
        body = _payload_for("x" * 5000)["body"]
        assert len(body["content"]) <= 2000
        assert len(body["content"]) == _DISCORD_MAX_CHARS
    finally:
        restore()


def test_configured_channels_order_and_empty():
    from bucker.core.notify import configured_channels

    restore_empty = _set_channels()
    try:
        assert configured_channels() == []
    finally:
        restore_empty()

    restore_two = _set_channels(
        slack_webhook_url="https://hooks.slack.com/s",
        notify_webhook_url="https://e.com/h",
    )
    try:
        assert configured_channels() == ["slack", "webhook"]
    finally:
        restore_two()


def test_is_configured_with_slack_only():
    from bucker.core.notify import is_configured

    restore = _set_channels(slack_webhook_url="https://hooks.slack.com/s")
    try:
        assert is_configured() is True
    finally:
        restore()


# --------------------------------------------- HMAC webhook signing ----


SECRET = "whsec_test_123"


def test_sign_and_verify_round_trip():
    from bucker.core.notify import sign_payload, verify_webhook_signature

    body = b'{"text": "task completed"}'
    header = sign_payload(SECRET, body, timestamp=1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    assert verify_webhook_signature(
        SECRET, header, body,
        now=1_700_000_000 + 60,  # within tolerance
    )


def test_verify_rejects_tampered_body():
    from bucker.core.notify import sign_payload, verify_webhook_signature

    header = sign_payload(SECRET, b'{"text": "legit"}', timestamp=100)
    assert not verify_webhook_signature(
        SECRET, header, b'{"text": "forged"}', now=100
    )


def test_verify_rejects_stale_timestamps():
    """Replay protection: a captured signature older than the tolerance
    window must fail even though the MAC itself is valid."""
    from bucker.core.notify import sign_payload, verify_webhook_signature

    body = b"x"
    header = sign_payload(SECRET, body, timestamp=1_000_000)
    assert not verify_webhook_signature(
        SECRET, header, body, tolerance_s=300, now=1_000_000 + 301,
    )
    assert verify_webhook_signature(
        SECRET, header, body, tolerance_s=300, now=1_000_000 + 299,
    )


def test_verify_rejects_malformed_headers():
    from bucker.core.notify import verify_webhook_signature

    for bad in ("", "v1=abc", "t=abc,v1=abc", "t=1"):
        assert not verify_webhook_signature(SECRET, bad, b"body", now=1)


def test_verify_accepts_any_rotated_key_match():
    """Multiple v1 entries model key rotation: one match is enough."""
    from bucker.core.notify import sign_payload, verify_webhook_signature

    body = b"payload"
    old_sig = sign_payload("old-secret", body, timestamp=50).split("v1=")[1]
    new_header = sign_payload("new-secret", body, timestamp=50) \
        .replace("v1=", f"v1={old_sig},v1=", 1)
    # Header now lists BOTH the old and new signatures.
    assert verify_webhook_signature("new-secret", new_header, body, now=50)


def test_verify_needs_secret_and_header():
    from bucker.core.notify import verify_webhook_signature

    assert not verify_webhook_signature("", "t=1,v1=abc", b"b")
    assert not verify_webhook_signature(SECRET, "", b"b")


def test_build_event_data_fields():
    """Structured webhook payload: machine-readable fields next to prose."""
    from bucker.core.notify import build_event_data

    data = build_event_data("task", {
        "status": "completed", "attempts": 2, "cost_usd": 0.03,
        "verdict": {"passed": True, "verifier": "python_test_runner"},
    })
    assert data["event"] == "task"
    assert data["status"] == "completed"
    assert data["verifier_passed"] is True
    assert data["cost_usd"] == pytest.approx(0.03)


def test_structured_data_flattens_into_generic_webhook_only():
    """Chat platforms get prose only; unknown keys would be rejected or
    misrendered there. The generic webhook gets text + structured fields."""
    from bucker.core.notify import _payload_for

    restore = _set_channels(notify_webhook_url="https://e.com/hook")
    try:
        body = _payload_for("done", {"event": "task",
                                     "cost_usd": 0.5})["body"]
        assert body["text"] == "done"
        assert body["event"] == "task"
    finally:
        restore()

    restore_slack = _set_channels(
        slack_webhook_url="https://hooks.slack.com/s")
    try:
        body = _payload_for("done", {"event": "task"})["body"]
        assert body == {"text": "done"}
    finally:
        restore_slack()


# -------------------------------------------------- SSRF guard (review) --


def test_webhook_target_rejects_private_and_bad_schemes():
    """The notification path must never be a vector into internal services."""
    from bucker.core.notify import _validate_target

    # scheme
    for bad in ("file:///etc/passwd", "gopher://127.0.0.1:70/x", "ftp://x"):
        with pytest.raises(ValueError, match="scheme"):
            _validate_target(bad)
    # literal private / loopback / link-local
    for bad in ("http://127.0.0.1:8080/x", "http://10.0.0.5/x",
                "http://192.168.1.1/x", "http://169.254.169.254/latest/meta-data/"):
        with pytest.raises(ValueError, match="private|loopback|link-local|reserved"):
            _validate_target(bad)
    # public hostnames pass (DNS-resolvable only)
    assert _validate_target("https://example.com/x") == "https://example.com/x"
