"""Phase 1 workflow wiring tests.

Gap this closes: `code_task_workflow.py` ties every component together and had
no coverage at all. Worse, its failure modes are the kind Temporal only reveals
at runtime — an activity the workflow calls but the worker never registered
fails when a real task runs, not at import, so a green test suite would have
told you nothing.

None of this needs a Temporal server.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from bucker import worker as worker_module
from bucker.retry import Action, AttemptState, decide
from bucker.workflows.code_task_workflow import CodeTaskInput, CodeTaskWorkflow


# ------------------------------------------------------------ registration --
def test_workflow_is_registered_with_temporal():
    assert hasattr(CodeTaskWorkflow, "__temporal_workflow_definition")
    assert CodeTaskWorkflow.__temporal_workflow_definition.name == "CodeTaskWorkflow"


def test_workflow_input_has_sane_defaults():
    inp = CodeTaskInput(task_id="t", objective="do the thing")
    assert inp.max_retries == 2
    assert inp.budget_usd is None


def _activities_called_by_workflow() -> set[str]:
    """Names passed to execute_activity in the workflow source."""
    source = Path(
        inspect.getfile(CodeTaskWorkflow)
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name != "execute_activity" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name):
            called.add(first.id)
    return called


def _activities_registered_in_worker() -> set[str]:
    """Names inside the ``activities=[...]`` list in bucker/worker.py."""
    source = inspect.getsource(worker_module)
    tree = ast.parse(source)

    registered: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "activities":
            continue
        if isinstance(node.value, ast.List):
            for element in node.value.elts:
                if isinstance(element, ast.Name):
                    registered.add(element.id)
    return registered


def test_every_activity_the_workflow_calls_is_registered_in_the_worker():
    """The failure this catches only shows up when a real task runs.

    Temporal resolves activities by name at execution time, so a workflow that
    calls an unregistered activity imports fine, passes every unit test, and
    then hangs or fails on the first live task. Checking the wiring statically
    turns a runtime mystery into a build failure.
    """
    called = _activities_called_by_workflow()
    assert called, "parsed no activities — the AST walk is broken, not the wiring"

    # Worker() is constructed inside main(), so the registered list is not
    # importable as data; read it from the module source instead.
    registered = _activities_registered_in_worker()

    missing = called - registered
    assert not missing, (
        f"workflow calls activities the worker never registers: {sorted(missing)}. "
        f"This would fail only at runtime, on a real task."
    )


def test_workflow_module_has_no_nondeterministic_imports():
    """Temporal replays workflow code; anything impure at module scope breaks it."""
    source = Path(inspect.getfile(CodeTaskWorkflow)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    banned = ("random", "requests", "asyncpg", "httpx", "socket", "subprocess")
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        offenders += [n for n in names if n.split(".")[0] in banned]

    assert not offenders, f"non-deterministic imports in workflow module: {offenders}"


# --------------------------------------------------- loop bound vs. policy --
@pytest.mark.parametrize("max_retries", range(0, 6))
def test_retry_loop_always_terminates_within_its_bound(max_retries: int):
    """The workflow's loop bound and the retry policy must agree.

    The workflow iterates `range(1, max_retries + 2)`. If the policy could
    still say RETRY on the final iteration, the loop would fall through to the
    'unreachable' branch and a task would silently land in human review for the
    wrong reason. This proves the two definitions stay in step.
    """
    terminal_at = None

    for attempt in range(1, max_retries + 2):
        decision = decide(AttemptState(
            attempt=attempt,
            max_retries=max_retries,
            verification_passed=False,
            diagnostics="always failing",
        ))
        if decision.is_terminal:
            terminal_at = attempt
            break

    assert terminal_at is not None, (
        f"with max_retries={max_retries} the policy never reached a terminal "
        f"decision inside the workflow's loop bound — the fallthrough branch "
        f"would fire"
    )
    assert terminal_at == max_retries + 1


@pytest.mark.parametrize("max_retries", range(0, 6))
def test_a_failing_task_always_ends_in_human_review(max_retries: int):
    """Never silently discarded, never forced through as if it passed."""
    final = None
    for attempt in range(1, max_retries + 2):
        final = decide(AttemptState(
            attempt=attempt, max_retries=max_retries,
            verification_passed=False, diagnostics="boom",
        ))
        if final.is_terminal:
            break

    assert final.action is Action.ESCALATE


def test_passing_task_completes_on_first_attempt():
    d = decide(AttemptState(attempt=1, max_retries=2, verification_passed=True))
    assert d.action is Action.COMPLETE
