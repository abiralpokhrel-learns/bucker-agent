"""HTTP surface (BUILD_PLAN step 24) — wired to the real pipeline.

JSON API:
    POST   /tasks                    create a task, start its workflow
    GET    /tasks                    list tasks (with cost, status filter)
    GET    /tasks/{task_id}          status, cost, state
    GET    /tasks/{task_id}/events   the full audit trail
    POST   /tasks/{task_id}/replay   re-run from stored recordings
    POST   /tasks/{task_id}/rerun    new task with the same objective
    POST   /tasks/{task_id}/cancel   terminate the task's Temporal workflow
    GET    /api/system               platform health as JSON

Pages (BUILD_PLAN step 33 — server-rendered, no framework):
    GET    /                          aggregate dashboard
    GET    /tasks/new                 create-task form
    GET    /tasks/{task_id}/dashboard one-task view: what happened, why, how much
    GET    /tasks/{task_id}/replay    replay runner page
    GET    /system                    control center: model chain, providers, infra

Task routing: task_type="demo" starts the Phase 0 five-step demo workflow;
anything else (default "code_change") starts the real CodeTaskWorkflow —
planner → worker → verifier → retry/escalate. Budget, deadline and max
retries are passed through to the workflow; the planner picks the verifier
for code tasks.

Bearer-token auth over HTTPS. In dev the token defaults to 'dev-token' so the
quickstart curl commands in the README work without setup.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from uuid import UUID

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.status import (
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
)

from bucker.api.dashboard import (
    render_index,
    render_new_task_page,
    render_replay_page,
    render_system_page,
    render_task_dashboard,
    render_usage_page,
)
from bucker.config import settings
from bucker.core.blob import BlobStore
from bucker.core.events import EventType
from bucker.core.eventstore import EventStore, create_pool
from bucker.core.snapshots import SnapshotStore
from bucker.replay.engine import ReplayError, ReplayResult, replay_task
from bucker.router.client import RecordingStore

# ------------------------------------------------------------------- globals --

app = FastAPI(
    title="bucker-agent",
    version="0.1.0",
    description="Durable, verified agent execution. Nothing is trusted until verified.",
)

from bucker.api.gateway import router as gateway_router  # noqa: E402

app.include_router(gateway_router)

security = HTTPBearer(auto_error=False)

#: Initialised in lifespan so routes never import asyncpg at module scope.
_pool: "asyncpg.Pool | None" = None  # noqa: F821, UP037
_store: EventStore | None = None
_snaps: SnapshotStore | None = None
_blobs: BlobStore | None = None
#: True when the app is running without a database (startup pool creation
#: failed). The app stays up — /system and /usage show a degraded banner and
#: data routes answer 503 instead of dying mid-request.
_degraded: bool = False


def _get_pool():
    if _pool is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "database not initialised — Postgres is unreachable or "
                "migrate has not been run. Check /system and start it, then "
                "restart the API."
            ),
        )
    return _pool


def _get_store():
    global _store
    if _store is None:
        _store = EventStore(_get_pool())
    return _store


def _get_snaps():
    global _snaps
    if _snaps is None:
        _snaps = SnapshotStore(_get_pool(), _get_store())
    return _snaps


def _get_blobs():
    global _blobs
    if _blobs is None:
        _blobs = BlobStore(settings.blob_root)
    return _blobs


# -------------------------------------------------------------- middleware --


#: Hosts allowed to use the dev-token bypass. Anything else gets 401 when
#: BUCKER_API_TOKEN is the dev default, so a casually exposed server is not
#: wide open. "testserver" is httpx/TestClient's default Host.
_DEV_TOKEN_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver", ""}


@app.middleware("http")
async def _dev_token_host_guard(request, call_next):
    """Refuse non-localhost requests while auth is the dev-token bypass.

    dev-token is a convenience for local development, not a credential. If
    the API is bound to 0.0.0.0 (or reachable by hostname) while the token
    is still the default, every caller is effectively unauthenticated —
    which is fine on your own machine and a footgun anywhere else. This
    guard closes the gap: with dev-token active, only localhost hosts pass.
    """
    if settings.api_token == "dev-token":
        host = (request.headers.get("host") or "").split(":")[0].strip().lower()
        if host not in _DEV_TOKEN_LOCAL_HOSTS:
            return JSONResponse(
                status_code=HTTP_401_UNAUTHORIZED,
                content={
                    "detail": (
                        "BUCKER_API_TOKEN is still the dev default, so "
                        "non-localhost requests are refused. Set a real token "
                        "before exposing this API."
                    )
                },
            )
    return await call_next(request)


def _check_auth(creds: HTTPAuthorizationCredentials | None, *, write: bool = False):
    """Bearer-token guard with an optional read-only tier.

    - Production mode (BUCKER_PRODUCTION=1): the boot guard guarantees the
      admin token is NOT the dev default; this enforces it per request.
    - Dev mode (default): dev-token means no auth (localhost-only via the
      host guard — acceptable for a local quickstart, never beyond).
    - With BUCKER_READ_TOKEN set, GET routes accept it; every write route
      (write=True) requires the admin token.
    """
    if settings.api_token != "dev-token":
        token = creds.credentials if creds else ""
        if token == settings.api_token:
            return
        if (not write) and settings.read_token and token == settings.read_token:
            return
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token"
            + (" (write access requires the admin token)" if write else ""),
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Dev mode: dev-token default → no auth (localhost-only; host guard).
    return


# --------------------------------------------------------------- lifespan --


@app.on_event("startup")
async def _startup():
    from bucker.security.bootstrap import assert_safe_boot

    assert_safe_boot(component="api")
    global _pool
    if settings.api_token == "dev-token":
        # Loud, not silent: the dev-token bypass is a convenience, and a
        # server that outlives its dev machine is a deployment wearing a
        # costume. The host guard above refuses non-localhost callers, but
        # the operator should know the token is still the default.
        print(
            "WARNING: BUCKER_API_TOKEN is the dev default ('dev-token') — "
            "auth is BYPASSED for localhost. Set a real token before "
            "deploying or exposing this API.",
            file=sys.stderr,
        )
    if _pool is not None:
        # A pool is already injected (tests, embedding). Never clobber it:
        # creating a second pool here silently switches the app to a real
        # database connection mid-test.
        return
    try:
        _pool = await create_pool(settings.database_url)
    except Exception as exc:  # noqa: BLE001 — startup must never die silently
        # Degraded mode, not a crash: the app boots so /system and /usage
        # can show WHAT is wrong. Data routes answer 503 with a pointer
        # instead of a bare 500.
        global _degraded
        _degraded = True
        print(
            f"ERROR: database unavailable at startup ({type(exc).__name__}: "
            f"{str(exc)[:120]}) — running DEGRADED. Data routes will answer "
            "503. Fix Postgres (docker compose up -d) or run "
            "`uv run python -m bucker.cli migrate`, then restart.",
            file=sys.stderr,
        )
    else:
        print("database pool ready", file=sys.stderr)


@app.on_event("shutdown")
async def _shutdown():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# Make startup/shutdown patchable by tests.
app._startup_impl = _startup  # type: ignore[attr-defined]
app._shutdown_impl = _shutdown  # type: ignore[attr-defined]


# ------------------------------------------------------------ POST /tasks ---

#: Task types that run the Phase 0 demo workflow instead of the real pipeline.
_DEMO_TASK_TYPES = {"demo"}


async def _spawn_task(
    objective: str,
    task_type: str,
    *,
    verifier: str | None = None,
    budget_usd: float | None = None,
    deadline_minutes: int | None = None,
    max_retries: int = 2,
    adaptive: bool = False,
) -> tuple[str, str | None]:
    """Insert the task row, append TaskCreated, start the workflow.

    Delegates to the shared core path (bucker.core.tasks) so the HTTP API,
    CLI, MCP server and scheduler all create tasks identically — one code
    path, one audit trail. Returns (task_id, workflow_id).
    """
    from bucker.core.tasks import create_task

    task_id, workflow_id, schedule_error = await create_task(
        _get_store(),
        _get_pool(),
        objective=objective,
        task_type=task_type,
        verifier=verifier,
        budget_usd=budget_usd,
        deadline_minutes=deadline_minutes,
        max_retries=max_retries,
        adaptive=adaptive,
    )
    return task_id, workflow_id, schedule_error


# ------------------------------------------------------------ POST /tasks ---


@app.post("/tasks")
async def create_task(
    objective: str = Query(..., min_length=8, max_length=2000,
                           description="What should the task accomplish"),
    task_type: str = Query("code_change", description="Task domain"),
    verifier: str | None = Query(
        None,
        description="Verifier (demo tasks only; the planner picks for code tasks)",
    ),
    budget_usd: float | None = Query(None, ge=0,
                                     description="Hard cost ceiling"),
    deadline_minutes: int | None = Query(None, ge=1,
                                         description="Hard time ceiling"),
    max_retries: int = Query(
        2, ge=0, le=5,
        description="Verification retries before human review",
    ),
    adaptive: bool = Query(
        False,
        description="M3: vary retry strategy (switch model / chunk / clarify)",
    ),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    _check_auth(creds, write=True)

    task_id, workflow_id, schedule_error = await _spawn_task(
        objective,
        task_type,
        verifier=verifier,
        budget_usd=budget_usd,
        deadline_minutes=deadline_minutes,
        max_retries=max_retries,
        adaptive=adaptive,
    )

    return {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "scheduled": workflow_id is not None,
        "schedule_error": schedule_error,
        "status": "pending" if workflow_id else "schedule_failed",
        "objective": objective,
        "task_type": task_type,
    }


# ------------------------------------------------------------ GET /tasks ---

_TASK_LIST_SQL = """
SELECT t.id, t.task_type, t.objective, t.status, t.created_at,
       COALESCE(SUM(tm.cost_usd), 0) AS cost_usd,
       COALESCE(SUM(tm.total_tokens), 0) AS total_tokens,
       (SELECT COUNT(*) FROM events e WHERE e.task_id = t.id) AS event_count
