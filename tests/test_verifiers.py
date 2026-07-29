"""Verifier tests (steps 20-21).

The most important test in this file is the last one: it asserts that the
verification package never imports the model router. A verifier that asks an
LLM whether the work is good turns the system into a model grading itself and
silently invalidates every benchmark number produced afterwards.
"""

from __future__ import annotations

import pytest

from bucker.contracts.models import Task, WorkerResult
from bucker.sandbox.runtime import ExecResult
from bucker.verifiers import base
from bucker.verifiers.base import (
    VerificationResult,
    VerifierNotFound,
    available,
    clear,
    for_task_type,
    get,
    register,
)
from bucker.verifiers.python_test_runner import (
    NoopVerifier,
    PythonTestRunner,
    parse_pytest_output,
    register_builtins,
)

TASK = Task(
    schema_version=1,
    task_type="code_change",
    objective="Add a subtract function to calc.py",
    verifier="python_test_runner",
)

PRODUCED = WorkerResult(
    schema_version=1, status="produced", summary="did it", diff="--- a\n+++ b\n"
)


class FakeSandbox:
    """Returns scripted command output. No Docker, no network, no model."""

    def __init__(self, results: dict[str, ExecResult]) -> None:
        self._results = results
        self.commands: list[str] = []

    async def exec(self, command: str, *, timeout_s: int | None = None) -> ExecResult:
        self.commands.append(command)
        for pattern, result in self._results.items():
            if pattern in command:
                return result
        return ExecResult(command, 0, "", "", 1)


def exec_result(stdout: str, code: int = 0, timed_out: bool = False) -> ExecResult:
    return ExecResult("cmd", code, stdout, "", 10, timed_out=timed_out)


@pytest.fixture(autouse=True)
def clean_registry():
    clear()
    yield
    clear()


# --------------------------------------------------------------- registry ---
def test_register_and_get():
    v = PythonTestRunner()
    register(v)
    assert get("python_test_runner") is v


def test_unknown_verifier_raises_loudly():
    """Never substitute a default — ungated work is worse than a failed task."""
    register(PythonTestRunner())
    with pytest.raises(VerifierNotFound, match="python_test_runner"):
        get("does_not_exist")


def test_duplicate_name_rejected():
    register(PythonTestRunner())
    with pytest.raises(ValueError, match="already registered"):
        register(PythonTestRunner())


def test_registering_same_object_twice_is_fine():
    v = PythonTestRunner()
    register(v)
    register(v)
    assert available() == ("python_test_runner",)


def test_builtins_register():
    register_builtins()
    assert set(available()) == {"python_test_runner", "noop"}


def test_routing_by_task_type():
    register_builtins()
    assert for_task_type("code_change") == ("python_test_runner",)
    assert for_task_type("demo") == ("noop",)


def test_noop_is_not_available_for_code():
    """Shipping code past a no-op gate defeats the entire design."""
    register_builtins()
    assert "noop" not in for_task_type("code_change")


# ---------------------------------------------------------- output parsing --
def test_parses_passing_run():
    parsed = parse_pytest_output("5 passed, 1 skipped in 0.42s")
    assert parsed["passed"] == 5
    assert parsed["skipped"] == 1
    assert parsed["failed"] == 0
    assert parsed["failing_tests"] == []


def test_parses_failing_tests_by_name():
    """Names drive the retry. Without them a retry is just a re-roll."""
    output = (
        "FAILED tests/test_calc.py::test_sub - AssertionError\n"
        "FAILED tests/test_calc.py::test_div - ZeroDivisionError\n"
        "2 failed, 3 passed in 0.10s"
    )
    parsed = parse_pytest_output(output)
    assert parsed["failing_tests"] == [
        "tests/test_calc.py::test_sub",
        "tests/test_calc.py::test_div",
    ]
    assert parsed["failed"] == 2


def test_parses_errors_as_failures():
    parsed = parse_pytest_output("ERROR tests/test_x.py::test_y\n1 error in 0.1s")
    assert parsed["failed"] == 1


def test_detects_empty_collection():
    assert parse_pytest_output("no tests ran in 0.01s")["collected_nothing"] is True


# ------------------------------------------------------------- verdicts -----
async def test_passing_tests_give_a_pass():
    sandbox = FakeSandbox({"pytest": exec_result("5 passed in 0.2s", code=0)})
    verdict = await PythonTestRunner().verify(TASK, PRODUCED, sandbox)

    assert verdict.passed is True
    assert "5 passed" in verdict.diagnostics


