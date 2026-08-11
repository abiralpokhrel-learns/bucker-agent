"""python_test_runner — the first real verifier (step 21).

[HAND] — objective by construction: it runs the project's own tests inside the
sandbox and reads the exit code. No model is consulted, and the worker's
`summary` field is never examined. If the tests fail, the task failed, however
confident the worker was.

Diagnostics matter as much as the verdict. A bare "failed" gives the planner
nothing to work with on retry; the specific failing test names and the tail of
the output are what turn a retry into a targeted correction rather than a
re-roll.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from bucker.contracts.models import Task, WorkerResult
from bucker.sandbox.runtime import DockerSandbox
from bucker.verifiers.base import VerificationResult, register

#: pytest's terse failure lines, e.g. "FAILED tests/test_x.py::test_y - AssertionError"
_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)

#: The summary line, e.g. "2 failed, 5 passed in 0.31s"
_COUNTS = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped)")

MAX_DIAGNOSTIC_CHARS = 4000


def parse_pytest_output(output: str) -> dict:
    """Extract structured diagnostics from pytest output.

    Pure and tested — parsing is where this kind of code usually rots, and a
    silently-wrong parser would report green while tests were red.
    """
    failing = _FAILED_LINE.findall(output)
    counts = {kind.rstrip("s"): int(n) for n, kind in _COUNTS.findall(output)}
    return {
        "failing_tests": failing,
        "passed": counts.get("passed", 0),
        "failed": counts.get("failed", 0) + counts.get("error", 0),
        "skipped": counts.get("skipped", 0),
        "collected_nothing": "no tests ran" in output.lower(),
    }


@dataclass(slots=True)
class PythonTestRunner:
    """Runs pytest (and optionally ruff) against the post-diff workspace."""

    name: str = "python_test_runner"
    task_types: tuple[str, ...] = ("code_change",)
    test_command: str = "python -m pytest -q --no-header"
    lint_command: str = "python -m ruff check ."
    run_lint: bool = False
    timeout_s: int = 300

    async def verify(
        self,
        task: Task,
        result: WorkerResult,
        sandbox: DockerSandbox,
    ) -> VerificationResult:
        started = time.perf_counter()

        # A blocked worker produced nothing to verify. That is a legitimate
        # outcome, not a pass — there is no work here that survived a check.
        if result.status == "blocked":
            return VerificationResult(
                passed=False,
                verifier=self.name,
                diagnostics=f"worker blocked: {result.blocked_reason}",
                details={"blocked": True},
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        if result.status == "no_change_needed":
            # Still run the tests. "Nothing to do" is itself a claim, and the
            # cheapest way to check it is to see whether the suite is green.
            pass

        # The planner contract decides whether tests are part of "done".
        # A trivial task (create a file, tweak a constant) with
        # tests_required=false must NOT be failed for having no test suite
        # — the deliverable is the listed files, and that is what we check.
        if not task.constraints.tests_required:
            return await self._verify_files_exist(task, sandbox, started)

        test_run = await sandbox.exec(self.test_command, timeout_s=self.timeout_s)
        output = (test_run.stdout + "\n" + test_run.stderr).strip()
        parsed = parse_pytest_output(output)

        details: dict = {
            "exit_code": test_run.exit_code,
            "timed_out": test_run.timed_out,
            **parsed,
        }

        if test_run.timed_out:
            return VerificationResult(
                passed=False,
                verifier=self.name,
                diagnostics=f"test run timed out after {self.timeout_s}s",
                details=details,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        passed = test_run.exit_code == 0

        # A suite that collected nothing exits 5 in pytest, but treat any
        # "no tests ran" as a failure regardless: green-because-empty is the
        # most dangerous false pass there is.
        if parsed["collected_nothing"]:
            passed = False
            details["reason"] = "no tests collected"

        if passed and self.run_lint:
            lint_run = await sandbox.exec(self.lint_command, timeout_s=120)
            details["lint_exit_code"] = lint_run.exit_code
            if lint_run.exit_code != 0:
                passed = False
                output += "\n\n--- lint ---\n" + lint_run.stdout + lint_run.stderr

        return VerificationResult(
            passed=passed,
            verifier=self.name,
            diagnostics=_diagnostics(passed, parsed, output),
            details=details,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _verify_files_exist(
        self,
        task: Task,
        sandbox: DockerSandbox,
        started: float,
    ) -> VerificationResult:
        """Verify a no-tests contract: the listed files exist and are non-empty.

        When ``tests_required`` is false the deliverable IS the files — there
        is no suite to run, and pytest's "no tests collected" (exit 5) is not
        a meaningful failure for such a task. This checks the contract's
        ``files`` list against the sandbox instead. An empty file list means
        the planner deliberately left the worker unrestricted; we then accept
        the worker's result summary (nothing concrete was promised to check).
        """
        missing = []
        empty = []
        for path in task.files:
            try:
                content = sandbox.read_file(path)
            except Exception:  # noqa: BLE001 — file absent (any read error)
                missing.append(path)
                continue
            if not content.strip():
                empty.append(path)

        problems = [f"missing: {p}" for p in missing] + [
            f"empty: {p}" for p in empty
        ]
        if problems:
            return VerificationResult(
                passed=False,
                verifier=self.name,
                diagnostics="deliverable files missing: " + "; ".join(problems),
                details={
                    "tests_required": False,
                    "missing": missing,
                    "empty": empty,
                    "reason": "no tests required; files checked instead",
                },
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        return VerificationResult(
            passed=True,
            verifier=self.name,
            diagnostics=(
                f"{len(task.files)} file(s) present (tests not required)"
                if task.files
                else "worker summary accepted (no files listed, tests not required)"
            ),
            details={
                "tests_required": False,
                "files_checked": list(task.files),
            },
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def _diagnostics(passed: bool, parsed: dict, output: str) -> str:
    if passed:
        return f"{parsed['passed']} passed, {parsed['skipped']} skipped"

    lines = []
    if parsed["collected_nothing"]:
        lines.append("no tests were collected — nothing was actually verified")
    if parsed["failing_tests"]:
        lines.append("failing: " + ", ".join(parsed["failing_tests"][:20]))
    lines.append(f"{parsed['failed']} failed, {parsed['passed']} passed")
    lines.append("--- output tail ---")
    lines.append(output[-MAX_DIAGNOSTIC_CHARS:])
    return "\n".join(lines)


@dataclass(slots=True)
class NoopVerifier:
    """Always passes. For demo tasks and the Phase 0 workflow only.

    Deliberately named so it can never be mistaken for a real check, and
    deliberately not usable for ``code_change``: the type system is the
    reminder that shipping code past a no-op gate defeats the whole design.
    """

    name: str = "noop"
    task_types: tuple[str, ...] = ("demo",)

    async def verify(self, task, result, sandbox) -> VerificationResult:
        return VerificationResult(
            passed=True,
            verifier=self.name,
            diagnostics="noop verifier — nothing was checked",
            details={"noop": True},
        )


def register_builtins() -> None:
    """Register the verifiers that ship with the platform.

    Idempotent: safe to call multiple times (subsequent calls are no-ops).
    """
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return
    register(PythonTestRunner())
    register(NoopVerifier())
    # The second domain (BUILD_PLAN step 35): citation-consistency for
    # "research" tasks. Imported here, not at module scope, so importing this
    # module never drags in the citation checker (and vice versa).
    from bucker.verifiers.citation_checker import CitationVerifier
    register(CitationVerifier())
    _BUILTINS_REGISTERED = True


_BUILTINS_REGISTERED = False