FROM tasks t
LEFT JOIN telemetry tm ON tm.task_id = t.id
"""


@app.get("/tasks")
async def list_tasks(
    limit: int = Query(50, ge=1, le=500),
    status: str | None = Query(None, description="Filter by task status"),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """List tasks with per-task cost and event counts, newest first."""
    _check_auth(creds)

    sql = _TASK_LIST_SQL + ("WHERE t.status = $1 " if status else "")
    sql += "GROUP BY t.id ORDER BY t.created_at DESC LIMIT " + ("$2" if status else "$1")
    args: list = [status] + [limit] if status else [limit]

    async with _get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *args)

    total = await _get_pool().fetchval(
        "SELECT COUNT(*) FROM tasks" + (" WHERE status = $1" if status else ""),
        *([status] if status else []),
    )

    return {
        "total": int(total or 0),
        "tasks": [
            {
                "task_id": str(r["id"]),
                "task_type": r["task_type"],
                "objective": r["objective"],
                "status": r["status"],
                "cost_usd": float(r["cost_usd"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "event_count": int(r["event_count"] or 0),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


# --------------------------------------------------------- GET /tasks/{id} ---


# NOTE: /tasks/new must be registered BEFORE /tasks/{task_id} — FastAPI
# matches routes in definition order, and "{task_id}" would swallow "new"
# and fail UUID validation.


@app.get("/tasks/new", response_class=HTMLResponse)
async def new_task_page(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    _check_auth(creds)
    from bucker.templates import list_templates

    return render_new_task_page(templates=list_templates())


@app.get("/tasks/{task_id}")
async def get_task(
    task_id: UUID,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    _check_auth(creds)

    store = _get_store()
    events = await store.read_stream(task_id)
    if not events:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="task not found")

    snaps = _get_snaps()
    state = await snaps.get_state(task_id)

    return {
        "task_id": str(task_id),
        "status": state.get("status", "unknown"),
        "objective": state.get("objective"),
        "task_type": state.get("task_type"),
        "verifier": state.get("verifier"),
        "cost_usd": state.get("cost_usd", 0),
        "attempts": state.get("attempts", 0),
        "steps_completed": state.get("steps_completed", []),
        "event_count": len(events),
        "halted_reason": state.get("halted_reason"),
    }


# ------------------------------------------------ GET /tasks/{id}/events ----


@app.get("/tasks/{task_id}/events")
async def get_events(
    task_id: UUID,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    limit: int = Query(100, ge=1, le=1000),
    after: int = Query(0, ge=0),
) -> list[dict]:
    _check_auth(creds)

    store = _get_store()
    events = await store.read_stream(task_id, after_id=after, limit=limit)

    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "payload": e.payload,
            "created_at": e.created_at.isoformat(),
            "tool_output_ref": e.tool_output_ref,
        }
        for e in events
    ]


# ---------------------------------------------- POST /tasks/{id}/replay ----


@app.post("/tasks/{task_id}/replay")
async def trigger_replay(
    task_id: UUID,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    _check_auth(creds)

    store = _get_store()
    blobs = _get_blobs()

    try:
        result: ReplayResult = await replay_task(
            task_id,
            store=store,
            blobs=blobs,
        )
    except ReplayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "task_id": str(result.task_id),
        "match": result.match,
        "original_passed": result.original_passed,
        "replayed_passed": result.replayed_passed,
        "original_events": result.original_events,
        "diagnostics": result.diagnostics,
        "details": result.details,
    }


# -------------------------------------------- POST /tasks/{id}/rerun ------


@app.post("/tasks/{task_id}/rerun")
async def rerun_task(
    task_id: UUID,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Re-run a finished task: a brand-new task with the same objective.

    Honest by construction: the original event stream is append-only and
    never mutated; a re-run is a new task that happens to share the
    objective, type and budget. The workflow decides everything else again.
    """
    _check_auth(creds, write=True)

    async with _get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT task_type, objective, budget_usd FROM tasks WHERE id = $1",
            task_id,
        )
    if row is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="task not found")

    new_id, workflow_id, schedule_error = await _spawn_task(
        row["objective"],
        row["task_type"],
        budget_usd=row["budget_usd"],
    )

    return {
        "task_id": new_id,
        "original_task_id": str(task_id),
        "workflow_id": workflow_id,
        "scheduled": workflow_id is not None,
        "schedule_error": schedule_error,
        "status": "pending" if workflow_id else "schedule_failed",
    }


