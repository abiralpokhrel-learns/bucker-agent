"""Baseline agent tests (step 25).

Covers the prompt, the parsing, the control flow, and the integration with
a real sandbox. Uses a fake router so no network or database is needed for
unit tests — same pattern as test_worker.py.

Tests split:
  * pure — prompt, parsing, serialization. Always run.
  * integration — apply diffs, run tests in sandbox. Skip without Docker.
"""

from __future__ import annotations

import json

import pytest

from bucker.bench.baseline import (
    BaselineError,
    BaselineResult,
    _parse_model_output,
    build_prompt,
    build_workspace_view,
    run_baseline,
)
from bucker.router.client import ModelResponse
from bucker.sandbox.runtime import DockerSandbox, docker_available

# A valid diff that the test suite's calc.py/subtract problem expects.
VALID_DIFF = """\
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,5 @@
 def add(a, b):
     return a + b
+
+def subtract(a, b):
+    return a - b
"""

# A broken diff — wrong function name.
BROKEN_DIFF = """\
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,5 @@
 def add(a, b):
     return a + b
+
+def sub(a, b):
+    return a - b
"""


class FakeRouter:
    """Returns canned responses in order. Same shape as test_worker's."""

    def __init__(self, responses: list[dict], cost: float = 0.01) -> None:
        self._responses = list(responses)
        self._cost = cost
        self.model = "fake-model"
        self.mode = "recorded"

    async def complete(self, messages, *, purpose, **kwargs) -> ModelResponse:
        return ModelResponse(
            text=json.dumps(self._responses.pop(0)),
            model=self.model,
            cost_usd=self._cost,
            latency_ms=5,
            raw_ref="sha256:" + "0" * 64,
            request_ref="sha256:" + "1" * 64,
            from_recording=True,
        )


def router_with(*responses: dict, cost: float = 0.01) -> FakeRouter:
    return FakeRouter(list(responses), cost=cost)


# --------------------------------------------------------------- fixtures --


@pytest.fixture
def sandbox(tmp_path):
    """A workspace with calc.py — NOT started (no Docker needed for pure tests)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sb = DockerSandbox(ws)
    sb.write_file(
        "calc.py",
        "def add(a, b):\n    return a + b\n",
    )
    sb.write_file(
        "test_calc.py",
        "from calc import add, subtract\n\n\n"
        "def test_add():\n    assert add(2, 3) == 5\n\n\n"
        "def test_subtract():\n    assert subtract(5, 3) == 2\n",
    )
    return sb


@pytest.fixture
async def started_sandbox(tmp_path):
    """A running sandbox container with a git repo, so apply_diff works."""
    if not await docker_available():
        pytest.skip("docker not available")

    ws = tmp_path / "ws"
    ws.mkdir()
    sb = DockerSandbox(ws)
    sb.write_file(
        "calc.py",
        "def add(a, b):\n    return a + b\n",
    )
    sb.write_file(
        "test_calc.py",
        "from calc import add, subtract\n\n\n"
        "def test_add():\n    assert add(2, 3) == 5\n\n\n"
        "def test_subtract():\n    assert subtract(5, 3) == 2\n",
    )
    await sb.start()
    # git init so git apply works inside the container.
    await sb.exec("git init && git add . && git commit -m base --no-gpg-sign")
    try:
        yield sb
    finally:
        await sb.stop()


# ---------------------------------------------------------------- prompt --


def test_baseline_prompt_includes_objective():
    prompt = build_prompt("Add a subtract function", "(no files)")
    assert "Add a subtract function" in prompt


def test_baseline_prompt_includes_workspace(sandbox):
    view = build_workspace_view(sandbox, ["calc.py"])
    prompt = build_prompt("objective", view)
    assert "calc.py" in prompt
    assert "def add" in prompt


def test_baseline_prompt_includes_previous_attempt():
    prompt = build_prompt("obj", "ws", previous="Tests FAILED: 2 failed")
    assert "Tests FAILED" in prompt


def test_baseline_prompt_first_attempt_has_no_previous():
    prompt = build_prompt("obj", "ws")
    assert "first attempt" in prompt.lower()


def test_workspace_view_discovers_files_when_none_listed(sandbox):
    view = build_workspace_view(sandbox, [])
    assert "calc.py" in view
    assert "test_calc.py" in view


def test_workspace_view_reports_missing_file(sandbox):
    view = build_workspace_view(sandbox, ["nonexistent.py"])
    assert "does not exist" in view


# ---------------------------------------------------------------- parsing --


def test_parse_valid_done_false():
    parsed = _parse_model_output(
        json.dumps({"done": False, "diff": "--- a/x\n+++ b/x\n", "summary": "x"})
    )
    assert parsed["done"] is False
    assert "diff" in parsed


def test_parse_valid_done_true():
    parsed = _parse_model_output(
        json.dumps({"done": True, "summary": "done", "reason_done": "tests pass"})
    )
    assert parsed["done"] is True


def test_parse_strips_markdown_fences():
    parsed = _parse_model_output(
        '```json\n{"done": false, "diff": "x", "summary": "hi"}\n```'
    )
    assert parsed["done"] is False


def test_parse_strips_preamble():
    parsed = _parse_model_output(
        'Here is my result:\n\n{"done": true, "summary": "ok", "reason_done": "done"}'
    )
    assert parsed["done"] is True


def test_parse_rejects_non_json():
    with pytest.raises(ValueError, match="no JSON object"):
        _parse_model_output("just some text, no braces anywhere")


def test_parse_rejects_array():
    with pytest.raises(ValueError, match="expected a JSON object"):
        _parse_model_output("[1, 2, 3]")


# ----------------------------------------------- integration (needs Docker) --


async def test_completes_on_first_try(started_sandbox):
    """Model produces correct code, tests pass, baseline early-exits."""
    router = router_with({
        "done": False,
        "diff": VALID_DIFF,
        "summary": "Added subtract function",
    })

    result = await run_baseline(
        router,
        "Add a subtract function to calc.py",
        started_sandbox,
        files=["calc.py", "test_calc.py"],
    )

    assert result.status == "completed"
    assert result.passed
    assert result.total_iterations == 1  # early exit on first green test


async def test_declares_done_immediately_when_no_work_needed(started_sandbox):
    """Model says done=true on first call — e.g. objective already satisfied."""
    router = router_with({
        "done": True,
        "summary": "Already correct",
        "reason_done": "tests already pass",
    })

    result = await run_baseline(
        router, "objective", started_sandbox, files=["calc.py"]
    )

    assert result.total_iterations == 1


async def test_iterates_on_failure(started_sandbox):
    """First diff is wrong, tests fail, second attempt fixes it — early exit."""
    router = router_with(
        {
            "done": False,
            "diff": BROKEN_DIFF,
            "summary": "Added sub function (wrong name)",
        },
        {
            "done": False,
            "diff": VALID_DIFF,
            "summary": "Fixed: renamed to subtract",
        },
    )

    result = await run_baseline(
        router,
        "Add a subtract function to calc.py",
        started_sandbox,
        files=["calc.py", "test_calc.py"],
    )

    assert result.status == "completed"
    assert result.passed
    assert result.total_iterations == 2, (
        f"expected 2 iterations (broken, fix), got {result.total_iterations}"
    )


async def test_max_iterations_is_enforced(started_sandbox):
    """Model keeps producing wrong diffs — loop stops, doesn't spin forever."""
    responses = []
    for _ in range(5):
        responses.append({
            "done": False,
            "diff": BROKEN_DIFF,
            "summary": "still wrong",
        })

    router = router_with(*responses)

    result = await run_baseline(
        router,
        "Add a subtract function",
        started_sandbox,
        files=["calc.py", "test_calc.py"],
        max_iterations=5,
    )

    assert result.status == "max_iterations"
    assert result.total_iterations == 5
    assert not result.passed


