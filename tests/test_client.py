"""Python SDK client tests (bucker.client).

Fully hermetic: httpx.MockTransport plays the server. The tests pin the
wire contract the SDK promises — routes, query parameters, auth header,
typed error mapping, pagination-following iteration, and wait_for_task's
timeout behavior.
"""

from __future__ import annotations

import httpx
import pytest

from bucker.client import (
    AsyncBuckerClient,
    AuthenticationError,
    BuckerClient,
    ConflictError,
    NotFoundError,
    ServerError,
    ValidationError,
    WaitTimeoutError,
)


def _route(method: str, path: str, handler):
    """A MockTransport handler that dispatches (method, path)."""
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method != method or request.url.path != path:
            return httpx.Response(404, json={"detail": f"unrouted "
                                                     f"{request.method} {request.url.path}"})
        return handler(request)
    return handle


TASK_BODY = {
    "task_id": "11111111-2222-3333-4444-555555555555",
    "status": "pending",
    "objective": "Add a subtract function to calc.py",
}


# ---------------------------------------------------------------- sync ----


def test_create_task_sends_expected_query_and_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={**TASK_BODY, "task_id": "t-1"})

    transport = httpx.MockTransport(_route("POST", "/tasks", handler))
    with BuckerClient("http://test", token="secret-token",
                      transport=transport) as c:
        result = c.create_task(
            "Add a subtract function to calc.py",
            budget_usd=0.25, deadline_minutes=10, adaptive=True,
        )
    assert result["task_id"] == "t-1"
    assert seen["auth"] == "Bearer secret-token"
    assert seen["params"]["objective"] == "Add a subtract function to calc.py"
    assert seen["params"]["budget_usd"] == "0.25"
    assert seen["params"]["deadline_minutes"] == "10"
    assert seen["params"]["adaptive"] == "true"
    assert seen["params"]["max_retries"] == "2"


def test_error_mapping_by_status():
    cases = [
        (400, ValidationError), (401, AuthenticationError),
        (403, AuthenticationError), (404, NotFoundError),
        (409, ConflictError), (422, ValidationError),
        (500, ServerError), (503, ServerError),
    ]
    for status, exc_class in cases:
        def handler(request: httpx.Request, status=status) -> httpx.Response:
            return httpx.Response(status, json={"detail": "boom"})

        transport = httpx.MockTransport(_route("GET", "/tasks/t-1", handler))
        client = BuckerClient("http://test", token="x", transport=transport)
        with client, pytest.raises(exc_class) as info:
            client.get_task("t-1")
        assert info.value.status_code == status
        assert "boom" in str(info.value)


def test_list_tasks_pagination_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"total": 0, "limit": 5,
                                         "offset": 10, "tasks": []})

    transport = httpx.MockTransport(_route("GET", "/tasks", handler))
    with BuckerClient("http://test", token="x", transport=transport) as c:
        page = c.list_tasks(limit=5, offset=10)
    assert page == {"total": 0, "limit": 5, "offset": 10, "tasks": []}
    assert seen["params"]["offset"] == "10"


def test_iter_events_follows_pages_until_short_page():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        after = int(request.url.params.get("after", "0"))
        calls.append(after)
        # First call: a full page. Second: a short page, which ends it.
        events = (
            [{"id": i} for i in range(1, 101)] if len(calls) == 1
            else [{"id": 101}]
        )
        return httpx.Response(200, json=events)

    transport = httpx.MockTransport(
        _route("GET", "/tasks/t-1/events", handler)
    )
    with BuckerClient("http://test", token="x", transport=transport) as c:
        ids = [e["id"] for e in c.iter_events("t-1", page_size=100)]
    assert ids[0] == 1 and ids[-1] == 101
    assert len(ids) == 101
    assert calls == [0, 100]