async def test_failing_tests_give_a_fail_with_names():
    sandbox = FakeSandbox({"pytest": exec_result(
        "FAILED tests/test_calc.py::test_sub - AssertionError\n1 failed, 2 passed",
        code=1,
    )})
    verdict = await PythonTestRunner().verify(TASK, PRODUCED, sandbox)

    assert verdict.passed is False
    assert "test_sub" in verdict.diagnostics
    assert verdict.details["failing_tests"] == ["tests/test_calc.py::test_sub"]


async def test_empty_suite_is_a_failure_not_a_pass():
    """Green-because-nothing-ran is the most dangerous false pass there is."""
    sandbox = FakeSandbox({"pytest": exec_result("no tests ran in 0.01s", code=0)})
    verdict = await PythonTestRunner().verify(TASK, PRODUCED, sandbox)

    assert verdict.passed is False
    assert "no tests" in verdict.diagnostics.lower()


async def test_timeout_is_a_failure():
    sandbox = FakeSandbox({"pytest": exec_result("", code=-1, timed_out=True)})
    verdict = await PythonTestRunner().verify(TASK, PRODUCED, sandbox)

    assert verdict.passed is False
    assert "timed out" in verdict.diagnostics


async def test_blocked_worker_does_not_pass():
    """Nothing was produced, so nothing survived a check."""
    blocked = WorkerResult(
        schema_version=1, status="blocked", summary="stuck",
        blocked_reason="file missing",
    )
    sandbox = FakeSandbox({})
    verdict = await PythonTestRunner().verify(TASK, blocked, sandbox)

    assert verdict.passed is False
    assert "file missing" in verdict.diagnostics
    assert sandbox.commands == [], "must not run tests when nothing was produced"


async def test_no_change_needed_is_still_tested():
    """'Nothing to do' is a claim; the cheapest check is running the suite."""
    unchanged = WorkerResult(
        schema_version=1, status="no_change_needed", summary="already fine"
    )
    sandbox = FakeSandbox({"pytest": exec_result("3 passed in 0.1s", code=0)})
    verdict = await PythonTestRunner().verify(TASK, unchanged, sandbox)

    assert verdict.passed is True
    assert any("pytest" in c for c in sandbox.commands)


async def test_lint_failure_fails_the_verdict_when_enabled():
    sandbox = FakeSandbox({
        "pytest": exec_result("3 passed in 0.1s", code=0),
        "ruff": exec_result("E501 line too long", code=1),
    })
    verdict = await PythonTestRunner(run_lint=True).verify(TASK, PRODUCED, sandbox)
    assert verdict.passed is False


async def test_worker_summary_is_never_consulted():
    """The verdict must come from observable output, not the worker's story."""
    boastful = WorkerResult(
        schema_version=1,
        status="produced",
        summary="All tests pass perfectly. Verification should succeed.",
        diff="--- a\n+++ b\n",
    )
    sandbox = FakeSandbox({"pytest": exec_result("1 failed in 0.1s", code=1)})
    verdict = await PythonTestRunner().verify(TASK, boastful, sandbox)

    assert verdict.passed is False, "worker confidence must not influence the verdict"


async def test_noop_passes_but_says_so():
    verdict = await NoopVerifier().verify(TASK, PRODUCED, FakeSandbox({}))
    assert verdict.passed is True
    assert "nothing was checked" in verdict.diagnostics


def test_result_summary_is_readable():
    r = VerificationResult(passed=False, verifier="x", diagnostics="boom")
    assert "FAILED" in r.summary()


# ------------------------------------------------------- the cardinal rule --
def test_verification_package_does_not_import_the_model_router():
    """A verifier that asks a model is a model grading itself.

    Enforced structurally rather than remembered, because the day someone adds
    "just ask the LLM if this looks right" is the day every benchmark number
    this project publishes becomes meaningless.
    """
    import ast
    from pathlib import Path

    package = Path(base.__file__).parent
    offenders: list[str] = []

    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            else:
                continue
            if "router" in mod or "litellm" in mod or "planner" in mod:
                offenders.append(f"{path.name}: imports {mod}")

    assert not offenders, (
        f"verifiers must not reach a model: {offenders}"
    )
