"""OpenAI-compatible gateway API tests (hermetic: engine is injected with
SimulatedProviders — no network, no database)."""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient

from bucker.gateway.adapters import SimulatedProvider
from bucker.gateway.quota import QuotaManager
from bucker.gateway.registry import GatewayModel, ModelRegistry
from bucker.gateway.routing import RouterEngine
from tests.conftest import requires_db


class _FakePool:
    """Minimal pool: audit rows work, telemetry raises (and is swallowed)."""

    def __init__(self, healthy: bool = True):
        self.rows = []
        self.healthy = healthy

    async def fetchrow(self, sql, *args):
        self.rows.append((sql, args))
        return {"id": 1}

    async def acquire(self):
        return self

    async def release(self, conn):
        return None

    async def fetch(self, *a, **k):
        if not self.healthy:
            raise RuntimeError("database down")
        return [{"ok": 1}]


class _FakeEventStore:
    def __init__(self):
        self.events = []

    async def append(self, task_id, event_type, payload):
        self.events.append((task_id, event_type, payload))

    @property
    def pool(self):
        return _FakePool()


def _model(canonical_id: str, *, provider: str, priority: int = 100) -> GatewayModel:
    return GatewayModel(
        canonical_id=canonical_id,
        provider=provider,
        provider_model_id=canonical_id.split("/", 1)[1],
        family=canonical_id.split("/", 1)[1],
        context=128_000,
        max_output=8192,
        capabilities=frozenset({"tools", "streaming", "coding"}),
        price_input_per_m=None,
        price_output_per_m=None,
        free=False,
        priority=priority,
    )


def _client(monkeypatch, pool: _FakePool | None = None):
    import sys

    from bucker.api import gateway

    deepseek = SimulatedProvider("deepseek")
    other = SimulatedProvider("b")
    registry = ModelRegistry({
        "deepseek/deepseek-v4-flash": _model(
            "deepseek/deepseek-v4-flash", provider="deepseek", priority=0
        ),
        "b/model-2": _model("b/model-2", provider="b", priority=1),
    })
    engine = RouterEngine(
        registry=registry,
        adapters={"deepseek": deepseek, "b": other},
        quota=QuotaManager(),  # no pool -> quota checks are no-ops
        policy="priority",
        deadline_s=30.0,
        timeout_s=5.0,
    )
    monkeypatch.setattr(gateway, "_engine", engine)

    from bucker.api.app import app

    mod = sys.modules["bucker.api.app"]
    mod._pool = pool or _FakePool()
    mod._store = _FakeEventStore()
    mod._snaps = None
    return TestClient(app), deepseek, other


def _auth() -> dict:
    from bucker.config import settings

    return {"Authorization": f"Bearer {settings.api_token}"}


def test_gateway_requires_token(monkeypatch):
    c, _, _ = _client(monkeypatch)
    resp = c.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


def test_gateway_returns_openai_shape(monkeypatch):
    c, _, _ = _client(monkeypatch)
    resp = c.post(
        "/v1/chat/completions",
        headers=_auth(),
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == \
        "hello from deepseek/deepseek-v4-flash"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] >= 1
    assert body["model"].startswith("deepseek")
    assert uuid.UUID(body["task_id"])
    assert resp.headers.get("x-request-id")


def test_gateway_validates_body(monkeypatch):
    c, _, _ = _client(monkeypatch)
    resp = c.post(
        "/v1/chat/completions", headers=_auth(), json={"messages": []}
    )
    assert resp.status_code == 422
    resp = c.post(
        "/v1/chat/completions", headers=_auth(),
        json={"messages": [{"role": "admin", "content": "hi"}]},
    )
    assert resp.status_code == 422