def test_wait_for_task_returns_on_terminal_status():
    states = iter([
        {"status": "pending"},
        {"status": "in_progress"},
        {"status": "completed", "cost_usd": 0.02},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(states))

    transport = httpx.MockTransport(_route("GET", "/tasks/t-9", handler))
    with BuckerClient("http://test", token="x", transport=transport) as c:
        final = c.wait_for_task("t-9", poll_interval_s=0, timeout_s=5)
    assert final["status"] == "completed"


def test_wait_for_task_raises_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "running"})

    transport = httpx.MockTransport(_route("GET", "/tasks/t-9", handler))
    client = BuckerClient("http://test", token="x", transport=transport)
    with client, pytest.raises(WaitTimeoutError):
        client.wait_for_task("t-9", poll_interval_s=0, timeout_s=0.05)


def test_wait_stops_on_schedule_failed():
    """schedule_failed never becomes runnable; waiting on it must end."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "schedule_failed"})

    transport = httpx.MockTransport(_route("GET", "/tasks/t-9", handler))
    with BuckerClient("http://test", token="x", transport=transport) as c:
        task = c.wait_for_task("t-9", poll_interval_s=0, timeout_s=1)
    assert task["status"] == "schedule_failed"


def test_schedule_endpoints_shape_params():
    seen = {}

    def post_handler(request: httpx.Request) -> httpx.Response:
        seen["create"] = dict(request.url.params)
        return httpx.Response(200, json={"created": True})

    def pause_handler(request: httpx.Request) -> httpx.Response:
        seen["pause"] = dict(request.url.params)
        return httpx.Response(200, json={"paused": True})

    def router(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/schedules":
            return post_handler(request)
        if request.url.path == "/schedules/nightly/pause":
            return pause_handler(request)
        return httpx.Response(404)

    transport = httpx.MockTransport(router)
    with BuckerClient("http://test", token="x", transport=transport) as c:
        c.create_schedule("nightly", cron="0 9 * * 1-5", template="demo")
        c.pause_schedule("nightly")
    assert seen["create"]["cron"] == "0 9 * * 1-5"
    assert seen["create"]["template"] == "demo"
    assert "resume" not in seen["pause"]

    transport2 = httpx.MockTransport(router)
    with BuckerClient("http://test", token="x", transport=transport2) as c:
        c.resume_schedule("nightly")
    assert seen["pause"].get("resume") == "true"


def test_non_json_error_body_still_maps():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<html>gateway exploded</html>",
                              headers={"content-type": "text/html"})

    transport = httpx.MockTransport(_route("GET", "/tasks/t-1", handler))
    client = BuckerClient("http://test", token="x", transport=transport)
    with client, pytest.raises(ServerError) as info:
        client.get_task("t-1")
    assert "gateway exploded" in str(info.value)


# --------------------------------------------------------------- async ----


async def test_async_client_round_trip_and_wait():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/tasks":
            return httpx.Response(200, json=TASK_BODY)
        # GET /tasks/<any-id> reports the terminal state.
        if request.method == "GET" and request.url.path.startswith("/tasks/"):
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(404, json={"detail": "?"})

    transport = httpx.MockTransport(handler)
    async with AsyncBuckerClient("http://test", token="x",
                                 transport=transport) as c:
        created = await c.create_task(TASK_BODY["objective"])
        assert created["status"] == "pending"
        state = await c.wait_for_task(created["task_id"],
                                      poll_interval_s=0, timeout_s=1)
        assert state["status"] == "completed"


async def test_async_iter_events_pages():
    async def handler(request: httpx.Request) -> httpx.Response:
        after = int(request.url.params.get("after", "0"))
        # page_size=2: first call returns a FULL page, second a short one.
        events = [{"id": 1}, {"id": 2}] if after == 0 else [{"id": 3}]
        return httpx.Response(200, json=events)

    transport = httpx.MockTransport(handler)
    async with AsyncBuckerClient("http://test", token="x",
                                 transport=transport) as c:
        ids = [e["id"] async for e in c.iter_events("t-1", page_size=2)]
    assert ids == [1, 2, 3]


async def test_async_error_mapping():
    """404 maps to NotFoundError through the async surface too."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    transport = httpx.MockTransport(handler)
    async with AsyncBuckerClient("http://test", token="x",
                                 transport=transport) as c:
        raised = False
        try:
            await c.get_task("ghost")
        except NotFoundError as exc:
            raised = True
            assert exc.status_code == 404
            assert "nope" in str(exc)
    assert raised
