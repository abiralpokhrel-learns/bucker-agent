"""Python SDK: drive bucker from code instead of curl.

One class per event loop model — ``BuckerClient`` (sync) and
``AsyncBuckerClient`` — sharing one surface so scripts, notebooks, MCP
servers, and test suites talk to the same API the dashboard does:

    from bucker.client import BuckerClient

    bucker = BuckerClient(base_url="http://localhost:8123")
    task = bucker.create_task("Add a subtract function to calc.py",
                              budget_usd=0.25)
    result = bucker.wait_for_task(task["task_id"], timeout_s=900)
    print(result["status"], result["cost_usd"])

Errors are typed exceptions, never bare dicts: AuthenticationError,
NotFoundError, ConflictError, ValidationError, ServerError — each carries
the HTTP status and the server's detail string. ``wait_for_task`` polls
``get_task`` until a terminal status and raises WaitTimeoutError rather
than hanging forever; it works identically against the full stack and
lite mode because both serve the same routes.

The token defaults to the configured ``BUCKER_API_TOKEN`` (dev-token on a
laptop). Sending Authorization headers to a dev-token server is harmless;
omitting them on a real deployment is not.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from bucker.core.watch import TERMINAL_STATUSES

#: A schedule-failed task will never run; callers waiting on it must wake.
#: Everything else mirrors the pipeline's own vocabulary (bucker.core.watch).
CLIENT_TERMINAL_STATUSES = frozenset(TERMINAL_STATUSES) | {"schedule_failed"}

DEFAULT_BASE_URL = "http://localhost:8123"
DEFAULT_TIMEOUT_S = 30.0


class BuckerError(Exception):
    """Base class for every error this client raises."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 detail: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class AuthenticationError(BuckerError):
    """401/403 — missing, wrong, or insufficient token."""


class NotFoundError(BuckerError):
    """404 — unknown task, schedule, or route."""


class ConflictError(BuckerError):
    """409 — e.g. cancelling a workflow that already finished."""


class ValidationError(BuckerError):
    """400/422 — the server rejected the request's shape or values."""


class ServerError(BuckerError):
    """5xx — bucker itself failed; safe to retry with backoff."""


class WaitTimeoutError(BuckerError):
    """wait_for_task gave up before the task reached a terminal status."""


def _error_for(response: httpx.Response) -> BuckerError:
    """Map an HTTP error response to its typed exception."""
    try:
        body = response.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
    except ValueError:
        detail = response.text[:300]
    message = f"HTTP {response.status_code}: {str(detail)[:300]}"
    exc_class: dict[int, type[BuckerError]] = {
        400: ValidationError,
        401: AuthenticationError,
        403: AuthenticationError,
        404: NotFoundError,
        409: ConflictError,
        422: ValidationError,
    }
    if response.status_code in exc_class:
        return exc_class[response.status_code](
            message, status_code=response.status_code, detail=detail
        )
    return ServerError(message, status_code=response.status_code, detail=detail)


def _check(response: httpx.Response) -> httpx.Response:
    if response.is_error:
        raise _error_for(response)
    return response


