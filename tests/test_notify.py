"""Delivery tests (gateway, iter 7): message building + opt-in behavior.

Pure tests: message construction never depends on config; deliver() is a
no-op when nothing is configured (verified with a monkeypatched config).
"""

from __future__ import annotations

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
