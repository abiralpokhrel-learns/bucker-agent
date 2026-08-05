"""Worker: executes one typed Task and produces a structured, unverified result.

[HAND] — the load-bearing idea of this whole project lives here, and it is a
negative: **nothing this file returns is trusted.** The worker's own report that
it succeeded is a claim, not evidence. It becomes state only after a Verifier
runs objective checks against it.

That is why ``status: "produced"`` is deliberately not called ``"success"``.
Naming matters when the whole architecture exists to resist a model's
confidence.

Two guardrails beyond the schema:

  * **Blocked is a first-class outcome.** A worker that cannot do the task
    should say so. An invented diff costs a full verification cycle and
    teaches the system nothing.
  * **Workspace content is data, never instructions.** File contents and
    command output go into the prompt inside a clearly-marked untrusted
    section (prompt injection mitigation, doc 19). The structural separation
    is in the prompt file; the discipline of never concatenating tool output
    into the instruction region is here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from bucker.config import settings
from bucker.contracts.models import (
    Task,
    ValidationFailure,
    WorkerResult,
    validate_result,
)
from bucker.planner import extract_json
from bucker.router.client import ModelResponse, ModelRouter
from bucker.sandbox.runtime import DockerSandbox, ExecResult

PROMPTS = Path(__file__).parent / "prompts"

#: Truncate any single file or command output pasted into the prompt. A runaway
#: log should not blow the context window or the budget.
MAX_EXCERPT_CHARS = 4000


class WorkFailed(Exception):
    """The worker produced nothing schema-valid within its attempts."""

    def __init__(self, attempts: list[WorkAttempt]) -> None:
        self.attempts = attempts
        detail = "; ".join(f"attempt {i + 1}: {a.errors}" for i, a in enumerate(attempts))
        super().__init__(f"worker produced no valid result ({detail})")


@dataclass(slots=True)
class WorkAttempt:
    raw_text: str
    response: ModelResponse
    errors: list[str] = field(default_factory=list)
    result: WorkerResult | None = None
    # Self-critique loop metadata (empty when critique is disabled or skipped).
    critique_verdict: str | None = None        # "ok" | "needs_fix" | None
    critique_issues: list[str] = field(default_factory=list)
    repaired: bool = False                     # True when a repair round ran
    extra_calls: list[ModelResponse] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result is not None

    @property
    def cost_usd(self) -> float:
        return round(
            self.response.cost_usd + sum(c.cost_usd for c in self.extra_calls), 6
        )


@dataclass(slots=True)
class WorkOutcome:
    result: WorkerResult
    attempts: list[WorkAttempt]
    applied: ExecResult | None = None

    @property
    def cost_usd(self) -> float:
        return round(sum(a.cost_usd for a in self.attempts), 6)


# ------------------------------------------------------------------ prompt --
def _truncate(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


def build_workspace_view(sandbox: DockerSandbox, files: list[str]) -> str:
    """Render the files the task may touch, as untrusted data."""
    if not files:
        return "(no files listed in the contract)"

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


def build_prompt(task: Task, workspace_view: str) -> str:
    template = Template((PROMPTS / "worker_v1.md").read_text(encoding="utf-8"))
    return template.safe_substitute(
        contract=json.dumps(task.model_dump(), indent=2),
        workspace=workspace_view,
        objective=task.objective,
        skills=_skills_section(task.objective),
        context_facts=_facts_section(task.objective),
    )


# ------------------------------------------------- memory injection (harness) --


def _skills_section(objective: str) -> str:
    """Procedural memory: matched skills become part of working memory.

    Empty when no skills match — the worker behaves exactly as before, so
    enabling the memory system changes nothing until a skill is added.
    """
    try:
        from bucker.memory.skills import SkillStore

        skills = SkillStore().for_objective(objective, limit=3)
    except Exception:
        return "(no skills loaded)"
    if not skills:
        return "(none)"
    blocks = []
    for s in skills:
        blocks.append(
            f"### {s.name}\n\n{s.description}\n\n{s.body}\n"
        )
    return "\n".join(blocks)


def _facts_section(objective: str) -> str:
    """Semantic memory: durable facts relevant to this objective."""
    try:
        from bucker.memory.facts import MemoryStore

        facts = MemoryStore().context_for(objective, limit=5)
    except Exception:
        return "(no facts loaded)"
    if not facts:
        return "(none)"
    return "\n".join(f"- {f['text']}" for f in facts)


# ------------------------------------------------------------- validation ---
def _validate(raw_text: str) -> tuple[WorkerResult | None, list[str]]:
    try:
        data = extract_json(raw_text)
    except ValueError as exc:
        return None, [str(exc)]

    try:
        return validate_result(data), []
    except ValidationFailure as exc:
        return None, exc.errors
    except Exception as exc:
        return None, [f"{type(exc).__name__}: {exc}"]


# ------------------------------------------------- self-critique loop ----


@dataclass(slots=True)
class Critique:
    """Parsed verdict of the critic pass (loop engineering, phase A)."""

    verdict: str            # "ok" | "needs_fix"
    issues: list[str]
    fix_hint: str
    response: ModelResponse

    @property
    def wants_repair(self) -> bool:
        return self.verdict == "needs_fix" and bool(self.issues)


def build_critic_prompt(task: Task, workspace_view: str, diff: str) -> str:
    template = Template((PROMPTS / "critic_v1.md").read_text(encoding="utf-8"))
    return template.safe_substitute(
        contract=json.dumps(task.model_dump(), indent=2),
        objective=task.objective,
        diff=diff,
        workspace=_truncate(workspace_view, 4000),
    )


def build_repair_prompt(
    task: Task, critique: Critique, previous: str
) -> str:
    template = Template(
        (PROMPTS / "worker_v1_repair.md").read_text(encoding="utf-8")
    )
    return template.safe_substitute(
        critique="\n".join(f"- {i}" for i in critique.issues)
        + (f"\n\nfix_hint: {critique.fix_hint}" if critique.fix_hint else ""),
        objective=task.objective,
        previous=_truncate(previous, 4000),
    )


def _parse_critique(raw_text: str) -> tuple[dict | None, list[str]]:
    """Parse the critic's JSON. Never throws."""
    try:
        data = extract_json(raw_text)
    except ValueError as exc:
        return None, [str(exc)]
    if not isinstance(data, dict):
        return None, ["critique response is not an object"]
    verdict = data.get("verdict")
    if verdict not in ("ok", "needs_fix"):
        return None, [f"critique verdict must be 'ok'|'needs_fix', got {verdict!r}"]
    issues = data.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)]
    return {
        "verdict": verdict,
        "issues": [str(i) for i in issues][:10],
        "fix_hint": str(data.get("fix_hint", ""))[:500],
    }, []