# -------------------------------------------- POST /tasks/{id}/cancel -----


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: UUID,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Cancel a running task by terminating its Temporal workflow.

    Requires Temporal to be reachable. There is deliberately no DB mutation
    here — the workflow's termination is the source of truth, and the event
    stream stays append-only.
    """
    _check_auth(creds, write=True)

    try:
        from temporalio.client import Client

        client = await Client.connect(
            settings.temporal_host, namespace=settings.temporal_namespace
        )
        handle = client.get_workflow_handle(f"task-{task_id}")
        await asyncio.wait_for(
            handle.terminate(reason="cancelled from the control dashboard"),
            timeout=10,
        )
        return {"task_id": str(task_id), "cancelled": True}
    except Exception as exc:
        # temporalio has no WorkflowNotFoundError; a missing workflow is an
        # RPCError whose status is NOT_FOUND (gRPC code 5).
        from temporalio.service import RPCError

        not_found = (
            isinstance(exc, RPCError)
            and getattr(getattr(exc, "status", None), "name", "") == "NOT_FOUND"
        )
        if not_found:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail="no running workflow for this task (already finished, or never started)",
            ) from None
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail=f"could not cancel: {type(exc).__name__}: {str(exc)[:160]}",
        ) from exc


# ------------------------------------------------------------ /system ------


async def _probe_http(url: str, timeout: float = 3.0) -> dict:
    """Tiny HTTP probe for local services (Ollama). Never leaks response bodies.

    Pure asyncio on purpose: urllib in a thread + wait_for cancellation leaves
    the socket thread lingering (IPv6 localhost attempts on Windows hang the
    process at asyncio.run shutdown), and the probe must never do that.
    """
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, parsed.port or 80),
            timeout=timeout,
        )
        try:
            request = (
                f"GET {parsed.path or '/'} HTTP/1.1\r\n"
                f"Host: {parsed.hostname}\r\nConnection: close\r\n\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            return {"ok": status_line.startswith(b"HTTP/1.1 2")}
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}


async def _docker_image_exists(image: str) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)
        return {"ok": proc.returncode == 0, "detail": image}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}


async def _system_status() -> dict:
    """Health of everything the platform depends on. All probes timeout."""
    status: dict = {}

    # --- model chain -----------------------------------------------------
    status["model"] = {
        "primary": settings.model,
        "fallbacks": list(settings.model_fallbacks),
        "mode": settings.model_mode,
        "max_tokens_planner": settings.max_tokens_planner,
        "max_tokens_worker": settings.max_tokens_worker,
    }

    # --- providers (only probe what is actually configured) --------------
    providers: dict = {}
    chain = [settings.model, *settings.model_fallbacks]
    for model in chain:
        if model.startswith("ollama/") and "ollama" not in providers:
            # 127.0.0.1 explicitly: "localhost" can resolve to ::1 on Windows,
            # where Ollama does not listen, and a v6 connect that hangs is a
            # worse failure than a v4 refusal.
            providers["ollama"] = await _probe_http(
                "http://127.0.0.1:11434/api/tags", timeout=2
            )
            providers["ollama"]["detail"] = "127.0.0.1:11434"
        elif model.startswith("openrouter/") and "openrouter" not in providers:
            key = os.environ.get("OPENROUTER_API_KEY", "")
            shape_ok = key.startswith("sk-or-v1-") if key else None
            providers["openrouter"] = {
                "ok": bool(key),
                "detail": "key present, shape ok" if shape_ok else (
                    "key missing" if not key else "key present, unexpected shape"
                ),
                # The value itself is never exposed, by design.
            }
    status["providers"] = providers

    # --- degraded flag: startup pool creation failed ---------------------
    status["degraded"] = _degraded

    # --- infrastructure --------------------------------------------------
    infra: dict = {}
    pool = _pool
    if pool is None:
        infra["postgres"] = {"ok": False, "detail": "pool not initialised"}
    else:
        try:
            async with pool.acquire() as conn:
                one = await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=3)
            infra["postgres"] = {"ok": one == 1}
        except Exception as exc:
            infra["postgres"] = {
                "ok": False,
                "detail": f"{type(exc).__name__}: {str(exc)[:120]}",
            }

    try:
        from bucker.sandbox.runtime import docker_available

        docker_ok = await asyncio.wait_for(docker_available(), timeout=10)
        infra["docker"] = {"ok": docker_ok}
        if docker_ok:
            infra["sandbox_image"] = await _docker_image_exists(settings.sandbox_image)
        else:
            infra["sandbox_image"] = {"ok": False, "detail": "docker unavailable"}
    except Exception as exc:
        infra["docker"] = {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}

    try:
        from temporalio.client import Client

        await asyncio.wait_for(
            Client.connect(settings.temporal_host, namespace=settings.temporal_namespace),
            timeout=3,
        )
        infra["temporal"] = {"ok": True, "detail": settings.temporal_host}
    except Exception as exc:
        infra["temporal"] = {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}
    status["infra"] = infra

    # --- platform --------------------------------------------------------
    from bucker.verifiers import base as verifier_base

    recordings = RecordingStore(Path(settings.blob_root).parent / "recordings")
    status["platform"] = {
        "verifiers": list(verifier_base.available()),
        "recordings": recordings.count(),
        "tasks": None,
    }
    try:
        async with _get_pool().acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM tasks")
        status["platform"]["tasks"] = int(n or 0)
    except Exception:
        status["platform"]["tasks"] = None

    return status


# --------------------------------------------------------------- templates --


@app.get("/templates")
async def list_templates(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Task templates: named presets for the new-task form and schedules."""
    _check_auth(creds)
    from bucker.templates import list_templates as _list

    return {"templates": _list()}


