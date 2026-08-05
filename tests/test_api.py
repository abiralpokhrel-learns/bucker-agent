"""API tests (step 24).

Tests the FastAPI routes using mocked dependencies. No Postgres needed.
"""

from __future__ import annotations

from datetime import UTC
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from bucker.config import settings


class FakeConn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, sql, *args):
        pass

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        return 0

    async def fetchrow(self, sql, *args):
        return None


class FakePool:
    def acquire(self):
        """Return an async-context-manager-compatible connection. NOT async —
        asyncpg.Pool.acquire() is synchronous and returns an awaitable context
        manager, not a coroutine."""
        return FakeConn()

    async def close(self):
        pass

    async def fetchval(self, sql, *args):
        return 0


class FakeEventStore:
    def __init__(self):
        self._events: list = []
        self._pool = FakePool()  # review_task reads task rows via store._pool

    async def read_stream(self, task_id, after_id=0, limit=None):
        return self._events

    async def append(self, task_id=None, event_type="TaskCreated", payload=None,
                     **kwargs):
        from datetime import datetime

        from bucker.core.eventstore import Event

        e = Event(
            id=len(self._events) + 1,
            task_id=task_id or uuid4(),
            event_type=event_type,
            payload=payload or {},
            schema_version=1,
            created_at=datetime.now(UTC),
        )
        self._events.append(e)
        return e

    async def last_event_id(self, task_id):
        return 0

    async def count(self, task_id):
        return len(self._events)


class FakeSnapshots:
    async def get_state(self, task_id):
        return {"status": "pending"}


# ------------------------------------------------------------ helpers -------


def _inject_fakes():
    """Replace the app module globals with fakes."""
    import sys

    import bucker.api  # noqa: F401 — force module load

    mod = sys.modules["bucker.api.app"]
    mod._pool = FakePool()
    mod._store = FakeEventStore()
    mod._snaps = FakeSnapshots()
    mod._blobs = None


def _clear_fakes():
    import sys

    mod = sys.modules.get("bucker.api.app")
    if mod is not None:
        mod._pool = None
        mod._store = None
        mod._snaps = None


# ------------------------------------------------------------ test client --


@pytest.fixture
def client():
    """TestClient with mocked store."""
    _inject_fakes()

    import sys
    api_app = sys.modules["bucker.api.app"].app

    with TestClient(api_app, raise_server_exceptions=False) as c:
        yield c

    _clear_fakes()


# ---------------------------------------------------------------- routes ---