class _Base:
    """Shared configuration and request shaping for both clients."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        if token is None:
            from bucker.config import settings

            token = settings.api_token
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    @staticmethod
    def _create_params(
        objective: str,
        *,
        task_type: str,
        verifier: str | None,
        budget_usd: float | None,
        deadline_minutes: int | None,
        max_retries: int,
        adaptive: bool,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "objective": objective,
            "task_type": task_type,
            "max_retries": max_retries,
            "adaptive": str(adaptive).lower(),
        }
        if verifier is not None:
            params["verifier"] = verifier
        if budget_usd is not None:
            params["budget_usd"] = budget_usd
        if deadline_minutes is not None:
            params["deadline_minutes"] = deadline_minutes
        return params


class AsyncBuckerClient(_Base):
    """Async SDK surface. Safe to share across asyncio tasks."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(base_url, **kwargs)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
            transport=transport,
        )

    async def __aenter__(self) -> AsyncBuckerClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        response = await self._client.get(path, params=params)
        return _check(response).json()

    async def _post(self, path: str, params: dict | None = None) -> Any:
        response = await self._client.post(path, params=params)
        return _check(response).json()

    # ------------------------------------------------------------- reads ----

    async def system(self) -> dict:
        """Platform health: model chain, providers, infra, verifiers."""
        return await self._get("/api/system")

    async def usage(self) -> dict:
        """Token + cost totals, by model, by pipeline stage, per day."""
        return await self._get("/api/usage")

    async def templates(self) -> list[dict]:
        response = await self._get("/templates")
        return response.get("templates", [])

    async def get_task(self, task_id: str) -> dict:
        return await self._get(f"/tasks/{task_id}")

    async def list_tasks(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return await self._get("/tasks", params=params)

    async def events(
        self, task_id: str, *, after: int = 0, limit: int = 100
    ) -> list[dict]:
        return await self._get(
            f"/tasks/{task_id}/events",
            params={"after": after, "limit": limit},
        )

    async def iter_events(
        self, task_id: str, *, after: int = 0, page_size: int = 100
    ) -> Iterator[dict]:
        """Yield every event after ``after`` id, paging until exhausted."""
        cursor = after
        while True:
            page = await self.events(task_id, after=cursor, limit=page_size)
            if not page:
                return
            for event in page:
                yield event
                cursor = max(cursor, int(event.get("id", cursor)))
            if len(page) < page_size:
                return

    async def trajectory(self, task_id: str, *, fmt: str = "json") -> Any:
        """Full trace (model calls, tools, verdicts) as json/md/jsonl."""
        if fmt == "json":
            return await self._get(f"/tasks/{task_id}/trajectory",
                                   params={"format": "json"})
        response = await self._client.get(
            f"/tasks/{task_id}/trajectory", params={"format": fmt}
        )
        _check(response)
        return response.text

    # ------------------------------------------------------------ writes ----

    async def create_task(
        self,
        objective: str,
        *,
        task_type: str = "code_change",
        verifier: str | None = None,
        budget_usd: float | None = None,
        deadline_minutes: int | None = None,
        max_retries: int = 2,
        adaptive: bool = False,
    ) -> dict:
        """Create and schedule a task. Returns {task_id, status, ...}."""
        return await self._post(
            "/tasks",
            params=self._create_params(
                objective,
                task_type=task_type,
                verifier=verifier,
                budget_usd=budget_usd,
                deadline_minutes=deadline_minutes,
                max_retries=max_retries,
                adaptive=adaptive,
            ),
        )

    async def replay(self, task_id: str) -> dict:
        """Deterministic re-run from recordings; returns match/mismatch."""
        return await self._post(f"/tasks/{task_id}/replay")

    async def rerun(self, task_id: str) -> dict:
        """New task with the same objective; the original stays untouched."""
        return await self._post(f"/tasks/{task_id}/rerun")

    async def cancel(self, task_id: str) -> dict:
        return await self._post(f"/tasks/{task_id}/cancel")

    async def approve(self, task_id: str, note: str = "") -> dict:
        return await self._post(
            f"/tasks/{task_id}/approve", params={"note": note}
        )

    async def reject(self, task_id: str, note: str = "") -> dict:
        return await self._post(
            f"/tasks/{task_id}/reject", params={"note": note}
        )

    # --------------------------------------------------------- schedules ----

    async def create_schedule(
        self,
        schedule_id: str,
        *,
        cron: str,
        template: str = "code-fix",
        objective: str = "",
        budget_usd: float | None = None,
        deadline_minutes: int | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "schedule_id": schedule_id,
            "cron": cron,
            "template": template,
            "objective": objective,
        }
        if budget_usd is not None:
            params["budget_usd"] = budget_usd
        if deadline_minutes is not None:
            params["deadline_minutes"] = deadline_minutes
        return await self._post("/schedules", params=params)

    async def list_schedules(self) -> list[dict]:
        response = await self._get("/schedules")
        return response.get("schedules", [])

    async def delete_schedule(self, schedule_id: str) -> dict:
        response = await self._client.delete(f"/schedules/{schedule_id}")
        return _check(response).json()

    async def pause_schedule(self, schedule_id: str) -> dict:
        return await self._post(f"/schedules/{schedule_id}/pause")

    async def resume_schedule(self, schedule_id: str) -> dict:
        return await self._post(
            f"/schedules/{schedule_id}/pause", params={"resume": "true"}
        )

    # -------------------------------------------------------------- wait ----

    async def wait_for_task(
        self,
        task_id: str,
        *,
        timeout_s: float = 3600.0,
        poll_interval_s: float = 2.0,
        include: set[str] | None = None,
    ) -> dict:
        """Poll until the task reaches a terminal status; return its state.

        ``include`` adds non-terminal statuses to stop on early (e.g.
        {"needs_human_review"} is already terminal; pass {"running"} to
        return as soon as execution starts). Raises WaitTimeoutError when
        the deadline passes first — never hangs forever.
        """
        import asyncio
        import time

        stop_on = CLIENT_TERMINAL_STATUSES | (include or set())
        deadline = time.monotonic() + timeout_s
        while True:
            task = await self.get_task(task_id)
            status = task.get("status")
            if status in stop_on:
                return task
            if time.monotonic() >= deadline:
                raise WaitTimeoutError(
                    f"task {task_id} still {status!r} after {timeout_s}s"
                )
            await asyncio.sleep(poll_interval_s)


class BuckerClient(_Base):
    """Sync SDK surface — same routes, same errors, no event loop needed."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        transport: httpx.BaseTransport | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(base_url, **kwargs)
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
            transport=transport,
        )

    def __enter__(self) -> BuckerClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict | None = None) -> Any:
        return _check(self._client.get(path, params=params)).json()

    def _post(self, path: str, params: dict | None = None) -> Any:
        return _check(self._client.post(path, params=params)).json()

    # ------------------------------------------------------------- reads ----

    def system(self) -> dict:
        return self._get("/api/system")

    def usage(self) -> dict:
        return self._get("/api/usage")

    def templates(self) -> list[dict]:
        return self._get("/templates").get("templates", [])

    def get_task(self, task_id: str) -> dict:
        return self._get(f"/tasks/{task_id}")

    def list_tasks(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return self._get("/tasks", params=params)

    def events(
        self, task_id: str, *, after: int = 0, limit: int = 100
    ) -> list[dict]:
        return self._get(
            f"/tasks/{task_id}/events",
            params={"after": after, "limit": limit},
        )

    def iter_events(
        self, task_id: str, *, after: int = 0, page_size: int = 100
    ) -> Iterator[dict]:
        cursor = after
        while True:
            page = self.events(task_id, after=cursor, limit=page_size)
            if not page:
                return
            for event in page:
                yield event
                cursor = max(cursor, int(event.get("id", cursor)))
            if len(page) < page_size:
                return

    def trajectory(self, task_id: str, *, fmt: str = "json") -> Any:
        if fmt == "json":
            return self._get(f"/tasks/{task_id}/trajectory",
                             params={"format": "json"})
        response = self._client.get(
            f"/tasks/{task_id}/trajectory", params={"format": fmt}
        )
        _check(response)
        return response.text

    # ------------------------------------------------------------ writes ----

    def create_task(
        self,
        objective: str,
        *,
        task_type: str = "code_change",
        verifier: str | None = None,
        budget_usd: float | None = None,
        deadline_minutes: int | None = None,
        max_retries: int = 2,
        adaptive: bool = False,
    ) -> dict:
        return self._post(
            "/tasks",
            params=self._create_params(
                objective,
                task_type=task_type,
                verifier=verifier,
                budget_usd=budget_usd,
                deadline_minutes=deadline_minutes,
                max_retries=max_retries,
                adaptive=adaptive,
            ),
        )

    def replay(self, task_id: str) -> dict:
        return self._post(f"/tasks/{task_id}/replay")

    def rerun(self, task_id: str) -> dict:
        return self._post(f"/tasks/{task_id}/rerun")

    def cancel(self, task_id: str) -> dict:
        return self._post(f"/tasks/{task_id}/cancel")

    def approve(self, task_id: str, note: str = "") -> dict:
        return self._post(f"/tasks/{task_id}/approve", params={"note": note})

    def reject(self, task_id: str, note: str = "") -> dict:
        return self._post(f"/tasks/{task_id}/reject", params={"note": note})

    # --------------------------------------------------------- schedules ----

    def create_schedule(
        self,
        schedule_id: str,
        *,
        cron: str,
        template: str = "code-fix",
        objective: str = "",
        budget_usd: float | None = None,
        deadline_minutes: int | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "schedule_id": schedule_id,
            "cron": cron,
            "template": template,
            "objective": objective,
        }
        if budget_usd is not None:
            params["budget_usd"] = budget_usd
        if deadline_minutes is not None:
            params["deadline_minutes"] = deadline_minutes
        return self._post("/schedules", params=params)

    def list_schedules(self) -> list[dict]:
        return self._get("/schedules").get("schedules", [])

    def delete_schedule(self, schedule_id: str) -> dict:
        return _check(self._client.delete(f"/schedules/{schedule_id}")).json()

    def pause_schedule(self, schedule_id: str) -> dict:
        return self._post(f"/schedules/{schedule_id}/pause")

    def resume_schedule(self, schedule_id: str) -> dict:
        return self._post(
            f"/schedules/{schedule_id}/pause", params={"resume": "true"}
        )

    # -------------------------------------------------------------- wait ----

    def wait_for_task(
        self,
        task_id: str,
        *,
        timeout_s: float = 3600.0,
        poll_interval_s: float = 2.0,
        include: set[str] | None = None,
    ) -> dict:
        import time

        stop_on = CLIENT_TERMINAL_STATUSES | (include or set())
        deadline = time.monotonic() + timeout_s
        while True:
            task = self.get_task(task_id)
            status = task.get("status")
            if status in stop_on:
                return task
            if time.monotonic() >= deadline:
                raise WaitTimeoutError(
                    f"task {task_id} still {status!r} after {timeout_s}s"
                )
            time.sleep(poll_interval_s)


__all__ = [
    "AsyncBuckerClient",
    "AuthenticationError",
    "BuckerClient",
    "BuckerError",
    "CLIENT_TERMINAL_STATUSES",
    "ConflictError",
    "NotFoundError",
    "ServerError",
    "ValidationError",
    "WaitTimeoutError",
]
