from __future__ import annotations

import time
from dataclasses import dataclass

from bucker.config import settings
from bucker.contracts.models import Task, WorkerResult
from bucker.sandbox.runtime import DockerSandbox
from bucker.verifiers.base import VerificationResult

MAX_DIAGNOSTIC_CHARS = 4000


@dataclass(slots=True)
class LintVerifier:
    name: str = "lint_checker"
    task_types: tuple[str, ...] = ("code_change",)
    strict_mode: bool = False
    timeout_s: int = 120

    async def verify(
        self, task: Task, result: WorkerResult, sandbox: DockerSandbox
    ) -> VerificationResult:
        started = time.perf_counter()

        if result.status == "blocked":
            return VerificationResult(
                passed=False,
                verifier=self.name,
                diagnostics=f"worker blocked: {result.blocked_reason}",
                details={"blocked": True},
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        # Determine command
        lint_cmd = task.constraints.lint_command or settings.shell_verify_command
        if not lint_cmd:
            lint_cmd = self._auto_detect_linter(sandbox)

        if not lint_cmd:
            return VerificationResult(
                passed=True,
                verifier=self.name,
                diagnostics="no linter configured or auto-detected",
                details={"skipped": True},
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        lint_run = await sandbox.exec(lint_cmd, timeout_s=self.timeout_s)
        passed = lint_run.exit_code == 0
        details = {
            "lint_cmd": lint_cmd,
            "exit_code": lint_run.exit_code,
            "timed_out": lint_run.timed_out,
        }
        diagnostics = ""

        if lint_run.timed_out:
            passed = False
            diagnostics = f"linter timed out after {self.timeout_s}s"

        if not passed or (
            self.strict_mode and (lint_run.stdout.strip() or lint_run.stderr.strip())
        ):
            passed = False if self.strict_mode else passed
            output = (lint_run.stdout + "\n" + lint_run.stderr).strip()
            diagnostics = (
                f"Lint issues found (exit code {lint_run.exit_code}):\n"
                f"{output[-MAX_DIAGNOSTIC_CHARS:]}"
            )
        else:
            diagnostics = "Lint checks passed"

        return VerificationResult(
            passed=passed,
            verifier=self.name,
            diagnostics=diagnostics,
            details=details,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def _auto_detect_linter(self, sandbox: DockerSandbox) -> str | None:
        def has_file(name: str) -> bool:
            try:
                sandbox.read_file(name)
                return True
            except Exception:
                return False

        if has_file("pyproject.toml") or has_file("ruff.toml"):
            return "ruff check ."
        if has_file("Cargo.toml"):
            return "cargo clippy"
        if has_file("go.mod"):
            return "go vet ./..."
        if any(has_file(f) for f in [".eslintrc", ".eslintrc.json", "eslint.config.js"]):
            return "npx eslint ."
        return None
