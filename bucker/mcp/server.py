"""Model Context Protocol server (bucker/mcp/server.py).

Lets ANY agent that speaks MCP — Claude Desktop, Claude Code, Hermes,
Cursor, and every client that follows the protocol — delegate tasks to
bucker-agent's verified, durable execution pipeline.

What the client gets that a plain tool call does not:

  * create_task   -> the task runs through planner -> worker -> verifier,
                     with budgets, retries and a durable audit trail
  * get_task      -> the state rebuilt from the append-only event stream
  * list_tasks    -> recent tasks with real cost/token figures
  * replay_task   -> a deterministic, FREE re-run from recordings — the
                     client can prove a result was reproducible
  * cancel_task   -> terminate a running workflow
  * system_status -> model chain + infra health before spending anything

Transport: stdio (the MCP default). Run with:

    uv run python -m bucker.mcp.server

and register it in the client as a stdio MCP server. The server talks
directly to Postgres + Temporal (same code path as the HTTP API — see
bucker/core/tasks.py), so it works with no HTTP server running.

The `mcp` package is an optional dependency: `uv sync --extra mcp`.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from bucker.config import settings
from bucker.core.eventstore import EventStore, create_pool
from bucker.core.tasks import create_task, get_task, list_tasks


def _description() -> str:
    return (
        "bucker-agent: durable, verified agent execution. Nothing is "
        "trusted until it's verified, nothing is lost when it crashes. "
        "Tasks run planner -> worker -> verifier inside network-isolated "
        "sandboxes, with budgets, retries, and an append-only audit trail."
    )


def build_server():
    """Construct the FastMCP server. Imported lazily so the module imports
    without the `mcp` extra installed (the CLI explains how to install it).
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "bucker-agent",
        instructions=_description(),
    )

    # ------------------------------------------------------------ state ----
    _pool = None
    _store = None

    async def _ensure_state():
        nonlocal _pool, _store
        if _pool is None:
            _pool = await create_pool(settings.database_url)
            _store = EventStore(_pool)
        return _pool, _store

    async def _with_client(handler):
        """Wrap a tool body with client bootstrap + teardown."""
        pool, store = await _ensure_state()
        try:
            return await handler(pool, store)
        finally:
            pass  # pool lives for the server's lifetime

    # ------------------------------------------------------------- tools ----
    @mcp.tool()
    async def create_task_tool(
        objective: str,
        task_type: str = "code_change",
        budget_usd: float | None = None,
        deadline_minutes: int | None = None,
        max_retries: int = 2,
        adaptive: bool = False,
    ) -> str:
        """Create a task and start its verified workflow.

        The task runs planner -> worker -> verifier in a network-isolated
        sandbox. Returns the task id; poll get_task for the verdict.
        """
        async def _run(pool, store):
            task_id, workflow_id = await create_task(
                store,
                pool,
                objective=objective,
                task_type=task_type,
                budget_usd=budget_usd,
                deadline_minutes=deadline_minutes,
                max_retries=max_retries,
                adaptive=adaptive,
            )
            return (
                f"task {task_id} created"
                + (f", workflow {workflow_id}" if workflow_id else
                   " (Temporal offline — will start when a worker connects)")
            )
        return await _with_client(_run)

    @mcp.tool()
    async def get_task_tool(task_id: str) -> str:
        """Get one task's state, rebuilt from its event stream."""
        async def _run(pool, store):
            task = await get_task(pool, UUID(task_id))
            if task is None:
                return f"no task {task_id}"
            events = await store.read_stream(UUID(task_id))
            return (
                f"{task['task_id']} [{task['task_type']}] {task['status']} — "
                f"${task['cost_usd']:.4f}, {task['total_tokens']} tokens, "
                f"{task['event_count']} events, {len(events)} in stream\n"
                f"objective: {task['objective'][:200]}"
            )
        return await _with_client(_run)

    @mcp.tool()
    async def list_tasks_tool(limit: int = 20) -> str:
        """List recent tasks, newest first, with cost and tokens."""
        async def _run(pool, store):
            tasks = await list_tasks(pool, limit=min(limit, 100))
            if not tasks:
                return "no tasks yet"
            lines = []
            for t in tasks:
                lines.append(
                    f"{t['task_id'][:8]} {t['status']:>16} "
                    f"${t['cost_usd']:.4f} {t['total_tokens']:>8} tok  "
                    f"{t['objective'][:60]}"
                )
            return "\n".join(lines)
        return await _with_client(_run)

    @mcp.tool()
    async def replay_task_tool(task_id: str) -> str:
        """Deterministically re-run a task from its recordings (free, no
        model calls) and report whether the replay matches the original
        verification outcome."""
        async def _run(pool, store):
            from bucker.replay.engine import ReplayError, replay_task

            try:
                result = await replay_task(
                    UUID(task_id), store=store, blobs=_blobs()
                )
            except ReplayError as exc:
                return f"replay failed: {exc}"
            return (
                f"replay {'MATCHES' if result.match else 'MISMATCHES'} original — "
                f"original={'PASSED' if result.original_passed else 'FAILED'}, "
                f"replay={'PASSED' if result.replayed_passed else 'FAILED'}\n"
                f"{result.diagnostics[:300]}"
            )
        return await _with_client(_run)

    def _blobs():
        from bucker.core.blob import BlobStore
        return BlobStore(settings.blob_root)

    @mcp.tool()
    async def cancel_task_tool(task_id: str) -> str:
        """Cancel a running task by terminating its Temporal workflow."""
        async def _run(pool, store):
            from temporalio.client import Client

            try:
                client = await Client.connect(
                    settings.temporal_host, namespace=settings.temporal_namespace
                )
                handle = client.get_workflow_handle(f"task-{task_id}")
                await asyncio.wait_for(
                    handle.terminate(reason="cancelled via MCP"),
                    timeout=10,
                )
                return f"task {task_id} cancelled"
            except Exception as exc:
                return f"could not cancel {task_id}: {type(exc).__name__}: {str(exc)[:120]}"
        return await _with_client(_run)

    @mcp.tool()
    async def system_status_tool() -> str:
        """Check infra + model chain health before spending anything."""
        async def _run(pool, store):
            try:
                async with pool.acquire() as conn:
                    await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=3)
                db = "up"
            except Exception:
                db = "DOWN"
            try:
                from temporalio.client import Client

                await asyncio.wait_for(
                    Client.connect(
                        settings.temporal_host, namespace=settings.temporal_namespace
                    ),
                    timeout=3,
                )
                temporal = "up"
            except Exception:
                temporal = "DOWN"
            chain = [settings.model, *settings.model_fallbacks]
            return (
                f"model chain: {', '.join(chain)}\n"
                f"mode: {settings.model_mode}\n"
                f"postgres: {db}\ntemporal: {temporal}"
            )
        return await _with_client(_run)

    return mcp


def main() -> None:
    """Entry point: run the stdio MCP server."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        print(
            "the MCP server needs the optional dependency:\n"
            "    uv sync --extra mcp\n"
            "then run: uv run python -m bucker.mcp.server",
            file=__import__("sys").stderr,
        )
        raise SystemExit(2) from None

    server = build_server()
    server.run()  # stdio transport


if __name__ == "__main__":
    main()
