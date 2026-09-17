from __future__ import annotations

import json
import time
from dataclasses import dataclass

from bucker.contracts.models import Task, WorkerResult
from bucker.sandbox.runtime import DockerSandbox
from bucker.verifiers.base import VerificationResult

MAX_DIAGNOSTIC_CHARS = 4000

@dataclass(slots=True)
class RustVerifier:
    name: str = "rust_test_runner"
    task_types: tuple[str, ...] = ("code_change",)
    timeout_s: int = 600

    async def verify(self, task: Task, result: WorkerResult, sandbox: DockerSandbox) -> VerificationResult:
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

        # Run tests
        test_run = await sandbox.exec("cargo test --message-format=json", timeout_s=self.timeout_s)
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
        clippy_warnings = []

        for line in test_run.stdout.splitlines():
            try:
                msg = json.loads(line)
                if msg.get("type") == "test" and msg.get("event") == "failed":
                    failing_tests.append(msg.get("name", "Unknown test"))
            except Exception:
                pass

        if not passed:
            diagnostics += f"Tests failed: {len(failing_tests)} failing\n"
            if failing_tests:
                diagnostics += "Failing tests: " + ", ".join(failing_tests[:20]) + "\n"
            output_tail = (test_run.stdout + "\n" + test_run.stderr).strip()[-MAX_DIAGNOSTIC_CHARS:]
            diagnostics += f"\n--- test output tail ---\n{output_tail}\n\n"

        # Run clippy for warnings (does not fail the verify unless it exits non-zero if we want to be strict, but requirements say "warnings as diagnostics, not failures")
        clippy_run = await sandbox.exec("cargo clippy --message-format=json", timeout_s=self.timeout_s)
        for line in clippy_run.stdout.splitlines():
            try:
                msg = json.loads(line)
                if msg.get("reason") == "compiler-message":
                    message = msg.get("message", {})
                    if message.get("level") == "warning":
                        clippy_warnings.append(message.get("message", "warning"))
            except Exception:
                pass

        if clippy_warnings:
            diagnostics += f"Clippy warnings ({len(clippy_warnings)}):\n"
            for w in clippy_warnings[:10]:
                diagnostics += f" - {w}\n"

        if passed and not diagnostics:
            diagnostics = "All checks passed"

        return VerificationResult(
            passed=passed,
            verifier=self.name,
            diagnostics=diagnostics,
            details=details,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _verify_files_exist(self, task: Task, sandbox: DockerSandbox, started: float) -> VerificationResult:
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