def test_gateway_tool_call_shape(monkeypatch):
    c, deepseek, _ = _client(monkeypatch)
    deepseek.script("deepseek-v4-flash", "tool_call")
    resp = c.post(
        "/v1/chat/completions",
        headers=_auth(),
        json={
            "messages": [{"role": "user", "content": "run it"}],
            "tools": [{"type": "function",
                       "function": {"name": "run_shell", "parameters": {}}}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    tc = body["choices"][0]["message"]["tool_calls"][0]
    assert tc["id"] == "call_deepseek"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "run_shell"
    assert json.loads(tc["function"]["arguments"]) == {"cmd": "echo hi"}


def test_gateway_streams_sse(monkeypatch):
    c, _, _ = _client(monkeypatch)
    with c.stream(
        "POST", "/v1/chat/completions",
        headers=_auth(),
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = [ln for ln in resp.iter_lines() if ln]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
    texts = []
    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                texts.append(delta["content"])
    assert "".join(texts) == "hello from deepseek"
    assert chunks[-1].get("usage", {}).get("total_tokens", 0) >= 1


def test_gateway_streams_tool_call_deltas(monkeypatch):
    c, deepseek, _ = _client(monkeypatch)
    deepseek.script("deepseek-v4-flash", "stream_tool_call")
    with c.stream(
        "POST", "/v1/chat/completions",
        headers=_auth(),
        json={
            "messages": [{"role": "user", "content": "run it"}],
            "stream": True,
            "tools": [{"type": "function",
                       "function": {"name": "run_shell", "parameters": {}}}],
        },
    ) as resp:
        lines = [ln for ln in resp.iter_lines() if ln]
    chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
    args = ""
    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                args += tc["function"]["arguments"]
    assert json.loads(args) == {"cmd": "echo hi"}


def test_gateway_normalized_error_no_internal_leak(monkeypatch):
    c, deepseek, other = _client(monkeypatch)
    deepseek.script("deepseek-v4-flash", "server_error")
    other.script("model-2", "server_error")
    resp = c.post(
        "/v1/chat/completions", headers=_auth(),
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 503
    err = resp.json()["error"]
    assert err["type"] == "all_providers_failed_error"
    assert "all providers failed" in err["message"]
    # Safe message never contains provider internals or raw error text.
    assert "HTTP 500" not in err["message"]


def test_gateway_unknown_model_400(monkeypatch):
    c, _, _ = _client(monkeypatch)
    resp = c.post(
        "/v1/chat/completions", headers=_auth(),
        json={"messages": [{"role": "user", "content": "hi"}],
              "model": "nope/missing"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_models_endpoint_lists_routable_models(monkeypatch):
    from bucker.config import settings

    c, _, _ = _client(monkeypatch)
    resp = c.get("/v1/models", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert settings.model in ids
    assert "b/model-2" in ids


def test_health_live(monkeypatch):
    c, _, _ = _client(monkeypatch)
    resp = c.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_ready_reflects_database(monkeypatch):
    c, _, _ = _client(monkeypatch, pool=_FakePool(healthy=True))
    resp = c.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"

    c2, _, _ = _client(monkeypatch, pool=_FakePool(healthy=False))
    resp = c2.get("/health/ready")
    assert resp.status_code == 503


@requires_db
async def test_gateway_audit_chain_satisfies_both_fks(pool):
    """Regression: the audit chain must satisfy BOTH foreign keys. The old
    code inserted the tasks row, returned ITS id, and telemetry silently
    never wrote (telemetry.event_id references events.id, not tasks.id).
    Order matters too: events.task_id references tasks.id, so the tasks
    row must exist before the event append."""
    import uuid

    from bucker.api.gateway import _audit_start, _audit_telemetry
    from bucker.core.eventstore import EventStore

    store = EventStore(pool)
    tid = uuid.uuid4()
    event_id = await _audit_start(pool, store, tid, "audit chain test")
    assert event_id is not None

    await _audit_telemetry(
        pool, event_id, tid,
        model="audit/test-model", latency_ms=5, cost_usd=0.0001,
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )
    rows = await pool.fetch(
        "SELECT model_used, prompt_tokens FROM telemetry WHERE purpose = 'gateway'"
    )
    assert len(rows) == 1
    assert rows[0]["model_used"] == "audit/test-model"
    # The event exists and telemetry really references it.
    events = await pool.fetchrow("SELECT id FROM events WHERE id = $1", event_id)
    assert events is not None
