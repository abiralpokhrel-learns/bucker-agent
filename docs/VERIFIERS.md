# Writing a verifier

A verifier is how a task becomes a fact. The worker *claims* something;
the verifier *checks* it, objectively, with no model in the loop. This is
the project's core promise, so the contract is deliberately small and the
constraints are hard.

## The contract

```python
from bucker.contracts.models import Task, WorkerResult
from bucker.sandbox.runtime import DockerSandbox
from bucker.verifiers.base import Verifier, Verdict


class MyVerifier(Verifier):
    name = "my_verifier"          # registered name, used by planner/config

    async def verify(self, task: Task, result: WorkerResult,
                     sandbox: DockerSandbox) -> Verdict:
        ...
        return Verdict(
            passed=bool,
            verifier=self.name,
            details={...},         # structured facts for the dashboard
            diagnostics="...",     # text fed back into the retry prompt
            duration_ms=...,
        )
```

- `task` — the typed contract the planner produced (objective, files,
  verifier name, budget).
- `result` — the worker's `WorkerResult`: status, summary, and
  tool outputs (the diff, files touched, command logs).
- `sandbox` — a running, network-isolated container whose workspace
  already has the diff applied. Run the real checks inside it; never
  trust host state.

## The hard rules

1. **Never call a model.** The test suite asserts that verifier modules do
   not import the router. If your verifier needs a judgment call, encode
   the judgment as a rule, not as a model prompt.
2. **Run in the sandbox, not on the host.** The workspace the worker
   produced lives in the container; check it there.
3. **Deterministic.** Same inputs → same verdict. No wall-clock-dependent
   outcomes, no randomness.
4. **Fail with diagnostics, not exceptions.** A raised exception is a
   plumbing failure (and is reported as one); a failed check is a
   `Verdict(passed=False, diagnostics=...)` that the retry loop feeds back
   to the worker verbatim. Write diagnostics the way you would write a
   bug report to a junior engineer.

## Registering

Add the module to `bucker/verifiers/` and register it:

```python
# bucker/verifiers/__init__.py
from bucker.verifiers.my_verifier import MyVerifier

_BUILTINS: dict[str, type[Verifier]] = {
    ...
    MyVerifier.name: MyVerifier,
}
```

Then confirm the planner knows it exists (`bucker/planner.py`,
`KNOWN_VERIFIERS`) — the planner may only choose registered verifiers, and
a task whose verifier has since been removed fails loudly at replay time.

## Example: a JSON-schema verifier

```python
from __future__ import annotations

import json

from bucker.contracts.models import Task, WorkerResult
from bucker.sandbox.runtime import DockerSandbox
from bucker.verifiers.base import Verifier, Verdict


class JsonSchemaVerifier(Verifier):
    """Check a worker's output file against a JSON schema.

    The objective names the schema path and the output path:
    'validate output.json against schema.json'.
    """

    name = "json_schema"

    async def verify(self, task: Task, result: WorkerResult,
                     sandbox: DockerSandbox) -> Verdict:
        import time

        started = time.monotonic()
        # The worker declares which files it touched; the output must be
        # among them.
        out = next((f for f in result.files_touched if f.endswith(".json")
                    and "schema" not in f), None)
        if out is None:
            return Verdict(
                passed=False, verifier=self.name,
                details={"missing": "no output json in files_touched"},
                diagnostics="the worker produced no output.json",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        # The schema file was seeded into the workspace before the task.
        schema_txt = await sandbox.read_file("schema.json")
        output_txt = await sandbox.read_file(out)
        try:
            schema = json.loads(schema_txt)
            data = json.loads(output_txt)
        except json.JSONDecodeError as exc:
            return Verdict(
                passed=False, verifier=self.name,
                details={"error": str(exc)[:200]},
                diagnostics=f"invalid JSON in {out}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        # Minimal draft-4-ish validation: type + required + properties.
        errors = _validate(data, schema, path="$")
        passed = not errors
        return Verdict(
            passed=passed, verifier=self.name,
            details={"output": out, "errors": errors[:20]},
            diagnostics="schema validation passed" if passed else
                        "schema validation failed:\n" + "\n".join(errors[:20]),
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def _validate(data, schema, path):
    errors = []
    if schema.get("type") == "object":
        if not isinstance(data, dict):
            errors.append(f"{path}: expected object, got {type(data).__name__}")
            return errors
        for req in schema.get("required", []):
            if req not in data:
                errors.append(f"{path}: missing required field {req!r}")
        for key, subschema in (schema.get("properties") or {}).items():
            if key in data:
                errors.extend(_validate(data[key], subschema, f"{path}.{key}"))
    elif schema.get("type") == "array":
        if not isinstance(data, list):
            errors.append(f"{path}: expected array, got {type(data).__name__}")
        else:
            for i, item in enumerate(data):
                errors.extend(_validate(item, schema.get("items", {}), f"{path}[{i}]"))
    elif schema.get("type") == "string" and not isinstance(data, str):
        errors.append(f"{path}: expected string, got {type(data).__name__}")
    elif schema.get("type") == "number" and not isinstance(data, (int, float)):
        errors.append(f"{path}: expected number, got {type(data).__name__}")
    return errors
```

## Testing a verifier

Verifiers are tested against the sandbox like the builtins are (see
`tests/test_verifiers.py`): seed a workspace, apply the worker's diff,
run `verify`, assert the verdict. The model-free rule is enforced by a
source-level test:

```python
def test_verifiers_never_import_the_router():
    import ast
    from pathlib import Path

    src = Path("bucker/verifiers").read_text(encoding="utf-8")
    assert "import bucker.router" not in src
    assert "ModelRouter" not in src
```

Keep it that way: if a verifier ever needs a model, it is no longer a
verifier — it is a second opinion that must be reviewed by a human.
