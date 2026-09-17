from __future__ import annotations

import json
import time
from dataclasses import dataclass

from bucker.contracts.models import Task, WorkerResult
from bucker.sandbox.runtime import DockerSandbox
from bucker.verifiers.base import VerificationResult

MAX_DIAGNOSTIC_CHARS = 4000


@dataclass(slots=True)
class GoVerifier:
    name: str = "go_test_runner"
    task_types: tuple[str, ...] = ("code_change",)
    timeout_s: int = 300

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

        if not task.constraints.tests_required:
            return await self._verify_files_exist(task, sandbox, started)

        test_run = await sandbox.exec("go test ./... -json", timeout_s=self.timeout_s)
        details = {"test_exit_code": test_run.exit_code, "timed_out": test_run.timed_out}
        passed = test_run.exit_code == 0
        diagnostics = ""

        if test_run.timed_out:
            return VerificationResult(
                passed=False,
                verifier=self.name,
                diagnostics=f"test run timed out after {self.timeout_s}s",
                details=details,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        failing_tests = []
        for line in test_run.stdout.splitlines():
            try:
                msg = json.loads(line)
                if msg.get("Action") == "fail" and msg.get("Test"):
                    failing_tests.append(msg.get("Test"))
            except Exception:
                pass

        if not passed:
            diagnostics += f"Tests failed: {len(failing_tests)} failing\n"
            if failing_tests:
                diagnostics += "Failing tests: " + ", ".join(set(failing_tests[:20])) + "\n"
            output_tail = (test_run.stdout + "\n" + test_run.stderr).strip()[
                -MAX_DIAGNOSTIC_CHARS:
            ]
            diagnostics += f"\n--- test output tail ---\n{output_tail}\n\n"

        vet_run = await sandbox.exec("go vet ./...", timeout_s=self.timeout_s)
        if vet_run.exit_code != 0:
            vet_warnings = vet_run.stderr.strip()
            if vet_warnings:
                diagnostics += (
                    "go vet warnings:\n" + vet_warnings[-MAX_DIAGNOSTIC_CHARS:] + "\n"
                )

        if passed and not diagnostics:
            diagnostics = "All checks passed"

        return VerificationResult(
            passed=passed,
            verifier=self.name,
            diagnostics=diagnostics,
            details=details,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _verify_files_exist(
        self, task: Task, sandbox: DockerSandbox, started: float
    ) -> VerificationResult:
        missing, empty = [], []
        for path in task.files:
            try:
                content = sandbox.read_file(path)
                if not content.strip():
                    empty.append(path)
            except Exception:
                missing.append(path)

        problems = [f"missing: {p}" for p in missing] + [f"empty: {p}" for p in empty]
        if problems:
            return VerificationResult(
                passed=False,
                verifier=self.name,
                diagnostics="deliverable files missing: " + "; ".join(problems),
                details={"tests_required": False, "missing": missing, "empty": empty},
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        return VerificationResult(
            passed=True,
            verifier=self.name,
            diagnostics="files present (tests not required)",
            details={"tests_required": False},
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