def test_create_task_returns_task_id(client):
    resp = client.post(
        "/tasks",
        params={"objective": "build something useful for a change"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "task_id" in data
    assert data["status"] == "pending"


def test_create_task_defaults_to_real_pipeline(client):
    """The default task_type starts CodeTaskWorkflow, not the demo workflow.

    Temporal is unreachable in tests, so workflow_id is None — but the task
    must be created and the request must not fail.
    """
    resp = client.post(
        "/tasks",
        params={"objective": "add a subtract function to calc.py", "max_retries": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_type"] == "code_change"
    assert data["workflow_id"] is None  # Temporal down in tests, not fatal


def test_create_task_accepts_guardrails(client):
    resp = client.post(
        "/tasks",
        params={
            "objective": "fix the failing test in test_calc.py",
            "budget_usd": 0.25,
            "deadline_minutes": 10,
            "max_retries": 1,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


def test_create_demo_task_still_supported(client):
    resp = client.post(
        "/tasks",
        params={"objective": "run the five fake steps", "task_type": "demo"},
    )
    assert resp.status_code == 200
    assert resp.json()["task_type"] == "demo"


def test_create_task_rejects_short_objective(client):
    resp = client.post("/tasks", params={"objective": "short"})
    assert resp.status_code == 422


def test_create_task_rejects_negative_budget(client):
    resp = client.post(
        "/tasks",
        params={"objective": "a sufficiently long objective", "budget_usd": -1},
    )
    assert resp.status_code == 422


def test_get_task_404_for_unknown_id(client):
    resp = client.get(f"/tasks/{uuid4()}")
    assert resp.status_code == 404


def test_get_events_returns_list(client):
    resp = client.get(f"/tasks/{uuid4()}/events")
    assert resp.status_code == 200
    assert resp.json() == []


# ------------------------------------------------------------ task list ----


def test_list_tasks_returns_payload(client):
    resp = client.get("/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    assert data["tasks"] == []


def test_list_tasks_accepts_status_filter(client):
    resp = client.get("/tasks", params={"status": "completed", "limit": 10})
    assert resp.status_code == 200
    assert data_has_tasks_shape(resp.json())


def data_has_tasks_shape(data: dict) -> bool:
    return "total" in data and isinstance(data["tasks"], list)


# ---------------------------------------------------------------- pages ----


def test_index_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "bucker-agent" in resp.text
    assert "no tasks yet" in resp.text


def test_new_task_page_renders(client):
    resp = client.get("/tasks/new")
    assert resp.status_code == 200
    assert "Create a task" in resp.text
    assert "code_change" in resp.text


def test_task_dashboard_renders_for_existing_task(client):
    import sys
    from datetime import UTC, datetime

    from bucker.core.eventstore import Event

    task_id = uuid4()
    mod = sys.modules["bucker.api.app"]
    mod._store._events.append(Event(
        id=1, task_id=task_id, event_type="TaskCreated",
        payload={"objective": "do the thing"}, schema_version=1,
        created_at=datetime.now(UTC),
    ))

    resp = client.get(f"/tasks/{task_id}/dashboard")
    assert resp.status_code == 200
    assert "Event stream" in resp.text
    # The timeline renders a friendly label for TaskCreated, not the raw type.
    assert "no events yet" not in resp.text
    assert "pending" in resp.text  # fake snapshot state shows a status badge


def test_task_dashboard_404_for_unknown_task(client):
    resp = client.get(f"/tasks/{uuid4()}/dashboard")
    assert resp.status_code == 404


def test_replay_page_renders(client):
    resp = client.get(f"/tasks/{uuid4()}/replay")
    assert resp.status_code == 200
    assert "Run replay" in resp.text
    assert "Deterministic replay" in resp.text


def test_openapi_schema_loads(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    paths = schema["paths"]
    assert "/tasks" in paths
    assert "/tasks/{task_id}" in paths
    assert "/tasks/{task_id}/events" in paths
    assert "/tasks/{task_id}/replay" in paths
    assert "/tasks/{task_id}/rerun" in paths
    assert "/tasks/{task_id}/cancel" in paths
    assert "/system" in paths


# ----------------------------------------------------- rerun / cancel / system --


class FakeConnWithTask(FakeConn):
    """A connection whose task lookup returns a real row."""

    async def fetchrow(self, sql, *args):
        return {
            "task_type": "code_change",
            "objective": "add a subtract function to calc.py",
            "budget_usd": 0.5,
        }


class FakePoolWithTask(FakePool):
    def acquire(self):
        return FakeConnWithTask()


class FakeUsageConn(FakeConn):
    """A connection that answers the usage queries with empty-but-valid rows."""

    async def fetchrow(self, sql, *args):
        return {"tokens": 0, "cost": 0, "calls": 0}

    async def fetch(self, sql, *args):
        return []


class FakeReviewConn(FakeConn):
    """A connection whose task lookup returns an escalated task row."""

    def __init__(self, status: str = "needs_human_review"):
        self._status = status

    async def fetchrow(self, sql, *args):
        from datetime import UTC, datetime

        return {
            "id": "11111111-2222-3333-4444-555555555555",
            "task_type": "code_change",
            "objective": "add subtract",
            "status": self._status,
            "verifier": "python_test_runner",
            "budget_usd": 0.5,
            "cost_usd": 0.1,
            "total_tokens": 100,
            "event_count": 5,
            "created_at": datetime.now(UTC),
        }


class FakeReviewPool(FakePool):
    def __init__(self, status: str = "needs_human_review"):
        self._status = status

    def acquire(self):
        return FakeReviewConn(self._status)


# -------------------------------------------------- human review API --


def _inject_review_fakes(monkeypatch, status="needs_human_review"):
    import sys

    import bucker.api  # noqa: F401

    mod = sys.modules["bucker.api.app"]
    monkeypatch.setattr(mod, "_pool", FakeReviewPool(status))
    monkeypatch.setattr(mod, "_store", FakeEventStore())
    # review_task reads task rows via store._pool — point it at the review pool.
    mod._store._pool = FakeReviewPool(status)
    monkeypatch.setattr(mod, "_snaps", FakeSnapshots())
    return mod


def test_approve_escalated_task(client, monkeypatch):
    """Approve an escalated task: append-only review, status flips."""
    mod = _inject_review_fakes(monkeypatch)

    resp = client.post(
        "/tasks/11111111-2222-3333-4444-555555555555/approve",
        params={"note": "looks correct to me"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "human_approved"
    assert data["note"] == "looks correct to me"

    # The review is append-only: the fake store saw a HumanApproved event.
    store = mod._store
    types = [e.event_type for e in store._events]
    assert "HumanApproved" in types


def test_reject_escalated_task(client, monkeypatch):
    mod = _inject_review_fakes(monkeypatch)

    resp = client.post(
        "/tasks/11111111-2222-3333-4444-555555555555/reject",
        params={"note": "wrong approach"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "human_rejected"
    assert "HumanRejected" in [e.event_type for e in mod._store._events]


def test_review_rejects_non_escalated_task(client, monkeypatch):
    """Only needs_human_review tasks can be reviewed — 409 otherwise."""
    _inject_review_fakes(monkeypatch, status="in_progress")

    resp = client.post(
        "/tasks/11111111-2222-3333-4444-555555555555/approve"
    )
    assert resp.status_code == 409
    assert "needs_human_review" in resp.json()["detail"]


def test_review_missing_task_is_404(client, monkeypatch):
    """Unknown task id -> 404, not a crash."""
    import sys

    import bucker.api  # noqa: F401

    mod = sys.modules["bucker.api.app"]
    monkeypatch.setattr(mod, "_pool", FakePool())
    monkeypatch.setattr(mod, "_store", FakeEventStore())  # fetchrow -> None
    monkeypatch.setattr(mod, "_snaps", FakeSnapshots())

    resp = client.post(
        "/tasks/11111111-2222-3333-4444-555555555555/reject"
    )
    assert resp.status_code == 404


class FakeUsagePool(FakePool):
    def acquire(self):
        return FakeUsageConn()


def _inject_fakes_with_usage():
    import sys

    import bucker.api  # noqa: F401

    mod = sys.modules["bucker.api.app"]
    mod._pool = FakeUsagePool()
    mod._store = FakeEventStore()
    mod._snaps = FakeSnapshots()
    mod._blobs = None


def test_usage_page_renders_empty_state():
    import sys

    _inject_fakes_with_usage()
    api_app = sys.modules["bucker.api.app"].app
    try:
        with TestClient(api_app, raise_server_exceptions=False) as c:
            resp = c.get("/usage")
            assert resp.status_code == 200
            assert "Total tokens" in resp.text
            assert "Tokens by model" in resp.text
            assert "By pipeline stage" in resp.text
            assert "no model calls recorded yet" in resp.text
    finally:
        _clear_fakes()


def test_usage_json_has_all_sections():
    import sys

    _inject_fakes_with_usage()
    api_app = sys.modules["bucker.api.app"].app
    try:
        with TestClient(api_app, raise_server_exceptions=False) as c:
            resp = c.get("/api/usage")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_tokens"] == 0
            assert data["total_calls"] == 0
            assert data["by_model"] == []
            assert data["by_purpose"] == []
            assert data["per_day"] == []
    finally:
        _clear_fakes()


def test_index_shows_tokens_card(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Total tokens" in resp.text  # the usage card on the landing page


# ------------------------------------------- failure-state dashboard views --


def test_task_dashboard_shows_full_verifier_output():
    """A failed task must show the FULL diagnostics, not just the alert."""
    from bucker.api.dashboard import render_task_dashboard

    state = {
        "status": "verification_failed",
        "last_verification": {
            "passed": False,
            "diagnostics": "2 failed, 1 passed (truncated preview)",
        },
    }
    html = render_task_dashboard(
        str(uuid4()),
        state,
        [],
        verifier_output="=== FAILURES ===\nassert subtract(5, 3) == 2\nthe full retry prompt",
    )
    assert "Verifier output" in html
    assert "the full retry prompt" in html  # the FULL diagnostics, not truncated


def test_task_dashboard_lists_failed_model_calls():
    from bucker.api.dashboard import render_task_dashboard

    html = render_task_dashboard(
        str(uuid4()),
        {"status": "failed"},
        [],
        failed_model_calls=["AuthenticationError: 401 User not found"],
    )
    assert "Failed model calls" in html
    assert "401 User not found" in html


def test_task_dashboard_omits_panels_when_clean():
    """A passing task has no failure panels at all."""
    from bucker.api.dashboard import render_task_dashboard

    html = render_task_dashboard(
        str(uuid4()),
        {"status": "completed", "last_verification": {"passed": True}},
        [],
        verifier_output="3 passed",
    )
    assert "Verifier output" not in html  # passed -> no retry-prompt panel
    assert "Failed model calls" not in html


def _inject_fakes_with_task():
    import sys

    import bucker.api  # noqa: F401

    mod = sys.modules["bucker.api.app"]
    mod._pool = FakePoolWithTask()
    mod._store = FakeEventStore()
    mod._snaps = FakeSnapshots()
    mod._blobs = None


def test_rerun_creates_a_new_task_with_same_objective():
    import sys

    _inject_fakes_with_task()
    api_app = sys.modules["bucker.api.app"].app
    try:
        with TestClient(api_app, raise_server_exceptions=False) as c:
            resp = c.post(f"/tasks/{uuid4()}/rerun")
            assert resp.status_code == 200
            data = resp.json()
            assert data["task_id"] != data["original_task_id"]
            assert data["status"] == "pending"
            assert data["workflow_id"] is None  # Temporal down in tests, not fatal
    finally:
        _clear_fakes()


def test_rerun_404_for_unknown_task(client):
    resp = client.post(f"/tasks/{uuid4()}/rerun")
    assert resp.status_code == 404


def test_cancel_requires_temporal(client):
    """Without Temporal reachable, cancel explains rather than faking success."""
    resp = client.post(f"/tasks/{uuid4()}/cancel")
    assert resp.status_code == 409
    assert "could not cancel" in resp.json()["detail"]


def test_system_page_renders(client):
    """The control center renders; probes degrade gracefully in tests.

    Postgres is a fake (SELECT 1 -> 0), Docker may or may not be up, and
    Temporal is unreachable — every check must still render as a row.
    """
    resp = client.get("/system")
    assert resp.status_code == 200
    assert "control center" in resp.text
    assert "Model" in resp.text
    assert "Infrastructure" in resp.text
    assert "Verifiers" in resp.text


def test_system_json_has_model_and_infra(client):
    resp = client.get("/api/system")
    assert resp.status_code == 200
    data = resp.json()
    assert "model" in data
    assert "infra" in data
    assert "providers" in data
    assert "platform" in data
    # The configured primary model is reported, never a secret.
    assert data["model"]["primary"] == settings.model


def test_system_page_never_leaks_the_api_key(client):
    """Secret hygiene: the status page reports key *shape*, never the value."""
    import sys

    mod = sys.modules["bucker.api.app"]
    api_app = mod.app

    original_token = settings.api_token
    object.__setattr__(settings, "api_token", "super-secret-token-value")
    _inject_fakes()
    try:
        with TestClient(api_app, raise_server_exceptions=False) as c:
            # A key-shaped string must never appear in the rendered page.
            resp = c.get("/system")
            assert "super-secret-token-value" not in resp.text
    finally:
        _clear_fakes()
        object.__setattr__(settings, "api_token", original_token)


# ---------------------------------------------------------------- auth ----


def test_auth_required_when_not_dev_token():
    """When the API token is not dev-token, unauthenticated requests are 401."""
    import sys

    import bucker.api  # noqa: F401

    original_token = settings.api_token
    object.__setattr__(settings, "api_token", "secret-token")
    _inject_fakes()

    api_app = sys.modules["bucker.api.app"].app

    try:
        with TestClient(api_app, raise_server_exceptions=False) as c:
            resp = c.get(f"/tasks/{uuid4()}")
            assert resp.status_code == 401
    finally:
        _clear_fakes()
        object.__setattr__(settings, "api_token", original_token)


def test_auth_passes_with_correct_token():
    """Bearer token matching the configured token is accepted."""
    import sys

    import bucker.api  # noqa: F401

    original_token = settings.api_token
    object.__setattr__(settings, "api_token", "secret-token")
    _inject_fakes()

    api_app = sys.modules["bucker.api.app"].app

    try:
        with TestClient(api_app, raise_server_exceptions=False) as c:
            resp = c.get(
                f"/tasks/{uuid4()}",
                headers={"Authorization": "Bearer secret-token"},
            )
            assert resp.status_code == 404
    finally:
        _clear_fakes()
        object.__setattr__(settings, "api_token", original_token)


def test_dev_token_refuses_non_localhost_hosts():
    """With the dev-token bypass active, only localhost hosts are accepted.

    A casually exposed server (bound to 0.0.0.0, reached by LAN IP or
    hostname) must not be unauthenticated-by-default.
    """
    import sys

    import bucker.api  # noqa: F401

    _inject_fakes()
    api_app = sys.modules["bucker.api.app"].app
    try:
        with TestClient(api_app, raise_server_exceptions=False) as c:
            # Localhost still works in dev mode.
            local = c.get("/", headers={"host": "localhost:8000"})
            assert local.status_code == 200
            # A non-local host is refused even though dev-token is active.
            evil = c.get("/", headers={"host": "bucker.example.com"})
            assert evil.status_code == 401
            assert "dev default" in evil.json()["detail"]
    finally:
        _clear_fakes()


def test_dev_token_guard_skipped_when_real_token_set():
    """With a real token, non-localhost hosts are NOT special-cased."""
    import sys

    import bucker.api  # noqa: F401

    original_token = settings.api_token
    object.__setattr__(settings, "api_token", "real-secret")
    _inject_fakes()
    api_app = sys.modules["bucker.api.app"].app
    try:
        with TestClient(api_app, raise_server_exceptions=False) as c:
            resp = c.get(
                "/",
                headers={
                    "host": "bucker.example.com",
                    "Authorization": "Bearer real-secret",
                },
            )
            # Reaches the route (page renders); auth is per-token, not per-host.
            assert resp.status_code == 200
            # Without the token it is still rejected, host notwithstanding.
            denied = c.get("/", headers={"host": "bucker.example.com"})
            assert denied.status_code == 401
    finally:
        _clear_fakes()
        object.__setattr__(settings, "api_token", original_token)


# ------------------------------------------------------- templates / schedules --


def test_templates_endpoint_lists_presets(client):
    """GET /templates is pure — no DB, no Temporal."""
    resp = client.get("/templates")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()["templates"]]
    assert "code-fix" in ids and "research" in ids


def test_models_endpoint_has_catalog_and_chain(client):
    """GET /api/models: catalog tiers + configured chain annotations."""
    resp = client.get("/api/models")
    assert resp.status_code == 200
    data = resp.json()
    tiers = {m["tier"] for m in data["catalog"]}
    assert tiers == {"local", "free", "paid"}
    chain = data["configured_chain"]
    assert isinstance(chain, list)
    for entry in chain:
        assert "id" in entry and "tier" in entry and "provider" in entry
    assert "suggested_chain" in data


def test_models_page_renders_tiers(client):
    resp = client.get("/models-page")
    assert resp.status_code == 200
    assert "Model catalog" in resp.text
    assert "local" in resp.text and "free" in resp.text and "paid" in resp.text


def test_task_page_shows_critique_and_graph_panels():
    """The harness loop features are visible on the task page (iter 9)."""
    from bucker.api.dashboard import render_task_dashboard

    events = [
        {"event_type": "CritiqueCompleted", "payload": {
            "attempt": 1, "verdict": "needs_fix",
            "issues": ["bad hunk count"], "repaired": True}},
        {"event_type": "GraphStepCompleted", "payload": {
            "step_id": "add-sub", "status": "completed"}},
    ]
    html = render_task_dashboard(
        "11111111-2222-3333-4444-555555555555",
        {"status": "completed", "plan": None, "last_verification": {}},
        events,
    )
    assert "Self-critique loop" in html
    assert "needs_fix" in html and "repaired" in html
    assert "Graph steps" in html and "add-sub" in html


# --------------------------------------------------- memory / skills API --


def test_memory_api_add_and_list(client, tmp_path):
    """Semantic memory over HTTP: store + list + search."""
    from bucker.memory.facts import MemoryStore

    orig_root = MemoryStore.default_root
    MemoryStore.default_root = tmp_path / "memory"
    try:
        resp = client.post("/memory", params={"text": "tests run with pytest"})
        assert resp.status_code == 200
        assert resp.json()["stored"] is True

        listed = client.get("/memory")
        assert listed.status_code == 200
        assert listed.json()["count"] == 1

        found = client.get("/memory", params={"q": "pytest"})
        assert found.json()["count"] == 1
        missed = client.get("/memory", params={"q": "nonexistent"})
        assert missed.json()["count"] == 0
    finally:
        MemoryStore.default_root = orig_root


def test_skills_api_create_and_list(client, tmp_path):
    """Procedural memory over HTTP: create + list + fetch."""
    from bucker.memory.skills import SkillStore

    orig_root = SkillStore.default_root
    SkillStore.default_root = tmp_path / "skills"
    try:
        resp = client.post(
            "/skills",
            params={
                "name": "fix-tests",
                "description": "Repair a failing test suite",
                "procedure": "1. run tests\\n2. fix root cause",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["created"] is True

        listed = client.get("/skills")
        assert listed.json()["skills"][0]["name"] == "fix-tests"

        one = client.get("/skills/fix-tests")
        assert one.json()["skill"]["description"].startswith("Repair")

        bad = client.post(
            "/skills",
            params={"name": "Bad Name!", "description": "desc", "procedure": "proc"},
        )
        assert bad.status_code == 400
    finally:
        SkillStore.default_root = orig_root


def test_memory_and_skills_pages_render(client, tmp_path):
    from bucker.memory.facts import MemoryStore

    orig_root = MemoryStore.default_root
    MemoryStore.default_root = tmp_path / "memory"
    try:
        resp = client.get("/memory-page")
        assert resp.status_code == 200
        assert "semantic memory" in resp.text.lower() or "Facts" in resp.text
        resp = client.get("/skills-page")
        assert resp.status_code == 200
        assert "Add a skill" in resp.text
    finally:
        MemoryStore.default_root = orig_root


# --------------------------------------------------------- graphs API --


def test_graphs_endpoint_rejects_invalid_spec(client):
    """Validation happens before any DB/Temporal work — pure 400s."""
    resp = client.post("/graphs", json={"name": "x", "steps": [
        {"id": "a", "objective": "1", "depends_on": ["ghost"]},
    ]})
    assert resp.status_code == 400
    assert "not runnable" in resp.json()["detail"]

    resp = client.post("/graphs", json={"name": "x", "steps": [
        {"id": "a", "objective": "1"},
        {"id": "b", "objective": "2", "depends_on": ["a"]},
        {"id": "a", "objective": "3"},
    ]})
    assert resp.status_code == 400

    resp = client.post("/graphs", json={"steps": []})
    assert resp.status_code == 400


def test_graphs_endpoint_degraded_mode(client, monkeypatch):
    """With the pool down, the API answers 503, not a crash."""
    import sys

    # bucker/api/__init__.py re-exports the FastAPI instance as `app`, so
    # `import bucker.api.app as x` binds the instance — go through
    # sys.modules to reach the real module.
    app_mod = sys.modules["bucker.api.app"]
    orig = app_mod._degraded
    app_mod._degraded = True
    try:
        resp = client.post("/graphs", json={
            "name": "x", "steps": [{"id": "a", "objective": "1"}],
        })
        assert resp.status_code == 503
    finally:
        app_mod._degraded = orig


def test_new_task_page_renders_template_cards(client):
    resp = client.get("/tasks/new")
    assert resp.status_code == 200
    assert "Start from a template" in resp.text
    assert "applyTemplate" in resp.text


def test_schedules_list_503_when_temporal_down(client):
    """Schedules live in Temporal; without it the API says so clearly."""
    resp = client.get("/schedules")
    assert resp.status_code == 503
    assert "Temporal unreachable" in resp.json()["detail"]


def test_schedules_create_rejects_unknown_template(client):
    resp = client.post(
        "/schedules",
        params={
            "schedule_id": "nightly",
            "cron": "0 9 * * *",
            "template": "does-not-exist",
        },
    )
    assert resp.status_code == 400
    assert "unknown template" in resp.json()["detail"]


def test_schedules_page_renders_without_temporal(client):
    """The page degrades gracefully — an alert, not a 500."""
    resp = client.get("/schedules-page")
    assert resp.status_code == 200
    assert "Temporal is not reachable" in resp.text
    assert "Create a schedule" in resp.text


# ------------------------------------------------------- degraded mode -----


def test_data_routes_answer_503_without_a_pool():
    """Startup pool creation failed -> a clear 503, not a bare 500 or crash."""
    import sys

    import bucker.api  # noqa: F401

    api_app = sys.modules["bucker.api.app"].app
    mod = sys.modules["bucker.api.app"]

    async def _fail_pool(*args, **kwargs):
        raise ConnectionError("test: postgres deliberately down")

    original_create = mod.create_pool
    original_pool = mod._pool
    original_degraded = mod._degraded
    mod.create_pool = _fail_pool
    mod._pool = None
    mod._degraded = False
    try:
        with TestClient(api_app, raise_server_exceptions=False) as c:
            resp = c.get("/")
            assert resp.status_code == 503
            assert "database not initialised" in resp.json()["detail"]
            # API routes that touch the DB answer the same way.
            api = c.get("/tasks")
            assert api.status_code == 503
            # The app booted in degraded mode and can still say so.
            system = c.get("/system")
            assert system.status_code == 200
            assert "DEGRADED MODE" in system.text
    finally:
        mod.create_pool = original_create
        mod._pool = original_pool
        mod._degraded = original_degraded


def test_system_page_shows_degraded_banner():
    """When startup failed, /system explains the degraded state visibly."""
    import sys

    import bucker.api  # noqa: F401

    api_app = sys.modules["bucker.api.app"].app
    mod = sys.modules["bucker.api.app"]

    async def _fail_pool(*args, **kwargs):
        raise ConnectionError("test: postgres deliberately down")

    original_create = mod.create_pool
    original_pool = mod._pool
    original_degraded = mod._degraded
    mod.create_pool = _fail_pool
    mod._pool = None
    mod._degraded = False
    try:
        with TestClient(api_app, raise_server_exceptions=False) as c:
            resp = c.get("/system")
            assert resp.status_code == 200
            assert "DEGRADED MODE" in resp.text
            assert "database pool" in resp.text
            # The JSON API exposes the flag too.
            status = c.get("/api/system")
            assert status.json()["degraded"] is True
    finally:
        mod.create_pool = original_create
        mod._pool = original_pool
        mod._degraded = original_degraded