# -------------------------------------------------------------- schedules --


@app.post("/schedules")
async def create_schedule_endpoint(
    schedule_id: str = Query(..., min_length=3, max_length=64,
                             description="Stable identifier, e.g. 'nightly-bench'"),
    cron: str = Query(..., min_length=5, description="5-field cron, e.g. '0 9 * * 1-5'"),
    template: str = Query(..., description="Task template id"),
    objective: str = Query("", max_length=2000,
                           description="Override the template's objective"),
    budget_usd: float | None = Query(None, ge=0),
    deadline_minutes: int | None = Query(None, ge=1),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Create (or update) a recurring verified task on a cron schedule."""
    _check_auth(creds, write=True)
    from bucker.core.schedules import ScheduleSpec, create_schedule
    from bucker.templates import UnknownTemplateError

    try:
        return await create_schedule(ScheduleSpec(
            schedule_id=schedule_id,
            cron=cron,
            template=template,
            objective=objective,
            budget_usd=budget_usd,
            deadline_minutes=deadline_minutes,
        ))
    except UnknownTemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — Temporal errors are user-facing here
        raise HTTPException(
            status_code=409,
            detail=f"could not create schedule: {type(exc).__name__}: {str(exc)[:160]}",
        ) from exc


@app.get("/schedules")
async def list_schedules_endpoint(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """All schedules (from Temporal — the durable source of truth)."""
    _check_auth(creds)
    from bucker.core.schedules import list_schedules

    try:
        schedules = await list_schedules()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"Temporal unreachable: {type(exc).__name__}: {str(exc)[:120]}",
        ) from exc
    return {"schedules": schedules}


@app.delete("/schedules/{schedule_id}")
async def delete_schedule_endpoint(
    schedule_id: str,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Delete a schedule. 404 when it did not exist."""
    _check_auth(creds, write=True)
    from bucker.core.schedules import delete_schedule

    try:
        deleted = await delete_schedule(schedule_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"Temporal unreachable: {type(exc).__name__}: {str(exc)[:120]}",
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"schedule_id": schedule_id, "deleted": True}


# -------------------------------------------------------------- schedules page --


@app.get("/schedules-page", response_class=HTMLResponse)
async def schedules_page(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """HTML page for managing schedules (rendered by the dashboard)."""
    _check_auth(creds)
    from bucker.core.schedules import list_schedules
    from bucker.templates import list_templates

    try:
        schedules = await list_schedules()
        temporal_ok = True
    except Exception:  # noqa: BLE001
        schedules, temporal_ok = [], False

    from bucker.api.dashboard import render_schedules_page

    return render_schedules_page(
        schedules,
        templates=list_templates(),
        temporal_ok=temporal_ok,
    )


@app.get("/api/models")
async def models_json(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Model catalog + configured chain + live provider status."""
    _check_auth(creds)
    from bucker.models import CATALOG
    from bucker.providers import parse_model_chain, provider_status

    provider_info = await provider_status()
    return {
        "catalog": [
            {
                "id": m.id,
                "provider": m.provider,
                "tier": m.tier,
                "name": m.name,
                "context": m.context,
                "notes": m.notes,
            }
            for m in CATALOG
        ],
        "configured_chain": parse_model_chain(
            settings.model, settings.model_fallbacks
        ),
        "providers": provider_info["providers"],
        "suggested_chain": provider_info["suggested_chain"],
    }


@app.get("/models-page", response_class=HTMLResponse)
async def models_page(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """HTML page: browse models, see tiers and what is configured."""
    _check_auth(creds)
    from bucker.api.dashboard import render_models_page
    from bucker.models import CATALOG
    from bucker.providers import parse_model_chain, provider_status

    provider_info = await provider_status()

    return render_models_page(
        catalog=list(CATALOG),
        configured_chain=parse_model_chain(
            settings.model, settings.model_fallbacks
        ),
        providers=provider_info["providers"],
        suggested_chain=provider_info["suggested_chain"],
    )


# ------------------------------------------------------------------ skills --


@app.get("/skills")
async def list_skills(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Procedural memory: skills the worker can apply."""
    _check_auth(creds)
    from bucker.memory.skills import SkillStore

    return {"skills": [s.as_dict() for s in SkillStore().list()]}


@app.post("/skills")
async def create_skill(
    name: str = Query(..., min_length=3, max_length=64),
    description: str = Query(..., min_length=3, max_length=200),
    procedure: str = Query(..., min_length=3, max_length=2000),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Create a skill (procedural memory)."""
    _check_auth(creds, write=True)
    if not settings.enable_memory_api:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail="memory/skills API is disabled (BUCKER_ENABLE_MEMORY_API=0)",
        )
    from bucker.memory.skills import SkillStore

    try:
        skill = SkillStore().add(name, description, procedure.replace("\\n", "\n"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"skill": skill.as_dict(), "created": True}


@app.get("/skills/{name}")
async def get_skill(
    name: str,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    _check_auth(creds)
    from bucker.memory.skills import SkillStore

    skill = SkillStore().get(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"no skill named {name!r}")
    return {"skill": skill.as_dict()}


# ------------------------------------------------------------------ memory --


@app.get("/memory")
async def list_facts(
    q: str | None = Query(None, description="keyword search"),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Semantic memory: durable facts across sessions."""
    _check_auth(creds)
    from bucker.memory.facts import MemoryStore

    store = MemoryStore()
    facts = store.search(q) if q else store.list()
    return {"facts": facts, "count": len(facts)}


@app.post("/memory")
async def add_fact(
    text: str = Query(..., min_length=1, max_length=500),
    source: str = Query("user", max_length=100),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    _check_auth(creds, write=True)
    if not settings.enable_memory_api:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail="memory/skills API is disabled (BUCKER_ENABLE_MEMORY_API=0)",
        )
    from bucker.memory.facts import MemoryStore

    try:
        fact_id = MemoryStore().add(text, source=source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"fact_id": fact_id, "stored": True}


# --------------------------------------------------------------- trajectory --


@app.get("/tasks/{task_id}/trajectory")
async def task_trajectory(
    task_id: UUID,
    format: str = Query("json", pattern="^(json|md|jsonl)$"),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
):
    """The full trace of one run: model calls, tools, verdicts, in order."""
    _check_auth(creds)
    from bucker.core.trajectory import (
        export_trajectory,
        trajectory_to_jsonl,
        trajectory_to_markdown,
    )

    trajectory = await export_trajectory(task_id, _get_store())
    if not trajectory["events"]:
        raise HTTPException(status_code=404, detail="task not found or has no events")

    if format == "md":
        return Response(
            content=trajectory_to_markdown(trajectory),
            media_type="text/markdown",
        )
    if format == "jsonl":
        return Response(
            content=trajectory_to_jsonl(trajectory),
            media_type="application/x-ndjson",
        )
    return trajectory


@app.get("/memory-page", response_class=HTMLResponse)
async def memory_page(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Semantic memory page."""
    _check_auth(creds)
    from bucker.api.dashboard import render_memory_page
    from bucker.memory.facts import MemoryStore

    return render_memory_page(MemoryStore().list())


@app.get("/skills-page", response_class=HTMLResponse)
async def skills_page(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Procedural memory page."""
    _check_auth(creds)
    from bucker.api.dashboard import render_skills_page
    from bucker.memory.skills import SkillStore

    return render_skills_page([s.as_dict() for s in SkillStore().list()])


# ------------------------------------------------------------------- graphs --


@app.post("/graphs")
async def create_graph(
    spec: dict = Body(..., description="graph spec: name + steps with "
                                       "depends_on/objective/budget"),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Launch a multi-step task DAG (graph engineering).

    Each step is a full verified pipeline; independent steps run in
    parallel waves (Temporal child workflows).
    """
    _check_auth(creds, write=True)
    from bucker.contracts.graph import parse_spec, topological_waves, validate_graph

    try:
        parsed = parse_spec(spec)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid graph spec: {exc}") from exc
    errors = validate_graph(parsed)
    if errors:
        raise HTTPException(
            status_code=400, detail="graph is not runnable: " + "; ".join(errors)
        )

    if _degraded:
        raise HTTPException(status_code=503, detail="database unavailable")

    from bucker.core.tasks import create_task

    store = _get_store()
    task_id, workflow_id, schedule_error = await create_task(
        store,
        store._pool,
        objective=f"graph: {parsed.name} ({len(parsed.steps)} steps)",
        verifier="noop",
        graph_spec=spec,
    )
    if workflow_id is None:
        raise HTTPException(
            status_code=503,
            detail="Temporal unreachable — graph registered but not "
                   f"scheduled ({schedule_error})",
        )
    return {
        "task_id": task_id,
        "graph": parsed.as_dict(),
        "waves": len(topological_waves(parsed)),
    }


# ------------------------------------------------------- human review ----


@app.post("/tasks/{task_id}/approve")
async def approve_task(
    task_id: UUID,
    note: str = Query("", max_length=500),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Human approves an escalated (needs_human_review) task.

    The machine's verifier never passed, so the human is the judge. The
    verdict is append-only; the task becomes 'human_approved'.
    """
    _check_auth(creds, write=True)
    if _degraded:
        raise HTTPException(status_code=503, detail="database unavailable")
    from bucker.core.tasks import review_task

    try:
        return await review_task(_get_store(), task_id, approved=True, note=note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/tasks/{task_id}/reject")
async def reject_task(
    task_id: UUID,
    note: str = Query("", max_length=500),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Human rejects an escalated task (append-only, terminal)."""
    _check_auth(creds, write=True)
    if _degraded:
        raise HTTPException(status_code=503, detail="database unavailable")
    from bucker.core.tasks import review_task

    try:
        return await review_task(_get_store(), task_id, approved=False, note=note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/system")
async def system_status_json(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    _check_auth(creds)
    return await _system_status()


@app.get("/system", response_class=HTMLResponse)
async def system_page(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    _check_auth(creds)
    return render_system_page(await _system_status())


# ------------------------------------------------------------ /usage ------


async def _usage_stats() -> dict:
    """Token + cost usage: totals, by model, by purpose, per day.

    Answers the two questions operators actually ask: which API am I using,
    and how many tokens has it burned.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        total_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_usd), 0) AS cost,
                   COUNT(*) AS calls
            FROM telemetry WHERE model_used IS NOT NULL
            """
        )
        week_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_usd), 0) AS cost
            FROM telemetry
            WHERE model_used IS NOT NULL
              AND created_at > NOW() - INTERVAL '7 days'
            """
        )
        model_rows = await conn.fetch(
            """
            SELECT model_used AS model,
                   COUNT(*) AS calls,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(cost_usd), 0) AS cost
            FROM telemetry WHERE model_used IS NOT NULL
            GROUP BY model_used ORDER BY tokens DESC
            """
        )
        purpose_rows = await conn.fetch(
            """
            SELECT purpose,
                   COUNT(*) AS calls,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_usd), 0) AS cost
            FROM telemetry WHERE purpose IS NOT NULL
            GROUP BY purpose ORDER BY tokens DESC
            """
        )
        day_rows = await conn.fetch(
            """
            SELECT DATE(created_at) AS d,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_usd), 0) AS cost
            FROM telemetry
            WHERE model_used IS NOT NULL
              AND created_at > NOW() - INTERVAL '7 days'
            GROUP BY d ORDER BY d
            """
        )
        today_rows = await conn.fetch(
            """
            SELECT model_used AS model, COUNT(*) AS calls
            FROM telemetry
            WHERE model_used IS NOT NULL
              AND created_at >= CURRENT_DATE
            GROUP BY model_used
            """
        )

    by_model = [
        {
            "model": r["model"],
            "calls": int(r["calls"]),
            "tokens": int(r["tokens"]),
            "prompt_tokens": int(r["prompt_tokens"]),
            "completion_tokens": int(r["completion_tokens"]),
            "cost_usd": float(r["cost"]),
        }
        for r in model_rows
    ]
    max_tokens = max((m["tokens"] for m in by_model), default=0)
    for m in by_model:
        m["pct"] = (m["tokens"] / max_tokens * 100) if max_tokens else 0

    by_purpose = [
        {
            "purpose": r["purpose"] or "?",
            "calls": int(r["calls"]),
            "tokens": int(r["tokens"]),
            "cost_usd": float(r["cost"]),
        }
        for r in purpose_rows
    ]

    per_day = [
        {
            "day": r["d"].isoformat(),
            "tokens": int(r["tokens"]),
            "cost_usd": float(r["cost"]),
        }
        for r in day_rows
    ]
    max_day = max((d["tokens"] for d in per_day), default=0)
    for d in per_day:
        d["pct"] = (d["tokens"] / max_day * 100) if max_day else 0

    from bucker.models import free_tier_rows

    free_tier = free_tier_rows(
        {r["model"]: int(r["calls"]) for r in today_rows}
    )
    total_free_calls_today = sum(r["calls_today"] for r in free_tier)

    return {
        "total_tokens": int(total_row["tokens"]),
        "total_cost": float(total_row["cost"]),
        "total_calls": int(total_row["calls"]),
        "week_tokens": int(week_row["tokens"]),
        "week_cost": float(week_row["cost"]),
        "by_model": by_model,
        "by_purpose": by_purpose,
        "per_day": per_day,
        "free_tier": free_tier,
        "free_calls_today": total_free_calls_today,
    }


@app.get("/api/usage")
async def usage_json(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    _check_auth(creds)
    return await _usage_stats()


@app.get("/usage", response_class=HTMLResponse)
async def usage_page(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    _check_auth(creds)
    return render_usage_page(await _usage_stats())


# ---------------------------------------------------------------- pages ----

_AGGREGATES_SQL = {
    "by_status": "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status",
    "total_cost": "SELECT COALESCE(SUM(cost_usd), 0) FROM telemetry",
    "cost_by_type": """
        SELECT t.task_type AS task_type, SUM(tm.cost_usd) AS total
        FROM tasks t JOIN telemetry tm ON tm.task_id = t.id
        GROUP BY t.task_type ORDER BY total DESC
    """,
    "per_day": """
        SELECT DATE(created_at) AS d, COUNT(*) AS n
        FROM tasks WHERE created_at > NOW() - INTERVAL '7 days'
        GROUP BY d ORDER BY d
    """,
}


async def _index_stats() -> dict:
    """Aggregate numbers for the landing dashboard. One query per concern."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        status_rows = await conn.fetch(_AGGREGATES_SQL["by_status"])
        total_cost = float((await conn.fetchval(_AGGREGATES_SQL["total_cost"])) or 0)
        type_rows = await conn.fetch(_AGGREGATES_SQL["cost_by_type"])
        day_rows = await conn.fetch(_AGGREGATES_SQL["per_day"])
        total_tokens = int((
            await conn.fetchval(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM telemetry "
                "WHERE model_used IS NOT NULL"
            )
        ) or 0)

    by_status = {r["status"]: int(r["n"]) for r in status_rows}
    total = sum(by_status.values())
    completed = by_status.get("completed", 0)

    cost_by_type = [
        (r["task_type"], float(r["total"] or 0)) for r in type_rows
    ]
    max_cost = max((c for _, c in cost_by_type), default=0.0)
    cost_by_type = [(k, v, (v / max_cost * 100) if max_cost else 0) for k, v in cost_by_type]

    per_day = [
        (r["d"].isoformat(), int(r["n"])) for r in day_rows
    ]
    max_day = max((n for _, n in per_day), default=0)
    per_day = [(d, n, (n / max_day * 100) if max_day else 0) for d, n in per_day]

    return {
        "total": total,
        "by_status": by_status,
        "success_rate": (completed / total) if total else 0.0,
        "total_cost": total_cost,
        "total_tokens": total_tokens,
        "avg_cost": (total_cost / total) if total else 0.0,
        "cost_by_type": cost_by_type,
        "per_day": per_day,
    }


@app.get("/", response_class=HTMLResponse)
async def index_page(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    _check_auth(creds)

    stats = await _index_stats()

    async with _get_pool().acquire() as conn:
        rows = await conn.fetch(
            _TASK_LIST_SQL + "GROUP BY t.id ORDER BY t.created_at DESC LIMIT 20"
        )
    tasks = [
        {
            "id": str(r["id"]),
            "task_type": r["task_type"],
            "objective": r["objective"],
            "status": r["status"],
            "cost_usd": float(r["cost_usd"] or 0),
            "total_tokens": int(r["total_tokens"] or 0),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return render_index(stats, tasks)


@app.get("/tasks/{task_id}/dashboard", response_class=HTMLResponse)
async def task_dashboard(
    task_id: UUID,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Single-page HTML dashboard for one task.

    Shows what happened, why, how long, how much. No React, no JavaScript
    framework — one server-rendered page that answers the debugging question.
    """
    _check_auth(creds)

    store = _get_store()
    events = await store.read_stream(task_id)
    if not events:
        raise HTTPException(status_code=404, detail="task not found")

    snaps = _get_snaps()
    state = await snaps.get_state(task_id)

    # The debugging value is in the failure detail: fetch the FULL verifier
    # diagnostics blob (the state only carries a truncated copy) so the page
    # can show exactly what the retry would feed back.
    verifier_output = ""
    failed_model_calls: list[str] = []
    for e in events:
        if (
            e.event_type in (EventType.VERIFICATION_PASSED, EventType.VERIFICATION_FAILED)
            and e.tool_output_ref
        ):
            try:
                verifier_output = _get_blobs().get(e.tool_output_ref).decode("utf-8")
            except Exception:
                verifier_output = ""
        if e.event_type == EventType.MODEL_CALL_FAILED:
            err = str(e.payload.get("error", ""))[:200]
            if err:
                failed_model_calls.append(err)

    event_dicts = [
        {
            "id": e.id,
            "event_type": e.event_type,
            "payload": e.payload,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "tool_output_ref": e.tool_output_ref,
        }
        for e in events
    ]
    return render_task_dashboard(
        str(task_id), state, event_dicts,
        verifier_output=verifier_output,
        failed_model_calls=failed_model_calls,
    )


@app.get("/tasks/{task_id}/replay", response_class=HTMLResponse)
async def replay_page(
    task_id: UUID,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    _check_auth(creds)
    return render_replay_page(str(task_id))
