"""Hardening-review tests: enforced behavior, not documentation."""

from __future__ import annotations

import pytest


# --------------------------------------------------- boot guard (review) --
def test_production_mode_refuses_dev_token(monkeypatch):
    """BUCKER_PRODUCTION=1 + dev-token must refuse to boot (enforced)."""
    import bucker.config as config_mod
    from bucker.security.bootstrap import assert_safe_boot

    orig_prod = config_mod.settings.production
    orig_token = config_mod.settings.api_token
    try:
        object.__setattr__(config_mod.settings, "production", True)
        object.__setattr__(config_mod.settings, "api_token", "dev-token")
        with pytest.raises(SystemExit) as exc:
            assert_safe_boot(component="test")
        assert exc.value.code == 2
    finally:
        object.__setattr__(config_mod.settings, "production", orig_prod)
        object.__setattr__(config_mod.settings, "api_token", orig_token)


def test_production_mode_with_real_token_boots(monkeypatch):
    import bucker.config as config_mod
    from bucker.security.bootstrap import assert_safe_boot

    orig_prod = config_mod.settings.production
    orig_token = config_mod.settings.api_token
    try:
        object.__setattr__(config_mod.settings, "production", True)
        object.__setattr__(config_mod.settings, "api_token", "real-token-123")
        assert_safe_boot(component="test")  # must not raise
    finally:
        object.__setattr__(config_mod.settings, "production", orig_prod)
        object.__setattr__(config_mod.settings, "api_token", orig_token)


# ------------------------------------------- cost unknown fail-closed -----
def test_pre_spend_halts_when_cost_unknown_and_budget_set():
    from bucker.core.budget import pre_spend_decision

    decision = pre_spend_decision(
        0.0, 0.5, elapsed_minutes=0.1, deadline_minutes=None, attempt=1,
        cost_unknown=True,
    )
    assert decision is not None
    assert decision["action"] == "halt"
    assert "cost unknown" in decision["reason"]


def test_pre_spend_ignores_unknown_cost_without_budget():
    from bucker.core.budget import pre_spend_decision

    # No budget -> nothing to protect -> unknown cost alone does not halt.
    assert pre_spend_decision(
        0.0, None, elapsed_minutes=0.1, deadline_minutes=None, attempt=1,
        cost_unknown=True,
    ) is None


# ------------------------------------------------------ redaction (review) --
def test_prompt_redaction_removes_credentials():
    from bucker.router.client import _redact_messages

    messages = [
        {"role": "user", "content": "use sk-ant-abcdefghijklmnopqrstuvwxyz123 "
                                    "and postgres://u:pw@db:5432/x to fix it"},
        {"role": "assistant", "content": "fine"},
    ]
    out = _redact_messages(messages)
    assert "sk-ant-" not in out[0]["content"]
    assert "pw@" not in out[0]["content"]
    assert "[REDACTED:" in out[0]["content"]
    assert out[1]["content"] == "fine"  # untouched content stays intact


def test_raw_response_redaction():
    from bucker.router.client import _redact_raw

    gkey = "AIzaSy" + "A" * 33  # exactly 39 chars: the real Google key shape
    raw = {"choices": [{"message": {
        "content": f"key {gkey} used",
        "reasoning_content": "token ghp_123456789012345678901234567890123456"},
    }]}
    out = _redact_raw(raw)
    assert gkey not in out["choices"][0]["message"]["content"]
    assert "[REDACTED:" in out["choices"][0]["message"]["content"]
    assert "ghp_" not in out["choices"][0]["message"]["reasoning_content"]


# ------------------------------------------------------------- reconciler --
def test_reconcile_dry_run_reports_without_starting(monkeypatch):
    """Dry run must not start workflows (no network, no side effects)."""
    from bucker.core import tasks as tasks_mod
    from bucker.core.tasks import reconcile_pending

    started = []

    class _Row:
        def __getitem__(self, key):
            return {
                "id": "11111111-2222-3333-4444-555555555555",
                "task_type": "code",
                "objective": "fix it",
                "status": "schedule_failed",
                "budget_usd": 0.5,
            }[key]

        @property
        def id(self):  # noqa: A003
            return "11111111-2222-3333-4444-555555555555"

    class _Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def fetch(self, sql, *args):
            return [_Row()]

    class _Pool:
        def acquire(self):
            return _Conn()

    class _Store:
        _pool = _Pool()

        async def append(self, *a, **k):
            pass

    async def _no_start(*a, **k):
        started.append(a)

    import asyncio

    monkeypatch.setattr(tasks_mod, "start_task_workflow", _no_start)
    report = asyncio.run(reconcile_pending(_Pool(), _Store(), dry_run=True))
    assert report["found"] == 1
    assert report["skipped_dry_run"]
    assert started == []  # nothing started in dry run
