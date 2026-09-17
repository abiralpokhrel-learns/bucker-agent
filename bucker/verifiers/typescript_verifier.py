from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass

from bucker.contracts.models import Task, WorkerResult
from bucker.sandbox.runtime import DockerSandbox
from bucker.verifiers.base import VerificationResult

MAX_DIAGNOSTIC_CHARS = 4000


@dataclass(slots=True)
class TypeScriptVerifier:
    name: str = "typescript_test_runner"
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

        # Type checking
        tsc_run = await sandbox.exec("npx tsc --noEmit", timeout_s=self.timeout_s)
        diagnostics = ""
        passed = tsc_run.exit_code == 0
        details = {"tsc_exit_code": tsc_run.exit_code}

        if not passed:
            diagnostics += f"TypeScript errors:\n{tsc_run.stdout}\n{tsc_run.stderr}\n\n"

        # Testing
        pkg_json = ""
        with contextlib.suppress(Exception):
            pkg_json = sandbox.read_file("package.json")

        if "vitest" in pkg_json:
            test_cmd = "npx vitest run --reporter=json"
        elif "jest" in pkg_json:
            test_cmd = "npx jest --json"
        else:
            test_cmd = "npm test"

        test_run = await sandbox.exec(test_cmd, timeout_s=self.timeout_s)
        details["test_exit_code"] = test_run.exit_code
        details["timed_out"] = test_run.timed_out

        if test_run.timed_out:
            return VerificationResult(
                passed=False,
                verifier=self.name,
                diagnostics=f"test run timed out after {self.timeout_s}s",
                details=details,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        if "json" in test_cmd:
            try:
                # Find JSON part of output
                output = test_run.stdout
                start_idx = output.find("{")
                end_idx = output.rfind("}")
                if start_idx != -1 and end_idx != -1:
                    json_str = output[start_idx : end_idx + 1]
                    parsed = json.loads(json_str)

                    passed_count = parsed.get("numPassedTests", 0)
                    failed_count = parsed.get("numFailedTests", 0)

                    details["passed"] = passed_count
                    details["failed"] = failed_count

                    if failed_count > 0:
                        passed = False
                        failed_names = []
                        if "testResults" in parsed:
                            for tr in parsed["testResults"]:
                                if "assertionResults" in tr:
                                    for ar in tr["assertionResults"]:
                                        if ar.get("status") == "failed":
                                            failed_names.append(
                                                ar.get("fullName", ar.get("title", "Unknown"))
                                            )
                        diagnostics += f"{failed_count} failed, {passed_count} passed\n"
                        diagnostics += "Failing tests: " + ", ".join(failed_names[:20]) + "\n"
                else:
                    if test_run.exit_code != 0:
                        passed = False
                        diagnostics += "Tests failed (could not parse JSON output)\n"
            except Exception:
                if test_run.exit_code != 0:
                    passed = False
                    diagnostics += "Tests failed (could not parse JSON output)\n"
        else:
            if test_run.exit_code != 0:
                passed = False
                diagnostics += "Tests failed\n"

        if not passed:
            output_tail = (test_run.stdout + "\n" + test_run.stderr).strip()[
                -MAX_DIAGNOSTIC_CHARS:
            ]
            diagnostics += f"\n--- test output tail ---\n{output_tail}"

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
