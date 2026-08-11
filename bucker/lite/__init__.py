"""Lite-mode startup: one command, zero infrastructure.

``bucker lite`` runs the whole platform with NOTHING but Python
installed — no Docker, no Postgres, no Temporal, no uv. It:

1. Uses ``bucker_lite.db`` (sqlite) for storage — created automatically.
2. Sets ``BUCKER_SANDBOX_MODE=local`` so the worker runs as host
   subprocesses in a scratch dir instead of containers (no Docker).
3. Starts the dashboard/API on :8123. Tasks run in-process (see
   ``bucker/lite/runner.py``) instead of through Temporal.
4. Works with zero API keys for demo tasks; code_change tasks need a
   model key in ``.env`` (or ollama running locally), exactly like the
   full stack.

The full stack (``bucker dev``) is unchanged — this is a second entry
point, not a replacement. Production durability (Temporal + Postgres +
Docker isolation) still belongs to the full path.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
API_PORT = 8123
LITE_DB = "bucker_lite.db"


def lite_env() -> dict[str, str]:
    """Environment for a lite-mode process (worker/API)."""
    env = dict(os.environ)
    env["BUCKER_DATABASE_URL"] = f"sqlite:///{PROJECT_ROOT / LITE_DB}"
    env["BUCKER_SANDBOX_MODE"] = "local"
    # Model mode is NOT forced: recorded (the platform default) works with
    # zero API keys — demo tasks and replay need no providers. A user who
    # adds keys to .env and sets BUCKER_MODEL_MODE=live gets live calls,
    # exactly like the full stack.
    return env


async def ensure_db() -> None:
    """Create the sqlite schema if missing. Idempotent, fast."""
    from bucker.core.eventstore import create_pool

    env = lite_env()
    pool = await create_pool(env["BUCKER_DATABASE_URL"])
    await pool.init_schema()
    await pool.close()


async def run_lite(*, open_browser: bool = True, port: int = API_PORT) -> int:
    """Start the lite stack: schema + uvicorn, one process, one terminal."""
    print("  [lite] storage  : sqlite ->", PROJECT_ROOT / LITE_DB)
    print("  [lite] sandbox  : local host subprocesses (no Docker)")
    print("  [lite] runner   : in-process (no Temporal)")
    await ensure_db()
    print("  [lite] schema   : ready")

    env = lite_env()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uvicorn", "bucker.api.app:app",
        "--port", str(port),
        env=env,
        stdout=None,  # inherit: logs go to the terminal
        stderr=None,
    )
    print(f"  dashboard : http://localhost:{port}")
    print("  press Ctrl+C to stop")
    print()

    if open_browser:
        try:
            import webbrowser

            webbrowser.open(f"http://localhost:{port}")
            print("  [lite] opened the dashboard in your browser")
        except Exception:  # noqa: BLE001 — cosmetic
            pass

    try:
        await proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        print("\n  [lite] stopped")
    return 0