async def _critique(
    router: ModelRouter, task: Task, workspace_view: str, diff: str
) -> Critique | None:
    """Run the critic. Returns None when the critique cannot be parsed —
    a bad critique must never block the task, only skip the repair round."""
    prompt = build_critic_prompt(task, workspace_view, diff)
    response = await router.complete(
        [{"role": "user", "content": prompt}],
        purpose="critic",
    )
    parsed, _ = _parse_critique(response.text)
    if parsed is None:
        return None
    return Critique(
        verdict=parsed["verdict"],
        issues=parsed["issues"],
        fix_hint=parsed["fix_hint"],
        response=response,
    )


async def _repair(
    router: ModelRouter,
    task: Task,
    critique: Critique,
    previous_raw: str,
) -> tuple[WorkerResult | None, list[str], ModelResponse]:
    """One bounded repair round. Returns (result, errors, response).

    If the repair response is invalid JSON, fall back to the original —
    the critic is a first pass, never a blocker.
    """
    prompt = build_repair_prompt(task, critique, previous_raw)
    response = await router.complete(
        [{"role": "user", "content": prompt}],
        purpose="worker",
    )
    return (*_validate(response.text), response)


# ----------------------------------------------------------------- worker ---
async def execute_task(
    router: ModelRouter,
    task: Task,
    sandbox: DockerSandbox,
    *,
    max_attempts: int = 2,
    apply: bool = True,
) -> WorkOutcome:
    """Run one task. Returns an unverified result — the verifier decides truth.

    ``apply`` writes the diff into the sandbox workspace so a verifier can run
    against real files. It is applied inside the container, never on the host.
    """
    workspace_view = build_workspace_view(sandbox, task.files)
    messages = [{"role": "user", "content": build_prompt(task, workspace_view)}]

    attempts: list[WorkAttempt] = []

    for attempt_no in range(1, max_attempts + 1):
        response = await router.complete(messages, purpose="worker")
        result, errors = _validate(response.text)
        attempt = WorkAttempt(
            raw_text=response.text, response=response,
            errors=errors, result=result,
        )
        attempts.append(attempt)

        if result is not None:
            # ---- self-critique loop: one bounded repair round --------------
            # Only for produced work with a diff to review; never for
            # blocked/no_change_needed. A parse-failing OR provider-failing
            # critique skips the repair round — the safety net must never
            # sink the task it protects. The extra model calls are recorded
            # for cost attribution.
            if (
                settings.enable_critique
                and result.produced_work
                and result.diff
            ):
                try:
                    critique = await _critique(
                        router, task, workspace_view, result.diff
                    )
                    if critique is not None:
                        attempt.extra_calls.append(critique.response)
                        attempt.critique_verdict = critique.verdict
                        attempt.critique_issues = critique.issues
                        if critique.wants_repair:
                            repaired, _repair_errors, repair_response = await _repair(
                                router, task, critique, response.text
                            )
                            if repair_response is not None:
                                attempt.extra_calls.append(repair_response)
                            attempt.repaired = True
                            if repaired is not None:
                                # use the repair, keep the attempt record
                                result = repaired
                except Exception:  # noqa: BLE001 — critique must never block
                    pass
            # ----------------------------------------------------------------
            applied = None
            if apply and result.produced_work:
                # Applying can fail — a malformed diff is a real outcome the
                # verifier should see, not something to hide or retry blindly.
                # files_touched hints the target file when the model forgot
                # the ---/+++ headers (ensure_diff_headers in the sandbox).
                applied = await sandbox.apply_diff(
                    result.diff or "", files=result.files_touched
                )
            return WorkOutcome(result=result, attempts=attempts, applied=applied)

        if attempt_no < max_attempts:
            messages = [{
                "role": "user",
                "content": (
                    "Your previous response was not a valid WorkerResult.\n\n"
                    "Validation errors:\n"
                    + "\n".join(f"- {e}" for e in errors)
                    + "\n\nYour previous response:\n"
                    + _truncate(response.text, 2000)
                    + "\n\nReturn the corrected JSON object and nothing else."
                ),
            }]

    raise WorkFailed(attempts)
