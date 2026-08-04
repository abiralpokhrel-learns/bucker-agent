"""Verifier plugin interface (step 20).

[HAND] — this is the gate the entire architecture is built around. Everything
upstream produces claims; this is where a claim becomes a fact or gets thrown
back.

**The rule that must never be broken: a verifier does not ask a model.**
It runs tests, it lints, it compiles, it checks citations against sources. If a
verifier ever calls an LLM to decide whether work is good, the system has
quietly become "a model grading itself" — which is the exact thing this project
exists to avoid, and it would invalidate every benchmark number produced after
that point. `tests/test_verifiers.py` asserts the verification package does not
import the router, so this stays true by construction rather than by memory.

There is deliberately no universal ``is_this_good()``. Each domain registers its
own objective check: code gets tests and lint; research gets citation
consistency. Verifiers are selected by name from the task contract, so adding a
domain never means editing the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from bucker.contracts.models import Task, WorkerResult
    from bucker.sandbox.runtime import DockerSandbox


class VerifierNotFound(Exception):
    """The task named a verifier nobody registered.

    Loud on purpose. Silently substituting a default — especially a permissive
    one — would let work through ungated, which is worse than failing the task.
    """


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The verdict. ``passed`` is the only field that gates anything."""

    passed: bool
    verifier: str
    diagnostics: str = ""
    details: dict = field(default_factory=dict)
    duration_ms: int = 0

    def summary(self) -> str:
        head = "PASSED" if self.passed else "FAILED"
        return f"[{self.verifier}] {head}: {self.diagnostics[:500]}"


@runtime_checkable
class Verifier(Protocol):
    """What a verifier must provide.

    ``verify`` receives the sandbox so it can run real commands against real
    files. It must reach its verdict from observable output — exit codes, test
    results, parsed diagnostics — never from the worker's own description of
    what it did.
    """

    name: str
    task_types: tuple[str, ...]

    async def verify(
        self,
        task: Task,
        result: WorkerResult,
        sandbox: DockerSandbox,
    ) -> VerificationResult: ...


# ------------------------------------------------------------- registry -----
_REGISTRY: dict[str, Verifier] = {}


def register(verifier: Verifier) -> Verifier:
    """Register a verifier under its name. Idempotent for the same object."""
    existing = _REGISTRY.get(verifier.name)
    if existing is not None and existing is not verifier:
        raise ValueError(
            f"verifier {verifier.name!r} is already registered by "
            f"{type(existing).__name__}"
        )
    _REGISTRY[verifier.name] = verifier
    return verifier


def get(name: str) -> Verifier:
    verifier = _REGISTRY.get(name)
    if verifier is None:
        raise VerifierNotFound(
            f"no verifier registered as {name!r}. "
            f"Registered: {sorted(_REGISTRY) or '(none)'}"
        )
    return verifier


def available() -> tuple[str, ...]:
    """Names the planner is allowed to choose from."""
    return tuple(sorted(_REGISTRY))


def for_task_type(task_type: str) -> tuple[str, ...]:
    return tuple(sorted(
        name for name, v in _REGISTRY.items() if task_type in v.task_types
    ))


def clear() -> None:
    """Test helper. Never call this from application code."""
    _REGISTRY.clear()
    # Reset the builtins-registered flag so register_builtins() works again.
    import bucker.verifiers.python_test_runner as ptr
    ptr._BUILTINS_REGISTERED = False