async def test_records_cost_across_iterations(started_sandbox):
    """Each iteration pays for a model call. Early exit reduces cost."""
    router = router_with(
        {
            "done": False,
            "diff": BROKEN_DIFF,
            "summary": "attempt 1",
        },
        {
            "done": False,
            "diff": VALID_DIFF,
            "summary": "attempt 2",
        },
        cost=0.05,
    )

    result = await run_baseline(
        router,
        "Add subtract",
        started_sandbox,
        files=["calc.py", "test_calc.py"],
    )

    assert result.total_cost_usd == pytest.approx(0.10)  # 2 calls, early exit
    assert result.total_iterations == 2


async def test_model_declares_done_but_tests_fail(started_sandbox):
    """Model claims success, but the verifier would catch this — baseline
    runs the final test and reports honestly."""
    router = router_with({
        "done": False,
        "diff": BROKEN_DIFF,
        "summary": "work",
    }, {
        "done": True,
        "summary": "I'm sure this is right",
        "reason_done": "looks correct to me",
    })

    result = await run_baseline(
        router,
        "Add subtract function",
        started_sandbox,
        files=["calc.py", "test_calc.py"],
    )

    assert result.status == "failed"
    assert len(result.iterations) == 2


async def test_invalid_json_is_fed_back_for_repair(started_sandbox):
    """Model returns garbage — feed it back, don't crash."""
    router = router_with(
        "not json at all {{{",
        {
            "done": False,
            "diff": VALID_DIFF,
            "summary": "fixed",
        },
        {
            "done": True,
            "summary": "done",
            "reason_done": "green",
        },
    )

    result = await run_baseline(
        router,
        "Add subtract",
        started_sandbox,
        files=["calc.py", "test_calc.py"],
    )

    assert result.status == "completed"
    assert result.passed


async def test_no_diff_and_not_done_is_fed_back(started_sandbox):
    """Model says 'not done' but doesn't provide a diff."""
    router = router_with(
        {
            "done": False,
            "summary": "thinking...",
        },
        {
            "done": False,
            "diff": VALID_DIFF,
            "summary": "ok here",
        },
        {
            "done": True,
            "summary": "done",
            "reason_done": "green",
        },
    )

    result = await run_baseline(
        router,
        "Add subtract",
        started_sandbox,
        files=["calc.py", "test_calc.py"],
    )

    assert result.status == "completed"
    assert result.passed


async def test_sandbox_must_be_started(tmp_path):
    """Calling run_baseline without starting the sandbox is an error."""
    sb = DockerSandbox(tmp_path / "ws")
    router = router_with({"done": True, "summary": "x", "reason_done": "x"})

    with pytest.raises(BaselineError, match="must be started"):
        await run_baseline(router, "obj", sb, files=["calc.py"])


def test_baseline_result_serializable():
    """Result can be serialized for the experiment log."""
    result = BaselineResult(
        status="completed",
        final_diff="--- a/x\n+++ b/x\n",
        final_summary="done",
    )

    data = {
        "status": result.status,
        "cost": result.total_cost_usd,
        "iterations": result.total_iterations,
        "passed": result.passed,
        "final_summary": result.final_summary,
    }
    json.dumps(data)  # must not raise
