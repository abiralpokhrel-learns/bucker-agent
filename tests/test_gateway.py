"""OpenAI-compatible gateway tests (hermetic: RouterClient is patched)."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


class _FakeResp:
    text = "hello from the fake model"
    model = "deepseek/deepseek-v4-flash"
    cost_usd = 0.0001
    latency_ms = 42
    raw_ref = "blob:fake"


class _FakePool:
    """Minimal pool: fetchrow works, telemetry raises (and is swallowed)."""

    def __init__(self):
        self.rows = []

    async def fetchrow(self, sql, *args):
        self.rows.append((sql, args))
        return {"id": 1}

    async def acquire(self):
        return self

    async def release(self, conn):
        return None

    async def fetch(self, *a, **k):
        raise RuntimeError("no telemetry in tests")


class _FakeEventStore:
    def __init__(self):
        self.events = []

    async def append(self, task_id, event_type, payload):
        self.events.append((task_id, event_type, payload))

    @property
    def pool(self):
        return _FakePool()


def _client(monkeypatch) -> TestClient:
    import sys

    from bucker.api import gateway

    async def _fake_complete(self, messages, **kwargs):
        return _FakeResp()

    monkeypatch.setattr(gateway.ModelRouter, "complete", _fake_complete)
    from bucker.api.app import app

    mod = sys.modules["bucker.api.app"]
    mod._pool = _FakePool()
    mod._store = _FakeEventStore()
    mod._snaps = None
    return TestClient(app)


def test_gateway_requires_token(monkeypatch):
    c = _client(monkeypatch)
    resp = c.post("/v1/chat/completions",
                  json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 401


def test_gateway_returns_openai_shape(monkeypatch):
    from bucker.config import settings

    c = _client(monkeypatch)
    resp = c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello from the fake model"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] >= 1
    assert body["model"].startswith("deepseek")
    assert uuid.UUID(body["task_id"])


def test_gateway_validates_body(monkeypatch):
    from bucker.config import settings

    c = _client(monkeypatch)
    # empty messages list → 422
    resp = c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={"messages": []},
    )
    assert resp.status_code == 422
    # bad role → 422
    resp = c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={"messages": [{"role": "admin", "content": "hi"}]},
    )
    assert resp.status_code == 422


def test_gateway_stream_unsupported(monkeypatch):
    from bucker.config import settings

    c = _client(monkeypatch)
    resp = c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 400


def test_models_endpoint_lists_chain(monkeypatch):
    from bucker.config import settings

    c = _client(monkeypatch)
    resp = c.get("/v1/models",
                 headers={"Authorization": f"Bearer {settings.api_token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert settings.model in ids
    assert len(body["data"]) >= 1
