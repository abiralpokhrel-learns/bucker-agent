"""One-command local stack (usability pass).

The classic setup needed THREE terminals (Temporal + worker + dashboard),
a separate Temporal CLI install, a hand-made .env, and manual DB setup.
This module collapses that into two commands:

    uv run python -m bucker.cli setup   # checks + fixes + .env + DB
    uv run python -m bucker.cli dev     # starts the whole stack, one terminal

``plan_stack`` is PURE (returns what would be started) — the dry-run and
the live supervisor share it, and tests assert on it. The supervisor only
spawns what is not already running (ports are probed), so re-running is
safe and idempotent.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import socket
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_PORT = 8123
TEMPORAL_UI_PORT = 8233
TEMPORAL_GRPC_PORT = 7233
POSTGRES_PORT = 5432


def port_open(host: str = "127.0.0.1", port: int = 0, timeout: float = 1.0) -> bool:
    """True when something is listening on the port. Pure enough to test."""
    if not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_env(example: Path, target: Path) -> str:
    """Create .env from the example when missing; generate a random token.

    Returns 'created' or 'exists'. Never overwrites an existing .env.
    """
    if target.exists():
        return "exists"
    template = example.read_text(encoding="utf-8")
    token = secrets.token_hex(32)
    if "BUCKER_API_TOKEN=" in template:
        template = template.replace(
            "BUCKER_API_TOKEN=dev-token", f"BUCKER_API_TOKEN={token}"
        )
    target.write_text(template, encoding="utf-8")
    return "created"


def _has(command: str) -> bool:
    return shutil.which(command) is not None


def plan_stack(*, postgres_port: int = POSTGRES_PORT,
               temporal_port: int = TEMPORAL_UI_PORT,
               api_port: int = API_PORT) -> dict:
    """PURE plan: what the supervisor will start vs skip. Testable."""
    plan = {
        "postgres": "running" if port_open(port=postgres_port)
                    else ("start" if _has("docker") else "missing-docker"),
        "temporal": "running" if port_open(port=temporal_port)
                    else ("start-cli" if _has("temporal")
                          else ("start-docker" if _has("docker")
                                else "missing")),
        "worker": "start",
        "api": "running" if port_open(port=api_port) else "start",
    }
    plan["needs_docker"] = plan["postgres"] in ("start", "missing-docker") or \
        plan["temporal"] == "start-docker"
    return plan


def _print_plan(plan: dict) -> None:
    labels = {
        "running": "already running - skip",
        "start": "will start",
        "start-cli": "will start (temporal CLI)",
        "start-docker": "will start (docker image)",
        "missing": "MISSING - install temporal CLI or use Docker",
        "missing-docker": "MISSING - start Docker Desktop",
    }
    print("  postgres :", labels.get(plan["postgres"], plan["postgres"]))
    print("  temporal :", labels.get(plan["temporal"], plan["temporal"]))
    print("  worker   :", labels.get(plan["worker"], plan["worker"]))
    print("  api      :", labels.get(plan["api"], plan["api"]))


async def _wait_ready(name: str, port: int, timeout_s: float = 60) -> bool:
    for _ in range(int(timeout_s / 0.5)):
        if port_open(port=port):
            return True
        await asyncio.sleep(0.5)
    print(f"  [dev] {name} did not become ready in {timeout_s:.0f}s")
    return False


async def _spawn(cmd: list[str], *, env: dict | None = None,
                 silent: bool = False) -> asyncio.subprocess.Process:
    """Spawn a child that INHERITS the terminal (logs are visible).

    On Windows, PIPE streams on the proactor loop crash at shutdown, and
    hiding the worker/API logs is bad UX anyway — children inherit stdio.
    """
    full_env = {**os.environ, **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=full_env,
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )
    return proc


async def run_stack(*, live_models: bool = True, open_browser: bool = True) -> int:
    """Start everything not already running; Ctrl+C stops what we started."""
    plan = plan_stack()
    print("bucker dev — local stack")
    _print_plan(plan)
    started: list[asyncio.subprocess.Process] = []

    try:
        # --- postgres (docker compose, idempotent) -------------------------
        if plan["postgres"] == "start":
            print("  [dev] starting postgres via docker compose ...")
            await _spawn(["docker", "compose", "up", "-d"], silent=True)
            if not await _wait_ready("postgres", POSTGRES_PORT):
                return 2
        elif plan["postgres"] == "missing-docker":
            print("  [dev] Docker is not running. Start Docker Desktop first.")
            return 2

        # --- temporal --------------------------------------------------------
        if plan["temporal"] == "start-cli":
            print("  [dev] starting temporal (dev server) ...")
            started.append(await _spawn(["temporal", "server", "start-dev"],
                                        silent=True))
            if not await _wait_ready("temporal", TEMPORAL_UI_PORT):
                return 2
        elif plan["temporal"] == "start-docker":
            print("  [dev] starting temporal (docker dev-server image) ...")
            await _spawn(
                ["docker", "run", "-d", "--name", "bucker-temporal",
                 "-p", f"{TEMPORAL_GRPC_PORT}:7233",
                 "-p", f"{TEMPORAL_UI_PORT}:8233",
                 "temporalio/dev-server:latest"], silent=True)
            if not await _wait_ready("temporal", TEMPORAL_UI_PORT):
                return 2
        elif plan["temporal"] == "missing":
            print("  [dev] no temporal CLI and no Docker — cannot start Temporal.")
            return 2

        # --- worker -----------------------------------------------------------
        worker_env = {"BUCKER_MODEL_MODE": "live" if live_models else "recorded"}
        print("  [dev] starting worker ...")
        started.append(await _spawn(
            [sys.executable, "-m", "bucker.worker"], env=worker_env, silent=True))
        await asyncio.sleep(6)  # give the worker a moment to register

        # --- api ---------------------------------------------------------------
        if plan["api"] == "start":
            print("  [dev] starting dashboard ...")
            started.append(await _spawn(
                [sys.executable, "-m", "uvicorn", "bucker.api.app:app",
                 "--port", str(API_PORT)], silent=True))
            await _wait_ready("api", API_PORT)

        print()
        print(f"  dashboard : http://localhost:{API_PORT}")
        print(f"  temporal  : http://localhost:{TEMPORAL_UI_PORT}")
        print("  press Ctrl+C to stop everything")
        print()

        if open_browser:
            try:
                import webbrowser

                webbrowser.open(f"http://localhost:{API_PORT}")
                print("  [dev] opened the dashboard in your browser")
            except Exception:  # noqa: BLE001 — cosmetic
                pass

        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for proc in started:
            if proc.returncode is None:
                proc.terminate()
        for proc in started:
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
        if started:
            print("\n  [dev] stopped what it started")
    return 0


async def _db_migrated() -> bool:
    """Schema present? (migrations re-run top-to-bottom, so "applied" ==
    "the tables exist"). Any failure — no DB, wrong creds, nothing there —
    means setup is needed."""
    from bucker.config import settings

    try:
        import asyncpg

        conn = await asyncpg.connect(settings.database_url, timeout=3)
        try:
            return bool(
                await conn.fetchval(
                    "SELECT to_regclass('public.tasks') IS NOT NULL"
                )
            )
        finally:
            await conn.close()
    except Exception:  # noqa: BLE001 — any probe failure => not ready
        return False


async def first_run_needed() -> bool:
    """True when the stack is not ready: no .env, no reachable database,
    or migrations not applied. ``bucker dev`` bootstraps when this is
    true, so the ONLY command a user needs to remember is ``dev``."""
    if not (PROJECT_ROOT / ".env").exists():
        return True
    if not port_open(port=POSTGRES_PORT):
        return True
    return not await _db_migrated()


async def run_dev(
    *,
    live_models: bool = True,
    force_setup: bool = False,
    open_browser: bool = True,
) -> int:
    """The ONE command. First run: setup (prereqs, .env, Postgres,
    migrations) then start the stack. Later runs: start only."""
    if force_setup or await first_run_needed():
        print("\n  [dev] first run detected — initializing ...\n")
        rc = await run_setup()
        if rc != 0:
            print("  [dev] setup did not finish cleanly — fix the items above, "
                  "then run this command again.")
            return rc
    return await run_stack(live_models=live_models, open_browser=open_browser)


async def run_setup() -> int:
    """One-command environment bootstrap: checks, fixes, .env, DB.

    Deliberately SYNCHRONOUS (subprocess.run + time.sleep): setup is a
    short script, and asyncio subprocesses on the Windows proactor loop
    leak/crash at shutdown.
    """
    import subprocess
    import time

    print("bucker setup — getting your machine ready")
    ok = True

    # --- python / uv --------------------------------------------------------
    # (requires-python >= 3.11 is enforced by uv itself, so no version gate
    # is needed here — the check below is only about the tool being present.)
    if not _has_uv():
        if _try_install_uv():
            print("  [setup] uv installed")
        else:
            print("  [setup] uv missing. Install it, then rerun: "
                  "`winget install astral-sh.uv` "
                  "(or: pip install uv / curl -LsSf https://astral.sh/uv/install.sh | sh)")
            ok = False
    else:
        print(f"  [setup] uv ok (python {sys.version_info.major}.{sys.version_info.minor} "
              f"via uv run)")

    # --- docker ----------------------------------------------------------------
    if not _has("docker"):
        print("  [setup] docker missing — Docker Desktop: "
              "https://www.docker.com/products/docker-desktop/")
        if _open_browser_prompt("Open the download page?"):
            import webbrowser

            webbrowser.open("https://www.docker.com/products/docker-desktop/")
        ok = False
    elif not _has_docker_daemon():
        print("  [setup] Docker is installed but not running — start Docker Desktop.")
        ok = False
    else:
        print("  [setup] docker ok")

    # --- .env -------------------------------------------------------------------
    env_path = PROJECT_ROOT / ".env"
    example_path = PROJECT_ROOT / ".env.example"
    if example_path.exists():
        result = ensure_env(example_path, env_path)
        print("  [setup] .env created (BUCKER_API_TOKEN generated — "
              "open .env to add model keys)" if result == "created"
              else "  [setup] .env already present")
    else:
        print("  [setup] .env.example missing — cannot create .env")
        ok = False

    if not ok:
        print("  [setup] fix the items above, then run this command again.")
        return 1

    # --- database -----------------------------------------------------------------
    if not port_open(port=POSTGRES_PORT):
        print("  [setup] starting postgres ...")
        subprocess.run(["docker", "compose", "up", "-d"], check=False)
        for _ in range(60):
            if port_open(port=POSTGRES_PORT):
                break
            time.sleep(0.5)
        if not port_open(port=POSTGRES_PORT):
            print("  [setup] postgres did not start — check `docker ps`")
            return 2
    else:
        print("  [setup] postgres already running")
    subprocess.run([sys.executable, "-m", "bucker.cli", "migrate"], check=False)

    print()
    print("  everything is ready. Start the whole stack with one command:")
    print("      uv run python -m bucker.cli dev")
    print("  then open http://localhost:8123")
    return 0


def _has_docker_daemon() -> bool:
    """Docker daemon reachable? (Docker Desktop exposes a named pipe / tcp)."""
    try:
        import subprocess
        result = subprocess.run(["docker", "info"], capture_output=True,
                                timeout=15)
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _uv_install_path() -> Path | None:
    """uv often installs off-PATH for the CURRENT shell (new shells see it).
    Known home locations, checked so a fresh install still counts."""
    exe = "uv.exe" if os.name == "nt" else "uv"
    candidates = [
        Path.home() / ".local" / "bin" / exe,
        Path.home() / ".cargo" / "bin" / exe,
    ]
    return next((p for p in candidates if p.exists()), None)


def _has_uv() -> bool:
    return _has("uv") or _uv_install_path() is not None


def _open_browser_prompt(question: str) -> bool:
    """Yes/Enter => True; n/N => False; EOF (CI) => False."""
    try:
        answer = input(f"  {question} [Y/n] ").strip().lower()
    except EOFError:
        return False
    return answer in ("", "y", "yes")


def _try_install_uv() -> bool:
    """Offer to install uv (safe, user-scoped). Returns True if uv is
    available after the attempt (possibly via a known install path)."""
    if not _open_browser_prompt("uv is missing. Install it now?"):
        return False
    import subprocess

    try:
        if os.name == "nt" and _has("winget"):
            rc = subprocess.run(["winget", "install", "astral-sh.uv"],
                                check=False).returncode
        else:
            rc = subprocess.run(
                ["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
                check=False).returncode
    except Exception:  # noqa: BLE001
        return False
    return rc == 0 and _has_uv()
