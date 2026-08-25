"""Command-verifier tests (bucker.verifiers.command_runner).

The verifier's contract is narrow on purpose: exit code decides, output
becomes diagnostics, and an unconfigured command fails CLOSED. All tests
run against a fake sandbox — no Docker, no subprocesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from bucker.contracts.models import Task, WorkerResult
from bucker.sandbox.runtime import ExecResult
from bucker.verifiers.base import VerificationResult
from bucker.verifiers.command_runner import CommandVerifier


@dataclass
class FakeSandbox:
    """Records exec calls; replays canned ExecResults in order."""

    results: list[ExecResult] = field(default_factory=list)
    commands_seen: list[str] = field(default_factory=list)

    async def exec(self, command: str, *, timeout_s: int | None = None):
        self.commands_seen.append(command)
        return self.results.pop(0)


def make_task(**constraint_overrides) -> Task:
    constraints = {"tests_required": True, **constraint_overrides}
    return Task(
        task_type="code_change",
        objective="make the build command pass",
        verifier="command",
        constraints=constraints,
    )


def make_result(status: str = "produced") -> WorkerResult:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    return WorkerResult(status=status, summary=f"did it at {now}")


def ok_run(command: str = "make test") -> ExecResult:
    return ExecResult(command=command, exit_code=0,
                      stdout="all good", stderr="", duration_ms=12)


def fail_run(command: str = "make test") -> ExecResult:
    return ExecResult(command=command, exit_code=2,
                      stdout="", stderr="error: nope", duration_ms=8)


# ------------------------------------------------------------- pass/fail --


async def test_exit_zero_passes():
    sandbox = FakeSandbox(results=[ok_run()])
    verdict = await CommandVerifier(default_command="make test").verify(
        make_task(), make_result(), sandbox
    )
    assert verdict.passed is True
    assert verdict.details["exit_code"] == 0
    assert sandbox.commands_seen == ["make test"]


async def test_nonzero_exit_fails_with_output_tail():
    sandbox = FakeSandbox(results=[fail_run("npm test")])
    verdict = await CommandVerifier(default_command="npm test").verify(
        make_task(), make_result(), sandbox
    )
    assert verdict.passed is False
    assert "exit 2" in verdict.diagnostics
    assert "nope" in verdict.diagnostics
    assert sandbox.commands_seen == ["npm test"]


async def test_timeout_fails_not_passes():
    timed_out = ExecResult(command="slow-check", exit_code=-1,
                           stdout="partial", stderr="", duration_ms=300_000,
                           timed_out=True)
    sandbox = FakeSandbox(results=[timed_out])
    verdict = await CommandVerifier(default_command="slow-check").verify(
        make_task(), make_result(), sandbox
    )
    assert verdict.passed is False
    assert "timed out" in verdict.diagnostics
    assert verdict.details["timed_out"] is True


async def test_blocked_worker_fails_without_running_anything():
    sandbox = FakeSandbox()
    result = make_result()
    object.__setattr__(result, "status", "blocked")
    object.__setattr__(result, "blocked_reason", "prompt refused")
    verdict = await CommandVerifier(default_command="make test").verify(
        make_task(), result, sandbox
    )
    assert verdict.passed is False
    assert "blocked" in verdict.diagnostics
    assert sandbox.commands_seen == []


# -------------------------------------------------- command resolution ----


async def test_constraints_command_wins_over_default():
    sandbox = FakeSandbox(results=[ok_run("cargo test")])
    verifier = CommandVerifier(default_command="make test")
    task = make_task(**{"command": "cargo test"})
    await verifier.verify(task, make_result(), sandbox)
    assert sandbox.commands_seen == ["cargo test"]


async def test_settings_default_used_when_no_constraint(monkeypatch):
    import bucker.config as cfg

    object.__setattr__(cfg.settings, "shell_verify_command", "go test ./...")
    try:
        sandbox = FakeSandbox(results=[ok_run("go test ./...")])
        await CommandVerifier().verify(make_task(), make_result(), sandbox)
        assert sandbox.commands_seen == ["go test ./..."]
    finally:
        object.__setattr__(cfg.settings, "shell_verify_command", "")


async def test_no_command_configured_fails_closed():
    """The most important rule: a verifier that would run NOTHING must not
    pass. Silent green-because-unconfigured is the false pass this file
    exists to prevent."""
    import bucker.config as cfg

    original = cfg.settings.shell_verify_command
    object.__setattr__(cfg.settings, "shell_verify_command", "")
    try:
        sandbox = FakeSandbox(results=[ok_run()])  # must never be consumed
        verdict = await CommandVerifier().verify(make_task(), make_result(), sandbox)
        assert verdict.passed is False
        assert verdict.details.get("reason") == "no_command_configured"
        assert sandbox.commands_seen == []
    finally:
        object.__setattr__(cfg.settings, "shell_verify_command", original)


async def test_blank_constraint_command_falls_through_to_default():
    sandbox = FakeSandbox(results=[ok_run("default-cmd")])
    task = make_task(**{"command": "   "})
    await CommandVerifier(default_command="default-cmd").verify(
        task, make_result(), sandbox
    )
    assert sandbox.commands_seen == ["default-cmd"]


# ------------------------------------------------------------ registry ----


def test_command_verifier_is_registered_builtin():
    from bucker.verifiers import register_builtins

    register_builtins()
    from bucker.verifiers.base import available, get

    assert "command" in available()
    verifier = get("command")
    assert isinstance(verifier, CommandVerifier)


def test_registration_is_idempotent():
    from bucker.verifiers.command_runner import register_command_verifier
    from bucker.verifiers.python_test_runner import register_builtins

    register_builtins()
    before = VerificationResult  # any reference; just force imports settled
    register_command_verifier()
    register_command_verifier()
    del before
