"""First real end-to-end run: plan -> work -> verify, against a live model.

Everything up to now has been tested against fakes. This is the run that finds
out whether the pieces actually fit — and it produces the first recordings,
which is what makes the replay engine (step 23) testable against real data
rather than fixtures.

    # one-time: build the sandbox image
    docker build -f Dockerfile.sandbox -t bucker-sandbox:latest .

    # the real thing (free with a local Ollama model; otherwise provider-priced)
    uv run python -m scripts.smoke_run --live

    # free, from the recordings the live run produced
    uv run python -m scripts.smoke_run

The task is deliberately tiny: a two-function calculator with a failing test.
Small enough that a failure means the *plumbing* is broken, not that the model
found it hard. Save the hard tasks for the benchmark, where difficulty is the
point.

It runs the components directly rather than through Temporal. Durability is
already proven by M1; what is unproven is whether planner, sandbox, worker and
verifier compose. Keeping Temporal out means a failure here has one cause, not
two.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from bucker.config import settings
from bucker.contracts.models import Task
from bucker.core.blob import BlobStore
from bucker.core.events import EventType
from bucker.core.eventstore import EventStore, create_pool
from bucker.core.snapshots import SnapshotStore
from bucker.planner import PlanningFailed, generate_task_contract
from bucker.retry import AttemptState, decide
from bucker.router.client import ModelCallFailed, ModelRouter, RecordingMissing
from bucker.sandbox.runtime import DockerSandbox, docker_available
from bucker.verifiers import available as available_verifiers
from bucker.verifiers import get as get_verifier
from bucker.verifiers import register_builtins
from bucker.worker_agent import WorkFailed, execute_task

REPO = Path(__file__).resolve().parent.parent

# --- the toy project -------------------------------------------------------
# calc.py has add() but not subtract(); the test expects both. So the suite is
# red before the task and must be green after — a verifier that cannot tell
# those two states apart is worthless, and this is the cheapest way to find out.
CALC_PY = '''\
def add(a, b):
    """Return the sum of a and b."""
    return a + b
'''

TEST_CALC_PY = '''\
from calc import add, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2


def test_subtract_negative():
    assert subtract(0, 4) == -4
'''

OBJECTIVE = (
    "The test suite imports a `subtract` function from calc.py that does not "
    "exist yet. Add it so all tests in test_calc.py pass."
)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def explain_provider_error(message: str) -> str:
    """Turn a provider stack trace into something you can act on.

    The underlying libraries surface these as multi-screen tracebacks where the
    one useful sentence is buried. The failure is almost always billing, auth,
    or a wrong model name — and each has a different fix.
    """
    low = message.lower()
    head = "  PROVIDER CALL FAILED\n"

    if "402" in message or "more credits" in low or "payment required" in low:
        return head + (
            "  Cause: not enough credit on the account.\n\n"
            "  Note this is a *reservation*, not a charge — providers hold credit\n"
            "  against your max_tokens before generating. The actual answer here is\n"
            "  a few hundred tokens.\n\n"
            "  Options:\n"
            "    1. Add credit at https://openrouter.ai/settings/credits\n"
            "       (~$5 covers this smoke run many times over)\n"
            "    2. Use a cheaper or free model — set BUCKER_MODEL in .env, e.g.\n"
            "       openrouter/nvidia/nemotron-3-super-120b-a12b:free\n"
            "       (or choose a current ':free' model at https://openrouter.ai/models)\n"
            "    3. Lower the ceiling further:\n"
            "       BUCKER_MAX_TOKENS_PLANNER=1000 and BUCKER_MAX_TOKENS_WORKER=3000\n"
            "       in .env\n"
            "    4. Run a local model instead (no API credit):\n"
            "       install Ollama, download a coding model, then set\n"
            "       BUCKER_MODEL=ollama/<installed-model>\n"
        )

    if "401" in message or "unauthor" in low or "invalid api key" in low:
        return head + (
            "  Cause: the provider rejected the key.\n\n"
            "    - Was the key rotated or revoked?\n"
            "    - Does BUCKER_MODEL match the key's provider? An OpenRouter key\n"
            "      needs the 'openrouter/' prefix on the model name.\n"
        )

    if "not a valid model" in low or "404" in message or "model_not_found" in low:
        return head + (
            f"  Cause: the provider does not recognise the model name.\n\n"
            f"    BUCKER_MODEL is currently {settings.model!r}\n"
            f"    Check the exact identifier at https://openrouter.ai/models\n"
        )

    if "rate" in low and "limit" in low:
        return head + "  Cause: rate limited. Wait a moment and re-run.\n"

    return head + f"  {message[:600]}\n"


# -------------------------------------------------------------- diagnosis --
def diagnose_env(key_vars: tuple[str, ...]) -> str:
    """Say exactly which link in the chain is broken, not just 'no key'.

    The chain is: .env exists -> python-dotenv installed -> file parses ->
    the key line is uncommented -> the value is real. A message that says
    only "no key found" makes you check all five yourself.
    """
    from bucker.config import DOTENV_ERROR, DOTENV_LOADED, DOTENV_PATH

    lines: list[str] = []

    if DOTENV_ERROR and "not installed" in DOTENV_ERROR:
        return (
            f"      {DOTENV_ERROR}\n"
            f"      Until it is installed, nothing in .env is read at all."
        )

    if not DOTENV_PATH.exists():
        lines.append(f"      .env does not exist at {DOTENV_PATH}")
        # The classic Windows trap: Notepad silently appends .txt.
        strays = sorted(
            p.name for p in DOTENV_PATH.parent.glob(".env*") if p.name != ".env"
        )
        if strays:
            lines.append(f"      but these look close: {', '.join(strays)}")
            if any(s.endswith(".txt") for s in strays):
                lines.append(
                    "      -> Notepad appends .txt unless you pick "
                    '"All files" in the save dialog. Rename it to exactly .env'
                )
            else:
                lines.append("      -> rename the right one to exactly .env")
        return "\n".join(lines)

    lines.append(f"      .env found at {DOTENV_PATH}")
    lines.append(f"      loaded by python-dotenv: {DOTENV_LOADED}")

    try:
        from dotenv import dotenv_values

        values = dotenv_values(DOTENV_PATH)
    except Exception as exc:
        lines.append(f"      could not parse it: {type(exc).__name__}: {exc}")
        return "\n".join(lines)

    present = [k for k in key_vars if k in values and values[k]]
    if present:
        lines.append(
            f"      {present[0]} IS in the file but did not reach the process — "
            f"check for stray quotes or spaces around the '='"
        )
        return "\n".join(lines)

    lines.append(f"      keys parsed from the file: {sorted(values) or '(none)'}")

    # Is the key line there but commented out? The single most likely cause.
    raw = DOTENV_PATH.read_text(encoding="utf-8", errors="replace")
    for var in key_vars:
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") and var in stripped:
                lines.append(
                    f"      -> {var} is present but COMMENTED OUT:\n"
                    f"           {stripped[:70]}\n"
                    f"         Delete the leading '#' and put your key after the '='"
                )
                return "\n".join(lines)

    if "REPLACE_ME" in raw:
        lines.append("      -> the file still contains REPLACE_ME placeholders")

    lines.append(
        "      -> add a line with no leading '#', e.g.\n"
        "           OPENROUTER_API_KEY=sk-or-v1-yourkeyhere"
    )
    return "\n".join(lines)


# --------------------------------------------------------------- preflight --
async def preflight(live: bool) -> list[str]:
    """Check everything before spending a cent. Fail with instructions."""
    problems: list[str] = []

    if not await docker_available():
        problems.append("Docker is not running. Start Docker Desktop.")
    else:
        probe = subprocess.run(
            ["docker", "image", "inspect", settings.sandbox_image],
            capture_output=True,
        )
        if probe.returncode != 0:
            problems.append(
                f"sandbox image {settings.sandbox_image!r} not found. Build it:\n"
                f"      docker build -f Dockerfile.sandbox -t {settings.sandbox_image} ."
            )

    try:
        pool = await create_pool(settings.database_url)
        await pool.close()
    except Exception as exc:
        problems.append(
            f"cannot reach Postgres ({type(exc).__name__}). Try:\n"
            f"      docker compose up -d && uv run python -m bucker.cli migrate"
        )

    if live:
        # LiteLLM routes `ollama/<model>` to the Ollama service running on this
        # machine. It needs neither an API key nor provider credit, while still
        # exercising the real request -> response -> recording path.
        is_ollama = settings.model.startswith("ollama/")
        if is_ollama:
            local_model = settings.model.removeprefix("ollama/")
            try:
                probe = subprocess.run(
                    ["ollama", "show", local_model],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except FileNotFoundError:
                problems.append(
                    "BUCKER_MODEL uses Ollama but Ollama is not installed. Install it from "
                    "https://ollama.com, then run:\n"
                    f"      ollama pull {local_model}"
                )
            except subprocess.TimeoutExpired:
                problems.append(
                    "Ollama did not respond within 15 seconds. Start the Ollama service, "
                    f"then make sure model {local_model!r} is installed."
                )
            else:
                if probe.returncode != 0:
                    problems.append(
                        f"Local Ollama model {local_model!r} is not available. "
                        f"Download it with:\n      ollama pull {local_model}"
                    )

        key_vars = (
            "OPENROUTER_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
        )
        if not is_ollama and not any(os.environ.get(k) for k in key_vars):
            problems.append(
                "no provider API key reached the process.\n" + diagnose_env(key_vars)
            )

        # Cheap consistency check: an OpenRouter key with a bare model name is a
        # confusing 401 later. Catch the mismatch before spending anything.
        only_openrouter = not is_ollama and os.environ.get("OPENROUTER_API_KEY") and not any(
            os.environ.get(k) for k in key_vars[1:]
        )
        if only_openrouter and not settings.model.startswith("openrouter/"):
            problems.append(
                f"OPENROUTER_API_KEY is set but BUCKER_MODEL is "
                f"{settings.model!r}. OpenRouter models need the prefix, e.g.\n"
                f"      BUCKER_MODEL=openrouter/anthropic/claude-sonnet-4"
            )
        try:
            import litellm  # noqa: F401
        except ImportError:
            problems.append("litellm not installed. Run: uv sync --extra llm")

    return problems


# ------------------------------------------------------------------- setup --
def seed_workspace(task_id: str) -> Path:
    workspace = Path(settings.blob_root).parent / "workspace" / task_id
    workspace.mkdir(parents=True, exist_ok=True)

    # Write LF, never CRLF: the sandbox is a Linux container, and Windows
    # text-mode writes (Path.write_text with default newline=None) translate
    # \n -> \r\n, which breaks git apply, pytest, and every tool in the
    # image. sandbox.write_file has the same guard (newline="") — this is
    # the host-side equivalent. Found the hard way: a CRLF workspace makes
    # every LF-context diff fail to apply.
    def _write(name: str, content: str) -> None:
        with open(workspace / name, "w", encoding="utf-8", newline="") as f:
            f.write(content)

    _write("calc.py", CALC_PY)
    _write("test_calc.py", TEST_CALC_PY)
    return workspace


# --------------------------------------------------------------------- run --
async def main(live: bool, keep: bool) -> int:
    register_builtins()
    mode = "live" if live else "recorded"
    os.environ["BUCKER_MODEL_MODE"] = mode

    rule("BUCKER SMOKE RUN :: plan -> work -> verify")
    say(f"  model      {settings.model}")
    say(f"  mode       {mode}" + ("   (this run costs money)" if live else "   (free)"))
    say(f"  image      {settings.sandbox_image}")
    say(f"  verifiers  {', '.join(available_verifiers())}")

    problems = await preflight(live)
    if problems:
        say("\nPREFLIGHT FAILED:")
        for p in problems:
            say(f"  - {p}")
        return 1
    say("\n  preflight ok")

    task_id = uuid4()
    workspace = seed_workspace(str(task_id))
    blobs = BlobStore(settings.blob_root)
    router = ModelRouter(blobs, mode=mode)

    pool = await create_pool(settings.database_url)
    store = EventStore(pool)
    snaps = SnapshotStore(pool, store)

    started = time.time()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tasks (id, task_type, objective, status) "
                "VALUES ($1, 'code_change', $2, 'pending')",
                task_id, OBJECTIVE,
            )
        await store.append(
            task_id, EventType.TASK_CREATED,
            {"objective": OBJECTIVE, "task_type": "code_change"},
            idempotency_key=f"{task_id}:created",
        )

        # --- 1. plan --------------------------------------------------
        rule("1. PLANNER — fuzzy objective becomes a typed contract")
        say(f"  max_tokens  {router.max_tokens_for('planner')}"
            f"   (always set — an unbounded generation is an unbounded bill)")
        try:
            plan = await generate_task_contract(router, OBJECTIVE)
        except ModelCallFailed as exc:
            say("\n" + explain_provider_error(str(exc)))
            return 2
        except RecordingMissing as exc:
            say(f"\n  no recording for this call:\n  {exc}")
            say("\n  Run once with --live to record it.")
            return 2
        except PlanningFailed as exc:
            say(f"\n  PLANNER FAILED after {len(exc.attempts)} attempts")
            for i, att in enumerate(exc.attempts, 1):
                say(f"    attempt {i}: {att.errors}")
            return 1

        task: Task = plan.task
        say(f"  task_type        {task.task_type}")
        say(f"  objective        {task.objective}")
        say(f"  files            {task.files}")
        say(f"  verifier         {task.verifier}")
        say(f"  budget/deadline  ${task.budget_usd} / {task.deadline_minutes}min")
        say(f"  attempts         {len(plan.attempts)}"
            + ("  (repaired after a schema failure)" if plan.repaired else ""))
        say(f"  cost             ${plan.cost_usd:.6f}")

        # The planner picks the verifier; a bad pick must not silently pass.
        if task.verifier not in available_verifiers():
            say(f"\n  planner chose an unregistered verifier: {task.verifier!r}")
            return 1

        # --- 2. work --------------------------------------------------
        rule("2. WORKER — produces a claim, inside the sandbox")
        sandbox = DockerSandbox(workspace)
        await sandbox.start()
        try:
            say(f"  container   {sandbox.container_name}  (network: none)")

            # Prove the suite is red first. A verifier that cannot distinguish
            # before from after is not verifying anything.
            before = await sandbox.exec("python -m pytest -q 2>&1 | tail -3")
            before_line = (
                before.stdout.strip().splitlines()[-1]
                if before.stdout.strip() else "(no output)"
            )
            say(f"  tests before  {before_line}")

            try:
                outcome = await execute_task(router, task, sandbox)
            except ModelCallFailed as exc:
                say("\n" + explain_provider_error(str(exc)))
                return 2
            except WorkFailed as exc:
                say(f"\n  WORKER FAILED after {len(exc.attempts)} attempts")
                for i, att in enumerate(exc.attempts, 1):
                    say(f"    attempt {i}: {att.errors}")
                return 1

            result = outcome.result
            say(f"  status        {result.status}   <- a claim, not a verdict")
            say(f"  summary       {result.summary[:100]}")
            say(f"  files_touched {result.files_touched}")
            say(f"  cost          ${outcome.cost_usd:.6f}")
            if outcome.applied is not None:
                ok = "applied" if outcome.applied.exit_code == 0 else "FAILED TO APPLY"
                say(f"  diff          {ok} (exit {outcome.applied.exit_code})")
                if outcome.applied.secret_findings:
                    say(f"  secrets       {len(outcome.applied.secret_findings)} redacted")

            # --- 3. verify --------------------------------------------
            rule("3. VERIFIER — the claim meets an objective check")
            verifier = get_verifier(task.verifier)
            verdict = await verifier.verify(task, result, sandbox)

            say(f"  verifier    {verdict.verifier}")
            say(f"  passed      {verdict.passed}")
            say(f"  duration    {verdict.duration_ms}ms")
            say(f"  diagnostics {verdict.diagnostics[:400]}")
        finally:
            await sandbox.stop()

        await store.append(
            task_id,
            EventType.VERIFICATION_PASSED if verdict.passed
            else EventType.VERIFICATION_FAILED,
            {"verifier": verdict.verifier, "details": verdict.details},
            tool_output_ref=blobs.put(verdict.diagnostics),
            idempotency_key=f"{task_id}:verify-1",
        )

        # --- 4. decide ------------------------------------------------
        rule("4. POLICY — what happens next")
        total_cost = plan.cost_usd + outcome.cost_usd
        elapsed = (time.time() - started) / 60.0
        decision = decide(AttemptState(
            attempt=1, max_retries=2,
            verification_passed=verdict.passed,
            diagnostics=verdict.diagnostics,
            cost_usd=total_cost, budget_usd=task.budget_usd,
            elapsed_minutes=elapsed, deadline_minutes=task.deadline_minutes,
        ))
        say(f"  action  {decision.action.upper()}")
        say(f"  reason  {decision.reason}")

        # --- summary --------------------------------------------------
        rule("RESULT")
        events = await store.read_stream(task_id)
        state = await snaps.get_state(task_id)

        say(f"  task_id      {task_id}")
        say(f"  verdict      {'PASSED' if verdict.passed else 'FAILED'}")
        say(f"  total cost   ${total_cost:.6f}")
        say(f"  elapsed      {elapsed * 60:.1f}s")
        say(f"  events       {len(events)}  ({', '.join(e.event_type for e in events)})")
        say(f"  recordings   {router.recordings.count()} stored")
        say(f"  state        {state['status']}")

        if verdict.passed:
            say("\n  The pipeline works end to end: a fuzzy objective became a typed")
            say("  contract, a model wrote real code in an isolated container, and")
            say("  the project's own tests — not the model — decided it was correct.")
            if live:
                say("\n  Re-run without --live to replay from recordings, free and")
                say("  deterministic. That is step 23's foundation.")
        else:
            say("\n  Verification failed. That is a legitimate outcome, not a crash —")
            say("  the diagnostics above are exactly what a retry would feed back to")
            say("  the planner. Read them before assuming the plumbing is broken.")

        if not keep:
            say(f"\n  workspace: {workspace}")

        return 0 if verdict.passed else 1
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="bucker end-to-end smoke run")
    parser.add_argument("--live", action="store_true",
                        help="call a real model and create recordings")
    parser.add_argument("--keep", action="store_true",
                        help="suppress the workspace path note")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.live, args.keep)))
