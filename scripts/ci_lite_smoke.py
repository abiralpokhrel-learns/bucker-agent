"""CI smoke test for the lite path — the thing we actually sell.

Boots the whole zero-infrastructure platform exactly the way a fresh user
would (the launcher command in LITE_CMD), then proves the core loop works:
dashboard answers, a demo task runs end to end, the append-only event
stream is produced, and the verdict is `completed`.

This is deliberately a REAL end-to-end run, not unit tests: it is the
regression gate for

  * the dependency split (base install has no temporalio/asyncpg — the
    bucker.temporal_compat shim + lazy imports must make lite work
    without them),
  * the launchers (start.bat on Windows CI, start.sh-equivalent on
    Linux CI),
  * the demo pipeline + event store over the HTTP surface.

Usage:
    LITE_CMD="..." python scripts/ci_lite_smoke.py

LITE_CMD defaults to `python -m bucker.cli lite` (POSIX). On Windows CI
it is `cmd /c start.bat` so the real launcher is exercised. Exit 0 only
when a demo task reaches `completed` with a non-empty event stream.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = "http://localhost:8123"
#: How the platform is started. CI overrides this to exercise the real
#: launcher (e.g. `cmd /c start.bat` on Windows, `./start.sh` on Linux).
#: shell=True, so the whole command line is fine as one string — but the
#: default must quote sys.executable because the repo path contains spaces.
LITE_CMD = os.environ.get("LITE_CMD", f'"{sys.executable}" -m bucker.cli lite')


def _http(method: str, path: str, body: dict | None = None, timeout: float = 30):
    """Tiny urllib request helper (httpx may not be importable pre-install).

    Returns (status, payload) where payload is parsed JSON when the body
    looks like JSON, else the raw text (e.g. the dashboard's HTML).
    """
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        status = e.code
    except Exception as e:  # noqa: BLE001 — connection refused while booting
        return 0, {"error": str(e)}
    try:
        return status, json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return status, raw


def wait_for_dashboard(proc: subprocess.Popen, timeout: float = 300) -> None:
    """Poll the dashboard until it answers, or the server dies first."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise SystemExit(
                f"FATAL: lite server exited during boot (code {proc.returncode})\n{out}"
            )
        status, _ = _http("GET", "/", timeout=5)
        if status >= 100:  # any HTTP answer means the server is up
            return
        time.sleep(1.5)
    raise SystemExit(f"FATAL: dashboard did not answer on {BASE} within {timeout}s")


def terminate(proc: subprocess.Popen) -> None:
    """Kill the server and its whole tree (lite spawns uvicorn as a child)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True, check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    print(f"[smoke] repo     : {REPO}")
    print(f"[smoke] lite cmd : {LITE_CMD}")
    # Fail fast if something is already on 8123 — a leftover server with a
    # stale DB handle answers the health probe but 500s every request
    # (seen locally; would otherwise cost a whole wait cycle).
    status, _ = _http("GET", "/", timeout=5)
    if status >= 100:
        raise SystemExit(
            f"FATAL: something is already answering on {BASE} "
            "(status {status}) — kill it first, or the smoke run will "
            "exercise the wrong server."
        )
    # Own process group so terminate() can take the uvicorn child down too.
    log = open(REPO / "lite-smoke.log", "w", encoding="utf-8")  # noqa: SIM115 — the handle must outlive the Popen for its whole lifetime
    if os.name == "nt":
        # Windows: NO shell=True. With it, Python wraps the command as
        # `cmd /c "cmd /c start.bat"` — the double nesting hangs the batch
        # and swallows all output (verified empirically: zero bytes written,
        # process alive forever). shell=False passes the command line raw to
        # CreateProcess, which runs the batch normally.
        proc = subprocess.Popen(
            LITE_CMD, cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
        )
    else:
        proc = subprocess.Popen(
            LITE_CMD, cwd=REPO, shell=True, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    try:
        wait_for_dashboard(proc)

        status, task = _http(
            "POST",
            "/tasks?objective=ci+smoke:+demo+pipeline+end+to+end&task_type=demo"
            "&budget_usd=0.50&deadline_minutes=10",
        )
        assert status == 200, f"POST /tasks failed: {status} {task}"
        task_id = task["task_id"]
        print(f"[smoke] task      : {task_id} ({task.get('status')})")

        deadline = time.monotonic() + 180
        last = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise SystemExit("FATAL: lite server died mid-task — see lite-smoke.log")
            status, detail = _http("GET", f"/tasks/{task_id}", timeout=10)
            if status == 200:
                last = detail.get("status", "?")
                print(f"[smoke]   status  : {last} (events: {detail.get('event_count')})")
                if last in {"completed", "failed", "halted", "needs_human_review"}:
                    break
            time.sleep(3)
        else:
            raise SystemExit(f"FATAL: task did not finish in 180s (last: {last})")

        events_status, events = _http("GET", f"/tasks/{task_id}/events", timeout=10)
        n_events = len(events) if events_status == 200 and isinstance(events, list) else 0

        print()
        print(f"[smoke] verdict   : {last}")
        print(f"[smoke] events    : {n_events}")
        if last != "completed":
            raise SystemExit(f"FAIL: expected completed, got {last}")
        if n_events < 10:  # the demo pipeline produces 13+ events
            raise SystemExit(f"FAIL: event stream too thin ({n_events} < 10)")
        print("[smoke] PASS — lite platform boots and a demo task verifies end to end.")
        return 0
    finally:
        terminate(proc)
        log.close()


if __name__ == "__main__":
    sys.exit(main())
