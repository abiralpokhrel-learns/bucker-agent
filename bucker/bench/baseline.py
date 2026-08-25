"""Baseline single-agent loop (BUILD_PLAN step 25).

[HAND] — this is the comparator the whole architecture is judged against.
Make it honest. A weak strawman baseline invalidates every benchmark number
published after it, and a strawman found out after publication is a
credibility-destroying event, not a fixable bug.

The baseline does:
  1. Read the workspace files.
  2. Ask the model for a diff.
  3. Apply the diff in the sandbox.
  4. Run the tests.
  5. Feed the results back to the model.
  6. Repeat until the model declares done or max iterations is hit.

It does NOT have:
  - A planner that produces a typed contract
  - A verifier that separately checks the result
  - A retry policy with failure-context escalation
  - Schema-enforced structured output (the diff is applied and tested, period)

What it DOES share with the bucker architecture:
  - Same model (via the same ModelRouter)
  - Same sandbox (DockerSandbox, same isolation)
  - Same tools (file read, diff apply, command exec)

The fair comparison is: same model, same tools, same isolation — does the
planner+worker+verifier structure beat a simple loop, or is it just adding
cost and latency for no benefit? That is the M2 question, and the baseline
must be strong enough that a positive answer actually means something.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from bucker.config import settings
from bucker.router.client import ModelResponse, ModelRouter
from bucker.sandbox.runtime import DockerSandbox

PROMPTS = Path(__file__).parent / "prompts"

#: How many fix-it iterations before the baseline gives up. Kept low
#: deliberately — a single-agent loop that needs more than this many tries
#: is failing, and an unbounded loop just burns budget.
MAX_ITERATIONS = 5

#: Truncation limit for any text pasted into the prompt.
MAX_EXCERPT_CHARS = 4000


class BaselineError(Exception):
    """The baseline loop itself failed — not the model, but the plumbing."""


# -------------------------------------------------------------- data types --


@dataclass(slots=True)
class BaselineIteration:
    """One turn of the loop: model proposes, tests decide."""

    iteration: int
    raw_text: str
    response: ModelResponse
    diff: str | None = None
    summary: str = ""
    test_exit_code: int | None = None
    test_output: str = ""
    test_passed: bool = False
    declared_done: bool = False

    @property
    def cost_usd(self) -> float:
        return self.response.cost_usd


@dataclass(slots=True)
class BaselineResult:
    """What the baseline agent produced after its loop."""

    status: str  # "completed" | "failed" | "max_iterations" | "error"
    final_diff: str | None = None
    final_summary: str = ""
    iterations: list[BaselineIteration] = field(default_factory=list)
    error: str | None = None

    @property
    def total_cost_usd(self) -> float:
        return round(sum(it.cost_usd for it in self.iterations), 6)

    @property
    def total_iterations(self) -> int:
        return len(self.iterations)

    @property
    def passed(self) -> bool:
        return self.status == "completed"


# --------------------------------------------------------------- prompting --


def _truncate(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


def build_workspace_view(sandbox: DockerSandbox, files: list[str]) -> str:
    """Render workspace files the baseline agent is allowed to see."""
    if not files:
        # List everything visible in the workspace so the test is fair: the
        # structured pipeline lists files in the contract, the baseline
        # discovers them. Both get the same information, different mechanism.
        # Real SWE-bench workspaces are full repo trees, so walk recursively
        # (bounded depth + skip junk dirs) and prefer source files; a
        # top-level-only listing sees nothing but directories and the model
        # is left guessing — a strawman, not a baseline.
        try:
            skip_dirs = {
                ".git", "__pycache__", "node_modules", ".venv", "venv",
                ".tox", ".mypy_cache", ".pytest_cache", "build", "dist",
            }
            src_exts = {".py", ".js", ".ts", ".rst", ".md", ".cfg", ".toml"}
            discovered: list[str] = []
            for p in sorted(sandbox.workspace.rglob("*")):
                if len(p.relative_to(sandbox.workspace).parts) > 4:
                    continue
                if any(part in skip_dirs for part in p.parts):
                    continue
                rel = p.relative_to(sandbox.workspace).as_posix()
                if p.is_file() and p.suffix in src_exts:
                    discovered.append(rel)
            files = discovered[:40]
        except OSError:
            discovered = []
            files = []

    if not files:
        return "(no files in workspace)"

    parts: list[str] = []
    for path in files:
        try:
            content = sandbox.read_file(path)
        except (FileNotFoundError, OSError):
            parts.append(f"### {path}\n(file does not exist)")
            continue
        except Exception as exc:
            parts.append(f"### {path}\n(unreadable: {type(exc).__name__})")
            continue
        parts.append(f"### {path}\n```\n{_truncate(content)}\n```")

    return "\n\n".join(parts)


def build_prompt(
    objective: str,
    workspace_view: str,
    previous: str = "",
) -> str:
    """Render the baseline prompt. Previous-attempt context is empty on
    the first call; filled with test output on subsequent iterations."""
    template_text = (PROMPTS / "baseline_v1.md").read_text(encoding="utf-8")
    template = Template(template_text)
    return template.safe_substitute(
        objective=objective,
        workspace=workspace_view,
        previous=previous or "(this is the first attempt — no previous results)",
    )


# -------------------------------------------------------------- validation --


def _parse_model_output(raw_text: str) -> dict:
    """Extract JSON from a model response.

    Models wrap JSON in fences or add preamble even when told not to.
    Stripping that is not lenience — the content is still validated.

    Delegates to ``bucker.planner.extract_json`` so the baseline shares the
    same parsing (and the same quote-repair fallback) as the worker; a diff
    parser that behaves differently between the two systems would skew the
    benchmark.
    """
    from bucker.planner import extract_json

    return extract_json(raw_text)


# ----------------------------------------------------------------- agent ----


async def run_baseline(
    router: ModelRouter,
    objective: str,
    sandbox: DockerSandbox,
    *,
    files: list[str] | None = None,
    test_command: str = "python -m pytest -q --no-header 2>&1",
    max_iterations: int = MAX_ITERATIONS,
) -> BaselineResult:
    """Run the baseline single-agent loop.

    This is the comparator: same model, same sandbox, same tools. No planner
    that produces a contract; no verifier that separately gates the output.
    Just the model, the workspace, and the tests.

    Returns a BaselineResult that can be passed to a verifier (the same
    python_test_runner the structured pipeline uses) for a fair comparison:
    both paths produce a diff, and the same verifier decides truth.
    """
    if not sandbox._started:
        raise BaselineError("sandbox must be started before calling run_baseline")

    files_list = list(files) if files else []
    workspace_view = build_workspace_view(sandbox, files_list)
    messages = [
        {
            "role": "user",
            "content": build_prompt(objective, workspace_view, previous=""),
        }
    ]

    iterations: list[BaselineIteration] = []
    last_diff: str | None = None

    for iteration in range(1, max_iterations + 1):
        cost_before = sum(it.cost_usd for it in iterations)
        if cost_before >= settings.default_budget_usd:
            return BaselineResult(
                status="error",
                iterations=iterations,
                error="budget exceeded before iteration could start",
            )

        response = await router.complete(messages, purpose="worker")
        raw_text = response.text

        try:
            parsed = _parse_model_output(raw_text)
        except ValueError as exc:
            # Model produced something unparseable. Record it as a failed
            # iteration and try again with the error fed back.
            iterations.append(
                BaselineIteration(
                    iteration=iteration,
                    raw_text=raw_text,
                    response=response,
                    summary=f"parse error: {exc}",
                )
            )
            messages = [
                {
                    "role": "user",
                    "content": (
                        f"Your previous response was not valid JSON: {exc}\n\n"
                        f"Response:\n{_truncate(raw_text, 2000)}\n\n"
                        f"Return the JSON object and nothing else."
                    ),
                }
            ]
            continue

        declared_done = bool(parsed.get("done", False))
        diff = parsed.get("diff") or None
        summary = parsed.get("summary", "")

        if declared_done:
            # Model says it's done. Apply the last known-good diff (if any)
            # and run tests one final time to decide truth.
            test_exit_code = None
            test_output = ""
            test_passed = False

            if last_diff is not None:
                # Re-apply the accumulated diff for a clean test run.
                apply_result = await sandbox.apply_diff(last_diff)
                test_result = await sandbox.exec(test_command)
                test_output = (test_result.stdout + "\n" + test_result.stderr).strip()
                test_exit_code = test_result.exit_code
                test_passed = test_exit_code == 0

            iterations.append(
                BaselineIteration(
                    iteration=iteration,
                    raw_text=raw_text,
                    response=response,
                    diff=last_diff,
                    summary=summary,
                    test_exit_code=test_exit_code,
                    test_output=test_output,
                    test_passed=test_passed,
                    declared_done=True,
                )
            )

            return BaselineResult(
                status="completed" if test_passed else "failed",
                final_diff=last_diff,
                final_summary=summary,
                iterations=iterations,
            )

        if diff is None:
            # Model didn't declare done but also produced no diff.
            iterations.append(
                BaselineIteration(
                    iteration=iteration,
                    raw_text=raw_text,
                    response=response,
                    summary=summary or "no diff produced",
                )
            )
            messages = [
                {
                    "role": "user",
                    "content": (
                        "You set done=false but did not include a diff. "
                        "Produce a unified diff that changes the code to make "
                        "the tests pass, or set done=true with a reason."
                    ),
                }
            ]
            continue

        # --- apply + test ---------------------------------------------------
        last_diff = diff
        apply_result = await sandbox.apply_diff(diff)

        if apply_result.exit_code != 0:
            # Diff failed to apply. This is not a test failure — the model
            # produced a malformed diff. Feed the apply error back.
            test_output = (
                f"DIFF FAILED TO APPLY (exit {apply_result.exit_code}):\n"
                f"{apply_result.stdout}\n{apply_result.stderr}"
            )
            test_passed = False
            test_exit_code = apply_result.exit_code
        else:
            test_result = await sandbox.exec(test_command)
            test_output = (test_result.stdout + "\n" + test_result.stderr).strip()
            test_exit_code = test_result.exit_code
            test_passed = test_exit_code == 0

        iterations.append(
            BaselineIteration(
                iteration=iteration,
                raw_text=raw_text,
                response=response,
                diff=diff,
                summary=summary,
                test_exit_code=test_exit_code,
                test_output=test_output,
                test_passed=test_passed,
                declared_done=declared_done,
            )
        )

        if test_passed:
            # Early exit: tests are green before max_iterations.
            return BaselineResult(
                status="completed",
                final_diff=diff,
                final_summary=summary,
                iterations=iterations,
            )

        # Feed result back for the next iteration.
        messages = [
            {
                "role": "user",
                "content": (
                    f"Test results after your change:\n\n"
                    f"```\n{_truncate(test_output, 3000)}\n```\n\n"
                    f"Tests {'PASSED' if test_passed else 'FAILED'}"
                    f" (exit {test_exit_code}).\n\n"
                    f"Produce a corrected diff that fixes the specific failures"
                    f" above. If you believe the tests are wrong or the task"
                    f" cannot be done, set done=true and explain why in"
                    f" reason_done."
                ),
            }
        ]

    # Ran out of iterations.
    return BaselineResult(
        status="max_iterations",
        final_diff=last_diff,
        final_summary=(
            f"exhausted {max_iterations} iterations without passing tests"
        ),
        iterations=iterations,
    )
