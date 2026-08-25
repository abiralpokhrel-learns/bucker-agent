"""command — run an arbitrary shell command as the verification gate.

The second real verifier, and the one that opens bucker beyond Python
projects: `make test`, `npm test`, `go test ./...`, `cargo test`, a
custom script — whatever the project's own definition of "done" is. Same
contract as python_test_runner: exit code decides, output becomes
diagnostics, and no model is ever consulted.

Where the command comes from (first match wins):

1. The task contract: ``constraints.command`` in the planner's Task
   (constraints is open-ended by schema, so this needs no contract change).
2. The platform default: ``BUCKER_SHELL_VERIFY_COMMAND``.

If NEITHER is set the task FAILS with a loud diagnostic rather than
passing — a verifier that ran nothing has verified nothing, and a silent
pass here would be the exact "green-because-empty" false pass
python_test_runner refuses to produce.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from bucker.config import settings
from bucker.contracts.models import Task, WorkerResult
from bucker.sandbox.runtime import DockerSandbox
from bucker.verifiers.base import VerificationResult, register

MAX_DIAGNOSTIC_CHARS = 4000


@dataclass(slots=True)
class CommandVerifier:
    """Runs one shell command inside the task's sandbox; exit 0 passes."""

    name: str = "command"
    task_types: tuple[str, ...] = ("code_change",)
    timeout_s: int = 300
    #: Overrides settings.shell_verify_command when non-empty (used by
    #: tests and embedders; normal deployments configure via env).
    default_command: str = ""

    def resolve_command(self, task: Task) -> str:
        """The command for this task: contract override > configured default."""
        raw = getattr(task.constraints, "command", None)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        configured = self.default_command or settings.shell_verify_command
        return configured.strip()

    async def verify(
        self,
        task: Task,
        result: WorkerResult,
        sandbox: DockerSandbox,
    ) -> VerificationResult:
        started = time.perf_counter()

        # A blocked worker produced nothing to verify. Same posture as
        # python_test_runner: a legitimate outcome, never a pass.
        if result.status == "blocked":
            return VerificationResult(
                passed=False,
                verifier=self.name,
                diagnostics=f"worker blocked: {result.blocked_reason}",
                details={"blocked": True},
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        command = self.resolve_command(task)
        if not command:
            # Fail CLOSED. There is no defensible reading of "no command
            # was configured" that means the work succeeded.
            return VerificationResult(
                passed=False,
                verifier=self.name,
                diagnostics=(
                    "no verify command: set constraints.command on the "
                    "task contract or BUCKER_SHELL_VERIFY_COMMAND — the "
                    "command verifier refuses to pass without running "
                    "anything"
                ),
                details={"reason": "no_command_configured"},
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        run = await sandbox.exec(command, timeout_s=self.timeout_s)
        output = (run.stdout + "\n" + run.stderr).strip()
        details: dict = {
            "command": command,
            "exit_code": run.exit_code,
            "timed_out": run.timed_out,
            "duration_ms": run.duration_ms,
        }

        if run.timed_out:
            diagnostics = (
                f"verify command timed out after {self.timeout_s}s: {command}"
                f"\n--- output tail ---\n{output[-MAX_DIAGNOSTIC_CHARS:]}"
            )
            return VerificationResult(
                passed=False,
                verifier=self.name,
                diagnostics=diagnostics,
                details=details,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        passed = run.exit_code == 0
        if passed:
            diagnostics = f"exit 0: {command} ({len(output)} chars of output)"
        else:
            diagnostics = (
                f"exit {run.exit_code}: {command}"
                f"\n--- output tail ---\n{output[-MAX_DIAGNOSTIC_CHARS:]}"
            )
        return VerificationResult(
            passed=passed,
            verifier=self.name,
            diagnostics=diagnostics,
            details=details,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def register_command_verifier() -> None:
    """Idempotent registration hook, called from register_builtins()."""
    from bucker.verifiers.base import _REGISTRY

    if "command" not in _REGISTRY:
        register(CommandVerifier())
